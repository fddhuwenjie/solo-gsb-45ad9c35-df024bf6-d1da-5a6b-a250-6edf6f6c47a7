# 量纲表达式与仿射单位换算 API

基于 **Python + FastAPI + Pydantic** 的单位换算服务：解析单位表达式（乘、除、整数幂），
规范化量纲，支持 SI 词头与摄氏/华氏/开尔文仿射温标；`/evaluate` 支持对带单位的变量
公式做量纲感知的求值，`/solve` 支持用带单位的等式反求一个未知量。所有结果均为**精确十进制**。

## 特性

- **表达式解析**：`kg*m/s^2`、`km/h`、`(m/s)^2`、`J/(kg*K)`，支持 `*`、`/`、`^`（或 `**`）、括号与整数幂
- **变量公式求值（`POST /evaluate`）**：1～20 个带 `value`/`unit` 的变量 + 公式（变量、十进制定值、`+ - * /`、括号、整数幂），解析时以 `Fraction` 同步合并数值与量纲，结果精确换算到 `output_unit`
- **等式求解（`POST /solve`）**：用带单位的等式反求未知量：两侧以 `Fraction` 同步构建未知量的一次系数与常数项并校验量纲，支持 `+ - * /`、括号与纯常数幂，解精确换算到线性的 `output_unit`
- **顺序计算工作单（`POST /worksheet`）**：一次请求提交初始带单位变量与 **2～5 个步骤**，每步要么是公式求值、要么是一次线性方程反求，给出唯一结果名与目标单位；后续步骤可按结果名引用先前步骤的结果，步骤间传递**精确分数数值与绝对温度属性**，返回各步骤的 `value`/`unit`/`dimension` 供核对完整过程；任一步错误返回 **422 且不返回部分结果**
- **可修订工作单（`POST`/`GET`/`PATCH /revisable-worksheets`）**：把一份 2～5 步工作单连同**已发布定义集标识或活动别名**创建为可反复调整的持久化工作单。整单计算成功才保存输入、逐步结果、实际使用的定义集标识与初始修订号（`revision=1`）；按标识读取当前内容、结果与修订号；修订时必须携带**预期修订号**，每次只能替换一个初始变量的值与单位、或一个现有步骤（按结果名定位、名称不变）的公式/方程及目标单位，随后**整单重算**并共同保存新输入、新逐步结果与递增修订号（下游步骤不沿用旧结果）。同一修订的并发修改最多一项成功，其余返回 **409**；计算失败返回 **422**、写盘失败回滚，二者都不改变已保存内容；别名切换后仍使用创建时固定的定义；并发读取只见同一修订的输入与结果；**容器重建后仍可读取并继续修订**
- **步骤结果绑定（`POST /revisable-worksheets/{id}/bindings`）**：把一张**上游**可修订工作单的某个步骤结果绑定为新创建的**下游**工作单的同名初始变量。请求提交上游标识（路径参数）、步骤名、上游 `expected_revision` 与含同名绑定变量的下游工作单；以该步骤结果的**值和单位整体覆盖**该变量，下游整单计算成功才共同保存关联与下游（201），返回下游标识、上下游两单修订号与逐步结果。两单固定**同一份已发布定义集**（下游沿用上游创建时固定的标识），仅允许**一对一**关联、**不允许链式**关联。此后沿用 `PATCH /revisable-worksheets/{id}` 修订上游时，绑定变量自动以新步骤结果覆盖并**整单重算下游**，两单的输入、结果与修订号在同一临界区共同落盘；下游计算失败（**422**）或写盘失败时两单都不改变，并发读取不会见到混版。下游仍可修改其他变量或步骤，但直接修改绑定变量返回 **422**；上游不存在返回 **404**、上游修订号过期返回带当前号的 **409**。读取下游可见上游标识、步骤及对应修订号；**容器重建后关联仍生效**；无关联工作单行为不变

- **工作单换版诊断（`POST /worksheet-diagnose`）**：一次请求提交同一份 2～5 步工作单与旧、新两份**已发布**定义集的标识或活动别名；请求开始时分别固定两侧实际指向，用各自定义各执行一遍（只读、不改定义集或别名），逐步并排返回两侧结果与**首个含义变化**。比较按 **SI 口径精确分数数值、量纲与绝对温度属性**进行，而非显示字符串——旧版 `span=2 m`、新版 `span=3 m` 时输入 `1 span` 并输出到 `span`，两侧虽都显示 `1 span` 也会报告物理值已变。某一侧第 k 步业务失败时保留此前步骤对照并回报该步错误，失败后步骤标记 `not_run` 不计成功；未知定义集/别名或非法请求返回 422
- **工作单输入迁移（`POST /worksheet-migrate`）**：一次请求提交现有 2～5 步工作单与旧、新定义集标识或活动别名（请求开始分别固定两侧实际指向）；先在旧定义下执行，再把每个初始变量改写成**新定义下单位表达式相同、物理量（含量纲与绝对温度属性）也相同的精确十进制值**，返回可直接交给 `/worksheet` 的新工作单（引用固定的新定义集标识）。改写后整张工作单在新定义下再执行一遍，逐步核对内部精确量、量纲与绝对温度属性（显示值允许变化）；旧工作单出错、新定义下变量或步骤单位不兼容、改写值无法精确十进制表示、新工作单出错或结果含义变化时返回 **422 并指出变量或步骤**，不返回迁移工作单。操作只读，不改写定义集或别名
- **量纲规范化**：输出规范单位表达式与 SI 基本量纲表达式，相同输入永远产生相同输出
- **SI 词头**：`Y Z E P T G M k h da d c m u µ n p f a z y`（如 `km`、`mV`、`µm`）
- **仿射温标**：`degC`（°C）、`degF`（°F）、`K` 互为绝对温度换算；温差单位 `dC`/`dF`/`dK` 严格隔离，**禁止温差与绝对温度混算**；公式中的绝对温标变量只能作为唯一项
- **精确十进制**：内部使用有理数精确运算；结果不能表示为有限十进制或超过精度上限时返回 422，绝不输出舍入值
- **请求级自定义单位**：四个接口（`/convert`、`/evaluate`、`/solve`、`/worksheet`）均可携带 1～6 项 `custom_units`（`symbol`/正十进制 `factor`/`base_unit`，含义为 `1 symbol = factor 个 base_unit`），支持前向引用与链式定义；自定义符号不与内置单位/词头冲突、不自动获得词头、不能引用 `degC`/`degF`/裸 `K`，且仅在本次请求内有效（不修改全局单位表，并发安全）
- **可复用定义集发布（`POST /custom-unit-sets`）**：把 1～6 项已按上述规则校验并展开的定义发布为**可复用定义集**，服务生成不可猜测的标识；已发布内容不可修改，四个接口可凭 `custom_unit_set_id` 选用（与请求内 `custom_units` 互斥），定义落盘保存，**容器重建后仍可使用**
- **活动别名（`GET`/`PUT /custom-unit-set-aliases`）**：为已发布的不可变定义集设置一个长期不变的**别名**：调用方始终沿用同一名称，维护者把别名切换到新发布的定义集。切换采用乐观并发（`expected_id`，初建为 `null`）：当前指向不一致返回 **409**、目标不存在返回 **422**，均不改写原指向；别名在请求开始时解析一次（`/worksheet` 整个工作单只解析一次），四个接口可传 `custom_unit_set_alias`（与 `custom_units`、`custom_unit_set_id` 互斥），与定义集共同落盘，**容器重建后仍有效**
- **定义集迁移包（`POST /custom-unit-set-migration/export`、`/import`）**：通过 HTTP 一次导出 1～6 个已发布定义集及导出时指向它们的活动别名；另一实例整包校验后导入，为各定义集生成**新的本地标识**、把包内别名改指新标识，并返回旧→新标识映射。包内引用悬空、定义非法或目标端已有同名别名时**整包拒绝**，不留下部分定义或别名；导入与落盘共同生效（写盘失败回滚内存状态），导出的定义与指向取自**同一份快照**；原有定义、别名、调用不受影响，新引用容器重建后仍可使用
- **统一 422**：维度不兼容、未知变量/符号、非法幂、除零、非法仿射用法、非线性方程、零系数、精度溢出均返回 422 且不产出结果

## 快速开始

### Docker Compose（单条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口：

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 仅 Docker

```bash
docker build -t unit-convert-api .
docker run --rm -p 8000:8000 unit-convert-api
```

### 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest            # 运行测试
```

## API 说明

### `POST /convert`

请求体：

| 字段    | 类型          | 说明                                       |
| ------- | ------------- | ------------------------------------------ |
| `value` | 十进制数/字符串 | 定值；建议用字符串传递以保证十进制精确性 |
| `from`  | 字符串        | 源单位表达式                               |
| `to`    | 字符串        | 目标单位表达式                             |
| `custom_units` | 数组（可省略） | 1～6 项请求级自定义单位声明，详见下文；与 `custom_unit_set_id`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_id` | 字符串（可省略） | 已发布自定义单位定义集标识，详见下文；不存在返回 422；与 `custom_units`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_alias` | 字符串（可省略） | 已发布定义集的活动别名，详见下文；请求开始解析一次，别名不存在返回 422；与 `custom_units`、`custom_unit_set_id` 互斥 |

成功响应 `200`：

| 字段        | 说明                                 |
| ----------- | ------------------------------------ |
| `value`     | 换算结果的精确十进制表示（字符串）   |
| `unit`      | 目标单位表达式的规范形式             |
| `dimension` | 规范量纲表达式（SI 基本单位）        |

```bash
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{"value": "36", "from": "km/h", "to": "m/s"}'
```

```json
{"value": "10", "unit": "m/s", "dimension": "m/s"}
```

更多示例：

```bash
# 复合量纲 -> 导出单位
{"value": "1",    "from": "kg*m/s^2", "to": "N"}        -> {"value": "1",     "unit": "N",    "dimension": "kg*m/s^2"}
# 仿射温标
{"value": "98.6", "from": "degF",     "to": "degC"}     -> {"value": "37",    "unit": "degC", "dimension": "K"}
{"value": "0",    "from": "degC",     "to": "K"}        -> {"value": "273.15","unit": "K",    "dimension": "K"}
# 温差（线性）
{"value": "1",    "from": "dC",       "to": "dF"}       -> {"value": "1.8",   "unit": "dF",   "dimension": "K"}
# 规范化：乱序输入得到规范输出
{"value": "1",    "from": "s^-2*kg*m","to": "N"}        -> {"value": "1",     "unit": "N",    "dimension": "kg*m/s^2"}
```

### 请求级自定义单位（四个接口通用）

请求体可携带 `custom_units`，每项为 `{"symbol", "factor", "base_unit"}`，
含义为 **1 symbol = factor 个 base_unit**（`factor` 为正十进制数，建议用字符串传递）。
源、目标及变量单位都按**同一组定义**解析；允许在定义中前向引用本次尚未声明完的符号，
成功响应的 `unit` 保留自定义符号的规范表达式，`dimension` 仍为 SI 基本量纲。

```bash
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "value": "10",
    "from": "ft",
    "to": "m",
    "custom_units": [{"symbol": "ft", "factor": "0.3048", "base_unit": "m"}]
  }'
```

```json
{"value": "3.048", "unit": "m", "dimension": "m"}
```

链式定义（`furlong` 引用后声明的 `chain`，声明顺序无关）：

```bash
# 2 furlong = 2 * 10 chain = 20 * 22 m = 440 m
{"value": "2", "from": "furlong", "to": "m",
 "custom_units": [
   {"symbol": "furlong", "factor": "10",  "base_unit": "chain"},
   {"symbol": "chain",   "factor": "22",  "base_unit": "m"}
 ]}
-> {"value": "440", "unit": "m", "dimension": "m"}

# 自定义单位之间也可以互相换算
{"value": "1", "from": "mile", "to": "ft",
 "custom_units": [
   {"symbol": "mile", "factor": "1609.344", "base_unit": "m"},
   {"symbol": "ft",   "factor": "0.3048",   "base_unit": "m"}
 ]}
-> {"value": "5280", "unit": "ft", "dimension": "m"}

# 结果单位保留自定义符号的规范表达式（正指数排序，负指数以 / 连接）
{"value": "1", "from": "N*bar", "to": "kg*bar*m/s^2",
 "custom_units": [{"symbol": "bar", "factor": "100000", "base_unit": "Pa"}]}
-> {"value": "1", "unit": "bar*kg*m/s^2", "dimension": "kg^2/s^4"}
```

`/evaluate` 与 `/solve` 的用法相同——变量单位与 `output_unit` 也在该组定义下解析：

```bash
# /evaluate：以“打”为计数单位（无量纲自定义单位）
{"variables":{"a":{"value":"2","unit":"dozen"},"b":{"value":"3","unit":"dozen"}},
 "formula":"a*b","output_unit":"dozen^2",
 "custom_units":[{"symbol":"dozen","factor":"12","base_unit":"1"}]}
-> {"value":"6","unit":"dozen^2","dimension":"1"}

# /solve：x rod -> m
{"variables":{"a":{"value":"3","unit":"rod"}},
 "equation":"x = a","solve_for":"x","output_unit":"m",
 "custom_units":[{"symbol":"rod","factor":"5.0292","base_unit":"m"}]}
-> {"value":"15.0876","unit":"m","dimension":"m"}

# 温度维度只能走线性基准（带词头的 K 或 dC/dF/dK），自定义单位本身是线性的
{"value":"3","from":"mdeg","to":"mK",
 "custom_units":[{"symbol":"mdeg","factor":"2","base_unit":"mK"}]}
-> {"value":"6","unit":"mK","dimension":"K"}
```

**自定义单位规则**：

- 数量为 1～6 项；同一请求内 `symbol` 不得重名；不同请求可对同名符号给出不同定义，
  并发请求也各按本次定义计算，**不会修改全局单位表**；省略 `custom_units` 时一切照旧
- `symbol` 须以字母开头、仅含字母/数字/下划线（≤ 32 字符）；不得与内置单位、别名或
  **任何可由 SI 词头解析的符号**冲突：`m`、`kg`、`km`、`MHz`、`mL`、`l`、`°C` 等均不可声明；
  纯词头字面量 `k`、`da` 解析不出单位，可以声明
- 自定义符号**不自动获得词头**：声明了 `foo` 后 `kfoo` 仍是未知符号
- `base_unit` 为单位表达式，可引用本次声明的其他自定义符号（允许前向引用与任意长度的链），
  但**不得引用 `degC`、`degF` 或裸 `K`**（包括 `1/K`、`kg*degC` 等组合）；
  温度维度请使用 `dC`/`dF`/`dK` 或带词头的 `K`（如 `mK`），此时自定义单位是纯线性的
- 重名、循环引用（含自引用）、未知引用、非法比例（非正数/非有限十进制/超过 50 位有效数字）、
  非法符号或禁引温标均使**整次请求返回 422**——即使该符号没有被源/目标表达式实际使用

### 发布可复用的自定义单位定义集

通过 `POST /custom-unit-sets` 可把 1～6 项**现有 `custom_units` 格式**的定义发布为可复用的
定义集。服务端会先按上一节的全部规则**校验并展开每一个条目**（链式/前向引用同样立即展开）：

- 任一条目非法（重名、与内置单位/词头冲突、循环、未知引用、非法比例、禁引温标等）
  整次发布返回 **422**，**不会生成标识、不会留下任何可引用的定义集**（也不写盘）；
- 成功返回 **201** 与服务生成的标识 `id`（不可猜测），同时回显已发布条目，
  `factor` 为精确十进制字符串；
- **已发布内容不可修改**：服务不提供更新/删除接口；以相同内容再次发布会得到一个**新标识**，
  旧标识含义不变。

```bash
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{
    "custom_units": [
      {"symbol": "furlong", "factor": "10",  "base_unit": "chain"},
      {"symbol": "chain",   "factor": "22",  "base_unit": "m"},
      {"symbol": "ft",      "factor": "0.3048", "base_unit": "m"}
    ]
  }'
```

```json
{
  "id": "cus_82327f8def2025cf00cd703f12b95c8c",
  "custom_units": [
    {"symbol": "furlong", "factor": "10",     "base_unit": "chain"},
    {"symbol": "chain",   "factor": "22",     "base_unit": "m"},
    {"symbol": "ft",      "factor": "0.3048", "base_unit": "m"}
  ]
}
```

之后在 `/convert`、`/evaluate`、`/solve`、`/worksheet` 请求体中携带 `custom_unit_set_id` 即可选用该组定义，
**一次调用的源、目标与变量单位都按同一份定义解析（`/worksheet` 的所有步骤共用一份）**：

```bash
# 2 furlong = 2 * 10 chain = 20 * 22 m = 440 m
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "value": "2",
    "from": "furlong",
    "to": "m",
    "custom_unit_set_id": "cus_82327f8def2025cf00cd703f12b95c8c"
  }'
```

```json
{"value": "440", "unit": "m", "dimension": "m"}
```

`/evaluate`、`/solve` 用法相同（变量单位与 `output_unit` 也在该定义集下解析）：

```bash
# /evaluate
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"n": {"value": "10", "unit": "ft"}, "w": {"value": "2", "unit": "ft"}},
    "formula": "n*w",
    "output_unit": "ft^2",
    "custom_unit_set_id": "<已发布的标识>"
  }'
# -> {"value": "20", "unit": "ft^2", "dimension": "m^2"}

# /solve
curl -X POST http://localhost:8000/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"a": {"value": "3", "unit": "ft"}},
    "equation": "x = a",
    "solve_for": "x",
    "output_unit": "m",
    "custom_unit_set_id": "<已发布的标识>"
  }'
# -> {"value": "0.9144", "unit": "m", "dimension": "m"}
```

**选用规则**：`custom_units` 与 `custom_unit_set_id` 互斥，同时出现整次请求
返回 422；二者都缺省时四个接口保持原有行为（三种来源 `custom_units` /
`custom_unit_set_id` / `custom_unit_set_alias` 的完整选用规则见下文
[“为已发布定义集设置活动别名”](#为已发布定义集设置活动别名切换实际定义而不改调用方名称)
一节）。

- 不同定义集允许对**同名符号**给出不同含义；每次调用都构造独立的解析上下文，
  并发调用互不污染，也不会改写全局单位表；
- 定义集保存在数据目录（默认 `/data`，可用环境变量 `UNIT_API_DATA_DIR` 覆盖）的
  `custom_unit_sets.json` 中；Docker Compose 已把命名卷 `unit_sets_data` 挂载到该目录，
  **容器重建/进程重启后已发布的定义集仍可使用**；
- 示例：发布一组长度定义后把服务停掉再 `docker compose up`，原 `id` 依然可用于换算；
  重启后还可继续发布新定义集，旧定义不受影响。

### 为已发布定义集设置活动别名（切换实际定义而不改调用方名称）

已发布的定义集**不可变**：要修正定义，只能再发布一份新定义集并拿到一个**新标识**。
为了让调用方无需修改请求中的名称，服务支持在不可变定义集之上建立**活动别名**：
调用方始终传同一个 `custom_unit_set_alias`，维护者通过切换接口改变别名实际指向的
定义集标识。

**查询别名**：

```bash
# 全部别名及其当前指向（按别名排序）
curl http://localhost:8000/custom-unit-set-aliases
# {"aliases":[{"alias":"lengths","custom_unit_set_id":"cus_..."}]}

# 单个别名（不存在返回 422）
curl http://localhost:8000/custom-unit-set-aliases/lengths
# {"alias":"lengths","custom_unit_set_id":"cus_82327f8def2025cf00cd703f12b95c8c"}
```

**创建/切换别名**（`PUT /custom-unit-set-aliases/{alias}`），请求体为
`{"custom_unit_set_id", "expected_id"}`，采用**乐观并发**：

- 仅当别名当前指向与 `expected_id` 一致时切换才生效；**初建别名时当前没有指向，
  `expected_id` 必须为 `null`**；
- 当前指向与 `expected_id` 不一致时返回 **409**（响应体中带回服务端 `current_id`，
  可重新查询后重试），**不改写原指向**；
- 目标定义集标识不存在（或别名名称非法）时返回 **422**，同样**不改变任何指向**；
- 别名名称仅允许字母、数字、下划线与连字符，长度 1～64。

```bash
# 1) 发布第一版（不可变）并初建别名 lengths 指向它（expected_id 为 null）
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"ft","factor":"0.3048","base_unit":"m"}]}'
# {"id":"cus_v1_id", ...}

curl -X PUT http://localhost:8000/custom-unit-set-aliases/lengths \
  -H 'Content-Type: application/json' \
  -d '{"custom_unit_set_id":"cus_v1_id","expected_id":null}'
# 200 {"alias":"lengths","custom_unit_set_id":"cus_v1_id"}

# 2) 调用方始终使用别名，无需知道背后的标识
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{"value":"10","from":"ft","to":"m","custom_unit_set_alias":"lengths"}'
# {"value":"3.048","unit":"m","dimension":"m"}

# 3) 维护者发布修正后的第二版（旧版定义与标识保持不变）
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"ft","factor":"0.30480","base_unit":"m"}]}'
# {"id":"cus_v2_id", ...}

# 4) 携带“当前指向” cus_v1_id 作为 expected_id 完成切换；
#    若期间已被别人切换（当前不再是 cus_v1_id），本次返回 409 且不生效
curl -X PUT http://localhost:8000/custom-unit-set-aliases/lengths \
  -H 'Content-Type: application/json' \
  -d '{"custom_unit_set_id":"cus_v2_id","expected_id":"cus_v1_id"}'
# 200 {"alias":"lengths","custom_unit_set_id":"cus_v2_id"}
```

冲突与非法目标示例：

```bash
# expected_id 与当前指向不一致 -> 409，原指向不变
PUT /custom-unit-set-aliases/lengths
{"custom_unit_set_id":"cus_v3_id","expected_id":"cus_v1_id"}
# 409 {"detail":{"error":"alias_conflict",
#       "message":"别名 'lengths' 当前指向与 expected_id 不一致：……",
#       "current_id":"cus_v2_id"}}

# 目标定义集不存在 -> 422，原指向不变
PUT /custom-unit-set-aliases/lengths
{"custom_unit_set_id":"cus_does_not_exist","expected_id":"cus_v2_id"}
# 422 {"detail":{"error":"invalid_request","message":"别名 'lengths' 无法绑定到不存在的定义集: ……"}}
```

**用别名调用四个计算接口**：在 `/convert`、`/evaluate`、`/solve`、`/worksheet`
请求体中传 `custom_unit_set_alias` 即可。别名在**请求开始时解析一次**，本次请求
的源、目标与变量单位始终使用解析到的那一版定义（`/worksheet` 中所有步骤共用该版
定义）；即使别名在请求处理过程中被切换，本次请求也会**完整使用旧版或新版**，不会
跨版本混用。别名不存在整次请求返回 **422**。

```bash
# /evaluate
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"n": {"value": "10", "unit": "ft"}, "w": {"value": "2", "unit": "ft"}},
    "formula": "n*w",
    "output_unit": "ft^2",
    "custom_unit_set_alias": "lengths"
  }'
# -> {"value": "20", "unit": "ft^2", "dimension": "m^2"}

# /solve
curl -X POST http://localhost:8000/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"a": {"value": "3", "unit": "ft"}},
    "equation": "x = a",
    "solve_for": "x",
    "output_unit": "m",
    "custom_unit_set_alias": "lengths"
  }'
# -> {"value": "0.9144", "unit": "m", "dimension": "m"}
```

**别名规则**：

| 请求中的字段                         | 行为                                                             |
| ------------------------------------ | ---------------------------------------------------------------- |
| 只有 `custom_units`                  | 沿用请求级定义（原有行为，仅本次请求有效）                        |
| 只有 `custom_unit_set_id`            | 直接按该已发布定义集解析（原有行为）；标识不存在返回 422          |
| 只有 `custom_unit_set_alias`         | 请求开始解析一次别名并全程使用该版定义；**别名不存在返回 422**    |
| 三者中任意两个（或三个）同时出现     | **整次请求返回 422**（一次调用只能使用一种自定义单位来源）        |
| 三者都不带                           | 四个接口保持原有行为与响应，不使用任何自定义单位                  |

- 切换别名与落盘**共同生效**：内存指向更新与 JSON 原子写盘在同一临界区完成，
  写盘失败会回滚进程内指向（不改变原指向）后返回错误；
- 别名与定义集保存在同一个 `custom_unit_sets.json` 中，命名卷挂载方式不变，
  **容器重建/进程重启后别名仍然有效**；更早版本写出的、没有别名字段的旧文件
  也能正常加载（按空别名处理，原定义集照常可用）；
- 别名只是不可变定义集之上的一层引用：旧标识含义永不改变，切换别名不影响任何
  直接使用 `custom_unit_set_id` 的调用；不选别名（也不带其他自定义来源）的
  请求行为与以前完全一致。

### 在实例间迁移定义集与别名（导出/导入迁移包）

定义集标识由各实例在发布时随机生成，同一组定义在不同实例上的标识不同。
迁移包用于把 1～6 个**已发布定义集**连同导出时**指向它们的活动别名**整体搬到
另一实例：导入端重新校验全部定义、生成**新的本地标识**、把包内别名改指对应
新标识，并返回旧标识到新标识的映射。整个过程只走 HTTP，包体就是普通 JSON，
可保存为文件中转。

**第 1 步：在源实例导出**（`POST /custom-unit-set-migration/export`，
请求体给 1～6 个已发布定义集标识，不可重复）：

```bash
curl -X POST http://localhost:8000/custom-unit-set-migration/export \
  -H 'Content-Type: application/json' \
  -d '{
    "custom_unit_set_ids": [
      "cus_82327f8def2025cf00cd703f12b95c8c",
      "cus_a1b2c3d4e5f60718293a4b5c6d7e8f90"
    ]
  }'
```

```json
{
  "kind": "unit-api.custom-unit-set-migration",
  "version": 1,
  "sets": {
    "cus_82327f8def2025cf00cd703f12b95c8c": [
      {"symbol": "furlong", "factor": "10", "base_unit": "chain"},
      {"symbol": "chain",   "factor": "22", "base_unit": "m"},
      {"symbol": "ft",      "factor": "0.3048", "base_unit": "m"}
    ],
    "cus_a1b2c3d4e5f60718293a4b5c6d7e8f90": [
      {"symbol": "dozen", "factor": "12", "base_unit": "1"}
    ]
  },
  "aliases": {
    "lengths":  "cus_82327f8def2025cf00cd703f12b95c8c",
    "counting": "cus_a1b2c3d4e5f60718293a4b5c6d7e8f90"
  }
}
```

- `sets` 只含被选中的定义集，键为**源实例标识**，内容与发布回显完全一致；
- `aliases` 只含导出时刻**指向其中任一被选定义集**的活动别名；没有别名指向
  所选定义集时为 `{}`。定义集存在性检查与别名筛选在服务端**同一临界区的
  同一份快照**内完成——导出期间即使别名被并发切换，包内别名引用的定义也必然
  在包内，不会出现“定义来自切换前、指向来自切换后”的跨快照拼装；
- 标识不存在、重复或数量不在 1～6 个时返回 **422**，不产生包。

可把响应直接保存为文件：

```bash
curl -sS -X POST http://localhost:8000/custom-unit-set-migration/export \
  -H 'Content-Type: application/json' \
  -d '{"custom_unit_set_ids":["cus_82327f8def2025cf00cd703f12b95c8c"]}' \
  > migration-package.json
```

**第 2 步：在目标实例导入**（`POST /custom-unit-set-migration/import`，
请求体即导出响应，可原样提交）：

```bash
curl -X POST http://localhost:8001/custom-unit-set-migration/import \
  -H 'Content-Type: application/json' \
  --data-binary @migration-package.json
```

```json
{
  "package": { "...": "（回显实际导入的包）" },
  "id_map": {
    "cus_82327f8def2025cf00cd703f12b95c8c": "cus_f0e1d2c3b4a5968778695a4b3c2d1e0f"
  },
  "aliases": [
    {"alias": "lengths", "custom_unit_set_id": "cus_f0e1d2c3b4a5968778695a4b3c2d1e0f"}
  ]
}
```

- 每个定义集获得目标实例生成的**全新本地标识**（不沿用源标识）；`id_map`
  给出源标识 → 新标识的对应关系，包内别名自动指向对应的新标识；
- **整包事务**：以下任一情况整包拒绝（**422**），**不留下任何部分定义或别名**——
  包内某定义集按发布时完全相同的规则校验失败（重名/与内置单位冲突/循环/
  未知引用/禁引温标/非法比例等）、包内别名指向了不在包内的标识（悬空引用）、
  别名名称非法、**目标实例已存在任一同名别名**、`kind`/`version` 不被支持、
  定义集数量不在 1～6 个；
- 导入是**纯增量**操作：目标实例原有的定义集、别名与正在进行的调用完全不受
  影响；同一包可导入到多个实例（各自得到不同的新标识）。同名定义符号允许与
  目标端已有定义集并存——定义集之间相互独立，符号含义只在单次调用的上下文内
  生效；
- 新标识生成、别名改指与 JSON 原子落盘在同一临界区内**共同生效**：写盘失败
  会回滚全部新增的定义集与别名，内存状态也保持导入前原样；导入内容与原有内容
  写入同一个 `custom_unit_sets.json`，命名卷挂载方式不变，**容器重建/进程重启
  后迁移来的定义集与新引用仍然可用**。

**第 3 步：在目标实例按别名（或新标识）调用**，用法与源实例完全相同：

```bash
# 按迁移来的活动别名调用——调用方请求体无需改变
curl -X POST http://localhost:8001/convert \
  -H 'Content-Type: application/json' \
  -d '{"value":"2","from":"furlong","to":"m","custom_unit_set_alias":"lengths"}'
# {"value":"440","unit":"m","dimension":"m"}

# 也可直接用 id_map 中的新标识
curl -X POST http://localhost:8001/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "value":"2","from":"furlong","to":"m",
    "custom_unit_set_id":"cus_f0e1d2c3b4a5968778695a4b3c2d1e0f"
  }'
# {"value":"440","unit":"m","dimension":"m"}
```

> **在本机演示两个实例**：Compose 的容器端口与数据目录均可用环境变量覆盖，
> 再起一个实例监听 8001、使用独立命名卷即可：
>
> ```bash
> docker compose up --build -d                  # 源实例：localhost:8000
> UNIT_API_PORT=8001 \
>   docker compose -p unitapi-dst up --build -d  # 目标实例：localhost:8001
> ```
>
> 不同的 Compose 项目名（`-p unitapi-dst`）会自动使用独立的命名卷，
> 两个实例的已发布定义集互不可见——这正是迁移包的典型使用场景。

### `POST /evaluate`

对带单位的变量映射与公式求值，并把结果**精确换算**到 `output_unit`。

请求体：

| 字段          | 类型   | 说明                                                        |
| ------------- | ------ | ----------------------------------------------------------- |
| `variables`   | 对象   | 1～20 个变量映射；键为变量名，值为 `{"value", "unit"}`      |
| `formula`     | 字符串 | 求值公式（见下文语法）                                      |
| `output_unit` | 字符串 | 结果目标单位表达式                                          |
| `custom_units`| 数组（可省略） | 1～6 项请求级自定义单位声明，详见上文；与 `custom_unit_set_id`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_id` | 字符串（可省略） | 已发布自定义单位定义集标识；不存在返回 422；与 `custom_units`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_alias` | 字符串（可省略） | 已发布定义集的活动别名；请求开始解析一次，别名不存在返回 422；与 `custom_units`、`custom_unit_set_id` 互斥 |

变量名须以字母或下划线开头，仅含字母、数字、下划线（≤32 字符）；
`value` 同样建议以 JSON 字符串传递以保证十进制精确性。成功响应与 `/convert` 相同。

```bash
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {
      "v": {"value": "60", "unit": "km/h"},
      "t": {"value": "2",  "unit": "h"}
    },
    "formula": "v*t",
    "output_unit": "km"
  }'
```

```json
{"value": "120", "unit": "km", "dimension": "m"}
```

更多示例：

```bash
# 同量纲相加（自动折算不同词头）
{"variables":{"a":{"value":"1","unit":"m"},"b":{"value":"50","unit":"cm"}},
 "formula":"a+b","output_unit":"m"}
-> {"value":"1.5","unit":"m","dimension":"m"}

# 牛顿第二定律
{"variables":{"m":{"value":"2","unit":"kg"},"a":{"value":"3","unit":"m/s^2"}},
 "formula":"m*a","output_unit":"N"}
-> {"value":"6","unit":"N","dimension":"kg*m/s^2"}

# (v^2-u^2)/(2*a)，负指数、括号、十进制定值均可
{"variables":{"v":{"value":"20","unit":"m/s"},"u":{"value":"10","unit":"m/s"},
              "a":{"value":"5","unit":"m/s^2"}},
 "formula":"(v^2-u^2)/(2*a)","output_unit":"m"}
-> {"value":"30","unit":"m","dimension":"m"}

# 绝对温标变量只能作为公式唯一项（可整体加括号），然后正常仿射换算
{"variables":{"T":{"value":"98.6","unit":"degF"}},"formula":"T","output_unit":"degC"}
-> {"value":"37","unit":"degC","dimension":"K"}

# 温差是线性量，可以加减
{"variables":{"a":{"value":"10","unit":"dC"},"b":{"value":"5","unit":"dC"}},
 "formula":"a-b","output_unit":"dF"}
-> {"value":"9","unit":"dF","dimension":"K"}
```

**求值规则**：

- 解析时用 `Fraction` 同步合并数值与量纲：乘除量纲指数加减；只有**同量纲**项才能相加/相减
- 幂指数必须是**无量纲整数**（字面整数或无量纲整数变量，|n| ≤ 99），如 `x^2`、`f^-1`；`2^x`（x 有量纲）、`x^0.5` 返回 422
- 不允许隐式相乘：`2 x`、`x y`、`x(y)` 均为语法错误
- 除零（含 `0^-n`）返回 422 `division_by_zero`
- 绝对温标变量（单位为裸 `degC`/`degF`/`K`）**只能作为公式唯一项**：`T+a`、`T*2`、`-T`、`T^1`、`T/1` 均返回 422 `affine_mixing`；且其结果只能换算到绝对温标（不能换算到 `dC`/`dF`/`dK`、`mK` 或复合单位）
- 温度维度下，温差变量（`dC`/`dF`/`dK`、带词头 K 等）可以正常参与线性运算；温差结果不能换算到绝对温标
- 公式中的十进制定值视为无量纲，有效数字同样受 50 位上限约束

### `POST /solve`

用带单位的等式反求未知量 `solve_for`：等式两侧分别解析为关于未知量的一次仿射形式
（`Fraction` 精确系数 + 常数项，量纲同步校验），解
`(左系数 - 右系数)·x = 右常数 - 左常数`，并把解**精确换算**到 `output_unit`。

请求体：

| 字段          | 类型   | 说明                                                        |
| ------------- | ------ | ----------------------------------------------------------- |
| `variables`   | 对象   | 0～20 个变量映射（可省略）；键为变量名，不得包含 `solve_for` |
| `equation`    | 字符串 | 含**恰好一个等号**的等式，两侧语法同 `/evaluate` 的公式      |
| `solve_for`   | 字符串 | 待解未知量的名称（不得出现在 `variables` 中）                |
| `output_unit` | 字符串 | 结果目标单位表达式，**须为线性单位**（不能是 `degC`/`degF`） |
| `custom_units`| 数组（可省略） | 1～6 项请求级自定义单位声明，详见上文；与 `custom_unit_set_id`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_id` | 字符串（可省略） | 已发布自定义单位定义集标识；不存在返回 422；与 `custom_units`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_alias` | 字符串（可省略） | 已发布定义集的活动别名；请求开始解析一次，别名不存在返回 422；与 `custom_units`、`custom_unit_set_id` 互斥 |

成功响应与 `/convert` 相同（`value`/`unit`/`dimension`）。

```bash
curl -X POST http://localhost:8000/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {
      "v": {"value": "60",  "unit": "km/h"},
      "s": {"value": "120", "unit": "km"}
    },
    "equation": "v*t = s",
    "solve_for": "t",
    "output_unit": "h"
  }'
```

```json
{"value": "2", "unit": "h", "dimension": "s"}
```

更多示例：

```bash
# 无需变量的纯数值方程
{"equation":"2*x = 10","solve_for":"x","output_unit":"1"}
-> {"value":"5","unit":"1","dimension":"1"}

# 牛顿第二定律反求加速度
{"variables":{"F":{"value":"6","unit":"N"},"m":{"value":"2","unit":"kg"}},
 "equation":"F = m*a","solve_for":"a","output_unit":"m/s^2"}
-> {"value":"3","unit":"m/s^2","dimension":"m/s^2"}

# 未知量可出现在等式两侧
{"variables":{"a":{"value":"3","unit":"m"},"b":{"value":"7","unit":"m"}},
 "equation":"x + a = b - x","solve_for":"x","output_unit":"m"}
-> {"value":"2","unit":"m","dimension":"m"}

# 温差是线性量，可正常参与求解
{"variables":{"a":{"value":"5","unit":"dC"},"b":{"value":"12","unit":"dC"}},
 "equation":"x + a = b","solve_for":"x","output_unit":"dF"}
-> {"value":"12.6","unit":"dF","dimension":"K"}

# 零值因子/相消子式掩盖不了未知量：结构非线性，返回 422
{"equation":"0*x*x+x = 1","solve_for":"x","output_unit":"1"}
-> 422 {"detail":{"error":"nonlinear_equation",
        "message":"含未知量的子式相乘导致次数超过一次（非线性）"}}

{"equation":"(x-x)^2+x = 1","solve_for":"x","output_unit":"1"}
-> 422 {"detail":{"error":"nonlinear_equation",
        "message":"未知量不能参与幂运算（非线性）"}}
```

**求解规则**：

- `equation` 必须恰好含一个 `=`；`solve_for` 不得出现在 `variables` 中（违反返回 422 `invalid_request`）
- 未知量只能处于**一次线性位置**：`x*x`、`(x+1)*(x-1)`、`1/x`、`x^2`、`2^x` 等均返回 422 `nonlinear_equation`；幂只允许作用于纯常数子式（如 `2^3`、`2^-2`）
- 是否“含未知量”按**结构**判定：零值因子（`0*x`）或相消子式（`x-x`）即使数值为 0 也掩盖不了未知量，`0*x*x`、`(x-x)^2`、`1/(x-x)` 同样返回 422 `nonlinear_equation`；加减、取负与括号嵌套都会保留这一结构标记
- 未知量系数为零时方程**无解**（两侧常数项不相等，如 `x = x+1`）或**有无穷多解**（恒等式，如 `x = x`），均返回 422 `no_unique_solution`
- 加减与等式两侧的量纲同步校验；解与 `output_unit` 量纲不兼容返回 422 `dimension_mismatch`
- 绝对温标变量（裸 `degC`/`degF`/`K`）不能出现在等式中（422 `affine_mixing`）；`output_unit` 必须是线性单位（不能是 `degC`/`degF`）
- 除零返回 422 `division_by_zero`；解不是有限十进制或超过精度上限返回 422 `precision_overflow`
- 解恒为零且量纲不受方程约束时（如 `2*x = x`），接受任意线性目标单位，结果为 `0`

### `POST /worksheet`

在一次请求内**按顺序执行 2～5 个步骤**的量纲计算工作单：初始提交 0～20 个带
`value`/`unit` 的变量，每个步骤给出唯一结果名 `name`、目标单位 `output_unit`，
以及**公式求值**或**一次线性方程反求**所需内容；后续步骤可按结果名引用先前
步骤的结果。响应按提交顺序返回每一步的 `value`/`unit`/`dimension`，供调用方
核对完整计算过程。

请求体：

| 字段          | 类型   | 说明                                                        |
| ------------- | ------ | ----------------------------------------------------------- |
| `variables`   | 对象（可省略） | 0～20 个初始带单位变量，规则同 `/evaluate`；步骤结果名不得与之重名 |
| `steps`       | 数组   | **2～5 个步骤**，按提交顺序执行；每步字段见下表             |
| `custom_units`| 数组（可省略） | 1～6 项请求级自定义单位声明，详见上文；与 `custom_unit_set_id`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_id` | 字符串（可省略） | 已发布定义集标识，规则同三个既有接口；不存在返回 422 |
| `custom_unit_set_alias` | 字符串（可省略） | 活动别名；**整个工作单只解析一次**，即使执行期间别名被切换，各步骤也使用同一版定义；不存在返回 422 |

每个步骤的字段（两种步骤恰好出现一种）：

| 字段          | 类型   | 说明                                                        |
| ------------- | ------ | ----------------------------------------------------------- |
| `name`        | 字符串 | 本步骤结果名（变量名词法，≤32 字符）；整个工作单唯一且不得与初始变量重名 |
| `output_unit` | 字符串 | 结果目标单位表达式；反求步骤须为线性单位（不能是 `degC`/`degF`） |
| `formula`     | 字符串 | 求值步骤：公式语法同 `/evaluate`                            |
| `equation` + `solve_for` | 字符串 | 反求步骤：等式与未知量，规则同 `/solve`（`equation` 恰好一个等号） |

```bash
curl -X POST http://localhost:8000/worksheet \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {
      "v":  {"value": "60",   "unit": "km/h"},
      "t":  {"value": "2",    "unit": "h"},
      "T0": {"value": "98.6", "unit": "degF"}
    },
    "steps": [
      {"name": "s",  "formula": "v*t",            "output_unit": "km"},
      {"name": "th", "equation": "v*th = s", "solve_for": "th", "output_unit": "h"},
      {"name": "T",  "formula": "T0",             "output_unit": "degC"}
    ]
  }'
```

```json
{
  "steps": [
    {"name": "s",  "value": "120", "unit": "km",   "dimension": "m"},
    {"name": "th", "value": "2",   "unit": "h",    "dimension": "s"},
    {"name": "T",  "value": "37",  "unit": "degC", "dimension": "K"}
  ]
}
```

**工作单规则**：

- 步骤严格按提交顺序执行：公式/等式只能引用初始变量与**先前步骤**的结果；
  引用尚未执行的步骤返回 422 `unknown_variable`
- 结果名必须唯一，且**不能覆盖**初始变量或先前结果名；`solve_for` 也不得与
  初始变量/先前结果重名（违反返回 422 `invalid_request`，在执行前校验）
- 步骤间传递的是内部 **SI 口径精确分数与绝对温度属性**（不是上一步渲染出的
  十进制字符串），因此中间步骤无任何精度损失；某一步的结果是绝对温度时，
  后续步骤仍只能把它作为公式唯一项使用，仿射规则与 `/evaluate` 完全一致
- 求值与反求各自**沿用现有的量纲、仿射及精度规则**；任一步失败
  （未知变量/符号、量纲不兼容、除零、非线性、零系数、仿射混算、精度溢出等）
  整次请求返回 **422，不返回任何部分结果**
- 单位上下文（请求内 `custom_units` / 已发布定义集 / 活动别名）在工作单开始时
  构造一次，三种来源的互斥规则与三个既有接口一致；活动别名在此时解析一次，
  之后所有步骤（变量单位、公式中的引用单位、各步 `output_unit`）都使用同一版
  定义，执行期间切换别名不会造成跨版本混用
- 上一步的 `output_unit` 只影响该步响应的 `value`/`unit` 呈现；后续步骤拿到的
  始终是精确的 SI 口径数值，可自由换用其他目标单位

### `POST /worksheet-diagnose`

**工作单换版诊断**：在不修改任何定义集或别名的前提下，把同一份现有工作单
（变量与 **2～5 个步骤**，结构与 `POST /worksheet` 的请求体一致，但不含
`custom_units`/`custom_unit_set_id`/`custom_unit_set_alias`）分别放到
**旧版**与**新版**两份已发布定义集下各执行一遍，并排返回两侧每一步的结果、
错误与**首个物理含义变化**，用于换版（重新发布 + 切换别名）前核对。

请求体：

| 字段        | 类型 | 说明                                                                 |
| ----------- | ---- | -------------------------------------------------------------------- |
| `variables` | 对象（可省略） | 0～20 个初始带单位变量，规则同 `/worksheet`                    |
| `steps`     | 数组 | **2～5 个步骤**，格式与 `/worksheet` 完全一致                        |
| `old`       | 对象 | 旧版定义集引用：`{"custom_unit_set_id": "..."}` 或 `{"custom_unit_set_alias": "..."}`（二者恰好一个） |
| `new`       | 对象 | 新版定义集引用：格式同 `old`                                         |

两侧引用在**请求开始时各解析一次并固定实际指向**：即使别名在请求处理过程中
被切换，同侧的初始变量解析与全部步骤也始终使用开始时解析到的那一版定义，
不会出现同侧步骤跨版本混用；响应的 `resolved_id` 回显固定下来的实际标识。
整个诊断为只读操作，不修改定义集或别名。

**比较按内部物理含义进行，不按显示字符串**：两侧共同成功的同一步骤比较

1. SI 基单位口径的**精确分数数值**（步骤间传递的就是这个值）；
2. **量纲**（维度向量）；
3. **绝对温度属性**（结果是否为绝对温度）。

因此“显示相同但物理值已变”也会被检出：旧版 `1 span = 2 m`、新版
`1 span = 3 m` 时，输入 `1 span`、输出单位仍为 `span`，两侧该步都显示
`1 span`，但内部值分别为 `2 m` 与 `3 m`，仍报告 `value` 差异。

成功响应 **200**：

| 字段 | 说明 |
| ---- | ---- |
| `old` / `new` | 两侧各自的 `reference`（请求原样回显）、`resolved_id`（固定后的实际标识）、`setup_error`（初始变量在该侧定义下解析失败时的错误）与 `steps` |
| `old.steps[]` / `new.steps[]` | 与提交顺序一致的逐步情况：`status` 为 `ok`（含 `value`/`unit`/`dimension`）、`error`（含 `error`/`message`）或 `not_run`（该侧此前已失败，本步未执行、不计成功） |
| `steps[]` | 并排对照：`index`/`name` 下的 `old`、`new`（结构同上）、`meaning_changed` 与 `differences`（仅两侧该步都成功时才比较；取值为 `value`/`dimension`/`absolute` 的子集） |
| `first_meaning_change` | 首个含义变化步骤的 `index`/`name`/`differences`；两侧全部成功且含义一致（或没有共同成功的步骤）时为 `null` |

若某一侧在第 k 步因量纲冲突等业务错误失败：该侧此前步骤的成功对照照常返回，
第 k 步回报错误码与信息（与 `POST /worksheet` 的错误码相同，如
`dimension_mismatch`、`unknown_symbol`、`division_by_zero`、
`nonlinear_equation`、`precision_overflow` 等），其后步骤为 `not_run`；
另一侧不受影响、可继续执行到结束。注意这与原 `POST /worksheet` 不同：
工作单业务错误在诊断中是 **200 响应内的结构化数据**，不是 HTTP 422。

**HTTP 422** 只用于请求级错误：旧/新引用的定义集标识或活动别名不存在、
引用里 `id` 与 `alias` 同时给（或都不给）、别名名称非法、步骤数不在 2～5、
步骤类型/名称非法等——此时不执行任何步骤。

```bash
# 1) 发布旧版（1 span = 2 m）与新版（1 span = 3 m）
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"span","factor":"2","base_unit":"m"}]}'
# -> {"id":"cus_v1", ...}
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"span","factor":"3","base_unit":"m"}]}'
# -> {"id":"cus_v2", ...}

# 2) 同一份工作单分别按旧版、新版执行
curl -X POST http://localhost:8000/worksheet-diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"x": {"value": "1", "unit": "span"}},
    "steps": [
      {"name": "a", "formula": "x", "output_unit": "span"},
      {"name": "b", "formula": "x", "output_unit": "m"}
    ],
    "old": {"custom_unit_set_id": "cus_v1"},
    "new": {"custom_unit_set_id": "cus_v2"}
  }'
```

```json
{
  "old": {
    "reference": {"custom_unit_set_id": "cus_v1", "custom_unit_set_alias": null},
    "resolved_id": "cus_v1",
    "setup_error": null,
    "steps": [
      {"status": "ok", "value": "1", "unit": "span", "dimension": "m", "error": null},
      {"status": "ok", "value": "2", "unit": "m", "dimension": "m", "error": null}
    ]
  },
  "new": {
    "reference": {"custom_unit_set_id": "cus_v2", "custom_unit_set_alias": null},
    "resolved_id": "cus_v2",
    "setup_error": null,
    "steps": [
      {"status": "ok", "value": "1", "unit": "span", "dimension": "m", "error": null},
      {"status": "ok", "value": "3", "unit": "m", "dimension": "m", "error": null}
    ]
  },
  "steps": [
    {"index": 0, "name": "a",
     "old": {"status": "ok", "value": "1", "unit": "span", "dimension": "m", "error": null},
     "new": {"status": "ok", "value": "1", "unit": "span", "dimension": "m", "error": null},
     "meaning_changed": true, "differences": ["value"]},
    {"index": 1, "name": "b",
     "old": {"status": "ok", "value": "2", "unit": "m", "dimension": "m", "error": null},
     "new": {"status": "ok", "value": "3", "unit": "m", "dimension": "m", "error": null},
     "meaning_changed": true, "differences": ["value"]}
  ],
  "first_meaning_change": {"index": 0, "name": "a", "differences": ["value"]}
}
```

注意第 0 步两侧显示都是 `1 span`，`meaning_changed` 仍为 `true`
（`differences: ["value"]`）——物理值已从 `2 m` 变为 `3 m`。

用活动别名引用两侧同样可以（请求开始即固定，执行中切换不影响同侧）：

```bash
curl -X POST http://localhost:8000/worksheet-diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"x": {"value": "1", "unit": "span"}},
    "steps": [
      {"name": "a", "formula": "x", "output_unit": "span"},
      {"name": "b", "formula": "x", "output_unit": "m"}
    ],
    "old": {"custom_unit_set_alias": "span_prev"},
    "new": {"custom_unit_set_alias": "span_next"}
  }'
```

一侧某步业务失败的响应片段（旧版第 1 步量纲冲突，其后不执行；新版正常）：

```json
"old": {
  "resolved_id": "cus_v1", "setup_error": null,
  "steps": [
    {"status": "ok", "value": "2", "unit": "m", "dimension": "m", "error": null},
    {"status": "error", "value": null, "unit": null, "dimension": null,
     "error": {"error": "dimension_mismatch", "message": "量纲不兼容: ……"}}
  ]
}
```

### `POST /worksheet-migrate`

**工作单输入迁移**：把一份在旧版定义集下可正常执行的现有工作单（初始变量 +
**2～5 个步骤**，结构与 `POST /worksheet` 的请求体一致，但不含
`custom_units`/`custom_unit_set_id`/`custom_unit_set_alias`）改写为在**新版**
已发布定义集下物理量完全相同的等价工作单，一次请求完成全部核对：

1. 请求开始时分别**固定**旧/新两侧引用的实际定义集标识（`old`/`new` 各为
   `{"custom_unit_set_id": "..."}` 或 `{"custom_unit_set_alias": "..."}`，
   二者恰好一个；别名各解析一次，执行期间切换别名不会造成同侧混版）；
2. **先在旧定义下完整执行**原工作单——旧工作单本身出错即返回 422；
3. 把每个**初始变量**改写为：同一单位表达式在新定义下解析，量纲与绝对温度
   属性不变，数值换算为该单位在新定义下的**精确十进制值**；
4. 用固定的新定义集**标识**组装新工作单并在新定义下**完整复核一遍**：
   逐步核对内部精确量（SI 分数数值、量纲、绝对温度属性）与旧执行一致——
   **显示值允许变化**；
5. 全部通过后返回可直接交给 `POST /worksheet` 的新工作单。

整个操作为只读，不修改定义集或别名。

成功响应 **200**：

| 字段 | 说明 |
| ---- | ---- |
| `old_resolved_id` / `new_resolved_id` | 请求开始时固定的两侧实际定义集标识 |
| `worksheet` | 可直接提交给 `POST /worksheet` 的新工作单：`variables`（单位表达式不变、数值已改写）、`steps`（与原工作单一致）、`custom_unit_set_id`（固定的新标识，而非别名——此后别名再被切换也不影响它） |

**换版成功的完整调用示例**：

```bash
# 1) 旧版：1 span = 2 m；修正后的新版：1 span = 4 m
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"span","factor":"2","base_unit":"m"}]}'
# -> {"id":"cus_v1", ...}
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"span","factor":"4","base_unit":"m"}]}'
# -> {"id":"cus_v2", ...}

# 2) 迁移现有工作单（x = 3 span 的物理量是 6 m，新版下应改写为 1.5 span）
curl -X POST http://localhost:8000/worksheet-migrate \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"x": {"value": "3", "unit": "span"}},
    "steps": [
      {"name": "a", "formula": "x*2", "output_unit": "span"},
      {"name": "b", "formula": "a",   "output_unit": "m"}
    ],
    "old": {"custom_unit_set_id": "cus_v1"},
    "new": {"custom_unit_set_id": "cus_v2"}
  }'
```

```json
{
  "old_resolved_id": "cus_v1",
  "new_resolved_id": "cus_v2",
  "worksheet": {
    "variables": {"x": {"value": "1.5", "unit": "span"}},
    "steps": [
      {"name": "a", "output_unit": "span", "formula": "x*2"},
      {"name": "b", "output_unit": "m", "formula": "a"}
    ],
    "custom_unit_set_id": "cus_v2"
  }
}
```

```bash
# 3) 响应中的 worksheet 可直接交给 /worksheet（引用固定的新标识 cus_v2）
curl -X POST http://localhost:8000/worksheet \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"x": {"value": "1.5", "unit": "span"}},
    "steps": [
      {"name": "a", "formula": "x*2", "output_unit": "span"},
      {"name": "b", "formula": "a",   "output_unit": "m"}
    ],
    "custom_unit_set_id": "cus_v2"
  }'
# {"steps":[{"name":"a","value":"3","unit":"span","dimension":"m"},
#           {"name":"b","value":"12","unit":"m","dimension":"m"}]}
```

注意第 0 步显示值从旧定义下的 `6 span` 变为 `3 span`（显示值允许变化），
而第 1 步折到内置单位 `m` 两侧都是 `12`——物理量保持一致。

**HTTP 422**（不返回迁移工作单；响应在统一 `detail` 上额外指出出错的
`variable`，或 `step_index`/`step_name`）：

- 旧工作单出错：初始变量在旧定义下无法解析（指出变量），或某步业务失败
  （指出步骤，错误码与 `POST /worksheet` 相同）；
- 初始变量的单位在新定义下无法解析、量纲或绝对温度属性不一致
  （指出变量）；
- 改写值无法精确十进制表示（或超出输入精度上限）：`precision_overflow`
  （指出变量）——例如新版改为 `1 span = 3 m` 时，`x = 1 span` 的物理量
  是 `2 m`，改写成 `span` 为 `2/3`，无法精确表示：

```json
{"detail": {"error": "precision_overflow",
            "message": "初始变量 'x' 的改写值无法表示为精确十进制: ……",
            "variable": "x"}}
```

- 改写后的工作单在新定义下某步失败（如步骤目标单位在新版下不存在或
  量纲不兼容、显示值无法精确表示），指出步骤；
- 某步结果含义变化（内部精确数值/量纲/绝对温度属性与旧执行不一致）：
  `meaning_changed`，指出步骤；
- 旧/新引用的定义集标识或活动别名不存在、引用里 `id` 与 `alias` 同时给
  （或都不给）、步骤数不在 2～5 等请求级错误：`invalid_request`。

### 可修订工作单（`POST`/`GET`/`PATCH /revisable-worksheets`）

工作单往往需要反复调整输入或步骤。**可修订工作单**把一份现有 2～5 步工作单
（结构同 `POST /worksheet` 的变量与步骤）与**一份已发布定义集**（标识或活动
别名）一起持久化保存，并为每次成功修改保留一个自洽的整单快照：当前输入、
全部步骤的逐步结果、实际使用的定义集标识与一个单调递增的**修订号**
（`revision`）。

核心约定：

- **整单成功才保存**：创建时先用固定定义把整单执行一遍，任一步失败返回
  **422**，不创建工作单、不写盘；
- **创建时固定定义**：请求给 `custom_unit_set_id` 或 `custom_unit_set_alias`
  （恰好一个）；别名在创建开始时解析一次，保存的是解析到的具体标识——此后
  别名再切换，该工作单（及其后续修订）仍始终使用创建时的定义；
- **修订只改一处**：每次修订只能替换**一个**初始变量的 `value` 与 `unit`
  （变量名不变），或按**现有步骤结果名**替换该步骤的公式/方程及目标单位
  （步骤位置与名称不变；步骤可在求值与反求两种形态间替换，仍须满足工作单
  全部既有校验）；不能新增/删除/重命名变量或步骤；
- **整单重算**：修订成功后从新输入开始重跑全部步骤，逐步结果全部来自本次
  计算——下游步骤绝不会沿用旧修订的结果；
- **乐观并发**：修订必须携带读取时的 `expected_revision`；同一修订的并发
  修改最多一项成功，其余返回 **409**（响应带回 `current_revision`，重新读取
  后重试即可），落选请求不执行重算；
- **失败不留痕**：修改非法或整单重算任一步失败返回 **422**，发生在任何写入
  之前；写盘失败回滚内存状态——两种失败都不改变已保存的输入、结果或修订号；
- **快照一致读**：读取在同一临界区返回完整快照，并发读者不会看到
  “新输入 + 旧结果”的混版状态；
- **容器重建**：工作单保存在数据目录（默认 `/data`）的
  `revisable_worksheets.json` 中（与定义集同目录、命名卷挂载方式不变），
  容器/进程重建后仍可读取并继续修订。

**第 1 步：创建（`POST /revisable-worksheets`，成功 201）**

```bash
# 先发布一份定义集（也可以直接使用既有标识；用别名同理）
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"ft","factor":"0.3048","base_unit":"m"}]}'
# -> {"id":"cus_...", ...}

curl -X POST http://localhost:8000/revisable-worksheets \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {
      "v": {"value": "60", "unit": "km/h"},
      "t": {"value": "2",  "unit": "h"}
    },
    "steps": [
      {"name": "s",  "formula": "v*t",            "output_unit": "km"},
      {"name": "th", "equation": "v*th = s", "solve_for": "th", "output_unit": "h"}
    ],
    "custom_unit_set_id": "cus_..."
  }'
```

```json
{
  "id": "rws_1f2e3d4c5b6a79887766554433221100",
  "revision": 1,
  "custom_unit_set_id": "cus_...",
  "variables": {
    "v": {"value": "60", "unit": "km/h"},
    "t": {"value": "2",  "unit": "h"}
  },
  "steps": [
    {"name": "s",  "formula": "v*t",           "output_unit": "km"},
    {"name": "th", "equation": "v*th = s", "solve_for": "th", "output_unit": "h"}
  ],
  "results": [
    {"name": "s",  "value": "120", "unit": "km", "dimension": "m"},
    {"name": "th", "value": "2",   "unit": "h",  "dimension": "s"}
  ]
}
```

用别名创建时把 `custom_unit_set_id` 换成 `"custom_unit_set_alias": "lengths"`
即可；响应中 `custom_unit_set_id` 是创建开始时固定下来的实际标识。引用的
标识/别名不存在、两个引用同时给（或都不给）、或工作单任一步计算失败均为
**422**，不创建工作单。

**第 2 步：读取（`GET /revisable-worksheets/{id}`）**

```bash
curl http://localhost:8000/revisable-worksheets/rws_1f2e3d4c5b6a79887766554433221100
```

返回与创建成功相同的结构（当前修订的 `revision`、`variables`、`steps` 与
同属这一修订的 `results`）；标识不存在返回 **404**。

**第 3 步：修订（`PATCH /revisable-worksheets/{id}`）**

请求体为 `{"expected_revision": <读取到的修订号>, "variable": ...}` 或
`{"expected_revision": ..., "step": ...}`，二者恰好一个。成功返回修订后的
完整结构，`revision` 递增。

```bash
# 3a) 替换一个初始变量的值与单位（变量名不变）：t: 2 h -> 3 h
curl -X PATCH http://localhost:8000/revisable-worksheets/rws_1f2e3d4c5b6a79887766554433221100 \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 1,
    "variable": {"name": "t", "value": "3", "unit": "h"}
  }'
```

```json
{
  "id": "rws_...", "revision": 2, "custom_unit_set_id": "cus_...",
  "variables": {
    "v": {"value": "60", "unit": "km/h"},
    "t": {"value": "3",  "unit": "h"}
  },
  "steps": [
    {"name": "s",  "formula": "v*t",           "output_unit": "km"},
    {"name": "th", "equation": "v*th = s", "solve_for": "th", "output_unit": "h"}
  ],
  "results": [
    {"name": "s",  "value": "180", "unit": "km", "dimension": "m"},
    {"name": "th", "value": "3",   "unit": "h",  "dimension": "s"}
  ]
}
```

```bash
# 3b) 替换一个现有步骤的公式与目标单位（步骤名不变）：
#     s 的公式改为 v*t/2 并输出到 m；下游 th 按新的 s 整单重算（结果为 1 h，
#     而非旧结果推导的 2 h）
curl -X PATCH http://localhost:8000/revisable-worksheets/rws_1f2e3d4c5b6a79887766554433221100 \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 2,
    "step": {"name": "s", "formula": "v*t/2", "output_unit": "m"}
  }'

# 3c) 步骤也可在求值与反求两种形态间替换（名称不变，仍须满足既有校验）
curl -X PATCH http://localhost:8000/revisable-worksheets/rws_1f2e3d4c5b6a79887766554433221100 \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 3,
    "step": {"name": "th", "equation": "v*th = s", "solve_for": "th", "output_unit": "min"}
  }'
```

修订的错误与并发语义：

| 情况 | 状态 | 已保存内容 |
| ---- | ---- | ---------- |
| `expected_revision` 与当前修订号不一致（同一修订的并发修改落选） | **409** `revision_conflict`（响应含 `current_revision`） | 不变 |
| 工作单标识不存在 | **404** `worksheet_not_found` | — |
| 变量/步骤名不存在，或试图新增/删除/重命名 | **422** `invalid_request` | 不变 |
| 替换后步骤形态非法（公式与方程混给、反求缺 `solve_for`、方程多等号等） | **422** | 不变 |
| 整单重算任一步失败（量纲冲突、除零、非线性、精度溢出等，错误码同 `/worksheet`） | **422** | 不变 |
| 服务端写盘失败 | **500**（内存已回滚） | 输入、结果与修订号均不变，可按原修订号重试 |

409 响应示例：

```json
{"detail": {"error": "revision_conflict",
            "message": "工作单 'rws_...' 当前修订号为 2，提交的 expected_revision 为 1：……",
            "current_revision": 2}}
```

调用方按 `current_revision` 重新 `GET` 最新内容、合并自己的修改后用新的
修订号重试即可。容器重建（`docker compose up` 重建容器、命名卷保留）后，
`GET` 仍返回最后一次成功修订，并可继续 `PATCH` 修订；既有的
`POST /worksheet` 等所有调用与 Compose 启动方式保持不变。

### 把步骤结果绑定为另一张工作单的初始变量（关联与上游修订联动）

工作单之间经常需要**接力**：一张工作单（**上游**）某一步算出的结果，
作为另一张工作单（**下游**）的初始输入；上游修订后，下游应自动使用新
结果重算，而无需人工抄写。通过

`POST /revisable-worksheets/{worksheet_id}/bindings`

建立这种一对一关联：路径参数 `{worksheet_id}` 是**上游**工作单标识，
请求体提交：

| 字段 | 说明 |
| ---- | ---- |
| `step_name` | 要绑定的**上游现有步骤结果名**；下游工作单必须包含**同名**初始变量 |
| `expected_revision` | 读取到的**上游**当前修订号；过期返回 409（带回 `current_revision`） |
| `variables` / `steps` | 待创建的**下游**工作单本体（2～5 步，结构同 `POST /worksheet`，但不含定义集引用） |

约定与语义：

- **值与单位整体覆盖**：下游同名初始变量在提交时给什么值/单位都无所谓
  （它只是占位），建立关联时以上游步骤结果的 `value` 与 `unit` **整体
  覆盖**，随后用覆盖后的输入把下游整张工作单计算一遍；
- **同一份已发布定义集**：下游不接受自己的定义集引用，固定沿用上游创建
  时解析并固定的那一版 `custom_unit_set_id`——两单始终在同一份定义下计算，
  别名之后切换也不影响它们；
- **计算成功才保存**：下游任一步失败返回 **422**，不创建关联、不创建下游
  工作单；
- **仅一对一、不允许链式**：一张工作单最多是一条关联的上游或下游；已经是
  下游的单不能再作为上游去绑定（A→B 时不能再 B→C）。违反返回 **422**；
- **上游不存在 404、修订号过期 409**：过期响应与普通修订一样带回
  `current_revision`，重新读取后重试即可。

成功响应 **201** 就是下游工作单的完整内容（结构与普通可修订工作单响应
相同），并额外携带 `binding` 字段（上游标识、步骤名、绑定值对应的上游
修订号）：

```bash
# 上游：v=60 km/h、t=2 h；步骤 s = v*t = 120 km（revision=1）
curl -X POST http://localhost:8000/revisable-worksheets \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {
      "v": {"value": "60", "unit": "km/h"},
      "t": {"value": "2",  "unit": "h"}
    },
    "steps": [
      {"name": "s",  "formula": "v*t",            "output_unit": "km"},
      {"name": "th", "equation": "v*th = s", "solve_for": "th", "output_unit": "h"}
    ],
    "custom_unit_set_id": "cus_..."
  }'
# -> {"id": "rws_UP", "revision": 1, "results": [{"name": "s", "value": "120", ...}, ...]}

# 绑定：把上游步骤 s 的结果绑定为下游同名初始变量 s（提交时的 "0 h" 仅为占位，
#       会被 120 km 整体覆盖）；下游另有自己的变量 k 与两个步骤
curl -X POST http://localhost:8000/revisable-worksheets/rws_UP/bindings \
  -H 'Content-Type: application/json' \
  -d '{
    "step_name": "s",
    "expected_revision": 1,
    "variables": {
      "s": {"value": "0", "unit": "h"},
      "k": {"value": "2", "unit": "1"}
    },
    "steps": [
      {"name": "half",  "formula": "s/2",    "output_unit": "km"},
      {"name": "again", "formula": "half*k", "output_unit": "km"}
    ]
  }'
```

```json
{
  "id": "rws_DOWN",
  "revision": 1,
  "custom_unit_set_id": "cus_...",
  "variables": {
    "s": {"value": "120", "unit": "km"},
    "k": {"value": "2",   "unit": "1"}
  },
  "steps": [
    {"name": "half",  "formula": "s/2",    "output_unit": "km"},
    {"name": "again", "formula": "half*k", "output_unit": "km"}
  ],
  "results": [
    {"name": "half",  "value": "60",  "unit": "km", "dimension": "m"},
    {"name": "again", "value": "120", "unit": "km", "dimension": "m"}
  ],
  "binding": {
    "upstream_worksheet_id": "rws_UP",
    "step_name": "s",
    "upstream_revision": 1
  }
}
```

- 读取**下游**（`GET /revisable-worksheets/rws_DOWN`）可见同样的 `binding`：
  上游标识、绑定步骤名与当前绑定值对应的上游修订号；读取上游或任何无关联
  工作单时响应不含 `binding` 字段，行为与以前完全一致。

**上游修订时联动下游**：沿用既有的 `PATCH /revisable-worksheets/{id}`，
请求体没有任何变化——修订上游成功后，服务在同一临界区内用新的步骤结果
覆盖下游绑定变量、把下游**整单重算**，两单的输入、结果与修订号共同落盘
（两单修订号各自 +1，下游 `binding.upstream_revision` 同步为新的上游
修订号）：

```bash
# 上游 t: 2 h -> 3 h；s 随之从 120 km 变为 180 km
curl -X PATCH http://localhost:8000/revisable-worksheets/rws_UP \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1,
       "variable": {"name": "t", "value": "3", "unit": "h"}}'
# -> {"id": "rws_UP", "revision": 2,
#     "results": [{"name": "s", "value": "180", ...}, {"name": "th", "value": "3", ...}]}

curl http://localhost:8000/revisable-worksheets/rws_DOWN
```

```json
{
  "id": "rws_DOWN", "revision": 2,
  "variables": {
    "s": {"value": "180", "unit": "km"},
    "k": {"value": "2",   "unit": "1"}
  },
  "results": [
    {"name": "half",  "value": "90",  "unit": "km", "dimension": "m"},
    {"name": "again", "value": "180", "unit": "km", "dimension": "m"}
  ],
  "binding": {"upstream_worksheet_id": "rws_UP", "step_name": "s",
              "upstream_revision": 2}
}
```

联动与并发的错误语义：

| 情况 | 状态 | 两单已保存内容 |
| ---- | ---- | -------------- |
| 上游 `expected_revision` 过期（同一上游修订的并发修改落选） | **409**（含 `current_revision`） | 都不变；落选者不触发任何重算 |
| 上游标识不存在 | **404** `worksheet_not_found` | — |
| 下游整单重算任一步失败（如新绑定值与下游步骤量纲不兼容） | **422**（错误码同 `/worksheet`） | **上游与下游都不变**，可按上游原修订号重试 |
| 服务端写盘失败 | **500**（内存已回滚） | **两单**的输入、结果与修订号均不变 |
| 下游直接 `PATCH` 被绑定的初始变量 | **422** `invalid_request` | 不变；请通过修订上游联动更新 |
| 建立关联时上游已有下游、或上游本身已是下游（链式） | **422** `invalid_request` | 不创建关联或下游 |
| 绑定步骤在上游不存在、下游缺少同名初始变量、下游计算失败 | **422** | 不创建关联或下游 |

补充约定：

- **下游仍可自治**：下游可以照常修订**其他**初始变量或任意现有步骤
  （名称不变的既有规则）；下游自行修订只递增下游自己的修订号，不改变
  `binding.upstream_revision`。此后上游再次修订时，按下游当前的步骤定义
  整单重算；
- **每次上游修订都联动**：即使本次修改的变量并不影响绑定步骤的数值，下游
  也会整单重算并把修订号、`upstream_revision` 同步推进，保证两单始终同属
  一个联动快照；
- **无混版读**：上游与下游存放在同一个 JSON 文件、由同一把锁保护，
  “上游重算 + 下游重算 + 两单落盘”在同一临界区完成，并发读者要么看到
  联动前的两单、要么看到联动后的两单，不会看到“上游已更新、下游仍旧版”
  或“新输入 + 旧结果”；
- **容器重建后关联仍生效**：绑定信息随两单一起保存在数据目录的
  `revisable_worksheets.json` 中，`docker compose up` 重建容器（命名卷
  保留）后 `GET` 下游仍返回 `binding`，上游 `PATCH` 仍继续联动；
- 绑定只发生在两张**可修订工作单**之间；`POST /worksheet` 等无状态接口
  与没有关联的工作单行为完全不变。

### 其他端点

| 端点         | 说明                             |
| ------------ | -------------------------------- |
| `POST /worksheet-diagnose` | 工作单换版诊断：同一 2～5 步工作单分别在旧/新已发布定义集（标识或活动别名）下执行，逐步并排对照并报告首个含义变化；业务失败结构化返回，未知定义集/非法请求 422 |
| `POST /worksheet-migrate` | 工作单输入迁移：把现有 2～5 步工作单的初始变量改写为新定义集下物理量相同的精确十进制值，返回可直接交给 `/worksheet` 的新工作单（引用固定的新标识）；失败 422 并指出变量或步骤 |
| `POST /revisable-worksheets` | 创建可修订工作单（201）：提交 2～5 步工作单与已发布定义集标识或活动别名，整单成功才保存输入、逐步结果、固定定义集标识与初始修订号 1，返回工作单标识；引用不存在或任一步失败 422 且不创建 |
| `GET /revisable-worksheets/{id}` | 读取可修订工作单当前修订的输入、步骤定义、逐步结果与修订号；不存在 404 |
| `PATCH /revisable-worksheets/{id}` | 修订可修订工作单：携带 `expected_revision`，只替换一个初始变量或一个现有步骤（名称不变），整单重算后共同保存并把修订号 +1；修订号冲突 409（带回 `current_revision`）、标识不存在 404、修改非法或重算失败 422；写盘失败回滚，均不改变已保存内容。若该单是绑定上游，还会在同一事务内用新步骤结果覆盖下游绑定变量并整单重算、共同落盘（下游失败/写盘失败时两单都不变） |
| `POST /revisable-worksheets/{id}/bindings` | 把上游（路径 `id`）步骤结果绑定为新建下游工作单的同名初始变量（201）：提交 `step_name`、上游 `expected_revision` 与下游工作单；以步骤结果的值和单位覆盖该变量，下游整单计算成功才共同保存关联与下游，返回下游标识、两单修订号与结果；两单固定同一份已发布定义集，仅一对一、不允许链式；上游不存在 404、修订号过期 409（带回当前号）、步骤/变量缺失或下游计算失败 422 且不创建任何内容 |
| `POST /custom-unit-sets` | 发布 1～6 项自定义单位定义为可复用定义集，返回服务生成标识（201） |
| `GET /custom-unit-set-aliases` | 列出全部活动别名及其当前指向的定义集标识 |
| `GET /custom-unit-set-aliases/{alias}` | 查询单个活动别名当前指向（不存在返回 422） |
| `PUT /custom-unit-set-aliases/{alias}` | 创建/切换活动别名（乐观并发：`expected_id` 初建为 `null`）；成功 200，冲突 409，目标不存在 422 |
| `POST /custom-unit-set-migration/export` | 导出 1～6 个已发布定义集及指向它们的活动别名（同一快照）为迁移包 JSON；标识不存在/重复/越界 422 |
| `POST /custom-unit-set-migration/import` | 导入迁移包（201）：整包校验通过后生成新本地标识、别名改指新标识，返回旧→新标识映射；定义非法/引用悬空/同名别名冲突整包 422 拒绝，不留部分内容 |
| `GET /units` | 列出全部单位、词头与别名         |
| `GET /health`| 健康检查                         |
| `GET /`      | 服务信息                         |

## 表达式语法

### 单位表达式（`from`/`to`/`output_unit`/变量 `unit`）

```
expr    := term (("*" | "/") term)*
term    := factor (("^" | "**") exponent)?
factor  := "(" expr ")" | SYMBOL | "1"
exponent:= ["("] ["+" | "-"] INTEGER [")"]
```

- 不允许隐式相乘：`kgm` 是未知符号，必须写 `kg*m`
- 表达式中只允许数字 `1`（表示无量纲）
- 幂指数必须为整数且 |n| ≤ 99：`m^2`、`s^-2`、`m**3`、`m^(2)` 均可；`m^0.5` 返回 422

### 公式（`/evaluate` 的 `formula`）

```
expr   := term (("+" | "-") term)*
term   := unary (("*" | "/") unary)*
unary  := ("+" | "-") unary | factor
factor := PRIMARY (("^" | "**") unary)?    # 幂右结合
PRIMARY:= "(" expr ")" | VARIABLE | DECIMAL
```

- 运算符优先级：括号 > 一元正负号与幂（`-x^2` = `-(x^2)`，幂右结合：`2^3^2` = 512）> `*` `/` > `+` `-`
- 指数位置允许一元负号：`x^-2`；十进制定值如 `2`、`0.5` 为无量纲精确分数
- 不允许隐式相乘：`2x`、`x y`、`x(y)` 均返回 422

## 温度规则

| 类别     | 单位                | 说明                                       |
| -------- | ------------------- | ------------------------------------------ |
| 绝对温度 | `degC` `degF` `K`   | 仿射换算，如 `98.6 degF -> 37 degC`        |
| 温差     | `dC` `dF` `dK`      | 纯线性换算，如 `1 dC -> 1.8 dF`            |

- `degC`/`degF` 必须**单独出现**：`kg*degC`、`degC^2`、`J/degF` 均返回 422
- 复合表达式中的温度请使用 `K`（如 `J/(kg*K)`）
- 绝对温度与温差互转（如 `degC -> dK`、`dC -> degC`）返回 422
- `K` 是中性的：既可参与绝对温度换算，也可与温差单位换算
- 在 `/evaluate` 中：单位为裸 `degC`/`degF`/`K` 的变量属于**绝对温标变量**，只能作为公式唯一项（`T` 或 `(T)`）；温差变量（`dC`/`dF`/`dK`、`mK` 等）可正常线性运算
- 在 `/solve` 中：绝对温标变量不能出现在等式中；温差变量可正常参与求解；目标单位须为线性单位
- 别名：`°C`→`degC`，`°F`→`degF`

## 支持的单位

- **SI 基本单位**：`m`、`g`（`kg` 由词头组合）、`s`、`A`、`K`、`mol`、`cd`
- **SI 导出单位**：`Hz N Pa J W C V F ohm S Wb T H lm lx Bq Gy Sv kat`、`rad`
- **常用非 SI**：`min`、`h`、`d`、`L`
- **温度**：`degC`、`degF`、`dC`、`dF`、`dK`
- **词头**：上表 SI 词头可作用于所有 `prefixable` 单位（`GET /units` 查看），如 `km`、`ms`、`mL`、`kPa`、`µm`
- **别名**：`Ω`→`ohm`，`l`/`ℓ`→`L`

## 精确性与精度上限

- 内部以分数（`Fraction`）做精确有理数运算，输出前才化为十进制
- 仅当结果是**有限十进制**且有效数字 ≤ **50 位**时输出，否则返回 422 `precision_overflow`
  - 例如 `1 degF -> K`（255.92777…，无限循环）与 `1 s -> min`（0.01666…）都会返回 422
  - 而 `32 degF -> K = 273.15`、`30 s -> min = 0.5` 可以精确输出
- 输入 `value`（及公式中的十进制定值）的有效数字同样不得超过 50 位
- 可通过环境变量调整：`UNIT_API_MAX_PRECISION`（默认 50）、`UNIT_API_MAX_POWER`（默认 99）

## 错误码

换算类错误响应体统一为 `{"detail": {"error": "<错误码>", "message": "<说明>"}}`，
**均为 HTTP 422**：

| 错误码               | 含义                                   |
| -------------------- | -------------------------------------- |
| `invalid_request`    | 请求参数校验失败（缺字段、值非法等；自定义单位的重名/符号冲突/循环引用/非法比例/禁引温标，以及 `custom_units`、`custom_unit_set_id`、`custom_unit_set_alias` 任意两个同时出现、引用的定义集标识或活动别名不存在、别名绑定的目标定义集不存在，以及迁移包导出标识不存在/重复/越界、导入时包内别名悬空或别名非法/目标端已有同名别名/包格式不支持也归入此码）   |
| `invalid_syntax`     | 表达式/公式语法错误（含隐式相乘）      |
| `unknown_symbol`     | 未知单位符号（含自定义单位的 `base_unit` 引用了未声明符号） |
| `unknown_variable`   | 公式引用了未在变量映射中提供的变量     |
| `invalid_power`      | 非法幂（指数非无量纲整数或超出上限）   |
| `dimension_mismatch` | 量纲不兼容（加减项或结果与目标单位）   |
| `division_by_zero`   | 除数为零（含对零取负整数幂）           |
| `affine_mixing`      | 温差与绝对温度混算 / 仿射温标被组合    |
| `nonlinear_equation` | 未知量处于非线性位置（相乘、作除数、参与幂；按结构判定，零值因子/相消子式不豁免） |
| `no_unique_solution` | 未知量系数为零：方程无解或有无穷多解   |
| `precision_overflow` | 结果不是有限十进制或超过精度上限       |
| `meaning_changed`    | 仅 `POST /worksheet-migrate`：复核时发现某步内部精确数值/量纲/绝对温度属性与旧定义下不一致，迁移被拒绝 |

`POST /worksheet-migrate` 的 422 响应在 `detail` 上额外携带 `variable`
（出错的初始变量名）或 `step_index`/`step_name`（出错的步骤位置与结果名），
此时不返回迁移工作单。

**唯一的非 422 领域状态**：`PUT /custom-unit-set-aliases/{alias}` 在别名当前指向
与提交的 `expected_id` 不一致时返回 **HTTP 409**：

```json
{"detail": {"error": "alias_conflict",
            "message": "别名 'lengths' 当前指向与 expected_id 不一致：……",
            "current_id": "cus_..."}}
```

`current_id` 为服务端当前指向（初建冲突、当前没有指向时省略该字段）。冲突时不切换、
不落盘、不改写原指向；调用方可按 `current_id` 重新读取后决定是否重试。

可修订工作单的乐观修订在 `PATCH /revisable-worksheets/{id}` 的
`expected_revision` 与当前修订号不一致时同样返回 **HTTP 409**：

```json
{"detail": {"error": "revision_conflict",
            "message": "工作单 'rws_...' 当前修订号为 2，提交的 expected_revision 为 1：……",
            "current_revision": 2}}
```

同一修订的并发修改最多一项成功；落选请求不执行重算、不改变已保存的输入、
结果或修订号，调用方可按 `current_revision` 重新读取后重试。读取/修订一个
不存在的工作单标识返回 **HTTP 404**（`worksheet_not_found`）。建立步骤结果
关联（`POST /revisable-worksheets/{id}/bindings`）时上游标识不存在同样返回
**404**，上游 `expected_revision` 过期同样返回 **409**；关联的一对一/非链式
约束冲突、绑定步骤或下游同名变量缺失、下游计算失败、下游直接修改绑定变量
均为 **422**（`invalid_request` 或对应计算错误码）。

## 项目结构

```
app/
  main.py       # FastAPI 应用、路由与异常处理（含别名 409）
  schemas.py    # Pydantic 请求/响应模型
  parser.py     # 单位表达式解析器（递归下降）与规范化输出
  evaluator.py  # 公式求值器（Fraction 数值/量纲合并、温标规则）
  solver.py     # 等式求解器（未知量一次系数/常数的 Fraction 构建与量纲校验）
  worksheet.py  # 顺序计算工作单：2～5 个求值/反求步骤顺序执行，步骤间传递精确量与绝对温度属性；另有逐步收集器（不抛业务错误）供换版诊断与输入迁移使用
  revisions.py  # 可修订工作单：创建/读取/乐观修订（CAS）、单项目替换、整单重算、修订号递增与 JSON 原子落盘/重载（容器重建后可继续修订）；步骤结果绑定：下游创建（一对一/非链式校验、绑定变量覆盖、下游整单成功才共同落盘）与上游修订联动（同一临界区两单重算与共同落盘/回滚）、绑定信息持久化与悬空修复
  diagnose.py   # 工作单换版诊断：开始时固定旧/新定义集指向，两侧各执行同一工作单，按内部精确数值/量纲/绝对温度属性逐步比较并定位首个含义变化
  migrate.py    # 工作单输入迁移：旧定义下执行后把初始变量改写为新定义下物理量相同的精确十进制值，复核整张新工作单并返回可直接交给 /worksheet 的结果
  units.py      # 单位注册表、SI 词头与别名
  custom_units.py  # 请求级自定义单位（custom_units）：冲突/循环/禁引校验与链式解析，三种来源选择
  unit_sets.py  # 已发布定义集与活动别名：校验发布、标识生成、别名乐观切换（CAS）、迁移包导出（同快照）/整包导入（新标识+别名改指+共同落盘）、线程安全与 JSON 落盘/重载
  converter.py  # 量纲校验、仿射规则与精确十进制换算
  errors.py     # 领域错误（换算类统一映射 422；别名冲突 AliasConflictError 映射 409）
  config.py     # 精度/幂次上限/数据目录配置
tests/
  test_api.py       # /convert 端到端测试
  test_evaluate.py  # /evaluate 端到端测试
  test_solve.py     # /solve 端到端测试
  test_worksheet.py # /worksheet 顺序工作单（引用规则/精确与温标传递/三来源/别名只解析一次）端到端测试
  test_worksheet_diagnose.py # /worksheet-diagnose 换版诊断（显示相同物理值已变/量纲与绝对温度/部分失败/别名固定/422）端到端测试
  test_worksheet_migrate.py # /worksheet-migrate 输入迁移（改写变量/往返提交/别名固定/各类 422 指出变量或步骤）端到端测试
  test_revisable_worksheets.py # 可修订工作单创建/读取/修订（单项目替换/整单重算/下游不沿用旧结果/409 CAS/404/422/别名固定/重建后续修订/写盘回滚/一致读）端到端测试
  test_step_result_bindings.py # 步骤结果绑定（建立关联/覆盖与同定义集/读取 binding/一对一与非链式/404/409/422/上游修订联动两单共同落盘/下游失败与写盘失败两单回滚/并发只联动一次/下游自治与绑定变量保护/重建后关联仍生效/旧文件兼容）端到端测试
  test_custom_units.py  # 请求级自定义单位（四个接口）端到端测试
  test_unit_sets.py     # 可复用定义集发布/选用/互斥/隔离/不可变/持久化测试
  test_unit_set_aliases.py  # 活动别名查询/切换（409/422）/四接口选用/并发一致性/持久化与旧文件兼容测试
Dockerfile
docker-compose.yml  # 命名卷 unit_sets_data -> /data，持久化已发布定义集、活动别名与可修订工作单
```
