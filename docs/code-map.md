# 源码地图

从 [Conversation.message()](../backend/agent_harness/conversation.py)开始：保存输入、检查会话、调用原生 Agent、收集回复和保存检查点。

| 文件或目录 | 职责 |
| --- | --- |
| [agent.py](../backend/agent_harness/agent.py) | Agent、Toolkit 与 Skill 组装 |
| [prompts/system.md](../backend/agent_harness/prompts/system.md)、[prompts/__init__.py](../backend/agent_harness/prompts/__init__.py) | 完整业务提示词模板；替换场景和客户资料占位符 |
| [tools/](../backend/agent_harness/tools/) | 带类型的业务函数、参数与权限边界 |
| [business_data/catalog.py](../backend/agent_harness/business_data/catalog.py) | 地区与条件筛选、有效期、费用计算 |
| [scenarios/meituan_rider/catalog.json](../backend/scenarios/meituan_rider/catalog.json) | 各场景独立保存业务记录、城市配置和方案模板 |
| [expansion.py](../backend/agent_harness/business_data/expansion.py)、[locations.py](../backend/agent_harness/business_data/locations.py)、[pricing.py](../backend/agent_harness/business_data/pricing.py) | 方案生成、地点解析与价格计算 |
| [skills/](../backend/agent_harness/skills/README.md) | 按四个业务场景组织的 17 个问题型 Skill |
| [memory/](../backend/agent_harness/memory/README.md) | 画像注入、通话后提取、后台重试、证据和版本 |
| [runtime/service.py](../backend/agent_harness/runtime/service.py) | 会话、消息、审计和持久化 |
| [database.py](../backend/database.py)、[migrations/](../backend/migrations/) | PostgreSQL 连接池、版本迁移与测试 schema 隔离 |
| [session_manager.py](../backend/agent_harness/runtime/session_manager.py) | 会话集合、冻结快照和共享知识索引 |
| [events.py](../backend/agent_harness/runtime/events.py) | 从原生事件记录回复与工具轨迹 |
| [rag/](../backend/agent_harness/rag/README.md) | 官方资料采集、清洗和混合检索 |
| [scenarios/](../backend/scenarios/) | 四场景目标、字段、工具和资源引用 |
| [api/app.py](../backend/api/app.py) | 本机鉴权、语音请求、快照和宿主停止 |
| [channels/speech.py](../backend/channels/speech.py) | ASR/TTS HTTP 适配 |
| [evaluation/datasets/](../evaluation/datasets/) | 四场景各 100 道 RAG 题、100 个动态客户案例及完整语料 |
| [evaluation/rag/](../evaluation/rag/) | 检索执行、Recall、Precision 与 MRR |
| [evaluation/dialogue/](../evaluation/dialogue/) | 普通模型模拟客户、真实文本 Agent、四项质量评分与 token/首 token 时延 |
| [evaluation/judges/](../evaluation/judges/) | 事实来源整理、目标与事实评审 |
| [evaluation/common/](../evaluation/common/) | 数据选择、完整 trace、运行清单与报告 |

运行、维护和扩展命令见[项目 README](../README.md)与[扩展指南](extending-agent.md)。
