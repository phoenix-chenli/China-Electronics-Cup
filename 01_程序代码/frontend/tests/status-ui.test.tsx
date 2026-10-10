import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {Claims} from '../src/workspace'
import {EventSummary,ModelPackageStatus,QueryContextBar} from '../src/agent'

const noop=()=>{}
const config={mode:'model' as const,connection_id:'m-new',max_steps:8,available:true,query_scope:'database' as const,database_connection_id:'db-new',connections:[{id:'m-new',name:'新模型',model:'model-new',enabled:true,available:true}],databases:[{id:'db-new',name:'新数据库',kind:'sqlite',available:true}]}

test('missing detail is unverified, not an arithmetic contradiction',()=>{
 const html=renderToStaticMarkup(<Claims result={{claims:[{id:'a',text:'示例结论',counter_check:{status:'unverified',detail:'缺少明细'}}]}} onEvidence={noop}/>)
 assert.ok(html.includes('资料不足，未核验'))
 assert.ok(!html.includes('复算不一致'))
 const failed=renderToStaticMarkup(<Claims result={{claims:[{id:'a',text:'示例结论',counter_check:{status:'failed'}}]}} onEvidence={noop}/>)
 assert.ok(failed.includes('复算不一致'))
 const unknown=renderToStaticMarkup(<Claims result={{claims:[{id:'a',text:'示例结论',counter_check:{status:'future_state'}}]}} onEvidence={noop}/>)
 assert.ok(unknown.includes('复算状态待确认'))
})

test('source event summary preserves failure, cancellation and unknown duration',()=>{
 for(const [status,label] of [['failed','失败'],['cancelled','已取消'],['running','进行中']]){
  const html=renderToStaticMarkup(<EventSummary event={{label:'解析原文',status}}/>)
  assert.ok(html.includes(label));assert.ok(html.includes('status-'+status))
  assert.ok(!html.includes('circle-check'));assert.ok(!html.includes('undefined'));assert.ok(!html.includes(' ms'))
 }
 assert.ok(renderToStaticMarkup(<EventSummary event={{label:'读取资料',status:'completed',duration_ms:0}}/>).includes('0 ms'))
})

test('followup shows its frozen source and model after global config changes',()=>{
 const result={query_spec:{query_scope:'database',database_connection_id:'db-old'},database_query:{source:{connection_id:'db-old',name:'旧库存库'}},agent:{mode:'model',model:'model-old'}}
 const html=renderToStaticMarkup(<QueryContextBar config={config} result={result} onSettings={noop} onNew={noop}/>)
 for(const text of ['追问沿用','旧库存库','model-old','默认设置已改变','新建问答'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('新数据库'));assert.ok(!html.includes('model-new'))
})

test('new query explains unavailable model while local sales stays usable',()=>{
 const missing=renderToStaticMarkup(<QueryContextBar config={{...config,connections:[],connection_id:null,available:false}} onSettings={noop}/>)
 assert.ok(missing.includes('问答模型不可用'));assert.ok(missing.includes('请先选择可用模型'))
 const local=renderToStaticMarkup(<QueryContextBar config={{...config,mode:'local',query_scope:'sales',connections:[],connection_id:null}} onSettings={noop}/>)
 assert.ok(local.includes('本地确定性查询'));assert.ok(!local.includes('needs-setup'))
})

test('model package distinguishes installed, bundled, absent and failed states',()=>{
 for(const [model,label] of [[{installed:true,verified:true},'文件校验通过'],[{bundled:true},'随包可安装'],[{bundled:false},'需下载模型'],[{bundled:true,status:'failed'},'安装失败']]){
  assert.ok(renderToStaticMarkup(<ModelPackageStatus model={model}/>).includes(String(label)))
 }
})
