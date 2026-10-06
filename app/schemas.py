"""Pydantic 请求/响应模型。"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import MAX_PRECISION


def _coerce_value(v: Any) -> Any:
    """JSON -> Decimal 预处理：拒绝布尔，float 取最短十进制表示避免二进制污染。"""
    if isinstance(v, bool):
        raise ValueError("value 必须是十进制数")
    if isinstance(v, float):
        v = repr(v)
    return v


def _check_value(d: Decimal) -> Decimal:
    if not d.is_finite():
        raise ValueError("value 必须为有限十进制数")
    if len(d.as_tuple().digits) > MAX_PRECISION:
        raise ValueError(f"value 有效数字超过 {MAX_PRECISION} 位的精度上限")
    return d


class CustomUnitItem(BaseModel):
    """请求级自定义线性单位声明：``1 symbol = factor 个 base_unit``。"""

    symbol: str = Field(min_length=1, max_length=32, description="自定义单位符号")
    factor: Decimal = Field(
        gt=0, description="正十进制比例：1 symbol = factor 个 base_unit"
    )
    base_unit: str = Field(
        min_length=1,
        max_length=200,
        description="基准单位表达式（可引用本次声明的其他自定义单位；"
        "不得引用 degC、degF 或裸 K）",
    )

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("factor", mode="before")
    @classmethod
    def _coerce_factor(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("factor")
    @classmethod
    def _check_factor(cls, d: Decimal) -> Decimal:
        return _check_value(d)


#: 三个换算接口均可携带的请求级自定义单位声明（1～6 项）。
CustomUnits = list[CustomUnitItem]


#: 活动别名（custom unit set alias）的词法：与 unit_sets.ALIAS_NAME_RE 一致。
ALIAS_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class CustomUnitsSelectionModel(BaseModel):
    """三个换算接口共用的自定义单位来源选择。

    自定义单位可以来自请求内的 ``custom_units``（仅本次请求有效）、已发布的
    定义集标识 ``custom_unit_set_id``，或活动别名 ``custom_unit_set_alias``
    （请求开始时解析一次，解析到哪一版就整次请求使用哪一版）。三者两两互斥，
    同时出现整次返回 422；都缺省时三个接口保持原有行为。
    """

    model_config = ConfigDict(populate_by_name=True)

    custom_units: CustomUnits | None = Field(
        default=None,
        min_length=1,
        max_length=6,
        description="请求级自定义线性单位声明（1～6 项），仅在本次请求内有效；"
        "与 custom_unit_set_id、custom_unit_set_alias 互斥",
    )
    custom_unit_set_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="已发布自定义单位定义集的服务端标识；与 custom_units、"
        "custom_unit_set_alias 互斥，标识不存在时整次请求返回 422",
    )
    custom_unit_set_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=ALIAS_NAME_PATTERN,
        description="已发布定义集的活动别名；请求开始时解析一次，源、目标及"
        "变量单位始终使用解析到的该版定义；与 custom_units、"
        "custom_unit_set_id 互斥，别名不存在时整次请求返回 422",
    )

    @model_validator(mode="after")
    def _check_custom_units_sources_exclusive(self) -> "CustomUnitsSelectionModel":
        chosen = [
            name
            for name in (
                "custom_units",
                "custom_unit_set_id",
                "custom_unit_set_alias",
            )
            if getattr(self, name) is not None
        ]
        if len(chosen) > 1:
            raise ValueError(
                "、".join(chosen)
                + " 不能同时出现：一次调用只能选用一种自定义单位来源"
            )
        return self


class ConvertRequest(CustomUnitsSelectionModel):
    """POST /convert 请求体。

    ``value`` 建议以 JSON 字符串传递以保证十进制精确性；
    JSON 数值也会被接受（按其最短十进制表示解释）。
    """

    value: Decimal = Field(description="十进制定值")
    from_unit: str = Field(alias="from", min_length=1, max_length=200, description="源单位表达式")
    to_unit: str = Field(alias="to", min_length=1, max_length=200, description="目标单位表达式")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("value")
    @classmethod
    def _check_value(cls, d: Decimal) -> Decimal:
        return _check_value(d)


class Variable(BaseModel):
    """POST /evaluate 中的单个变量：带 ``value`` 与 ``unit`` 的映射值。"""

    value: Decimal = Field(description="变量的十进制定值")
    unit: str = Field(min_length=1, max_length=200, description="变量单位表达式")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("value")
    @classmethod
    def _check_value(cls, d: Decimal) -> Decimal:
        return _check_value(d)


_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")


class EvaluateRequest(CustomUnitsSelectionModel):
    """POST /evaluate 请求体。"""

    variables: dict[str, Variable] = Field(
        min_length=1, max_length=20, description="变量映射（1～20 个），键为公式中的变量名"
    )
    formula: str = Field(min_length=1, max_length=500, description="求值公式")
    output_unit: str = Field(min_length=1, max_length=200, description="结果目标单位表达式")

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, variables: dict[str, Variable]) -> dict[str, Variable]:
        for name in variables:
            if not _VAR_NAME_RE.match(name):
                raise ValueError(
                    f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                    "仅含字母、数字、下划线，长度不超过 32"
                )
        return variables


class SolveRequest(CustomUnitsSelectionModel):
    """POST /solve 请求体。"""

    variables: dict[str, Variable] = Field(
        default_factory=dict,
        max_length=20,
        description="变量映射（0～20 个，可省略），键为方程中的变量名；不得包含 solve_for",
    )
    equation: str = Field(
        min_length=1, max_length=500, description="含恰好一个等号的方程"
    )
    solve_for: str = Field(min_length=1, max_length=32, description="待解未知量的名称")
    output_unit: str = Field(
        min_length=1, max_length=200, description="结果目标单位表达式（须为线性单位）"
    )

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, variables: dict[str, Variable]) -> dict[str, Variable]:
        for name in variables:
            if not _VAR_NAME_RE.match(name):
                raise ValueError(
                    f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                    "仅含字母、数字、下划线，长度不超过 32"
                )
        return variables

    @field_validator("equation")
    @classmethod
    def _check_single_equals(cls, equation: str) -> str:
        if equation.count("=") != 1:
            raise ValueError("equation 必须恰好包含一个等号 '='")
        return equation

    @field_validator("solve_for")
    @classmethod
    def _check_solve_for_name(cls, name: str) -> str:
        if not _VAR_NAME_RE.match(name):
            raise ValueError(
                f"未知量名称 {name!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        return name

    @model_validator(mode="after")
    def _check_solve_for_not_in_variables(self) -> "SolveRequest":
        if self.solve_for in self.variables:
            raise ValueError(f"solve_for {self.solve_for!r} 不得出现在 variables 中")
        return self


class WorksheetStep(BaseModel):
    """``POST /worksheet`` 中的单个步骤。

    两种步骤恰好出现一种：``formula`` 走公式求值，``equation`` + ``solve_for``
    走一次线性方程反求。两种参数混给或缺省都使整次请求返回 422。
    后续步骤可按 ``name`` 引用本步骤结果；``name`` 在整个工作单中唯一，
    且不得与初始变量重名。
    """

    name: str = Field(
        min_length=1,
        max_length=32,
        description="本步骤结果名：唯一，后续步骤按此名引用，不得覆盖已有变量",
    )
    output_unit: str = Field(
        min_length=1, max_length=200, description="结果目标单位表达式"
    )
    formula: str | None = Field(
        default=None, min_length=1, max_length=500, description="求值公式（求值步骤）"
    )
    equation: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="含恰好一个等号的方程（反求步骤）",
    )
    solve_for: str | None = Field(
        default=None, min_length=1, max_length=32, description="待解未知量名称（反求步骤）"
    )

    @model_validator(mode="after")
    def _check_exactly_one_step_kind(self) -> "WorksheetStep":
        is_evaluate = self.formula is not None
        is_solve = self.equation is not None or self.solve_for is not None
        if is_evaluate and is_solve:
            raise ValueError(
                "formula 与 equation/solve_for 不能同时出现：每个步骤只能是"
                "公式求值或一次方程反求"
            )
        if not is_evaluate and not is_solve:
            raise ValueError(
                "每个步骤必须提供 formula（求值）或 equation 与 solve_for（反求）"
            )
        if is_solve and (self.equation is None or self.solve_for is None):
            raise ValueError("反求步骤必须同时提供 equation 与 solve_for")
        if not _VAR_NAME_RE.match(self.name):
            raise ValueError(
                f"结果名 {self.name!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        if is_solve and not _VAR_NAME_RE.match(self.solve_for):
            raise ValueError(
                f"未知量名称 {self.solve_for!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        if is_solve and self.equation.count("=") != 1:
            raise ValueError("equation 必须恰好包含一个等号 '='")
        return self


class WorksheetBody(BaseModel):
    """工作单本体：初始变量 + 2～5 个顺序执行的步骤（不含单位来源选择）。

    供 ``POST /worksheet`` 与 ``POST /worksheet-diagnose`` 共用：
    诊断请求两侧的单位来源固定由旧/新定义集引用决定，因此不继承
    :class:`CustomUnitsSelectionModel`，而步骤与变量的全部校验保持一致。
    """

    variables: dict[str, Variable] = Field(
        default_factory=dict,
        max_length=20,
        description="初始带单位变量映射（0～20 个，可省略）；步骤结果不得覆盖这些名字",
    )
    steps: list[WorksheetStep] = Field(
        min_length=2,
        max_length=5,
        description="按顺序执行的步骤（2～5 个）；后续步骤可按结果名引用先前步骤",
    )

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, variables: dict[str, Variable]) -> dict[str, Variable]:
        for name in variables:
            if not _VAR_NAME_RE.match(name):
                raise ValueError(
                    f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                    "仅含字母、数字、下划线，长度不超过 32"
                )
        return variables

    @model_validator(mode="after")
    def _check_step_names_unique_and_not_overriding(self) -> "WorksheetBody":
        names: set[str] = set(self.variables)
        for step in self.steps:
            if step.name in names:
                raise ValueError(
                    f"结果名 {step.name!r} 与初始变量或先前步骤结果重名："
                    "步骤不能引用未执行步骤，也不能覆盖已有变量"
                )
            names.add(step.name)
        return self


class WorksheetRequest(WorksheetBody, CustomUnitsSelectionModel):
    """POST /worksheet 请求体：初始变量 + 2～5 个顺序执行的步骤。"""


class WorksheetStepResult(BaseModel):
    """单个步骤的结果。"""

    name: str = Field(description="该步骤的结果名")
    value: str = Field(description="换算结果的精确十进制表示")
    unit: str = Field(description="目标单位表达式的规范形式")
    dimension: str = Field(description="规范量纲表达式（SI 基本单位）")


class WorksheetResponse(BaseModel):
    """POST /worksheet 的成功响应：各步骤按提交顺序给出结果。"""

    steps: list[WorksheetStepResult] = Field(
        description="各步骤结果（顺序与提交一致），供核对完整计算过程"
    )


# ---------------------------------------------------------------------------
# 可修订工作单（POST/GET/PATCH /revisable-worksheets）
# ---------------------------------------------------------------------------


class RevisableWorksheetCreateRequest(WorksheetBody):
    """POST /revisable-worksheets 请求体：现有 2～5 步工作单 + 一份定义集引用。

    与诊断/迁移请求一致：单位来源只接受**已发布**定义集的标识或活动别名
    （请求级 ``custom_units`` 不参与持久化），二者恰好给出一个。别名在
    创建开始时解析一次，保存的是解析到的具体标识——此后别名再切换也不
    影响该工作单。
    """

    custom_unit_set_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="已发布定义集标识；与 custom_unit_set_alias 恰好给出一个；"
        "不存在返回 422，不创建工作单",
    )
    custom_unit_set_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=ALIAS_NAME_PATTERN,
        description="已发布定义集的活动别名；创建开始时解析一次并固定实际指向，"
        "此后别名再切换仍使用创建时的定义；与 custom_unit_set_id 恰好给出一个；"
        "别名不存在返回 422，不创建工作单",
    )

    @model_validator(mode="after")
    def _check_exactly_one_ref(self) -> "RevisableWorksheetCreateRequest":
        if (self.custom_unit_set_id is None) == (self.custom_unit_set_alias is None):
            raise ValueError(
                "custom_unit_set_id 与 custom_unit_set_alias 必须恰好给出一个"
            )
        return self


class VariableReplacement(BaseModel):
    """修订指令：替换一个**现有**初始变量的值与单位（变量名不变）。"""

    name: str = Field(
        min_length=1,
        max_length=32,
        description="要替换的现有初始变量名；该变量不存在时整次修订返回 422",
    )
    value: Decimal = Field(
        description="新的十进制定值（建议用字符串传递）；与单位一起整体替换"
    )
    unit: str = Field(
        min_length=1, max_length=200, description="新的变量单位表达式"
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, name: str) -> str:
        if not _VAR_NAME_RE.match(name):
            raise ValueError(
                f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        return name

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("value")
    @classmethod
    def _check_value(cls, d: Decimal) -> Decimal:
        return _check_value(d)


class StepReplacement(BaseModel):
    """修订指令：按现有结果名替换一个步骤的公式/方程及目标单位。

    替换内容的字段规则与 :class:`WorksheetStep` 完全一致（求值/反求恰好
    一种、结果名词法合法、方程恰好一个等号等）；``name`` 只用于定位现有
    步骤，步骤位置与名称保持不变，不能新增、删除或重命名步骤。步骤可以
    在求值与反求两种形态之间替换，只要替换后整张工作单仍满足既有校验。
    """

    name: str = Field(
        min_length=1,
        max_length=32,
        description="要替换的现有步骤结果名；该步骤不存在时整次修订返回 422；"
        "步骤位置与名称保持不变",
    )
    output_unit: str = Field(
        min_length=1, max_length=200, description="新的结果目标单位表达式"
    )
    formula: str | None = Field(
        default=None, min_length=1, max_length=500, description="新的求值公式（求值步骤）"
    )
    equation: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="新的方程（反求步骤，恰好一个等号）",
    )
    solve_for: str | None = Field(
        default=None, min_length=1, max_length=32, description="新的待解未知量名称（反求步骤）"
    )

    @model_validator(mode="after")
    def _check_step_shape(self) -> "StepReplacement":
        # 复用 WorksheetStep 的全部步骤级校验（两种步骤恰好一种、名称/未知量
        # 词法、方程单等号等），保证替换内容与新建工作单同等严格；未提供的
        # 可选字段以 None 传入，由 WorksheetStep 的“恰好一种步骤”规则判定
        WorksheetStep(
            name=self.name,
            output_unit=self.output_unit,
            formula=self.formula,
            equation=self.equation,
            solve_for=self.solve_for,
        )
        return self

    def to_step(self) -> WorksheetStep:
        """转换为可直接放入工作单本体的 :class:`WorksheetStep`。"""

        return WorksheetStep(
            name=self.name,
            output_unit=self.output_unit,
            formula=self.formula,
            equation=self.equation,
            solve_for=self.solve_for,
        )


class RevisableWorksheetPatchRequest(BaseModel):
    """PATCH /revisable-worksheets/{id} 请求体：预期修订号 + 恰好一项修改。"""

    expected_revision: int = Field(
        ge=1,
        description="调用方读取到的当前修订号；与服务端当前修订号不一致时"
        "返回 409 且不做任何修改。初始修订号为 1，每次成功修订后 +1",
    )
    variable: VariableReplacement | None = Field(
        default=None,
        description="替换一个现有初始变量的值与单位；与 step 恰好给出一个",
    )
    step: StepReplacement | None = Field(
        default=None,
        description="按现有结果名替换一个步骤的公式/方程及目标单位（名称不变）；"
        "与 variable 恰好给出一个",
    )

    @model_validator(mode="after")
    def _check_exactly_one_change(self) -> "RevisableWorksheetPatchRequest":
        if (self.variable is None) == (self.step is None):
            raise ValueError(
                "variable 与 step 必须恰好给出一个：一次修订只能替换一个初始"
                "变量，或一个现有步骤"
            )
        return self


class StepResultBindingRequest(WorksheetBody):
    """POST /revisable-worksheets/{id}/bindings 请求体：把上游步骤结果绑定为下游初始变量。

    路径参数 ``id`` 为**上游**工作单标识；本请求体是待创建的**下游**工作单
    本体（2～5 步，不含单位来源——下游固定沿用上游创建时固定的同一份已发布
    定义集标识）。``step_name`` 给出上游步骤结果名：下游必须存在同名初始
    变量，关联建立时该变量的值与单位被该步骤结果的 ``value``/``unit``
    **整体覆盖**后再整单计算；``expected_revision`` 为调用方读取到的上游
    修订号，过期返回 409。
    """

    step_name: str = Field(
        min_length=1,
        max_length=32,
        description="要绑定的上游步骤结果名；该步骤必须在上游工作单中存在，"
        "且下游工作单必须存在同名初始变量（其值与单位由步骤结果覆盖）",
    )
    expected_revision: int = Field(
        ge=1,
        description="调用方读取到的上游当前修订号；与服务端不一致时返回 409，"
        "不建立关联、不创建下游工作单",
    )

    @field_validator("step_name")
    @classmethod
    def _check_step_name(cls, name: str) -> str:
        if not _VAR_NAME_RE.match(name):
            raise ValueError(
                f"步骤名 {name!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        return name


class WorksheetBindingInfo(BaseModel):
    """下游工作单上记录的步骤结果绑定信息（读取下游时可见）。

    - ``upstream_worksheet_id``：提供步骤结果的上游工作单标识；
    - ``step_name``：被绑定的上游步骤结果名（同时也是下游被覆盖的初始变量名）；
    - ``upstream_revision``：当前绑定变量值所对应的上游修订号：关联建立时
      为关联时的上游修订号，每次上游修订联动下游后同步为新的上游修订号。
    """

    upstream_worksheet_id: str = Field(description="上游工作单标识（步骤结果的来源）")
    step_name: str = Field(description="被绑定的上游步骤结果名（也是下游被覆盖的初始变量名）")
    upstream_revision: int = Field(
        ge=1,
        description="绑定值对应的上游修订号；上游修订联动后与上游当前修订号一致",
    )


class StoredVariable(BaseModel):
    """读取/修订响应中的单个初始变量（值为精确十进制字符串）。"""

    value: str = Field(description="变量的精确十进制值（字符串）")
    unit: str = Field(description="变量单位表达式")


class RevisableWorksheetResponse(BaseModel):
    """可修订工作单当前修订的完整内容（创建/读取/修订成功均返回本模型）。"""

    id: str = Field(description="服务生成的工作单标识（不可猜测）")
    revision: int = Field(
        description="当前修订号：创建成功为 1，每次成功修订递增 1；"
        "steps 与 variables 必然同属这一修订"
    )
    custom_unit_set_id: str = Field(
        description="创建时固定下来的实际定义集标识（即使创建时用的是别名，"
        "这里也是解析到的标识；别名此后切换不影响本工作单）；"
        "存在绑定时，上下游两单的该标识必然相同"
    )
    variables: dict[str, StoredVariable] = Field(
        description="当前修订的初始变量（单位表达式 + 精确十进制值）；"
        "被绑定的变量其值与单位来自上游步骤结果，直接修改它会返回 422"
    )
    steps: list[WorksheetStep] = Field(
        description="当前修订的步骤定义（2～5 个，顺序与执行顺序一致）"
    )
    results: list[WorksheetStepResult] = Field(
        description="当前修订在固定定义集下整单重算得到的逐步结果，"
        "与 variables、steps 同属 revision 这一修订"
    )
    binding: WorksheetBindingInfo | None = Field(
        default=None,
        description="本工作单作为下游时的绑定信息（上游标识、步骤名与对应"
        "上游修订号）；无关联的工作单不返回该字段",
    )


class UnitSetRef(BaseModel):
    """诊断请求中一侧（旧/新）使用的已发布定义集引用。

    与四个计算接口一样，既可以直接给定义集标识 ``custom_unit_set_id``，
    也可以给活动别名 ``custom_unit_set_alias``；二者恰好给出一个。
    别名在诊断请求开始时解析一次并把实际指向固定下来，随后该侧所有
    步骤都使用解析到的那一版定义，执行期间切换别名不会造成同侧混版。
    """

    custom_unit_set_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="已发布定义集标识；与 custom_unit_set_alias 恰好给出一个；不存在返回 422",
    )
    custom_unit_set_alias: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=ALIAS_NAME_PATTERN,
        description="已发布定义集的活动别名；请求开始解析一次并固定实际指向；"
        "与 custom_unit_set_id 恰好给出一个；别名不存在返回 422",
    )

    @model_validator(mode="after")
    def _check_exactly_one_ref(self) -> "UnitSetRef":
        if (self.custom_unit_set_id is None) == (self.custom_unit_set_alias is None):
            raise ValueError(
                "custom_unit_set_id 与 custom_unit_set_alias 必须恰好给出一个"
            )
        return self


class WorksheetDiagnoseRequest(WorksheetBody):
    """POST /worksheet-diagnose 请求体：同一工作单 + 旧/新两份定义集引用。"""

    old: UnitSetRef = Field(
        description="旧版已发布定义集的标识或活动别名；请求开始即固定实际指向"
    )
    new: UnitSetRef = Field(
        description="新版已发布定义集的标识或活动别名；请求开始即固定实际指向"
    )


class DiagnosticError(BaseModel):
    """诊断中某一侧准备阶段或某一步的业务错误（结构化回传，非 HTTP 422）。"""

    error: str = Field(description="机器可读错误码（与既有错误码一致）")
    message: str = Field(description="人类可读错误信息")


class DiagnosticSideStep(BaseModel):
    """诊断中某一侧在单个步骤上的结果。"""

    status: Literal["ok", "error", "not_run"] = Field(
        description="ok=该侧该步成功；error=该侧在该步业务失败；"
        "not_run=该侧此前已失败，本步未执行（不计为成功）"
    )
    value: str | None = Field(
        default=None, description="该步换算结果的精确十进制表示（status=ok 时给出）"
    )
    unit: str | None = Field(
        default=None, description="目标单位表达式的规范形式（status=ok 时给出）"
    )
    dimension: str | None = Field(
        default=None, description="规范量纲表达式（SI 基本单位；status=ok 时给出）"
    )
    error: DiagnosticError | None = Field(
        default=None, description="该步业务错误（status=error 时给出）"
    )


class DiagnosticSide(BaseModel):
    """诊断请求中一侧（旧/新）的完整执行情况。"""

    reference: UnitSetRef = Field(description="请求中提交的引用（标识或别名）")
    resolved_id: str | None = Field(
        default=None,
        description="请求开始时固定下来的实际定义集标识；引用无法解析时为 null",
    )
    setup_error: DiagnosticError | None = Field(
        default=None,
        description="初始变量在该侧定义下解析失败时的错误；"
        "此时该侧所有步骤均为 not_run",
    )
    steps: list[DiagnosticSideStep] = Field(
        description="与提交顺序一致的各步骤结果/错误/未执行情况"
    )


class FirstMeaningChange(BaseModel):
    """首个物理含义发生变化的步骤（按内部精确量比较，而非显示字符串）。"""

    index: int = Field(description="首个含义变化步骤的下标（从 0 开始）")
    name: str = Field(description="该步骤的结果名")
    differences: list[Literal["value", "dimension", "absolute"]] = Field(
        description="该步两侧的含义差异：value=内部精确数值不同（SI 口径分数）；"
        "dimension=量纲不同；absolute=绝对温度属性不同"
    )


class DiagnosticStepComparison(BaseModel):
    """单个步骤两侧的并排对照。"""

    index: int = Field(description="步骤下标（从 0 开始）")
    name: str = Field(description="步骤结果名")
    old: DiagnosticSideStep = Field(description="旧版定义集下的执行情况")
    new: DiagnosticSideStep = Field(description="新版定义集下的执行情况")
    meaning_changed: bool = Field(
        description="仅当两侧该步都成功且内部精确数值/量纲/绝对温度属性任一不同时为 true；"
        "显示字符串差异不影响判定"
    )
    differences: list[Literal["value", "dimension", "absolute"]] = Field(
        description="该步含义差异明细（meaning_changed 为 false 时为空）"
    )


class WorksheetDiagnoseResponse(BaseModel):
    """POST /worksheet-diagnose 的成功响应。"""

    old: DiagnosticSide = Field(description="旧版定义集一侧的解析与执行情况")
    new: DiagnosticSide = Field(description="新版定义集一侧的解析与执行情况")
    steps: list[DiagnosticStepComparison] = Field(
        description="逐步并排对照（顺序与提交一致）"
    )
    first_meaning_change: FirstMeaningChange | None = Field(
        default=None,
        description="首个物理含义变化的步骤；两侧全部成功且含义一致（或无共同成功步骤）时为 null",
    )


class WorksheetMigrateRequest(WorksheetBody):
    """POST /worksheet-migrate 请求体：现有工作单 + 旧/新两份定义集引用。

    结构与诊断请求一致：两侧引用在请求开始时各解析一次并固定实际指向，
    随后先在旧定义下执行原工作单，再把初始变量改写为新定义下物理量
    相同的精确十进制值。
    """

    old: UnitSetRef = Field(
        description="旧版已发布定义集的标识或活动别名；请求开始即固定实际指向"
    )
    new: UnitSetRef = Field(
        description="新版已发布定义集的标识或活动别名；请求开始即固定实际指向"
    )


class MigratedVariable(BaseModel):
    """迁移后的初始变量：单位表达式不变，数值改写为新定义下的精确十进制。"""

    value: str = Field(
        description="改写后的精确十进制值（字符串形式，可直接作为 /worksheet 输入）"
    )
    unit: str = Field(description="单位表达式（与原工作单完全一致）")


class MigratedWorksheet(BaseModel):
    """可直接提交给 POST /worksheet 的迁移后工作单。"""

    variables: dict[str, MigratedVariable] = Field(
        description="改写后的初始变量（原工作单无初始变量时为空对象）"
    )
    steps: list[WorksheetStep] = Field(description="与原工作单完全一致的步骤")
    custom_unit_set_id: str = Field(
        description="请求开始时固定的新版定义集标识（引用标识而非别名，"
        "此后别名再被切换也不影响本工作单）"
    )


class WorksheetMigrateResponse(BaseModel):
    """POST /worksheet-migrate 的成功响应。"""

    old_resolved_id: str = Field(description="请求开始时固定的旧版定义集标识")
    new_resolved_id: str = Field(description="请求开始时固定的新版定义集标识")
    worksheet: MigratedWorksheet = Field(
        description="可直接提交给 POST /worksheet 的迁移后工作单"
    )


class ConvertResponse(BaseModel):
    """POST /convert、POST /evaluate 与 POST /solve 的成功响应。"""

    value: str = Field(description="换算结果的精确十进制表示")
    unit: str = Field(description="目标单位表达式的规范形式")
    dimension: str = Field(description="规范量纲表达式（SI 基本单位）")


class PublishUnitSetRequest(BaseModel):
    """POST /custom-unit-sets 请求体：提交 1～6 项 custom_units 格式定义。"""

    model_config = ConfigDict(populate_by_name=True)

    custom_units: CustomUnits = Field(
        min_length=1,
        max_length=6,
        description="待发布的自定义线性单位声明（1～6 项），格式与三个换算接口"
        "的请求级 custom_units 完全一致；发布前按现有规则校验并展开全部条目",
    )


class PublishedCustomUnitItem(BaseModel):
    """已发布定义集回显的单条定义（factor 为精确十进制字符串）。"""

    symbol: str = Field(description="自定义单位符号")
    factor: str = Field(description="正十进制比例：1 symbol = factor 个 base_unit")
    base_unit: str = Field(description="基准单位表达式")


class PublishedUnitSetResponse(BaseModel):
    """POST /custom-unit-sets 的成功响应。"""

    id: str = Field(description="服务生成的定义集标识（不可猜测；已发布内容不可修改）")
    custom_units: list[PublishedCustomUnitItem] = Field(
        description="已发布的定义项（factor 回显为精确十进制字符串），顺序与提交一致"
    )


class BindUnitSetAliasRequest(BaseModel):
    """PUT /custom-unit-set-aliases/{alias} 请求体。"""

    custom_unit_set_id: str = Field(
        min_length=1,
        max_length=64,
        description="别名要指向的已发布定义集标识；不存在时返回 422 且不改变指向",
    )
    expected_id: str | None = Field(
        description="乐观并发：调用方认为别名当前指向的标识；初建别名时必须为 "
        "null。仅当当前指向与此一致时切换才生效，否则返回 409",
    )


class UnitSetAliasInfo(BaseModel):
    """单个活动别名的当前状态。"""

    alias: str = Field(description="活动别名（调用方长期沿用的名称）")
    custom_unit_set_id: str = Field(
        description="当前指向的已发布定义集标识（不可变定义集的引用）"
    )


class UnitSetAliasListResponse(BaseModel):
    """GET /custom-unit-set-aliases 的成功响应。"""

    aliases: list[UnitSetAliasInfo] = Field(
        description="全部活动别名及其当前指向（按别名排序）"
    )


#: 迁移包的格式标识与格式版本（导出/导入双方据此识别与兼容）。
MIGRATION_PACKAGE_KIND = "unit-api.custom-unit-set-migration"
MIGRATION_PACKAGE_VERSION = 1


class UnitSetMigrationExportRequest(BaseModel):
    """POST /custom-unit-set-migration/export 请求体：1～6 个已发布定义集标识。"""

    model_config = ConfigDict(populate_by_name=True)

    custom_unit_set_ids: list[str] = Field(
        min_length=1,
        max_length=6,
        description="要导出的已发布定义集标识（1～6 个，不可重复）；"
        "任一不存在或重复时整次请求返回 422",
    )


class UnitSetMigrationPackage(BaseModel):
    """实例间迁移包：1～6 个定义集 + 指向它们的活动别名。

    导出响应即本模型，可原样作为导入请求体交给另一实例。``sets`` 的键为
    **源实例标识**（导入时不沿用，仅用于包内引用与响应映射）；``aliases``
    的值必须是 ``sets`` 中存在的键，悬空引用整包拒绝。
    """

    kind: Literal[MIGRATION_PACKAGE_KIND] = Field(
        default=MIGRATION_PACKAGE_KIND,
        description="迁移包格式标识，用于识别包类型",
    )
    version: Literal[MIGRATION_PACKAGE_VERSION] = Field(
        default=MIGRATION_PACKAGE_VERSION, description="迁移包格式版本"
    )
    sets: dict[str, list[CustomUnitItem]] = Field(
        min_length=1,
        max_length=6,
        description="包内定义集：键为源实例的定义集标识（1～6 个、互不重复）；"
        "各定义集均按发布时相同的规则在导入端重新校验",
    )
    aliases: dict[str, str] = Field(
        default_factory=dict,
        description="导出时指向包内定义集的活动别名：键为别名，值为 sets 中"
        "的源实例标识；导入后改指目标实例生成的新标识。目标端已有同名别名"
        "或引用悬空时整包拒绝",
    )

    @field_validator("sets")
    @classmethod
    def _check_set_id_keys(cls, sets: dict[str, list[CustomUnitItem]]) -> dict:
        for set_id in sets:
            if not 1 <= len(set_id) <= 64:
                raise ValueError("定义集标识长度须在 1～64 之间")
        return sets

    @field_validator("aliases")
    @classmethod
    def _check_alias_keys_and_targets(cls, aliases: dict[str, str]) -> dict[str, str]:
        for alias in aliases:
            if not re.match(ALIAS_NAME_PATTERN, alias):
                raise ValueError(
                    f"别名 {alias!r} 不合法：仅允许字母、数字、下划线与连字符，"
                    "长度 1～64"
                )
        for alias, target in aliases.items():
            if not 1 <= len(target) <= 64:
                raise ValueError(f"别名 {alias!r} 的目标标识长度须在 1～64 之间")
        return aliases


class UnitSetMigrationImportResponse(BaseModel):
    """POST /custom-unit-set-migration/import 的成功响应。"""

    package: UnitSetMigrationPackage = Field(description="实际导入的迁移包（回显）")
    id_map: dict[str, str] = Field(
        description="源实例旧标识 -> 目标实例新生成的本地标识（按旧标识排序）"
    )
    aliases: list[UnitSetAliasInfo] = Field(
        description="导入后建立的活动别名及其指向的新标识（按别名排序）"
    )


class ErrorDetail(BaseModel):
    error: str = Field(description="机器可读错误码")
    message: str = Field(description="人类可读错误信息")


class ErrorResponse(BaseModel):
    detail: ErrorDetail
