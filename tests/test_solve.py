"""POST /solve 的端到端测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def solve(equation, solve_for, output_unit, variables=None):
    body = {"equation": equation, "solve_for": solve_for, "output_unit": output_unit}
    if variables is not None:
        body["variables"] = variables
    return client.post("/solve", json=body)


def var(value, unit):
    return {"value": value, "unit": unit}


def assert_ok(resp, value, unit, dimension):
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"value": value, "unit": unit, "dimension": dimension}


def assert_err(resp, code):
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


# ---------------------------------------------------------------------------
# 基本求解：系数与常数的精确合并
# ---------------------------------------------------------------------------


def test_pure_numeric_equation_without_variables():
    assert_ok(solve("2*x = 10", "x", "1"), "5", "1", "1")


def test_speed_times_time_solve_for_time():
    assert_ok(
        solve("v*t = s", "t", "h", {"v": var("60", "km/h"), "s": var("120", "km")}),
        "2", "h", "s",
    )


def test_newton_second_law_solve_for_acceleration():
    assert_ok(
        solve("F = m*a", "a", "m/s^2", {"F": var("6", "N"), "m": var("2", "kg")}),
        "3", "m/s^2", "m/s^2",
    )


def test_solve_for_force_with_derived_output_unit():
    assert_ok(
        solve("F = m*a", "F", "N", {"m": var("2", "kg"), "a": var("3", "m/s^2")}),
        "6", "N", "kg*m/s^2",
    )


def test_unknown_on_both_sides():
    # x + 3 = 7 - x  ->  2x = 4
    assert_ok(
        solve("x + a = b - x", "x", "m", {"a": var("3", "m"), "b": var("7", "m")}),
        "2", "m", "m",
    )


def test_parentheses_and_unary_minus():
    assert_ok(solve("-(x + 1) = -3", "x", "1"), "2", "1", "1")


def test_decimal_constants_and_prefix_conversion():
    assert_ok(solve("0.5*x = 2.5", "x", "1"), "5", "1", "1")
    assert_ok(solve("x = a", "x", "km", {"a": var("1500", "m")}), "1.5", "km", "m")


def test_constant_power_is_allowed():
    assert_ok(solve("2^3*x = 16", "x", "1"), "2", "1", "1")
    assert_ok(solve("x = 2^-2", "x", "1"), "0.25", "1", "1")


def test_delta_temperature_is_linear():
    assert_ok(
        solve("x + a = b", "x", "dF", {"a": var("5", "dC"), "b": var("12", "dC")}),
        "12.6", "dF", "K",
    )
    # 裸 K 作为线性目标单位也可以
    assert_ok(solve("x = a", "x", "K", {"a": var("10", "dC")}), "10", "K", "K")


def test_json_number_value_is_exact_decimal():
    assert_ok(solve("x = a", "x", "m", {"a": var(0.1, "m")}), "0.1", "m", "m")


def test_zero_solution_with_known_dimension():
    assert_ok(solve("x + a = a", "x", "m", {"a": var("1", "m")}), "0", "m", "m")


def test_zero_solution_with_undetermined_dimension():
    # 2*x = x 的解恒为零且量纲不受约束：接受任意线性目标单位
    assert_ok(solve("2*x = x", "x", "m"), "0", "m", "m")
    assert_ok(solve("2*x = x", "x", "1"), "0", "1", "1")


def test_unused_variables_are_allowed():
    # 未参与方程的变量（含绝对温标变量）不影响求解
    assert_ok(
        solve("x = 1", "x", "1", {"T": var("0", "degC"), "unused": var("9", "s")}),
        "1", "1", "1",
    )


# ---------------------------------------------------------------------------
# 非线性（未知量必须处于一次线性位置）
# ---------------------------------------------------------------------------


def test_unknown_times_unknown_is_nonlinear():
    assert_err(solve("x*x = 4", "x", "1"), "nonlinear_equation")
    assert_err(solve("(x+1)*(x-1) = 0", "x", "1"), "nonlinear_equation")


def test_unknown_in_divisor_is_nonlinear():
    assert_err(solve("1/x = 2", "x", "1"), "nonlinear_equation")
    assert_err(solve("a/x = 2", "x", "1", {"a": var("3", "m")}), "nonlinear_equation")


def test_unknown_in_power_is_nonlinear():
    assert_err(solve("x^2 = 4", "x", "1"), "nonlinear_equation")
    assert_err(solve("x^1 = 4", "x", "1"), "nonlinear_equation")
    assert_err(solve("2^x = 8", "x", "1"), "nonlinear_equation")


# ---------------------------------------------------------------------------
# 结构标记回归：零值因子/相消子式不能掩盖未知量
# ---------------------------------------------------------------------------


def test_zero_factor_does_not_hide_unknown_in_product():
    # 0*x 的系数数值为 0，但未知量的结构标记保留：再与含未知量的子式相乘仍非线性
    assert_err(solve("0*x*x = 1", "x", "1"), "nonlinear_equation")
    assert_err(solve("0*x*x+x = 1", "x", "1"), "nonlinear_equation")
    assert_err(solve("x*0*x = 1", "x", "1"), "nonlinear_equation")
    assert_err(
        solve("a*x*x = 1", "x", "1", {"a": var("0", "1")}), "nonlinear_equation"
    )


def test_cancelling_subexpression_does_not_hide_unknown_in_product():
    # x-x 相消后系数为 0，但结构标记保留：两个含未知量的子式相乘仍非线性
    assert_err(solve("(x-x)*(x-x) = 0", "x", "1"), "nonlinear_equation")
    assert_err(solve("(x-x)*x = 1", "x", "1"), "nonlinear_equation")


def test_unknown_in_divisor_despite_zero_value():
    # 除数含未知量即非线性，即使其数值为 0（不得退化为除零错误）
    assert_err(solve("1/(x-x) = 1", "x", "1"), "nonlinear_equation")
    assert_err(solve("x/(x-x) = 1", "x", "1"), "nonlinear_equation")
    assert_err(solve("1/(0*x) = 1", "x", "1"), "nonlinear_equation")


def test_unknown_in_power_despite_zero_value():
    # 底数或指数含未知量即非线性，即使其数值为 0
    assert_err(solve("(x-x)^2+x = 1", "x", "1"), "nonlinear_equation")
    assert_err(solve("(x-x)^1 = 0", "x", "1"), "nonlinear_equation")
    assert_err(solve("(0*x)^2 = 0", "x", "1"), "nonlinear_equation")
    assert_err(solve("2^(x-x) = 1", "x", "1"), "nonlinear_equation")


def test_structural_marker_survives_add_negate_and_nesting():
    # 加减、取负与括号嵌套后，未知量再进入乘/除/幂位置仍判定非线性
    assert_err(solve("-(0*x)*x = 1", "x", "1"), "nonlinear_equation")
    assert_err(solve("((0*x))*((x)) = 1", "x", "1"), "nonlinear_equation")
    assert_err(solve("(0*x+1)*(x-1) = 0", "x", "1"), "nonlinear_equation")
    assert_err(solve("(x-x+0)*x = 1", "x", "1"), "nonlinear_equation")
    assert_err(solve("1/-(x-x) = 1", "x", "1"), "nonlinear_equation")


def test_zero_coefficient_linear_cases_unchanged():
    # 未知量处于一次线性位置时，零系数仍是“无解/多解”，不是非线性
    assert_err(solve("0*x = 1", "x", "1"), "no_unique_solution")
    assert_err(solve("0*x = 0", "x", "1"), "no_unique_solution")
    assert_err(solve("(x-x)+1 = 2", "x", "1"), "no_unique_solution")
    # 纯常数零除数仍是除零错误
    assert_err(solve("x/0 = 1", "x", "1"), "division_by_zero")
    assert_err(solve("x/(a-a) = 1", "x", "1", {"a": var("5", "m")}), "division_by_zero")


# ---------------------------------------------------------------------------
# 零系数：无解或多解
# ---------------------------------------------------------------------------


def test_identity_has_infinitely_many_solutions():
    assert_err(solve("x = x", "x", "1"), "no_unique_solution")
    assert_err(solve("2 = 2", "x", "1"), "no_unique_solution")


def test_contradiction_has_no_solution():
    assert_err(solve("x = x + 1", "x", "1"), "no_unique_solution")
    assert_err(solve("2 = 3", "x", "1"), "no_unique_solution")


def test_zero_coefficient_from_variable():
    assert_err(solve("a*x = 1", "x", "1", {"a": var("0", "1")}), "no_unique_solution")


# ---------------------------------------------------------------------------
# 量纲、除零、仿射与精度（全部 422）
# ---------------------------------------------------------------------------


def test_dimension_mismatch_inside_equation():
    assert_err(
        solve("x + a = b", "x", "m", {"a": var("1", "m"), "b": var("1", "s")}),
        "dimension_mismatch",
    )


def test_dimension_mismatch_with_output_unit():
    assert_err(solve("x = a", "x", "s", {"a": var("1", "m")}), "dimension_mismatch")


def test_division_by_zero():
    assert_err(solve("x/0 = 1", "x", "1"), "division_by_zero")
    assert_err(solve("x/(a-a) = 1", "x", "1", {"a": var("5", "m")}), "division_by_zero")


def test_absolute_temperature_variable_rejected():
    assert_err(solve("x = T", "x", "K", {"T": var("0", "degC")}), "affine_mixing")
    assert_err(solve("T*x = 1", "x", "1", {"T": var("0", "degF")}), "affine_mixing")
    assert_err(solve("x = T", "x", "K", {"T": var("273.15", "K")}), "affine_mixing")


def test_affine_output_unit_rejected():
    assert_err(solve("x = a", "x", "degC", {"a": var("10", "dC")}), "affine_mixing")
    assert_err(solve("x = a", "x", "degF", {"a": var("10", "dC")}), "affine_mixing")


def test_precision_overflow():
    # x = 1/3 不是有限十进制
    assert_err(solve("a*x = 1", "x", "1", {"a": var("3", "1")}), "precision_overflow")
    # 方程中的十进制定值有效数字超过 50
    assert_err(solve("1" + "2" * 50 + "*x = 1", "x", "1"), "precision_overflow")


def test_unknown_variable_and_symbol():
    assert_err(solve("x = y", "x", "1"), "unknown_variable")
    assert_err(solve("x = a", "x", "m", {"a": var("1", "furlong")}), "unknown_symbol")


def test_syntax_errors():
    assert_err(solve("2x = 4", "x", "1"), "invalid_syntax")  # 隐式相乘
    assert_err(solve("x = ", "x", "1"), "invalid_syntax")  # 空的一侧
    assert_err(solve("(x = 1", "x", "1"), "invalid_syntax")


# ---------------------------------------------------------------------------
# 请求校验（invalid_request）
# ---------------------------------------------------------------------------


def test_equation_must_have_exactly_one_equals():
    assert_err(solve("x + 1", "x", "1"), "invalid_request")
    assert_err(solve("x = 1 = 2", "x", "1"), "invalid_request")


def test_solve_for_must_not_be_in_variables():
    assert_err(solve("x = 1", "x", "1", {"x": var("1", "1")}), "invalid_request")


def test_solve_for_must_be_a_valid_name():
    assert_err(solve("a-b = 1", "a-b", "1"), "invalid_request")


def test_request_validation():
    def raw(body):
        return client.post("/solve", json=body)

    # 缺字段
    assert raw({"equation": "x = 1", "solve_for": "x"}).status_code == 422
    # 变量超过 20 个
    many = {f"v{i}": var("1", "1") for i in range(21)}
    assert raw(
        {"variables": many, "equation": "x = 1", "solve_for": "x", "output_unit": "1"}
    ).status_code == 422
    # 非法变量名
    bad = {
        "variables": {"a-b": var("1", "1")},
        "equation": "x = 1",
        "solve_for": "x",
        "output_unit": "1",
    }
    resp = raw(bad)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"
    # value 非法
    bad_value = {
        "variables": {"a": var(True, "m")},
        "equation": "x = a",
        "solve_for": "x",
        "output_unit": "m",
    }
    assert raw(bad_value).status_code == 422
