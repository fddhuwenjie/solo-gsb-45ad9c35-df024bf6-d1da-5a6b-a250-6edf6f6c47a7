"""工作单步骤结果绑定（POST /revisable-worksheets/{id}/bindings）的端到端测试。

覆盖：

- 建立关联：提交上游标识（路径参数）、上游步骤名、上游 expected_revision
  与含同名绑定变量的下游工作单；以步骤结果的值与单位覆盖该变量，下游整单
  计算成功才共同保存关联与下游（201），返回下游标识、两单修订号与结果，
  下游固定沿用上游的同一份已发布定义集；
- 读取：下游响应可见上游标识、步骤名与对应的上游修订号；上游与无关联工作单
  响应不含 binding 字段；
- 一对一 / 非链式：同一上游不能绑定第二个下游；已经是下游的单不能再作为上游；
- 乐观并发：上游修订号过期返回带 current_revision 的 409，不建立关联；
- 上游不存在返回 404；步骤名缺失、下游缺少同名变量、下游计算失败返回 422，
  且不创建任何工作单或关联；
- 上游修订（PATCH）：用新步骤结果覆盖下游绑定变量并整单重算下游，两单的
  输入、结果与修订号共同落盘；下游计算失败（422）或写盘失败时两单都不变；
- 下游自治：可修改其他变量或步骤；直接修改绑定变量返回 422；
- 容器/进程重建后关联仍生效并可继续联动；无关联工作单行为不变。
"""

from __future__ import annotations

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


def create_upstream(set_id=None, *, body=None, revision_check=None):
    if body is None:
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
    body = dict(body)
    body["custom_unit_set_id"] = set_id
    r = client.post("/revisable-worksheets", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def bind(upstream_id, step_name, expected_revision, *, variables, steps):
    return client.post(
        f"/revisable-worksheets/{upstream_id}/bindings",
        json={
            "step_name": step_name,
            "expected_revision": expected_revision,
            "variables": variables,
            "steps": steps,
        },
    )


def patch(wid, expected, **change):
    return client.patch(
        f"/revisable-worksheets/{wid}",
        json={"expected_revision": expected, **change},
    )


def standard_downstream():
    """占位 s=0 h 会被上游 s=120 km 覆盖；下游再引用 s 与乘数 k。"""

    return {
        "variables": {
            "s": {"value": "0", "unit": "h"},
            "k": {"value": "2", "unit": "1"},
        },
        "steps": [
            {"name": "half", "formula": "s/2", "output_unit": "km"},
            {"name": "again", "formula": "half*k", "output_unit": "km"},
        ],
    }


# ---------------------------------------------------------------------------
# 建立关联
# ---------------------------------------------------------------------------


def test_bind_overrides_variable_and_creates_downstream():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    r = bind(up["id"], "s", 1, **standard_downstream())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("rws_")
    assert body["id"] != up["id"]
    # 下游初始修订号为 1；绑定信息含上游标识、步骤名与对应上游修订号
    assert body["revision"] == 1
    assert body["binding"] == {
        "upstream_worksheet_id": up["id"],
        "step_name": "s",
        "upstream_revision": 1,
    }
    # 占位值 0 h 被上游步骤结果 120 km 整体覆盖（值与单位）
    assert body["variables"]["s"] == {"value": "120", "unit": "km"}
    assert body["variables"]["k"] == {"value": "2", "unit": "1"}
    # 下游结果来自覆盖后的整单计算
    assert body["results"] == [
        {"name": "half", "value": "60", "unit": "km", "dimension": "m"},
        {"name": "again", "value": "120", "unit": "km", "dimension": "m"},
    ]
    # 下游固定与上游相同的已发布定义集标识
    assert body["custom_unit_set_id"] == sid


def test_bind_creates_no_worksheet_when_downstream_calculation_fails():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    before = revisions.get_store().count()
    # s 被覆盖为长度 km，但第一步输出到时间单位 s -> 量纲冲突
    r = bind(
        up["id"], "s", 1,
        variables={"s": {"value": "0", "unit": "h"}},
        steps=[
            {"name": "a", "formula": "s", "output_unit": "h"},
            {"name": "b", "formula": "a", "output_unit": "h"},
        ],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "dimension_mismatch"
    # 不留下下游工作单或关联
    assert revisions.get_store().count() == before
    assert "binding" not in client.get(f"/revisable-worksheets/{up['id']}").json()


def test_bind_unknown_upstream_is_404():
    r = client.post(
        "/revisable-worksheets/rws_does_not_exist/bindings",
        json={
            "step_name": "s",
            "expected_revision": 1,
            "variables": {"s": {"value": "0", "unit": "h"}},
            "steps": [
                {"name": "a", "formula": "s", "output_unit": "km"},
                {"name": "b", "formula": "a", "output_unit": "km"},
            ],
        },
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "worksheet_not_found"


def test_bind_stale_upstream_revision_is_409_with_current_revision():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    r = patch(up["id"], 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200
    before = revisions.get_store().count()
    r = bind(up["id"], "s", 1, **standard_downstream())
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 2
    # 冲突不建立关联、不创建下游
    assert revisions.get_store().count() == before
    assert "binding" not in client.get(f"/revisable-worksheets/{up['id']}").json()


def test_bind_unknown_step_name_is_422():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    payload = standard_downstream()
    payload["variables"] = {"nope": {"value": "0", "unit": "h"}}
    r = bind(up["id"], "nope", 1, **payload)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_request"
    assert revisions.get_store().count() == 1


def test_bind_requires_same_named_downstream_variable():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    # 上游步骤 s 存在，但下游没有同名初始变量
    r = bind(
        up["id"], "s", 1,
        variables={"other": {"value": "0", "unit": "h"}},
        steps=[
            {"name": "a", "formula": "other", "output_unit": "h"},
            {"name": "b", "formula": "a", "output_unit": "h"},
        ],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_request"
    assert revisions.get_store().count() == 1


def test_bind_downstream_uses_upstream_pinned_definition_set():
    # 上游用别名创建并固定在 v1；关联与下游联动始终使用 v1
    v1 = publish([cu("foo", "0.3048", "m")])
    v2 = publish([cu("foo", "0.31", "m")])
    client.put(
        "/custom-unit-set-aliases/lengths",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    created = client.post(
        "/revisable-worksheets",
        json={
            "variables": {"x": {"value": "10", "unit": "foo"}},
            "steps": [
                {"name": "a", "formula": "x", "output_unit": "foo"},
                {"name": "b", "formula": "x", "output_unit": "m"},
            ],
            "custom_unit_set_alias": "lengths",
        },
    )
    assert created.status_code == 201
    uid = created.json()["id"]
    client.put(
        "/custom-unit-set-aliases/lengths",
        json={"custom_unit_set_id": v2, "expected_id": v1},
    )
    r = bind(
        uid, "b", 1,
        variables={"b": {"value": "0", "unit": "m"}},
        steps=[
            {"name": "c", "formula": "b", "output_unit": "m"},
            {"name": "e", "formula": "c*2", "output_unit": "m"},
        ],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # 下游沿用上游固定的 v1 标识（别名已切到 v2 也不受影响）
    assert body["custom_unit_set_id"] == v1
    # b 覆盖为 10 foo 折成的 3.048 m（v1），不是 v2 的 3.1 m
    assert body["results"][0]["value"] == "3.048"


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def test_downstream_get_shows_binding_upstream_get_does_not():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    bound = bind(up["id"], "s", 1, **standard_downstream())
    did = bound.json()["id"]

    got = client.get(f"/revisable-worksheets/{did}").json()
    assert got["binding"] == {
        "upstream_worksheet_id": up["id"],
        "step_name": "s",
        "upstream_revision": 1,
    }
    # 上游与无关联工作单不携带 binding 字段
    assert "binding" not in client.get(f"/revisable-worksheets/{up['id']}").json()
    other = create_upstream(sid)
    assert "binding" not in client.get(
        f"/revisable-worksheets/{other['id']}"
    ).json()


# ---------------------------------------------------------------------------
# 一对一、非链式
# ---------------------------------------------------------------------------


def test_upstream_can_have_only_one_downstream():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    r = bind(up["id"], "s", 1, **standard_downstream())
    assert r.status_code == 201
    before = revisions.get_store().count()
    # 同一上游再绑第二个下游 -> 422，且不创建
    r = bind(up["id"], "d", 1, **{
        "variables": {
            "d": {"value": "0", "unit": "h"},
            "k": {"value": "1", "unit": "1"},
        },
        "steps": [
            {"name": "a", "formula": "d", "output_unit": "h"},
            {"name": "b", "formula": "a*k", "output_unit": "h"},
        ],
    })
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_request"
    assert revisions.get_store().count() == before


def test_downstream_cannot_serve_as_upstream_no_chaining():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]
    before = revisions.get_store().count()
    # 试图把下游再作为上游 -> 链式关联，422
    r = bind(
        did, "half", 1,
        variables={"half": {"value": "0", "unit": "km"}},
        steps=[
            {"name": "z1", "formula": "half", "output_unit": "km"},
            {"name": "z2", "formula": "z1", "output_unit": "km"},
        ],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_request"
    assert revisions.get_store().count() == before


# ---------------------------------------------------------------------------
# 上游修订联动下游
# ---------------------------------------------------------------------------


def test_upstream_patch_overrides_bound_variable_and_recomputes_downstream():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]

    # 上游 t: 2 h -> 3 h；s 从 120 km 变为 180 km
    r = patch(up["id"], 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    assert [x["value"] for x in r.json()["results"]] == ["180", "3"]

    # 两单共同落盘：下游修订号 1 -> 2，绑定变量与结果同步
    down = client.get(f"/revisable-worksheets/{did}").json()
    assert down["revision"] == 2
    assert down["variables"]["s"] == {"value": "180", "unit": "km"}
    assert [x["value"] for x in down["results"]] == ["90", "180"]
    assert down["binding"]["upstream_revision"] == 2


def test_upstream_patch_propagates_new_step_unit_and_value():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    # 下游绑定 s，单位 km
    did = bind(up["id"], "s", 1, **{
        "variables": {"s": {"value": "0", "unit": "km"}},
        "steps": [
            {"name": "m", "formula": "s", "output_unit": "m"},
            {"name": "mm", "formula": "s", "output_unit": "m"},
        ],
    }).json()["id"]
    assert client.get(f"/revisable-worksheets/{did}").json()["results"][0]["value"] == "120000"
    # 上游把 s 的目标单位改为 m：步骤结果单位变化同步覆盖下游变量
    r = patch(up["id"], 1, step={"name": "s", "formula": "v*t", "output_unit": "m"})
    assert r.status_code == 200, r.text
    down = client.get(f"/revisable-worksheets/{did}").json()
    assert down["variables"]["s"] == {"value": "120000", "unit": "m"}
    assert [x["value"] for x in down["results"]] == ["120000", "120000"]
    assert down["binding"]["upstream_revision"] == 2


def test_upstream_patch_with_downstream_failure_changes_neither_sheet():
    sid = publish([cu("ft", "0.3048", "m")])
    up_body = {
        "variables": {"x": {"value": "10", "unit": "ft"}},
        "steps": [
            {"name": "a", "formula": "x", "output_unit": "m"},
            {"name": "b", "formula": "x", "output_unit": "ft"},
        ],
    }
    up = create_upstream(sid, body=up_body)
    # 下游 b=10 ft，b*b 输出 ft^2
    did = bind(up["id"], "b", 1, **{
        "variables": {"b": {"value": "1", "unit": "ft"}},
        "steps": [
            {"name": "a2", "formula": "b*b", "output_unit": "ft^2"},
            {"name": "b2", "formula": "a2", "output_unit": "ft^2"},
        ],
    }).json()["id"]

    # 上游把 b 改成无量纲：上游自身仍可计算，但联动下游时 b*b -> ft^2 量纲冲突
    r = patch(up["id"], 1, step={"name": "b", "formula": "1", "output_unit": "1"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "dimension_mismatch"
    # 两单的输入、结果与修订号都不变
    up_now = client.get(f"/revisable-worksheets/{up['id']}").json()
    down_now = client.get(f"/revisable-worksheets/{did}").json()
    assert up_now["revision"] == 1
    assert down_now["revision"] == 1
    assert up_now["steps"][1]["formula"] == "x"
    assert up_now["results"][1] == {"name": "b", "value": "10", "unit": "ft",
                                   "dimension": "m"}
    assert down_now["variables"]["b"] == {"value": "10", "unit": "ft"}
    assert [x["value"] for x in down_now["results"]] == ["100", "100"]
    # 失败后仍可用原修订号成功修订
    r = patch(up["id"], 1, variable={"name": "x", "value": "20", "unit": "ft"})
    assert r.status_code == 200
    down_now = client.get(f"/revisable-worksheets/{did}").json()
    assert down_now["variables"]["b"] == {"value": "20", "unit": "ft"}


def test_upstream_patch_stale_revision_is_409_and_neither_sheet_changes():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]
    assert patch(up["id"], 1, variable={"name": "t", "value": "3", "unit": "h"}).status_code == 200
    r = patch(up["id"], 1, variable={"name": "t", "value": "4", "unit": "h"})
    assert r.status_code == 409
    assert r.json()["detail"]["current_revision"] == 2
    assert client.get(f"/revisable-worksheets/{up['id']}").json()["revision"] == 2
    # 联动没有发生：下游仍为上游修订 2 联动后的修订 2
    down = client.get(f"/revisable-worksheets/{did}").json()
    assert down["revision"] == 2
    assert down["binding"]["upstream_revision"] == 2


def test_upstream_patch_write_failure_rolls_back_both_sheets():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]
    store = revisions.get_store()

    os.remove(store.path)
    os.mkdir(store.path)  # 原子替换必然失败
    failing = TestClient(app, raise_server_exceptions=False)
    r = failing.patch(
        f"/revisable-worksheets/{up['id']}",
        json={"expected_revision": 1,
              "variable": {"name": "t", "value": "9", "unit": "h"}},
    )
    assert r.status_code == 500
    os.rmdir(store.path)

    up_now = client.get(f"/revisable-worksheets/{up['id']}").json()
    down_now = client.get(f"/revisable-worksheets/{did}").json()
    assert up_now["revision"] == 1
    assert down_now["revision"] == 1
    assert up_now["variables"]["t"]["value"] == "2"
    assert down_now["variables"]["s"] == {"value": "120", "unit": "km"}
    # 回滚后两单仍可按原修订号继续
    r = patch(up["id"], 1, variable={"name": "t", "value": "4", "unit": "h"})
    assert r.status_code == 200
    assert client.get(f"/revisable-worksheets/{did}").json()["revision"] == 2


def test_concurrent_upstream_patches_only_one_propagates():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]

    def attempt(i):
        return patch(
            up["id"], 1, variable={"name": "t", "value": str(2 + i), "unit": "h"}
        ).status_code

    with ThreadPoolExecutor(12) as pool:
        statuses = list(pool.map(attempt, range(20)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 19

    up_now = client.get(f"/revisable-worksheets/{up['id']}").json()
    down_now = client.get(f"/revisable-worksheets/{did}").json()
    assert up_now["revision"] == 2
    # 只有一次联动：下游修订号恰为 2，且绑定值（值+单位）与上游当前步骤结果一致
    assert down_now["revision"] == 2
    expected_s = str(int(up_now["variables"]["t"]["value"]) * 60)
    assert down_now["variables"]["s"] == {"value": expected_s, "unit": "km"}
    assert {
        "value": up_now["results"][0]["value"],
        "unit": up_now["results"][0]["unit"],
    } == down_now["variables"]["s"]
    assert down_now["binding"]["upstream_revision"] == 2


# ---------------------------------------------------------------------------
# 下游自治
# ---------------------------------------------------------------------------


def test_downstream_can_modify_other_variables_and_steps():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]

    # 修改非绑定变量 k
    r = patch(did, 1, variable={"name": "k", "value": "3", "unit": "1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["variables"]["s"] == {"value": "120", "unit": "km"}
    assert [x["value"] for x in body["results"]] == ["60", "180"]
    # 下游自行修订不改变绑定记录的上游修订号
    assert body["binding"]["upstream_revision"] == 1

    # 修改步骤（非绑定数据）：again 改为 half/k（仍输出 km）
    r = patch(did, 2, step={"name": "again", "formula": "half/k",
                            "output_unit": "km"})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 3
    assert [x["value"] for x in r.json()["results"]] == ["60", "20"]

    # 随后上游修订仍正常联动（下游已自行改过，按其当前步骤整单重算）
    r = patch(up["id"], 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200, r.text
    down = client.get(f"/revisable-worksheets/{did}").json()
    assert down["revision"] == 4
    assert down["variables"]["s"] == {"value": "180", "unit": "km"}
    assert [x["value"] for x in down["results"]] == ["90", "30"]
    assert down["binding"]["upstream_revision"] == 2


def test_downstream_direct_modification_of_bound_variable_is_422():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]
    r = patch(did, 1, variable={"name": "s", "value": "1", "unit": "km"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_request"
    # 已保存内容不变，仍可按原修订号修订其他变量
    assert client.get(f"/revisable-worksheets/{did}").json()["revision"] == 1
    r = patch(did, 1, variable={"name": "k", "value": "5", "unit": "1"})
    assert r.status_code == 200 and r.json()["revision"] == 2


def test_upstream_variable_change_unrelated_to_bound_step_still_propagates():
    # 上游任意成功修订都触发联动（整单重算保证一致），即便修改的变量不影响
    # 绑定步骤的数值：下游修订号与绑定的上游修订号仍同步推进
    sid = publish([cu("ft", "0.3048", "m")])
    # 上游含不参与 s 计算的变量 u
    up = create_upstream(sid, body={
        "variables": {
            "v": {"value": "60", "unit": "km/h"},
            "t": {"value": "2", "unit": "h"},
            "u": {"value": "7", "unit": "m"},
        },
        "steps": [
            {"name": "s", "formula": "v*t", "output_unit": "km"},
            {"name": "d", "equation": "v*th = s", "solve_for": "th",
             "output_unit": "h"},
        ],
    })
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]
    r = patch(up["id"], 1, variable={"name": "u", "value": "8", "unit": "m"})
    assert r.status_code == 200
    down = client.get(f"/revisable-worksheets/{did}").json()
    assert down["revision"] == 2
    assert down["variables"]["s"] == {"value": "120", "unit": "km"}
    assert down["binding"]["upstream_revision"] == 2


# ---------------------------------------------------------------------------
# 持久化：容器/进程重建
# ---------------------------------------------------------------------------


def test_binding_survives_store_reinitialization_and_keeps_propagating():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    did = bind(up["id"], "s", 1, **standard_downstream()).json()["id"]
    assert patch(up["id"], 1, variable={"name": "t", "value": "3", "unit": "h"}).status_code == 200
    data_dir = revisions.get_store().path.rsplit("/", 1)[0]

    # 模拟容器/进程重建：同一数据目录重新加载两个存储
    reloaded = revisions.RevisionStore(data_dir)
    reloaded.load()
    revisions._store = reloaded
    unit_sets.init_store(data_dir)

    up_now = client.get(f"/revisable-worksheets/{up['id']}").json()
    down = client.get(f"/revisable-worksheets/{did}").json()
    assert up_now["revision"] == 2
    assert down["revision"] == 2
    assert down["binding"] == {
        "upstream_worksheet_id": up["id"],
        "step_name": "s",
        "upstream_revision": 2,
    }
    assert down["variables"]["s"] == {"value": "180", "unit": "km"}

    # 重建后上游修订仍能联动下游
    r = patch(up["id"], 2, variable={"name": "t", "value": "4", "unit": "h"})
    assert r.status_code == 200
    down = client.get(f"/revisable-worksheets/{did}").json()
    assert down["revision"] == 3
    assert down["binding"]["upstream_revision"] == 3
    assert down["variables"]["s"] == {"value": "240", "unit": "km"}
    assert [x["value"] for x in down["results"]] == ["120", "240"]

    # 重建后仍禁止直接修改绑定变量，仍允许修改其他变量
    r = patch(did, 3, variable={"name": "s", "value": "1", "unit": "km"})
    assert r.status_code == 422
    r = patch(did, 3, variable={"name": "k", "value": "9", "unit": "1"})
    assert r.status_code == 200


def test_old_store_file_without_binding_loads_as_unbound():
    sid = publish([cu("ft", "0.3048", "m")])
    up = create_upstream(sid)
    data_dir = revisions.get_store().path.rsplit("/", 1)[0]

    # 手工构造一个没有 binding 字段的旧版记录文件
    import json
    old_store = revisions.get_store()
    raw = json.load(open(old_store.path, encoding="utf-8"))
    for record in raw["sheets"].values():
        record.pop("binding", None)
    with open(old_store.path, "w", encoding="utf-8") as f:
        json.dump(raw, f)

    reloaded = revisions.RevisionStore(data_dir)
    reloaded.load()
    revisions._store = reloaded
    got = client.get(f"/revisable-worksheets/{up['id']}").json()
    assert "binding" not in got
    # 无关联工作单行为不变，可继续修订
    r = patch(up["id"], 1, variable={"name": "t", "value": "5", "unit": "h"})
    assert r.status_code == 200 and r.json()["revision"] == 2
