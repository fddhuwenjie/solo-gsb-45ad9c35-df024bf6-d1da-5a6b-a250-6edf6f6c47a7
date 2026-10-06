"""可顺序执行的量纲计算工作单（``POST /worksheet``）。

一次请求提交初始带单位变量与 2～5 个步骤，步骤按提交顺序执行：

- 每个步骤要么是公式求值（``formula``），要么是一次线性方程反求
  （``equation`` + ``solve_for``），并把结果精确换算到 ``output_unit``；
- 后续步骤可按结果名引用**先前步骤**的结果；引用未执行步骤或覆盖已有
  变量（初始变量或先前结果名）都在 schema 层被拒绝为 422；
- 步骤间传递的是 :class:`~app.evaluator.Quantity`——SI 基单位口径的
  **精确分数数值**与**绝对温度属性**（``absolute`` 标记），不经十进制
  序列化，因此没有中间精度损失；求值与反求沿用各自既有的量纲、仿射与
  精度规则；
- 任一步骤失败都抛出 :class:`~app.errors.ConversionError`（HTTP 422），
  端点不返回任何部分结果。

:func:`collect_worksheet_run` 与主流程共用同一套逐步执行逻辑，但它不抛出
业务错误，而是把准备阶段错误与每个步骤的结果/错误/未执行状态原样收集
回来，供 ``POST /worksheet-diagnose`` 做旧/新定义集并排诊断。

自定义单位来源（请求内 ``custom_units`` / 已发布定义集 / 活动别名）与
三个既有接口完全一致：单位上下文（:class:`~app.custom_units.UnitScope`）
在工作单开始时构造一次，所有步骤的单位解析都使用同一份定义；活动别名
因此在整个工作单内只解析一次——即使执行期间别名被切换，各步骤也使用
同一版定义，不会跨版本混用。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .converter import (
    convert_fraction,
    is_absolute_target,
    to_decimal_str,
)
from .errors import ConversionError, affine_mixing
from .evaluator import Quantity, evaluate, make_quantity
from .parser import ParsedExpression, canonical, canonical_dim, parse_expression
from .schemas import WorksheetRequest, WorksheetStepResult
from .solver import solve

#: 求值步骤中“绝对温标唯一项”结果在内部使用的源单位项（开尔文）。
_KELVIN_TERMS = (("K", 1),)


@dataclass(frozen=True)
class _StepOutcome:
    """单个步骤的内部结果：响应字段 + 供后续步骤引用的精确量。"""

    result: WorksheetStepResult
    quantity: Quantity


@dataclass(frozen=True)
class StepRecord:
    """单个步骤的收集结果（诊断用）。

    - ``result``/``quantity`` 非空：该步成功；
    - ``error`` 非空：该步业务失败（量纲冲突、除零、非线性、精度溢出等）；
    - 三者皆空：该侧此前步骤已失败，本步未执行。
    """

    result: WorksheetStepResult | None = None
    quantity: Quantity | None = None
    error: ConversionError | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None


@dataclass(frozen=True)
class WorksheetRun:
    """一次工作单执行的完整收集结果（诊断用）。

    ``setup_error`` 非空表示初始变量解析即失败，此时 ``steps`` 全部为
    未执行记录；否则 ``steps`` 与提交步骤一一对应，首个失败步骤之后的
    记录均为未执行（后续步骤不允许使用失败步骤的结果）。
    """

    setup_error: ConversionError | None
    steps: list[StepRecord]


def _convert_step_result(
    quantity: Quantity, dst: ParsedExpression
) -> tuple[Fraction, str, str]:
    """把步骤结果（SI 口径精确量）精确换算到目标单位。

    仿射及精度规则与 ``/evaluate``、``/solve`` 完全一致：
    绝对温标结果只能换算到绝对温标；反求结果只能换算到线性单位。
    返回 ``(目标单位下的精确分数, 规范单位, 规范量纲)``。
    """

    if quantity.absolute:
        if not is_absolute_target(dst):
            raise affine_mixing(
                "绝对温度结果只能换算到绝对温标（degC/degF/K），"
                "不能换算到温差单位或复合单位"
            )
        src = ParsedExpression(dim=quantity.dim, scale=Fraction(1), terms=_KELVIN_TERMS)
    else:
        src = ParsedExpression(dim=quantity.dim, scale=Fraction(1), terms=())
    value_fr = convert_fraction(quantity.value_si, src, dst)
    return value_fr, canonical(dst.terms), canonical_dim(dst.dim)


def initial_env(req: WorksheetRequest, scope) -> dict[str, Quantity]:
    """解析初始变量单位并折叠为 SI 口径精确量；未知单位等错误在此抛出。"""

    return {
        name: make_quantity(var.value, parse_expression(var.unit, scope))
        for name, var in req.variables.items()
    }


def _execute_step(step, env: dict[str, Quantity], scope) -> _StepOutcome:
    """执行单个步骤并把结果精确换算到其目标单位。

    与 ``/evaluate``、``/solve`` 完全一致的顺序：先解析该步目标单位，
    再求值/反求，最后做量纲与仿射校验、精确换算与十进制化；任一处失败
    都抛出 :class:`~app.errors.ConversionError`，且不修改 ``env``。
    """

    dst = parse_expression(step.output_unit, scope)
    if step.formula is not None:
        # 求值：env 只含初始变量与先前步骤结果，引用未执行步骤会因名字
        # 尚不存在而以 unknown_variable 返回 422
        quantity = evaluate(step.formula, env)
    else:
        # 反求：未知量不得与初始变量/先前结果同名（schema 已保证结果名
        # 唯一，但 solve_for 独立于结果名，需在此排除覆盖）
        if step.solve_for in env:
            raise ConversionError(
                "invalid_request",
                f"solve_for {step.solve_for!r} 不得与初始变量或先前步骤结果重名",
            )
        if dst.affine is not None:
            raise affine_mixing(
                "求解结果只能换算到线性单位，不能换算到绝对温标（degC/degF）"
            )
        value_si, dim = solve(step.equation, step.solve_for, env)
        if dim is None:
            # 解恒为零且量纲不受方程约束（如 2*x = x）：接受任意线性目标
            # 单位，量纲取目标单位
            quantity = Quantity(Fraction(0), dst.dim)
        else:
            quantity = Quantity(value_si, dim)

    value_fr, unit, dimension = _convert_step_result(quantity, dst)
    # 十进制化在每步产出时完成：非有限十进制或超精度即在此抛出
    result = WorksheetStepResult(
        name=step.name,
        value=to_decimal_str(value_fr),
        unit=unit,
        dimension=dimension,
    )
    return _StepOutcome(result=result, quantity=quantity)


def _run_steps(req: WorksheetRequest, scope) -> list[_StepOutcome]:
    """顺序执行全部步骤；任一步失败即抛出（422，无部分结果）。"""

    # 初始变量先解析单位并折叠为 SI 口径的精确量；未知单位在此即报 422
    env = initial_env(req, scope)

    outcomes: list[_StepOutcome] = []
    for step in req.steps:
        # 注意：_execute_step 失败时不会把结果写入 env，后续步骤因此不可能
        # 引用到失败步骤的结果
        outcome = _execute_step(step, env, scope)
        outcomes.append(outcome)
        # 步骤间传递精确分数与绝对温度属性（而非上面渲染出的十进制字符串）
        env[step.name] = outcome.quantity
    return outcomes


def run_worksheet(req: WorksheetRequest, scope) -> list[WorksheetStepResult]:
    """顺序执行工作单，返回各步骤结果；任一步失败即抛出（422，无部分结果）。

    ``scope`` 为工作单开始时解析好的单位上下文（三种来源之一或 ``None``），
    整个工作单共用，保证别名只解析一次、各步骤使用同一版自定义单位定义。
    """

    return [outcome.result for outcome in _run_steps(req, scope)]


def collect_worksheet_run(req: WorksheetRequest, scope) -> WorksheetRun:
    """逐步收集工作单执行情况但不抛出业务错误（供换版诊断使用）。

    返回的 :class:`WorksheetRun` 保留：

    - 初始变量解析阶段的错误（``setup_error``）；
    - 每个成功步骤的渲染结果与 SI 口径精确量（供后续步骤与跨版比较）；
    - 首个失败步骤的业务错误，以及其后全部未执行步骤的占位记录。

    执行语义与 :func:`run_worksheet` 完全一致（相同顺序、相同错误），
    区别仅在于错误被收集而非抛出；“失败后的步骤不计为成功”。
    """

    try:
        env = initial_env(req, scope)
    except ConversionError as exc:
        return WorksheetRun(
            setup_error=exc,
            steps=[StepRecord() for _ in req.steps],
        )

    records: list[StepRecord] = []
    failed = False
    for step in req.steps:
        if failed:
            # 此前已有步骤失败：本步不执行，也不计为成功
            records.append(StepRecord())
            continue
        try:
            outcome = _execute_step(step, env, scope)
        except ConversionError as exc:
            records.append(StepRecord(error=exc))
            failed = True
        else:
            records.append(
                StepRecord(result=outcome.result, quantity=outcome.quantity)
            )
            # 与主流程一致：步骤间传递精确分数与绝对温度属性
            env[step.name] = outcome.quantity
    return WorksheetRun(setup_error=None, steps=records)
