"""POST /convert 的端到端测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def conv(value, frm, to):
    return client.post("/convert", json={"value": value, "from": frm, "to": to})


def assert_ok(resp, value, unit, dimension):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"value": value, "unit": unit, "dimension": dimension}


def assert_err(resp, code):
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


# ---------------------------------------------------------------------------
# 基本换算与规范化
# ---------------------------------------------------------------------------


def test_newton_compound():
    assert_ok(conv("1", "kg*m/s^2", "N"), "1", "N", "kg*m/s^2")


def test_prefix_and_power():
    assert_ok(conv("1.5", "km", "m"), "1500", "m", "m")


def test_kmh_to_ms():
    assert_ok(conv("36", "km/h", "m/s"), "10", "m/s", "m/s")


def test_litre_to_cubic_metre():
    assert_ok(conv("1", "L", "m^3"), "0.001", "m^3", "m^3")


def test_parenthesized_power():
    assert_ok(conv("2", "(m/s)^2", "m^2/s^2"), "2", "m^2/s^2", "m^2/s^2")


def test_canonical_normalization():
    # 输入乱序/非规范，输出规范化且可往返解析
    assert_ok(conv("1", "s^-2*kg*m", "N"), "1", "N", "kg*m/s^2")
    assert_ok(conv("1", "N", "m*kg/s^2"), "1", "kg*m/s^2", "kg*m/s^2")


def test_dimensionless():
    assert_ok(conv("5", "1", "1"), "5", "1", "1")


def test_json_number_value_is_exact():
    # JSON 数值 0.1 按十进制 0.1 解释，而非二进制浮点
    assert_ok(conv(0.1, "m", "cm"), "10", "cm", "cm")


def test_stable_output():
    r1 = conv("98.6", "degF", "degC")
    r2 = conv("98.6", "degF", "degC")
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content


# ---------------------------------------------------------------------------
# 仿射温标与温差
# ---------------------------------------------------------------------------


def test_celsius_to_kelvin():
    assert_ok(conv("0", "degC", "K"), "273.15", "K", "K")


def test_kelvin_to_celsius():
    assert_ok(conv("273.15", "K", "degC"), "0", "degC", "K")


def test_fahrenheit_to_celsius():
    assert_ok(conv("98.6", "degF", "degC"), "37", "degC", "K")


def test_celsius_to_fahrenheit():
    assert_ok(conv("100", "degC", "degF"), "212", "degF", "K")


def test_fahrenheit_alias():
    assert_ok(conv("32", "°F", "K"), "273.15", "K", "K")


def test_delta_units():
    assert_ok(conv("1", "dC", "dF"), "1.8", "dF", "K")
    assert_ok(conv("9", "dF", "dC"), "5", "dC", "K")
    assert_ok(conv("1", "dK", "dC"), "1", "dC", "K")


def test_kelvin_is_neutral():
    assert_ok(conv("10", "K", "dC"), "10", "dC", "K")
    assert_ok(conv("10", "dC", "K"), "10", "K", "K")


def test_absolute_delta_mixing_rejected():
    assert_err(conv("1", "dC", "degC"), "affine_mixing")
    assert_err(conv("1", "degC", "dK"), "affine_mixing")
    assert_err(conv("1", "degC", "mK"), "affine_mixing")


def test_affine_in_compound_rejected():
    assert_err(conv("1", "kg*degC", "kg*K"), "affine_mixing")
    assert_err(conv("1", "degC^2", "K^2"), "affine_mixing")
    assert_err(conv("1", "J/degF", "J/K"), "affine_mixing")


def test_kelvin_in_compound_ok():
    assert_ok(conv("1", "J/(kg*K)", "N*m/(kg*K)"), "1", "N*m/K/kg", "m^2/s^2/K")


# ---------------------------------------------------------------------------
# 错误场景（全部 422 且不产出结果）
# ---------------------------------------------------------------------------


def test_dimension_mismatch():
    assert_err(conv("1", "m", "s"), "dimension_mismatch")


def test_unknown_symbol():
    assert_err(conv("1", "furlong", "m"), "unknown_symbol")
    assert_err(conv("1", "m2", "m^2"), "unknown_symbol")  # 必须写成 m^2


def test_invalid_power():
    assert_err(conv("1", "m^0.5", "m"), "invalid_power")
    assert_err(conv("1", "m^-2.5", "m"), "invalid_power")
    assert_err(conv("1", "m^100", "m"), "invalid_power")


def test_invalid_syntax():
    assert_err(conv("1", "m//s", "m/s"), "invalid_syntax")
    assert_err(conv("1", "2*m", "m"), "invalid_syntax")
    assert_err(conv("1", "(m", "m"), "invalid_syntax")


def test_precision_overflow_result():
    # 1 degF -> K 是无限循环小数 255.92777...
    assert_err(conv("1", "degF", "K"), "precision_overflow")
    # 1 s -> min = 0.01666...
    assert_err(conv("1", "s", "min"), "precision_overflow")
    # 50 位有效数字 * 10^9 超出 50 位精度上限
    assert_err(conv("1" + "0" * 49, "m", "nm"), "precision_overflow")


def test_precision_overflow_input():
    assert_err(conv("1" * 51, "m", "m"), "invalid_request")


def test_missing_field():
    resp = client.post("/convert", json={"value": "1", "from": "m"})
    assert_err(resp, "invalid_request")


# ---------------------------------------------------------------------------
# 其他端点
# ---------------------------------------------------------------------------


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_units_listing():
    resp = client.get("/units")
    assert resp.status_code == 200
    symbols = {u["symbol"] for u in resp.json()["units"]}
    assert {"m", "kg", "degC", "degF", "dC", "N"} <= symbols
