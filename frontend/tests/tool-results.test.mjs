import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../src/tool-results.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {collectToolCalls, publicTraceEvents, renderToolCalls} = await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));

const resultEvent = (name, data) => ({type:'tool_result', data:{name, result:{status:'success', data}}});

test('ordinary business results show useful fields and escape HTML without internal catalog metadata', () => {
  const events = [
    {type:'tool_start', data:{tool_call_id:'station-1', name:'search_nearby_stations', arguments:{location:'北京望京'}}},
    {type:'tool_result', data:{tool_call_id:'station-1', name:'search_nearby_stations', state:'success', result:{
      status:'success', data:{source_type:'local_catalog', source:'internal-station-source', coverage:['北京'], items:[{name:'望京站', address:'<img src=x onerror=alert(1)>', distance_km:null}]},
    }}},
  ];
  const html = renderToolCalls(events);
  for (const value of ['查询附近站点','查询条件','北京望京','覆盖范围','望京站','距离（公里）','未提供']) assert.ok(html.includes(value), value);
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  for (const value of ['<img','internal-only-marker','internal-station-source','local_catalog']) assert.ok(!html.includes(value), value);
  assert.equal(collectToolCalls(events).length, 1);
});

test('incomplete tool events render without requiring unavailable fields', () => {
  const calls = collectToolCalls([
    null, {}, {type:'framework'},
    {type:'tool_start', data:{name:'search_policy', arguments:{query:'报名条件'}}},
    {type:'tool_result', data:{name:'search_policy', result:{text:'需要符合当地报名条件'}}},
    {type:'tool_end', data:{name:'search_policy', state:'success'}},
    {type:'tool_result', data:{result:{text:'旧版返回'}}},
  ]);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].complete, true);
  assert.deepEqual(calls[0].arguments, {query:'报名条件'});
  assert.match(renderToolCalls([]), /按问题查询/);
  assert.match(renderToolCalls([{type:'tool_start',data:{name:'search_policy'}}]), /查询中/);
});

test('separate concurrent calls remain separate and a failed tool end is visible', () => {
  const events = [
    {type:'tool_start', data:{name:'search_policy', tool_call_id:'a', arguments:{query:'条件'}}},
    {type:'tool_start', data:{name:'search_policy', tool_call_id:'b', arguments:{query:'费用'}}},
    {type:'tool_end', data:{name:'search_policy', tool_call_id:'a', state:'error'}},
    {type:'tool_result', data:{name:'search_policy', tool_call_id:'b', result:{text:'费用说明'}}},
  ];
  assert.equal(collectToolCalls(events).length, 2);
  assert.match(renderToolCalls(events), /查询失败/);
  assert.match(renderToolCalls(events), /费用说明/);
});

test('raw and projected results display the same areas, reference terms and assumptions', () => {
  const projected = {
    requested_location:'北京望京', matched_location:'北京市', coverage_level:'city',
    requested_filters:{delivery_type:'专送'}, assumed_filters:[{delivery_type:'专送'}],
    items:[{name:'配送费用参考', per_order_yuan:{min:4.5,max:6.5}, conditions:['按完成订单结算'], excluded:['车辆租赁成本']}],
  };
  const raw = {...projected, source_type:'local_catalog', source:{label:'internal-catalog-source'},
    match_type:'city_profile', is_fallback:true, fallback_reason:'locality_not_curated', generated:true,
    template_provenance:{source:{label:'内部模板'}}, pricing_basis:{factor:1.3},
    items:projected.items.map(item=>({...item, id:'catalog_delivery_pay', source_type:'local_catalog', source:{label:'内部来源'}, pricing_parameters:{factor:2}})),
  };
  const rawHtml = renderToolCalls([resultEvent('query_delivery_pay', raw)]);
  const projectedHtml = renderToolCalls([resultEvent('query_delivery_pay', projected)]);
  assert.equal(rawHtml, projectedHtml);
  for (const value of ['查询区域','北京望京','匹配区域','北京市','方案参考条件','按完成订单结算','车辆租赁成本','4.5','6.5']) assert.ok(rawHtml.includes(value), value);
});

test('known business tools suppress nested diagnostics even after source_type is removed', () => {
  const data = {requested_location:'<svg onload=alert(1)>', matched_location:'北京市', items:[{
    name:'附近服务点', address:null, phone:null, conditions:['具体服务以站点答复为准'],
    template_id:'internal-template', variant_id:'internal-variant', profile_id:'internal-profile',
    template_provenance:{source:{label:'internal-only-marker'}, profile_source:{label:'内部配置'}},
    pricing_basis:{factor:5}, source:{label:'catalog-private'},
  }]};
  const original = structuredClone(data);
  const html = renderToolCalls([resultEvent('search_driver_service_points', data)]);
  for (const value of ['internal-only-marker','internal-','catalog-private','pricing_basis','template_provenance']) assert.ok(!html.includes(value), value);
  assert.ok(html.includes('&lt;svg onload=alert(1)&gt;'));
  assert.ok(!html.includes('<svg'));
  assert.ok(html.includes('具体服务以站点答复为准'));
  assert.deepEqual(data, original, 'display projection must not rewrite raw evidence');
});

test('missing business contact fields remain unavailable', () => {
  const html = renderToolCalls([resultEvent('search_nearby_stations', {items:[
    {name:'望京服务点', address:null, phone:null, distance_meters:null},
  ]})]);
  assert.equal((html.match(/未提供/g) ?? []).length, 3);
});

test('business addresses and phone numbers are displayed as returned without contact actions', () => {
  const html = renderToolCalls([resultEvent('search_vehicle_rentals', {items:[
    {name:'望京车辆服务点',address:'北京市朝阳区望京北路 28 号',phone:'13812345678',distance_meters:null},
  ]})]);
  for (const value of ['地址','北京市朝阳区望京北路 28 号','电话','13812345678','距离（米）','未提供']) assert.ok(html.includes(value), value);
  assert.ok(!html.includes('href="tel:'));
});

test('financing references use returned product names and retain conditions without opaque catalog IDs', () => {
  const data = {
    requested_product_id:'客户咨询产品', effective_product_id:'catalog_generic_financing',
    items:[{id:'catalog_generic_financing', name:'通用费用参考方案', annual_rate_percent:8,
      assumptions:['按单利计算；实际费用以适用合同条款为准']}],
    calculation:{requested_product_id:'客户咨询产品', product_id:'catalog_generic_financing', principal_yuan:1000, interest_yuan:80, personal_quote:false},
  };
  const html = renderToolCalls([
    {type:'tool_start',data:{name:'estimate_financing_cost',arguments:{product_id:'catalog_generic_financing',amount:1000,term_months:12}}},
    resultEvent('estimate_financing_cost',data),
  ]);
  for (const value of ['请求产品','客户咨询产品','本次参考方案','通用费用参考方案','实际费用以适用合同条款为准','本金（元）','利息（元）','请求产品与本次参考方案不同']) assert.ok(html.includes(value), value);
  assert.ok(!html.includes('catalog_generic_financing'));
});

test('RAG actual citations and source evidence remain visible and escaped', () => {
  const event = resultEvent('search_policy', {source:{title:'官方报名规则', url:'https://example.org/policy'}, evidence:[{text:'政策原文 <script>bad()</script>', source:'官方来源'}]});
  const html = renderToolCalls([event]);
  for (const value of ['官方报名规则','https://example.org/policy','官方来源','政策原文 &lt;script&gt;bad()&lt;/script&gt;']) assert.ok(html.includes(value), value);
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('internal-only-marker'));
});

test('public traces omit raw business audits and project catalog results without mutating events', () => {
  const events = [
    {type:'business_query_audit',data:{name:'search_nearby_stations',result:{source_type:'local_catalog', source:'private-audit'}}},
    {type:'tool_start',data:{name:'estimate_financing_cost',arguments:{product_id:'catalog_generic_financing'}}},
    resultEvent('search_nearby_stations',{source_type:'local_catalog',items:[{name:'附近服务点',source:{label:'private-source'}}]}),
    resultEvent('search_policy',{source:{title:'官方来源'}}),
  ];
  const original = structuredClone(events);
  const visible = publicTraceEvents(events);
  assert.equal(visible.length, 3);
  assert.ok(!JSON.stringify(visible).includes('private-'));
  assert.ok(!JSON.stringify(visible).includes('catalog_generic_financing'));
  assert.ok(JSON.stringify(visible).includes('官方来源'));
  assert.deepEqual(events,original);
  assert.ok(!renderToolCalls(events).includes('private-audit'));
});
