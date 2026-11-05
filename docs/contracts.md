# 场景配置与工具

场景入口为 `backend/scenarios/<id>/scenario.yaml`，使用 `schema_version: '2.0'`。`growth validate` 加载四场景、资源和业务数据，不调用模型。

| 配置段 | 含义 |
| --- | --- |
| `scenario`、`identity` | 场景身份、语言和沟通背景 |
| `objective` | 对话目标；`stages` 描述目标范围，不控制固定顺序；`completion_basis: customer_statement` 使用客户反馈口径 |
| `customer_fields` | 可记忆的字段、类型、语义版本和有效期 |
| `business_data` | 本地业务目录的 `ref` 与 `source_type: local_catalog` |
| `tools` | 当前场景向对话 Agent 开放的业务函数 |
| `skills` | 标准 `SKILL.md` 路径、版本、描述和依赖工具 |
| `policies` | 禁止承诺、工具和会话超时 |

[scenario.py](../backend/agent_harness/scenario.py) 直接读取自维护 YAML 和引用资源；配置约定由测试覆盖。字段与资源正文参与快照哈希，会话绑定创建时的版本。

工具从函数签名生成参数 Schema，返回 `status/data/evidence/retryable`。业务查询可能返回 `success`、`needs_clarification`、`no_data`、`expired` 或 `denied`；参数不合法返回 `denied`，工具超时返回 `unknown`。本次调用包含完整结果和原因。`retryable` 目前为保留字段，所有工具路径均为 false。

`send_download_sms()` 不接收模型参数，客户与场景取自当前会话。结果提供下载短信的业务状态，同一会话重复调用复用发送回执。Agent 在客户同意后调用，并以工具结果判断是否已发送。

原始业务查询的来源、命中层级与模板信息保存到 `business_query_audit`。[model_projection](../backend/agent_harness/tools/boundary.py) 为模型筛选业务结果：保留方案、地址电话、金额、有效期、原始条件与适用条件，同时移除 `source`、`generated`、`disclosure`、`template_*` 等内部标记，模型与客户页面都看不到这些诊断字段。目录中的联系方式是教学占位（形如 `000-XXXX-XXXX`，并带 `phone_dialable: false`），不可拨打；政策工具的正文与出处不受该投影影响。

`requested_location`、`requested_filters` 保存原始输入，`matched_location`、`assumed_filters` 说明采用范围。未知金融产品的费用试算区分请求产品与计算产品；技术 ID 只用于跨工具引用。无位置或多城市冲突需澄清，非法参数和过期数据不能自动转为成功。

政策结论以 `clauses[].text` 的原文为依据；标题、适用提醒和搜索分数不替代正文。通话后画像提取输出字段变化及客户消息 ID、逐字引用；程序校验类型和证据作用域，在事务中比较来源先后、追加版本并保存审计。画像字段只由 `customer_fields` 定义。客户反馈表示对话进展，不能升级为平台事实。
