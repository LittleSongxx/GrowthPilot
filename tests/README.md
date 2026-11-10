# 测试目录

从项目根运行 `uv run pytest -q`。默认不调用外部模型与语音供应商，但存储测试需要真实 PostgreSQL + pgvector；先运行 `docker compose up -d --wait postgres`。真实供应商测试仅在显式设置 `GROWTH_RUN_LIVE=1` 后运行。

需要完整验收时，配置 `.env` 中的文本模型凭据，并显式开启真实模型调用：

```sh
GROWTH_RUN_LIVE=1 uv run pytest -q -ra
```

该命令同时执行普通回归和 12 项真实模型冒烟：2 项对话与记忆、6 项下载短信决策与自然收尾、4 项画像提取。短信仍按项目设计写入模拟发送审计。对话检索用例会在临时 PostgreSQL schema 中建立非空 pgvector 索引，使用真实 BGE 向量和文本模型，并通过新 Python 进程核对持久化结果；模型缓存可复用，首次缺少缓存时需要下载。凭据、网络或数据库不可用会导致测试失败，不会被自动当作跳过或通过。默认保留显式开关，避免普通开发测试意外消耗云端模型额度。

测试读取进程环境变量 `DATABASE_URL`（并自动加载项目根 `.env`，进程环境变量优先），默认连接本机 `55432` 端口的 `growthpilot` 数据库，不使用单独的 `TEST_DATABASE_URL`；非默认地址应先导出到测试进程环境。数据库用例创建 `growthpilot_test_<随机值>` 临时 schema，执行同一套迁移，并在用例收尾时关闭连接池、删除临时 schema。连接账号需要创建 schema 的权限，数据库服务需提供 pgvector。存储用例直接验证 PostgreSQL 的事务和查询行为；数据库不可用时相关测试明确失败。纯逻辑测试不建立数据库连接。

| 目录 | 范围 |
| --- | --- |
| `tests/agent_harness/` | 配置、业务目录、范围、参数、Memory、Skill 和结果投影 |
| `tests/runtime/` | Agent/Toolkit、生命周期、框架事件、ASR/TTS 和显式在线探针 |
| `tests/integration/` | 应用 CLI、鉴权、快照、清理和持久数据保留 |
| `tests/rag/` | 官方资料采集、分块、索引与检索 |
| `tests/evaluation/` | 检索指标、动态运行器、固定分母、trace、证据与独立评审 |

工具回归实际执行全部 12 个业务查询，检查场景权限、入参、地域匹配、未知地区兜底、来源审计与业务字段完整性。Memory 回归验证原话、更正、跨客户隔离和跨进程持久化；Skill 回归验证加载前后工具权限一致。这些是工程正确性测试，不是 Memory/Skill 效果对照实验。

覆盖评测使用固定输入矩阵与派生检查，并通过破坏金额、押金、来源或条件的用例确认执行器能够发现错误。裁判测试验证按轮资料与实际交付回复的对应、隐藏审计过滤、四维 1～5 整数评分和原文引用校验；指标测试区分未评与不适用，并验证分数分布、token 和响应时延统计。这些离线测试不证明模型的语义评分已经准确，真实判分仍需结合具体对话复核。

```sh
docker compose up -d --wait postgres
uv run pytest -q
npm --prefix frontend test
npm --prefix frontend run build
uv run python -m evaluation.checks.contracts --output .run/checks/growth-contracts.json
uv run python -m evaluation.checks.business_coverage --output .run/checks/growth-business-coverage.json
```

前端测试使用媒体 API 替身验证录音、播放和资源释放。离线通过不代替真实模型对话或真人试听；完整范围见[当前验证流程](../docs/current-validation.md)。
