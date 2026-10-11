import {originalPdfPage} from '../src/evidence'
import {sourcePosition} from '../src/result-presentation'
import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {QueryContextBar,TaskProgress,taskFeedback,projectQueryProfile,queryContextPolicy} from '../src/agent'
import {initialProjectId,taskScopeFields,taskRequestContext,ThreadHistory} from '../src/conversations'
import {AnalysisPage} from '../src/workspace'
import {HomePage} from '../src/home'
import {RagOutcome} from '../src/rag'

const noop=()=>{}
test('a real empty query displays its bounded missing-data explanation without claiming a technical failure',()=>{
 const result={answer_status:'insufficient_evidence',answer:'该SQL返回0行，不表示所问数值为0，也不证明全库或外部资料不存在。',claims:[{kind:'database_empty',status:'supported'}],missing_requirements:['当前筛选范围无匹配记录。'],evidence:[{id:'sql-1',type:'sql_result',source:'只读数据源'}]}
 const html=renderToStaticMarkup(<RagOutcome result={result}/>)
 for(const text of ['现有资料不足以给出确定值','不表示所问数值为0','不证明全库或外部资料不存在','当前筛选范围无匹配记录'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('本轮回答状态待确认'));assert.ok(html.includes('可查看的相关依据'));assert.ok(!html.includes('可查看的相关原文'))
 assert.equal(taskFeedback(result).kind,'insufficient')
})
test('DOCX citation labels keep logical sections distinct from physical PDF pages',()=>{
 assert.equal(sourcePosition({page:2,extraction_method:'docx_native'}),'逻辑节 2')
 assert.equal(sourcePosition({page:2,source_refs:[{source_method:'docx_native'}]}),'逻辑节 2')
 assert.equal(sourcePosition({page:2,extraction_method:'pdf_native'}),'第 2 页')
 assert.equal(sourcePosition({}),'')
})
const config={mode:'model' as const,connection_id:'model-1',max_steps:8,available:true,query_scope:'sales' as const,connections:[{id:'model-1',name:'已配置模型',model:'text-model',enabled:true,available:true}],databases:[],project_id:'public-statistics',project_name:'公开统计分析',source_label:'公开统计公报',document_count:3,profile:{default_scope:'auto',managed_public:true,starter_questions:['2024 年全国居民人均可支配收入是多少？','2024 年城镇化率有什么变化？','这些指标的统计口径是什么？']}}
function renderAnalysis(props:Record<string,any>){
 return renderToStaticMarkup(<AnalysisPage question="" setQuestion={noop} run={noop} busy={false} recompute={noop} onSave={noop} onCopy={noop} onEvidence={noop} navigate={noop} choose={noop} onRetry={noop} session={{id:'ui-test',messages:[],tasks:[]}} tools={[]} agentConfig={config} {...props}/>)
}

test('first visit selects public statistics while an existing remembered project remains selected',()=>{
 const projects=[{id:'default'},{id:'public-statistics'},{id:'private-project'}]
 assert.equal(initialProjectId(projects),'public-statistics')
 assert.equal(initialProjectId(projects,'default'),'default')
 assert.equal(initialProjectId(projects,'private-project'),'private-project')
 assert.equal(initialProjectId(projects,'deleted-project'),'public-statistics')
 assert.equal(initialProjectId([{id:'default'}]),'default')
})

test('manual scope is submitted only for a new unrestricted query',()=>{
 assert.deepEqual(taskScopeFields('knowledge'),{scope_override:'knowledge'})
 for(const invalid of ['',undefined,'auto','document','unknown'])assert.deepEqual(taskScopeFields(invalid),{})
 assert.deepEqual(taskScopeFields('database',true),{})
 assert.deepEqual(taskScopeFields('hybrid',false,'recompute'),{})
 assert.deepEqual(taskScopeFields('hybrid',false,'query',{document_id:'frozen',page:2}),{})
 assert.deepEqual(taskScopeFields('hybrid',false,'query',null,{scope_type:'document',document_ids:['frozen']}),{})
})

test('project metadata and local recommendation show actual source without replacing the saved global model',()=>{
 assert.equal(projectQueryProfile(config).document_count,3)
 const html=renderToStaticMarkup(<QueryContextBar config={config} routePreview={{query_scope:'knowledge',source_label:'国家统计局年度公报',document_count:2,reason:'问题涉及已关联公报中的收入指标。',scope_options:[{id:'knowledge',label:'知识库问答'}]}} onSettings={noop} onScopeChange={noop}/>)
 for(const text of ['推荐范围','知识库问答','公开统计分析','国家统计局年度公报','2 份文档','text-model','收入指标','自动匹配项目资料'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('本机销售示例'))
 assert.ok(!html.includes('value="hybrid"'))
 const initial=renderToStaticMarkup(<QueryContextBar config={config} onSettings={noop} onScopeChange={noop}/>)
 assert.ok(initial.includes('按问题自动匹配'));assert.ok(!initial.includes('value="sales"'))
})

test('historical followups freeze sources while using the current model connection',()=>{
 const result={query_spec:{metric:'document',query_scope:'knowledge'},rag:{scope:{documents:[{name:'原项目旧公报'}]}},agent:{mode:'model',model:'recorded-model'}}
 const html=renderToStaticMarkup(<QueryContextBar config={config} result={result} routePreview={{query_scope:'hybrid',source_label:'新项目资料'}} scopeOverride="hybrid" onSettings={noop} onScopeChange={noop}/>)
 for(const text of ['追问沿用','知识库问答','原项目旧公报','text-model','来源与版本沿用'])assert.ok(html.includes(text),text)
 for(const text of ['新项目资料','本次问答范围','recorded-model','当前文档'])assert.ok(!html.includes(text),text)
 const page=renderToStaticMarkup(<QueryContextBar config={config} result={{query_spec:{metric:'document',document_context:{page:2,parse_id:'old'}},agent:{mode:'local'}}} onSettings={noop} onScopeChange={noop}/>)
 assert.ok(page.includes('当前文档'));assert.ok(page.includes('固定文档版本'));assert.ok(!page.includes('本次问答范围'))
})

test('an inherited answer cannot mask a disabled current model or pretend its old model will run',()=>{
 const result={query_spec:{query_scope:'knowledge'},rag:{scope:{documents:[{name:'冻结公报'}]}},agent:{mode:'model',model:'old-model'}}
 const current={...config,connections:[{...config.connections[0],enabled:false}]}
 const unavailable=renderToStaticMarkup(<QueryContextBar config={current} result={result} onSettings={noop}/>)
 for(const text of ['冻结公报','问答模型不可用','请先选择可用模型和数据源','needs-setup'])assert.ok(unavailable.includes(text),text)
 assert.ok(!unavailable.includes('old-model'))
 const loading=renderToStaticMarkup(<QueryContextBar config={null} result={result} onSettings={noop}/>)
 assert.ok(loading.includes('问答模型设置尚未读取'));assert.ok(!loading.includes('old-model'))
})

test('failures distinguish timeout, connectivity, missing evidence, clarification, and cancellation',()=>{
 assert.equal(taskFeedback({status:'failed',error:'连接上游时请求超时'}).kind,'timeout')
 assert.equal(taskFeedback({status:'failed',error:'Network connection failed'}).kind,'connection')
 assert.equal(taskFeedback({answer_status:'refused',answer:''}).kind,'insufficient')
 assert.equal(taskFeedback({status:'needs_clarification',clarification:{title:'请指定年份'}}).detail,'请指定年份')
 assert.equal(taskFeedback({status:'cancelled'}).kind,'cancelled')
 const html=renderAnalysis({result:null,session:{id:'failed',messages:[{role:'user',text:'公报收入是多少？'}],tasks:[{id:'timeout-task',status:'failed',error:'上游请求超时'}]}})
 for(const text of ['本轮请求超时','上游请求超时','重试这一轮','检查模型连接'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('analysis-starter'))
})

test('partial claims remain readable and an empty refusal explains its absence of an answer',()=>{
 const partial={run_id:'partial',question:'收入与某未知条件',status:'completed',answer_status:'partial',query_spec:{query_scope:'knowledge'},claims:[{id:'c',text:'公报记载全国居民人均可支配收入为 41314 元。',status:'supported',evidence_ids:['e']}],evidence:[{id:'e',source:'年度公报',locator:{page:1}}],missing_requirements:['未知条件未在公报中说明'],rows:[],columns:[],actions:[],events:[]}
 const html=renderAnalysis({result:partial})
 for(const text of ['仅回答有依据的部分','41314','未知条件未在公报中说明','来源 1'])assert.ok(html.includes(text),text)
 const empty=renderAnalysis({result:{...partial,answer_status:'refused',answer:'',claims:[],missing_requirements:[]}})
 assert.ok(empty.includes('未取得支持这个问题的充分资料。'))
 assert.ok(!empty.includes('分析已完成'))
})

test('real task stage and elapsed time never fabricate a completion percentage',()=>{
 const unknown=renderToStaticMarkup(<TaskProgress task={{status:'queued',stage:'等待执行资源'}}/>)
 assert.ok(unknown.includes('正在排队'));assert.ok(!unknown.includes('已等待'))
 const running=renderToStaticMarkup(<TaskProgress task={{id:'real-task',status:'running',created_at:new Date(Date.now()-8000).toISOString(),stage:'正在检索固定文档'}}/>)
 for(const text of ['正在执行','正在检索固定文档','提交后已过'])assert.ok(running.includes(text),text)
 assert.ok(!running.includes('%'))
})

test('empty terminal history retains the recorded failure and an explicit retry',()=>{
 const html=renderToStaticMarkup(<ThreadHistory session={{messages:[{role:'user',text:'原问题',created_at:'2026-10-10T00:00:00Z'},{id:'answer',role:'assistant',status:'failed',text:'',task_id:'failed-task',created_at:'2026-10-10T00:00:00Z'}],tasks:[{id:'failed-task',status:'failed',error:'模型连接暂时断开'}]}} onSelect={noop} onRetry={noop}/>)
 assert.ok(html.includes('模型连接暂时断开'));assert.ok(html.includes('重试这轮'))
})

test('public project starter questions and document count replace the unrelated sales landing content',()=>{
 const html=renderToStaticMarkup(<HomePage tables={[]} docs={[]} dataReady dashboard={{sales:[{year:2024,amount:1}],trend:[]}} question="" setQuestion={noop} run={noop} project={{name:'公开统计分析'}} sessions={[]} tools={[]} onImport={noop} onCatalog={noop} agentConfig={config}/>)
 for(const text of ['全国居民人均可支配收入','城镇化率','统计口径','公开统计公报','项目关联文档','当前项目的提问趋势'])assert.ok(html.includes(text),text)
 for(const text of ['销售冠军','净销售额','共享示例数据'])assert.ok(!html.includes(text),text)
})


test('first failed result explains failure without claiming an earlier result exists',()=>{
 const failed={run_id:'failed-run',question:'公报比例',status:'failed',answer_status:'failed',answer:'本轮未完成：单位绑定不一致',query_spec:{query_scope:'hybrid'},claims:[],evidence:[],rows:[],columns:[],actions:[],events:[]}
 const html=renderAnalysis({result:failed,session:{id:'first-failure',messages:[],tasks:[{id:'failed-task',status:'failed',error:'单位绑定不一致',result:failed}]}})
 assert.ok(html.includes('本轮查询未完成'));assert.ok(html.includes('单位绑定不一致'))
 assert.ok(!html.includes('此前查询的结果'));assert.ok(!html.includes('本轮回答状态待确认'))
 const oldResult={...failed,run_id:'old-run',status:'completed',answer_status:'supported',claims:[{id:'c',text:'已查得的数据',status:'supported',evidence_ids:[]}]}
 const preserved=renderAnalysis({result:oldResult,session:{id:'has-older-result',messages:[],tasks:[{id:'failed-task',status:'failed',error:'新查询失败',result:failed}]}})
 assert.ok(preserved.includes('此前查询的结果'))
})


test('turning off context changes both the visible source and submitted parent fields',()=>{
 const prior={run_id:'old-result',query_spec:{query_scope:'knowledge'},rag:{scope:{documents:[{name:'旧冻结原文'}]}},agent:{model:'old-model'}}
 for(const retain of [true,false]){
  const policy=queryContextPolicy(true,retain)
  const body=taskRequestContext('old-message','database',policy.inheritContext,'query',null,null)
  assert.equal(body.inherit_context,retain);assert.equal(body.parent_message_id,retain?'old-message':null)
  assert.equal(body.scope_override,retain?undefined:'database')
  const html=renderToStaticMarkup(<QueryContextBar config={config} result={prior} inheritContext={policy.inheritContext} routePreview={{query_scope:'database',source_label:'本轮新资料'}} onSettings={noop} onScopeChange={noop}/>)
  if(retain){assert.ok(html.includes('追问沿用'));assert.ok(html.includes('旧冻结原文'));assert.ok(!html.includes('本次问答范围'))}
  else{assert.ok(html.includes('独立新问题'));assert.ok(html.includes('本轮新资料'));assert.ok(html.includes('本次问答范围'));assert.ok(!html.includes('来源与版本沿用'))}
 }
 assert.equal(taskFeedback({status:'failed',error:'数据库只读操作失败，请检查权限、字段、方言和服务超时；未执行写入',error_detail:{category:'database'}}).kind,'database')
})


test('source page mapping distinguishes original PDFs, scan excerpts, and Word sections',()=>{
 const scan={page_count:4,document:{format:'pdf',provenance:{origin:'original_scanned_pdf_page_excerpt',original_pdf_pages:[1,10,11,12]}}}
 assert.equal(originalPdfPage(scan,3),11);assert.equal(originalPdfPage(scan,5),null)
 assert.equal(originalPdfPage({page_count:8,document:{format:'pdf',provenance:{origin:'original_pdf_from_user_bundle'}}},7),7)
 assert.equal(originalPdfPage({page_count:4,document:{format:'docx',provenance:{origin:'faithful_native_text_excerpt_docx',original_pdf_pages:[1,7,8]}}},2),null)
})
