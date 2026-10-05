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

## JSON 快照与确定性差异比对

进程内保存 JSON 快照，重启后数据清空。提供 `POST /v1/snapshots`（201，返回该对象）、`GET /v1/snapshots`（按创建顺序）、`GET /v1/snapshots/{snapshot_id}`、`DELETE /v1/snapshots/{snapshot_id}`（204，无正文）。

- 创建请求只含 `id`（去除首尾空白后须非空、唯一）与 `value`（任意 JSON 值，包括 `null`）；保存与响应均与请求对象隔离，删除后 `id` 可复用。
- 重复 `id` 返回 409 `snapshot_exists`；读取、删除或比对不存在的快照返回 404 `snapshot_not_found`；失败创建不留数据。

`POST /v1/snapshots/{snapshot_id}/compare` 将必填的 `actual` 与保存值（expected）比对，不修改快照。请求只允许 `actual` 与可选 `ignore_paths`：无重复的 RFC 6901 JSON Pointer 字符串数组，缺省或为空数组表示不忽略；空指针 `""` 忽略整棵根，合法但未命中的路径无影响，命中节点及其后代均不比较。

- 对象键顺序不影响结果；数组按下标比较；布尔值不等于数字（`true` 不等于 `1`）。容器类型不同或标量不等只在当前位置产生一条 `value_mismatch`（回显 `expected` 与 `actual`），不向下展开。
- expected 缺少的实际成员产生 `missing_actual`（回显 `expected`）；actual 多出的成员产生 `unexpected_actual`（回显 `actual`）。
- 对象键按 Unicode 码点、数组按下标升序遍历，差异 `path` 使用 RFC 6901 转义（`~0`/`~1`），结果顺序稳定；对象成员取并集按键序交错输出。
- 成功返回 200：`{"snapshot_id", "passed", "summary", "differences"}`，`summary` 含 `total` 及 `value_mismatch`、`missing_actual`、`unexpected_actual` 三个计数，仅无差异时 `passed` 为 `true`。
- 畸形 JSON 返回 400 `invalid_json`；请求体类型错误、字段缺失或未知、`ignore_paths` 类型错误、指针非法或重复返回 400 `validation_error`。

## 运行记录

进程内保存运行（run）记录，重启后清空。`POST /v1/runs` 接收 `id`（去除首尾空白后须非空、唯一）与 `case_ids`（按声明顺序 1 至 100 个不重复的现有启用用例），创建时依次冻结每个用例的名称、`kind`、`timeout_seconds`、用例 `steps`、按执行计划语义展开的 `setup`/`teardown` 以及实例标识与参数，目录后续变化（含删除用例或夹具）不影响记录；单次运行的总实例数上限为 5000。创建成功返回 201 及状态为 `open` 的完整报告，`GET /v1/runs/{run_id}` 返回同一报告（报告字段不含冻结上下文，上下文仅用于诊断导出）。冻结失败时不留下部分运行。

外部运行器通过 `POST /v1/runs/{run_id}/results` 逐项提交结果：`instance_id`、`outcome`（`passed`/`failed`/`error`/`skipped`）、`duration_ms`（非负整数）与可选 `details`（任意 JSON 值）；实例须属于该运行且只能提交一次。另可携带可选 `coverage` 行覆盖片段，校验规则与合并语义见下文「行覆盖率片段与合并」。报告中的 `instances` 按冻结顺序输出，未提交项 `outcome` 为 `pending`，已提交项携带原结果；运行报告本身不包含任何覆盖数据。`summary` 含 `total`、`pending`、`passed`、`failed`、`error`、`skipped` 与 `duration_ms`，时长仅累计已提交项。`POST /v1/runs/{run_id}/complete` 仅在没有 pending 实例时将状态改为 `completed` 并返回 200 报告；`passed` 仅在已完成且 `failed` 与 `error` 均为零时为 `true`，`open` 时为 `false`。

- 畸形 JSON 返回 400 `invalid_json`；未知字段、字段类型或取值错误、重复 `case_ids`、实例超限返回 400 `validation_error`。
- 用例、运行或运行内实例不存在分别返回 404 `case_not_found`、`run_not_found`、`instance_not_found`。
- 禁用用例、运行 `id` 冲突、重复结果、仍有 pending 时完成分别返回 409 `case_disabled`、`run_exists`、`result_exists`、`run_incomplete`；重复完成或完成后提交返回 409 `run_completed`。
- 失败请求不留下运行或部分结果。

### 租约式并行领取

多个工作进程通过 `POST /v1/runs/{run_id}/claims` 租约领取待执行实例，普通运行与重试运行中的 pending 实例均可被领取，领取过程在服务锁内原子完成，并发请求不会让同一实例同时持有两个有效租约。请求体只允许 `worker_id`、`max_items`、`lease_seconds`：

- `worker_id`：必填字符串，去除首尾空白后须非空；`max_items` 缺省为 1，提供时必须是 1 至 100 的整数；`lease_seconds` 必填，为 1 至 3600 的整数。布尔值、小数、字符串或 `null` 均不合规。
- 响应 200 为 `{"claims": [...]}`；按冻结顺序扫描实例，跳过已有有效租约与已有结果的实例，至多选取 `max_items` 个；没有可领实例时返回 200 与空 `claims`。
- 每项含唯一且不透明的 `claim_id`，以及 `instance_id`、`case_id`、`case_name`、`kind`、`parameters`、`timeout_seconds`、`setup`、`steps`、`teardown`（上下文与诊断导出口径一致，均为创建/重试时的冻结副本）和纪元毫秒整数 `expires_at`；同一响应内各项的 `expires_at` 相同。
- 当前时间达到 `expires_at` 时租约失效：实例仍为 `pending`，不产生结果、时长或覆盖率，随后可被任意工作进程再次领取（新领取生成新的 `claim_id`），也可按下方无租约规则直接提交。
- 运行不存在返回 404 `run_not_found`；已完成运行返回 409 `run_completed`。请求体不是对象、含未知字段、字段类型或取值不合规返回 400 `validation_error`；畸形 JSON 返回 400 `invalid_json`。

`POST /v1/runs/{run_id}/results` 增加可选 `claim_id`（提供时须为非空字符串），与结果原子处理：

- 实例持有有效租约时，只有携带该实例当前租约的 `claim_id` 才能提交；未提供 `claim_id`、或携带属于其他实例的有效租约标识，返回 409 `claim_conflict`。
- 使用过期、已消费、未知或属于其他实例且已失效的标识返回 409 `claim_not_active`；任何冲突或失效失败都不写入结果或覆盖率。
- 提交成功时在同一原子步骤内消费租约并沿用既有规则写入 `outcome`、`duration_ms`、`details`、`coverage`，重复结果与完成规则不变；被消费的 `claim_id` 不可再次使用（再次提交为 409 `claim_not_active`）。
- 实例无有效租约且未提供 `claim_id` 时，保留原有的直接提交行为；实例无有效租约却提供 `claim_id` 返回 409 `claim_not_active`。
- 运行报告、跨运行聚合、JUnit XML、覆盖率与诊断导出、完成与重试响应均不增加任何租约字段；租约中的实例仍计入 `pending`，未完成结果前不可 complete。

### 占用超时判定

`POST /v1/runs/{run_id}/timeouts` 由平台判定占用中的超时实例。请求体必须是空 JSON 对象；判定在服务锁内对开放运行原子完成：仅当 pending 实例持有有效租约（未消费且未按 `expires_at` 失效），且服务端当前时间达到 `claimed_at`（领取时的纪元毫秒）加冻结 `timeout_seconds` 时才判定超时。已有结果、无租约、已消费或已按 `expires_at` 失效的租约均不处理——失效租约仍只让实例保持 `pending` 并可再次领取。

- 响应 200 为 `{"timed_out": [...], "run": {...}}`：`timed_out` 按冻结顺序列出本次新超时的 `instance_id`，无命中时为空数组；`run` 为完整运行报告。
- 命中实例在同一原子步骤内消费租约并写入结果：`outcome` 为 `error`，`duration_ms` 为冻结 `timeout_seconds` 乘 1000，`details` 固定含 `code`（`timeout`）、`timeout_seconds`、`worker_id`、`claimed_at`、`deadline_at`（等于 `claimed_at` 加超时毫秒）与 `detected_at`（本次检查时间），三个时间均为纪元毫秒整数。超时结果不含覆盖率，也不自动完成运行。
- 同一实例的结果提交与超时判定并发时只有一个成功；超时先成功后，携带原 `claim_id` 的迟到提交返回 409 `claim_not_active`，且不覆盖结果。
- 超时结果纳入现有 `summary`、JUnit XML、诊断导出、跨运行聚合与默认失败重试（`error` outcome）；冻结上下文与重试链语义不变。
- 运行不存在返回 404 `run_not_found`；运行已完成返回 409 `run_completed`；畸形 JSON 返回 400 `invalid_json`；请求体不是对象或含字段返回 400 `validation_error`。所有失败都不改变实例、租约或覆盖率。

### 失败实例重试

`POST /v1/runs/{run_id}/retry` 为**已完成**运行创建重试运行（201，返回 `open` 报告），不重新读取目录、不重新展开参数化。请求体含 `id`（与普通运行相同的去首尾空白、非空、唯一规则）与可选 `outcomes`；`outcomes` 缺省为 `["failed","error"]`，显式提供时必须是非空、无重复的数组，成员只能是 `failed` 或 `error`。

- 实例直接从源运行报告中按冻结顺序选取：仅保留最终 `outcome` 属于 `outcomes` 的实例，相对顺序不变；保留其 `instance_id`、`case_name` 与 `parameters`，`outcome` 重置为 `pending`，且不复制源结果的 `duration_ms` 与 `details`。每个所选实例同时复制直接源运行中冻结的 `kind`、`timeout_seconds`、`steps`、`setup` 与 `teardown`，不重新查询目录；因此源用例或夹具事后被删除、禁用或修改均不影响重试，链式重试同样只复制直接源的冻结上下文。
- 初始 `summary` 的 `total` 与 `pending` 等于所选数量，其余结果计数与 `duration_ms` 均为零。
- 重试报告额外含 `retry_of`（直接源运行 id）、`root_run_id`（整条重试链最初的普通运行 id）与 `attempt`（首次重试为 1，链式重试在直接源的 `attempt` 上加一）；普通运行报告不出现这些字段。
- 再次重试只依据**直接源运行**的最终结果筛选；同一源运行可以用不同的新 `id` 重试多次，各分支独立提交、互不影响。
- 重试运行继续复用现有结果提交、完成与读取入口；`summary`、`passed` 判定及单实例只能提交一次结果的语义与普通运行一致。
- 源运行不存在返回 404 `run_not_found`；源运行尚未完成返回 409 `run_incomplete`；筛选后没有可重试实例返回 409 `retry_not_needed`；新 `id` 已存在返回 409 `run_exists`。
- 畸形 JSON 返回 400 `invalid_json`；请求体不是对象、缺少 `id`、含未知字段，或 `outcomes` 的类型、成员、空值与重复性不符合约束时返回 400 `validation_error`。任何失败都不占用新 `id`，也不留下部分运行。

### 行覆盖率片段与合并

提交实例结果时可携带可选 `coverage` 字段，与结果原子写入：片段不合规时整个提交返回 400 `validation_error`，实例保持 `pending`，结果与覆盖数据均不保存。`coverage` 只能含 `files` 字段；`files` 是非空对象，键为非空文件路径，值只能包含必填的 `executable_lines` 与 `covered_lines`：

- 两者均为无重复正整数数组；`executable_lines` 不得为空，`covered_lines` 可以为空但必须是 `executable_lines` 的子集；布尔值、非整数、零与负数均不合规。
- 单个片段最多 1000 个文件，合计最多 100000 个可执行行号（按文件内去重后的数量求和）。
- 缺省 `coverage` 表示该实例不附带覆盖；显式提供时必须是合规的覆盖对象，`null` 或其他类型均返回 400 `validation_error`。`files` 为空或不是对象、文件值不是对象、缺少任一行号字段、文件条目含未知字段、`coverage` 含 `files` 之外字段、空路径等同样返回 400 `validation_error`。
- 覆盖片段按运行独立累积，与结果在同一提交内一并写入；片段内文件声明顺序与行号顺序均无语义，存储时按路径与行号归一化，响应对象与提交对象完全隔离。

`GET /v1/runs/{run_id}/coverage` 对**已完成**运行只读合并全部实例片段（200，`application/json; charset=utf-8`），不修改任何数据；重复读取同一运行返回顺序与内容稳定的结果。

- 相同路径的可执行行与已覆盖行分别取并集；文件按路径的 Unicode 码点顺序返回，每个文件的行号数组升序返回。
- 每个文件含 `path`、`executable_lines`、`covered_lines`、`missed_lines` 与 `coverage_percent`；`missed_lines` 为可执行行减去已覆盖行，百分比为已覆盖数 ÷ 可执行数 × 100 后四舍五入到两位小数（如 `2/3 → 66.67`）。
- 顶层返回 `run_id`、`summary` 与 `files`；`summary` 含 `files`（文件数）、`executable_lines`、`covered_lines`、`missed_lines` 及同口径 `coverage_percent`。没有任何覆盖片段时 `files` 为空数组、各计数为零、`coverage_percent` 为 `null`。
- 运行不存在返回 404 `run_not_found`；运行尚未完成返回 409 `run_incomplete`。
- 重试运行不继承源运行（或任一祖先运行）的覆盖片段，只汇总重试实例在新运行中提交的片段；源运行的覆盖数据不受重试及其后续提交影响。
- 该入口不改变运行报告结构、完成判定、重试筛选、JUnit XML 导出、跨运行聚合以及目录与快照入口；未知路由仍返回 404 `not_found`。

### JUnit XML 导出

`GET /v1/runs/{run_id}/junit.xml` 将**已完成**运行确定性导出为 JUnit XML（200，`Content-Type: application/xml; charset=utf-8`，UTF-8 正文，含 `<?xml version="1.0" encoding="UTF-8"?>` 声明，根元素为 `testsuite`）。同一运行未改变时多次导出的正文逐字节一致；导出为只读操作，不改变运行、目录或重试数据。

- `testsuite` 的 `name` 为 `testlattice.{run_id}`；`tests`、`failures`、`errors`、`skipped` 取报告总数与对应计数（`skipped` 计入 `tests` 与 `skipped`，不计入 `failures` 或 `errors`）；`time` 为各实例 `duration_ms` 之和换算的秒数，固定三位小数（整数毫秒换算，不用墙钟）。
- `testcase` 按冻结顺序生成：`classname` 为 `case_id`，`name` 为 `instance_id`，`time` 同样固定三位。
- 每个 `testcase` 的 `properties` 先写 `case_name`，再按参数名 Unicode 码点顺序写 `parameter.{name}`；属性值为紧凑 JSON（无多余空白、非 ASCII 不转义、对象键递归按码点排序），因此字符串值保留 JSON 引号。
- `passed` 不带结果子元素；`failed`、`error`、`skipped` 分别带 `failure`、`error`、`skipped`，前两者的 `message` 属性分别为 `failed` 与 `error`。
- 结果存在 `details` 时，将其按键递归排序后序列化为紧凑 JSON 写入结果子元素文本；`details` 缺省时文本为空。
- 重试运行还在 `testsuite` 的 `properties` 中依次写 `retry_of`、`root_run_id`、`attempt`（值同样为紧凑 JSON）；普通运行不写该 `properties` 块。
- 所有文本节点与属性值均做 XML 转义。
- 运行不存在返回 404 `run_not_found`；`open` 运行返回 409 `run_incomplete`；错误响应保持现有 JSON 结构与 `application/json; charset=utf-8`。未知路由仍返回 404 `not_found`。

### 失败诊断导出

`GET /v1/runs/{run_id}/diagnostics` 将**已完成**运行的失败与错误实例确定性导出为 JSON（200，`application/json; charset=utf-8`），提供独立于当前目录的复现上下文。导出为只读操作，不修改运行、覆盖率、目录、快照或重试链，也不改变运行报告、结果提交与完成规则、JUnit XML、覆盖率合并及跨运行聚合。

- 顶层含 `run_id`、`summary` 与 `failures`；`summary` 仅含 `total`、`failed`、`error`，统计本运行最终 `outcome` 为 `failed` 或 `error` 的实例，`passed` 与 `skipped` 不进入导出。没有失败或错误时仍返回 200，三个计数均为零且 `failures` 为空数组。
- `failures` 按冻结实例顺序排列，每项含 `instance_id`、`case_id`、`case_name`、`kind`、`parameters`、`outcome`、`duration_ms`、`timeout_seconds`、`setup`、`steps`、`teardown`；仅当原结果提交时含 `details` 才原样返回该字段。
- `setup` 与 `teardown` 保持执行计划的夹具分组结构与顺序（每项为 `{"fixture_id", "steps"}`，`setup` 依赖在前、`teardown` 严格反序）；`steps` 为冻结的用例步骤。响应中的全部嵌套值均与内部状态隔离。
- 重试运行还在顶层返回 `retry_of`、`root_run_id` 与 `attempt`，语义与重试报告一致；普通运行不出现这些字段。
- 同一运行未变化时重复读取的内容与数组顺序逐字节一致。
- 运行不存在返回 404 `run_not_found`；运行尚未完成返回 409 `run_incomplete`；错误 JSON 结构保持兼容；未知路径继续返回 404 `not_found`。

### 可移交运行归档

运行及其冻结上下文只保存在进程内，进程重启即丢失。归档入口允许外部系统把**已完成**运行导出为自包含 JSON，并在任意进程中恢复为 completed 运行；导出与导入均不读取、不修改目录、快照、资源池或其他运行。

`GET /v1/runs/{run_id}/archive` 返回 200（`application/json; charset=utf-8`）的自包含归档；同一运行未变化时重复导出的 UTF-8 正文逐字节一致，导出为只读操作。顶层只含：

- `archive_version`：固定为整数 `1`。
- `run`：与 `GET /v1/runs/{run_id}` 完全相同的完整报告（含 `id`、`status`、`passed`、按冻结顺序的实例完整结果明细与可重算的 `summary`；重试运行另含 `retry_of`、`root_run_id`、`attempt`）。
- `contexts`：按冻结实例顺序排列的上下文数组，每项含 `case_name`、`kind`、`parameters`、`timeout_seconds`、`setup`、`steps`、`teardown`；仅当冻结时存在资源需求时才含 `resource_requirements`。
- `coverage`：`{"files": [...]}`，保存覆盖率入口所需的规范化合并结果——文件按路径 Unicode 码点排序，每个文件含 `path`、升序无重复的 `executable_lines` 与作为其子集的 `covered_lines`；无覆盖片段时 `files` 为空数组。

归档不包含目录对象、夹具/快照/资源池定义、租约或任何其他运行。

`POST /v1/run-archives` 只接受上述完整归档，并以 `run.id` 建立 completed 记录；成功返回 201 及与运行读取入口相同的完整报告。恢复后的运行继续支持现有读取、JUnit XML、覆盖率、诊断、跨运行聚合与失败重试，并产生与导出前相同的结果；由于状态为 completed，领取、结果提交、超时判定与再次完成一律沿用 `run_completed` 语义。恢复后再次导出与源归档逐字节一致。

- 校验：归档必须是对象且只含四个顶层字段；`archive_version` 必须为 `1`（布尔值、字符串、小数均不合规）。`run` 的字段集合、类型、取值逐项校验：`status` 必须为 `completed`；实例数组非空，每个实例的 `instance_id` 在运行内唯一，所有 `outcome` 均非 `pending` 且属于现有取值；`duration_ms` 为非负整数；`summary` 必须能由实例结果逐项重算得到，`passed` 必须与失败/错误计数一致。
- `contexts` 必须与 `run.instances` 一一对应（数量相等、按冻结顺序配对），每项的名称与参数与配对实例一致，并满足现有的 kind、超时（1–86400 整数）、步骤与夹具分组（`{"fixture_id", "steps"}`）及资源需求约束。
- `coverage.files` 必须为数组，路径非空且不重复，行号为升序无重复正整数，可执行行非空，已覆盖行是其子集；文件与行的总量不限（合并口径而非片段口径）。
- 谱系自洽：普通运行不得出现 `retry_of`/`root_run_id`/`attempt` 中的任一个；重试运行三者必须同时出现且 `attempt` 为正整数、`retry_of` 不得指向自身、首次重试的 `root_run_id` 必须等于 `retry_of`。重试归档按冻结谱系恢复，**即使祖先运行从未导入**也允许恢复，并可继续链式重试与参与聚合。
- 运行不存在返回 404 `run_not_found`；导出 `open` 运行返回 409 `run_incomplete`。
- 畸形 JSON 返回 400 `invalid_json`；请求体非对象、字段缺失或未知、版本不支持、类型错误或内容不一致返回 400 `validation_error`；目标 `id` 已存在返回 409 `run_exists`。任何导入失败都不占用 `id`，也不留下运行或覆盖率的部分状态；全部校验在写入前完成。

### 跨运行聚合报告

`POST /v1/reports/aggregate` 对多个**已完成**运行做只读聚合（200），请求体只允许 `run_ids` 字段：按分析顺序的 1 至 100 个不重复非空运行 id，普通运行与重试运行均可参与。聚合只使用冻结的实例与结果，不查询用例目录，也不修改任何数据。

- 成功响应含 `run_count`、`passed`、`summary`、`runs`、`cases`。`runs` 保持请求顺序，每项含 `run_id`、`passed`、`summary`（`total`、`passed`、`failed`、`error`、`skipped`、`duration_ms`），重试运行另带 `retry_of`、`root_run_id`、`attempt`；顶层 `summary` 为各运行同名计数逐项求和，`passed` 仅在 `failed` 与 `error` 均为零时为 `true`。
- `cases` 按依次扫描各运行及其冻结实例时 `case_id` 首次出现的顺序排列，每项含 `case_id`、`summary`、`trend`、`runs`；用例级 `summary` 统计该用例在所有参与运行中的实例。用例内 `runs` 只列实际含该用例的运行并保持外层顺序，每项含 `run_id`、冻结的 `case_name`、`summary`、`passed`（统计该用例在该运行中的实例，`failed` 与 `error` 均为零时为 `true`）。
- 参数化实例与重复参数行均逐项计数，`skipped` 不算失败。`trend` 依据该用例在各运行中的通过序列：只出现一次为 `insufficient`；多次均通过或均未通过为 `stable_pass`/`stable_fail`；首轮通过而末轮未通过为 `regression`，反之为 `improvement`；首末状态相同但中间改变为 `fluctuating`。
- 畸形 JSON 返回 400 `invalid_json`；请求体非对象、缺少 `run_ids`、含未知字段，或 `run_ids` 的类型、数量、成员非空性、重复性不合规时返回 400 `validation_error`，不返回部分报告。结构校验后按顺序读取运行，首个不存在的运行返回 404 `run_not_found`，首个未完成的运行返回 409 `run_incomplete`。

### 实例稳定性分析

`POST /v1/reports/stability` 对 2 至 100 个**已完成**运行做只读的实例级稳定性分析（200），请求体只允许 `run_ids` 字段：按分析顺序的不重复非空运行 id，普通运行与重试运行一视同仁。分析只使用各运行冻结的实例与最终结果，不查询用例目录，也不修改任何状态，目录对象后续变化不影响输出；相同输入的响应内容与数组顺序完全一致。

- 成功响应含 `run_count`、`summary`、`instances`。实例以 `case_id` 与 `instance_id` 共同标识，按依次扫描各运行冻结实例时首次出现的顺序排列；某次运行不含该实例时不补观测。
- 每个实例项含 `case_id`、`instance_id`、`case_name`（最近一次观测的冻结名称）、`status`、`pass_rate`、`summary`、`observations`。`observations` 保持运行扫描顺序，每项含 `run_id`、`outcome`、`duration_ms`；来自重试运行的观测另带 `retry_of`、`root_run_id`、`attempt`。
- 实例级 `summary` 统计 `total`、`effective`、`passed`、`failed`、`error`、`skipped`、`duration_ms`，其中 `effective` 为排除 `skipped` 后的观测数。`pass_rate` 为 `passed / effective` 四舍五入到两位小数；`effective` 为零时为 `null`。
- `status` 分类：有效观测少于两次为 `insufficient`；否则有效观测全部为 `passed` 为 `stable_pass`，全部属于 `failed` 或 `error` 为 `stable_fail`，`passed` 与失败或错误并存为 `flaky`。顶层 `summary` 统计实例总数及四类状态各自的数量（`total`、`stable_pass`、`stable_fail`、`flaky`、`insufficient`）。
- 畸形 JSON 返回 400 `invalid_json`；请求体非对象、缺少 `run_ids`、含未知字段，或 `run_ids` 的类型、数量、成员非空性、重复性不合规时返回 400 `validation_error`，不返回部分结果。结构校验后按请求顺序读取运行，首个不存在的运行返回 404 `run_not_found`，首个未完成的运行返回 409 `run_incomplete`。

### 命名资源池

进程内保存命名资源池，用于协调稀缺资源，重启后清空。提供 `POST /v1/resource-pools`（201）、`GET /v1/resource-pools`（按创建顺序）、`GET /v1/resource-pools/{pool_id}` 与 `DELETE /v1/resource-pools/{pool_id}`（204，无正文）。

- 资源池字段：`id`（去除首尾空白后须非空、唯一）、`name`（非空，同样去除首尾空白）与 `capacity`（1 至 10000 的整数，布尔值不合规）；只允许这三个字段。
- 资源池响应含 `id`、`name`、`capacity`、`allocated` 与 `available`；`allocated` 按所有开放运行中持有有效租约（未消费、未过期）实例的冻结需求实时求和，`available` 为 `capacity` 减去 `allocated`。
- 重复 `id` 返回 409 `resource_pool_exists`；读取、删除不存在的池或用例引用不存在的池返回 404 `resource_pool_not_found`；仍被目录用例引用或被开放运行的冻结需求引用时删除返回 409 `resource_pool_in_use`，已完成的历史运行不阻止删除。
- 畸形 JSON 返回 400 `invalid_json`；请求体非对象、字段缺失或未知、`capacity` 类型或取值不合规返回 400 `validation_error`，失败不留部分资源池。

用例创建时可选 `resource_requirements`：一个把资源池 id 映射到正整数需求量的对象，随用例创建、读取、列表与执行计划（每个实例）返回；未声明该字段的用例视为空需求，其现有响应不增加资源字段。引用的池不存在返回 404 `resource_pool_not_found`；需求量超过池容量、需求量非正整数或字段结构不合规返回 400 `validation_error`，失败不留部分用例。

创建运行时每个实例的资源需求随执行上下文一同冻结，目录后续变化（含删除用例）不影响它；重试运行沿冻结上下文继承需求。领取实例时在所有开放运行间原子分配：仍按冻结顺序扫描，资源不足的实例被跳过但不阻塞后续可满足实例，响应仍不超过 `max_items`；并发领取不会使任何池的 `allocated` 超过 `capacity`。带资源的领取项额外返回 `resource_requirements`。分配持续到结果成功提交、平台判定超时或租约到期，并随该状态转换立即释放，后续领取与资源池读取立即可见。运行报告、结果提交、完成、超时、重试、覆盖率、JUnit XML、聚合与诊断行为不变。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含用例模型、并行执行与报告聚合的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
