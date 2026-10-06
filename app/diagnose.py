"""工作单换版诊断（``POST /worksheet-diagnose``）。

同一份现有工作单（2～5 步）分别在旧、新两份**已发布**单位定义集下各执行
一次，并排回传两侧的逐步结果与**首个物理含义变化**，用于在不可变定义集
换版（重新发布 + 切换别名）前发现“显示不变、物理含义已变”之类的问题。

关键约定：

- **开始时固定实际指向**：请求中的两侧引用既可以是定义集标识，也可以是
  活动别名；两侧别名/标识在任何步骤执行前各自解析一次并记录解析到的
  标识，随后每侧的初始变量与全部步骤都使用按这一版定义构造的独立
  :class:`~app.custom_units.UnitScope`。执行期间别名即使被切换，也不会
  造成同侧步骤混版；整个过程只读，不修改定义集或别名。
- **按内部精确量比较，而非显示字符串**：每个共同成功的步骤比较两侧
  :class:`~app.evaluator.Quantity` 的 SI 口径精确分数（``value_si``）、
  量纲向量与绝对温度属性。例如旧版 ``span=2 m``、新版 ``span=3 m`` 时，
  输入 ``1 span`` 再换算到 ``span``，两侧响应都显示 ``1 span``，但内部
  数值分别为 2 m 与 3 m，仍须报告 ``value`` 差异。
- **部分失败可对照**：某一侧在第 k 步业务失败（量纲冲突、未知符号等）时，
  保留该侧此前已完成步骤的对照并回报该步错误，失败后的步骤标记为
  ``not_run``，不计为成功；两侧均成功时返回全部步骤对照。
- 引用的定义集/别名不存在、请求体非法属于请求级错误，仍由端点返回 422；
- 工作单本身的业务错误（变量单位解析失败或某步失败）不再映射为 422，
  而是结构化地出现在 200 响应对应一侧，这是本诊断与 ``POST /worksheet``
  （任一步错误只返回 422、不返回部分结果）的唯一行为分界。
"""

from __future__ import annotations

from dataclasses import dataclass

from .evaluator import Quantity
from .schemas import (
    DiagnosticError,
    DiagnosticSide,
    DiagnosticSideStep,
    DiagnosticStepComparison,
    FirstMeaningChange,
    UnitSetRef,
    WorksheetDiagnoseRequest,
    WorksheetDiagnoseResponse,
    WorksheetStepResult,
)
from .unit_sets import UnitSetStore, get_store
from .worksheet import StepRecord, WorksheetRun, collect_worksheet_run

#: 含义差异的展示顺序：精确数值、量纲、绝对温度属性。
_DIFF_ORDER = ("value", "dimension", "absolute")


@dataclass(frozen=True)
class ResolvedSide:
    """一侧引用解析后的实际标识与按该版定义构造的请求级上下文。"""

    resolved_id: str
    scope: object


def resolve_side(ref: UnitSetRef, store: UnitSetStore) -> ResolvedSide:
    """在任何步骤执行前固定一侧的实际定义集标识并构造独立上下文。

    别名在此一次性快照（``get_alias`` 在锁内读取当前指向），之后整侧只
    使用返回的 scope；标识或别名不存在时抛出 422 领域错误。
    """

    if ref.custom_unit_set_alias is not None:
        resolved_id = store.get_alias(ref.custom_unit_set_alias)
    else:
        resolved_id = ref.custom_unit_set_id
        assert resolved_id is not None
        # 标识不存在在此抛出 422（build_scope 内部也会再取一次）
        store.get(resolved_id)
    return ResolvedSide(resolved_id=resolved_id, scope=store.build_scope(resolved_id))


def _error_payload(exc) -> DiagnosticError:
    return DiagnosticError(error=exc.code, message=exc.message)


def _side_step(record: StepRecord) -> DiagnosticSideStep:
    """把单步收集记录渲染为响应。"""

    if record.succeeded:
        result: WorksheetStepResult = record.result
        return DiagnosticSideStep(
            status="ok",
            value=result.value,
            unit=result.unit,
            dimension=result.dimension,
        )
    if record.error is not None:
        return DiagnosticSideStep(status="error", error=_error_payload(record.error))
    return DiagnosticSideStep(status="not_run")


def _build_side(
    ref: UnitSetRef, resolved_id: str, run: WorksheetRun
) -> DiagnosticSide:
    """渲染一侧（旧/新）的完整响应。"""

    return DiagnosticSide(
        reference=ref,
        resolved_id=resolved_id,
        setup_error=_error_payload(run.setup_error) if run.setup_error else None,
        steps=[_side_step(record) for record in run.steps],
    )


def quantity_differences(old: Quantity, new: Quantity) -> list[str]:
    """比较两个成功步骤的内部精确量，返回含义差异明细（不比较显示字符串）。"""

    differences: list[str] = []
    # SI 基单位口径的精确分数：即便目标单位下显示相同（如两侧都显示
    # “1 span”），物理值不同也在此检出
    if old.value_si != new.value_si:
        differences.append("value")
    if old.dim != new.dim:
        differences.append("dimension")
    if old.absolute != new.absolute:
        differences.append("absolute")
    # 按固定顺序输出，保证响应确定
    return [name for name in _DIFF_ORDER if name in differences]


def diagnose_worksheet(
    req: WorksheetDiagnoseRequest, *, store: UnitSetStore | None = None
) -> WorksheetDiagnoseResponse:
    """执行换版诊断并组装 200 响应；未知定义集/别名等请求级错误抛出 422。"""

    chosen = store or get_store()

    # 1) 开始时分别固定两侧的实际指向（别名在任何步骤执行前各解析一次），
    #    随后各用各的独立 scope 执行同一工作单；整个诊断只读，不修改
    #    定义集或别名。任一侧引用不存在都在此抛出 422（不执行任何步骤）。
    old_resolved = resolve_side(req.old, chosen)
    new_resolved = resolve_side(req.new, chosen)

    # 2) 用各自定义执行同一工作单（业务错误被收集，不抛出；失败后的步骤
    #    标记为 not_run，不计为成功）
    old_run = collect_worksheet_run(req, old_resolved.scope)
    new_run = collect_worksheet_run(req, new_resolved.scope)

    # 3) 逐步并排对照：仅比较两侧共同成功步骤的内部精确量
    comparisons: list[DiagnosticStepComparison] = []
    first_change: FirstMeaningChange | None = None
    for index, step in enumerate(req.steps):
        old_record = old_run.steps[index]
        new_record = new_run.steps[index]
        old_quantity = old_record.quantity if old_record.succeeded else None
        new_quantity = new_record.quantity if new_record.succeeded else None
        differences = (
            quantity_differences(old_quantity, new_quantity)
            if old_quantity is not None and new_quantity is not None
            else []
        )
        if differences and first_change is None:
            first_change = FirstMeaningChange(
                index=index, name=step.name, differences=differences
            )
        comparisons.append(
            DiagnosticStepComparison(
                index=index,
                name=step.name,
                old=_side_step(old_record),
                new=_side_step(new_record),
                meaning_changed=bool(differences),
                differences=differences,
            )
        )

    return WorksheetDiagnoseResponse(
        old=_build_side(req.old, old_resolved.resolved_id, old_run),
        new=_build_side(req.new, new_resolved.resolved_id, new_run),
        steps=comparisons,
        first_meaning_change=first_change,
    )
