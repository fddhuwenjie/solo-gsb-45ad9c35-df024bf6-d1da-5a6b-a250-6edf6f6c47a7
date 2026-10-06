"""公式求值器：在解析过程中以 :class:`Fraction` 合并数值与量纲。

支持的语法（递归下降）::

    expr   := term (("+" | "-") term)*
    term   := unary (("*" | "/") unary)*
    unary  := ("+" | "-") unary | factor
    factor := PRIMARY (("^" | "**") unary)?   # 幂右结合，指数可带一元负号
    PRIMARY:= "(" expr ")" | VARIABLE | DECIMAL

规则：
- 变量必须来自请求体的变量映射；十进制定值为无量纲精确分数。
- 只允许显式运算符，不允许隐式相乘（``2 x``、``x y``、``x (y)`` 均为语法错误）。
- 幂底数随意，幂指数必须是无量纲整数（|n| ≤ ``MAX_POWER``）。
- 加减仅允许同量纲项；不同量纲直接报量纲冲突。
- 除数为零报除零错误。
- 绝对温标变量（单位为裸 ``degC``/``degF``/``K``）只能作为公式的唯一项
  （可整体加括号），参与任何运算都会报仿射用法错误。

求值结果以 SI 基单位口径表示（仿射变量预先加偏移），因此量纲为非温度维度时
可以自由线性组合；温度维度下仅温差（dC/dF/dK、带词头 K 或复合温度单位）
可以运算，绝对温度不行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Mapping

from .config import MAX_POWER, MAX_PRECISION
from .converter import is_absolute_temperature
from .errors import (
    ConversionError,
    affine_mixing,
    dimension_mismatch,
    division_by_zero,
    invalid_power,
    invalid_syntax,
    precision_overflow,
    unknown_variable,
)
from .parser import ParsedExpression, canonical_dim
from .units import DIMLESS

#: 括号/一元符号的最大嵌套深度，超出视为语法错误（避免递归过深）。
MAX_NESTING = 100

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<pow>\*\*|\^)
    | (?P<mul>\*)
    | (?P<div>/)
    | (?P<add>[+])
    | (?P<sub>[-])
    | (?P<lpar>\()
    | (?P<rpar>\))
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<bad>.)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Quantity:
    """求值中的量：SI 基单位口径的精确数值、量纲向量与绝对温标标记。

    ``value_si`` 的含义：

    - ``absolute`` 为 True 时，值是绝对温度对应的开尔文（含仿射偏移）；
    - 否则值是普通线性量，已按单位比例折算到 SI 基单位
      （温度维度下即“温差 × 单位系数”）。

    仿射温标变量携带的偏移使其不能参与运算；一旦进入运算结果即线性化，
    ``absolute`` 恒为 False。
    """

    value_si: Fraction
    dim: tuple[int, ...]
    absolute: bool = False


def make_quantity(value: Decimal, unit: ParsedExpression) -> Quantity:
    """把变量的 (十进制定值, 单位) 折叠为 SI 口径的量。"""
    value_si = Fraction(value)
    if unit.affine is not None:
        # 绝对温度：预先加仿射偏移，值即开尔文
        value_si = value_si * unit.affine.scale + unit.affine.offset
    else:
        value_si *= unit.scale
    return Quantity(
        value_si=value_si,
        dim=unit.dim,
        absolute=is_absolute_temperature(unit),
    )


def _sum_dim(a: tuple[int, ...], b: tuple[int, ...], sign: int) -> tuple[int, ...]:
    return tuple(x + sign * y for x, y in zip(a, b))


class _Evaluator:
    def __init__(self, text: str, variables: Mapping[str, Quantity]) -> None:
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
    def parse(self) -> Quantity:
        if not self._tokens:
            raise invalid_syntax("公式为空")
        result = self._expr()
        if self._peek() is not None:
            raise invalid_syntax(
                f"不允许隐式相乘或存在多余输入: {self._here()!r}"
            )
        return result

    def _expr(self) -> Quantity:
        left = self._term()
        while self._peek() in ("add", "sub"):
            op, _ = self._next()
            right = self._term()
            left = self._add(left, right, subtract=(op == "sub"))
        return left

    def _term(self) -> Quantity:
        left = self._unary()
        while self._peek() in ("mul", "div"):
            op, _ = self._next()
            right = self._unary()
            if op == "mul":
                left = self._multiply(left, right)
            else:
                left = self._divide(left, right)
        return left

    def _unary(self) -> Quantity:
        # 一元正负号位于乘除层与幂层之间：-x^2 解析为 -(x^2)
        if self._peek() in ("add", "sub"):
            op, _ = self._next()
            self._enter()
            try:
                operand = self._unary()
            finally:
                self._leave()
            self._reject_absolute(operand, "不能对绝对温度使用一元正负号")
            if op == "sub":
                operand = Quantity(-operand.value_si, operand.dim)
            return operand
        return self._factor()

    def _factor(self) -> Quantity:
        base = self._primary()
        if self._peek() == "pow":
            self._next()
            exponent = self._unary()  # 指数位置允许一元负号，如 x^-2
            base = self._power(base, exponent)
        return base

    def _primary(self) -> Quantity:
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
            if text not in self._vars:
                raise unknown_variable(text)
            return self._vars[text]
        if kind == "num":
            _, text = self._next()
            dec = Decimal(text)
            if len(dec.as_tuple().digits) > MAX_PRECISION:
                raise precision_overflow(
                    f"十进制定值有效数字超过 {MAX_PRECISION} 位的精度上限"
                )
            return Quantity(Fraction(dec), DIMLESS)
        raise invalid_syntax(f"语法错误: 此处不应出现 {self._here()!r}")

    # -- 运算（量纲在合并时同步计算）--------------------------------------
    @staticmethod
    def _reject_absolute(q: Quantity, message: str) -> None:
        if q.absolute:
            raise affine_mixing(
                "绝对温标变量（单位为 degC/degF/K）只能作为公式的唯一项，"
                "不能参与任何运算: " + message
            )

    def _add(self, a: Quantity, b: Quantity, *, subtract: bool) -> Quantity:
        self._reject_absolute(a, "绝对温度不能加减")
        self._reject_absolute(b, "绝对温度不能加减")
        if a.dim != b.dim:
            raise dimension_mismatch(canonical_dim(a.dim), canonical_dim(b.dim))
        value = a.value_si - b.value_si if subtract else a.value_si + b.value_si
        return Quantity(value, a.dim)

    def _multiply(self, a: Quantity, b: Quantity) -> Quantity:
        self._reject_absolute(a, "绝对温度不能相乘")
        self._reject_absolute(b, "绝对温度不能相乘")
        return Quantity(a.value_si * b.value_si, _sum_dim(a.dim, b.dim, 1))

    def _divide(self, a: Quantity, b: Quantity) -> Quantity:
        self._reject_absolute(a, "绝对温度不能相除")
        self._reject_absolute(b, "绝对温度不能相除")
        if b.value_si == 0:
            raise division_by_zero()
        return Quantity(a.value_si / b.value_si, _sum_dim(a.dim, b.dim, -1))

    def _power(self, base: Quantity, exponent: Quantity) -> Quantity:
        self._reject_absolute(base, "绝对温度不能取幂")
        if exponent.dim != DIMLESS:
            raise invalid_power("幂指数必须是无量纲整数")
        exp = exponent.value_si
        if exp.denominator != 1:
            raise invalid_power("幂指数必须是整数")
        n = exp.numerator
        if abs(n) > MAX_POWER:
            raise invalid_power(f"幂指数绝对值不能超过 {MAX_POWER}")
        if n < 0 and base.value_si == 0:
            raise division_by_zero()
        return Quantity(base.value_si**n, tuple(x * n for x in base.dim))


def evaluate(formula: str, variables: Mapping[str, Quantity]) -> Quantity:
    """求值公式；任何非法输入都抛出 :class:`ConversionError`（对应 422）。"""
    try:
        return _Evaluator(formula, variables).parse()
    except ConversionError:
        raise
    except ZeroDivisionError:
        # Fraction 在极端组合下仍可能直接抛出（如 0 的负幂被先行拦截外的情形）
        raise division_by_zero() from None
