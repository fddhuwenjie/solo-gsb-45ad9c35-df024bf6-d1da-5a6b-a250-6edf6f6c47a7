"""方程求解器：用带单位的等式反求一个未知量。

等式两侧分别被解析为关于未知量 ``x`` 的仿射形式 ``coef * x + const``
（系数与常数均为 :class:`Fraction` 精确值并携带量纲），再解

    (lhs.coef - rhs.coef) * x = rhs.const - lhs.const

规则：
- 每侧语法与 /evaluate 的公式一致（变量、十进制定值、``+ - * /``、括号、
  整数幂），未知量以 ``solve_for`` 指定的名字引用，且不得出现在变量映射中。
- 未知量只能处于一次线性位置：两个含未知量的子式相乘、未知量作除数、
  未知量参与幂运算均判定为非线性；幂只允许作用于纯常数子式。
  是否“含未知量”按**结构**判定（``has_unknown`` 标记）：未知量一旦出现于
  某个子式中，即使其系数经零值因子（``0*x``）或相消（``x-x``）后数值为 0，
  该子式仍视为含未知量；加减、取负与括号嵌套都保留这一标记。
- 系数为零时方程无解（两侧常数项不相等）或有无穷多解（恒等式），均返回 422。
- 绝对温标变量（单位为裸 degC/degF/K）不能出现在方程中；温差变量可以。
- 结果以 SI 基单位口径返回；若解恒为零且量纲不受方程约束（如 ``2*x = x``），
  量纲为 ``None``，由调用方决定如何换算。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Mapping

from .config import MAX_POWER, MAX_PRECISION
from .errors import (
    affine_mixing,
    dimension_mismatch,
    division_by_zero,
    invalid_power,
    invalid_syntax,
    no_unique_solution,
    nonlinear_equation,
    precision_overflow,
    unknown_variable,
)
from .evaluator import MAX_NESTING, Quantity, _TOKEN_RE
from .parser import canonical_dim
from .units import DIMLESS


@dataclass(frozen=True)
class _LinearForm:
    """关于未知量的一次仿射形式：``coef * x + const``。

    分量为 ``None`` 表示该分量为零且量纲未定（量纲多态，可与任何量纲合并）；
    非 ``None`` 分量携带精确值与量纲（值本身也可能为零，但量纲已知，
    参与合并时按严格量纲校验，与 /evaluate 一致）。

    ``has_unknown`` 是结构标记：子式中是否出现过未知量，与系数数值无关。
    未知量经加减、取负、括号嵌套后标记保留；即使系数被零值因子或相消
    子式抵消为 0（如 ``0*x``、``x-x``），标记仍为 True，从而保证未知量
    再次进入乘法、除数或幂位置时仍被判定为非线性。
    """

    coef: Quantity | None
    const: Quantity | None
    has_unknown: bool = False


def _is_nonzero(term: Quantity | None) -> bool:
    return term is not None and term.value_si != 0


def _neg_term(term: Quantity | None) -> Quantity | None:
    return None if term is None else Quantity(-term.value_si, term.dim)


def _add_term(
    a: Quantity | None, b: Quantity | None, *, subtract: bool
) -> Quantity | None:
    if a is None:
        return _neg_term(b) if subtract else b
    if b is None:
        return a
    if a.dim != b.dim:
        raise dimension_mismatch(canonical_dim(a.dim), canonical_dim(b.dim))
    value = a.value_si - b.value_si if subtract else a.value_si + b.value_si
    return Quantity(value, a.dim)


def _mul_term(a: Quantity | None, b: Quantity | None) -> Quantity | None:
    if a is None or b is None:
        return None
    dim = tuple(x + y for x, y in zip(a.dim, b.dim))
    return Quantity(a.value_si * b.value_si, dim)


def _div_term(a: Quantity | None, b: Quantity) -> Quantity | None:
    """``a / b``；``b`` 必须是非零常数（调用方保证）。"""
    if a is None:
        return None
    dim = tuple(x - y for x, y in zip(a.dim, b.dim))
    return Quantity(a.value_si / b.value_si, dim)


def _add_form(a: _LinearForm, b: _LinearForm, *, subtract: bool) -> _LinearForm:
    return _LinearForm(
        _add_term(a.coef, b.coef, subtract=subtract),
        _add_term(a.const, b.const, subtract=subtract),
        a.has_unknown or b.has_unknown,
    )


def _neg_form(a: _LinearForm) -> _LinearForm:
    return _LinearForm(_neg_term(a.coef), _neg_term(a.const), a.has_unknown)


def _mul_form(a: _LinearForm, b: _LinearForm) -> _LinearForm:
    # (ac*x + ak) * (bc*x + bk)：两侧都含未知量时出现 x^2 项，判定非线性；
    # 是否含未知量按结构判定，系数被零值因子/相消子式抵消为 0 也不例外
    if a.has_unknown and b.has_unknown:
        raise nonlinear_equation("含未知量的子式相乘导致次数超过一次（非线性）")
    const = _mul_term(a.const, b.const)
    if _is_nonzero(a.coef):
        coef = _mul_term(a.coef, b.const)
    elif _is_nonzero(b.coef):
        coef = _mul_term(a.const, b.coef)
    else:
        coef = None
    return _LinearForm(coef, const, a.has_unknown or b.has_unknown)


def _div_form(a: _LinearForm, b: _LinearForm) -> _LinearForm:
    if b.has_unknown:
        raise nonlinear_equation("未知量不能出现在除数中（非线性）")
    if not _is_nonzero(b.const):
        raise division_by_zero()
    return _LinearForm(
        _div_term(a.coef, b.const), _div_term(a.const, b.const), a.has_unknown
    )


def _pow_form(base: _LinearForm, exponent: _LinearForm) -> _LinearForm:
    if base.has_unknown or exponent.has_unknown:
        raise nonlinear_equation("未知量不能参与幂运算（非线性）")
    return _LinearForm(None, _pow_const(base.const, exponent.const))


def _pow_const(base: Quantity, exponent: Quantity) -> Quantity:
    """纯常数子式的整数幂，语义与 /evaluate 一致。

    调用方（``_pow_form``）保证两侧都不含未知量，而不含未知量的仿射形式
    常数项必非 ``None``，因此这里只处理确定的常数。
    """
    if exponent.dim != DIMLESS:
        raise invalid_power("幂指数必须是无量纲整数")
    exp = exponent.value_si
    if exp.denominator != 1:
        raise invalid_power("幂指数必须是整数")
    n = exp.numerator
    if abs(n) > MAX_POWER:
        raise invalid_power(f"幂指数绝对值不能超过 {MAX_POWER}")
    if base.value_si == 0 and n < 0:
        raise division_by_zero()
    return Quantity(base.value_si**n, tuple(x * n for x in base.dim))


class _Solver:
    """把等式一侧解析为关于未知量的仿射形式（递归下降，语法同 /evaluate）。"""

    def __init__(
        self, text: str, unknown: str, variables: Mapping[str, Quantity]
    ) -> None:
        self._unknown = unknown
        self._vars = variables
        self._tokens: list[tuple[str, str]] = []
        for m in _TOKEN_RE.finditer(text):
            kind = m.lastgroup or "bad"
            if kind == "ws":
                continue
            if kind == "bad":
                raise invalid_syntax(f"无法识别的字符: {m.group()!r}")
            self._tokens.append((kind, m.group()))
        self._pos = 0
        self._depth = 0

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > MAX_NESTING:
            raise invalid_syntax(f"公式嵌套深度超过 {MAX_NESTING} 层的上限")

    def _leave(self) -> None:
        self._depth -= 1

    # -- 词法辅助 ---------------------------------------------------------
    def _peek(self) -> str | None:
        return self._tokens[self._pos][0] if self._pos < len(self._tokens) else None

    def _next(self) -> tuple[str, str]:
        if self._pos >= len(self._tokens):
            raise invalid_syntax("公式意外结束")
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _here(self) -> str:
        return self._tokens[self._pos][1] if self._pos < len(self._tokens) else "<结尾>"

    # -- 语法 -------------------------------------------------------------
    def parse(self) -> _LinearForm:
        if not self._tokens:
            raise invalid_syntax("等式一侧为空")
        result = self._expr()
        if self._peek() is not None:
            raise invalid_syntax(
                f"不允许隐式相乘或存在多余输入: {self._here()!r}"
            )
        return result

    def _expr(self) -> _LinearForm:
        left = self._term()
        while self._peek() in ("add", "sub"):
            op, _ = self._next()
            right = self._term()
            left = _add_form(left, right, subtract=(op == "sub"))
        return left

    def _term(self) -> _LinearForm:
        left = self._unary()
        while self._peek() in ("mul", "div"):
            op, _ = self._next()
            right = self._unary()
            left = _mul_form(left, right) if op == "mul" else _div_form(left, right)
        return left

    def _unary(self) -> _LinearForm:
        if self._peek() in ("add", "sub"):
            op, _ = self._next()
            self._enter()
            try:
                operand = self._unary()
            finally:
                self._leave()
            return _neg_form(operand) if op == "sub" else operand
        return self._factor()

    def _factor(self) -> _LinearForm:
        base = self._primary()
        if self._peek() == "pow":
            self._next()
            exponent = self._unary()  # 指数位置允许一元负号，如 2^-1
            base = _pow_form(base, exponent)
        return base

    def _primary(self) -> _LinearForm:
        kind = self._peek()
        if kind == "lpar":
            self._next()
            self._enter()
            try:
                inner = self._expr()
            finally:
                self._leave()
            if self._peek() != "rpar":
                raise invalid_syntax(f"语法错误: 期望 ')'，实际为 {self._here()!r}")
            self._next()
            return inner
        if kind == "name":
            _, text = self._next()
            if text == self._unknown:
                return _LinearForm(Quantity(Fraction(1), DIMLESS), None, True)
            quantity = self._vars.get(text)
            if quantity is None:
                raise unknown_variable(text)
            if quantity.absolute:
                raise affine_mixing(
                    "绝对温标变量（单位为 degC/degF/K）不能出现在待解方程中，"
                    "温差请使用 dC/dF/dK"
                )
            return _LinearForm(None, Quantity(quantity.value_si, quantity.dim))
        if kind == "num":
            _, text = self._next()
            dec = Decimal(text)
            if len(dec.as_tuple().digits) > MAX_PRECISION:
                raise precision_overflow(
                    f"十进制定值有效数字超过 {MAX_PRECISION} 位的精度上限"
                )
            return _LinearForm(None, Quantity(Fraction(dec), DIMLESS))
        raise invalid_syntax(f"语法错误: 此处不应出现 {self._here()!r}")


def solve(
    equation: str, unknown: str, variables: Mapping[str, Quantity]
) -> tuple[Fraction, tuple[int, ...] | None]:
    """求解 ``equation`` 中的未知量，返回 ``(SI 口径精确值, 量纲)``。

    量纲为 ``None`` 表示解恒为零且方程未约束其量纲（如 ``2*x = x``）。
    任何非法输入都抛出 :class:`ConversionError`（对应 422）。
    """
    if equation.count("=") != 1:
        raise invalid_syntax("等式必须恰好包含一个等号 '='")
    lhs_text, rhs_text = equation.split("=")
    lhs = _Solver(lhs_text, unknown, variables).parse()
    rhs = _Solver(rhs_text, unknown, variables).parse()
    # (lhs.coef - rhs.coef) * x = rhs.const - lhs.const
    coef = _add_term(lhs.coef, rhs.coef, subtract=True)
    const = _add_term(rhs.const, lhs.const, subtract=True)
    if not _is_nonzero(coef):
        if _is_nonzero(const):
            raise no_unique_solution("方程无解：未知量系数为零且两侧常数项不相等")
        raise no_unique_solution("方程有无穷多解：未知量系数为零（恒等式）")
    if const is None:
        return Fraction(0), None
    value = const.value_si / coef.value_si
    dim = tuple(c - k for c, k in zip(const.dim, coef.dim))
    return value, dim
