/** 展示业务解答；内部查询诊断信息保留在服务端事件日志中。 */
export const escapeHTML = (value: unknown) => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]!));

interface ToolCall {
  id: string;
  name: string;
  arguments?: unknown;
  result?: unknown;
  complete: boolean;
  state?: string;
}

const labels: Record<string, string> = {
  search_policy: '查询政策知识', read_memory: '读取客户记忆', write_memory: '保存客户记忆', Skill: '读取沟通技能',
  search_nearby_stations: '查询附近站点', query_delivery_pay: '查询配送计酬', search_vehicle_rentals: '查询车辆租赁',
  search_driver_service_points: '查询司机服务点', query_driver_earnings: '查询司机计酬', search_car_rentals: '查询司机租车方案',
  query_delivery_options: '查询门店配送方式', query_merchant_fee_plans: '查询门店费用方案', query_merchant_offers: '查询新商活动',
  query_product_options: '查询产品说明', query_financial_offers: '查询产品活动', estimate_financing_cost: '测算费用',
  query: '问题', city: '城市', area: '区域', location: '位置', delivery_type: '配送类型',
  status: '结果', data: '查询内容', message: '说明', reason: '原因', source: '来源',
  coverage: '覆盖范围', updated_at: '更新时间', as_of: '数据日期',
  name: '名称', title: '标题', description: '说明', address: '地址', phone: '电话', phone_number: '电话', distance_km: '距离（公里）', distance_meters: '距离（米）',
  monthly_rent: '月租', deposit: '押金', requirements: '要求', conditions: '适用条件',
  currency: '币种', amount: '金额', unit: '单位', notes: '补充说明', limitations: '使用范围',
  results: '查询结果', items: '条目', references: '依据', content: '内容', text: '内容',
  payload: '业务内容', filters: '筛选条件', matched_location: '匹配区域', requested_location: '查询区域',
  coverage_level: '适用范围', requested_product_id: '请求产品',
  product_id: '产品', effective_product_id: '本次参考方案',
  requested_filters: '客户查询条件', assumed_filters: '方案参考条件',
  evidence: '数据依据', question: '需要进一步了解', valid_from: '有效期开始', valid_until: '有效期结束',
  recruitment_status: '招募情况', distance_note: '距离说明', address_note: '地址说明', availability: '可用情况',
  monthly_rent_yuan: '月租（元）', deposit_yuan: '押金（元）', minimum_term_months: '最短租期（月）',
  battery_included: '是否含电池', battery_service: '电池服务', maintenance: '维修服务', other_fees_note: '其他费用',
  included: '已包含', excluded: '未包含', services: '服务内容', responsibility: '服务责任',
  pay_basis: '计酬方式', per_order_yuan: '每单金额（元）', settlement_note: '结算说明',
  monthly_income: '月收入', monthly_income_note: '月收入说明', income_note: '收入说明',
  vehicle_type: '车辆类型', service_type: '业务类型', store_category: '门店类目', delivery_mode: '配送方式',
  is_new_merchant: '是否新商家', is_new_user: '是否新用户', benefit: '活动内容', eligibility_note: '适用资格说明',
  fare_components: '费用构成', base_distance_km: '基础里程（公里）', base_fare_yuan: '基础费用（元）', additional_km_yuan: '每增加一公里（元）',
  platform_service_fee_percent: '平台服务费（%）', technical_service_fee_percent: '技术服务费（%）', delivery_fee_yuan_per_order: '每单配送费（元）',
  amount_range_yuan: '金额范围（元）', annual_rate_percent: '年利率（%）', service_fee_yuan: '服务费（元）', term_months: '期限（月）',
  interest_method: '计息方式', repayment_method: '还款方式', purpose_note: '用途说明', purposes: '适用用途',
  assumptions: '测算假设', quote_note: '报价说明', coverage_note: '覆盖说明', min: '最低', max: '最高', label: '说明',
  calculation: '费用测算', principal_yuan: '本金（元）', interest_yuan: '利息（元）', total_cost_yuan: '总费用（元）', total_repayment_yuan: '应还总额（元）',
  formula: '计算公式', rounding: '金额精度', personal_quote: '是否个人报价', eligibility_filters: '适用条件',
  product_reference_note: '方案说明',
};

const values: Record<string, string> = {
  success: '成功', denied: '无法执行', needs_clarification: '需要补充信息', no_data: '暂无数据', expired: '数据已过期',
  knowledge_not_indexed: '知识库尚未初始化，请先完成首次启动。',
  scenario_not_indexed: '当前场景尚未建立知识索引。',
  index_rebuild_required: '知识索引需要重新构建。',
  simple_interest_bullet: '按单利计算，到期偿还',
  locality: '区域范围', generic: '通用参考条件', not_applicable: '不涉及地区限制', city: '城市范围',
  __generic__: '通用条件',
};

const businessQueries = new Set([
  'search_nearby_stations', 'query_delivery_pay', 'search_vehicle_rentals',
  'search_driver_service_points', 'query_driver_earnings', 'search_car_rentals',
  'query_delivery_options', 'query_merchant_fee_plans', 'query_merchant_offers',
  'query_product_options', 'query_financial_offers', 'estimate_financing_cost',
]);
const diagnosticFields = new Set([
  'source_type', 'source', 'evidence', 'disclosure', 'mode',
  'match_type', 'is_fallback', 'fallback_reason', 'generated', 'location_basis',
  'template_provenance', 'template_source', 'template_id', 'variant_id', 'profile_id', 'profile_source',
  'pricing_basis', 'pricing_parameters', 'pricing_profile', 'pricing_template',
  'id', 'kind', 'location_id', 'station_id', 'scenario_id', 'retryable',
]);
const productFields = new Set(['product_id', 'requested_product_id', 'effective_product_id']);

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function businessView(value: unknown, productNames: Map<string, string>, field = ''): unknown {
  if (Array.isArray(value)) return value.map(item => businessView(item, productNames));
  const entries = object(value);
  if (entries) return Object.fromEntries(Object.entries(entries)
    .filter(([key]) => !diagnosticFields.has(key))
    .map(([key, item]) => [key, businessView(item, productNames, key)]));
  if (productFields.has(field) && typeof value === 'string') {
    return productNames.get(value) ?? (value.startsWith('catalog_') ? '未提供名称' : value);
  }
  return value;
}

function resultProductNames(result: unknown): Map<string, string> {
  const data = object(object(result)?.data);
  const productNames = new Map<string, string>();
  if (Array.isArray(data?.items)) for (const item of data.items) {
    const record = object(item), title = record?.name ?? record?.title;
    if (typeof record?.id === 'string' && typeof title === 'string') productNames.set(record.id, title);
  }
  return productNames;
}

function resultForDisplay(name: string, result: unknown): unknown {
  const data = object(object(result)?.data);
  if (!businessQueries.has(name) && (name === 'search_policy' || data?.source_type !== 'local_catalog')) return result;
  const productNames = resultProductNames(result);
  const display = businessView(result, productNames);
  const displayData = object(object(display)?.data);
  if (displayData && data?.requested_product_id && data.effective_product_id && data.requested_product_id !== data.effective_product_id) {
    displayData.product_reference_note = '请求产品与本次参考方案不同，费用请按本次方案的适用条件理解。';
  }
  return display;
}

export function collectToolCalls(events: unknown[]): ToolCall[] {
  const calls = new Map<string, ToolCall>();
  events.forEach((value, index) => {
    const event = object(value), data = object(event?.data);
    if (!data || !['tool_start', 'tool_result', 'tool_end'].includes(String(event?.type))) return;
    const name = typeof data.name === 'string' ? data.name : '工具查询';
    // 历史事件可能缺少调用 ID；仅匹配最近一次尚未完成的同名调用。
    const pending = [...calls.values()].reverse().find(call => call.name === name && !call.complete);
    const id = typeof data.tool_call_id === 'string' ? data.tool_call_id
      : event?.type !== 'tool_start' && pending ? pending.id : `legacy-${index}`;
    const call = calls.get(id) ?? {id, name, complete: false};
    if (event?.type === 'tool_start') call.arguments = data.arguments;
    if (event?.type === 'tool_result') { call.result = data.result; call.complete = true; }
    if (typeof data.state === 'string') call.state = data.state;
    // 旧日志中无法匹配的 tool_end 事件不会补充可读证据。
    if (event?.type !== 'tool_end' || calls.has(id)) calls.set(id, call);
  });
  return [...calls.values()];
}

function renderValue(value: unknown, depth = 0): string {
  if (value === null || value === undefined) return '<span class="muted">未提供</span>';
  if (Array.isArray(value)) return value.length ? `<ul class="query-list">${value.map(item => `<li>${renderValue(item, depth + 1)}</li>`).join('')}</ul>` : '<span class="muted">暂无匹配项</span>';
  const entries = object(value);
  if (entries && depth < 10) return `<dl class="query-fields">${Object.entries(entries).map(([key, item]) => `<div><dt>${escapeHTML(labels[key] ?? key)}</dt><dd>${renderValue(item, depth + 1)}</dd></div>`).join('')}</dl>`;
  return `<span>${escapeHTML(entries ? JSON.stringify(entries) : typeof value === 'boolean' ? (value ? '是' : '否') : typeof value === 'string' ? values[value] ?? value : value)}</span>`;
}

function queryContext(result: unknown): string {
  const data = object(object(result)?.data);
  if (!data) return '';
  const context = Object.fromEntries([
    'requested_location', 'matched_location', 'requested_product_id', 'effective_product_id',
    'coverage_level', 'requested_filters', 'assumed_filters', 'product_reference_note',
  ].filter(key => data[key] !== undefined).map(key => [key, data[key]]));
  return Object.keys(context).length ? `<div class="query-section"><h3>适用信息</h3>${renderValue(context)}</div>` : '';
}

export function publicTraceEvents(events: unknown[]): unknown[] {
  const productNames = new Map<string, string>();
  for (const call of collectToolCalls(events)) {
    if (businessQueries.has(call.name)) for (const [id, name] of resultProductNames(call.result)) productNames.set(id, name);
  }
  return events.filter(event => object(event)?.type !== 'business_query_audit').map(value => {
    const event = object(value), data = object(event?.data);
    if (!data) return value;
    const name = String(data.name ?? '');
    if (event?.type === 'tool_result') return {...event, data: {...data, result: resultForDisplay(name, data.result)}};
    if (event?.type === 'tool_start' && businessQueries.has(name)) return {...event, data: {...data, arguments: businessView(data.arguments, productNames)}};
    return value;
  });
}

export function renderToolCalls(events: unknown[]): string {
  const calls = collectToolCalls(events).filter(call => call.name !== 'end_call');
  if (!calls.length) return '<div class="muted">Agent 会按问题查询政策或业务资料，查询内容会显示在这里。</div>';
  return calls.map((call, index) => {
    const result = resultForDisplay(call.name, call.result);
    const args = businessQueries.has(call.name) ? businessView(call.arguments, resultProductNames(call.result)) : call.arguments;
    const items = object(object(result)?.data)?.items;
    const count = Array.isArray(items) ? items.length : 0;
    const meta = call.complete ? `已返回${count ? ' · ' + count + ' 项' : ''}` : call.state === 'error' ? '查询失败' : '查询中';
    // 默认只展开第一张卡；其余收起，避免长结果把页面撑开。
    const open = call.complete && index === 0 ? 'open' : '';
    return `<details class="query-result" ${open}>
    <summary>${escapeHTML(labels[call.name] ?? call.name)} <span class="mini">${meta}</span></summary>
    ${queryContext(result)}
    ${args !== undefined ? `<div class="query-section"><h3>查询条件</h3>${renderValue(args)}</div>` : ''}
    ${result !== undefined ? `<div class="query-section"><h3>返回内容</h3>${renderValue(result)}</div>` : ''}
  </details>`;
  }).join('');
}
