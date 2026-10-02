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
- 用例字段：`id`、`name`、`suite_id`、`kind`（`unit`/`api`/`browser`/`contract`）、非空 `steps`（每项为含非空 `action` 的对象），以及可选 `tags`（非空字符串，按首次出现去重）、`enabled`（默认 `true`）、`timeout_seconds`（默认 300，1–86400 的整数）。
- `GET /v1/suites` 按同层创建顺序返回父节点在前的深度优先排列。
- `GET /v1/cases` 保持创建顺序，支持 `suite_id`、`include_descendants`（只能与 `suite_id` 同用）、`kind`、`enabled`、`tags`（全部包含，可重复传参或逗号分隔）过滤。
- 含直接或间接子套件或用例的套件不可删除（409 `suite_not_empty`）。
- 错误统一为 `{"error": {"code", "message"}}`：JSON 解析失败为 400 `invalid_json`，其余请求体/过滤参数错误为 400 `validation_error`，不存在为 404 `suite_not_found`/`case_not_found`，`id` 冲突为 409 `suite_exists`/`case_exists`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含用例模型、并行执行与报告聚合的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
