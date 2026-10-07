"""可修订工作单（revisable worksheets）的持久化存储与修订历史。

可修订工作单在 :class:`~app.schemas.WorksheetBody`（初始变量 + 2～5 个
顺序步骤）之上额外绑定**创建时固定的已发布定义集标识**，并为每次成功
修改保存一个不可变的修订快照（revision）：

- ``POST /revisable-worksheets``：提交现有 2～5 步工作单及已发布定义集
  标识或活动别名；别名在创建开始时解析一次，整单计算成功后才把输入、
  逐步结果、实际使用的定义集标识与初始修订号（1）共同落盘，返回工作单
  标识。整单不计算成功则不留下任何工作单。
- ``GET /revisable-worksheets/{id}``：按标识读取当前（最新）修订的
  输入、逐步结果与修订号。
- ``PATCH /revisable-worksheets/{id}``：携带预期修订号，只允许替换
  **一个**初始变量的值与单位，或**一个**现有步骤（按结果名定位，名称
  不变）的公式/方程及目标单位；整单重新计算成功后，新输入、新的逐步
  结果与递增的修订号在同一临界区内共同保存。

**跨工作单结果绑定（worksheets binding）**：可把一张工作单（下游）的
某个初始变量绑定为另一张工作单（上游）某一步的结果：

- ``POST /revisable-worksheets/{id}/bindings``：提交上游预期修订号、
  步骤结果名、绑定变量名及含该变量的下游工作单（变量 + 2～5 步）；
  下游与上游固定使用**同一份**已发布定义集，绑定变量提交的值与单位
  被上游步骤结果的精确值与规范单位整体覆盖，下游整单计算成功后才把
  关联与下游共同保存，返回下游标识、两单修订号与绑定结果。上游不
  存在 404、修订号过期 409（带回当前号）、其余非法 422，均不落任何
  痕迹；
- 只允许**一对一**关联：每张工作单至多作为一个关联的上游或下游，
  **不允许链式关联**（A→B 后不能再 B→C 或 C→A），也不能自关联；
- 上游随后走原 ``PATCH`` 修订：服务端用新的绑定步骤结果覆盖下游
  绑定变量、整单重算下游，并在同一临界区把两单的输入、结果与修订号
  共同落盘——下游重算失败（422）或写盘失败时两单都保持原样，读者
  绝不会看到两单的混版状态；下游自身的修订号同时 +1，绑定信息中的
  上游修订号同步为上游最新修订；
- 下游仍可修订其他变量或步骤，但直接 PATCH 绑定变量返回 422；
- 绑定信息保存在下游记录中（同一 JSON 文件），容器/进程重建后关联
  继续生效；读取下游响应中的 ``binding`` 字段可见上游标识、步骤名、
  绑定变量及对应上游修订号。

保证：

- **整单重算**：修订时从新的初始变量开始重跑全部步骤，下游步骤绝不
  沿用旧修订的结果；
- **乐观并发（CAS）**：同一修订的并发修改在同一把锁内串行——先到者
  通过校验并完成重算与提交，落选者在重算开始前即得到 409
  （:class:`RevisionConflictError`），不执行计算，也不改变任何已保存
  内容；
- **快照一致读**：读路径在同一把锁内返回某一修订完整快照，并发读者
  不会读到“新输入 + 旧结果”的混版状态；
- **定义固定**：创建时把别名解析为具体的定义集标识并保存，之后别名
  再切换也不影响该工作单（始终使用创建时固定的定义）；
- **共同生效**：计算成功后才在锁内更新内存状态并原子写盘；计算失败
  （422）发生在写入之前，写盘失败回滚内存状态——两种失败都不改变已
  保存的输入、结果或修订号；
- **容器重建**：全部状态保存在数据目录（默认 ``/data``）的单个 JSON
  文件中，采用“临时文件 + 原子替换”，进程重启/容器重建后仍可读取并
  继续修订。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .config import DATA_DIR
from .errors import ConversionError
from .schemas import (
    WorksheetBody,
    WorksheetRequest,
    WorksheetStep,
    WorksheetStepResult,
)

#: 持久化文件名（位于 :data:`~app.config.DATA_DIR` 下，与定义集存储同目录）。
STORE_FILENAME = "revisable_worksheets.json"

#: 标识前缀（revisable worksheet）+ 随机字节数。
_ID_PREFIX = "rws_"
_ID_BYTES = 16

#: 存储文件格式版本。
_STORE_VERSION = 1

#: 创建时的初始修订号。
INITIAL_REVISION = 1


def _invalid_revision_request(message: str) -> ConversionError:
    """修订请求内容非法（与请求体校验失败同为 422）。"""

    return ConversionError("invalid_request", message)


class RevisionNotFoundError(ConversionError):
    """按标识读取/修订的工作单不存在（端点层映射为 HTTP 404）。"""

    def __init__(self, worksheet_id: str) -> None:
        super().__init__("invalid_request", f"可修订工作单不存在: {worksheet_id!r}")
        self.worksheet_id = worksheet_id


class RevisionConflictError(Exception):
    """乐观并发冲突：提交的预期修订号与当前修订号不一致（HTTP 409）。

    此时不执行计算、不改变任何已保存内容；响应体带回服务端当前修订号
    ``current_revision``，供调用方重新读取后重试。
    """

    def __init__(
        self,
        worksheet_id: str,
        current_revision: int,
        expected_revision: int,
    ) -> None:
        self.worksheet_id = worksheet_id
        self.current_revision = current_revision
        self.expected_revision = expected_revision
        self.message = (
            f"工作单 {worksheet_id!r} 当前修订号为 {current_revision}，"
            f"提交的 expected_revision 为 {expected_revision}："
            "该修订已被其他修改取代，请重新读取后重试"
        )
        super().__init__(self.message)


class VariableChange:
    """替换一个初始变量的值与单位（变量名不变）。"""

    def __init__(self, name: str, value: Any, unit: str) -> None:
        self.name = name
        #: Decimal：由 schema 层保证为有限十进制且未超精度上限
        self.value = value
        self.unit = unit


class StepChange:
    """替换一个现有步骤（按结果名定位，名称不变）的公式/方程及目标单位。"""

    def __init__(self, step: WorksheetStep) -> None:
        self.step = step

    @property
    def name(self) -> str:
        return self.step.name


class RevisionStore:
    """可修订工作单的持久化存储（线程安全、原子落盘）。"""

    def __init__(self, data_dir: str = DATA_DIR) -> None:
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, STORE_FILENAME)
        self._lock = threading.RLock()
        #: 工作单标识 -> 工作单当前修订记录
        self._sheets: dict[str, dict] = {}
        self._loaded = False

    @property
    def path(self) -> str:
        """持久化文件的完整路径。"""

        return self._path

    def load(self) -> None:
        """从数据目录加载全部可修订工作单；文件不存在时以空存储启动。

        文件存在但无法解析时抛出异常使启动失败（宁可不可用也不静默丢数据）。
        """

        with self._lock:
            if not os.path.exists(self._path):
                self._sheets = {}
                self._loaded = True
                return
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
                raise ValueError(f"可修订工作单存储文件版本不被支持: {self._path}")
            sheets = raw.get("sheets")
            if not isinstance(sheets, dict):
                raise ValueError(f"可修订工作单存储文件的 sheets 字段格式非法: {self._path}")
            loaded: dict[str, dict] = {}
            for sheet_id, record in sheets.items():
                loaded[sheet_id] = self._validate_record(sheet_id, record)
            # 第二遍校验绑定引用：上游必须存在（自关联、悬空引用、链式关联
            # 在正常写路径上都不可能出现，磁盘文件被外部改坏时让启动失败，
            # 宁可不可用也不静默丢关联）
            for sheet_id, record in loaded.items():
                binding = record.get("binding")
                if binding is not None and binding["upstream_worksheet_id"] not in loaded:
                    raise ValueError(
                        f"可修订工作单 {sheet_id!r} 的绑定上游 "
                        f"{binding['upstream_worksheet_id']!r} 不存在: {self._path}"
                    )
            self._sheets = loaded
            self._loaded = True

    def count(self) -> int:
        """已保存的工作单数量（主要供测试与运维观察）。"""

        self._ensure_loaded()
        with self._lock:
            return len(self._sheets)

    def snapshot(self, worksheet_id: str) -> dict:
        """返回某工作单当前修订的**深拷贝**快照；不存在时抛出 404 领域错误。

        深拷贝在锁内完成：并发修改/落盘不会改动调用方拿到的对象，读者
        看到的必然是某一修订自洽的输入与结果。
        """

        self._ensure_loaded()
        with self._lock:
            record = self._sheets.get(worksheet_id)
            if record is None:
                raise RevisionNotFoundError(worksheet_id)
            return deepcopy(record)

    def create(
        self,
        body: WorksheetBody,
        resolved_set_id: str,
        results: list[WorksheetStepResult],
    ) -> tuple[str, dict]:
        """整单计算成功后创建工作单并原子落盘，返回 ``(标识, 新记录深拷贝)``。

        调用方必须**先**在固定的定义集上下文上完成整单计算（成功）再调用
        本方法；本方法只负责标识生成、初始修订组装与共同落盘。写盘失败时
        删除刚加入的记录并重新抛出，已保存内容不发生任何变化。
        """

        self._ensure_loaded()
        record = {
            "revision": INITIAL_REVISION,
            "custom_unit_set_id": resolved_set_id,
            "worksheet": _dump_body(body),
            "results": [_dump_result(r) for r in results],
        }
        with self._lock:
            while True:
                sheet_id = _ID_PREFIX + secrets.token_hex(_ID_BYTES)
                if sheet_id not in self._sheets:
                    break
            self._sheets[sheet_id] = deepcopy(record)
            try:
                self._persist_locked()
            except Exception:
                # 写盘失败：创建不落任何痕迹，已保存内容保持原样
                del self._sheets[sheet_id]
                raise
            record["id"] = sheet_id
            return sheet_id, deepcopy(record)

    def revise(
        self,
        worksheet_id: str,
        expected_revision: int,
        change: VariableChange | StepChange,
        recalculate: Callable[[WorksheetRequest], list[WorksheetStepResult]],
    ) -> dict:
        """在乐观并发保护下应用修改、整单重算并提交新修订。

        整个“CAS 判定 → 应用修改 → schema 重校验 → 整单重算 → （若本单是
        上游：驱动下游绑定变量并重算下游）→ 修订号递增 → 原子落盘”在同一把
        锁内完成（重算只是 2～5 步精确分数运算，持锁开销可忽略；scope 由
        调用方在进入本方法前构造好，因此不会与定义集存储发生锁嵌套）：

        1. 当前工作单不存在：抛 :class:`RevisionNotFoundError`（404）；
        2. 当前修订号与 ``expected_revision`` 不一致：抛
           :class:`RevisionConflictError`（409），落选请求在任何重算之前
           即返回，不改变已保存内容；
        3. 把单项目修改应用到当前快照并重新做 schema 校验：非法抛 422
           （含直接修改下游绑定变量），发生在任何写入之前；
        4. 用调用方提供的 ``recalculate`` 对修改后的整单从头重算（全部步骤
           结果都来自本次计算，绝不沿用旧修订的步骤结果）：业务失败抛 422，
           同样发生在任何写入之前；
        5. 若本单已被一个下游关联，则用新的绑定步骤结果（值与单位）覆盖
           下游绑定变量并把下游整单重算；绑定步骤在新修订中不存在或下游
           任一步失败抛 422，两单都不改变；下游自身修订号同时 +1，绑定
           信息中的上游修订号同步为新修订；
        6. 两单修订号 +1、输入与逐步结果共同原子落盘；写盘失败回滚内存中
           的全部改动，两单已保存的输入、结果与修订号都保持原样。

        返回新修订记录的深拷贝（含 ``id``）。
        """

        self._ensure_loaded()
        with self._lock:
            record = self._sheets.get(worksheet_id)
            if record is None:
                raise RevisionNotFoundError(worksheet_id)
            if record["revision"] != expected_revision:
                raise RevisionConflictError(
                    worksheet_id, record["revision"], expected_revision
                )
            # 修改只作用于深拷贝并重新校验；此时 record 尚未被改动
            changed_request = self._apply_change_locked(record, change)
            # 整单重算：失败抛 422，record 仍保持原修订内容
            results = recalculate(changed_request)

            # 若本单是某个一对一关联的上游，同步驱动下游：绑定步骤的新结果
            # 覆盖下游绑定变量，下游从新初始变量开始整单重算。任何失败在
            # 下面的提交之前抛出，两单都保持原修订。
            downstream_id = self._downstream_of_locked(worksheet_id)
            downstream_commit: tuple[dict, dict, list[WorksheetStepResult]] | None = None
            if downstream_id is not None:
                downstream_record = self._sheets[downstream_id]
                bound = downstream_record["binding"]
                bound_result = _result_by_name(results, bound["step_name"])
                if bound_result is None:
                    raise _invalid_revision_request(
                        f"下游绑定的步骤 {bound['step_name']!r} 在修订后的上游"
                        "工作单中不存在：步骤只能整体替换（名称不变），关联未改变"
                    )
                downstream_request = _bound_downstream_request(
                    downstream_record, bound_result
                )
                downstream_results = recalculate(downstream_request)
                downstream_commit = (
                    downstream_record,
                    _dump_body(downstream_request),
                    downstream_results,
                )

            # 先保留两单回滚副本，全部计算已成功，才进入“内存修改 + 落盘”
            previous_upstream = deepcopy(record)
            record["revision"] = expected_revision + 1
            record["worksheet"] = _dump_body(changed_request)
            record["results"] = [_dump_result(r) for r in results]
            previous_downstream = None
            if downstream_commit is not None:
                downstream_record, downstream_body, downstream_results = downstream_commit
                previous_downstream = deepcopy(downstream_record)
                downstream_record["revision"] += 1
                downstream_record["worksheet"] = downstream_body
                downstream_record["results"] = [
                    _dump_result(r) for r in downstream_results
                ]
                downstream_record["binding"]["upstream_revision"] = (
                    expected_revision + 1
                )
            try:
                self._persist_locked()
            except Exception:
                # 写盘失败：两单一起回滚到上一修订，修订与落盘必须共同生效
                self._sheets[worksheet_id] = previous_upstream
                if previous_downstream is not None:
                    self._sheets[downstream_id] = previous_downstream  # type: ignore[possibly-undefined]
                raise
            return deepcopy(record)

    def create_binding(
        self,
        upstream_id: str,
        expected_revision: int,
        step_name: str,
        bound_variable: str,
        downstream_body: WorksheetBody,
        run_downstream: Callable[[WorksheetRequest], list[WorksheetStepResult]],
    ) -> tuple[str, dict, WorksheetStepResult]:
        """校验并创建一对一结果绑定与下游工作单，共同原子落盘。

        ``run_downstream`` 由调用方提供（按上游固定的定义集构造 scope），
        与 :meth:`revise` 的重算回调一致，避免本层与定义集存储发生锁嵌套。
        全部校验与计算都在任何写入之前完成：

        1. 上游不存在：:class:`RevisionNotFoundError`（404，先于 CAS 判定）；
        2. 上游当前修订号与 ``expected_revision`` 不一致：
           :class:`RevisionConflictError`（409，带回当前号）；
        3. 步骤结果名在上游当前修订中不存在、绑定变量不在下游请求体中、
           两单定义集不是同一份、或一对一/无链式/不自关联约束被破坏：422；
        4. 用上游步骤结果（精确值与规范单位）覆盖绑定变量，整单执行下游：
           任一步失败 422；
        5. 关联与下游初始修订（1）共同原子落盘；写盘失败删除刚创建的下游，
           上游与已保存内容均不改变。

        返回 ``(下游标识, 下游新记录深拷贝, 绑定步骤结果)``。
        """

        self._ensure_loaded()
        with self._lock:
            upstream = self._sheets.get(upstream_id)
            if upstream is None:
                raise RevisionNotFoundError(upstream_id)
            if upstream["revision"] != expected_revision:
                raise RevisionConflictError(
                    upstream_id, upstream["revision"], expected_revision
                )
            bound_result = _result_by_name(
                [WorksheetStepResult.model_validate(r) for r in upstream["results"]],
                step_name,
            )
            if bound_result is None:
                raise _invalid_revision_request(
                    f"上游工作单 {upstream_id!r} 的当前修订中不存在结果名为 "
                    f"{step_name!r} 的步骤"
                )
            if bound_variable not in downstream_body.variables:
                raise _invalid_revision_request(
                    f"下游工作单中不存在要绑定的初始变量 {bound_variable!r}："
                    "绑定变量必须是下游请求体 variables 中的现有变量"
                )

            # 一对一 + 无链式 + 不自关联：上游不能已是下游，不能已绑定过别的
            # 下游；新下游使用新标识（尚不存在），但仍拒绝把既有下游再当上游
            # 的链式形态以及自关联（ID 碰撞在生成时另行规避）。
            if upstream.get("binding") is not None:
                raise _invalid_revision_request(
                    f"上游工作单 {upstream_id!r} 已作为另一张工作单的下游："
                    "不允许链式关联"
                )
            if self._downstream_of_locked(upstream_id) is not None:
                raise _invalid_revision_request(
                    f"上游工作单 {upstream_id!r} 已绑定过一个下游工作单："
                    "只允许一对一关联"
                )

            downstream_request = WorksheetRequest.model_validate(
                {
                    **downstream_body.model_dump(),
                    "custom_unit_set_id": upstream["custom_unit_set_id"],
                }
            )
            # 覆盖必须在 schema 重校验之后作用于对象（值/单位取自上游步骤结果）
            downstream_request.variables[bound_variable].value = bound_result.value
            downstream_request.variables[bound_variable].unit = bound_result.unit
            # 覆盖后再走一次与 /worksheet 相同的变量精度等校验
            downstream_results = run_downstream(downstream_request)

            while True:
                downstream_id = _ID_PREFIX + secrets.token_hex(_ID_BYTES)
                if downstream_id not in self._sheets and downstream_id != upstream_id:
                    break
            record = {
                "revision": INITIAL_REVISION,
                "custom_unit_set_id": upstream["custom_unit_set_id"],
                "worksheet": _dump_body(downstream_request),
                "results": [_dump_result(r) for r in downstream_results],
                "binding": {
                    "upstream_worksheet_id": upstream_id,
                    "step_name": step_name,
                    "variable": bound_variable,
                    "upstream_revision": expected_revision,
                },
            }
            self._sheets[downstream_id] = deepcopy(record)
            try:
                self._persist_locked()
            except Exception:
                # 写盘失败：下游不落任何痕迹，上游与已保存内容保持原样
                del self._sheets[downstream_id]
                raise
            record["id"] = downstream_id
            return downstream_id, deepcopy(record), bound_result

    # -- 内部 -------------------------------------------------------------

    def _downstream_of_locked(self, upstream_id: str) -> str | None:
        """返回与给定上游一对一关联的下游标识；无关联时为 None（调用方持锁）。"""

        for sheet_id, record in self._sheets.items():
            binding = record.get("binding")
            if binding is not None and binding["upstream_worksheet_id"] == upstream_id:
                return sheet_id
        return None

    def _apply_change_locked(
        self, record: dict, change: VariableChange | StepChange
    ) -> WorksheetRequest:
        """把单项目修改应用到当前修订快照并重新校验（调用方须持锁）。

        只允许：替换一个**已有**初始变量的值与单位（变量名不变），或按
        **现有结果名**替换该步骤的公式/方程及目标单位（步骤位置与名称
        不变，步骤可在求值/反求两种形态间替换，但仍须满足步骤自身与
        整张工作单的既有校验）。
        """

        body_raw = deepcopy(record["worksheet"])

        if isinstance(change, VariableChange):
            variables = body_raw["variables"]
            name = change.name
            if name not in variables:
                raise _invalid_revision_request(
                    f"要修改的初始变量 {name!r} 不存在：修订只能替换已有初始"
                    "变量的值与单位，不能新增、删除变量或更改变量名"
                )
            if (
                record.get("binding") is not None
                and record["binding"]["variable"] == name
            ):
                raise _invalid_revision_request(
                    f"初始变量 {name!r} 已绑定到上游工作单 "
                    f"{record['binding']['upstream_worksheet_id']!r} 的步骤 "
                    f"{record['binding']['step_name']!r} 的结果：绑定变量由上游"
                    "修订自动驱动，不能直接修改；可修订其他变量或步骤"
                )
            variables[name] = {"value": str(change.value), "unit": change.unit}
        else:
            steps = body_raw["steps"]
            target = change.name
            index = next((i for i, s in enumerate(steps) if s["name"] == target), None)
            if index is None:
                raise _invalid_revision_request(
                    f"要修改的步骤 {target!r} 不存在：修订只能按现有结果名替换"
                    "该步骤的公式/方程及目标单位，不能新增、删除或重命名步骤"
                )
            steps[index] = change.step.model_dump(exclude_none=True)

        body_raw["custom_unit_set_id"] = record["custom_unit_set_id"]
        try:
            return WorksheetRequest.model_validate(body_raw)
        except ValueError as exc:
            raise _invalid_revision_request(f"修改后的工作单不合法: {exc}") from None

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _validate_record(self, sheet_id: str, record: object) -> dict:
        """加载时校验单条记录：结构非法即让启动失败，不静默丢数据。"""

        if not isinstance(record, dict):
            raise ValueError(f"可修订工作单 {sheet_id!r} 的记录格式非法: {self._path}")
        revision = record.get("revision")
        set_id = record.get("custom_unit_set_id")
        worksheet = record.get("worksheet")
        results = record.get("results")
        if (
            not isinstance(revision, int)
            or revision < INITIAL_REVISION
            or not isinstance(set_id, str)
            or not isinstance(worksheet, dict)
            or not isinstance(results, list)
        ):
            raise ValueError(f"可修订工作单 {sheet_id!r} 的记录字段格式非法: {self._path}")
        # 用 schema 重新校验输入与结果，保证加载后立即可用于读取/继续修订
        body = WorksheetBody.model_validate(worksheet)
        parsed_results = [WorksheetStepResult.model_validate(item) for item in results]
        if len(parsed_results) != len(body.steps):
            raise ValueError(f"可修订工作单 {sheet_id!r} 的结果数与步骤数不一致: {self._path}")
        validated = {
            "revision": revision,
            "custom_unit_set_id": set_id,
            "worksheet": _dump_body(body),
            "results": [_dump_result(r) for r in parsed_results],
        }
        # 绑定信息为可选字段：存在时校验结构、上游修订号与绑定变量/步骤名
        # 仍能在本单/上游中找到（上游存在性由 load 的第二遍检查保证）
        binding = record.get("binding")
        if binding is not None:
            validated["binding"] = self._validate_binding_locked(
                sheet_id, binding, validated
            )
        return validated

    def _validate_binding_locked(
        self, sheet_id: str, binding: object, own_record: dict
    ) -> dict:
        """加载时校验下游记录中的绑定块（引用完整性）。"""

        if not isinstance(binding, dict):
            raise ValueError(f"可修订工作单 {sheet_id!r} 的绑定信息格式非法: {self._path}")
        upstream_id = binding.get("upstream_worksheet_id")
        step_name = binding.get("step_name")
        variable = binding.get("variable")
        upstream_revision = binding.get("upstream_revision")
        if (
            not isinstance(upstream_id, str)
            or not isinstance(step_name, str)
            or not isinstance(variable, str)
            or not isinstance(upstream_revision, int)
            or upstream_revision < INITIAL_REVISION
        ):
            raise ValueError(f"可修订工作单 {sheet_id!r} 的绑定字段格式非法: {self._path}")
        if variable not in own_record["worksheet"]["variables"]:
            raise ValueError(
                f"可修订工作单 {sheet_id!r} 的绑定变量 {variable!r} 不存在: {self._path}"
            )
        return {
            "upstream_worksheet_id": upstream_id,
            "step_name": step_name,
            "variable": variable,
            "upstream_revision": upstream_revision,
        }

    def _persist_locked(self) -> None:
        """把全部工作单当前修订全量写入临时文件并原子替换（调用方须持锁）。"""

        os.makedirs(self._data_dir, exist_ok=True)
        payload = {"version": _STORE_VERSION, "sheets": self._sheets}
        tmp_path = f"{self._path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)


def _dump_body(body: WorksheetBody) -> dict:
    """把工作单本体序列化为可 JSON 持久化的普通字典（Decimal -> str）。"""

    return {
        "variables": {
            name: {"value": str(var.value), "unit": var.unit}
            for name, var in body.variables.items()
        },
        "steps": [step.model_dump(exclude_none=True) for step in body.steps],
    }


def _dump_result(result: WorksheetStepResult) -> dict:
    return result.model_dump()


def _result_by_name(
    results: list[WorksheetStepResult], name: str
) -> WorksheetStepResult | None:
    """按结果名取步骤结果；不存在时返回 None。"""

    return next((r for r in results if r.name == name), None)


def _bound_downstream_request(
    downstream_record: dict, bound_result: WorksheetStepResult
) -> WorksheetRequest:
    """用上游步骤结果覆盖下游绑定变量，组装待整单重算的下游请求。

    覆盖作用在当前快照的深拷贝上并重新做 schema 校验；失败抛 422，
    发生在任何写入之前。
    """

    body_raw = deepcopy(downstream_record["worksheet"])
    variable = downstream_record["binding"]["variable"]
    body_raw["variables"][variable] = {
        "value": bound_result.value,
        "unit": bound_result.unit,
    }
    body_raw["custom_unit_set_id"] = downstream_record["custom_unit_set_id"]
    try:
        return WorksheetRequest.model_validate(body_raw)
    except ValueError as exc:
        raise _invalid_revision_request(f"覆盖绑定变量后的下游工作单不合法: {exc}") from None


# ---------------------------------------------------------------------------
# 进程内共享的存储单例；由应用启动时初始化（也可在测试中重新初始化）。
# ---------------------------------------------------------------------------

_store: RevisionStore | None = None
_store_lock = threading.RLock()


def init_store(data_dir: str = DATA_DIR) -> RevisionStore:
    """（重新）初始化进程级存储单例并加载磁盘上的可修订工作单。"""

    global _store
    with _store_lock:
        store = RevisionStore(data_dir)
        store.load()
        _store = store
    return store


def get_store() -> RevisionStore:
    """获取进程级存储单例；尚未初始化时按默认数据目录初始化。"""

    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                init_store()
    return _store
