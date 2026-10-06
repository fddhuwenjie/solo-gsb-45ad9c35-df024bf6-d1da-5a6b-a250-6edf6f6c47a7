"""领域错误定义。

换算类领域错误最终都以 HTTP 422 返回，响应体为::

    {"detail": {"error": "<错误码>", "message": "<人类可读信息>"}}

别名（active alias）的乐观切换在“当前指向与 expected_id 不一致”时以
HTTP 409 返回 :class:`AliasConflictError`，同样不改变任何指向。
"""

from __future__ import annotations


class ConversionError(Exception):
    """换算过程中的领域错误（一律映射为 HTTP 422）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class MigrationError(ConversionError):
    """工作单输入迁移失败（HTTP 422）：在统一错误负载上指出出错的变量或步骤。

    ``variable`` 非空表示某个初始变量出问题（旧定义下无法解析、新定义下
    单位不兼容、或改写值无法精确十进制表示）；``step_index``/``step_name``
    非空表示某个步骤出问题（旧工作单该步失败、改写后的工作单该步失败、
    或该步结果含义变化）。此时不返回迁移工作单。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        variable: str | None = None,
        step_index: int | None = None,
        step_name: str | None = None,
    ) -> None:
        super().__init__(code, message)
        self.variable = variable
        self.step_index = step_index
        self.step_name = step_name


class AliasConflictError(Exception):
    """别名切换冲突：当前指向与提交的 expected_id 不一致（HTTP 409）。

    此时既不切换别名指向，也不改变任何已发布定义集；响应体可携带服务端
    当前指向 ``current_id``，供调用方重新读取后重试。
    """

    def __init__(self, alias: str, current_id: str | None, expected_id: str | None) -> None:
        self.alias = alias
        self.current_id = current_id
        self.expected_id = expected_id
        self.message = (
            f"别名 {alias!r} 当前指向与 expected_id 不一致："
            f"当前为 {current_id!r}，提交的 expected_id 为 {expected_id!r}"
        )
        super().__init__(self.message)


def invalid_syntax(message: str) -> ConversionError:
    return ConversionError("invalid_syntax", message)


def unknown_symbol(symbol: str) -> ConversionError:
    return ConversionError("unknown_symbol", f"未知单位符号: {symbol!r}")


def unknown_variable(name: str) -> ConversionError:
    return ConversionError("unknown_variable", f"公式引用了未提供的变量: {name!r}")


def division_by_zero() -> ConversionError:
    return ConversionError("division_by_zero", "除数为零")


def invalid_power(message: str) -> ConversionError:
    return ConversionError("invalid_power", message)


def dimension_mismatch(src: str, dst: str) -> ConversionError:
    return ConversionError(
        "dimension_mismatch",
        f"量纲不兼容: 源量纲为 {src!r}, 目标量纲为 {dst!r}",
    )


def affine_mixing(message: str) -> ConversionError:
    return ConversionError("affine_mixing", message)


def precision_overflow(message: str) -> ConversionError:
    return ConversionError("precision_overflow", message)


def nonlinear_equation(message: str) -> ConversionError:
    return ConversionError("nonlinear_equation", message)


def no_unique_solution(message: str) -> ConversionError:
    return ConversionError("no_unique_solution", message)
