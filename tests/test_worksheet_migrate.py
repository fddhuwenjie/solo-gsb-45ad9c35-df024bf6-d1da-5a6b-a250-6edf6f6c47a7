"""POST /worksheet-migrate 的端到端测试。

覆盖：
- 换版成功：初始变量被改写为新定义下同一单位表达式、同一物理量的精确
  十进制值，返回的工作单可直接交给 /worksheet（引用固定的新标识）；
- 显示值允许变化，内部精确量/量纲/绝对温度属性逐步核对一致；
- 标识与活动别名引用、别名请求开始解析一次（执行中切换不混版）；
- 422：旧工作单出错（指出步骤/变量）、新定义下变量单位不兼容、改写值
  无法精确十进制表示、新工作单出错（指出步骤）、结果含义变化；
- 迁移只读：不改写定义集或别名；/worksheet 等原接口行为不变。
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


def migrate(steps, old_ref, new_ref, variables=None):
    body: dict = {"steps": steps, "old": old_ref, "new": new_ref}
    if variables is not None:
        body["variables"] = variables
    return client.post("/worksheet-migrate", json=body)


def by_id(set_id):
    return {"custom_unit_set_id": set_id}


def by_alias(name):
    return {"custom_unit_set_alias": name}


SPAN_V1 = [{"symbol": "span", "factor": "2", "base_unit": "m"}]
SPAN_V2 = [{"symbol": "span", "factor": "4", "base_unit": "m"}]
SPAN_V3 = [{"symbol": "span", "factor": "3", "base_unit": "m"}]


# ---------------------------------------------------------------------------
# 换版成功：改写初始变量并返回可直接提交的新工作单
# ---------------------------------------------------------------------------


def test_migrate_rewrites_variables_and_returns_submittable_worksheet():
    # 旧版 1 span = 2 m、新版 1 span = 4 m：x = 3 span 的物理量是 6 m，
    # 改写为新版下的 1.5 span；步骤保持不变。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    steps = [
        ev_step("a", "x*2", "span"),
        ev_step("b", "a", "m"),
    ]
    resp = migrate(steps, by_id(v1), by_id(v2), variables={"x": var("3", "span")})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 请求开始固定的两侧实际标识
    assert data["old_resolved_id"] == v1
    assert data["new_resolved_id"] == v2

    worksheet = data["worksheet"]
    # 单位表达式不变，数值改写为新定义下的精确十进制
    assert worksheet["variables"] == {"x": {"value": "1.5", "unit": "span"}}
    # 步骤原样保留（求值步骤不带 equation/solve_for 空字段）
    assert worksheet["steps"] == [
        {"name": "a", "output_unit": "span", "formula": "x*2"},
        {"name": "b", "output_unit": "m", "formula": "a"},
    ]
    # 引用固定的新定义集标识，而非别名
    assert worksheet["custom_unit_set_id"] == v2

    # 返回的工作单可直接交给 /worksheet
    rerun = client.post("/worksheet", json=worksheet)
    assert rerun.status_code == 200, rerun.text
    new_steps = rerun.json()["steps"]
    # 显示值允许变化：第 0 步旧定义下显示 6 span，新定义下显示 3 span
    old_run = client.post(
        "/worksheet",
        json={"variables": {"x": var("3", "span")}, "steps": steps,
              "custom_unit_set_id": v1},
    )
    assert old_run.status_code == 200, old_run.text
    assert old_run.json()["steps"][0]["value"] == "6"
    assert new_steps[0]["value"] == "3"
    # 物理量相同：换算到同一内置单位 m 时两侧显示一致
    assert old_run.json()["steps"][1] == {"name": "b", "value": "12",
                                          "unit": "m", "dimension": "m"}
    assert new_steps[1] == {"name": "b", "value": "12",
                            "unit": "m", "dimension": "m"}


def test_migrate_without_variables():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    steps = [ev_step("a", "1+1", "1"), ev_step("b", "a*3", "1")]
    resp = migrate(steps, by_id(v1), by_id(v2))
    assert resp.status_code == 200, resp.text
    worksheet = resp.json()["worksheet"]
    assert worksheet["variables"] == {}
    assert worksheet["custom_unit_set_id"] == v2
    rerun = client.post("/worksheet", json=worksheet)
    assert rerun.status_code == 200, rerun.text
    assert [s["value"] for s in rerun.json()["steps"]] == ["2", "6"]


def test_migrate_solve_steps_and_absolute_temperature():
    # 反求步骤与绝对温度变量一并迁移：绝对温度改写后数值不变（温标为内置
    # 仿射单位），量纲与绝对温度属性保持一致。
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    variables = {
        "x": var("2", "span"),
        "T0": var("98.6", "degF"),
    }
    steps = [
        solve_step("y", "y = x*3", "y", "span"),
        ev_step("total", "y+x", "m"),
        ev_step("Tc", "T0", "degC"),
    ]
    resp = migrate(steps, by_id(v1), by_id(v2), variables=variables)
    assert resp.status_code == 200, resp.text
    worksheet = resp.json()["worksheet"]
    # x = 2 span = 4 m -> 新版 1 span；绝对温度数值不变
    assert worksheet["variables"]["x"] == {"value": "1", "unit": "span"}
    assert worksheet["variables"]["T0"] == {"value": "98.6", "unit": "degF"}
    # 反求步骤的 equation/solve_for 原样保留
    assert worksheet["steps"][0] == {
        "name": "y", "output_unit": "span", "equation": "y = x*3",
        "solve_for": "y",
    }
    rerun = client.post("/worksheet", json=worksheet)
    assert rerun.status_code == 200, rerun.text
    new_steps = rerun.json()["steps"]
    # y = 3x = 12 m -> 新版 3 span；total = 16 m；Tc = 37 degC
    assert new_steps[0] == {"name": "y", "value": "3", "unit": "span",
                            "dimension": "m"}
    assert new_steps[1] == {"name": "total", "value": "16", "unit": "m",
                            "dimension": "m"}
    assert new_steps[2] == {"name": "Tc", "value": "37", "unit": "degC",
                            "dimension": "K"}


def test_migrate_with_alias_references_pins_both_sides():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    client.put(
        "/custom-unit-set-aliases/span_prev",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    client.put(
        "/custom-unit-set-aliases/span_next",
        json={"custom_unit_set_id": v2, "expected_id": None},
    )
    steps = [ev_step("a", "x", "span"), ev_step("b", "a", "m")]
    resp = migrate(
        steps, by_alias("span_prev"), by_alias("span_next"),
        variables={"x": var("2", "span")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 两侧别名在请求开始各解析一次并固定
    assert data["old_resolved_id"] == v1
    assert data["new_resolved_id"] == v2
    # 返回的工作单引用固定的新标识而非别名
    assert data["worksheet"]["custom_unit_set_id"] == v2
    assert data["worksheet"]["variables"] == {"x": {"value": "1", "unit": "span"}}


def test_alias_switch_during_request_does_not_mix_versions(monkeypatch):
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
    steps = [ev_step("a", "x", "span"), ev_step("b", "a", "m")]
    resp = migrate(
        steps, by_alias("lens"), by_alias("lens"),
        variables={"x": var("2", "span")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 旧侧固定在 v1、新侧固定在 v2，无单侧混版
    assert data["old_resolved_id"] == v1
    assert data["new_resolved_id"] == v2
    # x = 2 span(v1) = 4 m -> 1 span(v2)
    assert data["worksheet"]["variables"] == {"x": {"value": "1", "unit": "span"}}
    assert data["worksheet"]["custom_unit_set_id"] == v2


def test_migrate_does_not_mutate_sets_or_aliases():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    client.put(
        "/custom-unit-set-aliases/lens",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    steps = [ev_step("a", "x", "span"), ev_step("b", "a", "m")]
    resp = migrate(
        steps, by_alias("lens"), by_id(v2), variables={"x": var("2", "span")}
    )
    assert resp.status_code == 200, resp.text
    # 迁移只读：别名指向不变、定义集数量不变
    aliases = client.get("/custom-unit-set-aliases").json()["aliases"]
    assert aliases == [{"alias": "lens", "custom_unit_set_id": v1}]
    assert unit_sets.get_store().count() == 2


# ---------------------------------------------------------------------------
# 422：旧工作单出错（指出变量或步骤）
# ---------------------------------------------------------------------------


def test_old_worksheet_setup_error_points_at_variable():
    # 变量单位 ft 只存在于新版定义集：旧侧准备阶段即失败
    v1 = publish(SPAN_V1)
    foot = publish([{"symbol": "ft", "factor": "0.3048", "base_unit": "m"}])
    steps = [ev_step("a", "q", "m"), ev_step("b", "q*2", "m")]
    resp = migrate(steps, by_id(v1), by_id(foot), variables={"q": var("10", "ft")})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "unknown_symbol"
    assert detail["variable"] == "q"
    assert "worksheet" not in resp.json()


def test_old_worksheet_step_error_points_at_step():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    steps = [
        ev_step("a", "x", "m"),
        ev_step("b", "a+y", "m"),
    ]
    variables = {"x": var("1", "span"), "y": var("1", "s")}
    resp = migrate(steps, by_id(v1), by_id(v2), variables=variables)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "dimension_mismatch"
    assert detail["step_index"] == 1
    assert detail["step_name"] == "b"
    assert "worksheet" not in resp.json()


# ---------------------------------------------------------------------------
# 422：新定义下变量单位不兼容 / 改写值无法精确十进制表示
# ---------------------------------------------------------------------------


def test_variable_unit_unknown_under_new_points_at_variable():
    foot = publish([{"symbol": "ft", "factor": "0.3048", "base_unit": "m"}])
    v2 = publish(SPAN_V2)
    steps = [ev_step("a", "q", "m"), ev_step("b", "q*2", "m")]
    resp = migrate(steps, by_id(foot), by_id(v2), variables={"q": var("10", "ft")})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "unknown_symbol"
    assert detail["variable"] == "q"
    assert "worksheet" not in resp.json()


def test_variable_dimension_incompatible_under_new_points_at_variable():
    # 同名符号旧版基于 m、新版基于 s：量纲不兼容
    v1 = publish([{"symbol": "zz", "factor": "2", "base_unit": "m"}])
    v2 = publish([{"symbol": "zz", "factor": "3", "base_unit": "s"}])
    steps = [ev_step("a", "x", "m"), ev_step("b", "a", "m")]
    resp = migrate(steps, by_id(v1), by_id(v2), variables={"x": var("1", "zz")})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "dimension_mismatch"
    assert detail["variable"] == "x"
    assert "worksheet" not in resp.json()


def test_rewrite_value_not_exact_decimal_points_at_variable():
    # 旧版 1 span = 2 m、新版 1 span = 3 m：x = 1 span = 2 m
    # 改写为 2/3 span，无法精确十进制表示
    v1 = publish(SPAN_V1)
    v3 = publish(SPAN_V3)
    steps = [ev_step("a", "x", "span"), ev_step("b", "x", "m")]
    resp = migrate(steps, by_id(v1), by_id(v3), variables={"x": var("1", "span")})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "precision_overflow"
    assert detail["variable"] == "x"
    assert "worksheet" not in resp.json()


def test_rewrite_value_beyond_input_precision_limit_is_422():
    # 改写值是有限十进制但有效数字超过 50 位上限，同样拒绝（保证返回的
    # 工作单必然能作为 /worksheet 输入）
    big_old = publish([{"symbol": "huge", "factor": "1" + "0" * 49, "base_unit": "m"}])
    small_new = publish([{"symbol": "huge", "factor": "0.1", "base_unit": "m"}])
    steps = [ev_step("a", "x", "m"), ev_step("b", "a", "m")]
    resp = migrate(
        steps, by_id(big_old), by_id(small_new), variables={"x": var("1", "huge")}
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "precision_overflow"
    assert detail["variable"] == "x"


# ---------------------------------------------------------------------------
# 422：新工作单出错 / 结果含义变化（指出步骤）
# ---------------------------------------------------------------------------


def test_new_worksheet_unknown_step_unit_points_at_step():
    # 变量单位 m 两侧兼容；第 0 步目标单位 span 只存在于旧版
    v1 = publish(SPAN_V1)
    other = publish([{"symbol": "ww", "factor": "1", "base_unit": "m"}])
    steps = [ev_step("a", "x", "span"), ev_step("b", "x", "m")]
    resp = migrate(steps, by_id(v1), by_id(other), variables={"x": var("2", "m")})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "unknown_symbol"
    assert detail["step_index"] == 0
    assert detail["step_name"] == "a"
    assert "worksheet" not in resp.json()


def test_new_worksheet_step_dimension_mismatch_points_at_step():
    # 变量 x 用内置单位 m；步骤目标单位 span 在新版下量纲变为 s
    v1 = publish(SPAN_V1)
    span_s = publish([{"symbol": "span", "factor": "3", "base_unit": "s"}])
    steps = [ev_step("a", "x", "span"), ev_step("b", "x", "m")]
    resp = migrate(steps, by_id(v1), by_id(span_s), variables={"x": var("2", "m")})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "dimension_mismatch"
    assert detail["step_index"] == 0
    assert detail["step_name"] == "a"


def test_new_worksheet_step_precision_overflow_points_at_step():
    # x = 3 span(2m) = 6 m -> 新版 2 span(3m)，改写精确；
    # 但 y = x/3 = 2 m 折回新版 span 为 2/3，显示值无法精确十进制表示
    v1 = publish(SPAN_V1)
    v3 = publish(SPAN_V3)
    steps = [
        ev_step("y", "x/3", "span"),
        ev_step("b", "y", "m"),
    ]
    resp = migrate(steps, by_id(v1), by_id(v3), variables={"x": var("3", "span")})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "precision_overflow"
    assert detail["step_index"] == 0
    assert detail["step_name"] == "y"
    assert "worksheet" not in resp.json()


def test_meaning_change_points_at_step():
    # 解恒为零且量纲不受方程约束时，步骤结果量纲取自目标单位：
    # 新版 span 量纲变为 s，该步内部量纲与旧执行不一致 -> 含义变化
    v1 = publish(SPAN_V1)
    span_s = publish([{"symbol": "span", "factor": "3", "base_unit": "s"}])
    steps = [
        solve_step("z", "2*y = y", "y", "span"),
        ev_step("w", "1", "1"),
    ]
    resp = migrate(steps, by_id(v1), by_id(span_s))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "meaning_changed"
    assert detail["step_index"] == 0
    assert detail["step_name"] == "z"
    assert "worksheet" not in resp.json()


# ---------------------------------------------------------------------------
# 请求级 422（与 /worksheet-diagnose 一致）
# ---------------------------------------------------------------------------


def test_unknown_set_id_is_422():
    v1 = publish(SPAN_V1)
    steps = [ev_step("a", "1", "1"), ev_step("b", "a", "1")]
    resp = migrate(steps, by_id("cus_does_not_exist"), by_id(v1))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"
    assert "worksheet" not in resp.json()


def test_unknown_alias_is_422():
    v1 = publish(SPAN_V1)
    steps = [ev_step("a", "1", "1"), ev_step("b", "a", "1")]
    resp = migrate(steps, by_alias("nope"), by_id(v1))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_ref_with_both_id_and_alias_is_422():
    v1 = publish(SPAN_V1)
    steps = [ev_step("a", "1", "1"), ev_step("b", "a", "1")]
    resp = migrate(
        steps,
        {"custom_unit_set_id": v1, "custom_unit_set_alias": "lengths"},
        by_id(v1),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_step_count_limits_still_apply():
    v1 = publish(SPAN_V1)
    v2 = publish(SPAN_V2)
    one = migrate([ev_step("a", "1", "1")], by_id(v1), by_id(v2))
    assert one.status_code == 422
    six = migrate(
        [ev_step(f"s{i}", "1", "1") for i in range(6)], by_id(v1), by_id(v2)
    )
    assert six.status_code == 422


def test_same_set_on_both_sides_migrates_to_identical_values():
    set_id = publish(SPAN_V1)
    steps = [ev_step("a", "x*2", "span"), ev_step("b", "a", "m")]
    resp = migrate(steps, by_id(set_id), by_id(set_id),
                   variables={"x": var("3", "span")})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["old_resolved_id"] == data["new_resolved_id"] == set_id
    # 同一集合：改写后数值与原值一致（规范化十进制形式）
    assert data["worksheet"]["variables"] == {"x": {"value": "3", "unit": "span"}}


# ---------------------------------------------------------------------------
# 原 /worksheet 行为保持不变
# ---------------------------------------------------------------------------


def test_original_worksheet_endpoint_unchanged():
    v2 = publish(SPAN_V2)
    body = {
        "variables": {"x": var("1.5", "span")},
        "steps": [ev_step("a", "x*2", "span"), ev_step("b", "a", "m")],
        "custom_unit_set_id": v2,
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["steps"] == [
        {"name": "a", "value": "3", "unit": "span", "dimension": "m"},
        {"name": "b", "value": "12", "unit": "m", "dimension": "m"},
    ]
    # 任一步错误仍为 422 且不返回部分结果
    bad = {
        "variables": {"x": var("1", "m"), "y": var("1", "s")},
        "steps": [ev_step("a", "x", "m"), ev_step("b", "a+y", "m")],
    }
    resp = client.post("/worksheet", json=bad)
    assert resp.status_code == 422
    assert "steps" not in resp.json()
