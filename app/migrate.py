"""工作单输入迁移（``POST /worksheet-migrate``）。

把一份在旧版已发布定义集下可正常执行的现有工作单（初始变量 + 2～5 个
步骤）改写为在新版定义集下**物理量完全相同**的等价工作单，一次请求完成：

- **开始时固定实际指向**：旧/新两侧引用（定义集标识或活动别名）在任何
  步骤执行前各解析一次并固定实际标识，随后整侧只使用按该版定义构造的
  独立 :class:`~app.custom_units.UnitScope`；执行期间别名即使被切换，
  也不会造成同侧混版。整个迁移为只读操作，不修改定义集或别名。
- **先在旧定义下执行**：初始变量解析失败或任一步骤业务失败都抛出
  :class:`~app.errors.MigrationError`（HTTP 422），并指出对应变量或步骤。
- **改写每个初始变量**：单位表达式字符串保持不变，在新定义下重新解析，
  要求量纲与绝对温度属性与旧定义下一致，并把 SI 口径精确数值换算为该
  单位在新定义下的数值；改写值必须能表示为精确十进制（且满足
  ``/worksheet`` 的输入精度上限），否则 422 并指出变量。
- **复核整张新工作单**：用固定的新定义集标识组装新工作单并完整执行，
  逐步核对内部精确量（SI 分数数值、量纲、绝对温度属性）与旧执行一致；
  显示值允许变化。新工作单任一步出错或某步含义变化都返回 422 并指出
  步骤，此时不返回迁移工作单。

成功响应中的 ``worksheet`` 可直接提交给 ``POST /worksheet``：它引用的是
请求开始时固定的新定义集**标识**（而非别名），此后别名再被切换也不影响
该工作单。
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from .config import MAX_PRECISION
from .converter import convert_fraction, is_absolute_temperature, to_decimal_str
from .diagnose import quantity_differences, resolve_side
from .errors import ConversionError, MigrationError
from .parser import ParsedExpression, canonical_dim, parse_expression
from .schemas import (
    MigratedVariable,
    MigratedWorksheet,
    Variable,
    WorksheetMigrateRequest,
    WorksheetMigrateResponse,
    WorksheetRequest,
)
from .unit_sets import UnitSetStore, get_store
from .worksheet import WorksheetRun, collect_worksheet_run, initial_env

#: 绝对温度变量在内部换算时使用的源单位项（开尔文）。
_KELVIN_TERMS = (("K", 1),)


def _old_run_or_raise(req: WorksheetMigrateRequest, scope) -> WorksheetRun:
    """在旧定义下完整执行原工作单；任何失败都抛出指出变量/步骤的 422。"""

    run = collect_worksheet_run(req, scope)
    if run.setup_error is not None:
        # 准备阶段错误一定来自某个变量的单位解析：逐个重加以定位到变量
        for name, var in req.variables.items():
            try:
                parse_expression(var.unit, scope)
            except ConversionError as exc:
                raise MigrationError(
                    exc.code,
                    f"初始变量 {name!r} 在旧定义集下解析失败: {exc.message}",
                    variable=name,
                ) from exc
        raise MigrationError(
            run.setup_error.code,
            f"初始变量在旧定义集下解析失败: {run.setup_error.message}",
        ) from run.setup_error
    for index, record in enumerate(run.steps):
        if record.error is not None:
            step = req.steps[index]
            raise MigrationError(
                record.error.code,
                f"工作单在旧定义集下第 {index} 步（{step.name}）失败: "
                f"{record.error.message}",
                step_index=index,
                step_name=step.name,
            ) from record.error
    return run


def _rewrite_variable(
    name: str, var: Variable, old_quantity, new_scope
) -> MigratedVariable:
    """把单个初始变量改写为新定义下同一单位表达式、同一物理量的精确十进制值。

    单位表达式字符串不变；在新定义下重新解析后，量纲与绝对温度属性必须与
    旧定义下一致，数值换算结果必须是可作为 ``/worksheet`` 输入的精确十进制。
    """

    try:
        new_expr = parse_expression(var.unit, new_scope)
    except ConversionError as exc:
        raise MigrationError(
            exc.code,
            f"初始变量 {name!r} 的单位 {var.unit!r} 在新定义集下无法解析: "
            f"{exc.message}",
            variable=name,
        ) from exc
    if new_expr.dim != old_quantity.dim:
        raise MigrationError(
            "dimension_mismatch",
            f"初始变量 {name!r} 的单位 {var.unit!r} 在新定义集下的量纲为 "
            f"{canonical_dim(new_expr.dim)!r}，与旧定义下的 "
            f"{canonical_dim(old_quantity.dim)!r} 不兼容",
            variable=name,
        )
    if is_absolute_temperature(new_expr) != old_quantity.absolute:
        raise MigrationError(
            "affine_mixing",
            f"初始变量 {name!r} 的单位 {var.unit!r} 在新定义集下的绝对温度"
            "属性与旧定义下不一致",
            variable=name,
        )
    if old_quantity.absolute:
        # 绝对温度：内部值为开尔文，按仿射路径换算回同一温标
        src = ParsedExpression(
            dim=old_quantity.dim, scale=Fraction(1), terms=_KELVIN_TERMS
        )
    else:
        # 线性量（含温差）：以 SI 基单位口径构造源表达式
        src = ParsedExpression(dim=old_quantity.dim, scale=Fraction(1), terms=())
    try:
        new_value = convert_fraction(old_quantity.value_si, src, new_expr)
    except ConversionError as exc:
        raise MigrationError(
            exc.code,
            f"初始变量 {name!r} 无法换算为新定义下的同一单位: {exc.message}",
            variable=name,
        ) from exc
    try:
        value_str = to_decimal_str(new_value)
    except ConversionError as exc:
        raise MigrationError(
            exc.code,
            f"初始变量 {name!r} 的改写值无法表示为精确十进制: {exc.message}",
            variable=name,
        ) from exc
    # 改写值必须同样满足 /worksheet 的输入精度上限，保证新工作单可直接提交
    if len(Decimal(value_str).as_tuple().digits) > MAX_PRECISION:
        raise MigrationError(
            "precision_overflow",
            f"初始变量 {name!r} 的改写值有效数字超过 {MAX_PRECISION} 位的"
            "精度上限，无法作为 /worksheet 输入",
            variable=name,
        )
    return MigratedVariable(value=value_str, unit=var.unit)


def _verify_new_run(
    req: WorksheetMigrateRequest,
    old_run: WorksheetRun,
    new_req: WorksheetRequest,
    new_scope,
) -> None:
    """在新定义下执行改写后的工作单，逐步核对内部精确量与旧执行一致。

    显示值允许变化；任一步业务失败或某步的 SI 精确数值/量纲/绝对温度属性
    与旧执行不一致都抛出指出该步的 422。
    """

    run = collect_worksheet_run(new_req, new_scope)
    if run.setup_error is not None:
        # 每个变量已逐个改写并校验，理论上不会发生；防御性处理
        raise MigrationError(
            run.setup_error.code,
            f"改写后的初始变量在新定义集下解析失败: {run.setup_error.message}",
        ) from run.setup_error
    for index, record in enumerate(run.steps):
        step = req.steps[index]
        if record.error is not None:
            raise MigrationError(
                record.error.code,
                f"改写后的工作单在新定义集下第 {index} 步（{step.name}）失败: "
                f"{record.error.message}",
                step_index=index,
                step_name=step.name,
            ) from record.error
        old_quantity = old_run.steps[index].quantity
        assert old_quantity is not None and record.quantity is not None
        differences = quantity_differences(old_quantity, record.quantity)
        if differences:
            raise MigrationError(
                "meaning_changed",
                f"第 {index} 步（{step.name}）在新定义集下的结果含义发生变化"
                f"（{', '.join(differences)}），迁移被拒绝",
                step_index=index,
                step_name=step.name,
            )


def migrate_worksheet(
    req: WorksheetMigrateRequest, *, store: UnitSetStore | None = None
) -> WorksheetMigrateResponse:
    """执行工作单输入迁移并组装 200 响应；各类失败抛出指出变量/步骤的 422。"""

    chosen = store or get_store()

    # 1) 请求开始时分别固定两侧的实际指向（别名各解析一次），随后整次迁移
    #    只使用这两份独立上下文；整个过程只读，不修改定义集或别名。任一侧
    #    引用不存在都在此抛出 422（不执行任何步骤）。
    old_resolved = resolve_side(req.old, chosen)
    new_resolved = resolve_side(req.new, chosen)

    # 2) 先在旧定义下完整执行原工作单（任一步失败即 422，指出变量或步骤）
    old_run = _old_run_or_raise(req, old_resolved.scope)
    old_env = initial_env(req, old_resolved.scope)

    # 3) 把每个初始变量改写为新定义下同一单位表达式、同一物理量的精确十进制值
    migrated_variables = {
        name: _rewrite_variable(name, var, old_env[name], new_resolved.scope)
        for name, var in req.variables.items()
    }

    # 4) 组装可直接提交给 /worksheet 的新工作单：引用固定的新定义集标识
    migrated = MigratedWorksheet(
        variables=migrated_variables,
        steps=list(req.steps),
        custom_unit_set_id=new_resolved.resolved_id,
    )

    # 5) 在新定义下完整执行改写后的工作单，逐步核对内部精确量、量纲与
    #    绝对温度属性（显示值允许变化）；任一步失败或含义变化即 422
    new_req = WorksheetRequest(
        variables={
            name: Variable(value=var.value, unit=var.unit)
            for name, var in migrated_variables.items()
        },
        steps=list(req.steps),
    )
    _verify_new_run(req, old_run, new_req, new_resolved.scope)

    return WorksheetMigrateResponse(
        old_resolved_id=old_resolved.resolved_id,
        new_resolved_id=new_resolved.resolved_id,
        worksheet=migrated,
    )
