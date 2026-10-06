"""FastAPI 应用入口。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from fractions import Fraction

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .converter import (
    convert,
    convert_fraction,
    is_absolute_target,
    to_decimal_str,
)
from .custom_units import scope_for_request
from .diagnose import diagnose_worksheet
from .errors import (
    AliasConflictError,
    ConversionError,
    MigrationError,
    affine_mixing,
)
from .evaluator import evaluate, make_quantity
from .migrate import migrate_worksheet
from .parser import ParsedExpression, canonical, canonical_dim, parse_expression
from .revisions import (
    RevisionConflictError,
    RevisionNotFoundError,
    StepChange,
    VariableChange,
    get_store as get_revision_store,
    init_store as init_revision_store,
)
from .schemas import (
    BindUnitSetAliasRequest,
    ConvertRequest,
    ConvertResponse,
    ErrorResponse,
    EvaluateRequest,
    PublishUnitSetRequest,
    PublishedUnitSetResponse,
    RevisableWorksheetCreateRequest,
    RevisableWorksheetPatchRequest,
    RevisableWorksheetResponse,
    SolveRequest,
    StepResultBindingRequest,
    StoredVariable,
    UnitSetAliasInfo,
    UnitSetAliasListResponse,
    UnitSetMigrationExportRequest,
    UnitSetMigrationImportResponse,
    UnitSetMigrationPackage,
    UnitSetRef,
    WorksheetBindingInfo,
    WorksheetBody,
    WorksheetDiagnoseRequest,
    WorksheetDiagnoseResponse,
    WorksheetMigrateRequest,
    WorksheetMigrateResponse,
    WorksheetRequest,
    WorksheetResponse,
    WorksheetStep,
)
from .solver import solve
from .diagnose import resolve_side
from .unit_sets import get_store
from .units import ALIASES, PREFIXES, REGISTRY
from .worksheet import run_worksheet


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时加载磁盘上的已发布自定义单位定义集（容器重建后仍可使用）
    get_store()
    # 同样加载磁盘上的可修订工作单（容器重建后仍可读取并继续修订）
    init_revision_store()
    yield


app = FastAPI(
    title="量纲表达式与仿射单位换算 API",
    version=__version__,
    description=(
        "解析单位表达式（乘、除、整数幂），规范化量纲，支持 SI 词头与"
        "摄氏/华氏/开尔文仿射温标；/evaluate 支持带单位的变量公式求值，"
        "/solve 支持用带单位的等式反求一个未知量（未知量须处于一次线性位置），"
        "所有结果均为精确十进制，维度不兼容、未知符号、非法幂、除零、"
        "非法仿射用法、非线性方程、零系数或精度溢出时返回 422。"
        "/worksheet 可在一次请求内顺序执行 2～5 个求值/反求步骤，"
        "后续步骤按结果名引用先前步骤，步骤间传递精确数值与绝对温度属性。"
        "/worksheet-diagnose 把同一工作单分别放到旧/新两份已发布定义集"
        "（标识或活动别名）下各执行一遍，按内部精确数值、量纲与绝对温度"
        "属性逐步对照并报告首个含义变化。"
        "/worksheet-migrate 则把现有工作单的初始变量改写为在新定义集下"
        "物理量相同的精确十进制值，返回可直接交给 /worksheet 的新工作单。"
        "POST/GET/PATCH /revisable-worksheets 把现有 2～5 步工作单连同"
        "已发布定义集标识或活动别名创建为可反复修订的持久化工作单：整单"
        "成功才共同保存输入、逐步结果、创建时固定的定义集标识与初始修订号；"
        "修订须携带预期修订号且只能替换一个初始变量或一个现有步骤（名称"
        "不变），整单重算后共同保存并递增修订号；同修订并发修改最多一项"
        "成功（其余 409），计算失败 422，写盘失败回滚，容器重建后仍可"
        "读取并继续修订。"
        "POST /revisable-worksheets/{id}/bindings 把上游工作单的某个步骤"
        "结果绑定为新建下游工作单的同名初始变量：提交步骤名、上游"
        "expected_revision 与含绑定变量的下游工作单，以步骤结果的值和"
        "单位覆盖该变量，下游整单计算成功才共同保存关联与下游；两单固定"
        "同一份已发布定义集，仅一对一、不允许链式。此后修订上游时在同一"
        "临界区覆盖绑定变量、整单重算下游并把两单共同落盘（下游失败 422 "
        "或写盘失败时两单都不变）；下游可改其他变量或步骤，直接改绑定"
        "变量 422，上游不存在 404，修订号过期 409；容器重建后关联仍生效。"
        "可通过 POST /custom-unit-sets 把自定义单位发布为可复用定义集，"
        "再由四个接口凭标识选用。"
        "POST /custom-unit-set-migration/export 可一次导出 1～6 个已发布"
        "定义集及指向它们的活动别名作为迁移包，另一实例用 "
        "POST /custom-unit-set-migration/import 整包校验后导入：为各定义集"
        "生成新本地标识、迁移别名指向并返回旧→新标识映射。"
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 异常处理：领域错误与参数校验错误统一返回 422
# ---------------------------------------------------------------------------


@app.exception_handler(ConversionError)
async def conversion_error_handler(_: Request, exc: ConversionError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": {"error": exc.code, "message": exc.message}},
    )


@app.exception_handler(MigrationError)
async def migration_error_handler(_: Request, exc: MigrationError) -> JSONResponse:
    # 迁移失败：在统一 422 负载上额外指出出错的变量或步骤，不返回迁移工作单
    detail: dict = {"error": exc.code, "message": exc.message}
    if exc.variable is not None:
        detail["variable"] = exc.variable
    if exc.step_index is not None:
        detail["step_index"] = exc.step_index
        detail["step_name"] = exc.step_name
    return JSONResponse(status_code=422, content={"detail": detail})


@app.exception_handler(AliasConflictError)
async def alias_conflict_error_handler(_: Request, exc: AliasConflictError) -> JSONResponse:
    # 乐观切换冲突：当前指向与 expected_id 不一致；不改变任何指向
    detail: dict = {"error": "alias_conflict", "message": exc.message}
    if exc.current_id is not None:
        detail["current_id"] = exc.current_id
    return JSONResponse(status_code=409, content={"detail": detail})


@app.exception_handler(RevisionConflictError)
async def revision_conflict_error_handler(
    _: Request, exc: RevisionConflictError
) -> JSONResponse:
    # 可修订工作单乐观并发冲突：当前修订号与 expected_revision 不一致；
    # 不执行重算，不改变已保存的输入、结果或修订号
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "error": "revision_conflict",
                "message": exc.message,
                "current_revision": exc.current_revision,
            }
        },
    )


@app.exception_handler(RevisionNotFoundError)
async def revision_not_found_error_handler(
    _: Request, exc: RevisionNotFoundError
) -> JSONResponse:
    # 按标识读取/修订的工作单不存在
    return JSONResponse(
        status_code=404,
        content={"detail": {"error": "worksheet_not_found", "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "error": "invalid_request",
                "message": "请求参数校验失败",
                "errors": errors,
            }
        },
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@app.post(
    "/custom-unit-sets",
    response_model=PublishedUnitSetResponse,
    status_code=201,
    responses={422: {"model": ErrorResponse, "description": "任一条目非法，未发布"}},
    summary="发布可复用的自定义单位定义集",
)
def publish_unit_set_endpoint(req: PublishUnitSetRequest) -> PublishedUnitSetResponse:
    # 先按现有规则校验并展开全部条目：任一项非法即抛 422，且不写入任何定义集
    set_id = get_store().publish(req.custom_units)
    return PublishedUnitSetResponse(
        id=set_id,
        custom_units=[
            {"symbol": item.symbol, "factor": str(item.factor), "base_unit": item.base_unit}
            for item in req.custom_units
        ],
    )


@app.get(
    "/custom-unit-set-aliases",
    response_model=UnitSetAliasListResponse,
    summary="查询全部活动别名及其当前指向的定义集",
)
def list_unit_set_aliases_endpoint() -> UnitSetAliasListResponse:
    aliases = get_store().list_aliases()
    return UnitSetAliasListResponse(
        aliases=[
            UnitSetAliasInfo(alias=alias, custom_unit_set_id=target)
            for alias, target in aliases.items()
        ]
    )


@app.get(
    "/custom-unit-set-aliases/{alias}",
    response_model=UnitSetAliasInfo,
    responses={422: {"model": ErrorResponse, "description": "别名不存在"}},
    summary="查询单个活动别名当前指向的定义集",
)
def get_unit_set_alias_endpoint(alias: str) -> UnitSetAliasInfo:
    return UnitSetAliasInfo(
        alias=alias, custom_unit_set_id=get_store().get_alias(alias)
    )


@app.put(
    "/custom-unit-set-aliases/{alias}",
    response_model=UnitSetAliasInfo,
    responses={
        409: {"description": "当前指向与 expected_id 不一致，未切换"},
        422: {"model": ErrorResponse, "description": "目标定义集不存在或别名非法，未切换"},
    },
    summary="创建/切换活动别名（乐观并发：expected_id）",
)
def bind_unit_set_alias_endpoint(
    alias: str, req: BindUnitSetAliasRequest
) -> UnitSetAliasInfo:
    # 仅当当前指向与 expected_id 一致时生效（初建须为 null）；
    # 目标不存在返回 422；切换与落盘共同生效，失败不改写原指向。
    target = get_store().bind_alias(
        alias, req.custom_unit_set_id, req.expected_id
    )
    return UnitSetAliasInfo(alias=alias, custom_unit_set_id=target)


@app.post(
    "/custom-unit-set-migration/export",
    response_model=UnitSetMigrationPackage,
    responses={422: {"model": ErrorResponse, "description": "标识不存在、重复或数量越界，未导出"}},
    summary="导出定义集迁移包（1～6 个定义集及指向它们的活动别名）",
)
def export_unit_set_migration_endpoint(
    req: UnitSetMigrationExportRequest,
) -> UnitSetMigrationPackage:
    # 定义集存在性校验与别名筛选在存储层同一把锁内的同一份快照完成：
    # 并发切换别名时，包内定义与指向必然相互吻合，不会跨快照拼装。
    sets, aliases = get_store().export_package(list(req.custom_unit_set_ids))
    return UnitSetMigrationPackage(
        sets={
            set_id: list(items)
            for set_id, items in sets.items()
        },
        aliases=aliases,
    )


@app.post(
    "/custom-unit-set-migration/import",
    response_model=UnitSetMigrationImportResponse,
    status_code=201,
    responses={
        422: {
            "model": ErrorResponse,
            "description": "包内定义非法、别名引用悬空、别名非法或目标端已有"
            "同名别名：整包拒绝，不留下部分定义或别名",
        }
    },
    summary="导入定义集迁移包：生成新本地标识并迁移别名指向",
)
def import_unit_set_migration_endpoint(
    package: UnitSetMigrationPackage,
) -> UnitSetMigrationImportResponse:
    # 先按发布时完全相同的规则校验包内全部定义，再校验别名引用完整性与
    # 目标端同名冲突；任一失败整包拒绝。通过后新标识生成、别名指向与原子
    # 落盘在同一临界区共同生效，写盘失败回滚全部新增内容。
    id_map = get_store().import_package(
        sets={set_id: list(items) for set_id, items in package.sets.items()},
        aliases=dict(package.aliases),
    )
    return UnitSetMigrationImportResponse(
        package=package,
        id_map=id_map,
        aliases=[
            UnitSetAliasInfo(alias=alias, custom_unit_set_id=id_map[old_target])
            for alias, old_target in sorted(package.aliases.items())
        ],
    )


@app.post(
    "/convert",
    response_model=ConvertResponse,
    responses={422: {"model": ErrorResponse, "description": "换算失败"}},
    summary="单位换算",
)
def convert_endpoint(req: ConvertRequest) -> ConvertResponse:
    scope = scope_for_request(
        req.custom_units, req.custom_unit_set_id, req.custom_unit_set_alias
    )
    src = parse_expression(req.from_unit, scope)
    dst = parse_expression(req.to_unit, scope)
    value = convert(req.value, src, dst)
    return ConvertResponse(
        value=value,
        unit=canonical(dst.terms),
        dimension=canonical_dim(dst.dim),
    )


@app.post(
    "/evaluate",
    response_model=ConvertResponse,
    responses={422: {"model": ErrorResponse, "description": "求值/换算失败"}},
    summary="带单位的变量公式求值并换算到目标单位",
)
def evaluate_endpoint(req: EvaluateRequest) -> ConvertResponse:
    scope = scope_for_request(
        req.custom_units, req.custom_unit_set_id, req.custom_unit_set_alias
    )
    # 变量先解析单位并折叠为 SI 口径的精确量；未知单位在此即报 422
    quantities = {
        name: make_quantity(var.value, parse_expression(var.unit, scope))
        for name, var in req.variables.items()
    }
    result = evaluate(req.formula, quantities)
    dst = parse_expression(req.output_unit, scope)

    if result.absolute:
        # 绝对温标变量作为公式唯一项：走仿射换算路径
        if not is_absolute_target(dst):
            raise affine_mixing(
                "绝对温度结果只能换算到绝对温标（degC/degF/K），"
                "不能换算到温差单位或复合单位"
            )
        src = ParsedExpression(dim=result.dim, scale=Fraction(1), terms=(("K", 1),))
        value_fr = convert_fraction(result.value_si, src, dst)
    else:
        # 线性量（含温差）：以 SI 基单位口径构造源表达式
        src = ParsedExpression(dim=result.dim, scale=Fraction(1), terms=())
        value_fr = convert_fraction(result.value_si, src, dst)

    return ConvertResponse(
        value=to_decimal_str(value_fr),
        unit=canonical(dst.terms),
        dimension=canonical_dim(dst.dim),
    )


@app.post(
    "/solve",
    response_model=ConvertResponse,
    responses={422: {"model": ErrorResponse, "description": "求解失败"}},
    summary="用带单位的等式反求一个未知量",
)
def solve_endpoint(req: SolveRequest) -> ConvertResponse:
    scope = scope_for_request(
        req.custom_units, req.custom_unit_set_id, req.custom_unit_set_alias
    )
    # 变量先解析单位并折叠为 SI 口径的精确量；未知单位在此即报 422
    quantities = {
        name: make_quantity(var.value, parse_expression(var.unit, scope))
        for name, var in req.variables.items()
    }
    value_si, dim = solve(req.equation, req.solve_for, quantities)
    dst = parse_expression(req.output_unit, scope)
    if dst.affine is not None:
        raise affine_mixing(
            "求解结果只能换算到线性单位，不能换算到绝对温标（degC/degF）"
        )
    if dim is None:
        # 解恒为零且量纲不受方程约束（如 2*x = x）：接受任意线性目标单位
        value_fr = Fraction(0)
    else:
        # 线性量：以 SI 基单位口径构造源表达式
        src = ParsedExpression(dim=dim, scale=Fraction(1), terms=())
        value_fr = convert_fraction(value_si, src, dst)
    return ConvertResponse(
        value=to_decimal_str(value_fr),
        unit=canonical(dst.terms),
        dimension=canonical_dim(dst.dim),
    )


@app.post(
    "/worksheet",
    response_model=WorksheetResponse,
    responses={422: {"model": ErrorResponse, "description": "任一步骤失败；不返回部分结果"}},
    summary="按顺序执行的量纲计算工作单（2～5 步，步骤间可引用先前结果）",
)
def worksheet_endpoint(req: WorksheetRequest) -> WorksheetResponse:
    # 单位上下文在工作单开始时构造一次：三种自定义单位来源在此选择其一，
    # 活动别名也在此刻解析一次，随后所有步骤共用同一版定义（执行期间切换
    # 别名不影响本工作单）。任一步骤错误均抛 422，不返回部分结果。
    scope = scope_for_request(
        req.custom_units, req.custom_unit_set_id, req.custom_unit_set_alias
    )
    return WorksheetResponse(steps=run_worksheet(req, scope))


@app.post(
    "/worksheet-diagnose",
    response_model=WorksheetDiagnoseResponse,
    responses={
        422: {"model": ErrorResponse, "description": "请求非法或旧/新定义集（标识或别名）不存在"}
    },
    summary="工作单换版诊断：同一 2～5 步工作单分别在旧/新已发布定义集下执行并逐步对照",
)
def worksheet_diagnose_endpoint(
    req: WorksheetDiagnoseRequest,
) -> WorksheetDiagnoseResponse:
    # 开始时分别固定旧/新两侧引用的实际指向（别名各解析一次），随后两侧各用
    # 各的独立 scope 执行同一份工作单，不修改定义集或别名；按内部精确分数、
    # 量纲与绝对温度属性比较（而非显示字符串）。工作单业务错误（变量解析或
    # 某步失败）结构化出现在 200 响应中；未知定义集/别名与非法请求仍为 422。
    return diagnose_worksheet(req)


@app.post(
    "/worksheet-migrate",
    response_model=WorksheetMigrateResponse,
    response_model_exclude_none=True,
    responses={
        422: {
            "model": ErrorResponse,
            "description": "旧工作单出错、变量/步骤在新定义下不兼容、改写值无法"
            "精确十进制表示、新工作单出错或结果含义变化；响应指出变量或步骤，"
            "不返回迁移工作单",
        }
    },
    summary="工作单输入迁移：把现有 2～5 步工作单改写为在新定义集下物理量"
    "相同的等价工作单",
)
def worksheet_migrate_endpoint(req: WorksheetMigrateRequest) -> WorksheetMigrateResponse:
    # 请求开始分别固定旧/新两侧引用的实际指向（别名各解析一次，执行期间
    # 切换不混版）；先在旧定义下执行原工作单，再把每个初始变量改写为
    # 新定义下同一单位表达式、同一物理量的精确十进制值；最后用固定的新
    # 标识组装新工作单并完整复核（逐步核对内部精确量、量纲与绝对温度
    # 属性，显示值允许变化）。任何一处失败都返回 422 并指出变量或步骤，
    # 不返回迁移工作单；整个操作只读，不修改定义集或别名。
    return migrate_worksheet(req)


# ---------------------------------------------------------------------------
# 可修订工作单：创建 / 读取 / 修订
# ---------------------------------------------------------------------------


def _render_revisable_response(sheet_id: str, record: dict) -> RevisableWorksheetResponse:
    """把存储层记录渲染为可修订工作单响应（输入、结果与修订号同属一个快照）。"""

    worksheet = record["worksheet"]
    binding = record.get("binding")
    return RevisableWorksheetResponse(
        id=sheet_id,
        revision=record["revision"],
        custom_unit_set_id=record["custom_unit_set_id"],
        variables={
            name: StoredVariable(value=var["value"], unit=var["unit"])
            for name, var in worksheet["variables"].items()
        },
        steps=[
            WorksheetStep.model_validate(step) for step in worksheet["steps"]
        ],
        results=record["results"],
        binding=(
            WorksheetBindingInfo(
                upstream_worksheet_id=binding["upstream_worksheet_id"],
                step_name=binding["step_name"],
                upstream_revision=binding["upstream_revision"],
            )
            if binding is not None
            else None
        ),
    )


@app.post(
    "/revisable-worksheets",
    response_model=RevisableWorksheetResponse,
    response_model_exclude_none=True,
    status_code=201,
    responses={
        422: {
            "model": ErrorResponse,
            "description": "引用的定义集标识/别名不存在，或工作单任一步骤计算失败；"
            "不创建任何工作单",
        }
    },
    summary="创建可修订工作单：整单计算成功后保存输入、逐步结果、固定定义集标识与初始修订号",
)
def create_revisable_worksheet_endpoint(
    req: RevisableWorksheetCreateRequest,
) -> RevisableWorksheetResponse:
    # 创建开始时把引用（标识或活动别名）解析为具体的定义集标识并固定下来：
    # 此后别名再被切换，该工作单仍始终使用创建时的这一版定义。
    store = get_store()
    ref = UnitSetRef(
        custom_unit_set_id=req.custom_unit_set_id,
        custom_unit_set_alias=req.custom_unit_set_alias,
    )
    resolved = resolve_side(ref, store)

    # 整单在固定定义集下完整执行一遍：任一步失败抛 422，发生在任何写入之前，
    # 不会留下工作单。
    body = WorksheetBody.model_validate(
        req.model_dump(exclude={"custom_unit_set_id", "custom_unit_set_alias"})
    )
    results = run_worksheet(body, resolved.scope)

    # 计算成功后才生成标识、组装初始修订（revision=1）并原子落盘；写盘失败
    # 回滚刚创建的记录，已保存内容不受影响。
    sheet_id, record = get_revision_store().create(
        body, resolved.resolved_id, results
    )
    return _render_revisable_response(sheet_id, record)


@app.get(
    "/revisable-worksheets/{worksheet_id}",
    response_model=RevisableWorksheetResponse,
    response_model_exclude_none=True,
    responses={404: {"description": "工作单标识不存在"}},
    summary="按标识读取可修订工作单当前修订的输入、逐步结果与修订号",
)
def get_revisable_worksheet_endpoint(worksheet_id: str) -> RevisableWorksheetResponse:
    # 快照在存储层同一把锁内取出：并发修改进行中读到的必然是某一修订自洽的
    # 输入与结果，不会出现“新输入 + 旧结果”的混版状态。
    record = get_revision_store().snapshot(worksheet_id)
    return _render_revisable_response(worksheet_id, record)


@app.patch(
    "/revisable-worksheets/{worksheet_id}",
    response_model=RevisableWorksheetResponse,
    response_model_exclude_none=True,
    responses={
        404: {"description": "工作单标识不存在"},
        409: {
            "description": "expected_revision 与当前修订号不一致：未修改；"
            "响应带回 current_revision",
        },
        422: {
            "model": ErrorResponse,
            "description": "修改引用了不存在的变量/步骤、修改后工作单非法或整单"
            "重算任一步骤失败；已保存的输入、结果与修订号不变",
        },
    },
    summary="修订可修订工作单：乐观并发 + 单项目替换 + 整单重算 + 修订号递增",
)
def patch_revisable_worksheet_endpoint(
    worksheet_id: str, req: RevisableWorksheetPatchRequest
) -> RevisableWorksheetResponse:
    revision_store = get_revision_store()
    unit_store = get_store()

    if req.variable is not None:
        change = VariableChange(
            name=req.variable.name,
            value=req.variable.value,
            unit=req.variable.unit,
        )
    else:
        assert req.step is not None
        change = StepChange(req.step.to_step())

    def recalculate(changed_request: WorksheetRequest):
        # 用创建时固定的定义集构造 scope（始终按标识取，别名切换不影响本单）；
        # 在存储锁内被回调，但此处不触碰定义集存储的写路径，无锁嵌套问题。
        scope = unit_store.build_scope(changed_request.custom_unit_set_id)
        # 整单从新初始变量开始重跑全部步骤：结果全部来自本次计算，
        # 下游步骤不会沿用旧修订的任何结果。
        return run_worksheet(changed_request, scope)

    record = revision_store.revise(
        worksheet_id, req.expected_revision, change, recalculate
    )
    return _render_revisable_response(worksheet_id, record)


@app.post(
    "/revisable-worksheets/{worksheet_id}/bindings",
    response_model=RevisableWorksheetResponse,
    response_model_exclude_none=True,
    status_code=201,
    responses={
        404: {"description": "上游工作单标识不存在"},
        409: {
            "description": "expected_revision 与上游当前修订号不一致：未建立关联；"
            "响应带回 current_revision",
        },
        422: {
            "model": ErrorResponse,
            "description": "上游已是别人的下游/已有自己的下游（一对一、非链式）、"
            "步骤名在上游不存在或下游缺少同名初始变量、下游整单计算失败；"
            "均不创建关联或下游工作单",
        },
    },
    summary="把上游步骤结果绑定为新建下游工作单的初始变量（一对一、非链式；"
    "下游固定同一份已发布定义集）",
)
def bind_step_result_endpoint(
    worksheet_id: str, req: StepResultBindingRequest
) -> RevisableWorksheetResponse:
    # 路径标识为上游：校验 CAS 与一对一/非链式约束后，用上游当前步骤结果的
    # value/unit 整体覆盖下游同名初始变量（下游沿用上游固定的同一定义集），
    # 下游整单计算成功才在同一临界区共同保存关联与下游；任一步失败（404/409/
    # 422）都不留下关联或下游工作单。
    revision_store = get_revision_store()
    unit_store = get_store()

    body = WorksheetBody.model_validate(
        req.model_dump(exclude={"step_name", "expected_revision"})
    )

    def recalculate(downstream_request: WorksheetRequest):
        # 下游与其上游使用同一份（上游创建时固定的）已发布定义集；
        # 在存储锁内被回调，但只走定义集存储的读路径，无锁嵌套问题。
        scope = unit_store.build_scope(downstream_request.custom_unit_set_id)
        return run_worksheet(downstream_request, scope)

    downstream_id, record = revision_store.bind_downstream(
        worksheet_id,
        req.expected_revision,
        req.step_name,
        body,
        recalculate,
    )
    return _render_revisable_response(downstream_id, record)


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/units", summary="列出全部支持的单位、词头与别名")
def list_units() -> dict:
    return {
        "units": [
            {
                "symbol": u.symbol,
                "name": u.name,
                "dimension": canonical_dim(u.dim),
                "affine": u.affine,
                "prefixable": u.prefixable,
            }
            for u in REGISTRY.values()
        ],
        "prefixes": sorted(PREFIXES),
        "aliases": ALIASES,
    }


@app.get("/", summary="服务信息")
def root() -> dict[str, str]:
    return {
        "service": "量纲表达式与仿射单位换算 API",
        "version": __version__,
        "docs": "/docs",
    }
