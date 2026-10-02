# TestLattice

这是一个面向测试与质量保障的测试、QA 与端到端编排判定平台。长期目标是提供用例模型与夹具、参数化与断言库、并行执行与资源池、超时与挂起检测、覆盖率合并、缺陷最小复现、报告聚合和 CI 集成，把测试编排沉淀为可复用平台。

仓库采用 Python，当前冻结基线只提供进程健康检查。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m testlattice.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `TESTLATTICE_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态。

## 用例目录

进程内保存套件与用例，重启后数据清空。套件与用例分别通过 `/v1/suites` 与 `/v1/cases` 创建（201）、读取/查询（200）和删除（204，无正文）。

- 套件字段：`id`（唯一）、`name`（非空，与 `id` 一样去除首尾空白后校验）、可选 `parent_id`；层级支持任意深度，禁止自引用与成环。
- 用例字段：`id`、`name`、`suite_id`、`kind`（`unit`/`api`/`browser`/`contract`）、非空 `steps`（每项为含非空 `action` 的对象），以及可选 `tags`（非空字符串，按首次出现去重）、`enabled`（默认 `true`）、`timeout_seconds`（默认 300，1–86400 的整数）、`parameterization`。
- `GET /v1/suites` 按同层创建顺序返回父节点在前的深度优先排列。
- `GET /v1/cases` 保持创建顺序，支持 `suite_id`、`include_descendants`（只能与 `suite_id` 同用）、`kind`、`enabled`、`tags`（全部包含，可重复传参或逗号分隔）过滤。
- 含直接或间接子套件或用例的套件不可删除（409 `suite_not_empty`）。

## 参数化与实例预览

用例创建时可提供可选的 `parameterization` 字段，只能在 `axes` 与 `rows` 两种形式中选择一种；原始定义随创建响应、`GET /v1/cases/{id}` 与列表查询原样返回，未提供该字段的用例不会被补入默认字段。

- `axes`：非空对象，轴名称匹配 `[A-Za-z_][A-Za-z0-9_]*`，每个轴为非空数组，元素只能是字符串、数字、布尔值或 `null`。展开结果为各轴取值的笛卡尔积，轴按请求声明顺序参与，最右侧轴变化最快。
- `rows`：非空对象数组，每行至少一个参数，所有行的参数名集合完全一致，参数名与值约束同 `axes`，按输入行顺序展开。
- 重复取值、重复组合或重复行均保留；展开实例数上限为 1000。形式混用、未知字段、空轴/空行、行结构不一致、非法名称、非标量值或超限均返回 400 `validation_error`，且不留下部分用例。

`GET /v1/cases/{case_id}/instances` 返回 `{"case_id", "count", "instances"}` 预览（不修改目录数据，禁用用例也可预览）。每个实例为 `{"id", "parameters"}`：`id` 是用例 id 加从零开始的方括号序号（如 `c1[0]`）；无 `parameterization` 的用例返回唯一一个 `parameters` 为空对象的实例。用例不存在返回 404 `case_not_found`，删除用例后该入口随之不可用。

## 夹具与执行计划

夹具（fixture）描述可复用的准备/清理步骤，进程内保存、重启清空。提供 `POST /v1/fixtures`（201）、`GET /v1/fixtures`（按创建顺序）、`GET /v1/fixtures/{fixture_id}` 与 `DELETE /v1/fixtures/{fixture_id}`（204，无正文）。

- 夹具字段：`id`（唯一）、`name`（均去除首尾空白后须非空）、`setup_steps`、`teardown_steps`（均为步骤数组，至少一边非空，步骤约束同用例步骤），以及可选 `dependencies`。
- `dependencies` 按声明顺序保存，只能引用已存在的夹具，不得重复或自依赖；字段缺省时不补入响应，显式空数组保留。
- 用例创建时可选 `fixture_ids`，按声明顺序引用已有夹具且不得重复；缺省不出现在创建、读取或列表响应中，显式空数组保留。
- 引用的夹具不存在返回 404 `fixture_not_found`；非法字段、类型、标识、步骤、重复引用或自依赖返回 400 `validation_error`，失败不留部分数据；夹具 `id` 冲突返回 409 `fixture_exists`。
- 仍被其他夹具直接依赖或被用例直接引用的夹具不可删除（409 `fixture_in_use`）；删除不存在的夹具返回 404 `fixture_not_found`。

`GET /v1/cases/{case_id}/execution-plan` 只读预览各参数化实例的夹具编排，返回 `{"case_id", "count", "instances"}`。每个实例保留 `id` 与 `parameters`，并带有 `setup`、`steps`、`teardown`：`steps` 为用例步骤；`setup` 与 `teardown` 的每项为 `{"fixture_id", "steps"}`。依赖夹具先进入 `setup`，`teardown` 严格反序；同一夹具经多条路径只出现一次，同级次序由 `fixture_ids` 与 `dependencies` 的声明顺序决定。无夹具时 `setup` 与 `teardown` 为空数组；禁用用例仍可预览，预览不修改目录；用例不存在或已删除返回 404 `case_not_found`。

- 错误统一为 `{"error": {"code", "message"}}`：JSON 解析失败为 400 `invalid_json`，其余请求体/过滤参数错误为 400 `validation_error`，不存在为 404 `suite_not_found`/`case_not_found`/`fixture_not_found`，`id` 冲突为 409 `suite_exists`/`case_exists`/`fixture_exists`，未知路由为 404 `not_found`。

## 断言求值

`POST /v1/assertions/evaluate` 提供无状态的 JSON 值判定，不读取也不修改目录数据。请求体为对象，含 `actual`（任意 JSON 值，包括 `null`）与 `assertions`（1 至 1000 项的数组），仅允许这两个字段。每项断言含唯一非空字符串 `id`、`operator`、`expected`，可选 `path`（缺省为空字符串，指向 `actual` 根值）；断言与结果均严格保持请求顺序。

- `path` 遵循 RFC 6901 JSON Pointer：空字符串表示根，`/` 分隔对象键与数组下标，`~0` 与 `~1` 分别转义 `~` 与 `/`；数组下标须为无符号十进制且无前导零（`-` 不是合法下标）。路径不存在或下标无效时该项判为 `path_not_found`，非法指针语法在请求阶段返回 400。
- `equals`/`not_equals` 按 JSON 结构深比较：布尔值与数字不同（`true` 不等于 `1`），数组顺序有意义，对象键顺序无意义。
- `contains`：字符串检查字符串子串（`expected` 必须也是字符串，否则 `type_mismatch`）；数组检查是否含深度相等的元素（`expected` 可以是任意 JSON 值）；其他类型组合一律 `type_mismatch`。
- `type` 的 `expected` 只能是 `null`、`boolean`、`number`、`string`、`array`、`object` 之一；实际值类型不符时该项判为 `type_mismatch`。
- 单项结果回显 `id`、`path`、`operator`、`expected`，路径存在时额外回显解析到的 `actual`，并含 `passed` 与 `code`：通过为 `ok`，值不满足为 `value_mismatch`，路径不存在/下标无效为 `path_not_found`，`contains`/`type` 类型不符为 `type_mismatch`。单项失败不中止后续求值。
- 成功返回 200：`{"passed", "summary", "results"}`，`summary` 为 `{"total", "passed", "failed"}`，`passed` 仅在全部通过时为 `true`。
- 非 JSON 请求体返回 400 `invalid_json`；请求体不是对象、缺少 `actual`/`assertions`、数组为空或超过 1000 项、断言不是对象、含未知字段、`id` 缺失/为空/重复、`operator` 未知、`type` 的 `expected` 非法或 `path` 不是合法 JSON Pointer 时返回 400 `validation_error`，不返回任何部分结果。

## JSON 快照与差异比对

进程内保存 JSON 快照，重启后数据清空；响应与保存状态完全隔离。提供 `POST /v1/snapshots`（201）、`GET /v1/snapshots`（按创建顺序）、`GET /v1/snapshots/{snapshot_id}` 与 `DELETE /v1/snapshots/{snapshot_id}`（204，无正文，删除后 id 可复用）。

- 创建请求只含 `id` 与 `value`：`id` 去除首尾空白后须非空且唯一，`value` 接受包括 `null` 在内的任意 JSON 值；失败创建不留数据。
- `POST /v1/snapshots/{snapshot_id}/compare` 把必填的 `actual` 与保存值做确定性比对，请求只允许 `actual` 与可选 `ignore_paths`；比对为只读，不修改快照。
- `ignore_paths` 是无重复的 RFC 6901 JSON Pointer 字符串数组，缺省或为空表示不忽略；命中节点及其后代不参与比对，空指针忽略根值，合法但未命中的路径无影响。
- 比对语义：对象键顺序无关、数组按下标、布尔值不等于数字；容器类型不同或标量不等只在当前位置产生一条 `value_mismatch`。期望成员在实际中缺失产生 `missing_actual`（回显 `expected`），实际多出成员产生 `unexpected_actual`（回显 `actual`），`value_mismatch` 同时回显两者。
- 遍历顺序确定：对象键按 Unicode 码点升序、数组按下标升序；差异 `path` 使用 RFC 6901 转义，结果顺序稳定。
- 成功返回 200：`{"snapshot_id", "passed", "summary", "differences"}`；`summary` 含 `total` 及 `missing_actual`/`unexpected_actual`/`value_mismatch` 三种计数，仅无差异时 `passed` 为 `true`。
- 错误：`id` 冲突为 409 `snapshot_exists`；读取、删除或比对不存在项为 404 `snapshot_not_found`；畸形 JSON 为 400 `invalid_json`；请求体类型错误、字段缺失或未知、`ignore_paths` 类型错误、指针非法或重复均为 400 `validation_error`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含用例模型、并行执行与报告聚合的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
