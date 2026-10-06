"""请求级自定义线性单位（``custom_units``）。

每个请求可携带 1～6 项自定义单位声明，每项给出 ``symbol``、正十进制
``factor`` 与 ``base_unit`` 表达式，含义为 ``1 symbol = factor 个 base_unit``。

约束：

- 自定义符号不得与内置单位、别名或任何可由 SI 词头解析的符号冲突
  （如 ``m``、``kg``、``km``、``l``、``°C`` 均不可声明），也不自动获得词头
  （``kfoo`` 不会被解析为 ``k*foo``）；
- ``base_unit`` 可引用**本次请求**声明的其他自定义单位（允许前向引用，
  按名称解析、与声明顺序无关），但不得引用 ``degC``/``degF`` 或裸 ``K``
  （温度维度请使用 ``dC``/``dF``/``dK`` 或带词头的 ``K``，如 ``mK``）；
- 重名、循环引用、未知引用或非法比例均使整次请求返回 422。

声明仅在本次请求内有效：:class:`UnitScope` 是纯请求级对象，不修改全局
单位注册表；不同请求（含并发）可对同名符号给出不同定义而互不影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

from .errors import ConversionError
from .parser import _TOKEN_RE, parse_expression
from .units import ResolvedUnit, resolve

#: 自定义符号的词法：与单位表达式中的 SYMBOL 词法一致。
_SYMBOL_RE = re.compile(r"^[A-Za-zµμΩ°][A-Za-z0-9µμΩ°_]{0,31}$")

#: base_unit 中禁止直接引用的符号（仿射温标与裸开尔文）。
_FORBIDDEN_BASE_SYMBOLS = frozenset({"degC", "degF", "°C", "°F", "K"})


def invalid_custom_unit(message: str) -> ConversionError:
    """自定义单位声明非法：统一归入 ``invalid_request``（HTTP 422）。"""

    return ConversionError("invalid_request", message)


@dataclass(frozen=True)
class CustomUnitSpec:
    """单条自定义单位声明（factor 已由 schema 层保证为正有限十进制）。"""

    symbol: str
    factor: Fraction  # 1 symbol = factor 个 base_unit
    base_unit: str


class UnitScope:
    """一次请求的单位解析上下文：内置注册表（只读）+ 本次声明的自定义单位。"""

    def __init__(self, items: Iterable[CustomUnitSpec]) -> None:
        self._specs: dict[str, CustomUnitSpec] = {}
        for spec in items:
            if not _SYMBOL_RE.match(spec.symbol):
                raise invalid_custom_unit(
                    f"自定义单位符号 {spec.symbol!r} 不合法：须以字母开头，"
                    "仅含字母、数字、下划线（长度不超过 32）"
                )
            # 与内置单位、别名、可由 SI 词头解析的符号冲突一律拒绝
            if resolve(spec.symbol) is not None:
                raise invalid_custom_unit(
                    f"自定义单位符号 {spec.symbol!r} 与内置单位、别名或可由 "
                    "SI 词头解析的符号冲突，且自定义符号不会自动获得词头"
                )
            if spec.symbol in self._specs:
                raise invalid_custom_unit(
                    f"自定义单位符号 {spec.symbol!r} 在同一次请求中重复声明"
                )
            self._specs[spec.symbol] = spec
        #: 已解析的自定义符号 -> 完全展开（量纲/比例）的线性单位
        self._resolved: dict[str, ResolvedUnit] = {}
        #: 正在解析中的自定义符号链（循环引用检测）
        self._visiting: set[str] = set()
        # 立即解析全部声明：即使某符号未被单位表达式使用，其自身定义中的
        # 循环/未知引用/禁引温标也必须使整次请求失败
        for symbol in self._specs:
            self._resolve(symbol)

    @property
    def symbols(self) -> tuple[str, ...]:
        """本次声明的全部自定义符号（按声明顺序）。"""

        return tuple(self._specs)

    def resolve_symbol(self, text: str) -> ResolvedUnit | None:
        """把符号解析为单位；无法解析时返回 ``None``。

        自定义符号优先（精确匹配，不做词头组合），其余回退到内置解析。
        构造期间允许重入（前向引用），由 :meth:`_resolve` 完成递归解析。
        """

        if text in self._specs:
            return self._resolved[text] if text in self._resolved else self._resolve(text)
        return resolve(text)

    # -- 定义解析 ---------------------------------------------------------

    def _resolve(self, symbol: str) -> ResolvedUnit:
        cached = self._resolved.get(symbol)
        if cached is not None:
            return cached
        if symbol in self._visiting:
            raise invalid_custom_unit(
                f"自定义单位定义存在循环引用：{symbol!r} 直接或间接引用了自身"
            )
        spec = self._specs[symbol]
        self._check_forbidden_base_symbols(spec.base_unit)
        self._visiting.add(symbol)
        try:
            # base_unit 与源/目标/变量单位使用同一组定义解析
            expr = parse_expression(spec.base_unit, self)
        finally:
            self._visiting.discard(symbol)
        # 自定义单位一律为纯线性单位：展开为 SI 口径的量纲与比例，
        # terms 中仅保留自定义符号本身（由调用方的解析器负责记录）
        resolved = ResolvedUnit(
            symbol=symbol,
            dim=expr.dim,
            scale=expr.scale * spec.factor,
            offset=Fraction(0),
            affine=False,
        )
        self._resolved[symbol] = resolved
        return resolved

    @staticmethod
    def _check_forbidden_base_symbols(base_unit: str) -> None:
        """base_unit 不得直接引用 degC/degF（含别名）或裸 K。"""

        for m in _TOKEN_RE.finditer(base_unit):
            if m.lastgroup == "sym" and m.group() in _FORBIDDEN_BASE_SYMBOLS:
                raise invalid_custom_unit(
                    "自定义单位的 base_unit 不得引用 degC、degF 或裸 K"
                    f"（发现 {m.group()!r}）；温度维度请使用 dC/dF/dK "
                    "或带词头的 K（如 mK）"
                )


def build_scope(items: Iterable[CustomUnitSpec] | None) -> UnitScope | None:
    """由请求体的 ``custom_units`` 构造请求级上下文；缺省/空时返回 ``None``。

    返回 ``None`` 时三个接口走原有解析路径，行为与响应字段完全不变。
    """

    if not items:
        return None
    specs = [
        CustomUnitSpec(symbol=item.symbol, factor=Fraction(item.factor), base_unit=item.base_unit)
        for item in items
    ]
    return UnitScope(specs)


def scope_for_request(
    custom_units: Iterable[CustomUnitSpec] | None,
    custom_unit_set_id: str | None = None,
    custom_unit_set_alias: str | None = None,
    *,
    store: "UnitSetStore | None" = None,
) -> UnitScope | None:
    """按请求选择自定义单位来源并构造请求级上下文。

    - 携带请求内 ``custom_units``：按原有规则就地构造（仅本次请求有效）；
    - 携带 ``custom_unit_set_id``：加载对应**已发布**定义集；标识不存在时
      抛出 422；
    - 携带 ``custom_unit_set_alias``：在请求开始时**解析一次**活动别名当前
      指向的定义集，随后整个请求（源、目标及变量单位）都使用该份定义；
      别名不存在时抛出 422。别名在请求处理中即使被切换，本次请求仍完整
      使用解析时的旧版或新版，不会跨版本混用；
    - 三者都缺省：返回 ``None``，三个接口保持原有行为。

    三种来源两两互斥，同时出现由 schema 层
    （:class:`~app.schemas.CustomUnitsSelectionModel`）先行拒绝为 422。
    每次调用都返回独立的 :class:`UnitScope`，不同请求（含并发）互不污染。
    """

    if custom_units is not None:
        return build_scope(custom_units)
    if custom_unit_set_id is not None or custom_unit_set_alias is not None:
        from .unit_sets import UnitSetStore, get_store

        chosen: UnitSetStore = store or get_store()
        if custom_unit_set_alias is not None:
            return chosen.build_scope_for_alias(custom_unit_set_alias)
        return chosen.build_scope(custom_unit_set_id)
    return None
