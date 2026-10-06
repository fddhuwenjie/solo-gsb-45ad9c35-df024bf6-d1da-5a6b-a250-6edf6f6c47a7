"""定义集迁移包（export/import）的端到端测试。

覆盖：

- POST /custom-unit-set-migration/export：导出 1～6 个已发布定义集，以及导出
  时刻指向它们的活动别名；未指向包内定义集的别名不导出；
- 标识不存在、重复、数量越界返回 422；
- 导出的定义与别名指向来自同一把锁内的同一份快照（并发切换不跨快照拼装）；
- POST /custom-unit-set-migration/import：整包校验通过后生成新本地标识、
  别名改指新标识，返回旧→新标识映射；
- 定义非法、包内别名悬空、别名非法、目标端已有同名别名：整包拒绝，
  不留下部分定义或别名；
- 导入与落盘共同生效：写盘失败回滚内存中的全部新增内容；容器重建后
  新引用仍可使用；导入不影响原有定义、别名与调用；
- 导出响应可原样作为导入请求体（HTTP 层往返）。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app import unit_sets
from app.schemas import CustomUnitItem

client = TestClient(app)

EXPORT_PATH = "/custom-unit-set-migration/export"
IMPORT_PATH = "/custom-unit-set-migration/import"


def cu(symbol, factor, base_unit):
    return {"symbol": symbol, "factor": factor, "base_unit": base_unit}


def publish(defs):
    r = client.post("/custom-unit-sets", json={"custom_units": defs})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def bind(alias, set_id, expected_id):
    r = client.put(
        f"/custom-unit-set-aliases/{alias}",
        json={"custom_unit_set_id": set_id, "expected_id": expected_id},
    )
    assert r.status_code == 200, r.text
    return r


def export(set_ids):
    return client.post(EXPORT_PATH, json={"custom_unit_set_ids": list(set_ids)})


def use_fresh_store(tmp_path, name="dst"):
    """把进程级单例切换到一个全新的空目标实例存储（模拟另一实例）。"""

    dst = unit_sets.UnitSetStore(str(tmp_path / name))
    dst.load()
    unit_sets._store = dst
    return dst


def assert_err(resp, code, status=422):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["error"] == code


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def test_export_returns_package_with_sets_and_pointing_aliases():
    s1 = publish([cu("ft", "0.3048", "m")])
    s2 = publish([cu("chain", "22", "m"), cu("furlong", "10", "chain")])
    s_other = publish([cu("rod", "5.0292", "m")])
    bind("lengths", s1, None)
    bind("ranges", s2, None)
    bind("other", s_other, None)

    r = export([s1, s2])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "unit-api.custom-unit-set-migration"
    assert body["version"] == 1
    # 定义内容与顺序按提交返回，键为源实例标识
    assert list(body["sets"]) == [s1, s2]
    assert body["sets"][s1] == [cu("ft", "0.3048", "m")]
    assert body["sets"][s2] == [
        cu("chain", "22", "m"),
        cu("furlong", "10", "chain"),
    ]
    # 仅导出指向包内定义集的别名；指向 s_other 的 "other" 不在包内
    assert body["aliases"] == {"lengths": s1, "ranges": s2}


def test_export_single_set_without_aliases():
    sid = publish([cu("dozen", "12", "1")])
    body = export([sid]).json()
    assert list(body["sets"]) == [sid]
    assert body["aliases"] == {}


def test_export_nonexistent_id_is_422():
    sid = publish([cu("ft", "0.3048", "m")])
    assert_err(export([sid, "cus_does_not_exist"]), "invalid_request")
    assert_err(export(["cus_does_not_exist"]), "invalid_request")


def test_export_duplicate_ids_is_422():
    sid = publish([cu("ft", "0.3048", "m")])
    assert_err(export([sid, sid]), "invalid_request")


def test_export_id_count_limits():
    ids = [publish([cu(f"u{i}", "1", "m")]) for i in range(7)]
    assert_err(export([]), "invalid_request")
    r = export(ids[:6])
    assert r.status_code == 200, r.text
    assert_err(export(ids[:7]), "invalid_request")


def test_export_package_alias_target_always_inside_package_under_concurrency():
    # 包内别名引用与定义必须来自同一份快照：并发切换时，导出的别名要么指
    # 包内被选定义，要么（指向包外定义时）不被导出——绝不可能悬空。
    s1 = publish([cu("foo", "2", "m")])
    s2 = publish([cu("foo", "3", "m")])
    store = unit_sets.get_store()
    store.bind_alias("live", s1, None)
    barrier_state = {"n": 0}

    def do_export(_):
        r = export([s1])
        assert r.status_code == 200, r.text
        body = r.json()
        # 若别名被导出，其指向必须在包内定义集之中
        for target in body["aliases"].values():
            assert target in body["sets"]
        return body["aliases"].get("live")

    def switch(_):
        # 在 s1/s2 之间来回切换，制造与导出的竞争
        cur = store.get_alias("live")
        nxt = s2 if cur == s1 else s1
        try:
            store.bind_alias("live", nxt, cur)
        except Exception:
            pass
        barrier_state["n"] += 1

    with ThreadPoolExecutor(8) as pool:
        futs = [pool.submit(do_export, i) for i in range(100)]
        sfuts = [pool.submit(switch, i) for i in range(100)]
        targets = [f.result() for f in futs]
        [f.result() for f in sfuts]
    # 指 s1 时别名进包；指 s2（包外）时别名不导出；没有任何其他取值
    assert set(targets) <= {s1, None}
    assert s1 in targets  # 必然有导出在首次切换前完成


# ---------------------------------------------------------------------------
# 导入：正常路径与标识映射
# ---------------------------------------------------------------------------


def test_import_creates_new_ids_and_repoints_aliases(tmp_path):
    s1 = publish([cu("ft", "0.3048", "m")])
    s2 = publish([cu("chain", "22", "m"), cu("furlong", "10", "chain")])
    bind("lengths", s1, None)
    bind("ranges", s2, None)
    package = export([s1, s2]).json()

    # 导入到一个独立的目标实例
    use_fresh_store(tmp_path)
    r = client.post(IMPORT_PATH, json=package)
    assert r.status_code == 201, r.text
    body = r.json()
    id_map = body["id_map"]
    assert set(id_map) == {s1, s2}
    new_ids = set(id_map.values())
    # 目标端生成全新标识，不沿用源标识，也不与源标识冲突
    assert new_ids.isdisjoint({s1, s2})
    assert len(new_ids) == 2
    # 别名改指对应的新标识
    aliases = {a["alias"]: a["custom_unit_set_id"] for a in body["aliases"]}
    assert aliases == {"lengths": id_map[s1], "ranges": id_map[s2]}
    # 回显包体
    assert body["package"]["sets"] == package["sets"]


def test_imported_definitions_and_aliases_are_usable(tmp_path):
    sid = publish([cu("ft", "0.3048", "m")])
    bind("lengths", sid, None)
    package = export([sid]).json()

    use_fresh_store(tmp_path)
    body = client.post(IMPORT_PATH, json=package).json()
    new_id = body["id_map"][sid]

    # 新标识可用且含义与源定义一致
    r = client.post(
        "/convert",
        json={"value": "10", "from": "ft", "to": "m",
              "custom_unit_set_id": new_id},
    )
    assert r.json() == {"value": "3.048", "unit": "m", "dimension": "m"}
    # 迁移来的别名指向新标识
    assert client.get("/custom-unit-set-aliases/lengths").json() == {
        "alias": "lengths", "custom_unit_set_id": new_id
    }
    r = client.post(
        "/convert",
        json={"value": "1", "from": "ft", "to": "m",
              "custom_unit_set_alias": "lengths"},
    )
    assert r.json()["value"] == "0.3048"


def test_import_preserves_existing_definitions_aliases_and_calls(tmp_path):
    # 源实例准备迁移包（不含别名，避免与目标端别名重名）
    sid = publish([cu("ft", "0.3048", "m")])
    package = export([sid]).json()
    package["aliases"] = {}

    # 目标实例已有自己的定义集、别名与调用
    use_fresh_store(tmp_path)
    r = client.post(
        "/custom-unit-sets",
        json={"custom_units": [cu("bar", "7", "m")]},
    )
    old_id = r.json()["id"]
    bind("keep", old_id, None)

    r = client.post(IMPORT_PATH, json=package)
    assert r.status_code == 201, r.text
    # 原有定义集、别名与调用完全不受影响
    r = client.post(
        "/convert",
        json={"value": "1", "from": "bar", "to": "m",
              "custom_unit_set_id": old_id},
    )
    assert r.json()["value"] == "7"
    assert client.get("/custom-unit-set-aliases/keep").json() == {
        "alias": "keep", "custom_unit_set_id": old_id
    }
    # 重复导入是纯增量：内容相同但标识全新，包内别名为空不冲突，同样成功
    assert client.post(IMPORT_PATH, json=package).status_code == 201


def test_imported_references_survive_container_rebuild(tmp_path):
    # 源实例发布、导出
    sid = publish([cu("chain", "22", "m"), cu("furlong", "10", "chain")])
    bind("ranges", sid, None)
    package = export([sid]).json()

    # 目标实例使用独立数据目录（模拟另一实例）导入
    dst_dir = tmp_path / "dst"
    dst = unit_sets.UnitSetStore(str(dst_dir))
    dst.load()
    unit_sets._store = dst
    body = client.post(IMPORT_PATH, json=package).json()
    new_id = body["id_map"][sid]
    assert body["aliases"] == [
        {"alias": "ranges", "custom_unit_set_id": new_id}
    ]

    # 模拟容器/进程重建：同目录重新加载
    rebuilt = unit_sets.UnitSetStore(str(dst_dir))
    rebuilt.load()
    assert rebuilt.get(new_id)  # 不抛异常
    assert rebuilt.get_alias("ranges") == new_id
    unit_sets._store = rebuilt
    r = client.post(
        "/convert",
        json={"value": "2", "from": "furlong", "to": "m",
              "custom_unit_set_alias": "ranges"},
    )
    assert r.json()["value"] == "440"


# ---------------------------------------------------------------------------
# 导入：整包拒绝场景（不留部分定义或别名）
# ---------------------------------------------------------------------------


def test_import_invalid_definition_rejects_whole_package(tmp_path):
    sid = publish([cu("ft", "0.3048", "m")])
    package = export([sid]).json()
    # 第一个定义集合法，第二个定义集非法（未知基准符号）
    package["sets"]["cus_bad"] = [cu("zzz", "1", "nope_nope")]

    use_fresh_store(tmp_path)
    # 未知符号沿用现有错误码 unknown_symbol（与发布接口完全相同的校验）
    assert_err(client.post(IMPORT_PATH, json=package), "unknown_symbol")
    store = unit_sets.get_store()
    assert store.count() == 0  # 合法的那个也没有留下
    assert store.list_aliases() == {}


def test_import_conflicting_builtin_symbol_rejects_whole_package(tmp_path):
    use_fresh_store(tmp_path)
    package = {
        "kind": "unit-api.custom-unit-set-migration",
        "version": 1,
        "sets": {"cus_x": [cu("m", "1", "m")]},  # 与内置单位冲突
        "aliases": {},
    }
    assert_err(client.post(IMPORT_PATH, json=package), "invalid_request")
    assert unit_sets.get_store().count() == 0


def test_import_dangling_alias_rejects_whole_package(tmp_path):
    sid = publish([cu("ft", "0.3048", "m")])
    package = export([sid]).json()
    package["aliases"]["ghost"] = "cus_not_in_package"

    dst = use_fresh_store(tmp_path)
    # 目标端预置一条与包无关的别名，导入失败后它必须原样保留
    r = client.post(
        "/custom-unit-sets",
        json={"custom_units": [cu("bar", "7", "m")]},
    )
    pre = r.json()["id"]
    bind("preexisting", pre, None)

    assert_err(client.post(IMPORT_PATH, json=package), "invalid_request")
    # 没有留下任何来自包的定义集或别名；预置内容原样保留
    assert unit_sets.get_store().count() == 1
    assert unit_sets.get_store().list_aliases() == {"preexisting": pre}


def test_import_invalid_alias_name_rejects_whole_package(tmp_path):
    sid = publish([cu("ft", "0.3048", "m")])
    package = export([sid]).json()
    package["aliases"]["bad name"] = sid

    use_fresh_store(tmp_path)
    assert_err(client.post(IMPORT_PATH, json=package), "invalid_request")
    assert unit_sets.get_store().count() == 0


def test_import_existing_alias_name_conflict_rejects_whole_package(tmp_path):
    sid = publish([cu("ft", "0.3048", "m")])
    package = export([sid]).json()
    package["aliases"] = {"lengths": sid}  # 目标端将已有同名别名

    dst = use_fresh_store(tmp_path)
    r = client.post(
        "/custom-unit-sets",
        json={"custom_units": [cu("bar", "7", "m")]},
    )
    existing = r.json()["id"]
    bind("lengths", existing, None)

    assert_err(client.post(IMPORT_PATH, json=package), "invalid_request")
    # 没有新增定义集，原别名指向不变
    assert dst.count() == 1
    assert dst.get_alias("lengths") == existing


def test_import_two_sets_one_alias_conflict_rolls_back_everything(tmp_path):
    # 两个定义集 + 两个别名，其中一个别名在目标端已存在：整包回滚，
    # 另一个别名与两个定义集也都不得留下
    a = publish([cu("aa", "1", "m")])
    b = publish([cu("bb", "2", "m")])
    bind("shared", a, None)
    bind("unique", b, None)
    package = export([a, b]).json()

    dst = use_fresh_store(tmp_path)
    r = client.post(
        "/custom-unit-sets",
        json={"custom_units": [cu("zz", "9", "m")]},
    )
    pre = r.json()["id"]
    bind("shared", pre, None)

    assert_err(client.post(IMPORT_PATH, json=package), "invalid_request")
    # 整包回滚：只剩预置内容
    assert dst.count() == 1
    assert dst.list_aliases() == {"shared": pre}


def test_import_empty_sets_rejected(tmp_path):
    use_fresh_store(tmp_path)
    package = {
        "kind": "unit-api.custom-unit-set-migration",
        "version": 1,
        "sets": {},
        "aliases": {},
    }
    assert_err(client.post(IMPORT_PATH, json=package), "invalid_request")


def test_import_too_many_sets_rejected(tmp_path):
    use_fresh_store(tmp_path)
    package = {
        "kind": "unit-api.custom-unit-set-migration",
        "version": 1,
        "sets": {f"cus_{i}": [cu(f"u{i}", "1", "m")] for i in range(7)},
        "aliases": {},
    }
    assert_err(client.post(IMPORT_PATH, json=package), "invalid_request")


def test_import_bad_kind_or_version_rejected(tmp_path):
    sid = publish([cu("ft", "0.3048", "m")])
    package = export([sid]).json()
    use_fresh_store(tmp_path)
    assert_err(client.post(IMPORT_PATH, json=dict(package, kind="x")), "invalid_request")
    assert_err(client.post(IMPORT_PATH, json=dict(package, version=2)), "invalid_request")


# ---------------------------------------------------------------------------
# 导入：原子性与落盘
# ---------------------------------------------------------------------------


def test_failed_disk_write_rolls_back_entire_import(tmp_path, monkeypatch):
    sid = publish([cu("ft", "0.3048", "m")])
    bind("lengths", sid, None)
    package = export([sid]).json()

    dst = use_fresh_store(tmp_path, "dst")
    # 预置一条原有内容，验证失败后它保持可用
    r = client.post(
        "/custom-unit-sets",
        json={"custom_units": [cu("bar", "7", "m")]},
    )
    pre = r.json()["id"]
    bind("keep", pre, None)
    before_count = dst.count()
    before_aliases = dst.list_aliases()

    def boom(*_args, **_kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(unit_sets.os, "replace", boom)
    import pytest

    # 包内别名改名为不冲突的名字
    pkg = json.loads(json.dumps(package))
    pkg["aliases"] = {"imported_lengths": sid}
    pkg_sets = {
        k: [CustomUnitItem.model_validate(item) for item in v]
        for k, v in pkg["sets"].items()
    }
    with pytest.raises(OSError):
        dst.import_package(sets=pkg_sets, aliases=dict(pkg["aliases"]))

    # 内存状态保持原样：没有新增定义集或别名
    assert dst.count() == before_count
    assert dst.list_aliases() == before_aliases
    # 磁盘文件也保持原样（没有包内的新内容）
    raw = json.load(open(dst.path, encoding="utf-8"))
    assert set(raw["sets"]) == {pre}
    assert raw["aliases"] == {"keep": pre}

    # 恢复后重新导入成功，且新引用可用
    monkeypatch.undo()
    id_map = dst.import_package(sets=pkg_sets, aliases=dict(pkg["aliases"]))
    new_id = id_map[sid]
    assert dst.get_alias("imported_lengths") == new_id
    scope = dst.build_scope(new_id)
    from app.parser import parse_expression
    from app.converter import convert

    assert convert("1", parse_expression("ft", scope), parse_expression("m", scope)) == "0.3048"
    # 原有内容仍可用
    assert dst.get_alias("keep") == pre


def test_persisted_import_file_loads_normally(tmp_path):
    # 落盘文件包含新旧全部内容，且可被同一存储格式重新加载
    sid = publish([cu("ft", "0.3048", "m")])
    bind("lengths", sid, None)
    package = export([sid]).json()

    dst = use_fresh_store(tmp_path)
    body = client.post(IMPORT_PATH, json=package).json()
    new_id = body["id_map"][sid]

    raw = json.load(open(dst.path, encoding="utf-8"))
    assert raw["version"] == 1
    assert new_id in raw["sets"]
    assert raw["aliases"] == {"lengths": new_id}


# ---------------------------------------------------------------------------
# HTTP 层往返
# ---------------------------------------------------------------------------


def test_http_roundtrip_export_then_import_across_two_stores(tmp_path):
    # 完整 HTTP 往返：源实例导出 -> 目标实例导入 -> 新引用可用
    s1 = publish([cu("ft", "0.3048", "m"), cu("yd", "3", "ft")])
    s2 = publish([cu("dozen", "12", "1")])
    bind("lengths", s1, None)
    bind("counting", s2, None)
    r = export([s1, s2])
    assert r.status_code == 200
    exported_body = r.json()

    use_fresh_store(tmp_path)
    r = client.post(IMPORT_PATH, json=exported_body)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id_map"][s1] and body["id_map"][s2]

    # 1 yd = 3 ft = 0.9144 m
    r = client.post(
        "/convert",
        json={"value": "1", "from": "yd", "to": "m",
              "custom_unit_set_alias": "lengths"},
    )
    assert r.json() == {"value": "0.9144", "unit": "m", "dimension": "m"}
    r = client.post(
        "/evaluate",
        json={
            "variables": {"n": {"value": "2", "unit": "dozen"}},
            "formula": "n",
            "output_unit": "1",
            "custom_unit_set_alias": "counting",
        },
    )
    assert r.json()["value"] == "24"
    # /worksheet 也可使用迁移来的别名
    r = client.post(
        "/worksheet",
        json={
            "variables": {"a": {"value": "1", "unit": "yd"}},
            "steps": [
                {"name": "x", "formula": "a", "output_unit": "ft"},
                {"name": "y", "formula": "x", "output_unit": "m"},
            ],
            "custom_unit_set_alias": "lengths",
        },
    )
    assert r.status_code == 200, r.text
    assert [s["value"] for s in r.json()["steps"]] == ["3", "0.9144"]


def test_export_body_survives_json_file_roundtrip(tmp_path):
    # 模拟“导出后存文件、另一实例读文件再导入”：JSON 序列化/反序列化往返
    sid = publish([cu("ft", "0.3048", "m")])
    bind("lengths", sid, None)
    pkg_path = tmp_path / "package.json"
    pkg_path.write_text(json.dumps(export([sid]).json()), encoding="utf-8")
    reloaded = json.loads(pkg_path.read_text(encoding="utf-8"))

    use_fresh_store(tmp_path, "remote")
    r = client.post(IMPORT_PATH, json=reloaded)
    assert r.status_code == 201, r.text
    new_id = r.json()["id_map"][sid]
    assert r.json()["aliases"] == [
        {"alias": "lengths", "custom_unit_set_id": new_id}
    ]


# ---------------------------------------------------------------------------
# 存储层直接调用
# ---------------------------------------------------------------------------


def test_store_helpers_reject_bad_inputs_directly():
    from app.errors import ConversionError
    import pytest

    store = unit_sets.get_store()
    with pytest.raises(ConversionError):
        store.export_package([])
    with pytest.raises(ConversionError):
        store.export_package(["cus_nope"])
    sid = publish([cu("ft", "0.3048", "m")])
    with pytest.raises(ConversionError):
        store.export_package([sid, sid])
    with pytest.raises(ConversionError):
        store.import_package({}, {})
    x_items = [CustomUnitItem.model_validate(cu("ft", "1", "m"))]
    # 悬空别名
    with pytest.raises(ConversionError):
        store.import_package({"cus_x": x_items}, {"a": "cus_missing"})
    # 非法别名
    with pytest.raises(ConversionError):
        store.import_package({"cus_x": x_items}, {"bad/name": "cus_x"})
