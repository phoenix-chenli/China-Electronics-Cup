import test from 'node:test';import assert from 'node:assert/strict';
import {buildResultChart} from '../src/resultChart.ts';
const answer=(rows=[['甲',10],['乙',20]],extra={})=>({status:'completed',result:{verification:{status:'passed'},columns:['对象','收入（万元）'],rows,...extra}});
const options={kind:'bar',xColumn:'对象',yColumns:['收入（万元）']};
test('only genuine published rows generate values with zero baseline',()=>{const a=answer(),old=structuredClone(a);const r=buildResultChart(a,options);assert.equal(r.status,'ready');assert.deepEqual(r.points.map(p=>p.values[0]),[10,20]);assert.deepEqual(r.yDomain,[0,20]);assert.equal(r.series[0].unit,'万元');assert.deepEqual(a,old);});
for(const status of ['running','failed','cancelled','needs_clarification','insufficient_evidence'])test(`status ${status} not chartable`,()=>assert.equal(buildResultChart({...answer(),status},options).status,'unavailable'));
test('stale data rejected',()=>assert.equal(buildResultChart({...answer(),scope_stale:true},options).status,'unavailable'));
test('limited verification rejected',()=>assert.equal(buildResultChart(answer(undefined,{verification:{status:'limited'}}),options).status,'unavailable'));
test('never fall back to query or display or candidate rows',()=>{const a=answer(undefined,{rows:undefined,query:{rows:[['甲',999]]},display_rows:[['甲',888]],evidence:[{rows:[['甲',777]]}]});assert.equal(buildResultChart(a,options).status,'unavailable');});
test('negative and zero values preserve signs and zero baseline',()=>{const r=buildResultChart(answer([['甲',-7.2],['乙',0]]),options);assert.deepEqual(r.yDomain,[-7.2,0]);assert.deepEqual(r.points.map(p=>p.values[0]),[-7.2,0]);});
test('missing stays null; zero stays zero; no gap interpolation',()=>{const r=buildResultChart(answer([['甲',null],['乙','——'],['丙',0]]),options);assert.deepEqual(r.points.map(p=>p.values[0]),[null,null,0]);assert.equal(r.connectGaps,false);});
for(const value of ['约3','3万元','NaN',Infinity,NaN,'1,00','9.8.2',true,{},'9007199254740992'])test(`invalid numeric ${String(value)} rejected`,()=>assert.equal(buildResultChart(answer([['甲',value]]),options).status,'unavailable'));
test('thousand separators and signed percentages remain actual scales',()=>{const a=answer([['甲','−7.2%'],['乙','0.6%']],{columns:['对象','增速（%）']});const r=buildResultChart(a,{...options,yColumns:['增速（%）']});assert.deepEqual(r.points.map(p=>p.values[0]),[-7.2,.6]);assert.equal(r.series[0].unit,'%');assert.equal(buildResultChart(answer([['甲','1,000.25']]),options).points[0].values[0],1000.25);});
test('mixed units cannot share misleading axis',()=>{const a=answer([['甲',3,7]],{columns:['对象','收入（万元）','人口（人）']});assert.equal(buildResultChart(a,{...options,yColumns:['收入（万元）','人口（人）']}).status,'unavailable');});
test('known same units may compare; unknown multi-series does not guess',()=>{const a=answer([['甲',3,7]],{columns:['对象','A','B'],column_units:{A:'万元',B:'万元'}});assert.equal(buildResultChart(a,{...options,yColumns:['A','B']}).status,'ready');delete a.result.column_units;assert.equal(buildResultChart(a,{...options,yColumns:['A','B']}).status,'unavailable');});
test('single unknown unit remains explicitly unknown',()=>{const r=buildResultChart(answer([['甲',3]],{columns:['对象','指标']}),{...options,yColumns:['指标']});assert.equal(r.status,'ready');assert.equal(r.series[0].unit,null);assert.ok(r.notes.some(n=>n.includes('未标注单位')));});
test('header unit and suffix conflicts rejected',()=>assert.equal(buildResultChart(answer([['甲','5%']]),options).status,'unavailable'));
test('duplicates not silently aggregated',()=>assert.equal(buildResultChart(answer([['甲',1],['甲',2]]),options).status,'unavailable'));
test('data cap rejects instead of silently sampling',()=>assert.equal(buildResultChart(answer(),{...options,maxPoints:1}).status,'unavailable'));
test('source truncation stays visible',()=>{const r=buildResultChart(answer(undefined,{query:{truncated:true}}),options);assert.equal(r.rowsTruncated,true);assert.ok(r.notes.some(n=>n.includes('已截断')));});
test('line uses real row order and records order policy',()=>{const r=buildResultChart(answer([['2024',10],['2022',5]]),{...options,kind:'line'});assert.deepEqual(r.points.map(p=>p.x),['2024','2022']);assert.ok(r.notes[0].includes('行顺序'));});
test('scatter takes paired real numeric cells and missing gaps',()=>{const a=answer([[1,3],[2,null],[3,7]],{columns:['投入（元）','收入（元）']});const r=buildResultChart(a,{kind:'scatter',xColumn:'投入（元）',yColumns:['收入（元）']});assert.equal(r.status,'ready');assert.deepEqual(r.xDomain,[0,3]);assert.deepEqual(r.points[1].values,[null]);});
test('scatter needs one Y series and real X',()=>{assert.equal(buildResultChart(answer(),{...options,kind:'scatter'}).status,'unavailable');});
test('all missing is not a zero chart',()=>assert.equal(buildResultChart(answer([['甲',null],['乙','']]),options).status,'unavailable'));
test('malformed rows or repeated field names rejected',()=>{assert.equal(buildResultChart(answer([['甲']]),options).status,'unavailable');assert.equal(buildResultChart(answer([['甲',1]],{columns:['对象','对象']}),options).status,'unavailable');});
test('undeclared bare values are not silently promoted to percentages',()=>{const a=answer([['甲',.05],['乙','5%']],{columns:['对象','增长']});assert.equal(buildResultChart(a,{...options,yColumns:['增长']}).status,'unavailable');});
test('metadata cannot override contradictory real header unit',()=>assert.equal(buildResultChart(answer(undefined,{column_units:{'收入（万元）':'亿元'}}),options).status,'unavailable'));
test('numeric underflow cannot become fictitious zero',()=>assert.equal(buildResultChart(answer([['甲','1e-999']]),options).status,'unavailable'));

// Reduced field-for-field shape of the actual readonly Chinook Top3 API result;
// source identity is synthetic. No model output/candidate row fallback is used.
const executedSQL=()=>({status:'completed',scope_stale:false,
  sources:[{id:'db',sha256:'fixture-hash',parser_version:'fixture-parser'}],
  result:{verification:{status:'limited',checks:['单条只读查询通过语法检查和SQLite权限检查']},
    columns:['账单国家','总额'],rows:[['USA','523.06'],['Canada','303.96'],['France','195.1']],
    query:{sql:'SELECT BillingCountry, DECIMAL_SUM(Total) FROM Invoice GROUP BY BillingCountry ORDER BY CAST(DECIMAL_SUM(Total) AS NUMERIC) DESC LIMIT 3',
      parameters:[],truncated:false,used_tables:['Invoice'],read_columns:{Invoice:['BillingCountry','Total']},elapsed_ms:14,returned_rows:3,total_rows:3},
    claims:[{id:'C1',text:'保存的三个国家及总额。',evidence_ids:['E1']}],
    evidence:[{id:'E1',kind:'table',source_id:'db',sha256:'fixture-hash',parser_version:'fixture-parser',table_name:'Invoice',lineage:'table',row_numbers:[],stale:false}]}});
const sqlOptions={kind:'bar',xColumn:'账单国家',yColumns:['总额']};
test('actual limited SQL result shape charts only saved rows and keeps limited scope',()=>{
  const a=executedSQL(),before=structuredClone(a);a.result.query.rows=[['fake',999]];a.result.display_rows=[['fake',888]];
  const result=buildResultChart(a,sqlOptions);
  assert.equal(result.status,'ready');assert.deepEqual(result.points.map(p=>p.values[0]),[523.06,303.96,195.1]);
  assert.equal(a.result.verification.status,'limited');assert.ok(result.notes.some(n=>n.includes('查询结果')&&n.includes('业务定义核对')));
  assert.deepEqual(a.result.rows,before.result.rows);assert.equal(result.series[0].unit,null);
});
for(const [name,mutate] of [
  ['failed',a=>a.status='failed'],['partial',a=>a.status='insufficient_evidence'],
  ['stale answer',a=>a.scope_stale=true],['freshness unknown',a=>delete a.scope_stale],
  ['no query',a=>delete a.result.query],['trace-only query',a=>a.result.query={trace:[]}],
  ['no SQL',a=>a.result.query.sql=''],['no elapsed execution',a=>delete a.result.query.elapsed_ms],
  ['nonfinite elapsed',a=>a.result.query.elapsed_ms=Infinity],['negative elapsed',a=>a.result.query.elapsed_ms=-1],
  ['row count mismatch',a=>a.result.query.returned_rows=2],['string row count',a=>a.result.query.returned_rows='3'],
  ['bad parameters',a=>a.result.query.parameters=null],['no table scope',a=>a.result.query.used_tables=[]],
  ['wrong table',a=>a.result.query.used_tables=['Other']],['missing claims',a=>a.result.claims=[]],
  ['claim cites unknown evidence',a=>a.result.claims[0].evidence_ids=['E9']],
  ['claim lacks evidence',a=>a.result.claims[0].evidence_ids=[]],['empty claim',a=>a.result.claims[0].text=''],
  ['stale evidence',a=>a.result.evidence[0].stale=true],['unknown evidence freshness',a=>delete a.result.evidence[0].stale],
  ['wrong evidence version',a=>a.result.evidence[0].sha256='old'],['wrong parser',a=>a.result.evidence[0].parser_version='old'],
  ['foreign source',a=>a.result.evidence[0].source_id='other'],['no source versions',a=>a.sources=[]],
  ['text evidence instead of executed table',a=>a.result.evidence[0].kind='text'],
  ['duplicate evidence id',a=>a.result.evidence.push({...a.result.evidence[0]})],
  ['candidate-only rows',a=>{a.result.query.rows=a.result.rows;delete a.result.rows;}],
])test(`limited SQL refuses ${name}`,()=>{const a=executedSQL();mutate(a);assert.equal(buildResultChart(a,sqlOptions).status,'unavailable');});
test('limited SQL named parameters and truncation remain transparent',()=>{
  const a=executedSQL();a.result.query.parameters={country:'USA'};a.result.query.truncated=true;
  const result=buildResultChart(a,sqlOptions);assert.equal(result.status,'ready');assert.equal(result.rowsTruncated,true);
  assert.ok(result.notes.some(n=>n.includes('已截断')));
});
test('uncited table metadata cannot complete query provenance',()=>{
  const a=executedSQL();a.result.query.used_tables.push('Other');a.result.evidence.push({...a.result.evidence[0],id:'E2',table_name:'Other'});
  assert.equal(buildResultChart(a,sqlOptions).status,'unavailable');
});

test('historical rendering is explicit and never mutates the saved answer',()=>{
  const a=answer();a.scope_stale=true;const before=structuredClone(a);
  const chart=buildResultChart(a,{...options,allowHistorical:true});
  assert.equal(chart.status,'ready');assert.equal(chart.historical,true);assert.ok(chart.notes.some(n=>n.includes('历史图表')));
  assert.deepEqual(a,before);assert.equal(buildResultChart(a,options).status,'unavailable');
});
test('historical limited SQL keeps original source hashes and execution proofs',()=>{
  const a=executedSQL();a.scope_stale=true;a.result.evidence[0].stale=true;
  const options={...sqlOptions,allowHistorical:true};
  assert.equal(buildResultChart(a,options).status,'ready');
  a.result.evidence[0].sha256='current-unrelated';assert.equal(buildResultChart(a,options).status,'unavailable');
});
for(const [name,mutate] of [
  ['failed',a=>a.status='failed'],['candidate-only',a=>{a.result.query.rows=a.result.rows;delete a.result.rows;}],
  ['missing execution proof',a=>delete a.result.query.elapsed_ms],['unknown evidence version',a=>delete a.result.evidence[0].parser_version],
  ['unknown freshness',a=>delete a.scope_stale],['unknown evidence freshness',a=>delete a.result.evidence[0].stale],
])test(`historical display still refuses ${name}`,()=>{const a=executedSQL();a.scope_stale=true;mutate(a);assert.equal(buildResultChart(a,{...sqlOptions,allowHistorical:true}).status,'unavailable');});
