"""换算核心：量纲校验、仿射温标规则、精确十进制输出。

精确性策略：
- 内部一律使用 :class:`fractions.Fraction` 做精确有理数运算；
- 仅当结果能表示为不超过 ``MAX_PRECISION`` 位有效数字的有限十进制时
  才输出，否则判定为“精度溢出”并抛出 422 错误；
- 相同输入永远产生相同输出（纯函数、无随机性、无时间依赖）。
"""

from __future__ import annotations

from decimal import Context, Decimal, Inexact
from fractions import Fraction

from .config import MAX_PRECISION
from .errors import affine_mixing, dimension_mismatch, precision_overflow
from .parser import ParsedExpression, canonical_dim

#: 开尔文作为“中性”温度：既可与绝对温标换算，也可与温差单位换算。
_KELVIN_TERMS = (("K", 1),)


def is_absolute_temperature(expr: ParsedExpression) -> bool:
    """表达式是否为“绝对温度变量”的单位（裸 ``degC``/``degF``/``K``）。

    仿射温标只按规范项判定（其比例与偏移在 ``affine`` 字段中）；
    带词头的 K（如 ``mK``）与复合表达式不算——它们只能表示温差。
    """
    if expr.affine is not None:
        return expr.terms in ((("degC", 1),), (("degF", 1),))
    return expr.terms == _KELVIN_TERMS and expr.scale == 1


def is_absolute_target(expr: ParsedExpression) -> bool:
    """表达式是否为合法的绝对温度换算目标（仿射温标或裸 ``K``）。"""
    if expr.affine is not None:
        return True
    return expr.terms == _KELVIN_TERMS and expr.scale == 1


def _affine_params(expr: ParsedExpression, label: str) -> tuple[Fraction, Fraction]:
    """取表达式的仿射参数 (scale, offset)。

    仅允许两种情况：表达式本身是仿射温标（degC/degF），或恰好是 ``K``。
    其余（温差单位、带词头的 K、复合表达式）一律视为绝对/温差混算。
    """
    if expr.affine is not None:
        return expr.affine.scale, expr.affine.offset
    if expr.terms == _KELVIN_TERMS and expr.scale == 1:
        return Fraction(1), Fraction(0)
    raise affine_mixing(
        f"禁止温差与绝对温度混算: {label}表达式不是合法的绝对温标"
        "（绝对温度请使用 degC/degF/K，温差请使用 dC/dF/dK）"
    )


def convert(value: Decimal, src: ParsedExpression, dst: ParsedExpression) -> str:
    """把 ``value``（以 ``src`` 表示）换算为以 ``dst`` 表示的精确十进制字符串。"""
    return to_decimal_str(convert_fraction(Fraction(value), src, dst))


def convert_fraction(value: Fraction, src: ParsedExpression, dst: ParsedExpression) -> Fraction:
    """与 :func:`convert` 相同，但输入/输出均为精确分数（SI 基单位口径）。

    ``value`` 必须已经是 ``src`` 单位下的数值；仿射源单位会先加偏移。
    """
    if src.dim != dst.dim:
        raise dimension_mismatch(canonical_dim(src.dim), canonical_dim(dst.dim))

    if src.affine is not None or dst.affine is not None:
        s_scale, s_offset = _affine_params(src, "源")
        d_scale, d_offset = _affine_params(dst, "目标")
        base = value * s_scale + s_offset  # 换算到开尔文
        return (base - d_offset) / d_scale
    return value * src.scale / dst.scale


def to_decimal_str(fr: Fraction) -> str:
    """把精确分数渲染为有限十进制字符串；无法精确表示时抛出精度溢出。"""
    if fr == 0:
        return "0"
    ctx = Context(prec=MAX_PRECISION, traps=[Inexact])
    try:
        dec = ctx.divide(Decimal(fr.numerator), Decimal(fr.denominator))
    except Inexact:
        raise precision_overflow(
            f"换算结果不是有限十进制或超过 {MAX_PRECISION} 位有效数字的精度上限"
        ) from None
    return format(_strip_trailing_zeros(dec), "f")


def _strip_trailing_zeros(dec: Decimal) -> Decimal:
    """去掉小数末尾的零（不依赖上下文精度，保证规范化且确定）。"""
    sign, digits, exponent = dec.as_tuple()
    digit_list = list(digits)
    while len(digit_list) > 1 and digit_list[-1] == 0:
        digit_list.pop()
        exponent += 1
    return Decimal((sign, tuple(digit_list), exponent))
