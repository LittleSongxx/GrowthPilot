# Agent Harness

[Conversation.message()](conversation.py)保存输入、检查会话、调用 AgentScope `reply_stream()`、收集回复并保存检查点。模型与工具之间的循环由框架负责。

| 文件 / 目录 | 职责 |
| --- | --- |
| [agent.py](agent.py) | Agent、Toolkit 与 Skill 组装 |
| [prompts/system.md](prompts/system.md)、[prompts/__init__.py](prompts/__init__.py) | 完整业务提示词模板与动态资料替换 |
| [tools/](tools/) | 政策与四场景业务查询、费用计算和下载短信 |
| [business_data/](business_data/README.md) | 方案筛选、地区范围、有效期、审计和计算 |
| [skills/](skills/README.md) | 按需加载的沟通和业务方法 |
| [runtime/events.py](runtime/events.py) | 原生工具事件、回复与用量观察 |
| [runtime/session_manager.py](runtime/session_manager.py) | 会话集合与共享知识索引 |
| [runtime/store.py](runtime/store.py) | 框架检查点和运行事件 |
| [runtime/service.py](runtime/service.py)、[memory/](memory/README.md) | 会话、消息、审计、画像注入和通话后提取 |
| [scenario.py](scenario.py) | 加载场景配置、资源和快照 |

`Conversation` 通过 `.tools` 调用场景函数、`.events` 观察原生事件；`runtime/` 管理会话集合与存储。宿主控制停止、超时和资源释放，`finally` 保存检查点。工具异常保留失败状态与原因。

阅读或修改系统提示词，从 [system.md](prompts/system.md) 开始：模板只定义当前客服的角色、下载使用 App 的目标、客户背景和必要沟通原则。`${role}`、`${app_name}`、`${customer_background}` 分别来自场景 `identity` 中的 `role`、`app_name`、`background`；`${customer_context}` 注入经过模型可见字段筛选的客户资料。完整场景配置不进入提示词，工具 Schema 和 Skill 元信息由 AgentScope 提供，Agent 自主决定如何推进。

渲染使用标准库 `string.Template` 单次替换，缺少变量会报错，资料中的同名占位符不会二次展开。模板正文需要美元符号时写作 `$$`，注入资料无需转义。

客户画像自动注入，通话后异步提取客户自述，Skill 指导工作方法，RAG 返回官方正文，业务目录提供地区方案与费用。原始目录查询保留来源审计，模型结果提供业务字段；金额、联系人、条件和估算假设必须完整。下载短信由无参数工具按当前会话的客户与场景发送并保存回执，同一会话重复调用复用原结果。扩展见[指南](../../docs/extending-agent.md)。
