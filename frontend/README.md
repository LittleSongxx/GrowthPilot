# 前端阅读入口

从项目根运行 `uv run growthpilot up --scenario didi_driver` 即可启动完整系统。前端是 TypeScript + Vite，不单独保存客户业务状态或模型凭据。

| 文件 | 职责 |
| --- | --- |
| [src/main.ts](src/main.ts) | 四场景选择、语音会话、对话记录、客户画像与后台更新状态和工具查询展示 |
| [src/api.ts](src/api.ts) | 调用后端 HTTP 接口与绑定会话身份 |
| [src/voice.ts](src/voice.ts) | WebAudio 持续收音、PCM 音频流播放、当前通话音频缓存和资源释放 |
| [src/audio-processing.ts](src/audio-processing.ts) | 按音量和停顿自动断句，处理 WAV 编码与 PCM 转换 |
| [public/audio-capture-worklet.js](public/audio-capture-worklet.js) | AudioWorklet 采集麦克风音频片段 |
| [src/tool-results.ts](src/tool-results.ts) | 展示业务名称、匹配区域、地址电话、适用条件和费用测算，隔离内部诊断字段 |
| [src/acceptance.ts](src/acceptance.ts) | 四场景共 19 个可选语音观察案例，记录版本为 `dialogue-growth-v2` |
| [src/style.css](src/style.css) | 页面样式 |

仅修改界面时，在本目录执行 `npm run check` 或 `npm run build`。构建产物为 `dist/`，由后端服务提供。界面定期读取当前会话和工具事件，不查询外部注册、办理或投递状态。普通业务视图聚焦客户问题，政策查询保留 RAG 返回的正文、真实来源和引用。

普通查询结果按工具业务展示名称、查询区域、匹配区域、客户条件和方案参考条件。返回的地址与手机号正常展示，不提供拨号交互；缺失值显示“未提供”。金融测算保留利率、费用、适用条件和假设，并区分客户请求的产品与本次参考方案。产品名称取自返回条目，无法解析的内部句柄不会被当作产品名称展示。

界面接受原始业务目录结果和服务端投影结果。业务工具按工具名识别，不依赖 `source_type` 单独判断；当前目录来源标识为 `local_catalog`，内部条目使用 `catalog_` 句柄。内部来源、生成模板、定价参数等诊断字段不进入普通业务展示；`business_query_audit` 原始事件由后台保留，普通调用轨迹不展示它们。显示投影不修改原始事件或业务条款；地址、电话按返回值呈现，缺失值保持未提供。

页面只有语音入口：点击「开始通话」并允许麦克风后直接说话，停顿 700 毫秒自动提交 WAV，单段最长 60 秒。ASR 识别完整录音后调用业务 Agent；Agent 生成最终回答后开始 TTS，浏览器收到 PCM 片段即可播放。用户开口时先在本地停播，再停止旧一轮的 Agent 回答和 TTS，继续接收新发言。

「静音」暂停接收客户发言，「取消静音」恢复；「重播回复」复用当前通话已完成的音频，合成失败时只重试 TTS；「结束通话」停止收音、播放并释放资源。后端通过 `end_after_playback` 请求结束时，前端等待结束语实际播放完成再关闭通话；期间插话撤销结束请求，TTS 失败则可重播同一句结束语。系统通话工具 `end_call` 保留在调用轨迹中，不展示为业务查询卡片。当前实现使用 HTTP，没有增加 WebSocket 或更换语音模型。

`npm test` 检查语音处理、录放与资源释放，以及业务结果和内部诊断的展示边界、旧事件兼容、RAG 引用保留和 HTML 转义。自动测试不能替代外放回声、噪声和真人插话验收。

`growthpilot voice-case --case dialogue-growth-v2-R01` 会生成对应场景的入口，并预选当前观察案例。D03/D04 需使用同一 `--customer` 标识。观察记录携带完整案例 ID 和 `review_version`；旧版本反馈仍可读取，但不会计入本版观察结果。自动测试和界面检查不代表已经完成真实语音验收。
