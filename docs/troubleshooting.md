# 本机排障

先运行 `uv run growth doctor` 和 `uv run growth validate`。默认不调用云端模型；`doctor --online` 才发出连通性请求。应用日志在 `.run/application.log`，默认端口为 8787。

| 问题 | 检查方向 |
| --- | --- |
| 无法启动 | Python 3.11、`uv sync --locked`、Node/npm、8787 是否占用、必需凭据与 `SESSION_SECRET` |
| 模型配置缺失或切换不生效 | 使用 `AGENT_*`、`JUDGE_*`、`CUSTOMER_*`、`PROFILE_*`；不再读取 `DEEPSEEK_*`、`OPENAI_*` 或 `LLM_*`。检查进程环境是否覆盖 `.env`，修改后重启；辅助角色已有的显式配置不会随 Agent 自动更换 |
| 模型返回 401、404 或找不到模型 | 核对对应角色的 API Key、模型权限和 `BASE_URL`；基础地址不追加 `/chat/completions`。Qwen 文本地址含 `/compatible-mode/v1`，不能误用仅供语音的域名根地址；密钥地域和计费方案也需对应 |
| 独立模型地址缺少密钥 | 裁判、客户或画像模型改用不同 `BASE_URL` 时，同时填写该角色的 `API_KEY`；仅在沿用 Agent 地址时才能默认复用 Agent 密钥 |
| 模型拒绝 temperature 或 max_tokens | 将受影响角色的 `TEMPERATURE` 设为 `null`，或按服务商要求将 `MAX_TOKENS_PARAM` 改为 `max_completion_tokens`；生成参数各角色独立，需要逐个配置 |
| 模型拒绝 thinking、enable_thinking 或 reasoning_effort | 核对对应角色的 `EXTRA_BODY` 和 `REASONING_EFFORT` 是否适用当前服务；切换厂商后清理上一家的扩展参数。YAML 不默认发送思考参数，但 `.env.example` 显式设置了 DeepSeek 参数；清空后是否思考由服务商默认值决定 |
| 对话能回复，但工具或画像、评审失败 | Agent 模型需支持 Chat Completions 流式工具调用与工具结果续接；画像、客户和评审模型需支持 `response_format` 的 `json_object` 模式。连通性请求通过不能证明这些能力可用 |
| 数据库连接失败 | `docker compose up -d --wait postgres`，核对 `DATABASE_URL` 的主机、端口、数据库和密码，再运行 `growth database status`；修改 Compose 密码不等于修改已有数据卷中的角色密码 |
| 表结构未初始化或版本不匹配 | 运行 `growth database init`；已执行迁移的内容校验失败时恢复原文件并新增编号迁移，不删除业务数据绕过检查 |
| 缺少 vector 扩展 | 本地使用项目的 `pgvector/pgvector:pg17` 镜像；外部 PostgreSQL 需先安装 pgvector，再由有权限的账号初始化 |
| 缺少知识或版本不一致 | `growth up` 校验固定教学语料，并将数据库恢复为对应的四场景版本；失败时查看 `.run/knowledge-initialization.log`。手动重建运行 `growth knowledge build`，不重新采集网页 |
| 教学语料哈希校验失败 | 检查 `backend/agent_harness/rag/knowledge/v1/` 是否完整且属于同一 Git 版本；恢复对应版本文件，不跳过校验或混用其他版本分块 |
| 向量模型缓存缺失 | 运行 `growth knowledge build` 或重新 `growth up` 准备模型，首次可能联网下载至 `.run/knowledge/models/`；客户通话中的搜索不会下载模型 |
| 在线采集后启动又恢复旧资料 | `--refresh` 与 `--from-cache` 用于实验；下次 `growth up` 会恢复 `backend/agent_harness/rag/knowledge/v1/` 固定教学版本。要发布新教学版本，需同步审阅语料、哈希及评测标注 |
| 地区查询没有结果 | 核对地点、城市冲突、参数及有效期。未收录地区可使用通用方案；`no_data/expired` 不表示当地业务不存在 |
| 查询范围不符 | 对照原始条件和实际适用条件，查看内部 `business_query_audit` |
| 回复包含内部诊断内容 | 检查业务字段、`model_projection`、Skill 和输出审计；修复数据流，避免只在最终文本中删词 |
| 金融费用与 App 不同 | 试算基于返回条款、金额和期限，核对采用的产品及假设；个人合同须另行核对 |
| 画像未更新 | 是否已结束通话；查看 state 中的 `profile_update` 状态；确认模型凭据、客户原话与字段类型。后台最多自动尝试三次，失败不修改旧画像 |
| 没有调用工具 | 问题是否需要查询，查看工具轨迹；Skill 无需每轮调用 |
| 无语音或播放受阻 | 使用本机 Chrome，检查麦克风权限、系统输入输出设备和 `DASHSCOPE_BASE_URL` 根域名；开始通话后直接发言 |
| 发言被提前截断或没有提交 | 检查麦克风音量和背景噪声；自动断句依据音量与停顿，句中短暂停顿和说完后的静音需分别试听 |
| Agent 被自己的声音打断 | 先用耳机对比，确认浏览器回声消除与系统音频设备；外放效果需在实际设备上验证 |
| TTS 失败 | 文字回复仍保留，只重试合成，不重新执行工具；完整音频已缓存时重播不会再次调用 TTS |
| 结束后仍有资源 | 检查停止、超时和检查点日志，确认浏览器媒体已释放 |

```sh
uv run growth down
# 只读回放，不重新执行工具
uv run growth replay --run my-run
# 默认预览；核对范围后才加 --execute
uv run growth reset --run my-run
uv run growth clean --expired
```

清理保留持久 Memory 及其必要来源。不要为排查启动问题覆盖 `.env` 或删除数据库卷。数据库初始化、知识重建与备份见[数据库说明](database.md)，验证当前改动按[当前验证流程](current-validation.md)执行。
