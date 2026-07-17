# 后端入口

先读 [Conversation.message()](agent_harness/conversation.py)：保存客户输入、调用 `Agent.reply_stream()`、收集回复并保存检查点。再读 [agent.py](agent_harness/agent.py) 了解 AgentScope Agent、Toolkit 和 Skill 的组装。模型选择工具，框架执行并回填结果。

| 入口 | 职责 |
| --- | --- |
| [conversation.py](agent_harness/conversation.py) | 对话输入、回复、检查点与宿主停止 |
| [runtime/events.py](agent_harness/runtime/events.py) | 框架事件、工具调用与回复记录 |
| [runtime/session_manager.py](agent_harness/runtime/session_manager.py) | 创建、读取和关闭会话，共享政策索引 |
| [tools/](agent_harness/tools/) | 2 个共同工具（政策检索与下载短信）、12 个目录查询与金融费用试算、2 个确定性预算工具；骑手/司机/商家各注册 6 个，金融注册 5 个 |
| [skills/](agent_harness/skills/README.md) | 按四个业务场景组织的 17 个问题 SOP，按当前问题加载 |
| [business_data/](agent_harness/business_data/README.md) | 本地业务目录、地区筛选、来源审计和费用计算 |
| [runtime/service.py](agent_harness/runtime/service.py)、[memory/](agent_harness/memory/README.md) | 会话、消息、审计、画像注入与通话后提取 |
| [scenarios/](scenarios/) | 对话目标、业务字段、工具和 Skill |
| [scenario.py](agent_harness/scenario.py) | 加载场景配置、资源和快照 |
| [api/app.py](api/app.py)、[channels/speech.py](channels/speech.py) | 本机 HTTP 入口、鉴权、ASR/TTS |
| [rag/](agent_harness/rag/README.md) | 官方资料 RAG |
| [database.py](database.py)、[migrations/](migrations/) | PostgreSQL 连接池、隔离 schema 和按版本执行的 SQL 迁移 |
| [cli.py](cli.py)、[maintenance.py](maintenance.py)、[retention.py](retention.py) | 启动、验证、清理预览和数据保留 |

`FunctionTool` 从类型和 docstring 生成参数 Schema。工具校验会话、权限与参数，返回完整业务结果。`send_download_sms()` 从当前会话取得客户与场景，内部模拟发送并保存回执，同一会话重复调用复用原结果；对模型只提供发送状态。[common.py](agent_harness/tools/common.py) 集中定义跨场景工具，包括骑手/司机的条件出勤预算；[meituan_merchant.py](agent_harness/tools/meituan_merchant.py) 提供商家单笔贡献计算，明确区分扣费前实收和扣费后到账，避免重复扣费，结果均保留输入口径和未计入项目。本地目录使用 `local_catalog` 来源，官方政策与客户自述分别保存。

`LocalSkillLoader` 按需读取自包含的 `SKILL.md` 正文，不扩大工具权限，也不依赖运行时读取额外 `references` 文件。场景加载拒绝重复 Skill ID 和未开放的 `required_tools` 依赖。Skill 负责业务判断顺序，具体工具参数留在工具定义中；知识承接需另用实际 RAG 结果核验，不能仅凭来源配置通过。

宿主控制结束会话、取消请求和释放资源；Agent 理解拒绝后自然道别。`snapshot()` 返回 session、messages、memory、audit，客户反馈用于继续对话。

从项目根运行 `uv run growth up --scenario meituan_rider`，静态页面与 API 共用应用服务。扩展见[工具与 Skill 指南](../docs/extending-agent.md)。

业务库与 RAG 共用 PostgreSQL 服务。`Settings.app_db` 保存连接与 schema 配置，调用方通过连接池借用连接，多步写入显式使用事务；不再把数据库当成本地文件操作。数据库启动与迁移见[数据库说明](../docs/database.md)。
