import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {RagCompleteness,RagOutcome} from '../src/rag'

const coverage={answered:1,total:2,complete:false,requirements:[
 {id:'R1',text:'提交材料',answer_status:'answered',source_status:'supported'},
 {id:'R2',text:'赔付例外',answer_status:'unanswered',source_status:'supported',reason:'答案遗漏该分项'}]}

test('question coverage stays separate from fully cited output',()=>{
 const html=renderToStaticMarkup(<RagOutcome result={{claims:[{text:'提交发票'}],answer_status:'partial',coverage:{percent:100},rag:{completeness:coverage}}}/>)
 for(const text of ['逐项回答：1 / 2','已有证据，尚未答出','提交材料','赔付例外','不代表事实准确率'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('100%'))
 assert.ok(html.includes('<details open="">'))
})

test('unresolved sources and conflicts are distinguished',()=>{
 const rows=[{...coverage.requirements[1],source_status:'missing'},
 {id:'R3',text:'生效版本',answer_status:'conflicted',source_status:'conflicted'}]
 const html=renderToStaticMarkup(<RagCompleteness result={{rag:{completeness:{...coverage,requirements:rows}}}}/>)
 assert.ok(html.includes('仍缺证据'));assert.ok(html.includes('证据冲突'))
})

test('completed ledger is collapsed and labels the actual repair route',()=>{
 const html=renderToStaticMarkup(<RagCompleteness result={{rag:{completeness:{...coverage,answered:2,complete:true},completion_repair:{action:'retrieve'}}}}/>)
 assert.ok(html.includes('逐项回答：2 / 2'));assert.ok(html.includes('定向补查并补写'))
 assert.ok(!html.includes('<details open='))
})

test('rewrite status does not pretend another retrieval happened',()=>{
 const html=renderToStaticMarkup(<RagCompleteness result={{rag:{completeness:coverage,completion_repair:{action:'rewrite'}}}}/>)
 assert.ok(html.includes('依据已有证据补写'));assert.ok(html.includes('仍有未解决分项'))
 assert.ok(!html.includes('定向补查'))
})

test('legacy or unplanned results do not fabricate a completion percentage',()=>{
 for(const result of [{},{rag:{}},{rag:{completeness:{total:0,requirements:[]}}}])assert.equal(renderToStaticMarkup(<RagCompleteness result={result}/>),'')
})

test('question labels and evidence reasons are escaped',()=>{
 const html=renderToStaticMarkup(<RagCompleteness result={{rag:{completeness:{...coverage,requirements:[{id:'x',text:'<script>bad()</script>',reason:'<img src=x>',answer_status:'unanswered'}]}}}}/>)
 assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'))
 assert.ok(!html.includes('<img src=x>'))
})
