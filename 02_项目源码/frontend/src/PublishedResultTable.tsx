import {useState} from 'react';
import {ArrowDown,ArrowUp,ArrowUpRight,Check,Download,RotateCcw} from 'lucide-react';
import type {AnswerEvidence} from './types';
import type {PublishedTable} from './resultTables';
import {compareCells,resultCell} from './resultTables';
import {readerCsv} from './readerTools';

export function PublishedResultTable({table,evidence,onEvidence}:{table:PublishedTable;evidence:AnswerEvidence[];onEvidence:(e:AnswerEvidence)=>void}){
 const [sort,setSort]=useState<{column:number;direction:1|-1}|null>(null),[page,setPage]=useState(0),[exported,setExported]=useState(false);
 const order=table.rows.map((_,i)=>i);if(sort)order.sort((a,b)=>{const av=table.rows[a][sort.column],bv=table.rows[b][sort.column];return av==null||bv==null?compareCells(av,bv):sort.direction*compareCells(av,bv)||a-b;});
 const numeric=table.columns.map((_,j)=>table.rows.some(row=>row[j]!=null)&&table.rows.every(row=>row[j]==null||typeof row[j]==='number'||typeof row[j]==='string'&&/^[+-]?[\d,.]+$/.test(row[j] as string)));
 const size=50,total=table.rows.length,pages=Math.max(1,Math.ceil(total/size));const current=Math.min(page,pages-1),visible=order.slice(current*size,(current+1)*size);
 const tableSources=evidence.filter(e=>e.kind==='table'&&(!table.evidence_ids?.length||table.evidence_ids.includes(e.id)));
 const exportCsv=()=>{const csv=readerCsv(table.columns,order.map(i=>table.rows[i]));const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download=`${(table.title??table.id).replace(/[\\/:*?"<>|]/g,'_')}_查询结果.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);setExported(true);};
 return <section className="answer-result-table"><div className="section-minihead"><h3>{table.title??'查询汇总结果'}</h3><span>{total.toLocaleString()} 行 · {table.columns.length} 列</span></div>
  <div className="result-table-tools"><span>{total.toLocaleString()} 行 · {table.columns.length} 列 · 已发布结果</span>{sort&&<button className="subtle" onClick={()=>{setSort(null);setPage(0);}}><RotateCcw size={14}/>恢复原顺序</button>}<button className="subtle" onClick={exportCsv} disabled={!total}>{exported?<Check size={14}/>:<Download size={14}/>}导出结果 CSV</button></div>
  {total?<div className="table-scroll" tabIndex={0} aria-label="已发布的完整查询结果，可横向滚动"><table><thead><tr>{table.columns.map((column,j)=><th scope="col" key={j} className={numeric[j]?'numeric-cell':undefined} aria-sort={sort?.column===j?(sort.direction===1?'ascending':'descending'):'none'}><button title="点击排序，再次点击切换降序" onClick={()=>{setSort({column:j,direction:sort?.column===j&&sort.direction===1?-1:1});setPage(0);}}>{column}{sort?.column===j&&(sort.direction===1?<ArrowUp size={13}/>:<ArrowDown size={13}/>)}</button>{table.column_units?.[column]&&<small>{table.column_units[column]}</small>}</th>)}</tr></thead><tbody>{visible.map(i=><tr key={i}>{table.rows[i].map((v,j)=><td key={j} className={v==null?'result-null':typeof v==='number'||typeof v==='string'&&/^[+-]?[\d,.]+$/.test(v)?'numeric-cell':undefined} title={v==null?'空值（NULL），不是 0':undefined}>{resultCell(v,table.display_rows?.[i]?.[j]??v)}</td>)}</tr>)}</tbody></table></div>:<p className="answer-empty-result">查询已完成，没有符合本次条件的记录。可以调整筛选条件后继续。</p>}
  {pages>1&&<nav className="result-table-pagination" aria-label="查询结果分页"><span>{current*size+1}–{Math.min((current+1)*size,total)} / {total} 行</span><button disabled={!current} onClick={()=>setPage(current-1)}>上一页</button><span>第 {current+1} / {pages} 页</span><button disabled={current>=pages-1} onClick={()=>setPage(current+1)}>下一页</button></nav>}
  {table.query?.truncated&&<p className="notice warning">结果已达到返回上限，当前保留 {total} 行；未将这些行描述为全库数据。</p>}
  <div className="answer-source-foot"><span>{tableSources.map(e=>e.table_name).filter((v,i,a)=>v&&a.indexOf(v)===i).join(' · ')||'已发布结果快照'}</span>{tableSources[0]&&<button onClick={()=>onEvidence(tableSources[0])}>查看原始表<ArrowUpRight size={14}/></button>}</div>
  {exported&&<p className="caption" role="status">已生成 CSV：包含本次返回的全部 {total} 行，顺序与当前排序一致，数值使用原始精度；NULL 导出为空单元格。</p>}
 </section>;
}
