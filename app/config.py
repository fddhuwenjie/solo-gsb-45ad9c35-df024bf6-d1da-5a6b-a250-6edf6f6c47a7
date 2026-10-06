"""全局配置。"""

from __future__ import annotations

import os

#: 十进制精度上限（有效数字位数）。输入值与输出结果均不得超过该精度，
#: 否则判定为“精度溢出”并返回 422。可通过环境变量覆盖。
MAX_PRECISION: int = int(os.environ.get("UNIT_API_MAX_PRECISION", "50"))

#: 单位幂指数允许的最大绝对值，超出视为“非法幂”。
MAX_POWER: int = int(os.environ.get("UNIT_API_MAX_POWER", "99"))

#: 已发布自定义单位定义集的持久化目录（容器重建后仍可使用，见 docker-compose
#: 挂载的命名卷）；可通过环境变量覆盖。
DATA_DIR: str = os.environ.get("UNIT_API_DATA_DIR", "/data")
