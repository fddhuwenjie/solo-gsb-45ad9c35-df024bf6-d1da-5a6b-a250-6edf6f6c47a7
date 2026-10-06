"""POST /evaluate 的端到端测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def ev(formula, variables, output_unit):
    return client.post(
        "/evaluate",
        json={"variables": variables, "formula": formula, "output_unit": output_unit},
    )


def var(value, unit):
    return {"value": value, "unit": unit}


def assert_ok(resp, value, unit, dimension):
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"value": value, "unit": unit, "dimension": dimension}


def assert_err(resp, code):
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


# ---------------------------------------------------------------------------
# 基本求值：数值与量纲合并
# ---------------------------------------------------------------------------


def test_speed_times_time():
    assert_ok(
        ev("v*t", {"v": var("60", "km/h"), "t": var("2", "h")}, "km"),
        "120", "km", "m",
    )


def test_addition_same_dimension_with_prefixes():
    assert_ok(
        ev("a+b", {"a": var("1", "m"), "b": var("50", "cm")}, "m"),
        "1.5", "m", "m",
    )


def test_force_formula():
    assert_ok(
        ev("m*a", {"m": var("2", "kg"), "a": var("3", "m/s^2")}, "N"),
        "6", "N", "kg*m/s^2",
    )


def test_kinematic_equation():
    # (v^2 - u^2) / (2*a) = (400-100)/10 = 30 m
    assert_ok(
        ev(
            "(v^2-u^2)/(2*a)",
            {"v": var("20", "m/s"), "u": var("10", "m/s"), "a": var("5", "m/s^2")},
            "m",
        ),
        "30", "m", "m",
    )


def test_power_precedence_is_right_associative():
    x = var("1", "1")
    assert_ok(ev("2*3^2", {"x": x}, "1"), "18", "1", "1")
    assert_ok(ev("2^3^2", {"x": x}, "1"), "512", "1", "1")
    assert_ok(ev("(2^3)^2", {"x": x}, "1"), "64", "1", "1")


def test_unary_minus_binds_looser_than_power():
    assert_ok(ev("-x^2", {"x": var("2", "m")}, "m^2"), "-4", "m^2", "m^2")


def test_negative_exponent():
    assert_ok(ev("f^-1", {"f": var("2", "s")}, "Hz"), "0.5", "Hz", "1/s")


def test_variable_exponent_must_be_dimensionless_integer():
    assert_err(
        ev("a^b", {"a": var("3", "m"), "b": var("2", "s")}, "m^2"),
        "invalid_power",
    )
    assert_err(
        ev("a^b", {"a": var("3", "m"), "b": var("0.5", "1")}, "m"),
        "invalid_power",
    )
    assert_ok(
        ev("a^b", {"a": var("3", "m"), "b": var("2", "1")}, "m^2"),
        "9", "m^2", "m^2",
    )


def test_decimal_constants_are_dimensionless():
    assert_ok(
        ev("2*a+0.5*a", {"a": var("3", "m")}, "m"),
        "7.5", "m", "m",
    )


def test_dimensionless_result():
    assert_ok(
        ev("a/b", {"a": var("4", "m"), "b": var("2", "m")}, "1"),
        "2", "1", "1",
    )


def test_zero_result():
    assert_ok(ev("a-a", {"a": var("5", "m")}, "m"), "0", "m", "m")


def test_division_chaining_and_parentheses():
    assert_ok(
        ev("a/b/c", {"a": var("12", "m"), "b": var("2", "s"), "c": var("3", "s")}, "m/s^2"),
        "2", "m/s^2", "m/s^2",
    )
    assert_ok(
        ev("(a+b)*2", {"a": var("1", "m"), "b": var("2", "cm")}, "m"),
        "2.04", "m", "m",
    )


def test_json_number_value_is_exact_decimal():
    assert_ok(
        ev("a", {"a": var(0.1, "m")}, "m"),
        "0.1", "m", "m",
    )


def test_unused_variables_are_allowed():
    assert_ok(
        ev("a", {"a": var("1", "m"), "unused": var("9", "s")}, "m"),
        "1", "m", "m",
    )


# ---------------------------------------------------------------------------
# 绝对温标变量只能作为公式唯一项
# ---------------------------------------------------------------------------


def test_affine_variable_as_sole_term():
    assert_ok(
        ev("T", {"T": var("98.6", "degF")}, "degC"),
        "37", "degC", "K",
    )
    assert_ok(
        ev("T", {"T": var("0", "degC")}, "K"),
        "273.15", "K", "K",
    )


def test_parenthesized_sole_affine_variable_ok():
    assert_ok(
        ev("(T)", {"T": var("32", "degF")}, "K"),
        "273.15", "K", "K",
    )


def test_affine_variable_in_any_operation_rejected():
    t = {"T": var("0", "degC"), "a": var("1", "K")}
    assert_err(ev("T+a", t, "K"), "affine_mixing")
    assert_err(ev("T*2", {"T": var("0", "degC")}, "K"), "affine_mixing")
    assert_err(ev("T/1", {"T": var("0", "degC")}, "K"), "affine_mixing")
    assert_err(ev("-T", {"T": var("0", "degC")}, "K"), "affine_mixing")
    assert_err(ev("T^1", {"T": var("0", "degC")}, "K"), "affine_mixing")


def test_kelvin_variable_is_absolute():
    # 裸 K 变量属于绝对温标，不能参与运算……
    assert_err(
        ev("a+b", {"a": var("1", "K"), "b": var("2", "K")}, "K"),
        "affine_mixing",
    )
    # ……但作为唯一项可以换算到绝对温标
    assert_ok(ev("a", {"a": var("273.15", "K")}, "degC"), "0", "degC", "K")
    # 且不能换算到温差或带词头的 K
    assert_err(ev("a", {"a": var("10", "K")}, "dC"), "affine_mixing")
    assert_err(ev("a", {"a": var("1", "K")}, "mK"), "affine_mixing")


def test_delta_temperature_variables_are_linear():
    assert_ok(
        ev("a-b", {"a": var("10", "dC"), "b": var("5", "dC")}, "dF"),
        "9", "dF", "K",
    )
    # 温差结果不能换算到绝对温标
    assert_err(ev("a", {"a": var("10", "dC")}, "degC"), "affine_mixing")


def test_affine_unit_in_compound_variable_rejected():
    assert_err(ev("x", {"x": var("1", "kg*degC")}, "kg*K"), "affine_mixing")


# ---------------------------------------------------------------------------
# 错误场景（全部 422）
# ---------------------------------------------------------------------------


def test_unknown_variable():
    assert_err(ev("a+b", {"a": var("1", "m")}, "m"), "unknown_variable")


def test_unknown_unit_symbol():
    assert_err(ev("a", {"a": var("1", "furlong")}, "m"), "unknown_symbol")


def test_dimension_mismatch_in_formula():
    assert_err(
        ev("a+b", {"a": var("1", "m"), "b": var("1", "kg")}, "m"),
        "dimension_mismatch",
    )


def test_dimension_mismatch_with_output_unit():
    assert_err(ev("a", {"a": var("1", "m")}, "s"), "dimension_mismatch")


def test_division_by_zero():
    assert_err(
        ev("a/b", {"a": var("1", "m"), "b": var("0", "s")}, "m/s"),
        "division_by_zero",
    )
    assert_err(ev("x^-1", {"x": var("0", "m")}, "1/m"), "division_by_zero")


def test_implicit_multiplication_rejected():
    assert_err(ev("2 x", {"x": var("1", "1")}, "1"), "invalid_syntax")
    assert_err(
        ev("x y", {"x": var("1", "m"), "y": var("1", "s")}, "m*s"),
        "invalid_syntax",
    )
    assert_err(
        ev("x(y)", {"x": var("1", "1"), "y": var("1", "1")}, "1"),
        "invalid_syntax",
    )


def test_formula_syntax_errors():
    assert_err(ev("(x", {"x": var("1", "m")}, "m"), "invalid_syntax")
    assert_err(ev("x@2", {"x": var("1", "m")}, "m"), "invalid_syntax")
    assert_err(ev("x**", {"x": var("1", "m")}, "m"), "invalid_syntax")


def test_power_limits():
    assert_err(ev("x^100", {"x": var("1", "m")}, "m^100"), "invalid_power")
    assert_err(ev("x^0.5", {"x": var("1", "m")}, "m"), "invalid_power")


def test_precision_overflow():
    # 1 m / 3 m = 1/3 不是有限十进制
    assert_err(
        ev("a/b", {"a": var("1", "m"), "b": var("3", "m")}, "1"),
        "precision_overflow",
    )
    # 十进制定值有效数字超过 50
    assert_err(
        ev(
            "1" + "2" * 50,
            {"x": var("1", "1")},
            "1",
        ),
        "precision_overflow",
    )


def test_request_validation():
    def raw(body):
        return client.post("/evaluate", json=body)

    # 0 个 / 21 个变量
    assert raw({"variables": {}, "formula": "1", "output_unit": "1"}).status_code == 422
    many = {f"v{i}": var("1", "1") for i in range(21)}
    assert raw({"variables": many, "formula": "v0", "output_unit": "1"}).status_code == 422
    # 非法变量名
    bad = {"variables": {"a-b": var("1", "1")}, "formula": "1", "output_unit": "1"}
    resp = raw(bad)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_request"
    # 缺字段
    assert raw({"variables": {"a": var("1", "1")}}).status_code == 422
    # value 非法
    assert raw({"variables": {"a": var(True, "m")}, "formula": "a", "output_unit": "m"}).status_code == 422
