"""单位注册表：SI 基本/导出单位、SI 词头、仿射温标与温差单位。

内部所有比例系数均使用 :class:`fractions.Fraction` 精确表示，
保证换算过程无任何二进制浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# ---------------------------------------------------------------------------
# 量纲向量
# ---------------------------------------------------------------------------
# 顺序: 质量, 长度, 时间, 电流, 热力学温度, 物质的量, 发光强度
BASE_SYMBOLS: tuple[str, ...] = ("kg", "m", "s", "A", "K", "mol", "cd")
DIMLESS: tuple[int, ...] = (0, 0, 0, 0, 0, 0, 0)


def _dim(
    mass: int = 0,
    length: int = 0,
    time: int = 0,
    current: int = 0,
    temp: int = 0,
    amount: int = 0,
    lum: int = 0,
) -> tuple[int, ...]:
    return (mass, length, time, current, temp, amount, lum)


# ---------------------------------------------------------------------------
# 单位定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """注册表中的单位条目。"""

    symbol: str  # 规范符号（用于规范化输出）
    name: str  # 中文名称
    dim: tuple[int, ...]  # 量纲向量
    scale: Fraction  # 相对 SI 基单位的线性比例: base = scale * value (+ offset)
    offset: Fraction = Fraction(0)  # 仿射偏移（仅绝对温标非零）
    affine: bool = False  # 是否为仿射（绝对）温标
    prefixable: bool = False  # 是否允许加 SI 词头


def _defs() -> list[Unit]:
    F = Fraction
    return [
        # --- SI 基本单位 -------------------------------------------------
        Unit("m", "米", _dim(length=1), F(1), prefixable=True),
        Unit("g", "克", _dim(mass=1), F(1, 1000), prefixable=True),
        Unit("s", "秒", _dim(time=1), F(1), prefixable=True),
        Unit("A", "安培", _dim(current=1), F(1), prefixable=True),
        Unit("K", "开尔文", _dim(temp=1), F(1), prefixable=True),
        Unit("mol", "摩尔", _dim(amount=1), F(1), prefixable=True),
        Unit("cd", "坎德拉", _dim(lum=1), F(1), prefixable=True),
        # --- 无量纲 ------------------------------------------------------
        Unit("rad", "弧度", _dim(), F(1), prefixable=True),
        # --- SI 导出单位 -------------------------------------------------
        Unit("Hz", "赫兹", _dim(time=-1), F(1), prefixable=True),
        Unit("N", "牛顿", _dim(mass=1, length=1, time=-2), F(1), prefixable=True),
        Unit("Pa", "帕斯卡", _dim(mass=1, length=-1, time=-2), F(1), prefixable=True),
        Unit("J", "焦耳", _dim(mass=1, length=2, time=-2), F(1), prefixable=True),
        Unit("W", "瓦特", _dim(mass=1, length=2, time=-3), F(1), prefixable=True),
        Unit("C", "库仑", _dim(time=1, current=1), F(1), prefixable=True),
        Unit("V", "伏特", _dim(mass=1, length=2, time=-3, current=-1), F(1), prefixable=True),
        Unit("F", "法拉", _dim(mass=-1, length=-2, time=4, current=2), F(1), prefixable=True),
        Unit("ohm", "欧姆", _dim(mass=1, length=2, time=-3, current=-2), F(1), prefixable=True),
        Unit("S", "西门子", _dim(mass=-1, length=-2, time=3, current=2), F(1), prefixable=True),
        Unit("Wb", "韦伯", _dim(mass=1, length=2, time=-2, current=-1), F(1), prefixable=True),
        Unit("T", "特斯拉", _dim(mass=1, time=-2, current=-1), F(1), prefixable=True),
        Unit("H", "亨利", _dim(mass=1, length=2, time=-2, current=-2), F(1), prefixable=True),
        Unit("lm", "流明", _dim(lum=1), F(1), prefixable=True),
        Unit("lx", "勒克斯", _dim(lum=1, length=-2), F(1), prefixable=True),
        Unit("Bq", "贝可勒尔", _dim(time=-1), F(1), prefixable=True),
        Unit("Gy", "戈瑞", _dim(length=2, time=-2), F(1), prefixable=True),
        Unit("Sv", "希沃特", _dim(length=2, time=-2), F(1), prefixable=True),
        Unit("kat", "卡塔尔", _dim(time=-1, amount=1), F(1), prefixable=True),
        # --- 常用非 SI 单位（精确十进制比例） -----------------------------
        Unit("min", "分钟", _dim(time=1), F(60)),
        Unit("h", "小时", _dim(time=1), F(3600)),
        Unit("d", "天", _dim(time=1), F(86400)),
        Unit("L", "升", _dim(length=3), F(1, 1000), prefixable=True),
        # --- 仿射（绝对）温标 ---------------------------------------------
        # degC: K = 1 * x + 273.15
        Unit("degC", "摄氏度", _dim(temp=1), F(1), offset=F(27315, 100), affine=True),
        # degF: K = 5/9 * x + (273.15 - 32*5/9) = 5/9 * x + 45967/180
        Unit("degF", "华氏度", _dim(temp=1), F(5, 9), offset=F(45967, 180), affine=True),
        # --- 温差单位（纯线性，禁止与绝对温度混算） ------------------------
        Unit("dK", "开尔文温差", _dim(temp=1), F(1)),
        Unit("dC", "摄氏度温差", _dim(temp=1), F(1)),
        Unit("dF", "华氏度温差", _dim(temp=1), F(5, 9)),
    ]


REGISTRY: dict[str, Unit] = {u.symbol: u for u in _defs()}

#: 符号别名 -> 注册表规范符号
ALIASES: dict[str, str] = {
    "°C": "degC",
    "°F": "degF",
    "Ω": "ohm",
    "l": "L",
    "ℓ": "L",
}

#: SI 词头: 符号 -> 10 的幂次（da 为两字符，匹配时按长度降序优先）
_PREFIX_EXP: dict[str, int] = {
    "Y": 24, "Z": 21, "E": 18, "P": 15, "T": 12, "G": 9, "M": 6, "k": 3,
    "h": 2, "da": 1, "d": -1, "c": -2, "m": -3, "u": -6, "µ": -6, "μ": -6,
    "n": -9, "p": -12, "f": -15, "a": -18, "z": -21, "y": -24,
}
PREFIXES: dict[str, Fraction] = {s: Fraction(10) ** e for s, e in _PREFIX_EXP.items()}
_PREFIXES_BY_LEN = sorted(_PREFIX_EXP, key=len, reverse=True)


@dataclass(frozen=True)
class ResolvedUnit:
    """符号解析结果（可能带词头）。"""

    symbol: str  # 规范显示符号（如 "km"、"degC"）
    dim: tuple[int, ...]
    scale: Fraction  # 已含词头因子
    offset: Fraction
    affine: bool


def resolve(symbol: str) -> ResolvedUnit | None:
    """把符号解析为单位；无法解析时返回 ``None``。

    先精确匹配（含别名），再尝试“SI 词头 + 可加词头单位”的组合，
    词头按长度降序匹配（如 ``da`` 优先于 ``d``）。
    """

    def _wrap(unit: Unit, factor: Fraction, display: str) -> ResolvedUnit:
        return ResolvedUnit(
            symbol=display,
            dim=unit.dim,
            scale=unit.scale * factor,
            offset=unit.offset,
            affine=unit.affine,
        )

    name = ALIASES.get(symbol, symbol)
    unit = REGISTRY.get(name)
    if unit is not None:
        return _wrap(unit, Fraction(1), unit.symbol)

    for prefix in _PREFIXES_BY_LEN:
        if not name.startswith(prefix) or len(name) == len(prefix):
            continue
        rest = ALIASES.get(name[len(prefix):], name[len(prefix):])
        base = REGISTRY.get(rest)
        if base is not None and base.prefixable:
            return _wrap(base, PREFIXES[prefix], prefix + base.symbol)
    return None
