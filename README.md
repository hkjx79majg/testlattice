# TestLattice

这是一个面向测试与质量保障的测试、QA 与端到端编排判定平台。长期目标是提供用例模型与夹具、参数化与断言库、并行执行与资源池、超时与挂起检测、覆盖率合并、缺陷最小复现、报告聚合和 CI 集成，把测试编排沉淀为可复用平台。

仓库采用 Python，当前冻结基线只提供进程健康检查。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m testlattice.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `TESTLATTICE_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态。

## 用例目录

进程内维护套件（suite）与用例（case）两类资源，重启即清空：

- `POST /v1/suites`、`GET /v1/suites`、`GET /v1/suites/{id}`、`DELETE /v1/suites/{id}`
- `POST /v1/cases`、`GET /v1/cases`、`GET /v1/cases/{id}`、`DELETE /v1/cases/{id}`

套件支持任意深度嵌套（`parent_id`），查询按父节点在前的深度优先返回；用例查询保持创建顺序，支持 `suite_id`、`include_descendants`、`kind`、`enabled`、`tags` 过滤。创建/读取返回 201/200 JSON，删除返回无正文的 204；错误统一为 `{"error": {"code", "message"}}` 结构（`invalid_json`、`validation_error`、`suite_not_found`、`case_not_found`、`suite_exists`、`case_exists`、`suite_not_empty`）。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含用例模型、并行执行与报告聚合的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
