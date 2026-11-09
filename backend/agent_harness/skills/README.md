# 按业务问题加载 Skill

当前包含 **17 个场域业务 Skill**。场域决定身份、目标、工具和知识范围，Skill 负责解决当前问题的判断顺序。每次只加载需要的正文，不要求一通电话走完全部技能。

目录按四个场景归档，分组名称与 `backend/scenarios/` 一致：

```text
skills/
├── meituan_rider/             # 骑手：5 个业务 Skill
├── meituan_merchant/          # 商家：4 个业务 Skill
├── didi_driver/               # 司机：4 个业务 Skill
└── financial_consultation/    # 金融：4 个业务 Skill
```

每个分组下保留 `<skill_id>/SKILL.md`。分组仅整理文件位置，技能 ID 与按需加载机制保持一致。

## 已实现的能力

| 场域 | Skill | 主要产物 | 承接依据 |
| --- | --- | --- | --- |
| 骑手 | [work-fit](meituan_rider/meituan-rider-work-fit/SKILL.md) | 时间安排与配送模式的条件比较 | 招募 FAQ、客户时间偏好 |
| 骑手 | [cost-planning](meituan_rider/meituan-rider-cost-planning/SKILL.md) | 已知条件下的成本门槛或月度预算余额 | 计酬/租赁查询、出勤预算工具 |
| 骑手 | [rental-choice](meituan_rider/meituan-rider-rental-choice/SKILL.md) | 租期、费用与试跑计划是否匹配 | 租赁目录、客户预算及使用期限 |
| 骑手 | [protection](meituan_rider/meituan-rider-protection/SKILL.md) | 保障类型及原文支持的适用边界 | 职伤、家庭关怀等公开资料 |
| 骑手 | [onboarding](meituan_rider/meituan-rider-onboarding/SKILL.md) | 报名条件与材料准备缺项 | 招募 FAQ、服务规范、客户自述 |
| 商家 | [delivery-choice](meituan_merchant/meituan-merchant-delivery-choice/SKILL.md) | 配送方式的适用条件与费用取舍 | 配送和费率查询、客户承接条件 |
| 商家 | [order-economics](meituan_merchant/meituan-merchant-order-economics/SKILL.md) | 条件明确的单笔贡献 | 费率依据、客户成本、订单计算工具 |
| 商家 | [offer-fit](meituan_merchant/meituan-merchant-offer-fit/SKILL.md) | 可用权益、限制与待确认条件 | 本地权益查询、客户新老商家自述 |
| 商家 | [admission](meituan_merchant/meituan-merchant-admission/SKILL.md) | 类目对应的材料与缺项清单 | 招商规则、审查登记规范 |
| 司机 | [eligibility](didi_driver/didi-driver-eligibility/SKILL.md) | 城市、人、车分别核对的条件清单 | 全国及已覆盖城市准入原文 |
| 司机 | [work-budget](didi_driver/didi-driver-work-budget/SKILL.md) | 兼职固定投入与收入口径分析 | 计费/租车查询、出勤预算工具 |
| 司机 | [rental-choice](didi_driver/didi-driver-rental-choice/SKILL.md) | 租期、现金占用与使用计划的比较 | 租车目录、客户条件及实际合同待核项 |
| 司机 | [onboarding](didi_driver/didi-driver-onboarding/SKILL.md) | 当前阶段与有依据的准备事项 | 准入、资质及已收录办理说明 |
| 金融 | [trust-check](financial_consultation/financial-trust-check/SKILL.md) | 收费/身份主张的核验路径 | 官方 FAQ、反欺诈资料 |
| 金融 | [authorization](financial_consultation/financial-authorization/SKILL.md) | 注册、登录、信贷授权与代扣的区别 | 隐私/授权原文、App 信息查询 |
| 金融 | [cost-explanation](financial_consultation/financial-cost-explanation/SKILL.md) | 费用口径及现有计算支持的条件试算 | 费用/合同原文、产品和费用计算 |
| 金融 | [offer-fit](financial_consultation/financial-offer-fit/SKILL.md) | 信息服务权益的范围与资格条件 | 本地权益和产品查询 |

每份正文包括适用边界、最少信息、业务分支、完成条件、例子和具体能力依据。来源 ID 只登记材料来源，不表示已采集、已检索或当前适用；例子不作为客户事实或报价。

## 准入规则

新增 Skill 前逐项说明：客户问题、所需事实、具体工具/知识来源、Agent 判断、完成结果、代表性回放。只有现有能力可完成主要路径，或明确的小幅补充后可完成，才进入技能清单。

- Agent 可承担澄清、条件比较、费用归类和基于证据的推理。不得替代缺失的价格、政策、个人状态或实时经营数据。
- 可扩充已验证资料、目录中有依据的字段及确定性计算。不能仅通过编写 SOP 声称已获得内部接口、真实审核、订单诊断或实时库存。
- 未知项允许有针对性地补问或收窄结论；若典型问题主要只能转客服，这个 Skill 应推迟或缩小范围。
- 业务目录的城市覆盖不等于地方政策覆盖。外部指南无法收录时，不能仅凭 sources.yaml 中存在条目就引用。
- 目前不提供真实收入预测、店铺经营诊断、个人资格/理赔裁定、完整退租赔偿计算及任意 App 页面故障修复。
- 金融保持自愿了解、下载注册与功能理解的目标，不以授信、借款、放款为转化目标。

## 运行机制

目录采用 `<scenario_id>/<skill_id>/SKILL.md`。frontmatter 的 `name` 与场景声明的 Skill ID 一致，`description` 说明何时需要。场景 `skills[].ref` 指向标准文件，`required_tools` 声明必要工具。场景加载时拒绝缺少依赖或重复 ID 的配置；这项检查不等于知识检索验收。

`skill_loaders(scenario)` 为各项建立 AgentScope `LocalSkillLoader`，由 `Toolkit` 按需加载正文，加载不会增加工具权限。单个事实问题可直接查询；比较、预算、材料分诊和授权辨析应选择相应 Skill。客户更正条件后重新判断，拒绝和暂缓始终受系统规则约束。

当前 Skill 查看器只需读取自包含正文，不依赖文件工具、Shell 或额外 references 加载。场景快照保存引用时正文，运行时原生加载器读标准目录；修改后重新加载场景并创建新会话，已有历史快照保持不变。

原四份 `*_process` 已由问题型技能替代。多重顾虑排序、尊重拒绝、证据和权限要求在系统提示中持续生效，不单独加载通用沟通 Skill。

## 验证

```sh
uv run growthpilot validate
uv run pytest -q tests/agent_harness/test_native_skills.py tests/agent_harness/test_scenario_v2.py tests/agent_harness/test_budget_tools.py
uv run growthpilot evaluate --suite dialogue --scenario meituan_rider --limit 3 --run dialogue-preview
# 显式调用完整 Agent、动态客户模型与独立 judge
uv run growthpilot evaluate --suite dialogue --scenario meituan_rider --limit 3 --run dialogue-run --execute
```

Skill 作为完整 Agent 的能力参与统一动态客户评测，不设独立 Skill suite、强制加载评分或消融实验。工程测试继续保护声明、原生加载与工具权限；对话 trace 保留实际加载事件，供失败排查使用。成功加载不等于解决客户问题。

政策知识统一进入 RAG 题集，端到端只使用目标达成率、事实有据率和平均 Agent token。数据、公式、运行与报告见[评测说明](../../../evaluation/README.md)。
