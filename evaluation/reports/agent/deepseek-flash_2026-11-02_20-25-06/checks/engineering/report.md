# 完整工程评测结果

本轮工程检查已完整执行。配置、四场景契约、业务工具覆盖、前端测试和构建通过；完整 `pytest`（启用真实模型）为 **1136 通过、2 失败、0 跳过**，因此本报告不标记为全部通过。没有修改源码或配置。

## 执行结果

| 项目 | 结果 | 原始证据 |
| --- | --- | --- |
| `growth validate` | 4 个场景通过 | [日志](logs/growth-validate.log) |
| 前端测试 | 38 / 38 通过 | [日志](logs/frontend-test.log) |
| 前端生产构建 | TypeScript 检查和 Vite 构建通过 | [日志](logs/frontend-build.log) |
| 场景契约 | 4 个场景全部通过 | [报告](../contracts/report.json)、[日志](logs/contracts.log) |
| 业务工具覆盖 | 509 / 509 通过，12,043 条断言，996 次工具调用 | [报告](../business-coverage/report.json)、[日志](logs/business-coverage.log) |
| `GROWTH_RUN_LIVE=1 uv run pytest -q -ra` | 1138 项完整执行：1136 通过，2 失败；耗时 211.87 秒 | [完整日志](logs/pytest-live-full.log) |

完整 pytest 包含 12 项真实模型冒烟，其中 11 项通过、1 项失败。其余 1126 项为 1125 通过、1 项失败。真实模型冒烟覆盖对话与记忆、下载短信与自然收尾、画像提取；短信操作仍按项目设计记录本地模拟发送审计。

## 失败 1：裁判配置测试没有隔离环境变量

用例：`tests/integration/test_cli_and_models.py::test_judge_generation_configuration_reaches_settings_and_changes_snapshot`。

失败断言要求 `before.judge_extra_body == {}`，实际为 `{"thinking":{"type":"enabled"}}`。这是当前 `.env` 的合法裁判配置。前序业务覆盖测试通过 `Settings.from_env()` 加载真实 `.env`，这些变量留在 pytest 进程中；失败用例只把 `GROWTH_PROJECT_ROOT` 切到临时目录，没有清除已有 `JUDGE_*`。配置加载器按设计优先读取环境变量，因此临时 YAML 的默认值被覆盖。

证据链：业务覆盖 fixture → `evaluation/checks/business_coverage.py:261` → `backend/settings.py:155` 的 `load_dotenv(..., override=False)` → `backend/settings.py:174` 读取环境覆盖 → 失败用例第 62～64 行的默认值断言。同项目的 `tests/integration/test_llm_settings.py` 已使用环境隔离 fixture，但未覆盖这个模块。

已做一次定向复测：仅在子进程中禁用 dotenv 自动读取并取消 `JUDGE_EXTRA_BODY`、`JUDGE_REASONING_EFFORT`、`JUDGE_TEMPERATURE`，运行同一个原始用例，**1 项通过，耗时 0.19 秒**。[复测日志](logs/pytest-judge-isolated.log)

这证明该用例对运行环境和测试顺序敏感；完整测试的首次失败仍然保留，不能把复测通过计入原来 1138 项的通过数。

## 失败 2：金融短信确认后只口头告别，未成功登记结束意图

用例：`tests/runtime/test_download_sms_live.py::test_model_sends_requested_download_sms_and_respects_refusal[financial_consultation]`。

客户请求下载度小满 App 后，`send_download_sms` 成功调用 1 次，发送审计也为 1 条。随后客户说“好的，收到了。”，模型回复：

> 好的，那您先下载安装，有需要随时找我。

此时 `runtime.end_after_playback` 仍为 `False`，在测试第 58 行失败。系统提示第 27 行要求决定结束时调用 `end_call(message)`，`Conversation.end_after_playback` 只在结束意图已登记时为真。因此失败点是确认收信后的自然收尾控制，短信发送本身成功。

原日志未保存第二轮完整工具轨迹，无法继续区分“未调用结束工具”和“结束调用被拒绝”；没有网络失败证据。本次属于真实模型行为失败，未靠重复抽样替换结果。

## 真实模型原始记录

以下两份文件由本次完整 pytest 生成，保留真实回复、工具与框架事件、客户状态及持久化验收结果，作为通过用例的原始证据。文件内容未改写。

- [滴滴司机对话、检索与跨进程持久化](artifacts/live-dialogue-probes/runtime-text-smoke-growth-agent-7.8-1793622354.json)：278 条事件。
- [美团骑手布尔画像记忆](artifacts/live-dialogue-probes/runtime-typed-memory-1793622363.json)：50 条事件。

## 结果口径

本报告只汇总工程回归；正式质量成绩见 [RAG 全量报告](../../../../rag/rag-full-20261102/report.md) 与 [Agent 全量报告](../../report.md)。前端测试使用媒体 API 替身，不能替代真实 ASR/TTS 或真人试听。

机器可读结果、各命令与复测条件见 [summary.json](summary.json)。所附日志保留原始测试结果，仅对本机绝对路径和可能出现的凭据做脱敏。
