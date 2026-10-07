"""跨工作单结果绑定（POST /revisable-worksheets/{id}/bindings）端到端测试。

覆盖：

- 关联创建：提交上游标识、步骤名、upstream_revision 与含绑定变量的下游
  工作单；以步骤结果的值与单位覆盖绑定变量，下游整单计算成功才保存关联
  与下游（201），返回下游标识、两单修订号与绑定结果；两单固定同一份
  已发布定义集；
- 错误：上游不存在 404；修订号过期 409（带回当前号）；步骤名/绑定变量
  不存在、下游工作单非法、量纲不兼容等 422，且不创建下游；
- 一对一/无链式：同一上游不能绑定第二个下游；已是下游的工作单不能再做
  上游（不能 B→C）；其他工作单不能再绑定到一个下游（不能 C→A）；不允许
  自关联（ID 由服务端生成，经由存储层约束覆盖）；
- 上游修订：沿用 PATCH /revisable-worksheets/{id}，自动用新步骤结果覆盖
  绑定变量并整单重算下游，两单输入、结果与修订号共同落盘；下游重算失败
  返回 422 且两单均不变；写盘失败两单一起回滚；
- 下游自治：可修改其他变量或步骤；直接修改绑定变量 422；
- 读取：下游响应含 binding（上游标识、步骤、绑定变量及对应修订号）；
  无关联工作单响应不含 binding，行为不变；
- 并发：上游与下游的并发修改在同一把锁内串行，读者不会见到两单混版；
- 持久化：容器/进程重建后关联仍生效，可继续随上游修订联动。
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


def upstream_body(set_id):
    return {
        "variables": {
            "v": {"value": "60", "unit": "km/h"},
            "t": {"value": "2", "unit": "h"},
        },
        "steps": [
            {"name": "s", "formula": "v*t", "output_unit": "km"},
            {"name": "d", "equation": "v*th = s", "solve_for": "th",
             "output_unit": "h"},
        ],
        "custom_unit_set_id": set_id,
    }


def downstream_body(*, var_unit="km", extra=None):
    body = {
        # 绑定变量 x 的提交值与单位只是占位：创建时被上游步骤结果覆盖
        "variables": {"x": {"value": "0", "unit": var_unit}},
        "steps": [
            {"name": "y", "formula": "x/2", "output_unit": "km"},
            {"name": "z", "formula": "y", "output_unit": "m"},
        ],
    }
    if extra:
        body.update(extra)
    return body


def create_upstream(set_id):
    r = client.post("/revisable-worksheets", json=upstream_body(set_id))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def bind(uid, revision, step_name="s", bound_variable="x", body=None):
    if body is None:
        body = downstream_body()
    return client.post(
        f"/revisable-worksheets/{uid}/bindings",
        json={
            "upstream_revision": revision,
            "step_name": step_name,
            "bound_variable": bound_variable,
            **body,
        },
    )


def patch(wid, expected, **change):
    return client.patch(
        f"/revisable-worksheets/{wid}",
        json={"expected_revision": expected, **change},
    )


# ---------------------------------------------------------------------------
# 创建关联
# ---------------------------------------------------------------------------


def test_bind_overrides_variable_and_creates_downstream():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    r = bind(uid, 1)
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["upstream_worksheet_id"] == uid
    assert payload["upstream_revision"] == 1
    assert payload["downstream_revision"] == 1
    assert payload["downstream_worksheet_id"].startswith("rws_")
    assert payload["step_name"] == "s"
    assert payload["bound_variable"] == "x"
    # 绑定结果即上游 s：v*t = 120 km
    assert payload["bound_result"] == {
        "name": "s", "value": "120", "unit": "km", "dimension": "m"
    }
    downstream = payload["downstream"]
    # 提交的占位值 0 被上游结果 120 覆盖，单位也被覆盖
    assert downstream["variables"]["x"] == {"value": "120", "unit": "km"}
    assert downstream["custom_unit_set_id"] == sid
    # 下游整单按覆盖后的 x 重算：y = x/2 = 60 km，z = 60000 m
    assert [s["value"] for s in downstream["results"]] == ["60", "60000"]
    # 读取下游可见上游标识、步骤及对应修订号
    assert downstream["binding"] == {
        "upstream_worksheet_id": uid,
        "step_name": "s",
        "variable": "x",
        "upstream_revision": 1,
    }


def test_bind_downstream_pinned_to_same_published_set_as_upstream():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    r = bind(uid, 1)
    assert r.status_code == 201, r.text
    did = r.json()["downstream_worksheet_id"]
    got = client.get(f"/revisable-worksheets/{did}").json()
    assert got["custom_unit_set_id"] == sid


def test_bound_value_and_unit_come_from_upstream_step_result():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    # 绑定 d（结果为 2 h），占位变量给成不相关的单位/值，全部被覆盖
    r = bind(uid, 1, step_name="d", body={
        "variables": {"x": {"value": "9.99", "unit": "m"}},
        "steps": [
            {"name": "y", "formula": "x/2", "output_unit": "min"},
            {"name": "z", "formula": "y", "output_unit": "h"},
        ],
    })
    assert r.status_code == 201, r.text
    payload = r.json()
    assert payload["bound_result"] == {
        "name": "d", "value": "2", "unit": "h", "dimension": "s"
    }
    assert payload["downstream"]["variables"]["x"] == {"value": "2", "unit": "h"}
    # x = 2 h：y = x/2 = 1 h = 60 min，z = y = 1 h
    assert [s["value"] for s in payload["downstream"]["results"]] == ["60", "1"]


def test_unbound_worksheet_response_has_no_binding_field():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    got = client.get(f"/revisable-worksheets/{uid}").json()
    assert "binding" not in got


def test_bind_persists_downstream_and_link_to_disk():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]
    store = revisions.get_store()
    raw = json.load(open(store.path, encoding="utf-8"))
    assert did in raw["sheets"]
    assert raw["sheets"][did]["binding"] == {
        "upstream_worksheet_id": uid,
        "step_name": "s",
        "variable": "x",
        "upstream_revision": 1,
    }
    # 上游记录本身不出现绑定块（绑定信息保存在下游一侧）
    assert "binding" not in raw["sheets"][uid]


# ---------------------------------------------------------------------------
# 错误：404 / 409 / 422
# ---------------------------------------------------------------------------


def test_bind_unknown_upstream_is_404():
    r = bind("rws_does_not_exist", 1)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "worksheet_not_found"
    assert revisions.get_store().count() == 0


def test_bind_stale_revision_is_409_with_current_revision():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    r = patch(uid, 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200
    r = bind(uid, 1)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 2
    # 过期修订号不创建下游
    assert revisions.get_store().count() == 1


def test_bind_unknown_step_is_422_and_creates_nothing():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    r = bind(uid, 1, step_name="nope")
    assert r.status_code == 422
    assert revisions.get_store().count() == 1


def test_bind_unknown_bound_variable_is_422_and_creates_nothing():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    body = downstream_body()
    r = bind(uid, 1, bound_variable="missing", body=body)
    assert r.status_code == 422
    assert revisions.get_store().count() == 1


def test_bind_downstream_calculation_failure_is_422_and_creates_nothing():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    # 量纲不兼容：x = 120 km 用在时间位置上
    body = {
        "variables": {"x": {"value": "0", "unit": "km"}},
        "steps": [
            {"name": "y", "equation": "x + zp = zp", "solve_for": "zp",
             "output_unit": "km"},
            {"name": "z", "formula": "y", "output_unit": "m"},
        ],
    }
    r = bind(uid, 1, body=body)
    assert r.status_code == 422
    assert revisions.get_store().count() == 1


def test_bind_incompatible_unit_usage_still_overrides_before_running():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    # 占位单位与上游结果不同量纲（s vs km），覆盖后 x 是长度：
    # 第一步 x/2 输出 km 成功；若错误沿用占位单位则量纲冲突
    r = bind(uid, 1, body={
        "variables": {"x": {"value": "0", "unit": "s"}},
        "steps": [
            {"name": "y", "formula": "x/2", "output_unit": "km"},
            {"name": "z", "formula": "y", "output_unit": "m"},
        ],
    })
    assert r.status_code == 201, r.text
    assert r.json()["downstream"]["variables"]["x"] == {"value": "120", "unit": "km"}


def test_bind_rejects_invalid_downstream_shape():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    # 只有 1 步（工作单要求 2～5 步）
    r = bind(uid, 1, body={
        "variables": {"x": {"value": "0", "unit": "km"}},
        "steps": [{"name": "y", "formula": "x", "output_unit": "km"}],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 一对一 / 无链式
# ---------------------------------------------------------------------------


def test_upstream_can_only_have_one_downstream():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    assert bind(uid, 1).status_code == 201
    # 第二个下游：一对一冲突（即使带最新修订号）
    r = bind(uid, 1)
    assert r.status_code == 422
    assert "一对一" in r.json()["detail"]["message"]
    assert revisions.get_store().count() == 2


def test_downstream_cannot_serve_as_upstream_no_chain():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]
    # B(downstream) -> C：不允许链式
    r = bind(did, 1, step_name="y", bound_variable="q", body={
        "variables": {"q": {"value": "0", "unit": "km"}},
        "steps": [
            {"name": "a", "formula": "q", "output_unit": "km"},
            {"name": "b", "formula": "a", "output_unit": "m"},
        ],
    })
    assert r.status_code == 422
    assert "链式" in r.json()["detail"]["message"]
    assert revisions.get_store().count() == 2


def test_each_independent_upstream_allows_single_pair_and_rejects_second():
    """互不相关的两张上游各建一对一关联；各自的第二次绑定被拒绝。"""

    sid = publish([cu("ft", "0.3048", "m")])
    u1 = create_upstream(sid)
    d1 = bind(u1, 1).json()["downstream_worksheet_id"]
    u2 = create_upstream(sid)

    # u2 与 u1 无关：u2 -> 新下游是允许的独立一对一关联
    r = bind(u2, 1)
    assert r.status_code == 201, r.text
    d2 = r.json()["downstream_worksheet_id"]
    assert len({u1, d1, u2, d2}) == 4

    # u2 已成为上游：再绑第二个下游被一对一规则拒绝，不留新工作单
    r = bind(u2, 1)
    assert r.status_code == 422
    assert "一对一" in r.json()["detail"]["message"]
    assert revisions.get_store().count() == 4


def test_two_independent_pairs_are_allowed():
    sid = publish([cu("ft", "0.3048", "m")])
    u1 = create_upstream(sid)
    d1 = bind(u1, 1).json()["downstream_worksheet_id"]
    u2 = create_upstream(sid)
    r = bind(u2, 1)
    assert r.status_code == 201, r.text
    d2 = r.json()["downstream_worksheet_id"]
    assert len({u1, d1, u2, d2}) == 4


# ---------------------------------------------------------------------------
# 上游修订驱动下游
# ---------------------------------------------------------------------------


def test_patch_upstream_overrides_bound_variable_and_recomputes_downstream():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]

    r = patch(uid, 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    # 上游：s = 60*3 = 180 km
    assert r.json()["results"][0]["value"] == "180"

    downstream = client.get(f"/revisable-worksheets/{did}").json()
    # 下游绑定变量被新结果覆盖、整单重算，修订号同步递增，绑定块记录上游 2
    assert downstream["revision"] == 2
    assert downstream["variables"]["x"] == {"value": "180", "unit": "km"}
    assert [s["value"] for s in downstream["results"]] == ["90", "90000"]
    assert downstream["binding"]["upstream_revision"] == 2

    # 再修订一次：两单继续同步
    r = patch(uid, 2, variable={"name": "v", "value": "30", "unit": "km/h"})
    assert r.status_code == 200
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert downstream["revision"] == 3
    assert downstream["variables"]["x"] == {"value": "90", "unit": "km"}
    assert [s["value"] for s in downstream["results"]] == ["45", "45000"]
    assert downstream["binding"]["upstream_revision"] == 3


def test_patch_upstream_step_change_propagates_new_result_unit():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    # 下游绑定 s，后续步骤只做数值/单位换算
    did = bind(uid, 1, body={
        "variables": {"x": {"value": "0", "unit": "km"}},
        "steps": [
            {"name": "y", "formula": "x", "output_unit": "m"},
            {"name": "z", "formula": "y", "output_unit": "km"},
        ],
    }).json()["downstream_worksheet_id"]

    # 上游步骤 s 改为输出 m（公式不变）：绑定变量单位随结果变为 m
    r = patch(uid, 1, step={"name": "s", "formula": "v*t", "output_unit": "m"})
    assert r.status_code == 200, r.text
    assert r.json()["results"][0] == {
        "name": "s", "value": "120000", "unit": "m", "dimension": "m"
    }
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert downstream["variables"]["x"] == {"value": "120000", "unit": "m"}
    assert [s["value"] for s in downstream["results"]] == ["120000", "120"]


def test_downstream_recompute_failure_leaves_both_sheets_unchanged():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    # 下游第一步 x/x：x=120 时为 1；x=0 时除零
    did = bind(uid, 1, body={
        "variables": {"x": {"value": "0", "unit": "km"}},
        "steps": [
            {"name": "y", "formula": "x/x", "output_unit": "1"},
            {"name": "z", "formula": "y", "output_unit": "1"},
        ],
    }).json()["downstream_worksheet_id"]

    # 上游 t 改为 0 -> s = 0 -> 下游 x/x 除零
    r = patch(uid, 1, variable={"name": "t", "value": "0", "unit": "h"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "division_by_zero"

    # 两单输入、结果、修订号全部保持原样
    upstream = client.get(f"/revisable-worksheets/{uid}").json()
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert upstream["revision"] == 1
    assert upstream["variables"]["t"] == {"value": "2", "unit": "h"}
    assert upstream["results"][0]["value"] == "120"
    assert downstream["revision"] == 1
    assert downstream["variables"]["x"] == {"value": "120", "unit": "km"}
    assert [s["value"] for s in downstream["results"]] == ["1", "1"]
    assert downstream["binding"]["upstream_revision"] == 1

    # 失败后上游仍可按原修订号成功修订（此时下游必须能跟着成功）
    r = patch(uid, 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert downstream["revision"] == 2
    assert downstream["variables"]["x"]["value"] == "180"


def test_patch_upstream_unknown_id_is_404():
    sid = publish([cu("ft", "0.3048", "m")])
    r = patch("rws_nope", 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 下游自治：可改其他变量/步骤，绑定变量受保护
# ---------------------------------------------------------------------------


def test_downstream_can_modify_other_variables_and_steps():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    created = bind(uid, 1, body={
        "variables": {
            "x": {"value": "0", "unit": "km"},
            "k": {"value": "2", "unit": "1"},
        },
        "steps": [
            {"name": "y", "formula": "x*k", "output_unit": "km"},
            {"name": "z", "formula": "y", "output_unit": "m"},
        ],
    }).json()
    did = created["downstream_worksheet_id"]
    # 修改其他初始变量
    r = patch(did, 1, variable={"name": "k", "value": "0.5", "unit": "1"})
    assert r.status_code == 200, r.text
    # x 仍是上游的 120，y = 120*0.5 = 60
    assert r.json()["variables"]["x"] == {"value": "120", "unit": "km"}
    assert [s["value"] for s in r.json()["results"]] == ["60", "60000"]
    # 修改其他步骤
    r = patch(did, 2, step={"name": "y", "formula": "x*k/3", "output_unit": "km"})
    assert r.status_code == 200
    assert [s["value"] for s in r.json()["results"]] == ["20", "20000"]
    # 下游自己的修订不改变绑定块
    assert r.json()["binding"]["upstream_revision"] == 1


def test_direct_patch_of_bound_variable_is_422():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]
    r = patch(did, 1, variable={"name": "x", "value": "1", "unit": "km"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_request"
    # 已保存内容不变，仍可用原修订号修改其他变量
    current = client.get(f"/revisable-worksheets/{did}").json()
    assert current["revision"] == 1
    assert current["variables"]["x"]["value"] == "120"


def test_downstream_patch_failure_does_not_block_or_change_upstream():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]
    # 下游自己把步骤改坏（量纲冲突）：422，只影响下游，上游不动
    r = patch(did, 1, step={"name": "y", "formula": "x + x", "output_unit": "s"})
    assert r.status_code == 422
    upstream = client.get(f"/revisable-worksheets/{uid}").json()
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert upstream["revision"] == 1
    assert downstream["revision"] == 1
    # 上游之后仍可正常驱动下游（下游保留原步骤，联动重算成功）
    r = patch(uid, 1, variable={"name": "t", "value": "3", "unit": "h"})
    assert r.status_code == 200
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert downstream["revision"] == 2
    assert downstream["variables"]["x"]["value"] == "180"


# ---------------------------------------------------------------------------
# 乐观并发：两单串行、无混版读
# ---------------------------------------------------------------------------


def test_concurrent_upstream_patches_only_one_succeeds_per_revision():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]

    def attempt(i):
        return patch(
            uid, 1, variable={"name": "t", "value": str(2 + i), "unit": "h"}
        ).status_code

    with ThreadPoolExecutor(12) as pool:
        statuses = list(pool.map(attempt, range(24)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 23

    upstream = client.get(f"/revisable-worksheets/{uid}").json()
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert upstream["revision"] == 2
    # 下游恰好联动一次：值与上游自洽，无混版
    assert downstream["revision"] == 2
    assert downstream["binding"]["upstream_revision"] == 2
    expected = int(upstream["variables"]["t"]["value"]) * 60
    assert downstream["variables"]["x"]["value"] == str(expected)
    assert downstream["results"][0]["value"] == str(expected // 2)


def test_concurrent_reads_never_see_mixed_revisions():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]

    def worker(i):
        if i % 4 == 0:
            return ("w", patch(
                uid, 1, variable={"name": "t", "value": "5", "unit": "h"}
            ).status_code)
        # 关键不变量：每个 GET 是独立快照，读者拿到的**单张快照内部**必须
        # 自洽——下游 revision/绑定变量值/binding.upstream_revision 必须同属
        # 一个联动状态（联动前 1/120/1，联动后 2/300/2），上游同理。
        # 两次独立 GET 分别落在联动提交前后（跨快照的时间差）是允许的，
        # 但任何一张快照内部都不允许“新变量 + 旧修订号”之类的混版。
        downstream = client.get(f"/revisable-worksheets/{did}").json()
        down_rev = downstream["revision"]
        x = downstream["variables"]["x"]["value"]
        bound_rev = downstream["binding"]["upstream_revision"]
        y = downstream["results"][0]["value"]
        down_ok = (
            (down_rev == 1 and x == "120" and bound_rev == 1 and y == "60")
            or (down_rev == 2 and x == "300" and bound_rev == 2 and y == "150")
        )

        upstream = client.get(f"/revisable-worksheets/{uid}").json()
        up_rev = upstream["revision"]
        t = upstream["variables"]["t"]["value"]
        s = upstream["results"][0]["value"]
        up_ok = (
            (up_rev == 1 and t == "2" and s == "120")
            or (up_rev == 2 and t == "5" and s == "300")
        )
        return ("r", up_ok and down_ok)

    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(worker, range(128)))
    writes = [s for kind, s in outcomes if kind == "w"]
    assert 200 in writes and set(writes) <= {200, 409}
    assert all(ok for kind, ok in outcomes if kind == "r")


# ---------------------------------------------------------------------------
# 写盘失败：两单一起回滚
# ---------------------------------------------------------------------------


def test_write_failure_during_propagation_rolls_both_sheets_back():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]
    store = revisions.get_store()

    os.remove(store.path)
    os.mkdir(store.path)  # 路径变成目录，原子替换必然失败
    failing_client = TestClient(app, raise_server_exceptions=False)
    r = failing_client.patch(
        f"/revisable-worksheets/{uid}",
        json={"expected_revision": 1,
              "variable": {"name": "t", "value": "9", "unit": "h"}},
    )
    assert r.status_code == 500
    os.rmdir(store.path)

    upstream = client.get(f"/revisable-worksheets/{uid}").json()
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert upstream["revision"] == 1
    assert upstream["results"][0]["value"] == "120"
    assert downstream["revision"] == 1
    assert downstream["variables"]["x"]["value"] == "120"
    assert downstream["binding"]["upstream_revision"] == 1
    # 回滚后仍可按原修订号成功修订并联动
    r = patch(uid, 1, variable={"name": "t", "value": "5", "unit": "h"})
    assert r.status_code == 200
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert downstream["revision"] == 2
    assert downstream["variables"]["x"]["value"] == "300"


# ---------------------------------------------------------------------------
# 容器/进程重建：关联仍生效
# ---------------------------------------------------------------------------


def test_binding_survives_store_reinitialization_and_keeps_propagating():
    sid = publish([cu("ft", "0.3048", "m")])
    uid = create_upstream(sid)
    did = bind(uid, 1).json()["downstream_worksheet_id"]
    assert patch(uid, 1, variable={"name": "t", "value": "3", "unit": "h"}).status_code == 200
    data_dir = revisions.get_store().path.rsplit("/", 1)[0]

    reloaded = revisions.RevisionStore(data_dir)
    reloaded.load()
    revisions._store = reloaded
    unit_sets.init_store(data_dir)

    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert downstream["revision"] == 2
    assert downstream["variables"]["x"] == {"value": "180", "unit": "km"}
    assert downstream["binding"] == {
        "upstream_worksheet_id": uid,
        "step_name": "s",
        "variable": "x",
        "upstream_revision": 2,
    }

    # 重建后上游修订依然驱动下游
    r = patch(uid, 2, variable={"name": "t", "value": "4", "unit": "h"})
    assert r.status_code == 200
    downstream = client.get(f"/revisable-worksheets/{did}").json()
    assert downstream["revision"] == 3
    assert downstream["variables"]["x"] == {"value": "240", "unit": "km"}
    assert [s["value"] for s in downstream["results"]] == ["120", "120000"]
    assert downstream["binding"]["upstream_revision"] == 3

    # 重建后一对一约束仍然有效：不能再绑第二个下游
    r = bind(uid, 3)
    assert r.status_code == 422
    # 重建后绑定变量仍受保护
    r = patch(did, 3, variable={"name": "x", "value": "1", "unit": "km"})
    assert r.status_code == 422
