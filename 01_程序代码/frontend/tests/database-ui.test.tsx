import {DatabaseAnswerCheck} from '../src/database-query'
import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {DataTable} from '../src/ui'
import {ResultData,SourcePanel} from '../src/analysis-tools'
import {AnalysisPage} from '../src/workspace'
import {isDatabaseResult,savedAnalysisName} from '../src/result-presentation'

const result={
 run_id:'database-ui-fixture',question:'按产品编号列出库存成本',status:'completed',
 query_spec:{metric:'database',query_scope:'database',year:2024,region:'华东'},
 rows:[{product_id:'sku-1',product_name:'item',net_sales:1.23456789,amount:'123456789012345678.901234',note:null}],
 columns:['product_id','product_name','net_sales','amount','note'],sql:'SELECT product_id, net_sales, amount FROM imported_inventory',
 metrics:[{label:'不应显示的销售指标',value:12,unit:'CNY'}],formulas:[{value:10}],
 conditions:[{key:'year',label:'年份',display:'2024 年',value:2024,editable:true}],
 claims:[{id:'claim-1',text:'本轮结果仅来自执行的数据库查询。',evidence_ids:['sql-1'],status:'supported'}],coverage:{supported:1,total:1},
 evidence:[{id:'sql-1',type:'sql_result',source:'inventory',locator:{query_scope:'database',sql:'SELECT product_id, net_sales, amount FROM imported_inventory',params:[],rows:[{product_id:'sku-1',net_sales:1.23456789}],columns:['product_id','net_sales'],row_count:1}}],
 events:[],checks:[],actions:[],database_query:{source:{name:'库存工作库'},sql:'SELECT product_id, net_sales, amount FROM imported_inventory',params:[],joins:[],validation:{read_only:true,semantic_verified:false},attempts:1,row_limit:100,truncated:true,rows:[],columns:[]}
}
const noop=()=>{}

test('database table keeps colliding field names, exact numeric and decimal text, and NULL',()=>{
 const html=renderToStaticMarkup(<DataTable rows={result.rows} columns={result.columns} raw/>)
 for(const value of ['product_id','net_sales','1.23456789','123456789012345678.901234','NULL'])assert.ok(html.includes(value),value)
 assert.ok(!html.includes('净销售额（元）'))
 assert.ok(!html.includes('产品编号'))
 const sales=renderToStaticMarkup(<DataTable rows={result.rows} columns={['net_sales']}/>)
 assert.ok(sales.includes('净销售额（元）'))
})

test('database result retains every column and SQL evidence without guessed currency charts',()=>{
 const html=renderToStaticMarkup(<ResultData result={result} onSource={noop}/>)
 for(const value of ['product_id','1.23456789','库存工作库','查看实际 SQL','imported_inventory','查看关联规划','semantic_verified','结果已按返回上限截取'])assert.ok(html.includes(value),value)
 assert.ok(!html.includes('result-chart'))
 assert.ok(!html.includes('净销售额（元）'))
})

test('SQL precision view shows trailing zeros while original result and source remain unchanged',()=>{
 const original={...result,rows:[{amount:19.5,product_id:'0007',note:null}],columns:['amount','product_id','note'],
  database_query:{...result.database_query,rows:[{amount:19.5,product_id:'0007',note:null}],
   display_rows:[{amount:'19.50',product_id:'0007',note:null}],display_decimals:{amount:2}}}
 const before=JSON.stringify(original)
 const html=renderToStaticMarkup(<ResultData result={original} onSource={noop}/>)
 for(const value of ['19.50','0007','NULL','展开原始表格','原始返回值'])assert.ok(html.includes(value),value)
 assert.equal(JSON.stringify(original),before)
 const raw=renderToStaticMarkup(<DataTable rows={original.rows} columns={original.columns} raw/>)
 assert.ok(raw.includes('19.5'))
 assert.ok(!raw.includes('19.50'))
})

test('database analysis suppresses sales conditions, bonus shortcuts, metrics and regional followups',()=>{
 const storage=globalThis.localStorage
 Object.defineProperty(globalThis,'localStorage',{value:{getItem:()=>JSON.stringify(['scenario'])},configurable:true})
 try{
  const html=renderToStaticMarkup(<AnalysisPage result={result} question="" setQuestion={noop} run={noop} busy={false} recompute={noop} onSave={noop} onCopy={noop} onEvidence={noop} navigate={noop} choose={noop} session={{id:'test',messages:[],tasks:[]}} tools={[{id:'scenario',label:'情景推演',onSelect:noop}]} undo={result}/>)
  for(const value of ['数据库问数','库存工作库','数据与 SQL','结论概览'])assert.ok(html.includes(value),value)
  for(const value of ['condition-summary','调整奖金情景','情景推演','followup-options','不应显示的销售指标','condition-changes'])assert.ok(!html.includes(value),value)
 }finally{if(storage)Object.defineProperty(globalThis,'localStorage',{value:storage,configurable:true});else delete (globalThis as any).localStorage}
})

test('database SQL source preview also preserves original column labels and precision',()=>{
 const html=renderToStaticMarkup(<SourcePanel result={result} selected="sql-1" onSelect={noop} onOpen={noop} onProcess={noop}/>)
 assert.ok(html.includes('1.23456789'))
 assert.ok(html.includes('product_id'))
 assert.ok(!html.includes('净销售额（元）'))
})

test('empty database result still exposes SQL and saved names do not invent a sales year',()=>{
 const empty={...result,rows:[],columns:['product_id']}
 const html=renderToStaticMarkup(<ResultData result={empty} onSource={noop}/>)
 assert.ok(html.includes('没有符合条件的数据'))
 assert.ok(html.includes('查看实际 SQL'))
 assert.equal(savedAnalysisName(result),'按产品编号列出库存成本')
 assert.equal(savedAnalysisName({query_spec:{year:2024,region:'华东'}}),'2024 年华东经营分析')
 assert.equal(isDatabaseResult({query_spec:{metric:'document'}}),false)
})


test('database explanations that fail their unit or scope contract retain values with visible reasons',()=>{
 const result={database_answer_check:{status:'raw_fallback',detail:'已保留同次SQL实际结果',reasons:['计数不能变成百分比','净额口径尚未确认']}}
 const html=renderToStaticMarkup(<DatabaseAnswerCheck result={result}/>)
 for(const text of ['已保留查询原值','解释尚待核验','计数不能变成百分比','净额口径尚未确认'])assert.ok(html.includes(text),text)
 assert.equal(renderToStaticMarkup(<DatabaseAnswerCheck result={{}}/>),'')
})
