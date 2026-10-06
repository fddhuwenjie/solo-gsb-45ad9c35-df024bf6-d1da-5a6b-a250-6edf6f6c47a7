"""可复用自定义单位定义集（POST /custom-unit-sets）的端到端测试。

覆盖：

- 发布 1～6 项现有 custom_units 格式定义，返回服务生成的标识与回显；
- 发布前按现有规则校验并展开全部条目：任一项非法返回 422 且不留任何
  可引用定义集（不生成标识、不写入存储）；
- /convert、/evaluate、/solve 可按标识选用定义集；一次调用的源、目标与
  变量单位使用同一份定义；
- 请求内 custom_units 与 custom_unit_set_id 同时出现、标识不存在均 422；
- 不同定义集允许同名符号有不同含义；并发发布与并发调用互不污染、
  不修改全局单位表；
- 已发布内容不可修改、容器/进程重建后仍可使用；
- 不选用定义集（也不带 custom_units）时三个接口行为与响应完全不变。
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app import unit_sets
from app.units import REGISTRY

client = TestClient(app)


def cu(symbol, factor, base_unit):
    return {"symbol": symbol, "factor": factor, "base_unit": base_unit}


def assert_err(resp, code):
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


def publish(defs):
    r = client.post("/custom-unit-sets", json={"custom_units": defs})
    assert r.status_code == 201, r.text
    return r


# ---------------------------------------------------------------------------
# 发布：校验、展开、标识与回显
# ---------------------------------------------------------------------------


def test_publish_returns_server_generated_id_and_echo():
    defs = [
        cu("ft", "0.3048", "m"),
        cu("furlong", "10", "chain"),
        cu("chain", "22", "m"),
    ]
    r = publish(defs)
    body = r.json()
    assert body["id"].startswith("cus_")
    assert len(body["id"]) > len("cus_") + 8
    # factor 以精确十进制字符串回显，顺序与提交一致
    assert body["custom_units"] == defs


def test_publish_ids_are_unique():
    ids = {publish([cu("ft", "0.3048", "m")]).json()["id"] for _ in range(5)}
    assert len(ids) == 5


def test_publish_expands_all_entries_even_unused_ones():
    # 链式 + 前向引用：发布时全部条目都已成功展开（否则会 422）
    defs = [cu("furlong", "10", "chain"), cu("chain", "22", "m")]
    sid = publish(defs).json()["id"]
    r = client.post(
        "/convert",
        json={"value": "2", "from": "furlong", "to": "m", "custom_unit_set_id": sid},
    )
    assert r.status_code == 200
    assert r.json()["value"] == "440"


def test_publish_single_and_six_items():
    assert publish([cu("a1", "1", "m")]).status_code == 201
    six = [cu(f"u{i}", "1", "m") for i in range(6)]
    assert publish(six).status_code == 201


def test_publish_rejects_zero_and_seven_items():
    r = client.post("/custom-unit-sets", json={"custom_units": []})
    assert_err(r, "invalid_request")
    seven = [cu(f"u{i}", "1", "m") for i in range(7)]
    r = client.post("/custom-unit-sets", json={"custom_units": seven})
    assert_err(r, "invalid_request")


def test_publish_rejects_missing_field_and_bad_shape():
    r = client.post("/custom-unit-sets", json={})
    assert_err(r, "invalid_request")
    r = client.post("/custom-unit-sets", json={"custom_units": [{"symbol": "a"}]})
    assert_err(r, "invalid_request")
    r = client.post("/custom-unit-sets", json={"custom_units": "nope"})
    assert_err(r, "invalid_request")


# ---------------------------------------------------------------------------
# 任一项非法：422 且不能留下任何可引用的定义集
# ---------------------------------------------------------------------------


def test_invalid_entry_leaves_no_referencable_set():
    bad_sets = [
        [cu("m", "1", "1")],                      # 与内置单位冲突
        [cu("km", "1", "m")],                     # 与可由词头解析的符号冲突
        [cu("aa", "1", "m"), cu("aa", "2", "m")],  # 重名
        [cu("aa", "1", "aa")],                    # 自引用
        [cu("aa", "1", "bb"), cu("bb", "1", "aa")],  # 循环
        [cu("aa", "1", "zzz")],                   # 未知引用
        [cu("aa", "0", "m")],                     # 非正比例
        [cu("aa", "-1", "m")],
        [cu("aa", "abc", "m")],
        [cu("aa", "1", "degC")],                  # 禁引温标
        [cu("aa", "1", "K")],                     # 禁引裸 K
        [cu("good", "1", "m"), cu("aa", "1", "bb"), cu("bb", "1", "aa")],  # 未使用项有环
    ]
    before = unit_sets.get_store().count()
    for defs in bad_sets:
        r = client.post("/custom-unit-sets", json={"custom_units": defs})
        assert r.status_code == 422, (defs, r.text)
    assert unit_sets.get_store().count() == before


def test_failed_publish_writes_nothing_to_disk():
    store = unit_sets.get_store()
    client.post(
        "/custom-unit-sets",
        json={"custom_units": [cu("aa", "1", "bb"), cu("bb", "1", "aa")]},
    )
    assert store.count() == 0
    assert not os.path.exists(store.path)


# ---------------------------------------------------------------------------
# 三个接口按标识选用
# ---------------------------------------------------------------------------


def test_convert_uses_published_set():
    sid = publish([cu("ft", "0.3048", "m")]).json()["id"]
    r = client.post(
        "/convert",
        json={"value": "10", "from": "ft", "to": "m", "custom_unit_set_id": sid},
    )
    assert r.status_code == 200
    assert r.json() == {"value": "3.048", "unit": "m", "dimension": "m"}
    r = client.post(
        "/convert",
        json={"value": "3.048", "from": "m", "to": "ft", "custom_unit_set_id": sid},
    )
    assert r.json() == {"value": "10", "unit": "ft", "dimension": "m"}


def test_evaluate_variables_and_output_share_published_definitions():
    sid = publish([cu("dozen", "12", "1")]).json()["id"]
    r = client.post(
        "/evaluate",
        json={
            "variables": {
                "a": {"value": "2", "unit": "dozen"},
                "b": {"value": "3", "unit": "dozen"},
            },
            "formula": "a*b",
            "output_unit": "dozen^2",
            "custom_unit_set_id": sid,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"value": "6", "unit": "dozen^2", "dimension": "1"}


def test_solve_uses_published_set():
    sid = publish([cu("rod", "5.0292", "m")]).json()["id"]
    r = client.post(
        "/solve",
        json={
            "variables": {"a": {"value": "3", "unit": "rod"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "m",
            "custom_unit_set_id": sid,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"value": "15.0876", "unit": "m", "dimension": "m"}


def test_referenced_set_enforces_dimension_rules():
    sid = publish([cu("foo", "2", "m")]).json()["id"]
    r = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "s", "custom_unit_set_id": sid},
    )
    assert_err(r, "dimension_mismatch")


def test_unknown_symbol_under_published_set_is_422():
    sid = publish([cu("ft", "0.3048", "m")]).json()["id"]
    r = client.post(
        "/convert",
        json={"value": "1", "from": "nope", "to": "m", "custom_unit_set_id": sid},
    )
    assert_err(r, "unknown_symbol")


# ---------------------------------------------------------------------------
# 两种来源互斥；标识不存在
# ---------------------------------------------------------------------------


def test_custom_units_and_id_together_rejected():
    sid = publish([cu("ft", "0.3048", "m")]).json()["id"]
    bodies = [
        {"value": "1", "from": "ft", "to": "m"},
        {
            "variables": {"a": {"value": "1", "unit": "ft"}},
            "formula": "a",
            "output_unit": "m",
        },
        {
            "variables": {"a": {"value": "1", "unit": "ft"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "m",
        },
    ]
    paths = ("/convert", "/evaluate", "/solve")
    inline = [cu("ft", "1", "m")]
    for path, body in zip(paths, bodies):
        body = {**body, "custom_units": inline, "custom_unit_set_id": sid}
        assert_err(client.post(path, json=body), "invalid_request")


def test_nonexistent_set_id_rejected_on_all_endpoints():
    bodies = [
        {"value": "1", "from": "m", "to": "m"},
        {
            "variables": {"a": {"value": "1", "unit": "m"}},
            "formula": "a",
            "output_unit": "m",
        },
        {
            "variables": {"a": {"value": "1", "unit": "m"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "m",
        },
    ]
    for path, body in zip(("/convert", "/evaluate", "/solve"), bodies):
        body = {**body, "custom_unit_set_id": "cus_does_not_exist"}
        assert_err(client.post(path, json=body), "invalid_request")


def test_null_id_is_equivalent_to_absent():
    r = client.post(
        "/convert",
        json={"value": "1", "from": "m", "to": "cm", "custom_unit_set_id": None},
    )
    assert r.status_code == 200
    assert r.json()["value"] == "100"


# ---------------------------------------------------------------------------
# 同名符号的不同含义；请求间/定义集间隔离
# ---------------------------------------------------------------------------


def test_different_sets_can_redefine_same_symbol():
    sid_ft = publish([cu("x", "2", "m")]).json()["id"]
    sid_sec = publish([cu("x", "7", "s")]).json()["id"]
    r = client.post(
        "/convert",
        json={"value": "1", "from": "x", "to": "m", "custom_unit_set_id": sid_ft},
    )
    assert r.json()["value"] == "2"
    r = client.post(
        "/convert",
        json={"value": "1", "from": "x", "to": "s", "custom_unit_set_id": sid_sec},
    )
    assert r.json()["value"] == "7"
    # 不选定义集时 x 仍未知
    assert_err(
        client.post("/convert", json={"value": "1", "from": "x", "to": "m"}),
        "unknown_symbol",
    )


def test_inline_custom_units_do_not_leak_across_requests():
    sid = publish([cu("leak", "3", "m")]).json()["id"]
    # 另一请求用 inline 定义同名但不同含义，不影响按标识调用
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "leak", "to": "m",
            "custom_units": [cu("leak", "9", "m")],
        },
    )
    assert r.json()["value"] == "9"
    r = client.post(
        "/convert",
        json={"value": "1", "from": "leak", "to": "m", "custom_unit_set_id": sid},
    )
    assert r.json()["value"] == "3"
    assert "leak" not in REGISTRY


def test_convert_calls_with_different_sets_are_isolated_under_concurrency():
    sid_a = publish([cu("q", "2", "m")]).json()["id"]
    sid_b = publish([cu("q", "7", "m")]).json()["id"]

    def call(sid):
        r = client.post(
            "/convert",
            json={"value": "1", "from": "q", "to": "m", "custom_unit_set_id": sid},
        )
        return r.json()["value"]

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(call, [sid_a] * 25 + [sid_b] * 25))
    assert results.count("2") == 25
    assert results.count("7") == 25
    assert "q" not in REGISTRY


def test_concurrent_publishes_all_persist():
    def publish_one(i):
        return publish([cu(f"p{i}", "1", "m")]).json()["id"]

    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(publish_one, range(40)))
    assert len(set(ids)) == 40
    assert unit_sets.get_store().count() == 40
    for sid in ids:
        r = client.post(
            "/convert",
            json={"value": "1", "from": "m", "to": "m", "custom_unit_set_id": sid},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 不可变与持久化
# ---------------------------------------------------------------------------


def test_published_sets_are_immutable():
    sid = publish([cu("fix", "2", "m")]).json()["id"]
    # 没有更新/删除接口：PUT/PATCH/DELETE 均不允许
    for method in ("put", "patch", "delete"):
        r = getattr(client, method)(f"/custom-unit-sets/{sid}")
        assert r.status_code in (404, 405), (method, r.status_code)
    # 同路径 POST 只是再发布一份新定义，旧标识含义不变
    publish([cu("fix", "9", "m")])
    r = client.post(
        "/convert",
        json={"value": "1", "from": "fix", "to": "m", "custom_unit_set_id": sid},
    )
    assert r.json()["value"] == "2"


def test_sets_survive_store_reinitialization(tmp_path):
    # 发布后存储文件落盘
    store = unit_sets.get_store()
    sid = publish(
        [cu("ft", "0.3048", "m"), cu("kft", "1000", "ft")]
    ).json()["id"]
    assert os.path.exists(store.path)
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["version"] == 1
    assert sid in raw["sets"]

    # 模拟容器/进程重建：在同一数据目录重新加载存储
    reloaded = unit_sets.UnitSetStore(store.path.rsplit("/", 1)[0])
    reloaded.load()
    unit_sets._store = reloaded
    r = client.post(
        "/convert",
        json={"value": "1", "from": "kft", "to": "m", "custom_unit_set_id": sid},
    )
    assert r.status_code == 200
    assert r.json()["value"] == "304.8"
    # 重建后再发布仍可继续使用（文件可追加、旧内容保留）
    sid2 = publish([cu("rod", "5.0292", "m")]).json()["id"]
    r = client.post(
        "/convert",
        json={"value": "1", "from": "rod", "to": "m", "custom_unit_set_id": sid2},
    )
    assert r.json()["value"] == "5.0292"
    r = client.post(
        "/convert",
        json={"value": "1", "from": "kft", "to": "m", "custom_unit_set_id": sid},
    )
    assert r.json()["value"] == "304.8"


def test_stored_factor_keeps_exact_decimal_precision():
    factor = "1." + "1" * 40  # 40 位小数，精确十进制
    sid = publish([cu("prec", factor, "1")]).json()["id"]
    raw = json.load(open(unit_sets.get_store().path, encoding="utf-8"))
    stored = {item["symbol"]: item for item in raw["sets"][sid]}
    assert stored["prec"]["factor"] == factor
    r = client.post(
        "/convert",
        json={"value": "1", "from": "prec", "to": "1", "custom_unit_set_id": sid},
    )
    assert r.status_code == 200
    assert r.json()["value"] == factor


# ---------------------------------------------------------------------------
# 未选用定义集时原有行为完全保留
# ---------------------------------------------------------------------------


def test_original_behaviour_preserved_without_selection():
    r = client.post(
        "/convert", json={"value": "36", "from": "km/h", "to": "m/s"}
    )
    assert r.json() == {"value": "10", "unit": "m/s", "dimension": "m/s"}
    assert_err(
        client.post("/convert", json={"value": "1", "from": "ft", "to": "m"}),
        "unknown_symbol",
    )
    r = client.post(
        "/evaluate",
        json={
            "variables": {"v": {"value": "60", "unit": "km/h"}, "t": {"value": "2", "unit": "h"}},
            "formula": "v*t",
            "output_unit": "km",
        },
    )
    assert r.json()["value"] == "120"
    r = client.post(
        "/solve",
        json={
            "variables": {"F": {"value": "6", "unit": "N"}, "m": {"value": "2", "unit": "kg"}},
            "equation": "F = m*a",
            "solve_for": "a",
            "output_unit": "m/s^2",
        },
    )
    assert r.json()["value"] == "3"


def test_inline_custom_units_still_work_alongside_publishing():
    # 发布能力不影响原有请求内 custom_units
    publish([cu("ft", "1", "m")])
    r = client.post(
        "/convert",
        json={
            "value": "10", "from": "ft", "to": "m",
            "custom_units": [cu("ft", "0.3048", "m")],
        },
    )
    assert r.json() == {"value": "3.048", "unit": "m", "dimension": "m"}
