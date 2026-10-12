import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {AnalysisPage} from '../src/workspace'

const noop=()=>{}
const base={run_id:'normalize-1',question:'帝都的GDP是多少',status:'completed',query_spec:{query_scope:'database',metric:'database'},claims:[],rows:[],columns:[],evidence:[],events:[],actions:[]}
const boundary='只做词典与兼容形式的确定性归一；词表之外的写法原样返回，未命中不等于问题有误；不调用模型或外部服务。'

function render(result:Record<string,any>){
  return renderToStaticMarkup(<AnalysisPage result={result} question="" setQuestion={noop} run={noop} busy={false} recompute={noop} onSave={noop} onCopy={noop} onEvidence={noop} navigate={noop} choose={noop} onRetry={noop} session={{id:'ui-normalize',messages:[],tasks:[]}} tools={[]}/>)
}

test('a recorded rewrite is shown with its source while the original question stays visible',()=>{
  const html=render({...base,question_normalization:{applied:true,normalized:'北京的GDP是多少',boundary,
    changes:[{kind:'alias',from:'帝都',to:'北京',source:'用户或项目别名'}]}})
  for(const text of ['question-normalization','已按项目词表规范化','帝都','北京','用户或项目别名','原问题保持不变','帝都的GDP是多少'])assert.ok(html.includes(text),text)
  assert.ok(html.includes(boundary))
})

test('every recorded change is listed, including a traditional-form step',()=>{
  const html=render({...base,question_normalization:{applied:true,normalized:'统计总额',boundary,
    changes:[{kind:'traditional',from:'統計總額',to:'统计总额',source:'繁简对照表（有界，非完整转换器）'}]}})
  for(const text of ['統計總額','统计总额','繁简对照表'])assert.ok(html.includes(text),text)
})

test('no note is rendered without a recorded change',()=>{
  for(const value of [undefined,{applied:false,normalized:'x',changes:[],boundary},{applied:true,normalized:'x',changes:[],boundary}]){
    const html=render({...base,question_normalization:value})
    assert.ok(!html.includes('已按项目词表规范化'))
    assert.ok(!html.includes('question-normalization'))
  }
})
