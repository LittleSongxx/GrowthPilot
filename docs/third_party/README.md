# 上游资源归属

AgentScope 固定来源为 [官方提交 e4c1e074](https://github.com/agentscope-ai/agentscope/tree/e4c1e074dee3f0f6cca28ca29e934c4ec0605354)，通过依赖锁安装。原始 [LICENSE](AgentScope-LICENSE) 保留。

业务存储与政策 RAG 使用以下组件。Python 依赖的具体版本见 [uv.lock](../../uv.lock)，数据库镜像见 [compose.yaml](../../compose.yaml)，没有复制上游源码：

| 组件 | 用途 |
| --- | --- |
| FastEmbed / ONNX Runtime | 在本机运行中文向量模型 |
| `BAAI/bge-small-zh-v1.5` | 中文检索向量；由 FastEmbed 在构建或启动准备时下载到本地缓存 |
| [PostgreSQL 17](https://www.postgresql.org/docs/17/index.html) | 会话、画像、任务和政策分块等持久数据，事务与 schema 隔离 |
| [pgvector](https://github.com/pgvector/pgvector) | PostgreSQL 向量扩展，当前使用 512 维向量的精确余弦距离检索 |
| [Psycopg 3 / psycopg_pool](https://www.psycopg.org/psycopg3/docs/) | Python 数据库驱动与连接池 |
| Beautiful Soup | 官方 HTML 正文提取 |
| pypdf | 官方 PDF 文本提取 |

关键词检索由项目在 [index.py](../../backend/agent_harness/rag/index.py) 中实现：生成中文双字词项，将词频保存在 PostgreSQL，使用 SQL 计算 BM25，再与向量结果通过 RRF 合并。它不依赖额外的 BM25 或全文检索扩展；检索参数与适用规模见 [RAG 说明](../../backend/agent_harness/rag/README.md)。本次迁移在 PostgreSQL 17.11、pgvector 0.8.7 上完成验证；实际服务版本可用 `growth database status` 查看。

软件包与模型分别遵循其随附许可。官方资料的出处保存在 [sources.yaml](../../backend/agent_harness/rag/sources.yaml) 和 [backend/agent_harness/rag/knowledge/v1/](../../backend/agent_harness/rag/knowledge/v1/) 中的文档元数据、分块中。本版随源码发布 42 份资料整理后的教学正文与 1285 个分块，并保留原始 URL、内容哈希及抓取时间；没有复制原始网页、PDF 或 JSON 响应文件。第三方正文的权利归属不因整理或随源码发布而改变，也不自动适用本项目的软件许可证；出处记录本身不构成第三方授权说明。

`growth knowledge build --refresh` 显式采集的原始响应和整理结果位于 `.run/knowledge/documents/`，模型缓存位于 `.run/knowledge/models/`，两者均不随 Git 发布。可检索分块、向量、词频与语料版本存放在 PostgreSQL。

语音界面使用浏览器标准 Web Audio 与 `AudioWorklet` 采集和播放音频，后端通过公开 HTTP API 调用语音服务。
