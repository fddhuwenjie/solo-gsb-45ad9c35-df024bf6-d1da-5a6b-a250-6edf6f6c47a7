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

        整个“CAS 判定 → 应用修改 → schema 重校验 → 整单重算 → 修订号递增
        → 原子落盘”在同一把锁内完成（重算只是 2～5 步精确分数运算，持锁
        开销可忽略；scope 由调用方在进入本方法前构造好，因此不会与定义集
        存储发生锁嵌套）：

        1. 当前工作单不存在：抛 :class:`RevisionNotFoundError`（404）；
        2. 当前修订号与 ``expected_revision`` 不一致：抛
           :class:`RevisionConflictError`（409），落选请求在任何重算之前
           即返回，不改变已保存内容；
        3. 把单项目修改应用到当前快照并重新做 schema 校验：非法抛 422，
           发生在任何写入之前；
        4. 用调用方提供的 ``recalculate`` 对修改后的整单从头重算（全部步骤
           结果都来自本次计算，绝不沿用旧修订的步骤结果）：业务失败抛 422，
           同样发生在任何写入之前；
        5. 修订号 +1，新输入与新逐步结果共同原子落盘；写盘失败回滚内存中
           的全部改动，已保存的输入、结果与修订号保持原样。

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

            previous = deepcopy(record)
            record["revision"] = expected_revision + 1
            record["worksheet"] = _dump_body(changed_request)
            record["results"] = [_dump_result(r) for r in results]
            try:
                self._persist_locked()
            except Exception:
                # 写盘失败：回滚到上一修订，修订与落盘必须共同生效
                self._sheets[worksheet_id] = previous
                raise
            return deepcopy(record)

    # -- 内部 -------------------------------------------------------------

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
        return {
            "revision": revision,
            "custom_unit_set_id": set_id,
            "worksheet": _dump_body(body),
            "results": [_dump_result(r) for r in parsed_results],
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
