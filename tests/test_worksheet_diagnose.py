"""POST /worksheet-diagnose 的端到端测试。

覆盖：
- 内部精确量比较（显示字符串相同但物理值变化也须报告，span=2m vs 3m）；
- 量纲变化、首个含义变化的定位、同一定义集两侧无变化；
- 一侧某步业务失败时保留此前对照，失败后步骤 not_run，另一侧可继续；
- 初始变量单位在一侧无法解析（setup_error）；
- 标识/活动别名引用、别名请求开始解析一次（执行中切换不混版）；
- 未知定义集/别名、非法引用、步骤数非法均为 422；
- /worksheet 原有“任一步错误仅 422、无部分结果”行为保持不变。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app import unit_sets

client = TestClient(app, raise_server_exceptions=True)


def var(value, unit):
    return {"value": value, "unit": unit}


def ev_step(name, formula, output_unit):
    return {"name": name, "formula": formula, "output_unit": output_unit}


def solve_step(name, equation, solve_for, output_unit):
    return {
        "name": name,
        "equation": equation,
        "solve_for": solve_for,
        "output_unit": output_unit,
    }


def publish(custom_units):
    resp = client.post("/custom-unit-sets", json={"custom_units": custom_units})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def diagnose(steps, old_ref, new_ref, variables=None):
    body: dict = {"steps": steps, "old": old_ref, "new": new_ref}
    if variables is not None:
        body["variables"] = variables
    return client.post("/worksheet-diagnose", json=body)


def by_id(set_id):
    return {"custom_unit_set_id": set_id}


def by_alias(name):
    return {"custom_unit_set_alias": name}


SPAN_V1 = [{"symbol": "span", "factor": "2", "base_unit": "m"}]
SPAN_V2 = [{"symbol": "span", "factor": "3", "base_unit": "m"}]
TWO_STEPS = [
    ev_step("a", "1", "1"),
    ev_step("b", "a", "1"),
]


# ---------------------------------------------------------------------------
# 内部精确量比较：显示相同但物理值变化
# ---------------------------------------------------------------------------


def test_same_display_string_but_internal_value_changed_is_reported():
    # 规格中的核心例子：旧版 span=2m、新版 span=3m。
    # 输入 1 span、输出 span：两侧都显示 "1 span"，物理值却已变化。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [
            ev_step("a", "x", "span"),
            ev_step("b", "x", "m"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # resolved_id 回显请求开始时固定的实际指向
    assert data["old"]["resolved_id"] == v1
    assert data["new"]["resolved_id"] == v2

    row0, row1 = data["steps"]
    # 第 0 步两侧显示完全相同
    assert row0["old"]["value"] == "1"
    assert row0["new"]["value"] == "1"
    assert row0["old"]["unit"] == "span"
    assert row0["new"]["unit"] == "span"
    # 但按内部精确数值（2m vs 3m）仍须报告含义变化
    assert row0["meaning_changed"] is True
    assert row0["differences"] == ["value"]
    assert data["first_meaning_change"] == {
        "index": 0,
        "name": "a",
        "differences": ["value"],
    }
    # 第 1 步显示也随之不同（2 vs 3）
    assert row1["old"]["value"] == "2"
    assert row1["new"]["value"] == "3"
    assert row1["meaning_changed"] is True


def test_internal_value_propagates_to_later_steps():
    # 后续步骤引用先前结果时使用的是各自版本的内部精确量：
    # 同一公式 x*2，折回 span 后显示仍相同，但物理值不同。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [
            ev_step("a", "x*2", "span"),
            ev_step("b", "a", "m"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    rows = resp.json()["steps"]
    # 2 个 span：旧 4m / 新 6m；折回 span 都显示 2
    assert rows[0]["old"]["value"] == "2"
    assert rows[0]["new"]["value"] == "2"
    assert rows[0]["meaning_changed"] is True
    assert rows[1]["old"]["value"] == "4"
    assert rows[1]["new"]["value"] == "6"


def test_same_set_on_both_sides_has_no_meaning_change():
    set_id = publish(SPAN_V1)
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [
            ev_step("a", "x", "span"),
            ev_step("b", "x", "m"),
        ],
        "old": by_id(set_id),
        "new": by_id(set_id),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["first_meaning_change"] is None
    assert [row["meaning_changed"] for row in data["steps"]] == [False, False]
    assert all(row["differences"] == [] for row in data["steps"])
    assert data["old"]["resolved_id"] == data["new"]["resolved_id"] == set_id


def test_dimension_change_is_reported_even_with_identical_display():
    # 同名符号旧版基于 m、新版基于 s：两侧都显示 "1 zz"，
    # 但内部数值与量纲都不同。
    v1 = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "zz", "factor": "3", "base_unit": "s"}])
    body = {
        "variables": {"x": var("1", "zz")},
        "steps": [
            ev_step("a", "x", "zz"),
            ev_step("b", "x", "zz"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    row0 = data["steps"][0]
    assert row0["old"]["unit"] == row0["new"]["unit"] == "zz"
    assert row0["old"]["value"] == row0["new"]["value"] == "1"
    assert row0["differences"] == ["value", "dimension"]
    assert data["first_meaning_change"]["index"] == 0


def test_first_meaning_change_points_at_first_diverging_step():
    # 第 0 步两侧一致（1*1 无量纲），第 1 步才因 span 定义不同而分歧。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [
            ev_step("k", "1", "1"),
            ev_step("a", "x", "m"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["steps"][0]["meaning_changed"] is False
    assert data["steps"][1]["meaning_changed"] is True
    assert data["first_meaning_change"] == {
        "index": 1,
        "name": "a",
        "differences": ["value"],
    }


def test_absolute_temperature_steps_compare_equal_when_identical():
    # 两侧同一（空）自定义定义集下的绝对温度链路：绝对温度属性参与比较。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"T0": var("98.6", "degF")},
        "steps": [
            ev_step("Tc", "T0", "degC"),
            ev_step("Tk", "Tc", "K"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["first_meaning_change"] is None
    assert data["steps"][0]["old"]["value"] == "37"
    assert data["steps"][1]["new"]["value"] == "310.15"


# ---------------------------------------------------------------------------
# 部分失败：一侧某步错误，保留此前对照，失败后步骤 not_run
# ---------------------------------------------------------------------------


def test_one_side_step_error_keeps_prior_comparison_and_marks_later_not_run():
    # 旧版 zz=m（第 0 步输出 m 成功；第 1 步 a+1 量纲冲突）；
    # 新版 zz=s（第 0 步输出 m 即量纲冲突，第 1 步不执行）。
    v1 = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "zz", "factor": "3", "base_unit": "s"}])
    body = {
        "variables": {"x": var("1", "zz")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "a+1", "m"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    old0, old1 = data["old"]["steps"]
    new0, new1 = data["new"]["steps"]
    assert old0["status"] == "ok" and old0["value"] == "2"
    assert old1["status"] == "error"
    assert old1["error"]["error"] == "dimension_mismatch"
    assert new0["status"] == "error"
    assert new0["error"]["error"] == "dimension_mismatch"
    assert new1["status"] == "not_run"
    assert new1["error"] is None and new1["value"] is None

    # 并排对照：第 0 步只有一侧成功 -> 不做含义比较；
    # 没有“两侧共同成功且含义不同”的步骤 -> first_meaning_change 为 null
    assert data["steps"][0]["meaning_changed"] is False
    assert data["steps"][1]["meaning_changed"] is False
    assert data["first_meaning_change"] is None


def test_healthy_side_runs_fully_while_other_side_fails_midway():
    # 初始变量只用两侧都认识的符号 zz；第 0 步目标单位 m（两侧都成功），
    # 第 1 步目标单位 ww 仅新版有定义：旧版在第 1 步运行期 unknown_symbol，
    # 其第 0 步对照保留；新版两步都成功。
    only_zz = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    with_ww = publish([
        {"symbol": "zz", "factor": "3", "base_unit": "m"},
        {"symbol": "ww", "factor": "1", "base_unit": "m"},
    ])
    body = {
        "variables": {"x": var("1", "zz")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "x", "ww"),
        ],
        "old": by_id(only_zz),
        "new": by_id(with_ww),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 旧版准备阶段正常：第 0 步成功（1 zz = 2 m），第 1 步目标单位 ww 未知
    assert data["old"]["setup_error"] is None
    assert data["old"]["steps"][0]["status"] == "ok"
    assert data["old"]["steps"][0]["value"] == "2"
    assert data["old"]["steps"][1]["status"] == "error"
    assert data["old"]["steps"][1]["error"]["error"] == "unknown_symbol"
    # 新版两步都成功（1 zz = 3 m；折到 ww 为 3）
    assert data["new"]["setup_error"] is None
    assert [s["status"] for s in data["new"]["steps"]] == ["ok", "ok"]
    assert data["new"]["steps"][0]["value"] == "3"
    assert data["new"]["steps"][1]["value"] == "3"
    # 第 0 步两侧共同成功但内部数值不同（2m vs 3m）-> 首个含义变化
    assert data["steps"][0]["meaning_changed"] is True
    assert data["first_meaning_change"]["index"] == 0


def test_failed_steps_are_not_counted_as_success():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "a", "m"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    # 初始变量 x 未提供：两侧第 0 步都 unknown_variable，第 1 步 not_run
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    for side in ("old", "new"):
        steps = resp.json()[side]["steps"]
        assert steps[0]["status"] == "error"
        assert steps[0]["error"]["error"] == "unknown_variable"
        assert steps[1]["status"] == "not_run"


def test_precision_overflow_is_a_step_business_error_not_http_422():
    # 1/3 在 "1" 下不是有限十进制：该步以 precision_overflow 回报，
    # 诊断本身仍为 200（业务错误不出现在 HTTP 层）。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"a": var("1", "m"), "b": var("3", "m")},
        "steps": [
            ev_step("r", "a/b", "1"),
            ev_step("big", "r*3", "1"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    for side in ("old", "new"):
        steps = resp.json()[side]["steps"]
        assert steps[0]["status"] == "error"
        assert steps[0]["error"]["error"] == "precision_overflow"
        assert steps[1]["status"] == "not_run"


def test_solve_step_failure_is_reported_per_side():
    # 反求步骤非线性：该步错误、其后不执行；前一步对照保留。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    body = {
        "steps": [
            ev_step("a", "1", "1"),
            solve_step("b", "x*x = a", "x", "1"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["old"]["steps"][0]["status"] == "ok"
    assert data["old"]["steps"][1]["status"] == "error"
    assert data["old"]["steps"][1]["error"]["error"] == "nonlinear_equation"
    # 第 0 步两侧一致
    assert data["steps"][0]["meaning_changed"] is False


def test_setup_error_when_variable_unit_unknown_under_one_side():
    # 变量单位 ft 只存在于新版定义集：旧侧准备阶段即失败，
    # 旧侧所有步骤 not_run；新侧正常执行。
    v1 = publish(SPAN_V1)
    foot = publish([{"symbol": "ft", "factor": "0.3048", "base_unit": "m"}])
    body = {
        "variables": {"q": var("10", "ft")},
        "steps": [
            ev_step("a", "q", "m"),
            ev_step("b", "q*2", "ft"),
        ],
        "old": by_id(v1),
        "new": by_id(foot),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["old"]["setup_error"]["error"] == "unknown_symbol"
    assert [s["status"] for s in data["old"]["steps"]] == ["not_run", "not_run"]
    assert data["new"]["setup_error"] is None
    assert [s["status"] for s in data["new"]["steps"]] == ["ok", "ok"]
    assert data["new"]["steps"][0]["value"] == "3.048"
    assert data["first_meaning_change"] is None


# ---------------------------------------------------------------------------
# 别名引用：解析一次、执行中切换不混版
# ---------------------------------------------------------------------------


def test_alias_references_resolved_and_echoed():
    v1 = publish(SPAN_V1)
    r = client.put(
        "/custom-unit-set-aliases/lengths",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    assert r.status_code == 200
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "x", "span"),
        ],
        "old": by_alias("lengths"),
        "new": by_id(v1),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 回显的是引用原样 + 解析到的实际标识
    assert data["old"]["reference"] == {"custom_unit_set_id": None,
                                        "custom_unit_set_alias": "lengths"}
    assert data["old"]["resolved_id"] == v1
    assert data["first_meaning_change"] is None


def test_alias_switch_during_request_does_not_mix_versions_within_side(monkeypatch):
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    client.put(
        "/custom-unit-set-aliases/lens",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    store = unit_sets.get_store()
    orig_build = store.build_scope

    # 旧侧构造 scope 之后、步骤执行之前把别名切到 v2；
    # 旧侧必须完整使用 v1，新侧解析时取到 v2。
    def switched_build(set_id):
        scope = orig_build(set_id)
        if set_id == v1:
            r = client.put(
                "/custom-unit-set-aliases/lens",
                json={"custom_unit_set_id": v2, "expected_id": v1},
            )
            assert r.status_code == 200, r.text
        return scope

    monkeypatch.setattr(store, "build_scope", switched_build)
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "x", "span"),
        ],
        "old": by_alias("lens"),
        "new": by_alias("lens"),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["old"]["resolved_id"] == v1
    assert data["new"]["resolved_id"] == v2
    assert data["old"]["steps"][0]["value"] == "2"
    assert data["new"]["steps"][0]["value"] == "3"
    # 旧侧两步自始至终是 v1（折回 span 显示 1），无同侧混版
    assert data["old"]["steps"][1]["value"] == "1"


def test_resolution_happens_before_any_step_runs():
    # 旧引用不存在：整体 422，不执行任何一侧
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "x", "m"),
        ],
        "old": by_alias("missing"),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"
    assert "steps" not in resp.json()


# ---------------------------------------------------------------------------
# 请求级 422
# ---------------------------------------------------------------------------


def test_unknown_set_id_is_422():
    v1 = publish(SPAN_V1)
    resp = diagnose(TWO_STEPS, by_id("cus_does_not_exist"), by_id(v1))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_unknown_alias_is_422():
    v1 = publish(SPAN_V1)
    resp = diagnose(TWO_STEPS, by_alias("nope"), by_id(v1))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_ref_with_both_id_and_alias_is_422():
    v1 = publish(SPAN_V1)
    resp = diagnose(
        TWO_STEPS,
        {"custom_unit_set_id": v1, "custom_unit_set_alias": "lengths"},
        by_id(v1),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_ref_with_neither_id_nor_alias_is_422():
    v1 = publish(SPAN_V1)
    resp = diagnose(TWO_STEPS, {}, by_id(v1))
    assert resp.status_code == 422


def test_step_count_limits_still_apply():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    one = diagnose([ev_step("a", "1", "1")], by_id(v1), by_id(v2))
    assert one.status_code == 422
    six = diagnose(
        [ev_step(f"s{i}", "1", "1") for i in range(6)], by_id(v1), by_id(v2)
    )
    assert six.status_code == 422


def test_worksheet_body_validation_shared_with_diagnose():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    # 步骤重名（与 /worksheet 相同的工作单本体校验）
    body = {
        "steps": [
            ev_step("a", "1", "1"),
            ev_step("a", "2", "1"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    resp = client.post("/worksheet-diagnose", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# 原 /worksheet 行为保持不变
# ---------------------------------------------------------------------------


def test_original_worksheet_still_returns_422_without_partial_results():
    body = {
        "variables": {"x": var("1", "m"), "y": var("1", "s")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "a+y", "m"),
        ],
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dimension_mismatch"
    assert "steps" not in resp.json()


def test_diagnose_does_not_mutate_sets_or_aliases():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    client.put(
        "/custom-unit-set-aliases/lens",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    body = {
        "variables": {"x": var("1", "span")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "x", "span"),
        ],
        "old": by_id(v1),
        "new": by_id(v2),
    }
    assert client.post("/worksheet-diagnose", json=body).status_code == 200
    # 诊断只读：别名指向不变、定义集数量不变
    aliases = client.get("/custom-unit-set-aliases").json()["aliases"]
    assert aliases == [{"alias": "lens", "custom_unit_set_id": v1}]
    assert client.get("/custom-unit-set-aliases/lens").json() == {
        "alias": "lens",
        "custom_unit_set_id": v1,
    }
