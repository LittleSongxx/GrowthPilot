# 工程检查与历史探针

正式质量评测只有 [RAG 与动态客户对话](../README.md)。本目录集中保存工具契约、知识覆盖与语音检查的代码和输入，供维护工程功能、定位问题使用。这些结果不能折算为正式召回率、回答质量或业务转化率。

```sh
# 确定性业务契约：工具注册、权限、计算、持久化和隔离
uv run python -m evaluation.checks.contracts --output .run/checks/contracts.json
uv run python -m evaluation.checks.business_coverage --output .run/checks/business-coverage.json
# 历史知识覆盖探针：只检查已有索引返回的正文候选
uv run python -m evaluation.checks.knowledge_probe --questions evaluation/checks/data/knowledge_questions.yaml --output .run/checks/knowledge-check.json
uv run python -m evaluation.checks.policy_coverage --output .run/checks/policy-check.json
# 需要观察一次 Agent 自主检索时，显式添加 --agent；会调用云模型
uv run python -m evaluation.checks.knowledge_probe --agent --output .run/checks/agent-probe.json
```

`data/knowledge_questions.yaml` 为 16 道历史业务问题，`data/policy_coverage.yaml` 为 40 道政策覆盖检查，`data/skill-knowledge.yaml` 为 30 道历史知识依赖问题。它们没有完整相关片段标注，不计入正式 400 道 RAG 题，也没有独立 Skill 成绩。原文件名只用于保留来源。

`data/business-coverage-v1.json` 是确定性业务工具输入矩阵，不是对话客户题集。`growth evaluate --suite contracts` 和 `--suite business-coverage` 继续用于工程检查，不调用模型，也不接受 `--execute`；结果写入 `.run/checks/<run>/`，可用 `--run` 指定本轮名称。`config/acceptance.yaml` 保存语音验收设置。

语音探针同样不属于文本 Agent 正式评测：

```sh
# 调用云端 ASR、文本模型和 TTS
uv run python -m evaluation.checks.speech_probe --output .run/checks/speech-check.json
uv run growth evaluate --suite voice
```

输入和输出的相对路径均从项目根目录解析；`/growth-agent/...` 展示路径也会映射到当前项目。上述命令把工程检查的临时报告放在 `.run/checks/`，真实模型的对话探针也写入该目录，无需随教学源码提交。`evaluation/reports/` 只设 `rag/` 与 `agent/` 两个分类目录。本轮已保存的工程、契约与业务覆盖证据归档到对应 Agent 运行的 `checks/` 下，详见[本轮评测记录](../reports/README.md)。所有执行使用新输出路径。旧 `skills` suite 已移除；原 48 个固定对话与 20 个 Skill 案例中的业务问题可在新客户集追溯，固定台词和强制 Skill 加载评分不再使用。
