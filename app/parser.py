"""单位表达式解析器。

支持的语法（递归下降）::

    expr    := term (("*" | "/") term)*
    term    := factor (("^" | "**") exponent)?
    factor  := "(" expr ")" | SYMBOL | "1"
    exponent:= ["("] ["+" | "-"] INTEGER [")"]

规则：
- 仅支持乘、除与整数幂；不允许隐式相乘（``kgm`` 视为未知符号）。
- 表达式中只允许出现数字 ``1``（表示无量纲）。
- 幂指数必须为整数，且绝对值不超过 ``MAX_POWER``。
- 仿射（绝对）温标 ``degC``/``degF`` 必须单独出现，不得参与乘除或取幂。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

from .config import MAX_POWER
from .errors import (
    ConversionError,
    affine_mixing,
    invalid_power,
    invalid_syntax,
    unknown_symbol,
)
from .units import BASE_SYMBOLS, DIMLESS, ResolvedUnit, resolve

if TYPE_CHECKING:
    from .custom_units import UnitScope

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<pow>\*\*|\^)
    | (?P<mul>\*)
    | (?P<div>/)
    | (?P<lpar>\()
    | (?P<rpar>\))
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<sym>[A-Za-zµμΩ°][A-Za-z0-9µμΩ°_]*)
    | (?P<sign>[+-])
    | (?P<bad>.)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class ParsedExpression:
    """解析后的单位表达式。

    - ``dim``: 量纲向量
    - ``scale``: 相对 SI 基单位的精确线性比例
    - ``terms``: (规范符号, 指数) 序列，用于规范化输出
    - ``affine``: 若整个表达式恰为单一仿射温标，则为该单位，否则为 None
    """

    dim: tuple[int, ...]
    scale: Fraction
    terms: tuple[tuple[str, int], ...]
    affine: ResolvedUnit | None = None


_DIMENSIONLESS = ParsedExpression(DIMLESS, Fraction(1), ())


def _combine(a: ParsedExpression, b: ParsedExpression, divide: bool) -> ParsedExpression:
    if a.affine is not None or b.affine is not None:
        raise affine_mixing(
            "绝对温标（degC/degF）必须单独出现，不能与其他单位组合或相除"
        )
    sign = -1 if divide else 1
    dim = tuple(x + sign * y for x, y in zip(a.dim, b.dim))
    scale = a.scale / b.scale if divide else a.scale * b.scale
    merged: dict[str, int] = dict(a.terms)
    for sym, exp in b.terms:
        merged[sym] = merged.get(sym, 0) + sign * exp
        if merged[sym] == 0:
            del merged[sym]
    return ParsedExpression(dim, scale, tuple(merged.items()))


def _power(expr: ParsedExpression, n: int) -> ParsedExpression:
    if expr.affine is not None:
        if n == 1:
            return expr
        raise affine_mixing("绝对温标（degC/degF）不能取幂")
    return ParsedExpression(
        dim=tuple(x * n for x in expr.dim),
        scale=expr.scale**n,
        terms=tuple((sym, exp * n) for sym, exp in expr.terms),
    )


def _from_unit(unit: ResolvedUnit) -> ParsedExpression:
    return ParsedExpression(
        dim=unit.dim,
        scale=unit.scale,
        terms=((unit.symbol, 1),),
        affine=unit if unit.affine else None,
    )


class _Parser:
    def __init__(self, text: str, scope: UnitScope | None = None) -> None:
        self._scope = scope
        self._tokens: list[tuple[str, str]] = []
        for m in _TOKEN_RE.finditer(text):
            kind = m.lastgroup or "bad"
            if kind == "ws":
                continue
            if kind == "bad":
                raise invalid_syntax(f"无法识别的字符: {m.group()!r}")
            self._tokens.append((kind, m.group()))
        self._pos = 0

    # -- 词法辅助 ---------------------------------------------------------
    def _peek(self) -> str | None:
        return self._tokens[self._pos][0] if self._pos < len(self._tokens) else None

    def _next(self) -> tuple[str, str]:
        if self._pos >= len(self._tokens):
            raise invalid_syntax("表达式意外结束")
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, kind: str) -> tuple[str, str]:
        if self._peek() != kind:
            got = self._tokens[self._pos][1] if self._pos < len(self._tokens) else "<结尾>"
            raise invalid_syntax(f"语法错误: 期望 {kind}, 实际为 {got!r}")
        return self._next()

    # -- 语法 -------------------------------------------------------------
    def parse(self) -> ParsedExpression:
        if not self._tokens:
            raise invalid_syntax("表达式为空")
        expr = self._expr()
        if self._peek() is not None:
            raise invalid_syntax(f"无法解析的剩余输入: {self._tokens[self._pos][1]!r}")
        return expr

    def _expr(self) -> ParsedExpression:
        left = self._term()
        while self._peek() in ("mul", "div"):
            op, _ = self._next()
            right = self._term()
            left = _combine(left, right, divide=(op == "div"))
        return left

    def _term(self) -> ParsedExpression:
        base = self._factor()
        if self._peek() == "pow":
            self._next()
            base = _power(base, self._exponent())
        return base

    def _factor(self) -> ParsedExpression:
        kind = self._peek()
        if kind == "lpar":
            self._next()
            expr = self._expr()
            self._expect("rpar")
            return expr
        if kind == "num":
            _, text = self._next()
            if Decimal(text) != 1:
                raise invalid_syntax("表达式中只允许出现数字 1（表示无量纲）")
            return _DIMENSIONLESS
        if kind == "sym":
            _, text = self._next()
            # 存在请求级自定义单位时按该组定义解析，否则仅查全局注册表
            resolver = self._scope.resolve_symbol if self._scope is not None else resolve
            unit = resolver(text)
            if unit is None:
                raise unknown_symbol(text)
            return _from_unit(unit)
        got = self._tokens[self._pos][1] if self._pos < len(self._tokens) else "<结尾>"
        raise invalid_syntax(f"语法错误: 此处不应出现 {got!r}")

    def _exponent(self) -> int:
        parens = self._peek() == "lpar"
        if parens:
            self._next()
        sign = 1
        if self._peek() == "sign":
            _, text = self._next()
            sign = -1 if text == "-" else 1
        if self._peek() != "num":
            raise invalid_power("幂指数必须是整数")
        _, text = self._next()
        if "." in text:
            raise invalid_power(f"幂指数必须是整数, 不能是小数: {text!r}")
        if parens:
            self._expect("rpar")
        n = sign * int(text)
        if abs(n) > MAX_POWER:
            raise invalid_power(f"幂指数绝对值不能超过 {MAX_POWER}")
        return n


def parse_expression(text: str, scope: UnitScope | None = None) -> ParsedExpression:
    """解析单位表达式；任何非法输入都抛出 :class:`ConversionError`。

    ``scope`` 为请求级自定义单位上下文（见 :mod:`app.custom_units`）；
    缺省时仅按全局注册表解析，行为与无 ``custom_units`` 的请求完全一致。
    """

    return _Parser(text, scope).parse()


# ---------------------------------------------------------------------------
# 规范化输出
# ---------------------------------------------------------------------------


def _fmt_term(symbol: str, exp: int) -> str:
    return symbol if exp == 1 else f"{symbol}^{exp}"


def canonical(terms: tuple[tuple[str, int], ...]) -> str:
    """把 (符号, 指数) 序列渲染为规范表达式。

    正指数项按符号排序以 ``*`` 连接，负指数项依次以 ``/`` 连接，
    结果可被本解析器重新解析（往返一致）。无量纲渲染为 ``"1"``。
    """
    pos = sorted((s, e) for s, e in terms if e > 0)
    neg = sorted((s, e) for s, e in terms if e < 0)
    head = "*".join(_fmt_term(s, e) for s, e in pos) if pos else "1"
    tail = "".join("/" + _fmt_term(s, -e) for s, e in neg)
    return head + tail


def canonical_dim(dim: tuple[int, ...]) -> str:
    """把量纲向量渲染为规范量纲表达式。

    基本单位按固定 SI 顺序 (kg, m, s, A, K, mol, cd) 输出，
    正指数在前、负指数以 ``/`` 依次连接。无量纲渲染为 ``"1"``。
    """
    pos = [(s, e) for s, e in zip(BASE_SYMBOLS, dim) if e > 0]
    neg = [(s, e) for s, e in zip(BASE_SYMBOLS, dim) if e < 0]
    head = "*".join(_fmt_term(s, e) for s, e in pos) if pos else "1"
    tail = "".join("/" + _fmt_term(s, -e) for s, e in neg)
    return head + tail
