import test,{after} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
// Keep test output in this checkout even when node_modules links to another workspace.
const scratch=await mkdtemp(path.resolve('.answer-render-'));
after(()=>rm(scratch,{recursive:true,force:true}));
const bundled=await build({stdin:{contents:"export {AnswerReading} from './AnswerReading'; export {ConversationFlow} from './ConversationFlow';",resolveDir:path.resolve('src'),loader:'tsx'},bundle:true,write:false,platform:'node',format:'esm',packages:'external',jsx:'automatic',plugins:[{name:'omit-styles-in-static-test',setup(b){b.onResolve({filter:/\.css$/},args=>({path:args.path,namespace:'empty-css'}));b.onLoad({filter:/.*/,namespace:'empty-css'},()=>({contents:'',loader:'js'}));}}]});
const target=path.join(scratch,'answer.mjs');await writeFile(target,bundled.outputFiles[0].text);
const {AnswerReading,ConversationFlow}=await import(pathToFileURL(target).href);
const noop=()=>{};
const props=answer=>({answer,active:true,favorite:false,onEvidence:noop,onDraft:noop,onBranch:noop,onFavorite:noop,onVersions:noop,onCancel:noop,onError:noop,onReleaseChart:noop,onScenario:noop,onContext:noop});
const base={id:'a1',project_id:'p1',question:'审阅数据',status:'completed',mode:'document',created_at:'2026-10-18T08:00:00Z',elapsed_ms:400,scope_stale:false,sources:[],result:{claims:[{id:'C1',text:'# 正文标题\n\n## 第一节\n\n**已知事实** [E1]。\n\n## 第二节\n\n|项目|值|\n|--|--|\n|收入|3.00|\n\n$x^2$\n\n```sql\nSELECT 1\n```\n\n<script>alert(1)</script>\n\n[恶意链接](javascript:alert%281%29)\n\n![外链图片](https://example.com/tracker.png)',evidence_ids:['E1']}],evidence:[{id:'E1',kind:'text',source_id:'s1',source_name:'说明.pdf',quote:'原文证据',page:1,stale:false}],notes:[],verification:{status:'passed',checks:['保存的核对项']}}};
test('actual answer renderer produces rich text, safe formula, headings, citation buttons and table',()=>{
 const html=renderToStaticMarkup(React.createElement(AnswerReading,props(base)));
 assert.match(html,/<strong>已知事实<\/strong>/);assert.match(html,/<table>/);assert.match(html,/language-sql/);assert.match(html,/class="katex"/);
 assert.match(html,/answer-a1-part-0-heading-1/);assert.match(html,/aria-label="本轮回答目录"/);assert.match(html,/查看来源 E1，说明.pdf/);
 assert.doesNotMatch(html,/<script/);assert.doesNotMatch(html,/href="javascript:/);assert.doesNotMatch(html,/<img/);assert.doesNotMatch(html,/src="https:\/\/example.com/);
});
test('failed answer renderer does not show candidate facts, tables or fake success',()=>{
 const answer={...base,status:'failed',error:'HTTP 402',result:{...base.result,columns:['候选'],rows:[[999]],text:'unpublished candidate'}};
 const html=renderToStaticMarkup(React.createElement(AnswerReading,props(answer)));
 assert.match(html,/HTTP 402/);assert.doesNotMatch(html,/已知事实/);assert.doesNotMatch(html,/unpublished candidate/);assert.doesNotMatch(html,/<table>/);assert.doesNotMatch(html,/结论已核对/);
});
test('clarification options begin unchecked and require explicit action',()=>{
 const answer={...base,status:'needs_clarification',result:{...base.result,clarification:{question:'选择哪种指标？',options:['发票数量','发票金额']}}};
 const html=renderToStaticMarkup(React.createElement(AnswerReading,props(answer)));
 assert.match(html,/type="radio"/);assert.doesNotMatch(html,/ checked=""/);assert.match(html,/disabled=""[^>]*>填入确认条件/);
});

const audited=()=>({...base,result:{...base.result,completeness:{status:'assessed',complete:true,items:[{id:'Q1',status:'answered',claim_ids:['C1']}]},calculation_completeness:{version:'test',status:'not_required',requirements:[],issues:[]}}});
test('actual rendered badge requires full audits and retains historical answer content when they are absent',()=>{
 const verified=renderToStaticMarkup(React.createElement(AnswerReading,props(audited())));
 assert.match(verified,/结论已核对/);assert.match(verified,/lucide-check/);
 const historic=renderToStaticMarkup(React.createElement(AnswerReading,props(base)));
 assert.match(historic,/完整性待核对/);assert.match(historic,/<strong>已知事实<\/strong>/);
 assert.doesNotMatch(historic,/结论已核对|lucide-check/);
});
test('actual renderer refuses a checked badge for a historic calculation with falsely complete coverage',()=>{
 const value=audited();value.question='1000分是多少钱';value.result.calculation_completeness={version:'test',status:'incomplete',requirements:[{id:'Q1'}],issues:[{code:'missing_calculation'}]};
 const html=renderToStaticMarkup(React.createElement(AnswerReading,props(value)));
 assert.match(html,/计算尚未完整核对/);assert.doesNotMatch(html,/结论已核对|lucide-check/);assert.match(html,/<strong>已知事实<\/strong>/);
});
test('collapsed historical turns use the same conservative publication status as the open answer',()=>{
 for(const [value,label] of [[base,'完整性待核对'],[audited(),'结论已核对']]){
  const html=renderToStaticMarkup(React.createElement(ConversationFlow,{chain:[value],activeId:'another',children:()=>null}));
  assert.match(html,/aria-expanded="false"/);assert.ok(html.includes(label));
  if(value===base)assert.doesNotMatch(html,/结论已核对|已完成/);
 }
});

const conversionRecord=()=>({id:'K1',expression:'a',parameters:[{name:'a',value:'1000',unit:'分',evidence_ids:[],provenance:'user_question',is_hypothetical:false}],value:'10',unit:'元',display_value:'10.00',checks:['单位按有限白名单校验并换算'],rule_evidence_ids:['E1'],unit_handling:{evaluation_basis:'parameters_normalized_before_expression_then_converted_to_output',parameter_base_values:[{parameter:'a',original_value:'1000',original_unit:'分',base_value:'10.00',unit_factor:'0.01'}],output_unit:'元',output_unit_factor:'1',pure_unit_conversion:true,semantic_status:'not_established_by_arithmetic'}});
test('a verified conversion opens its existing calculation details and shows saved user input, unit factors and exact rule quote',()=>{
 const value=audited(),calculation=conversionRecord();
 value.result.calculations=[calculation];value.result.display_blocks=[{kind:'calculation',calculation}];
 value.result.evidence=[{...base.result.evidence[0],quote:'100分=1元。这是人民币的分，不是积分。'}];
 value.result.calculation_completeness={version:'test',status:'passed',requirements:[{id:'Q1'}],issues:[]};
 const html=renderToStaticMarkup(React.createElement(AnswerReading,props(value)));
 assert.match(html,/<details class="method-detail" open=""><summary>计算过程与参数（1）/);
 assert.match(html,/保存的换算结果：1000 分 = 10\.00 元/);assert.match(html,/来自本次问题/);assert.doesNotMatch(html,/假设输入/);
 assert.match(html,/1000 分 × 0\.01 → 基准值 10\.00/);assert.match(html,/基准结果 ÷ 1 → 10\.00 元/);
 assert.match(html,/<blockquote>100分=1元。这是人民币的分，不是积分。<\/blockquote>/);assert.match(html,/查看来源 E1，说明.pdf/);
 assert.equal((html.match(/保存的换算结果：/g)??[]).length,1,'one saved calculation produces one visible calculation block');
});
test('pending calculation records remain accessible and show their saved warning without a success badge',()=>{
 const value=audited(),calculation=conversionRecord();calculation.unit_handling.review_warning='请核对是否重复换算。';
 value.result.calculations=[calculation];value.result.calculation_completeness={version:'test',status:'incomplete',requirements:[{id:'Q1'}],issues:[{code:'unverified'}]};
 const html=renderToStaticMarkup(React.createElement(AnswerReading,props(value)));
 assert.match(html,/计算过程与参数（1）/);assert.match(html,/本轮回答尚未完整核对/);assert.match(html,/请核对是否重复换算/);
 assert.match(html,/保存的换算结果：1000 分 = 10\.00 元/);assert.doesNotMatch(html,/结论已核对|lucide-check/);
});
test('repeated StrictMode server rendering cannot open a pending calculation or create a checked badge',()=>{
 const value=audited();value.result.calculations=[conversionRecord()];value.result.completeness.items[0].claim_ids=[];
 const before=structuredClone(value);
 for(let pass=0;pass<2;pass++){
  const html=renderToStaticMarkup(React.createElement(React.StrictMode,null,React.createElement(AnswerReading,props(value))));
  assert.match(html,/完整性待核对/);assert.doesNotMatch(html,/结论已核对|lucide-check/);
  assert.match(html,/<details class="method-detail"><summary>计算过程与参数（1）/);
 }
 assert.deepEqual(value,before);
});
