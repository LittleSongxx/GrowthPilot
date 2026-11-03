# Growth Agent：四场景用增对话

Growth Agent 通过语音理解客户需求、解答业务问题，并在客户愿意时引导下载、注册和使用 App。当前支持美团骑手、美团商家、滴滴司机和度小满四个场景。


**客户语音 → DashScope ASR → AgentScope Agent（OpenAI 兼容文本模型）→ DashScope TTS → 浏览器播放**

Agent 根据客户当前问题自主选择工具和读取 Skill。四个场景共配置 17 个业务问题 Skill；Skill 提供判断顺序、分支和完成条件。客户画像在通话开始时自动注入，通话结束后由独立后台模型提取变化并保存。政策问题查询官方资料构建的本地 RAG；地区服务、费用方案和活动查询本地版本化业务目录。业务目录支持按地区配置生成结果与有条件的费用估算，目前未接入品牌实时业务接口。

## 启动

需要 Python 3.11、uv、Node.js 22.12+、Docker Compose 和本机 Chrome。业务记录和政策检索统一使用 PostgreSQL 17 + pgvector；也可以连接已有的兼容数据库。AgentScope 固定在 `e4c1e074dee3f0f6cca28ca29e934c4ec0605354`。

以下使用 `docker compose`；若安装的是独立 Compose 命令，替换为 `docker-compose`，参数相同。

```sh
cd growth-agent
uv sync --locked
# 仅首次创建；已有 .env 不要覆盖
cp .env.example .env
chmod 600 .env
# 在 .env 配置 AGENT_API_KEY、DASHSCOPE_API_KEY 和服务地址
# 启动本地 PostgreSQL，等待健康检查通过
docker compose up -d --wait postgres
uv run growth database init
uv run growth doctor --init-secrets
uv run growth validate
# 校验固定教学语料并准备四场景索引；首次可能下载本地向量模型
uv run growth up --build --scenario meituan_rider
```

统一入口为 [http://127.0.0.1:8787](http://127.0.0.1:8787)。静态页面与语音 API 共用一个应用服务；`growth down` 停止应用并保留数据库，`docker compose stop postgres` 可另行停止数据库。数据库默认只监听本机 `55432` 端口，数据保存在 Compose 命名卷中。

`DATABASE_URL` 指定连接，`DATABASE_SCHEMA` 默认是 `growth`。修改 Compose 端口或密码时同步修改连接串；当前工作目录、运行日志目录与数据库隔离无关。数据库初始化、表结构版本管理及备份见[数据库说明](docs/database.md)。应用仍按单个语音宿主运行，数据库连接不改变会话和音频状态的进程归属。

文本模型默认使用 DeepSeek，可通过下方配置切换。语音仍由 DashScope 提供 `qwen3-asr-flash` 和 `qwen3-tts-flash`，默认音色 `Cherry`；`DASHSCOPE_BASE_URL` 填 HTTPS 域名根地址。凭据只放服务端 `.env`；`doctor --init-secrets` 初始化会话密钥。

## 按用途配置和切换文本模型

Agent 统一使用 AgentScope 的 `OpenAIChatModel`，通过 OpenAI Chat Completions 接口调用模型。配置按用途划分，和具体厂商无关，无需修改场景、工具或 Skill：

| 前缀 | 用途 | 未指定连接时 |
| --- | --- | --- |
| `AGENT_*` | 业务对话 Agent | 使用配置文件默认地址和模型，密钥需填写 |
| `JUDGE_*` | 对话质量评审 | 沿用 Agent 的密钥、地址和模型 |
| `CUSTOMER_*` | 评测中的模拟客户 | 沿用 Agent 的密钥、地址和模型 |
| `PROFILE_*` | 通话后的客户画像提取 | 沿用 Agent 的密钥、地址和模型 |

在 `.env` 中选择下面一整组配置，替换已有同名变量并清理旧供应商的扩展参数，修改后重启应用。`BASE_URL` 填 API 基础地址，不要在末尾追加 `/chat/completions`。三个辅助角色的连接项留空时一起切换，也可以各自使用不同模型。下列型号用于演示配置方式，以服务商当前仍开放给账号的型号为准。

DeepSeek：

```dotenv
AGENT_API_KEY=填入你的 DeepSeek API Key
AGENT_BASE_URL=https://api.deepseek.com
AGENT_MODEL=deepseek-flash
AGENT_EXTRA_BODY='{"thinking":{"type":"disabled"}}'
CUSTOMER_EXTRA_BODY='{"thinking":{"type":"disabled"}}'
PROFILE_EXTRA_BODY='{"thinking":{"type":"disabled"}}'
JUDGE_EXTRA_BODY='{"thinking":{"type":"enabled"}}'
JUDGE_REASONING_EFFORT=high
JUDGE_TEMPERATURE=null
```

DeepSeek 的地址和模型名见[官方接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/)。其服务默认开启 high 思考，上面的完整示例和 `.env.example` 显式关闭 Agent、客户和画像提取的思考，并保留裁判的 high 思考；这些由配置控制，通用调用代码不会按厂商自动注入。开启思考时还要留出推理预算；DeepSeek 的思考与工具续接要求见[官方思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)。

GPT（OpenAI API）：

```dotenv
AGENT_API_KEY=填入你的 OpenAI API Key
AGENT_BASE_URL=https://api.openai.com/v1
AGENT_MODEL=gpt-4.1-mini
AGENT_EXTRA_BODY='{}'
JUDGE_EXTRA_BODY='{}'
CUSTOMER_EXTRA_BODY='{}'
PROFILE_EXTRA_BODY='{}'
JUDGE_TEMPERATURE=0
# 同时删除旧的 JUDGE_REASONING_EFFORT，避免把 DeepSeek 的 high 参数发给此型号。
```

示例型号支持流式和工具调用，但已列入弃用计划，计划于 2027-01-14 停用；切换前核对 [GPT-4.1 mini 模型说明](https://developers.openai.com/api/docs/models/gpt-4.1-mini)和[接口支持范围](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。选择其他 GPT 型号时也要确认它支持 Chat Completions 下的工具调用；仅支持 Responses API 的型号不能直接用于本项目。

Qwen（阿里云百炼北京地域）：

```dotenv
AGENT_API_KEY=填入你的百炼 API Key
AGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AGENT_MODEL=qwen-plus
AGENT_EXTRA_BODY='{"enable_thinking":false}'
JUDGE_EXTRA_BODY='{"enable_thinking":false}'
CUSTOMER_EXTRA_BODY='{"enable_thinking":false}'
PROFILE_EXTRA_BODY='{"enable_thinking":false}'
JUDGE_TEMPERATURE=0
# 同时删除旧的 JUDGE_REASONING_EFFORT，避免保留上一家服务的推理参数。
```

Qwen 的非标准参数通过 `extra_body` 传入；此例为四类调用分别关闭思考。密钥、地域和计费方案要与地址对应；其他地域或业务空间专属地址见[百炼 Base URL](https://help.aliyun.com/zh/model-studio/base-url)，型号和参数支持见[OpenAI 兼容接口](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。即使文本和语音都用百炼，也要分别配置 `AGENT_API_KEY` 与 `DASHSCOPE_API_KEY`；文本 URL 包含 `/compatible-mode/v1`，语音 URL 只填域名根地址。

每个角色都支持下面这些配置；把 `AGENT` 替换为 `JUDGE`、`CUSTOMER` 或 `PROFILE` 即可。默认值集中在 [models.yaml](backend/config/models.yaml)，环境变量覆盖对应配置，进程环境变量优先于 `.env`。

| 配置 | 用途 |
| --- | --- |
| `AGENT_API_KEY`、`AGENT_BASE_URL`、`AGENT_MODEL` | 该角色的密钥、API 基础地址和模型 ID |
| `AGENT_TEMPERATURE` | 采样温度；设为 `null` 时省略温度参数，适用于不接受自定义温度的模型 |
| `AGENT_MAX_TOKENS` | 该角色的输出上限 |
| `AGENT_MAX_TOKENS_PARAM` | 默认 `max_tokens`；服务商要求另一种参数名时改为 `max_completion_tokens` |
| `AGENT_REASONING_EFFORT` | 仅为支持该参数的模型配置推理强度；未配置时不发送 |
| `AGENT_EXTRA_BODY` | JSON 对象形式的供应商扩展参数，例如 Qwen 的 `enable_thinking`；不能覆盖消息、模型、工具和标准生成参数 |

**连接可以沿用 Agent，生成参数各角色独立。** 例如四个角色都使用不接受温度、且要求 `max_completion_tokens` 的型号时，应分别设置四个 `*_TEMPERATURE=null` 和四个 `*_MAX_TOKENS_PARAM=max_completion_tokens`。`EXTRA_BODY` 和 `REASONING_EFFORT` 也不互相继承；YAML 中默认不发送任何思考参数，`.env.example` 则显式配置了上述 DeepSeek 行为。删除这些配置后由服务商自己的默认值决定是否思考。

单独指定裁判等角色时，可只改同一服务中的模型 ID；如果改为不同地址，必须同时提供该角色的密钥，例如：

```dotenv
JUDGE_API_KEY=填入评审服务的 API Key
JUDGE_BASE_URL=https://api.openai.com/v1
JUDGE_MODEL=gpt-4.1-mini
JUDGE_EXTRA_BODY='{}'
JUDGE_TEMPERATURE=0
# 同时删除旧的 JUDGE_REASONING_EFFORT。
```

切换时替换整组配置，不要只改地址和模型：将四个角色的 `EXTRA_BODY` 改为新服务所需的值，删除不再适用的 `REASONING_EFFORT`，并核对温度和 token 参数。例如改为上述 GPT 示例时，四个扩展对象设为 `{}`，删除旧的 `JUDGE_REASONING_EFFORT=high`，将 `JUDGE_TEMPERATURE` 恢复为 `0`。已有的 `JUDGE_MODEL` 等显式配置会继续生效，删除或留空后才会沿用 Agent。项目按用途读取配置，不再读取 `DEEPSEEK_*`、`OPENAI_*` 或 `LLM_*`；旧配置请迁移到上面的角色变量。

“兼容 OpenAI 格式”不代表所有模型能力相同。Agent 需要 Chat Completions 的流式输出、函数工具调用及工具结果续接；画像提取、模拟客户与评审使用 `response_format={"type":"json_object"}`。服务商不支持的参数需按其文档调整；模型输出质量和实际工具使用仍要通过小样本对话评测确认。`uv run growth doctor --online` 检查实际文本连接，后续验证见[当前验证流程](docs/current-validation.md)。

## 四个场景的工具

每个场景向模型开放共同的 `search_policy`、`send_download_sms` 和下表的 3 个目录查询或金融费用试算。骑手、司机额外开放 `estimate_work_budget`，商家额外开放 `estimate_order_contribution`。业务函数共 16 个，骑手、司机、商家各注册 6 个，金融注册 5 个；AgentScope 另提供原生 `Skill` 工具。宿主单独提供系统通话工具 `end_call(message)`，参数中的一句结束语直接用于播放，不计入场景业务工具。

| 场景 | 业务查询工具 | 解决的问题 |
| --- | --- | --- |
| 美团骑手 | `search_nearby_stations`、`query_delivery_pay`、`search_vehicle_rentals` | 附近站点、当地计酬、租电动车的租金和押金 |
| 滴滴司机 | `search_driver_service_points`、`query_driver_earnings`、`search_car_rentals` | 当地服务点、收入与成本、租车方案 |
| 美团商家 | `query_delivery_options`、`query_merchant_fee_plans`、`query_merchant_offers` | 配送方式、收费方案、活动适用条件 |
| 度小满 | `query_product_options`、`query_financial_offers`、`estimate_financing_cost` | App 产品功能、活动条件和费用试算 |

两个新增预算工具只按客户提供或明确确认的条件计算。出勤预算区分固定成本、每日变动成本和可退押金；收入未知时计算成本覆盖门槛，收入假设明确时才算余额。订单贡献区分尚未扣平台及配送费的订单实收、已经扣完这些费用的到账金额，避免重复扣费；结果不等于门店净利润。未知成本不能填零，计算不会产生真实订单或个人收入预测。

业务目录依次使用精细地区记录、城市配置与通用方案，覆盖 60 个城市、11 类模板和 67 个业务分支。“我在长沙”可以直接查询，未收录县镇也可返回通用方案。不同服务实体的地址和联系电话各不相同，同一实体重复查询保持稳定。没有位置或同时涉及多个城市时先澄清；非法参数和已过期数据直接返回原因。

工具结果提供业务方案、地址电话、金额、条件、有效期与必要的估算假设。数据来源、命中层级和生成过程保留在内部审计中。Agent 引用实际返回值，不补造联系人或距离，不将估算说成个人收入、额度或审批保证。客户表示已下载或注册后，可以继续指导下一步；完成情况以客户反馈为依据。

`send_download_sms()` 在客户同意后，向当前会话客户发送对应场景的 App 下载短信，无需模型填写收件人或链接。同一会话重复调用复用发送回执，Agent 依据工具结果确认发送状态。教学实现使用本地模拟发送并保存记录，模型工具说明和返回值只包含业务含义，不暴露模拟实现。

详见[业务工具](backend/agent_harness/tools/README.md)和[业务目录](backend/agent_harness/business_data/README.md)。

## 按业务问题使用 Skill

| 场景 | 业务 Skill 数 | 主要问题 |
| --- | --- | --- |
| 美团骑手 | 5 | 时间与配送模式适配、起步成本、租车选择、保障边界、报名材料 |
| 美团商家 | 4 | 配送方式、单笔贡献、活动适配、入驻资质 |
| 滴滴司机 | 4 | 城市人车准入、出勤预算、租车选择、报名准备 |
| 度小满 | 4 | 身份收费核验、注册授权辨析、费用理解、活动条件 |

[系统提示词](backend/agent_harness/prompts/system.md)只定义当前客服的角色、客户背景、下载使用 App 的目标和必要边界。工具 Schema 与 Skill 元信息由框架提供，Agent 根据客户需求和对话进展自主选择能力、组织沟通。

每份 Skill 的主要路径都要由已开放工具、实际可检索知识、客户自述和 Agent 的合理判断承接。场景加载时校验 `required_tools`，但工具依赖齐全不等于知识已经覆盖。正文自包含，当前原生查看器不依赖额外 `references` 文件读取能力。完整清单、范围与扩展规则见 [Skill 说明](backend/agent_harness/skills/README.md)和[扩展指南](docs/extending-agent.md)。

## 政策知识库

固定教学语料随 Git 发布在 [backend/agent_harness/rag/knowledge/v1/](backend/agent_harness/rag/knowledge/v1/)，包含四场景的 42 份清洗正文、来源元数据和 1285 个分块，与 RAG 评测的冻结语料一致。`catalog.json` 记录文件哈希，构建前校验内容完整性；保存的项目路径只使用相对路径或以 `/growth-agent` 开头的项目路径，不包含作者的本机目录。检索使用本地中文 BGE 向量、PostgreSQL pgvector 余弦检索和词频 BM25 混合检索。工具返回条款正文及出处，检索范围由当前场景决定。

```sh
uv run growth knowledge build --scenario meituan_rider
uv run growth knowledge search --scenario meituan_rider --query '报名要交钱吗'
uv run growth knowledge status
# 仅在需要实验最新资料时，显式联网采集并构建
uv run growth knowledge build --refresh
# 复用上次在线采集的分块，兼容原来的缓存构建方式
uv run growth knowledge build --from-cache
```

默认的 `growth knowledge build` 校验固定语料并重建数据库索引，不重新采集网页；首次准备 BGE 模型仍可能联网下载。`growth up` 校验固定语料、比对四场景数据库版本并确保模型可用，版本不一致时恢复固定教学版本。因此，`--refresh` 或 `--from-cache` 建立的实验索引会在下次 `growth up` 时恢复为教学版本；这两个选项不能同时使用。

进程状态、日志、模型缓存和显式在线采集结果继续写入被 Git 忽略的 `.run/`；学员需要的固定教学语料独立保存在版本管理中的 `backend/agent_harness/rag/knowledge/v1/`，可检索分块、向量和词频保存在 PostgreSQL。这里冻结的是知识输入，政策有效期仍按实际日期过滤，不同日期的检索结果可能变化；模型采样和云端服务也不保证逐字输出相同答案。回答须依据当次取得的正文，缺少适用依据时补查、追问或说明缺口。参考 [RAG 说明](backend/agent_harness/rag/README.md)和[业务资料维护](docs/knowledge-business-inputs.md)。

命令示例中的文件路径均相对 `growth-agent/` 项目根目录。程序根据安装位置定位配置、语料和运行目录，读写文件时会动态解析为当前机器的绝对路径，不依赖作者的目录结构；日志、报告和教学资料中的展示路径使用相对路径或 `/growth-agent/...`。其中 `/growth-agent` 是展示用的项目标识，不要求在系统根目录创建同名文件夹。

本版教学语料冻结自 2026-10-30 的采集结果：43 份配置来源成功采集 42 份；福州驾驶员办证指南因站点 `robots.txt` 返回 HTTP 403 被排除。已有福州准入细则不能代替具体办证入口，索引可用也不表示全部知识已覆盖。正式检索评测使用按场景标注的相关原文；仍需保留适用地区、时间、条件和缺项。

## 使用与验证

1. 选择场景和客户标识，点击「开始通话」并允许麦克风。
2. 直接说话，页面自动判断发言结束并提交语音，显示转写、工具轨迹、回答和客户画像，并播放回复。后续发言无需再点击按钮。
3. 可以尝试：“我 50 岁，在长沙，想跑众包。附近有站点吗？没有车的话租车多少钱？”随后更正地点或补充顾虑，观察 Agent 如何组合查询。
4. Agent 思考或讲话时也可开口插话：页面立即停播并停止旧一轮回答，继续接收新发言。Agent 判断可以收尾时，播放一句结束语，实际播完后自动挂断；期间开口插话可继续聊。「结束通话」也可随时取消当前处理并释放麦克风等资源。后台随后整理客户画像，页面显示处理状态。新通话自动载入同客户同场景的有效画像；上次任务尚未完成时可能暂时读到旧资料。

「静音」暂停接收发言，「取消静音」后继续。默认不保存录音。ASR 在每次发言结束后识别完整录音；Agent 生成最终回答后，TTS 返回 PCM 音频流，浏览器边收边播。「重播回复」复用当前通话已收到的完整音频；TTS 失败保留文字回复，只重试语音合成，不重新调用业务工具。通话开始、超时、结束和资源释放由宿主管理。

```sh
# 工程回归使用真实 PostgreSQL 的临时 schema，不调用云端模型
docker compose up -d --wait postgres
uv run pytest -q
npm --prefix frontend test
npm --prefix frontend run build
uv run python -m evaluation.checks.contracts --output .run/checks/growth-contracts.json
uv run python -m evaluation.checks.business_coverage --output .run/checks/growth-business-coverage.json
# 两类正式评测默认只预览
uv run growth evaluate --suite rag --scenario meituan_rider --limit 3 --run my-rag-preview
uv run growth evaluate --suite dialogue --scenario meituan_rider --limit 3
# 显式执行：本地检索，以及会调用云模型的动态客户对话
uv run growth evaluate --suite rag --run my-rag-run --execute
uv run growth evaluate --suite dialogue --execute
```

正式评测只有 **RAG 检索**与**文本 Agent 端到端对话**。四场景各有 100 道 RAG 题和 100 个动态客户案例，可单独运行；每位客户默认连续通话 2 次，全量产生 800 条轨迹。不同客户默认 16 路并发，同一客户顺序通话并保留本轮记忆，不同完整评测之间隔离数据。客户使用 `CUSTOMER_*` 指定的文本模型（连接默认沿用 Agent），根据 Agent 实际回复继续沟通，无须部署专用模型。

RAG 指标为 Recall/Precision@1、3、5、8 和 MRR@8；端到端按 1～5 分评价目标完成度、问题解决度、事实可靠性和沟通体验，并统计每通 Agent token 与客户轮首 token 时延（TTFT）P50/P95。TTFT 从客户文本提交给 Agent 到最终面向客户回复的首个非空文本块到达，包含此前的工具调用等待，以流式文本块近似首 token；完整回复耗时仅留作诊断。报告保留各维度分数分布、低分理由及缺测情况，不合成总分。目标包括合理推进、咨询解决与恰当结束拒绝。题目由公开资料和模型辅助标注构造，客户设定为自建案例；离线成绩不是实际用户转化率。

正式结果分为 `evaluation/reports/rag/` 和 `evaluation/reports/agent/`。Agent 每轮按“模型名_评测日期”自动新建目录，日期包含时分秒，例如 `deepseek-flash_2026-11-02_20-25-06`，目录内保留完整清单、轨迹、裁判和报告。`--run` 可选：RAG 用于指定目录名，Agent 用于记录运行标签。预览写入 `.run/evaluation-previews/`，工程检查写入 `.run/checks/`。已经取得的 RAG、Agent 全量运行和独立补跑见[本轮评测记录](evaluation/reports/README.md)；原 Agent 全量运行的未完成状态仍保留。

Memory、Skill 作为完整 Agent 的能力自然参与对话，不设独立评测或消融。工程测试继续保护它们的加载、持久化和权限。报告保留原始 trace、版本、裁判输出、错误和用量；学习与运行步骤见[评测说明](evaluation/README.md)，其他维护检查见[当前验证流程](docs/current-validation.md)。

| 目录 | 内容 |
| --- | --- |
| [backend/agent_harness/](backend/agent_harness/README.md) | Agent、Toolkit、Skill、Memory 和生命周期 |
| [backend/agent_harness/business_data/](backend/agent_harness/business_data/README.md) | 版本化业务目录、地区解析、方案筛选和费用计算 |
| [backend/agent_harness/rag/](backend/agent_harness/rag/README.md) | 教学语料校验、官方资料采集、索引和证据检索 |
| [backend/agent_harness/rag/knowledge/v1/](backend/agent_harness/rag/knowledge/v1/) | 随 Git 发布的固定教学正文、来源、分块和文件哈希 |
| [backend/scenarios/](backend/scenarios/) | 四场景目标、字段、工具和 Skill 引用 |
| [frontend/](frontend/README.md) | 录音、播放和对话观察 |
| [evaluation/](evaluation/README.md) | RAG 检索、动态客户对话、四维质量评分与两项效率指标 |
| [tests/](tests/README.md)、[docs/](docs/README.md) | 回归测试、架构与扩展说明 |
