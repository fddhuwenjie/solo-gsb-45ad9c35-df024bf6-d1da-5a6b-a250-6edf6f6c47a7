"""POST /worksheet 的端到端测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app import unit_sets

client = TestClient(app, raise_server_exceptions=True)


def var(value, unit):
    return {"value": value, "unit": unit}


def ws(steps, variables=None, **extra):
    body: dict = {"steps": steps}
    if variables is not None:
        body["variables"] = variables
    body.update(extra)
    return client.post("/worksheet", json=body)


def ev_step(name, formula, output_unit):
    return {"name": name, "formula": formula, "output_unit": output_unit}


def solve_step(name, equation, solve_for, output_unit):
    return {
        "name": name,
        "equation": equation,
        "solve_for": solve_for,
        "output_unit": output_unit,
    }


def assert_ok(resp, expected):
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"steps": expected}


def assert_err(resp, code):
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


# ---------------------------------------------------------------------------
# 顺序求值：后续步骤按结果名引用先前步骤
# ---------------------------------------------------------------------------


def test_chained_evaluate_steps():
    # v*t = 120 km；再 s/2 = 60 km
    steps = [
        ev_step("s", "v*t", "km"),
        ev_step("half", "s/2", "km"),
    ]
    variables = {"v": var("60", "km/h"), "t": var("2", "h")}
    assert_ok(
        ws(steps, variables),
        [
            {"name": "s", "value": "120", "unit": "km", "dimension": "m"},
            {"name": "half", "value": "60", "unit": "km", "dimension": "m"},
        ],
    )


def test_steps_can_change_unit_between_steps():
    # 60 km/h * 2 h = 120 km -> 120000 m；再以 m 参与除法
    steps = [
        ev_step("s", "v*t", "m"),
        ev_step("speed", "s/t", "km/h"),
    ]
    variables = {"v": var("60", "km/h"), "t": var("2", "h")}
    assert_ok(
        ws(steps, variables),
        [
            {"name": "s", "value": "120000", "unit": "m", "dimension": "m"},
            {"name": "speed", "value": "60", "unit": "km/h", "dimension": "m/s"},
        ],
    )


def test_mixed_evaluate_and_solve_steps():
    # 第一步：F = m*a = 6 N；第二步：反求 F/4 = m2 * x 中的 x
    steps = [
        ev_step("F", "m*a", "N"),
        solve_step("x", "F/4 = m2*x", "x", "m/s^2"),
    ]
    variables = {"m": var("2", "kg"), "a": var("3", "m/s^2"), "m2": var("1", "kg")}
    assert_ok(
        ws(steps, variables),
        [
            {"name": "F", "value": "6", "unit": "N", "dimension": "kg*m/s^2"},
            {"name": "x", "value": "1.5", "unit": "m/s^2", "dimension": "m/s^2"},
        ],
    )


def test_solve_step_result_usable_in_later_evaluate():
    # 反求 t = 2 h；后续 t*v = 120 km
    steps = [
        solve_step("t", "v*t = s", "t", "h"),
        ev_step("s2", "t*v2", "km"),
    ]
    variables = {
        "v": var("60", "km/h"),
        "s": var("120", "km"),
        "v2": var("30", "km/h"),
    }
    assert_ok(
        ws(steps, variables),
        [
            {"name": "t", "value": "2", "unit": "h", "dimension": "s"},
            {"name": "s2", "value": "60", "unit": "km", "dimension": "m"},
        ],
    )


def test_five_steps_chain_and_dimensions():
    # 依次计算矩形面积、体积、密度、重力等，验证跨步量纲传递
    steps = [
        ev_step("area", "l*ww", "m^2"),
        ev_step("vol", "area*h", "m^3"),
        ev_step("rho", "mass/vol", "kg/m^3"),
        solve_step("g", "W = mass*g", "g", "m/s^2"),
        ev_step("F", "mass*g", "N"),
    ]
    variables = {
        "l": var("2", "m"),
        "ww": var("3", "m"),
        "h": var("4", "m"),
        "mass": var("24", "kg"),
        "W": var("240", "N"),
    }
    resp = ws(steps, variables)
    assert_ok(
        resp,
        [
            {"name": "area", "value": "6", "unit": "m^2", "dimension": "m^2"},
            {"name": "vol", "value": "24", "unit": "m^3", "dimension": "m^3"},
            {"name": "rho", "value": "1", "unit": "kg/m^3", "dimension": "kg/m^3"},
            {"name": "g", "value": "10", "unit": "m/s^2", "dimension": "m/s^2"},
            {"name": "F", "value": "240", "unit": "N", "dimension": "kg*m/s^2"},
        ],
    )


def test_steps_without_initial_variables():
    steps = [
        solve_step("x", "2*x = 10", "x", "1"),
        ev_step("y", "x*3", "1"),
    ]
    assert_ok(
        ws(steps),
        [
            {"name": "x", "value": "5", "unit": "1", "dimension": "1"},
            {"name": "y", "value": "15", "unit": "1", "dimension": "1"},
        ],
    )


# ---------------------------------------------------------------------------
# 步骤间传递：精确数值（不经十进制中间值）与绝对温度属性
# ---------------------------------------------------------------------------


def test_exact_fraction_propagates_between_steps():
    # 第一步得到 1/3（目标单位 "1" 允许有限…… 不允许，这里改为精确分数链路）：
    # a/b 在 "1" 下是 0.5（有限十进制），再 *3 = 1.5，全程精确
    steps = [
        ev_step("r", "a/b", "1"),
        ev_step("big", "r*3", "1"),
    ]
    variables = {"a": var("1", "m"), "b": var("2", "m")}
    assert_ok(
        ws(steps, variables),
        [
            {"name": "r", "value": "0.5", "unit": "1", "dimension": "1"},
            {"name": "big", "value": "1.5", "unit": "1", "dimension": "1"},
        ],
    )


def test_intermediate_precision_overflow_aborts_whole_worksheet():
    # 第一步输出 1/3（非有限十进制）-> 422，即使第二步本来可以成功
    steps = [
        ev_step("r", "a/b", "1"),
        ev_step("big", "r*3", "1"),
    ]
    variables = {"a": var("1", "m"), "b": var("3", "m")}
    assert_err(ws(steps, variables), "precision_overflow")


def test_absolute_temperature_attribute_propagates():
    # 第一步：华氏度唯一项 -> 摄氏度（结果仍是绝对温度）
    # 第二步：再次作为唯一项换算到 K
    steps = [
        ev_step("T", "T0", "degC"),
        ev_step("Tk", "T", "K"),
    ]
    variables = {"T0": var("98.6", "degF")}
    assert_ok(
        ws(steps, variables),
        [
            {"name": "T", "value": "37", "unit": "degC", "dimension": "K"},
            {"name": "Tk", "value": "310.15", "unit": "K", "dimension": "K"},
        ],
    )


def test_absolute_temperature_step_result_cannot_enter_operation():
    steps = [
        ev_step("T", "T0", "degC"),
        ev_step("x", "T+1", "K"),
    ]
    variables = {"T0": var("0", "degC")}
    assert_err(ws(steps, variables), "affine_mixing")


def test_delta_temperature_is_linear_across_steps():
    steps = [
        ev_step("dt", "a-b", "dC"),
        ev_step("dtf", "dt", "dF"),
    ]
    variables = {"a": var("10", "dC"), "b": var("5", "dC")}
    assert_ok(
        ws(steps, variables),
        [
            {"name": "dt", "value": "5", "unit": "dC", "dimension": "K"},
            {"name": "dtf", "value": "9", "unit": "dF", "dimension": "K"},
        ],
    )


def test_linear_result_cannot_convert_to_absolute_scale():
    steps = [
        ev_step("dt", "a-b", "dC"),
        ev_step("abs", "dt", "degC"),
    ]
    variables = {"a": var("10", "dC"), "b": var("5", "dC")}
    assert_err(ws(steps, variables), "affine_mixing")


def test_solve_step_output_unit_must_be_linear():
    # 反求步骤目标单位直接给绝对温标 -> 422
    bad = [
        solve_step("t", "v*t = s", "t", "degC"),
        ev_step("x", "t", "1"),
    ]
    variables = {"v": var("60", "km/h"), "s": var("120", "km")}
    assert_err(ws(bad, variables), "affine_mixing")
    # 对照：同一方程目标 h 可成功
    ok = [
        solve_step("t", "v*t = s", "t", "h"),
        ev_step("tt", "t", "h"),
    ]
    assert ws(ok, variables).status_code == 200


# ---------------------------------------------------------------------------
# 引用规则：只能引用先前结果，不能覆盖已有变量
# ---------------------------------------------------------------------------


def test_reference_to_not_yet_executed_step_rejected():
    steps = [
        ev_step("a", "b+1", "1"),
        ev_step("b", "2", "1"),
    ]
    assert_err(ws(steps, {"one": var("1", "1")}), "unknown_variable")


def test_solve_unknown_reference_to_future_step_rejected():
    steps = [
        solve_step("a", "x = later", "x", "1"),
        ev_step("later", "2", "1"),
    ]
    assert_err(ws(steps), "unknown_variable")


def test_duplicate_step_name_rejected():
    body = {
        "steps": [
            ev_step("r", "a", "m"),
            ev_step("r", "a", "m"),
        ],
        "variables": {"a": var("1", "m")},
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_step_name_overriding_initial_variable_rejected():
    body = {
        "steps": [
            ev_step("a", "2", "m"),
            ev_step("b", "a", "m"),
        ],
        "variables": {"a": var("1", "m")},
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_solve_for_overriding_existing_name_rejected():
    # solve_for 与初始变量同名
    steps = [
        solve_step("out", "x = a", "x", "m"),
        ev_step("z", "out", "m"),
    ]
    assert_err(ws(steps, {"x": var("1", "m"), "a": var("2", "m")}), "invalid_request")
    # solve_for 与先前步骤结果同名
    steps2 = [
        ev_step("prev", "a", "m"),
        solve_step("out2", "prev = 2*prev", "prev", "m"),
    ]
    assert_err(ws(steps2, {"a": var("1", "m")}), "invalid_request")


# ---------------------------------------------------------------------------
# 请求体校验
# ---------------------------------------------------------------------------


def test_step_count_limits():
    one = [ev_step("a", "1", "1")]
    assert ws(one).status_code == 422
    six = [ev_step(f"s{i}", "1", "1") for i in range(6)]
    assert ws(six).status_code == 422
    two = [ev_step("a", "1", "1"), ev_step("b", "a", "1")]
    assert ws(two).status_code == 200
    five = [ev_step("a", "1", "1")] + [
        ev_step(f"s{i}", "a", "1") for i in range(4)
    ]
    assert ws(five).status_code == 200


def test_step_must_be_exactly_one_kind():
    body = {
        "steps": [
            {"name": "a", "formula": "1", "equation": "x=1", "solve_for": "x",
             "output_unit": "1"},
            ev_step("b", "a", "1"),
        ]
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"

    body = {
        "steps": [
            {"name": "a", "output_unit": "1"},
            ev_step("b", "a", "1"),
        ]
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 422

    body = {
        "steps": [
            {"name": "a", "equation": "x=1", "output_unit": "1"},
            ev_step("b", "a", "1"),
        ]
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 422


def test_bad_step_and_variable_names():
    body = {
        "steps": [
            ev_step("a-b", "1", "1"),
            ev_step("c", "1", "1"),
        ]
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_step_error_returns_no_partial_results():
    # 第二步量纲不匹配 -> 422，响应中不含第一步结果
    steps = [
        ev_step("a", "x", "m"),
        ev_step("b", "a+y", "m"),
    ]
    variables = {"x": var("1", "m"), "y": var("1", "s")}
    resp = ws(steps, variables)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dimension_mismatch"
    assert "steps" not in resp.json()


def test_unknown_unit_in_later_step_aborts():
    steps = [
        ev_step("a", "x", "m"),
        ev_step("b", "a", "furlong"),
    ]
    assert_err(ws(steps, {"x": var("1", "m")}), "unknown_symbol")


def test_nonlinear_solve_step_aborts():
    steps = [
        ev_step("a", "1", "1"),
        solve_step("b", "x*x = a", "x", "1"),
    ]
    assert_err(ws(steps), "nonlinear_equation")


def test_no_unique_solution_aborts():
    steps = [
        ev_step("a", "1", "1"),
        solve_step("b", "x = x+a", "x", "1"),
    ]
    assert_err(ws(steps), "no_unique_solution")


# ---------------------------------------------------------------------------
# 自定义单位：三种来源、互斥与别名只解析一次
# ---------------------------------------------------------------------------

FOOT = {"symbol": "ft", "factor": "0.3048", "base_unit": "m"}


def test_worksheet_with_custom_units():
    # 面积 12 ft^2，再反求 area = side * three（three = 3 ft）中的 side
    body = {
        "variables": {
            "l": var("12", "ft"),
            "w": var("1", "ft"),
            "three": var("3", "ft"),
        },
        "steps": [
            ev_step("area", "l*w", "ft^2"),
            solve_step("side", "area = side*three", "side", "ft"),
        ],
        "custom_units": [FOOT],
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["steps"] == [
        {"name": "area", "value": "12", "unit": "ft^2", "dimension": "m^2"},
        {"name": "side", "value": "4", "unit": "ft", "dimension": "m"},
    ]


def test_custom_sources_mutually_exclusive():
    body = {
        "steps": [ev_step("a", "1", "1"), ev_step("b", "a", "1")],
        "custom_units": [FOOT],
        "custom_unit_set_id": "cus_does_not_exist",
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"


def test_worksheet_with_published_set_id():
    pub = client.post("/custom-unit-sets", json={"custom_units": [FOOT]})
    assert pub.status_code == 201, pub.text
    set_id = pub.json()["id"]
    body = {
        "variables": {"x": var("10", "ft")},
        "steps": [
            ev_step("m", "x", "m"),
            ev_step("back", "m", "ft"),
        ],
        "custom_unit_set_id": set_id,
    }
    resp = client.post("/worksheet", json=body)
    assert_ok(
        resp,
        [
            {"name": "m", "value": "3.048", "unit": "m", "dimension": "m"},
            {"name": "back", "value": "10", "unit": "ft", "dimension": "m"},
        ],
    )


def test_worksheet_unknown_set_id_is_422():
    body = {
        "steps": [ev_step("a", "1", "1"), ev_step("b", "a", "1")],
        "custom_unit_set_id": "cus_does_not_exist",
    }
    assert_err(client.post("/worksheet", json=body), "invalid_request")


def test_worksheet_alias_resolved_once_for_all_steps(monkeypatch):
    # 发布两版同名符号的定义集，别名先指向 v1
    v1 = client.post(
        "/custom-unit-sets",
        json={"custom_units": [{"symbol": "zz", "factor": "2", "base_unit": "m"}]},
    ).json()["id"]
    v2 = client.post(
        "/custom-unit-sets",
        json={"custom_units": [{"symbol": "zz", "factor": "3", "base_unit": "m"}]},
    ).json()["id"]
    r = client.put(
        "/custom-unit-set-aliases/lens",
        json={"custom_unit_set_id": v1, "expected_id": None},
    )
    assert r.status_code == 200, r.text

    store = unit_sets.get_store()
    orig_build = store.build_scope

    # 在工作单执行“期间”（scope 构造完成后、步骤执行前）切换别名：
    # 各步骤仍须使用请求开始时解析到的 v1 定义
    def switched_build(set_id):
        scope = orig_build(set_id)
        client.put(
            "/custom-unit-set-aliases/lens",
            json={"custom_unit_set_id": v2, "expected_id": v1},
        )
        return scope

    monkeypatch.setattr(store, "build_scope", switched_build)

    body = {
        "variables": {"x": var("1", "zz")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "a", "zz"),
        ],
        "custom_unit_set_alias": "lens",
    }
    resp = client.post("/worksheet", json=body)
    assert resp.status_code == 200, resp.text
    # v1: 1 zz = 2 m；再折回 zz 仍为 1（同版定义，zz 规范形式保留）
    assert resp.json()["steps"][0]["value"] == "2"
    assert resp.json()["steps"][1]["value"] == "1"
    assert resp.json()["steps"][1]["unit"] == "zz"

    # 切换完成后的下一次工作单请求使用 v2
    body2 = {
        "variables": {"x": var("1", "zz")},
        "steps": [
            ev_step("a", "x", "m"),
            ev_step("b", "a", "zz"),
        ],
        "custom_unit_set_alias": "lens",
    }
    resp2 = client.post("/worksheet", json=body2)
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["steps"][0]["value"] == "3"
    assert resp2.json()["steps"][1]["value"] == "1"


def test_worksheet_unknown_alias_is_422():
    body = {
        "steps": [ev_step("a", "1", "1"), ev_step("b", "a", "1")],
        "custom_unit_set_alias": "missing",
    }
    assert_err(client.post("/worksheet", json=body), "invalid_request")


def test_results_order_matches_submission():
    steps = [
        ev_step("zeta", "1", "1"),
        ev_step("alpha", "2", "1"),
        ev_step("mid", "zeta+alpha", "1"),
    ]
    resp = ws(steps)
    assert [s["name"] for s in resp.json()["steps"]] == ["zeta", "alpha", "mid"]
