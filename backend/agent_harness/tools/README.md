# 模型可调用工具

业务函数共 16 个：2 个共同工具（政策检索与下载短信）、12 个目录查询与金融费用试算，以及 2 个确定性预算工具。骑手、司机、商家各开放其中 6 个，金融开放 5 个；宿主另提供 `end_call` 通话工具，AgentScope 提供原生 `Skill` 工具。业务工具提供实际证据、计算结果和发送状态，供问题型 SOP 判断。

| 场景 | 工具 | 用途 |
| --- | --- | --- |
| 共同 | `search_policy(query)` | 检索当前场景的官方条款、操作说明和出处 |
| 共同 | `send_download_sms()` | 向当前会话客户发送对应场景的 App 下载短信 |
| 通话 | `end_call(message)` | 记录完整告别语，播放完成后结束通话 |
| 美团骑手 | `search_nearby_stations(location, delivery_type?)` | 查询区域站点和招募信息 |
| 美团骑手 | `query_delivery_pay(area, delivery_type, station_id?)` | 查询配送类型、站点对应的计酬规则 |
| 美团骑手 | `search_vehicle_rentals(location, battery_included?)` | 查询电动车租金、押金与服务范围 |
| 骑手与司机 | `estimate_work_budget(work_days, monthly_fixed_cost, daily_variable_cost, refundable_deposit, monthly_income?)` | 根据确认的出勤与成本计算月度预算、收入门槛和押金现金占用 |
| 滴滴司机 | `search_driver_service_points(location)` | 查询当地服务点及咨询事项 |
| 滴滴司机 | `query_driver_earnings(area, service_type)` | 查询计费与平台服务费规则 |
| 滴滴司机 | `search_car_rentals(location, vehicle_type?)` | 查询租车方案和费用范围 |
| 美团商家 | `query_delivery_options(area, store_category)` | 查询配送方式与职责差异 |
| 美团商家 | `query_merchant_fee_plans(area, store_category, delivery_mode)` | 查询费率、计费条件和未包含费用 |
| 美团商家 | `query_merchant_offers(area, store_category, is_new_merchant)` | 按客户自述条件筛选活动 |
| 美团商家 | `estimate_order_contribution(received_amount, goods_cost, packaging_cost, other_variable_cost, amount_basis?, fee_base?, service_fee_percent?, delivery_cost?)` | 区分扣费前实收与扣费后到账，计算单笔贡献 |
| 度小满 | `query_product_options(purpose)` | 查询 App 功能和产品说明 |
| 度小满 | `query_financial_offers(product_id, is_new_user)` | 查询产品相关活动及条件 |
| 度小满 | `estimate_financing_cost(product_id, amount, term_months)` | 根据条款试算费用，返回公式、假设与到期金额 |

`search_policy` 使用 [rag/](../rag/README.md) 构建的索引，在通话中不抓取网页。结论需要正文支持，无相关证据或索引不可用时返回 `no_data`。业务查询使用 [BusinessCatalog](../business_data/README.md) 的精细记录、城市配置和通用方案，内部来源为 `local_catalog`，不是品牌实时接口。

[common.py](common.py) 统一定义跨场景工具：政策检索、下载短信、通话结束，以及骑手和司机共用的条件预算；场景专用工具放在对应场景文件中，商家单笔贡献计算位于 [meituan_merchant.py](meituan_merchant.py)。预算计算内核保留在 [business_data/budget.py](../business_data/budget.py)。调用路径为 AgentScope → 工具函数 → `ToolContext` 绑定的政策索引、业务目录、独立预算计算或短信发送服务。[build_tools](registry.py) 按场景声明选择工具；[boundary.py](boundary.py) 处理参数、权限、超时、审计与结果投影。场景中每份 Skill 的 `required_tools` 必须属于已开放工具，加载技能本身不授予额外权限。

## 下载短信

客户同意接收下载链接后调用 `send_download_sms()`，无需传参。工具从当前会话取得客户和场景，由发送服务选择对应的 App 下载内容并记录发送结果。同一会话重复调用复用原发送回执，不重复发送；Agent 仅在工具返回成功后告知客户已发送，失败时依据实际结果回应。

教学项目使用本地模拟发送，不连接真实短信网关。模拟标记与内部模板不写入模型工具说明、参数或返回值；模型只需理解业务状态。接入真实短信服务时替换发送适配器，并保留会话绑定与重复调用保护。

## 通话结束

[common.py](common.py) 中的 `end_call` 定义结束时机、告别语参数和执行逻辑。`build_tools` 在 `ToolContext` 提供 `request_end_call` 回调时注册该工具；它不属于场景业务工具清单，但每次调用仍校验会话是否可用。

工具通过回调把告别语交给 [Conversation](../conversation.py)。`Conversation` 保存结束意图，并通过 AgentScope 的 `on_model_call` 中间件方法直接复用告别语，避免再请求一次模型。前端播放完成后才结束通话；客户插话或新输入会撤销待挂断状态。系统提示保留“收尾必须调用 `end_call`”这一条约定，具体调用方式由工具描述说明；组装 Agent 时不再追加重复提示。

## 条件预算的口径

`estimate_work_budget` 供骑手和司机使用。出勤天数、月固定成本、每出勤日变动成本与可退押金必须明确。`monthly_income` 是已扣平台费用、尚未扣经营成本的月收入假设；不填时只计算成本和覆盖门槛，不猜单量或收入。押金计入现金占用，不扣减经营余额。首月现金预算是整月成本加押金，不表示签约当天必须支付的金额。

`estimate_order_contribution` 的 `received_amount` 已减去商家承担的优惠，其他输入均为尚未从该金额扣除的成本。两种口径分别处理：

- `before_fees`：尚未扣技术服务费和配送费，必须独立确认 `fee_base`、`service_fee_percent` 和 `delivery_cost`；仅支持已明确的简单比例计费，不套用最低收费、阶梯或封顶规则。
- `after_fees`：已扣完平台与配送费用，只扣货品、包装及其他变动成本；上述三个费用参数必须省略。无需反推扣费前订单金额，也不能重复扣已结算的费用。

两工具都使用 Decimal 计算，金额输入最多两位小数。明确为零可以传入，未知成本必须澄清；结果在 `data.calculation` 中保留公式、假设和未计入项。单笔贡献未扣门店租金、固定人工等成本，不能称为净利润；预算余额也不是个人收入保证。计算证据来自确认的输入和确定性公式，不因经过工具就成为官方政策、真实合同或后台经营记录。

## 查询和结果边界

只读查询按地区和业务条件筛选，不修改 Memory。城市级地点即可查询，未收录县镇或合法条件可使用通用方案；无位置、多城市冲突、无效参数和过期数据需分别处理。站点 ID 与后续计酬查询绑定相同地区及配送类型。

结果保留原始位置与条件、实际采用范围、有效期、方案、联系人和费用。目录中的地址与电话是教学占位（`phone` 形如 `000-XXXX-XXXX`、`phone_dialable=false`），不可拨打或到访；真实业务接入时应替换为只读适配器。模型结果移除内部来源诊断，但不能丢失业务字段。未知金融用途默认 App 信息介绍；明确非借款意愿优先信息说明。未知产品试算保留原请求与采用产品的区别，信息类产品不计算借款费用。

客户画像由宿主自动注入，并在通话结束后异步整理，不向对话 Agent 提供读写工具。17 个业务 Skill 按需提供具体问题的判断顺序、分支和完成条件；参数用法仍以工具定义为准。客户反馈推进对话，通话生命周期由宿主管理。
