# 文档导航

- [项目启动与使用](../README.md)：安装、配置、语音体验和四个场景。
- [架构](architecture-and-learning.md)与[源码地图](code-map.md)：输入、决策、工具查询和回复的完整路径。
- [数据库](database.md)：PostgreSQL + pgvector 启动、表结构版本管理、知识重建和运行隔离。
- [业务工具](../backend/agent_harness/tools/README.md)与[业务目录](../backend/agent_harness/business_data/README.md)：查询条件、地区覆盖和费用计算。
- [政策 RAG](../backend/agent_harness/rag/README.md)与[资料维护](knowledge-business-inputs.md)：官方证据、适用范围和更新流程。
- [扩展指南](extending-agent.md)与[场景配置](contracts.md)：新增工具、Skill 或场景。
- [当前验证流程](current-validation.md)与[评测学习入口](../evaluation/README.md)：RAG、动态客户文本对话、trace 和指标。
- [接口边界](framework-compatibility.md)、[语音验证](voice-acceptance.md)与[排障](troubleshooting.md)：运行维护。
- [第三方来源](third_party/README.md)：依赖和许可。

当前功能与约束以源码、场景配置和上述文档为准。运行产物按每次验证命令保存到独立输出目录，源码仓库保留可复验的数据集和执行器。
