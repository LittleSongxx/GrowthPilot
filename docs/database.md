# PostgreSQL 与 pgvector

会话、消息、画像、后台任务、检查点和执行事件写入 PostgreSQL；政策 RAG 的分块、向量和关键词词频也写入同一个数据库服务。业务逻辑仍围绕 Agent、Skill、Memory 和工具组织，存储层通过 `DatabaseConfig` 明确传入连接与 schema。

## 本机启动

在 `GrowthPilot/` 中执行，已有 `.env` 时保留原文件，只补齐缺少的数据库配置：

以下命令使用 Docker Compose 插件形式 `docker compose`；只有独立命令时改用 `docker-compose`，其余参数相同。

```sh
# 首次配置时复制，已有文件不要覆盖
cp .env.example .env
chmod 600 .env
docker compose up -d --wait postgres
uv run growthpilot database init
uv run growthpilot database status
```

[compose.yaml](../compose.yaml) 使用 `pgvector/pgvector:pg17`，默认数据库与用户名为 `growthpilot`，密码为 `growthpilot_local`，端口仅绑定 `127.0.0.1:55432`。`.env` 中的默认连接为：

```dotenv
DATABASE_URL=postgresql://growthpilot:growthpilot_local@127.0.0.1:55432/growthpilot
DATABASE_SCHEMA=growthpilot
POSTGRES_PORT=55432
POSTGRES_PASSWORD=growthpilot_local
```

`POSTGRES_PORT`、`POSTGRES_PASSWORD` 用于 Compose；应用实际读取 `DATABASE_URL` 和 `DATABASE_SCHEMA`。更改前两项后同步更新连接串。Compose 的初始密码只在空数据卷初始化时使用，修改环境变量不会自动修改已有数据库角色的密码。

已有 PostgreSQL 服务时可以跳过 Compose，直接设置连接。服务需要安装 pgvector 扩展文件；初始化账号需有创建 schema、表和 `vector` 扩展的权限，或由管理员提前安装扩展。`DATABASE_SCHEMA` 用于同一数据库内的数据隔离，使用小写字母、数字和下划线，不能使用 PostgreSQL 系统 schema 名。

`growthpilot down` 只停止应用。`docker compose stop postgres` 停止本地数据库但保留数据；`docker compose up -d --wait postgres` 恢复服务。数据位于 Compose 命名卷 `growthpilot_postgres_data`，不在 `.run/` 中。

## 表结构与事务

[database.py](../backend/database.py) 提供原生 psycopg 连接池。连接按进程和数据库配置复用，普通查询自动提交；需要原子性的一组写入使用显式事务。会话消息使用显式 `seq` 序号，画像更新比较消息先后，不依赖数据库隐藏行号或模型完成时间。

[migrations/](../backend/migrations/) 中的编号 SQL 定义表结构。初始化时在事务中加迁移锁，检查已执行版本的 SHA-256，再顺序执行新版本，并将结果写入 `schema_migrations`。重复初始化不会重建数据。已经应用的 SQL 文件不能改写；新增表或字段应追加新的编号迁移，版本与内容不匹配时初始化会报错。

业务表和 RAG 表位于配置的同一个 schema。RAG 再通过 `namespace` 区分运行时知识与冻结评测语料，并通过 `scenario_id` 区分四个业务场景。默认运行时 namespace 为 `production`。固定教学正文、分块与来源保存在随 Git 发布的 `backend/agent_harness/rag/knowledge/v1/`。显式在线采集结果位于 `.run/knowledge/documents/` 和 `.run/knowledge/catalog.json`，BGE 模型缓存位于 `.run/knowledge/models/`；这些文件不承担在线查询存储。

当前 pgvector 使用精确余弦排序，关键词路使用 PostgreSQL 中保存的词频计算 BM25，再通过 RRF 合并候选。这个实现适合当前教学语料规模，便于观察完整检索过程；没有配置 HNSW、IVFFlat 或独立全文检索服务。语料扩大时，应先测量查询延迟与召回质量，再选择近似索引或专门检索服务。

## 从固定教学语料重建政策知识

默认构建会校验 `backend/agent_harness/rag/knowledge/v1/` 的文件哈希，并使用固定分块重建 PostgreSQL 中的 RAG 索引，无需访问政策网页；模型缓存缺失时仍可能下载 BGE 模型：

```sh
uv run growthpilot knowledge build
uv run growthpilot knowledge status
uv run growthpilot knowledge search --scenario meituan_rider --query '报名要交钱吗'
```

需要实验最新资料时，显式使用 `growthpilot knowledge build --refresh` 联网采集，或使用 `--from-cache` 复用 `.run/knowledge/catalog.json` 指向的上次采集结果；两个选项互斥。`growthpilot up` 会比对数据库与固定语料的四场景版本，不一致时恢复教学版本，并确保模型可用。更新分块、向量模型或检索算法后，使用已有 RAG 题集验证检索效果；历史评测报告保留其当时的存储与检索说明。

## 测试、评测与维护

工程回归使用真实 PostgreSQL，每次测试使用独立临时 schema，结束后释放连接并删除临时 schema。文本对话评测也为每轮创建独立业务 schema，同一客户的连续通话在本轮共享画像，评测结束后清理。更换 `GROWTH_RUNTIME_DIR` 只影响本地文件位置，不会隔离数据库；独立运行应使用独立 schema。具体入口见[测试说明](../tests/README.md)与[评测说明](../evaluation/README.md)。

数据库内容可以使用 PostgreSQL 自带工具备份。例如本地 Compose 默认数据库的备份与备份检查：

```sh
mkdir -p backups
docker compose exec -T postgres pg_dump -U growthpilot -d growthpilot -Fc > backups/growthpilot.dump
docker compose exec -T postgres pg_restore --list < backups/growthpilot.dump
```

固定教学语料由 Git 版本记录。若需保留在线采集实验，还应备份 `.run/knowledge/catalog.json` 及其引用的 `documents/` 快照；模型缓存可重新下载。恢复应在空的独立数据库中演练，并核对会话、画像和知识版本，不直接覆盖正在使用的库。

数据库支持多个连接不等于语音应用已经支持多进程部署。当前会话控制、音频和取消状态仍由单个宿主管理，启动恢复也按这个边界执行；继续按一个应用进程运行，不因迁移数据库直接增加 Uvicorn worker。
