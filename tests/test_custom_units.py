"""请求级自定义线性单位（custom_units）的端到端测试。

覆盖 /convert、/evaluate、/solve 三个接口：
- 自定义单位换算、前向引用、链式展开、自定义符号的规范输出；
- 冲突（内置单位/别名/SI 词头可解析符号）、重名、循环、未知引用、
  非法比例、禁引 degC/degF/裸 K 均返回 422；
- 不自动获得词头；请求间隔离（含并发）且不修改全局注册表；
- 无 custom_units 时原有行为完全不变。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app
from app.units import REGISTRY

client = TestClient(app)


def cu(symbol, factor, base_unit):
    return {"symbol": symbol, "factor": factor, "base_unit": base_unit}


def assert_ok(resp, value, unit, dimension):
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"value": value, "unit": unit, "dimension": dimension}


def assert_err(resp, code):
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == code


# ---------------------------------------------------------------------------
# /convert：基本换算、链式定义与前向引用
# ---------------------------------------------------------------------------


def test_simple_custom_unit():
    r = client.post(
        "/convert",
        json={
            "value": "10", "from": "ft", "to": "m",
            "custom_units": [cu("ft", "0.3048", "m")],
        },
    )
    assert_ok(r, "3.048", "m", "m")
    r = client.post(
        "/convert",
        json={
            "value": "3.048", "from": "m", "to": "ft",
            "custom_units": [cu("ft", "0.3048", "m")],
        },
    )
    assert_ok(r, "10", "ft", "m")


def test_chain_with_forward_reference():
    # 1 furlong = 10 chain = 220 m；furlong 先声明、引用后声明的 chain
    r = client.post(
        "/convert",
        json={
            "value": "2", "from": "furlong", "to": "m",
            "custom_units": [
                cu("furlong", "10", "chain"),
                cu("chain", "22", "m"),
            ],
        },
    )
    assert_ok(r, "440", "m", "m")
    r = client.post(
        "/convert",
        json={
            "value": "220", "from": "m", "to": "furlong",
            "custom_units": [
                cu("furlong", "10", "chain"),
                cu("chain", "22", "m"),
            ],
        },
    )
    assert_ok(r, "1", "furlong", "m")


def test_declaration_order_is_irrelevant():
    # 后声明的符号引用先声明的符号，同样可解析
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "frog", "to": "m",
            "custom_units": [
                cu("rod", "5.0292", "m"),
                cu("frog", "40", "rod"),
            ],
        },
    )
    assert r.status_code == 200
    assert r.json()["value"] == "201.168"


def test_custom_to_custom_conversion():
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "mile", "to": "ft",
            "custom_units": [
                cu("mile", "1609.344", "m"),
                cu("ft", "0.3048", "m"),
            ],
        },
    )
    assert_ok(r, "5280", "ft", "m")


def test_compound_and_power_custom_units():
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "box^3", "to": "m^3",
            "custom_units": [cu("box", "0.1", "m")],
        },
    )
    assert_ok(r, "0.001", "m^3", "m^3")
    r = client.post(
        "/convert",
        json={
            "value": "24", "from": "1", "to": "dozen",
            "custom_units": [cu("dozen", "12", "1")],
        },
    )
    assert_ok(r, "2", "dozen", "1")


def test_dimensionless_custom_unit():
    r = client.post(
        "/convert",
        json={
            "value": "3", "from": "gross", "to": "dozen",
            "custom_units": [
                cu("gross", "144", "1"),
                cu("dozen", "12", "1"),
            ],
        },
    )
    assert_ok(r, "36", "dozen", "1")


def test_canonical_unit_preserves_custom_symbols():
    # 结果单位保留自定义符号的规范表达式（正指数按符号排序、负指数以 / 连接）
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "N*bar", "to": "kg*bar*m/s^2",
            "custom_units": [cu("bar", "100000", "Pa")],
        },
    )
    assert_ok(r, "1", "bar*kg*m/s^2", "kg^2/s^4")
    # dimension 始终是 SI 基本量纲，不含自定义符号
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "bar", "to": "Pa",
            "custom_units": [cu("bar", "100000", "Pa")],
        },
    )
    assert_ok(r, "100000", "Pa", "kg/m/s^2")

def test_custom_dimension_mismatch():
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "foo", "to": "s",
            "custom_units": [cu("foo", "2", "m")],
        },
    )
    assert_err(r, "dimension_mismatch")


# ---------------------------------------------------------------------------
# /evaluate
# ---------------------------------------------------------------------------


def test_evaluate_with_custom_units():
    r = client.post(
        "/evaluate",
        json={
            "variables": {
                "a": {"value": "2", "unit": "dozen"},
                "b": {"value": "3", "unit": "dozen"},
            },
            "formula": "a*b",
            "output_unit": "dozen^2",
            "custom_units": [cu("dozen", "12", "1")],
        },
    )
    assert_ok(r, "6", "dozen^2", "1")


def test_evaluate_variables_and_output_share_definitions():
    r = client.post(
        "/evaluate",
        json={
            "variables": {
                "n": {"value": "10", "unit": "ft"},
                "w": {"value": "2", "unit": "ft"},
            },
            "formula": "n*w",
            "output_unit": "ft^2",
            "custom_units": [cu("ft", "0.3048", "m")],
        },
    )
    assert_ok(r, "20", "ft^2", "m^2")


def test_evaluate_custom_dimension_mismatch_in_formula():
    r = client.post(
        "/evaluate",
        json={
            "variables": {
                "a": {"value": "1", "unit": "foo"},
                "b": {"value": "1", "unit": "baz"},
            },
            "formula": "a+b",
            "output_unit": "foo",
            "custom_units": [cu("foo", "2", "m"), cu("baz", "3", "s")],
        },
    )
    assert_err(r, "dimension_mismatch")


# ---------------------------------------------------------------------------
# /solve
# ---------------------------------------------------------------------------


def test_solve_with_custom_units():
    r = client.post(
        "/solve",
        json={
            "variables": {
                "v": {"value": "60", "unit": "km/h"},
                "s": {"value": "120", "unit": "km"},
            },
            "equation": "v*t = s",
            "solve_for": "t",
            "output_unit": "h",
        },
    )
    assert_ok(r, "2", "h", "s")
    r = client.post(
        "/solve",
        json={
            "variables": {"a": {"value": "3", "unit": "rod"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "m",
            "custom_units": [cu("rod", "5.0292", "m")],
        },
    )
    assert_ok(r, "15.0876", "m", "m")


def test_solve_custom_chain():
    r = client.post(
        "/solve",
        json={
            "variables": {"a": {"value": "6", "unit": "mdeg"}},
            "equation": "x = a",
            "solve_for": "x",
            "output_unit": "mK",
            "custom_units": [cu("mdeg", "2", "mK")],
        },
    )
    assert_ok(r, "12", "mK", "K")


# ---------------------------------------------------------------------------
# 自定义温度链：只能线性（基准禁引 degC/degF/裸 K；dC/dF/dK 与带词头 K 可）
# ---------------------------------------------------------------------------


def test_custom_delta_temperature_chain_is_linear():
    defs = [cu("mdeg", "2", "mK")]
    r = client.post(
        "/convert",
        json={"value": "3", "from": "mdeg", "to": "mK", "custom_units": defs},
    )
    assert_ok(r, "6", "mK", "K")
    r = client.post(
        "/evaluate",
        json={
            "variables": {
                "a": {"value": "10", "unit": "mdeg"},
                "b": {"value": "4", "unit": "mdeg"},
            },
            "formula": "a-b",
            "output_unit": "mK",
            "custom_units": defs,
        },
    )
    assert_ok(r, "12", "mK", "K")
    # 线性量可以参与乘法（自定义单位不会变成仿射温标）
    r = client.post(
        "/evaluate",
        json={
            "variables": {"t": {"value": "5", "unit": "mdeg"}},
            "formula": "t*2",
            "output_unit": "mK",
            "custom_units": defs,
        },
    )
    assert_ok(r, "20", "mK", "K")


def test_forbidden_base_temperature_symbols():
    for base in ("degC", "degF", "°C", "°F", "K", "1/K", "K^2", "kg*degC", "J/degF"):
        r = client.post(
            "/convert",
            json={
                "value": "1", "from": "m", "to": "m",
                "custom_units": [cu("aa", "1", base)],
            },
        )
        assert_err(r, "invalid_request")


def test_prefixed_kelvin_and_delta_units_allowed_as_base():
    for base in ("mK", "nK", "dC", "dF", "dK"):
        r = client.post(
            "/convert",
            json={
                "value": "1", "from": "aa", "to": base,
                "custom_units": [cu("aa", "1", base)],
            },
        )
        assert r.status_code == 200, (base, r.text)


def test_forbidden_base_via_chain():
    # 自定义符号间接引用裸 K / degF 同样拒绝
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "aa", "to": "m",
            "custom_units": [cu("aa", "1", "bb"), cu("bb", "1", "K")],
        },
    )
    assert_err(r, "invalid_request")
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "aa", "to": "m",
            "custom_units": [cu("aa", "1", "bb"), cu("bb", "1", "degF")],
        },
    )
    assert_err(r, "invalid_request")


# ---------------------------------------------------------------------------
# 声明冲突：内置单位、别名、SI 词头可解析符号
# ---------------------------------------------------------------------------


def test_symbol_conflict_with_builtins():
    for symbol in ("m", "kg", "N", "Hz", "min", "h", "d", "L", "degC", "dC"):
        r = client.post(
            "/convert",
            json={
                "value": "1", "from": "m", "to": "m",
                "custom_units": [cu(symbol, "1", "m")],
            },
        )
        assert_err(r, "invalid_request")


def test_symbol_conflict_with_alias():
    for symbol in ("l", "ℓ", "Ω", "°C", "°F"):
        r = client.post(
            "/convert",
            json={
                "value": "1", "from": "m", "to": "m",
                "custom_units": [cu(symbol, "1", "m")],
            },
        )
        assert_err(r, "invalid_request")


def test_symbol_conflict_with_prefixed_unit():
    # 可由“词头 + 可加词头单位”解析的符号不得声明
    for symbol in ("km", "mm", "MHz", "kPa", "mL", "µm", "μm", "daN", "cm"):
        r = client.post(
            "/convert",
            json={
                "value": "1", "from": "m", "to": "m",
                "custom_units": [cu(symbol, "1", "m")],
            },
        )
        assert_err(r, "invalid_request")


def test_bare_prefix_letters_are_not_conflicts():
    # 纯 SI 词头字面量本身（k、da）解析不出任何单位，可以声明
    for symbol in ("k", "da"):
        r = client.post(
            "/convert",
            json={
                "value": "3", "from": symbol, "to": "1",
                "custom_units": [cu(symbol, "100", "1")],
            },
        )
        assert_ok(r, "300", "1", "1")


def test_custom_symbols_get_no_prefixes():
    # foo 已声明，kfoo 不会被当作 k*foo
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "kfoo", "to": "1",
            "custom_units": [cu("foo", "2", "1")],
        },
    )
    assert_err(r, "unknown_symbol")


# ---------------------------------------------------------------------------
# 重名、循环、未知引用、非法比例（均 422）
# ---------------------------------------------------------------------------


def test_duplicate_symbol_rejected():
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "m", "to": "m",
            "custom_units": [cu("aa", "1", "m"), cu("aa", "2", "m")],
        },
    )
    assert_err(r, "invalid_request")


def test_self_reference_is_a_cycle():
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "m", "to": "m",
            "custom_units": [cu("aa", "1", "aa")],
        },
    )
    assert_err(r, "invalid_request")


def test_cyclic_definitions_rejected():
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "m", "to": "m",
            "custom_units": [cu("aa", "1", "bb"), cu("bb", "1", "aa")],
        },
    )
    assert_err(r, "invalid_request")
    # 三环
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "m", "to": "m",
            "custom_units": [
                cu("aa", "1", "bb"),
                cu("bb", "1", "cc"),
                cu("cc", "1", "aa"),
            ],
        },
    )
    assert_err(r, "invalid_request")


def test_unknown_reference_rejected():
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "m", "to": "m",
            "custom_units": [cu("aa", "1", "zzz")],
        },
    )
    assert_err(r, "unknown_symbol")
    # 源表达式引用未声明符号也是 unknown_symbol
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "zzz", "to": "m",
            "custom_units": [cu("aa", "1", "m")],
        },
    )
    assert_err(r, "unknown_symbol")


def test_unused_symbol_with_bad_definition_still_rejected():
    # 整组定义都必须合法：未被表达式使用的符号有环也要失败
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "m", "to": "m",
            "custom_units": [
                cu("good", "1", "m"),
                cu("aa", "1", "bb"),
                cu("bb", "1", "aa"),
            ],
        },
    )
    assert_err(r, "invalid_request")


def test_invalid_factor_rejected():
    for factor in (0, -1, "-0.5", "abc", "1.2.3", True, "NaN", "Infinity", "-Infinity"):
        r = client.post(
            "/convert",
            json={
                "value": "1", "from": "m", "to": "m",
                "custom_units": [cu("aa", factor, "m")],
            },
        )
        assert r.status_code == 422, (factor, r.text)
        assert r.json()["detail"]["error"] == "invalid_request"


def test_factor_precision_limit():
    big = "1" + "0" * 50  # 51 位有效数字
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "m", "to": "m",
            "custom_units": [cu("aa", big, "m")],
        },
    )
    assert_err(r, "invalid_request")


def test_custom_units_count_limits():
    seven = [cu(f"u{i}", "1", "m") for i in range(7)]
    r = client.post(
        "/convert",
        json={"value": "1", "from": "m", "to": "m", "custom_units": seven},
    )
    assert_err(r, "invalid_request")
    r = client.post(
        "/convert",
        json={"value": "1", "from": "m", "to": "m", "custom_units": []},
    )
    assert_err(r, "invalid_request")


def test_malformed_custom_unit_items():
    def raw(units):
        return client.post(
            "/convert",
            json={"value": "1", "from": "m", "to": "m", "custom_units": units},
        )

    assert_err(raw([{"symbol": "", "factor": "1", "base_unit": "m"}]), "invalid_request")
    assert_err(raw([{"symbol": "a", "factor": "1"}]), "invalid_request")
    assert_err(raw([{"symbol": "a", "base_unit": "m"}]), "invalid_request")
    assert_err(raw([{"factor": "1", "base_unit": "m"}]), "invalid_request")
    assert_err(raw([{"symbol": "2a", "factor": "1", "base_unit": "m"}]), "invalid_request")
    assert_err(raw("not-a-list"), "invalid_request")


def test_base_unit_syntax_errors():
    r = client.post(
        "/convert",
        json={
            "value": "1", "from": "m", "to": "m",
            "custom_units": [cu("aa", "1", "m//s")],
        },
    )
    assert_err(r, "invalid_syntax")


# ---------------------------------------------------------------------------
# 请求隔离与并发：不同请求可重定义同名符号；全局表不变
# ---------------------------------------------------------------------------


def test_requests_are_isolated():
    r1 = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_units": [cu("foo", "2", "m")]},
    )
    r2 = client.post(
        "/convert",
        json={"value": "1", "from": "foo", "to": "m", "custom_units": [cu("foo", "7", "m")]},
    )
    assert_ok(r1, "2", "m", "m")
    assert_ok(r2, "7", "m", "m")
    # 下一个不带 custom_units 的请求中 foo 仍是未知符号
    r3 = client.post("/convert", json={"value": "1", "from": "foo", "to": "m"})
    assert_err(r3, "unknown_symbol")


def test_concurrent_requests_use_their_own_definitions():
    def call(factor):
        r = client.post(
            "/convert",
            json={"value": "1", "from": "foo", "to": "m", "custom_units": [cu("foo", factor, "m")]},
        )
        return r.json()["value"]

    factors = ["2"] * 25 + ["7"] * 25
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(call, factors))
    assert results.count("2") == 25
    assert results.count("7") == 25


def test_global_registry_is_not_mutated():
    client.post(
        "/convert",
        json={
            "value": "1", "from": "ft", "to": "m",
            "custom_units": [cu("ft", "0.3048", "m")],
        },
    )
    assert "ft" not in REGISTRY
    assert "foo" not in REGISTRY


# ---------------------------------------------------------------------------
# 无 custom_units：三个接口的原有行为与响应字段完全不变
# ---------------------------------------------------------------------------


def test_without_custom_units_behaviour_unchanged():
    assert_ok(
        client.post(
            "/convert", json={"value": "36", "from": "km/h", "to": "m/s"}
        ),
        "10", "m/s", "m/s",
    )
    assert_err(
        client.post("/convert", json={"value": "1", "from": "ft", "to": "m"}),
        "unknown_symbol",
    )
    assert_ok(
        client.post(
            "/evaluate",
            json={
                "variables": {
                    "v": {"value": "60", "unit": "km/h"},
                    "t": {"value": "2", "unit": "h"},
                },
                "formula": "v*t",
                "output_unit": "km",
            },
        ),
        "120", "km", "m",
    )
    assert_ok(
        client.post(
            "/solve",
            json={
                "variables": {
                    "F": {"value": "6", "unit": "N"},
                    "m": {"value": "2", "unit": "kg"},
                },
                "equation": "F = m*a",
                "solve_for": "a",
                "output_unit": "m/s^2",
            },
        ),
        "3", "m/s^2", "m/s^2",
    )


def test_null_custom_units_is_equivalent_to_absent():
    r = client.post(
        "/convert",
        json={"value": "1", "from": "m", "to": "cm", "custom_units": None},
    )
    assert_ok(r, "100", "cm", "m")
