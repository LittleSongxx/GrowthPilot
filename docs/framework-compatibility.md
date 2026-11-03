# AgentScope 与语音接口

AgentScope 固定提交为 `e4c1e074dee3f0f6cca28ca29e934c4ec0605354`。`Agent.reply_stream(UserMsg(...))` 负责模型决策、工具调用和结果回填；`ReActConfig.max_iters` 来自 `models.yaml`，限制单次回复的工具迭代。

`FunctionTool` 从函数类型与 docstring 生成参数 Schema，`Toolkit` 只注册场景声明的工具，`LocalSkillLoader` 按需加载标准 Skill。宿主保存资源快照，检查会话权限与参数；独立画像任务校验原话和字段类型；业务目录负责筛选、计算和有效性检查。

## 语音

[SpeechAPI](../backend/channels/speech.py) 通过 HTTP 调用 ASR `qwen3-asr-flash` 和 TTS `qwen3-tts-flash`。`DASHSCOPE_BASE_URL` 为 HTTPS 根域名，凭据只在后端。ASR 接收录音，TTS 接收已存储的最终回复；业务决策由 Agent 完成。

浏览器使用 `MediaRecorder` 和原生 audio。单段最大 2 MiB、录音最多 60 秒，会话最多 300 秒，默认不保存音频。较长回复由后端分段合成并合并为一个 WAV。

## HTTP

请求只接受本机 Host/Origin。创建会话后，客户端使用仅绑定当前会话的 Bearer token。

| 路径 | 行为 |
| --- | --- |
| GET `/api/health` | 凭据是否配置与语音链路类型；不发起线上连通性检查 |
| GET `/api/scenarios` | 四场景公开元数据 |
| POST `/api/sessions` | `{scenario_id,customer_id,run_id?}`，创建语音会话 |
| POST `/api/sessions/{id}/voice/start` | 通过 `host_control` 触发开场 |
| POST `/api/sessions/{id}/voice/turn` | 录音 → ASR → 保存客户转写 → Agent |
| GET `/api/sessions/{id}/voice/audio/{message_id}` | 合成当前会话已保存的 Agent 回复，可独立重试 |
| GET `/api/sessions/{id}/state`、`/events?after=N` | 会话、消息、客户画像、`profile_update` 任务状态、审计及执行事件 |
| POST `/api/sessions/{id}/stop` | `{reason}`，宿主停止当前请求并释放资源 |
| POST `/api/sessions/{id}/telemetry` | 浏览器播放与可选体验记录 |

文本评测使用内部 `Conversation.message(text, origin='customer')`；公共界面通过语音入口发送客户消息。

## 停止与证据

上传、ASR 和 Agent 属于同一输入任务，并发录音返回 409。停止会取消当前处理，通过 `finally` 保存检查点并释放客户端。TTS 失败保留文字和工具记录，重播只重试合成。

`events.py` 观察原生框架事件；工具调用期间的过程文字不作为最终语音播放。业务结果通过投影保留方案、联系方式、金额和条件，内部诊断单独保存。浏览器播放事件可用于定位故障，但不能证明真人听懂或直接作为声学延迟。

升级模型或框架后，重新验证工具、Skill、Memory、四场景查询和停止清理；人工试听单独记录，见[当前验证流程](current-validation.md)。
