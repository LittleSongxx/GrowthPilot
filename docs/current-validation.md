# 当前验证流程

先验证工程正确性，再运行正式质量评测。两类结果分开解释，离线测试通过不表示真实模型质量已经通过。

## 工程回归

```sh
docker compose up -d --wait postgres
uv run growth database init
uv run growth validate
uv run pytest -q
npm --prefix frontend test
npm --prefix frontend run build
uv run python -m evaluation.checks.contracts --output .run/checks/growth-contracts.json
uv run python -m evaluation.checks.business_coverage --output .run/checks/growth-business-coverage.json
```

业务契约检查工具注册、来源与适用范围、费用计算、短信回执、客户与场景隔离。Memory 和 Skill 的单元测试继续检查持久化、更正、原生加载及权限；它们不计算独立质量分，也不运行开关消融。

业务覆盖使用 [business-coverage-v1.json](../evaluation/checks/data/business-coverage-v1.json) 的确定性输入。时间相关检查使用固定日期，不表示当前地区业务数据仍然有效。

切换模型时，先按 [README](../README.md#按用途配置和切换文本模型) 配置 `AGENT_*`、`JUDGE_*`、`CUSTOMER_*`、`PROFILE_*`，再运行 `uv run growth doctor` 确认本地配置。需要发起真实连通性请求时执行 `uv run growth doctor --online`。连通性通过不代表工具循环、JSON 输出或业务质量已验证；随后用少量真实对话覆盖政策查询、业务工具和通话后的画像提取，再逐步增加评测范围。切换文本服务不会更改 DashScope ASR/TTS 配置。

## 正式质量评测

只有两类：四场景各 100 道 RAG 问题，以及各 100 个动态客户案例。先预览，再显式执行；对话正式运行每例两次，共 800 条轨迹。

```sh
uv run growth evaluate --suite rag --scenario meituan_rider --limit 3 --run rag-preview
uv run growth evaluate --suite dialogue --scenario meituan_rider --limit 3
# 本地检索执行不调用云端生成模型
uv run growth evaluate --suite rag --run rag-baseline --execute
# 真实对话执行调用 AGENT_*、CUSTOMER_*、PROFILE_* 与 JUDGE_* 配置的模型
uv run growth evaluate --suite dialogue --execute
```

RAG 报告 Recall/Precision@1、3、5、8 和 MRR@8；对话报告目标完成度、问题解决度、事实可靠性、沟通体验四项 1～5 分评分，以及每通 Agent token 和客户轮首 token 时延（TTFT）P50/P95。TTFT 从客户文本提交给 Agent 到最终回复首个非空文本块到达，包含此前工具调用等待；以流式文本块近似首 token，完整回复耗时仅留作诊断。各维度分别展示均分、分布、低分案例和未评数，不输出综合总分。数据来源是公开资料、模型辅助标注与自建客户设定，不是实际客户通话。结果不等同于真实业务转化。

正式 RAG 结果保存到 `evaluation/reports/rag/<run>/`；Agent 每轮自动新建 `evaluation/reports/agent/<模型名_YYYY-MM-DD_HH-MM-SS>/`，其下保留原有文件结构。`--run` 对 RAG 指定目录名，对 Agent 只记录运行标签。预览分别写入 `.run/evaluation-previews/rag/` 和 `.run/evaluation-previews/agent/`，工程检查写入 `.run/checks/`。查看结果时依次阅读 `manifest.json`、`report.md` 和逐条原始结果。比较 Agent 版本时，保持题集、客户和画像模型、裁判模型与评分提示、语料版本、连续通话次数及记忆隔离方式一致。辅助角色未指定连接时沿用 Agent，所以只改 `AGENT_MODEL` 也可能同时更换客户、画像与裁判；仅比较业务 Agent 模型时应显式固定其他三个角色。运行未完成、缺失用量和 judge 无效要先处理，不能当作低分、满分或不适用。

比较响应速度时还需核对 `latency_version`；当前为 `agent-first-token-v1`。旧版完整回复耗时不改标为 TTFT，没有 `first_token_seconds` 的历史轨迹需要重新执行采集。TTFT 不包含 ASR／TTS，也不等于浏览器显示文字或用户听见首音的延迟。

执行器、指标公式、筛选参数和报告阅读路径见[评测说明](../evaluation/README.md)。

## 兼容探针与当前评分口径

[工程检查说明](../evaluation/checks/README.md)保留知识覆盖和语音探针命令。ASR/TTS 观察见[语音验证](voice-acceptance.md)，不混入文本评测分数。语音验收以 `evaluation/checks/config/acceptance.yaml` 的人工记录为准；当前 `observations` 为空，`growth evaluate --suite voice` 只会返回 pending，不能当作已完成验收。

2026-11-02 的全量评测记录已按 RAG 与 Agent 分类，详见[本轮评测记录](../evaluation/reports/README.md)。RAG 已完成 400 道检索题；Agent 全量运行保留 800 条记录，其中 799 通完成并取得有效评分，另 1 通客户模型输出异常。该客户的两次连续通话已独立补跑，原全量运行仍保留未完成状态，不能用补跑结果覆盖原始记录。报告作为教学成果保留运行清单、案例快照、原始结果、裁判记录与汇总，完整产物说明见[评测说明](../evaluation/README.md#三看报告和追溯一次失败)。

`dialogue-rubric-v3.2.3` 使用四项分档质量评分，输出短 key `goal`、`answer`、`facts`、`tone`，每项保留分数和简短理由，不再要求输出引文。当前裁判由 `JUDGE_*` 配置，YAML 默认不发送思考或推理强度参数；`.env.example` 为 DeepSeek 显式设置了 `JUDGE_EXTRA_BODY='{"thinking":{"type":"enabled"}}'`、`JUDGE_REASONING_EFFORT=high` 和 `JUDGE_TEMPERATURE=null`。执行前应核对实际模型、思考配置及输出预算。原始对话、参考资料与工具回执仍提供给裁判并留档。修改裁判后应在新目录重评并注明对话来源，保留原始评分；重评分数变化不能当作 Agent 能力提升。预览只确认题集和运行计划，不表示裁判已经完成真实模型校准。

当前评分允许明确表达为估算或建议的合理区间与预算余量，不要求每个数字对应原文；不因未展示内部挂断事件或未在客户结束后追加问题而扣分。裁判输入不包含工具定义，保留实际业务资料与回执，不要求补充未被询问的可选事项，并区分一般规则与专项要求的适用边界。业务 Agent 的通用 prompt 保留可执行下一步和承接当前进度的规则。

当前版本按项目约定，将“美团骑手 App”和“美团众包 App”视为同义称呼，任何维度不因名称差异扣分，仍核对众包与专送的实际政策、资格和流程。骑手配置、短信、Skill 和动态案例保留同一个骑手场景；案例 `013、079、086` 的成功或拒绝条件不以应用名称为依据，分别检查众包注册卡点和配送类型对应的业务流程。题集变化通过运行清单哈希及案例快照追溯。

连续通话评分按开场记忆、本通客户确认和本通新增问题判断已完成与剩余要求，不因未重复已完成的查询而扣分，也不把历史自述当成外部核验。裁判输出支持 JSON 格式修复与有限重试；失败裁判可使用 `--retry-judge-from` 复用原对话补跑，详见[评测说明](../evaluation/README.md)。报告另列画像处理完整性，帮助识别记忆更新异常对后续通话的潜在影响。
