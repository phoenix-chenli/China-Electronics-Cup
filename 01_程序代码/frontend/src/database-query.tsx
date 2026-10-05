import React,{useState} from 'react'
import {Database,Maximize2} from 'lucide-react'
import {DataTable,Modal,Obj} from './ui'

export function DatabaseQueryDetails({query}: {query?:Obj}){
 if(!query)return null
 const rows=query.rows||[],source=query.source?.name||query.source?.label||'未记录来源名称'
 return <section className="database-query-details" aria-label="数据库查询详情">
  <div className="database-query-source"><Database size={16}/><strong>{source}</strong><span>实际返回 {rows.length} 行{query.row_limit!=null?' · 返回上限 '+query.row_limit+' 行':''}</span></div>
  {query.truncated&&<p className="database-query-warning" role="status">结果已按返回上限截取；当前展示不能代表全部匹配行。</p>}
  <p className="database-query-boundary">保留数据库原始列名和值。单位和业务口径需核对；只读与执行检查通过不等于业务语义已经核验。</p>
  <details><summary>查看实际 SQL 与参数</summary><pre>{query.sql||'未记录 SQL'}</pre><h4>参数</h4><pre>{JSON.stringify(query.params??[],null,2)}</pre></details>
  <details><summary>查看关联规划</summary>{Array.isArray(query.joins)&&query.joins.length===0?<p>本轮未记录关联路径。</p>:<pre>{JSON.stringify(query.joins??[],null,2)}</pre>}<p className="muted">关联规划与实际 SQL 一同核对，尤其是一对多关联和聚合粒度。</p></details>
  <details><summary>查看查询核验记录</summary><pre>{JSON.stringify(query.validation??{},null,2)}</pre></details>
  {query.attempts!=null&&<details><summary>查看查询尝试记录</summary><pre>{JSON.stringify(query.attempts,null,2)}</pre></details>}
 </section>
}

export function DatabaseResultData({result}: {result:Obj}){
 const [expanded,setExpanded]=useState(false)
 const query=result.database_query,rows=result.rows||query?.rows||[],columns=result.columns||query?.columns||[]
 return <section className="analysis-result-block database-result-block"><div className="section-title"><h3>数据库查询结果</h3><button className="text-link" onClick={()=>setExpanded(true)}><Maximize2 size={14}/>展开表格</button></div><DataTable rows={rows} columns={columns} limit={10} raw/><DatabaseQueryDetails query={query}/>{expanded&&<Modal title="数据库查询结果 · 原始字段和值" wide close={()=>setExpanded(false)}><DataTable rows={rows} columns={columns} limit={20} raw/><DatabaseQueryDetails query={query}/></Modal>}</section>
}
