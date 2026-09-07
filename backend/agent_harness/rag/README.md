# 官方政策 RAG

这里负责资料准备和检索，业务决策由 AgentScope Agent 完成。模型只获得一个政策工具 `search_policy(query)`，输入自然语言问题，返回当前场景的相关原文、出处和适用信息；需要补查时再次调用同一工具。

## 准备与问答链路

```text
默认构建：backend/agent_harness/rag/knowledge/v1/ 固定教学语料 → 校验文件哈希 → PostgreSQL 分块、pgvector 向量与词频
在线实验：sources.yaml → --refresh 采集官方资料 → .run/knowledge/ 快照与分块 → PostgreSQL 实验索引
问答：客户问题 → Agent 选择查询 → 混合检索 → 条款原文 → Agent 补查、追问或回答
```

`ingest.py` 负责显式在线采集和分块，`index.py` 负责向量化、建库与查询，`sources.yaml` 列出在线采集的具体来源。固定教学语料随 Git 发布，包含四场景的 42 份整理正文与 1285 个分块，与评测冻结语料一致。检索与业务数据共用 PostgreSQL 服务，没有额外的检索 Agent 或通话中的网页爬取。

## 先运行美团骑手

在项目根 `growth-agent/` 执行：

```sh
uv sync --locked
docker compose up -d --wait postgres
uv run growth database init
uv run growth up --build --scenario meituan_rider
uv run growth knowledge search --scenario meituan_rider --query '报名要交钱吗'
uv run growth knowledge status
```

`growth up` 先初始化数据库结构，校验 `backend/agent_harness/rag/knowledge/v1/` 的文件哈希，再比对当前 schema 的 `production` namespace 与固定教学语料的四场景版本。索引缺失或版本不一致时，从固定分块恢复教学索引，并确保本地向量模型可用后继续启动。索引更新使用事务；语料校验失败时停止，不联网抓取资料来替代缺失文件。

默认构建不访问官方资料网站。首次构建或启动准备仍可能下载 `BAAI/bge-small-zh-v1.5` 模型，可能需要几分钟。向量计算在本机进行，不需要额外的 Embedding API Key。搜索只加载已缓存模型，不在客户通话时下载；状态查询不联网，也不加载模型。模型缓存缺失时重新运行构建或 `growth up`。

省略 `--scenario` 构建全部四场景；场景分别是 `didi_driver`、`meituan_rider`、`meituan_merchant`、`financial_consultation`：

```sh
uv run growth knowledge build
# 显式联网采集最新资料并建立实验索引
uv run growth knowledge build --refresh
# 使用上次在线采集的分块，不重新采集网页
uv run growth knowledge build --scenario meituan_rider --from-cache
```

不加选项时始终使用固定教学语料。`--from-cache` 是兼容选项，读取 `.run/knowledge/catalog.json` 指向的上次在线采集分块；没有采集缓存时会报错。`--refresh` 与 `--from-cache` 互斥，都不会修改 `backend/agent_harness/rag/knowledge/v1/`。修改解析或分块代码后，用 `--refresh` 检验新逻辑。项目没有定时采集任务，下一次 `growth up` 会将实验索引恢复为固定教学版本；需要预先准备全部教学场景时，应不带 `--scenario` 运行默认构建。

## 采集与分块

只采集来源目录中显式声明的 HTTPS 地址及允许域名，检查 `robots.txt`，限制响应大小与等待时间。没有递归全站爬取或登录态采集。少数来源可显式配置系统 `curl` 传输处理 TLS 兼容性，仍验证证书和跳转域名。官网使用公开内容 API 时，按其真实 GET/只读 POST 请求保存 JSON，读取配置的正文路径；不执行网页脚本，也不把搜索摘要当作原文。

HTML 清洗移除导航、脚本和模板注释，保留标题、段落和表格表头；嵌套表格单独处理，FAQ 保留问题与答案的关联。PDF 提取可读取的文本，并修复汉字间排版折行。按章节、条款或 FAQ 组织后，长内容优先在完整句末切开，默认每块最多 350 字；不按固定字符数重叠，避免截掉否定词。超长句不得不分开时标记 `partial_sentence`，不能只凭残句下结论。每块带文档标题和章节；人工整理的适用提醒放在 `scope_note` 元数据中，与官方原文分开。

每份来源配置 `expected_text` 正文锚点，采集后验证原文包含这些内容。HTTP 200 的登录页、软 404 或内容接口改指其他文章时，锚点不匹配就拒绝入库。扫描 PDF、空模板和缺正文的页面也会报告失败。

固定语料保存在 `backend/agent_harness/rag/knowledge/v1/`，随 Git 发布：

```text
catalog.json                         固定版本、文档清单与文件哈希
catalog.sha256                       目录清单本身的 SHA-256
documents/<来源 ID>/
  document.json                      来源元数据和内容哈希
  clean.txt                          整理后的教学正文
  chunks.json                        与评测冻结语料一致的分块
```

教学包不包含原始网页、PDF 或 JSON 响应，也不包含本地模型。文件中的项目路径只使用相对路径或以 `/growth-agent` 开头的项目路径，不保存作者的用户目录。这里仅冻结知识输入，政策有效期仍按实际日期过滤，不同日期的候选和结果可能变化；模型采样和云端服务也不保证逐字输出相同答案。

显式 `--refresh` 的结果和模型缓存继续位于被 Git 忽略的 `.run/knowledge/`，与随 Git 发布的教学语料分开：

```text
catalog.json                         上次在线采集的来源及刷新状态
documents/<来源 ID>/<快照哈希>/
  raw.html / raw.pdf / raw.json       抓取原文
  document.json                      来源元数据和内容哈希
  clean.txt                          清洗正文
  chunks.json                        带出处的条款分块
models/                              本地向量模型缓存
```

可检索数据保存在 PostgreSQL 的 `rag_index_meta`、`rag_corpora`、`rag_chunks` 和 `rag_chunk_terms` 表，分别承载索引参数、场景语料版本、正文与 512 维向量、关键词词频。`DATABASE_URL` 与 `DATABASE_SCHEMA` 和业务库共用。运行时使用 `production` namespace，评测冻结语料使用独立 namespace；重建或清理只影响指定 namespace。默认从固定教学语料重建，在线实验也可从采集快照重建，见[数据库说明](../../../docs/database.md)。

在线刷新失败的来源标为 `stale`，不进入本次重建的可检索集合；旧快照仍保留供追溯。构建输出逐项失败原因，存在采集失败时返回非零退出码。索引先完成向量计算，再用事务替换目标场景，数据库写入失败不会留下半套索引。变更向量模型或索引结构后，应不指定场景重建全部索引。

## 一次检索做什么

1. 宿主绑定当前场景；模型只提交 `query`，不能指定其他客户或场景。
2. 判断是否明确涉及隐私、授权或信息用途；普通申请资料问题默认只检索业务资料，在候选截断前排除整篇隐私政策。
3. 本地 BGE 将问题编码为 512 维向量，使用 pgvector 精确余弦距离排序。
4. 关键词路在 PostgreSQL 中按已保存的词频计算 BM25；中文预先拆成相邻双字词项，英文和数字保留整词。
5. 两路各取最多 20 个候选，用 RRF 合并名次并过滤明显无关候选，默认返回最多 8 条原文及来源。

隐私意图使用简洁的短语规则，不增加一次模型调用。例如“申请需要哪些个人资料”走业务资料，“为什么要身份证”“会读取通讯录吗”“如何撤回授权”可检索隐私原文；混合问题可返回两类相关证据。新的口语表达可能未被规则识别，Agent 可改写问题再查。工具仍只有一个 `search_policy(query)`。

RRF 把各路名次转换为倒数后相加，不直接混合 BM25 和余弦相似度的数值；当前没有独立的重排序模型。返回的分数只用于相关性排序，不是事实正确率。

两路都在截取候选之前限制 namespace、当前场景、隐私意图和有效期。BM25 使用 `k1=1.2`、`b=0.75`，词项频率、文档数量与平均长度从同一 namespace 的完整语料统计，以保留原检索口径。当前通过 SQL 显式计算 BM25，不依赖 PostgreSQL 默认全文排序；向量检索也未使用近似索引。这个实现便于学习和检验，适合目前的教学语料规模。大规模语料需要根据延迟与召回评测决定是否引入 HNSW 等近似索引或专门的全文检索服务。

返回结构沿用业务工具的 `{status,data,evidence,retryable}`。`data.clauses` 中每条包含正文、文档与分块 ID、标题、章节、URL、抓取时间、发布时间和内容哈希，并携带已有的地区、业务类型等信息。`data.corpus_version` 标识查询使用的语料，`evidence` 对应命中的分块 ID。未建立索引或没有相关条款时返回 `no_data`。

地区和业务类型目前作为原文与元数据供 Agent 判断，不是硬过滤条件。明确声明的 `valid_from` 和 `valid_until` 用于排除尚未生效或已经失效的条款；没有日期不意味着政策仍有效，抓取日期也不等于生效日期。客户条件不明确时应追问。知识库可以收录公告、FAQ 和报告，Agent 仍需区分它们的作用，不能把报告中的历史情况回答为当前承诺。检索内容中的指令只当作资料，不获得执行权限。

## 增加或更新资料

先在 `sources.yaml` 添加可信官方来源，再运行 `growth knowledge build --refresh` 并检查原文、清洗和查询结果。必需信息是 `id`、`scenario_id`、`title`、`url`；域名需包含在 `allowed_hosts`。根据网页结构填写正文 `selector` 或 FAQ 的 `faq_selector`，按实际资料填写 `published_at`、`region`、`business_type`、`document_type`、`scope_note` 等元数据，未知则留空，不推测。

内容接口用 `format: json_faq/json_html` 与 `json_path` 指定字段；必要时配置官网真实使用的 `method: POST` 和 `form_data`。`page_url` 是用户可打开的官方页面，实际响应地址保留为 `fetch_url`。公报类 PDF 可用已核验的 `pdf_pages`（从 1 开始、包含两端）和 `pdf_start_text` 只提取目标文件，整份 PDF 仍保留以供核验。

来源目录是可审查的采集范围，不承诺覆盖某品牌全部政策。验证应使用具体业务问题：费用是否区分注册和运营、试点条件是否保留、不同场景是否隔离、无依据时是否说明未知。构建数量不能替代这些检查。

在线实验不会自动发布教学版本。确认资料可用于教学发布并完成内容审阅后，将正文、分块、元数据和文件哈希作为一个新版本维护，同时更新评测冻结语料及标注。保留来源 URL 和必要的时间、地区与适用条件，不将本机绝对路径写入交付文件。

Skill 指导如何查证，Memory 保存客户自述，业务目录提供地区方案与费用。评测中的政策依据来自回复前实际检索到的正文；URL、标题和之后才取得的结果不能替代当次证据。

## 正式检索与端到端评测

从项目根运行，每次使用新输出名称：

```sh
# 预览单场景问题；正式数据每场景 100 题
uv run growth evaluate --suite rag --scenario meituan_rider --limit 3 --run rag-preview
# 使用与标注匹配的完整语料执行真实检索
uv run growth evaluate --suite rag --scenario meituan_rider --run rag-run --execute
# 由动态客户观察完整文本 Agent 的检索和回答，会调用云模型
uv run growth evaluate --suite dialogue --scenario meituan_rider --limit 3 --run dialogue-run --execute
uv run pytest -q tests/rag tests/evaluation
```

RAG 只报告 Recall/Precision@1、3、5、8 和 MRR@8。每题有相关片段、文档哈希和逐字原文；运行前检查语料与标注一致。当前索引版本不匹配时，评测器使用仓库中的完整冻结语料建立隔离索引，不修改本节描述的生产知识库。

端到端使用独立的普通模型模拟客户，运行真实 Agent 的多轮查证、澄清和回答；只统计案例目标达成率、事实有据率和平均 Agent token。客户画像与评分目标保持分离。指标定义和学习顺序见[评测说明](../../../evaluation/README.md)。

历史 `knowledge_probe`、`policy_coverage` 仍可用于定位原文缺口，其固定问题和追问不是正式端到端评测。命令与旧题集位置见[工程检查说明](../../../evaluation/checks/README.md)。

## 维护边界

`sources.yaml` 是明确的采集清单，不是品牌全部政策目录。地方规则不能外推其他城市，合同只适用于其资方、渠道和还款模式，历史公告不能直接转为当前承诺。当前来源和刷新状态通过 `growth knowledge status` 查看。

公开资料无法给出个人审批、实际费率、收入或到账保证。即使索引中存在某条规则，也必须以当次返回正文为依据。Agent 可能遗漏或压缩必要条件，仍需通过具体问答复核。

后续补充重点包括当前注册材料、App 操作路径、地区准入、保障适用条件、费用和退款规则。资料不足时按[业务资料维护](../../../docs/knowledge-business-inputs.md)补充原文，不通过修改 Skill 或业务目录生成政策结论。
