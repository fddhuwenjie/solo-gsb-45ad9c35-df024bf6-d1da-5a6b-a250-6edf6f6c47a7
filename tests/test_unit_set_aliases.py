"""已发布定义集的活动别名（active aliases）端到端测试。

覆盖：

- PUT /custom-unit-set-aliases/{alias}：初建（expected_id=null）与切换
  （expected_id=当前指向）；仅当当前指向与 expected_id 一致时生效，
  否则 409 且不改写原指向；目标不存在/别名非法返回 422；
- GET /custom-unit-set-aliases 与 GET /custom-unit-set-aliases/{alias} 查询；
- /convert、/evaluate、/solve 携带 custom_unit_set_alias：请求开始解析一次
  别名，源、目标及变量始终使用同一版定义；未知别名 422；
- custom_unit_set_alias 与 custom_units、custom_unit_set_id 两两互斥；
- 切换与计算并发：单次请求完整使用旧版或新版，绝不跨版本混用；
- 切换与落盘共同生效：写盘失败回滚进程内指向；容器重建后别名仍有效；
  旧文件缺少 aliases 字段时照常加载已发布定义集；
- 不带别名、直接用 custom_unit_set_id 的调用维持原行为。
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app import unit_sets

client = TestClient(app)


def cu(symbol, factor, base_unit):
    return {"symbol": symbol, "factor": factor, "base_unit": base_unit}


def assert_err(resp, code, status=422):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["error"] == code


def publish(defs):
    r = client.post("/custom-unit-sets", json={"custom_units": defs})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def bind(alias, set_id, expected_id, status=200):
    return client.put(
        f"/custom-unit-set-aliases/{alias}",
        json={"custom_unit_set_id": set_id, "expected_id": expected_id},
    )


# ---------------------------------------------------------------------------
# 初建与切换（乐观并发）
# ---------------------------------------------------------------------------


def test_create_alias_with_expected_null():
    sid = publish([cu("foo", "2", "m")])
    r = bind("lengths", sid, None)
    assert r.status_code == 200, r.text
    assert r.json() == {"alias": "lengths", "custom_unit_set_id": sid}
    r = client.get("/custom-unit-set-aliases/lengths")
    assert r.json() == {"alias": "lengths", "custom_unit_set_id": sid}


def test_create_alias_requires_expected_null():
    sid = publish([cu("foo", "2", "m")])
    r = bind("a", sid, sid)
    assert_err(r, "alias_conflict", status=409)
    # 冲突时别名并未建立
    assert_err(client.get("/custom-unit-set-aliases/a"), "invalid_request")


def test_switch_alias_with_correct_expected_id():
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    bind("live", sid_v1, None)
    r = bind("live", sid_v2, sid_v1)
    assert r.status_code == 200
    assert r.json()["custom_unit_set_id"] == sid_v2
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_alias": "live"},
    )
    assert r.json()["value"] == "3"


def test_stale_expected_id_returns_409_and_keeps_old_target():
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    sid_v3 = publish([cu("foo", "5", "m")])
    bind("live", sid_v1, None)
    bind("live", sid_v2, sid_v1)
    # 用过期的 sid_v1 作为 expected_id 切到 v3：409，指向保持 v2
    r = bind("live", sid_v3, sid_v1)
    assert_err(r, "alias_conflict", status=409)
    assert r.json()["detail"]["current_id"] == sid_v2
    assert client.get("/custom-unit-set-aliases/live").json()[
        "custom_unit_set_id"
    ] == sid_v2
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_alias": "live"},
    )
    assert r.json()["value"] == "3"


def test_expected_null_on_existing_alias_conflicts():
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    bind("live", sid_v1, None)
    r = bind("live", sid_v2, None)
    assert_err(r, "alias_conflict", status=409)
    assert client.get("/custom-unit-set-aliases/live").json()[
        "custom_unit_set_id"
    ] == sid_v1


def test_bind_to_nonexistent_target_is_422_and_creates_nothing():
    sid = publish([cu("foo", "2", "m")])
    bind("live", sid, None)
    before = client.get("/custom-unit-set-aliases/live").json()
    # 不存在的目标：422，且原指向不变
    r = bind("live", "cus_does_not_exist", sid)
    assert_err(r, "invalid_request")
    assert client.get("/custom-unit-set-aliases/live").json() == before
    # 对新别名绑定不存在的目标：422，不留下别名
    r = bind("brand_new", "cus_does_not_exist", None)
    assert_err(r, "invalid_request")
    assert_err(client.get("/custom-unit-set-aliases/brand_new"), "invalid_request")
    assert client.get("/custom-unit-set-aliases").json()["aliases"] == [before]


def test_invalid_alias_name_is_422():
    sid = publish([cu("foo", "2", "m")])
    for bad in ("a b", "a/b", "a.b", "中文", "x" * 65):
        r = client.put(
            f"/custom-unit-set-aliases/{bad}",
            json={"custom_unit_set_id": sid, "expected_id": None},
        )
        assert r.status_code in (404, 422), (bad, r.status_code)
        if r.status_code == 422:
            assert r.json()["detail"]["error"] == "invalid_request"
    # 存储层对空名等无法经路径传递的名称同样拒绝
    from app.errors import ConversionError as _CE

    import pytest

    with pytest.raises(_CE):
        unit_sets.get_store().bind_alias("", sid, None)


def test_bind_missing_expected_id_field_is_422():
    sid = publish([cu("foo", "2", "m")])
    r = client.put(
        "/custom-unit-set-aliases/live", json={"custom_unit_set_id": sid}
    )
    assert_err(r, "invalid_request")


def test_rebind_same_target_is_idempotent():
    sid = publish([cu("foo", "2", "m")])
    bind("live", sid, None)
    r = bind("live", sid, sid)
    assert r.status_code == 200
    assert r.json()["custom_unit_set_id"] == sid


def test_alias_names_support_hyphens_underscores_and_digits():
    sid = publish([cu("foo", "1", "m")])
    for name in ("a-b_c1", "X", "0", "123-456"):
        assert bind(name, sid, None).status_code == 200


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def test_list_aliases_sorted_with_targets():
    sid = publish([cu("foo", "1", "m")])
    sid2 = publish([cu("bar", "1", "m")])
    bind("zeta", sid, None)
    bind("alpha", sid2, None)
    r = client.get("/custom-unit-set-aliases")
    assert r.status_code == 200
    assert r.json() == {
        "aliases": [
            {"alias": "alpha", "custom_unit_set_id": sid2},
            {"alias": "zeta", "custom_unit_set_id": sid},
        ]
    }


def test_list_aliases_empty_when_none():
    assert client.get("/custom-unit-set-aliases").json() == {"aliases": []}


def test_get_unknown_alias_is_422():
    assert_err(
        client.get("/custom-unit-set-aliases/nope"), "invalid_request"
    )


# ---------------------------------------------------------------------------
# 三个计算接口使用别名：一次请求一版定义
# ---------------------------------------------------------------------------


def test_convert_resolves_alias_once_and_uses_it_everywhere():
    sid = publish([cu("ft", "0.3048", "m")])
    bind("imperial", sid, None)
    r = client.post(
        "/convert",
        json={
            "value": "10", "from": "ft", "to": "m",
            "custom_unit_set_alias": "imperial",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"value": "3.048", "unit": "m", "dimension": "m"}


def test_evaluate_and_solve_use_alias():
    sid = publish([cu("dozen", "12", "1")])
    bind("counting", sid, None)
    r = client.post(
        "/evaluate",
        json={
            "variables": {
                "a": {"value": "2", "unit": "dozen"},
                "b": {"value": "3", "unit": "dozen"},
            },
            "formula": "a*b",
            "output_unit": "dozen^2",
            "custom_unit_set_alias": "counting",
        },
    )
    assert r.json() == {"value": "6", "unit": "dozen^2", "dimension": "1"}

    sid_rod = publish([cu("rod", "5.0292", "m")])
    bind("lengths", sid_rod, None)
    r = client.post(
        "/solve",
        json={
            "variables": {"a": {"value": "3", "unit": "rod"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "m",
            "custom_unit_set_alias": "lengths",
        },
    )
    assert r.json() == {"value": "15.0876", "unit": "m", "dimension": "m"}


def test_unknown_alias_is_422_on_all_endpoints():
    bodies = [
        {"value": "1", "from": "m", "to": "m"},
        {"variables": {"a": {"value": "1", "unit": "m"}},
         "formula": "a", "output_unit": "m"},
        {"variables": {"a": {"value": "1", "unit": "m"}},
         "equation": "x = a", "solve_for": "x", "output_unit": "m"},
    ]
    for path, body in zip(("/convert", "/evaluate", "/solve"), bodies):
        body = {**body, "custom_unit_set_alias": "ghost"}
        assert_err(client.post(path, json=body), "invalid_request")


def test_alias_switch_takes_effect_for_later_requests_not_inflight_definitions():
    # 旧版 foo=2m，新版 foo=3m；切换后新请求看到新版
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    bind("live", sid_v1, None)
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_alias": "live"},
    )
    assert r.json()["value"] == "2"
    bind("live", sid_v2, sid_v1)
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_alias": "live"},
    )
    assert r.json()["value"] == "3"
    # 旧定义集标识仍然可用且含义不变（不可变）
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_id": sid_v1},
    )
    assert r.json()["value"] == "2"


def test_alias_mutual_exclusion_with_other_sources():
    sid = publish([cu("ft", "0.3048", "m")])
    bind("imp", sid, None)
    bodies = [
        {"value": "1", "from": "ft", "to": "m"},
        {"variables": {"a": {"value": "1", "unit": "ft"}},
         "formula": "a", "output_unit": "m"},
        {"variables": {"a": {"value": "1", "unit": "ft"}},
         "equation": "x = a", "solve_for": "x", "output_unit": "m"},
    ]
    for path, body in zip(("/convert", "/evaluate", "/solve"), bodies):
        # alias + inline custom_units
        r = client.post(path, json={
            **body,
            "custom_unit_set_alias": "imp",
            "custom_units": [cu("ft", "1", "m")],
        })
        assert_err(r, "invalid_request")
        # alias + custom_unit_set_id
        r = client.post(path, json={
            **body,
            "custom_unit_set_alias": "imp",
            "custom_unit_set_id": sid,
        })
        assert_err(r, "invalid_request")
        # 三者同时出现
        r = client.post(path, json={
            **body,
            "custom_unit_set_alias": "imp",
            "custom_unit_set_id": sid,
            "custom_units": [cu("ft", "1", "m")],
        })
        assert_err(r, "invalid_request")


def test_null_alias_is_equivalent_to_absent():
    r = client.post(
        "/convert",
        json={"value": "1", "from": "m", "to": "cm", "custom_unit_set_alias": None},
    )
    assert r.status_code == 200
    assert r.json()["value"] == "100"


def test_direct_id_and_no_selection_keep_original_behaviour():
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "7", "m")])
    bind("live", sid_v1, None)
    bind("live", sid_v2, sid_v1)
    # 直接用原标识：与别名无关，始终是该不可变定义集
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_id": sid_v1},
    )
    assert r.json()["value"] == "2"
    # 不选任何来源：内置行为不变，foo 未知
    assert_err(
        client.post("/convert", json={"value": "1", "from": "foo", "to": "m"}),
        "unknown_symbol",
    )


# ---------------------------------------------------------------------------
# 并发：切换与计算同时进行，单次请求只能完整使用旧版或新版
# ---------------------------------------------------------------------------


def test_concurrent_switch_and_compute_never_mixes_versions():
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    store = unit_sets.get_store()
    store.bind_alias("live", sid_v1, None)

    def compute(_):
        r = client.post(
            "/convert",
            json={
                "value": "1", "from": "foo", "to": "m",
                "custom_unit_set_alias": "live",
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["value"]

    def switch(_):
        # 409（与并发切换竞争）是允许的；成功与否都不得影响计算一致性
        return store.bind_alias("live", sid_v2, sid_v1)

    with ThreadPoolExecutor(8) as pool:
        futs = [pool.submit(compute, i) for i in range(100)]
        sf = pool.submit(switch, 0)
        values = [f.result() for f in futs]
        sf.result()

    # 每次结果只能是旧版 2 或新版 3——不可能出现跨版本的其他值
    assert set(values) <= {"2", "3"}
    assert "2" in values  # 切换前必然有请求完成
    assert "3" in values  # 切换后必然有请求完成


def test_concurrent_conflicting_binds_only_one_succeeds():
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    sid_v3 = publish([cu("foo", "5", "m")])
    store = unit_sets.get_store()
    store.bind_alias("live", sid_v1, None)

    from app.errors import AliasConflictError

    outcomes = []

    def bind_to(target):
        try:
            store.bind_alias("live", target, sid_v1)
            outcomes.append(("ok", target))
        except AliasConflictError:
            outcomes.append(("conflict", target))

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(bind_to, [sid_v2, sid_v3]))
    oks = [t for kind, t in outcomes if kind == "ok"]
    assert len(oks) == 1
    final = store.get_alias("live")
    assert final in (sid_v2, sid_v3)
    assert final == oks[0]
    # 磁盘上的指向与进程内一致
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["aliases"]["live"] == final


# ---------------------------------------------------------------------------
# 持久化：共同生效、重建有效、旧文件兼容
# ---------------------------------------------------------------------------


def test_switch_persists_and_survives_reinitialization():
    store = unit_sets.get_store()
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    bind("live", sid_v1, None)
    bind("live", sid_v2, sid_v1)

    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["aliases"] == {"live": sid_v2}

    # 模拟容器/进程重建：同一数据目录重新加载
    data_dir = os.path.dirname(store.path)
    reloaded = unit_sets.UnitSetStore(data_dir)
    reloaded.load()
    assert reloaded.get_alias("live") == sid_v2
    unit_sets._store = reloaded

    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_alias": "live"},
    )
    assert r.json()["value"] == "3"


def test_old_file_without_aliases_field_loads_sets_normally(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    # 先用现有存储发布一个定义集，再手工删掉 aliases 字段模拟旧文件
    old = unit_sets.UnitSetStore(str(data_dir))
    old.load()
    unit_sets._store = old
    specs = [cu("foo", "2", "m")]
    r = client.post("/custom-unit-sets", json={"custom_units": specs})
    sid = r.json()["id"]
    path = old.path
    raw = json.load(open(path, encoding="utf-8"))
    del raw["aliases"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f)

    reloaded = unit_sets.UnitSetStore(str(data_dir))
    reloaded.load()  # 不应抛异常
    assert reloaded.count() == 1
    assert reloaded.list_aliases() == {}
    # 原定义集照常可用
    unit_sets._store = reloaded
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_unit_set_id": sid},
    )
    assert r.json()["value"] == "2"
    # 旧文件加载后仍可新建别名并落盘
    r = bind("live", sid, None)
    assert r.status_code == 200
    raw2 = json.load(open(path, encoding="utf-8"))
    assert raw2["aliases"] == {"live": sid}


def test_failed_disk_write_does_not_change_inmemory_pointer(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    store = unit_sets.UnitSetStore(str(data_dir))
    store.load()
    unit_sets._store = store
    sid_v1 = publish([cu("foo", "2", "m")])
    sid_v2 = publish([cu("foo", "3", "m")])
    store.bind_alias("live", sid_v1, None)
    assert os.path.exists(store.path)

    def boom(*_args, **_kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(unit_sets.os, "replace", boom)
    import pytest

    with pytest.raises(OSError):
        store.bind_alias("live", sid_v2, sid_v1)

    # 进程内指向保持旧版；查询与解析都仍是旧版
    assert store.get_alias("live") == sid_v1
    scope = store.build_scope_for_alias("live")
    from app.parser import parse_expression
    from app.converter import convert

    src = parse_expression("foo", scope)
    dst = parse_expression("m", scope)
    assert convert("1", src, dst) == "2"
    # 磁盘文件也仍是旧版
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["aliases"]["live"] == sid_v1

    # 恢复后可用正确的 expected_id 正常切换
    monkeypatch.undo()
    assert store.bind_alias("live", sid_v2, sid_v1) == sid_v2
    assert store.get_alias("live") == sid_v2
