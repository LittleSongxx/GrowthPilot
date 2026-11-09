# 扩展工具与 Skill

先明确客户要解决的具体决策、哪些条件会改变判断，以及怎样算解决。当前有 17 个场域业务 Skill，清单见 [Skill 说明](../backend/agent_harness/skills/README.md)。场域限定身份、目标、工具和知识范围，Skill 提供具体问题的 SOP。

## 先确认能力能够承接

每个候选 Skill 先说明客户问题、必要事实、具体工具或知识来源、Agent 负责的判断、预期产物和代表性回放。主要路径必须由现有能力或明确的小幅扩充完成：

- 澄清顾虑、比较已知条件、识别冲突和根据更正调整分支，由 Agent 承担。
- 通用政策和操作说明使用 `search_policy`；地区、方案、价格、服务点等数据使用业务目录；客户给出的成本和意愿保留为自述或确认假设。
- 客户同意接收下载链接后使用 `send_download_sms()`，按当前会话客户与场景发送，以工具结果判断发送是否成功。
- 缺少资料时，可补可维护的公开正文、具备依据的目录字段或确定性计算。依赖尚未取得的内部接口、个人审批、实时订单或完整合同的能力，应缩小范围或暂缓。
- 典型问题若多数只能转客服，先解决核心能力缺口。允许指出具体未知项，不能靠边界说明掩盖主要路径无法完成。

来源登记不等于可检索。补资料后实际构建知识库，使用具体客户问题检查返回正文是否包含必要的地区、时间、主体、条件和操作步骤。2026-10-30 的构建成功收录 43 份来源中的 42 份，福州驾驶员办证指南因 `robots.txt` 返回 HTTP 403 被排除；已有准入细则不能替代该指南的报名、考试和结果查询入口。目录的城市覆盖也不代表对应地方政策已覆盖。

## 扩展工具

1. 在 [tools/](../backend/agent_harness/tools/) 对应场景文件添加带 `@business_tool`、类型注解和清晰 docstring 的函数。
2. 将检索、计算或发送操作放入独立适配器，返回必要的业务结果和失败原因；查询还需保留来源、适用条件和有效期，发送需保留回执与重复调用保护。
3. 在 [registry.py](../backend/agent_harness/tools/registry.py) 登记函数，并在场景 `tools` 中声明名称。`FunctionTool` 根据签名生成参数 Schema，工具边界检查权限与参数。
4. 使用本地目录时，在对应场景的 `catalog.json` 配置记录、城市参数和通用方案，例如[骑手目录](../backend/scenarios/meituan_rider/catalog.json)。保留原始条件与实际采用条件；缺少地区、城市冲突、无效参数和过期数据分别处理。
5. 参数用法写入工具定义，业务判断写入对应 Skill。新工具在场景 `tools` 中开放后，再声明为 Skill 的 `required_tools`；缺失依赖会导致场景加载失败。
6. 补充有意义的回归：场景隔离、严格入参、地区范围、过期、计算、原始审计和模型结果完整性。按[当前验证流程](current-validation.md)运行，再做所需的真实知识和对话验证。

跨场景工具统一放在 [common.py](../backend/agent_harness/tools/common.py)，场景专用工具放在对应场景文件中。例如骑手和司机共用的 `estimate_work_budget` 计算客户确认条件下的成本门槛与余额，收入未知时不推测；[meituan_merchant.py](../backend/agent_harness/tools/meituan_merchant.py) 中的 `estimate_order_contribution` 区分 `before_fees` 和 `after_fees`，后者不再扣已结算的平台与配送费用。两者复用 [business_data/budget.py](../backend/agent_harness/business_data/budget.py) 的计算内核，保留公式和假设，不能用零填未知成本，也不能把单笔贡献称为门店净利润。

接入业务服务时，替换对应数据适配器，记录来源、版本和有效期。明确区分官方规则、本地配置、实时结果与客户自述。结果投影只整理展示字段，不能改变金额、联系方式或适用条件，也不能把估算变成已核验的个人结果。

## 扩展问题型 Skill

业务技能放在 `backend/agent_harness/skills/<scenario_id>/<skill_id>/SKILL.md`，场景分组与 `backend/scenarios/` 保持一致。frontmatter 的 `name` 与场景声明的 Skill ID 一致，`description` 简要说明触发问题。正文包含适用与不适用边界、最少信息、判断顺序、不同条件的分支、完成条件、正反例及能力承接依据。工具参数不复制成整篇教程，变化的费率和政策由当次查询提供证据。

在场景 `skills` 中声明 `id`、`ref`、`version`、`description` 和 `required_tools`。加载器校验重复 ID 及工具依赖；这只保证声明一致，仍需验证知识正文和业务分支。`LocalSkillLoader` 先展示名称与描述，Agent 自主判断是否加载正文。系统提示词保持简洁，不重复工具参数或规定 Skill 的调用顺序。

当前运行时使用自包含正文，没有额外 `references` 文件读取链路。不要把完成 SOP 所需步骤只放在附属文件或假定模型可使用文件、Shell、其他场域工具。场景快照保存引用时正文，运行时原生加载器读取标准目录；修改资源后重新加载场景并创建新会话，历史快照保留。

```sh
uv run growthpilot validate
uv run pytest -q tests/agent_harness/test_native_skills.py tests/agent_harness/test_scenario_v2.py tests/agent_harness/test_budget_tools.py
# 预览该场景统一的动态客户案例
uv run growthpilot evaluate --suite dialogue --scenario meituan_rider --limit 3 --run my-dialogue-preview
# 显式执行会调用真实业务模型、普通客户模型与独立评审
uv run growthpilot evaluate --suite dialogue --scenario meituan_rider --limit 3 --run my-dialogue-run --execute
```

新增或修改 Skill 后，补充其代表性客户问题到所属场景的统一动态案例集，用目标完成度、问题解决度、事实可靠性、沟通体验四项 1～5 分评分，以及 token、首 token 时延（TTFT）观察完整 Agent。TTFT 从客户文本提交到最终回复首个非空文本块到达，包含此前的 Skill 加载与工具调用等待；不要把工具调用前的中间说明当作最终回复，也不要用完整回复耗时代替。不要添加独立 Skill suite、强制加载指标或 Memory/Skill 消融实验。工程测试仍验证原生加载、工具依赖和权限，加载成功本身不表示问题已经解决。

如果新增业务依赖政策资料，应同时维护该场景的 RAG 相关证据标注，并冻结完整语料版本；不能通过缩窄题意或删除失败案例来提高分数。操作步骤见[评测说明](../evaluation/README.md)。

## 扩展客户画像

在场景 `customer_fields` 添加字段、类型、说明、枚举或范围及有效期；字段含义改变时增加 `semantic_version`。无需添加读取或写入工具。后台提取器会自动接收字段定义，并由代码严格校验候选结果。补充原话、否定、更正、未提及和跨客户隔离用例，参见[画像实现](../backend/agent_harness/memory/README.md)。
