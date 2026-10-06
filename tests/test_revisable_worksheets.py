"""可修订工作单（POST/GET/PATCH /revisable-worksheets）的端到端测试。

覆盖：

- 创建：提交 2～5 步工作单 + 已发布定义集标识或活动别名；整单成功才
  保存输入、逐步结果、实际使用的定义集标识与初始修订号 1，返回工作单
  标识（201）；整单失败（含未知定义集/别名）返回 422 且不创建；
- 读取：按标识返回当前内容、结果与修订号；不存在返回 404；
- 修订：必须提交 expected_revision；只能替换一个初始变量的值与单位，
  或一个现有步骤（按名定位、名称不变）的公式/方程及目标单位；整单
  重算后共同保存新输入、新结果与递增修订号，下游步骤全部重算不沿用
  旧结果；计算失败 422 且已保存内容不变；
- 乐观并发：同一修订的并发修改最多一项成功，其余 409 并带回当前修订号；
- 定义固定：别名创建后再切换，后续修订仍用创建时固定的定义；
- 持久化：容器/进程重建后仍可读取并继续修订；写盘失败回滚，不改变
  已保存的输入、结果或修订号。
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app import revisions, unit_sets

client = TestClient(app)


def cu(symbol, factor, base_unit):
    return {"symbol": symbol, "factor": factor, "base_unit": base_unit}


def publish(defs):
    r = client.post("/custom-unit-sets", json={"custom_units": defs})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def worksheet_body(set_ref=None, *, alias=False):
    body = {
        "variables": {
            "v": {"value": "60", "unit": "km/h"},
            "t": {"value": "2", "unit": "h"},
        },
        "steps": [
            {"name": "s", "formula": "v*t", "output_unit": "km"},
            {"name": "d", "equation": "v*th = s", "solve_for": "th",
             "output_unit": "h"},
        ],
    }
    if set_ref is not None:
        if alias:
            body["custom_unit_set_alias"] = set_ref
        else:
            body["custom_unit_set_id"] = set_ref
    return body


def create(body=None, *, set_id=None, alias=None):
    if body is None:
        if alias is not None:
            body = worksheet_body(alias, alias=True)
        else:
            body = worksheet_body(set_id)
    return client.post("/revisable-worksheets", json=body)


def patch(wid, expected, **change):
    return client.patch(
        f"/revisable-worksheets/{wid}",
        json={"expected_revision": expected, **change},
    )


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


def test_create_with_set_id_returns_id_revision_and_full_content():
    sid = publish([cu("ft", "0.3048", "m")])
    r = create(set_id=sid)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("rws_")
    assert len(body["id"]) > len("rws_") + 8
    assert body["revision"] == 1
    assert body["custom_unit_set_id"] == sid
    assert body["variables"] == {
        "v": {"value": "60", "unit": "km/h"},
        "t": {"value": "2", "unit": "h"},
    }
    assert body["results"] == [
        {"name": "s", "value": "120", "unit": "km", "dimension": "m"},
        {"name": "d", "value": "2", "unit": "h", "dimension": "s"},
    ]
    # steps 回显定义（求值/反求字段各自保留）
    assert body["steps"][0]["formula"] == "v*t"
    assert body["steps"][1]["equation"] == "v*th = s"


def test_create_with_alias_pins_resolved_set_id():
    sid = publish([cu("ft", "0.3048", "m")])
    r = client.put(
        "/custom-unit-set-aliases/lengths",
        json={"custom_unit_set_id": sid, "expected_id": None},
    )
    assert r.status_code == 200
    r = create(alias="lengths")
    assert r.status_code == 201, r.text
    assert r.json()["custom_unit_set_id"] == sid
    # 响应不回显别名，只回显固定下来的标识
    assert "custom_unit_set_alias" not in r.json()


def test_create_persists_to_disk():
    sid = publish([cu("ft", "0.3048", "m")])
    r = create(set_id=sid)
    store = revisions.get_store()
    assert os.path.exists(store.path)
    raw = json.load(open(store.path, encoding="utf-8"))
    assert raw["version"] == 1
    assert r.json()["id"] in raw["sheets"]


def test_create_requires_exactly_one_reference():
    body = worksheet_body()
    r = client.post("/revisable-worksheets", json=body)
    assert r.status_code == 422
    sid = publish([cu("ft", "0.3048", "m")])
    body = worksheet_body(sid)
    body["custom_unit_set_alias"] = "lengths"
    r = client.post("/revisable-worksheets", json=body)
    assert r.status_code == 422


def test_create_unknown_set_id_or_alias_is_422_and_creates_nothing():
    body = worksheet_body("cus_does_not_exist")
    r = client.post("/revisable-worksheets", json=body)
    assert r.status_code == 422
    body = worksheet_body("no_such_alias", alias=True)
    r = client.post("/revisable-worksheets", json=body)
    assert r.status_code == 422
    assert revisions.get_store().count() == 0


def test_create_calculation_failure_is_422_and_creates_nothing():
    sid = publish([cu("ft", "0.3048", "m")])
    body = worksheet_body(sid)
    # 量纲不兼容：把 v 改为时间，第一步 v*t 即量纲冲突
    body["variables"]["v"] = {"value": "60", "unit": "h"}
    r = client.post("/revisable-worksheets", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "dimension_mismatch"
    assert revisions.get_store().count() == 0


def test_create_rejects_inline_custom_units_source():
    # 请求级 custom_units 不被接受（只接受已发布定义集标识/别名）
    body = worksheet_body()
    body["custom_units"] = [cu("ft", "0.3048", "m")]
    r = client.post("/revisable-worksheets", json=body)
    assert r.status_code == 422


def test_create_unique_ids():
    sid = publish([cu("ft", "0.3048", "m")])
    ids = {create(set_id=sid).json()["id"] for _ in range(3)}
    assert len(ids) == 3


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def test_get_returns_current_revision():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    r = client.get(f"/revisable-worksheets/{wid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == wid
    assert body["revision"] == 1
    assert body["results"][0]["value"] == "120"


def test_get_unknown_id_is_404():
    r = client.get("/revisable-worksheets/rws_does_not_exist")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "worksheet_not_found"


# ---------------------------------------------------------------------------
# 修订：初始变量
# ---------------------------------------------------------------------------


def test_patch_variable_recomputes_whole_sheet_and_increments_revision():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    r = patch(wid, 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["variables"]["t"] == {"value": "3", "unit": "h"}
    # 第一步 v*t = 180 km；第二步 th = s/v = 3 h（下游也来自本次重算）
    assert [step["value"] for step in body["results"]] == ["180", "3"]


def test_patch_variable_can_change_unit():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    # 同一物理量换单位：60 km/h = 1 km/min，t=2 h 不变；
    # s = 1 km/min * 2 h = 120 km，th = s/v = 120 km/(60 km/h) = 2 h
    r = patch(wid, 1, variable={"name": "v", "value": "1", "unit": "km/min"})
    assert r.status_code == 200, r.text
    assert [step["value"] for step in r.json()["results"]] == ["120", "2"]


def test_patch_variable_must_reference_existing_name():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    r = patch(wid, 1, variable={"name": "nope", "value": "1", "unit": "h"})
    assert r.status_code == 422
    # 名称不合法也是 422（schema 层）
    r = patch(wid, 1, variable={"name": "1bad", "value": "1", "unit": "h"})
    assert r.status_code == 422


def test_patch_variable_calculation_failure_leaves_saved_revision_intact():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    r = patch(wid, 1, variable={"name": "v", "value": "60", "unit": "h"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "dimension_mismatch"
    # 已保存内容、结果、修订号不变
    current = client.get(f"/revisable-worksheets/{wid}").json()
    assert current["revision"] == 1
    assert current["variables"]["v"]["value"] == "60"
    assert current["results"][0]["value"] == "120"
    # 失败后仍可用原修订号继续修订
    r = patch(wid, 1, variable={"name": "t", "value": "4", "unit": "h"})
    assert r.status_code == 200 and r.json()["revision"] == 2


# ---------------------------------------------------------------------------
# 修订：步骤
# ---------------------------------------------------------------------------


def test_patch_step_formula_and_target_unit_recomputes_downstream():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    # 把第一步公式改为 v*t/2，目标单位改为 m；第二步引用 s，结果随之变化
    r = patch(wid, 1, step={
        "name": "s", "formula": "v*t/2", "output_unit": "m",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["steps"][0]["formula"] == "v*t/2"
    assert body["results"][0] == {"name": "s", "value": "60000", "unit": "m",
                                  "dimension": "m"}
    # 下游 th = s/v = 1 h，而非旧的 2 h——证明下游没有沿用旧结果
    assert body["results"][1]["value"] == "1"


def test_patch_step_can_swap_between_formula_and_solve_shapes():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    # 反求步骤 d -> 求值步骤（名称不变）
    r = patch(wid, 1, step={"name": "d", "formula": "s/v", "output_unit": "h"})
    assert r.status_code == 200, r.text
    assert r.json()["results"][1]["value"] == "2"
    assert "equation" not in r.json()["steps"][1]
    # 再换回反求步骤
    r = patch(wid, 2, step={
        "name": "d", "equation": "v*th = s", "solve_for": "th",
        "output_unit": "min",
    })
    assert r.status_code == 200, r.text
    assert r.json()["results"][1]["unit"] == "min"
    assert r.json()["results"][1]["value"] == "120"


def test_patch_step_must_reference_existing_name_and_keeps_name():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    r = patch(wid, 1, step={"name": "nope", "formula": "1", "output_unit": "1"})
    assert r.status_code == 422
    # 不能借替换重命名：新名字即便合法，也必须能在现有步骤中找到
    r = patch(wid, 1, step={"name": "s2", "formula": "1", "output_unit": "1"})
    assert r.status_code == 422


def test_patch_step_shape_validated_like_worksheet_step():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    # formula 与 equation 混给
    r = patch(wid, 1, step={
        "name": "s", "formula": "v*t", "equation": "x = 1",
        "solve_for": "x", "output_unit": "km",
    })
    assert r.status_code == 422
    # 反求步骤缺 solve_for
    r = patch(wid, 1, step={
        "name": "s", "equation": "x = 1", "output_unit": "km",
    })
    assert r.status_code == 422
    # 反求步骤目标单位为绝对温标
    r = patch(wid, 1, step={
        "name": "d", "equation": "x = 1", "solve_for": "x",
        "output_unit": "degC",
    })
    assert r.status_code == 422
    # 修订未发生
    assert client.get(f"/revisable-worksheets/{wid}").json()["revision"] == 1


def test_patch_step_failure_does_not_reuse_old_downstream_results():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    # 把第一步改成与 v 量纲不兼容的公式：整单失败，422，无任何保存
    r = patch(wid, 1, step={"name": "s", "formula": "v+t", "output_unit": "km"})
    assert r.status_code == 422
    current = client.get(f"/revisable-worksheets/{wid}").json()
    assert current["revision"] == 1
    assert current["results"][0]["value"] == "120"


def test_patch_requires_exactly_one_change_kind():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    r = client.patch(f"/revisable-worksheets/{wid}", json={"expected_revision": 1})
    assert r.status_code == 422
    r = client.patch(
        f"/revisable-worksheets/{wid}",
        json={
            "expected_revision": 1,
            "variable": {"name": "t", "value": "3", "unit": "h"},
            "step": {"name": "s", "formula": "v*t", "output_unit": "km"},
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 乐观并发
# ---------------------------------------------------------------------------


def test_concurrent_patches_on_same_revision_only_one_succeeds():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]

    def attempt(i):
        return patch(wid, 1, variable={"name": "t", "value": str(2 + i), "unit": "h"}).status_code

    with ThreadPoolExecutor(12) as pool:
        statuses = list(pool.map(attempt, range(24)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 23

    current = client.get(f"/revisable-worksheets/{wid}").json()
    assert current["revision"] == 2
    # 409 响应带回当前修订号
    r = patch(wid, 1, variable={"name": "t", "value": "9", "unit": "h"})
    assert r.status_code == 409
    assert r.json()["detail"]["current_revision"] == 2
    # 落选者重新读取后以新修订号重试可以成功
    r = patch(wid, 2, variable={"name": "t", "value": "9", "unit": "h"})
    assert r.status_code == 200 and r.json()["revision"] == 3


def test_patch_unknown_id_is_404_even_with_conflicting_revision():
    sid = publish([cu("ft", "0.3048", "m")])
    r = patch("rws_nope", 1, variable={"name": "t", "value": "9", "unit": "h"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 定义固定：别名切换后仍用创建时的定义
# ---------------------------------------------------------------------------


def test_alias_switch_after_creation_does_not_change_fixed_definitions():
    # 两版 ft：v1 = 0.3048 m，v2 = 0.31 m（自定义长度单位 foo 承载差异）
    v1 = publish([cu("foo", "0.3048", "m")])
    v2 = publish([cu("foo", "0.31", "m")])
    client.put(
        "/custom-unit-set-aliases/lengths",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )

    body = {
        "variables": {"x": {"value": "10", "unit": "foo"}},
        "steps": [
            {"name": "a", "formula": "x", "output_unit": "foo"},
            {"name": "b", "formula": "x", "output_unit": "m"},
        ],
        "custom_unit_set_alias": "lengths",
    }
    created = client.post("/revisable-worksheets", json=body)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    assert created.json()["custom_unit_set_id"] == v1
    assert created.json()["results"][1]["value"] == "3.048"

    # 别名切换到 v2
    r = client.put(
        "/custom-unit-set-aliases/lengths",
        json={"custom_unit_set_id": v2, "expected_id": v1},
    )
    assert r.status_code == 200

    # 修订（即使仍想用别名，也只认创建时固定的 v1 标识）
    r = patch(wid, 1, variable={"name": "x", "value": "20", "unit": "foo"})
    assert r.status_code == 200, r.text
    assert r.json()["custom_unit_set_id"] == v1
    assert r.json()["results"][1]["value"] == "6.096"  # 20 * 0.3048，而非 20 * 0.31


# ---------------------------------------------------------------------------
# 持久化：容器/进程重建后仍可读取并继续修订
# ---------------------------------------------------------------------------


def test_sheets_survive_store_reinitialization():
    sid = publish([cu("ft", "0.3048", "m")])
    created = create(set_id=sid)
    wid = created.json()["id"]
    data_dir = revisions.get_store().path.rsplit("/", 1)[0]

    # 做一次成功修订，磁盘上应为修订号 2
    r = patch(wid, 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200

    # 模拟容器/进程重建：在同一数据目录重新加载两个存储
    reloaded = revisions.RevisionStore(data_dir)
    reloaded.load()
    revisions._store = reloaded
    unit_sets.init_store(data_dir)

    got = client.get(f"/revisable-worksheets/{wid}").json()
    assert got["revision"] == 2
    assert got["custom_unit_set_id"] == sid
    assert [s["value"] for s in got["results"]] == ["180", "3"]

    # 可继续修订（且仍使用固定的定义集）
    r = patch(wid, 2, variable={"name": "t", "value": "4", "unit": "h"})
    assert r.status_code == 200
    assert r.json()["revision"] == 3
    assert [s["value"] for s in r.json()["results"]] == ["240", "4"]


def test_loaded_results_match_fresh_worksheet_execution():
    """每个已保存修订的 results 都必须与用同版定义直接跑 /worksheet 一致。"""

    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    patch(wid, 1, variable={"name": "t", "value": "0.5", "unit": "h"})
    current = client.get(f"/revisable-worksheets/{wid}").json()
    direct_body = {
        "variables": {
            name: {"value": var["value"], "unit": var["unit"]}
            for name, var in current["variables"].items()
        },
        "steps": current["steps"],
        "custom_unit_set_id": current["custom_unit_set_id"],
    }
    direct = client.post("/worksheet", json=direct_body)
    assert direct.status_code == 200
    assert direct.json()["steps"] == current["results"]


def test_write_failure_rolls_back_saved_revision():
    sid = publish([cu("ft", "0.3048", "m")])
    wid = create(set_id=sid).json()["id"]
    store = revisions.get_store()

    # 把存储文件路径变成目录，原子替换必然失败
    os.remove(store.path)
    os.mkdir(store.path)
    failing_client = TestClient(app, raise_server_exceptions=False)
    r = failing_client.patch(
        f"/revisable-worksheets/{wid}",
        json={"expected_revision": 1,
              "variable": {"name": "t", "value": "9", "unit": "h"}},
    )
    assert r.status_code == 500
    os.rmdir(store.path)

    current = client.get(f"/revisable-worksheets/{wid}").json()
    assert current["revision"] == 1
    assert current["variables"]["t"]["value"] == "2"
    assert current["results"][0]["value"] == "120"
    # 回滚后仍可按原修订号成功修订
    r = patch(wid, 1, variable={"name": "t", "value": "5", "unit": "h"})
    assert r.status_code == 200 and r.json()["revision"] == 2
