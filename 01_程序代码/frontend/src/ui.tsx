import {shouldSendKey} from './pet-state'
import {rawTableValue,savedAnalysisName} from './result-presentation'
import React, {lazy, Suspense, useEffect, useRef, useState} from 'react'
import {createPortal} from 'react-dom'
import {ArrowRight, ChevronLeft, ChevronRight, FileText, LoaderCircle, Search, Send, X} from 'lucide-react'
export type Obj = Record<string, any>
export type Page = 'home'|'analysis'|'evidence'|'data'|'scenario'|'records'|'settings'
export const examples = [
 '2024 年华东销售冠军是谁？结合项目复盘说明方法和奖金。',
 '2024 年华东净销售额是多少？',
 '2024 年华北有多少客户？',
 '增长率是多少？'
]
export async function api(path:string, body?:unknown, method?:string):Promise<any>{
 const r=await fetch(path, body===undefined?undefined:{method:method||'POST',headers:body instanceof FormData?undefined:{'Content-Type':'application/json'},body:body instanceof FormData?body:JSON.stringify(body)}).catch(()=>{throw new Error('本地服务未连接，请启动证流后重试')})
 let data:any; try{data=await r.json()}catch{throw new Error('服务返回了无法读取的内容')}
 if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'操作失败，请检查输入后重试')
 return data
}
export const num=(v:any)=>Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2})
export const money=(v:any)=>'¥'+num(v)
export const formatValue=(v:any,unit?:string)=>unit==='CNY'?money(v):unit==='ratio'?num(Number(v)*100)+'%':num(v)+(unit?' '+unit:'')
export const timeLabel=(v:string)=>new Date(v).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
export const fieldNames:Obj={product_id:'产品编号',product_name:'产品',net_sales:'净销售额（元）',gross_sales:'销售额（元）',order_count:'订单数',customer_count:'客户数',region:'区域',year:'年份',amount:'金额',current_net_sales:'本期净销售额（元）',compare_net_sales:'对比期净销售额（元）',growth_rate:'增长率',order_id:'订单编号',order_date:'日期',sales_amount:'销售额',returned_amount:'退货金额',customer_name:'客户',count:'数量',current_value:'本期',previous_value:'对比期',quantity:'数量',currency:'币种'}
const mascotAssets = {wave:'hero-welcome', work:'pet-04-thinking-v2', reading:'pet-06-reading-v2', pointing:'pet-09-pointing-v2', success:'pet-10-success'}
export function Mascot({pose='wave',className='',...props}: {pose?:keyof typeof mascotAssets,className?:string}&React.ImgHTMLAttributes<HTMLImageElement>){return <img className={'mascot '+className} src={'/'+mascotAssets[pose]+'.webp'} alt="证流助手" draggable={false} {...props}/>}
export function useMedia(query:string){const [matches,setMatches]=useState(()=>matchMedia(query).matches);useEffect(()=>{const mq=matchMedia(query);const update=()=>setMatches(mq.matches);update();mq.addEventListener('change',update);return()=>mq.removeEventListener('change',update)},[query]);return matches}
export function IconButton({label,children,...props}: {label:string}&React.ButtonHTMLAttributes<HTMLButtonElement>){return <button type="button" className="icon-button" aria-label={label} title={label} {...props}>{children}</button>}
export function Spinner({text='正在查询'}:{text?:string}){return <div className="loading" role="status"><LoaderCircle className="spin" size={20}/>{text}</div>}
export function Empty({title,children}:{title:string,children?:React.ReactNode}){return <div className="empty"><FileText size={32}/><strong>{title}</strong>{children}</div>}
export function RichText({text}:{text:string}){return <>{text.split('**').map((v,i)=>i%2?<strong key={i}>{v}</strong>:v)}</>}
export function Modal({title,close,children,wide=false,className=''}:{title:string,close:()=>void,children:React.ReactNode,wide?:boolean,className?:string}){
 const ref=useRef<HTMLDivElement>(null)
 useEffect(()=>{const prev=document.activeElement as HTMLElement; const root=ref.current; const oldOverflow=document.body.style.overflow;document.body.style.overflow='hidden'; if(!root?.contains(document.activeElement)){const visible=(selector:string)=>Array.from(root?.querySelectorAll<HTMLElement>(selector)||[]).find(el=>el.getClientRects().length>0&&getComputedStyle(el).visibility!=='hidden');(visible('input:not([disabled]),textarea:not([disabled]),select:not([disabled])')||visible('button:not([disabled]),[tabindex="0"]'))?.focus();} const key=(e:KeyboardEvent)=>{if(root!==Array.from(document.querySelectorAll('.modal[role="dialog"]')).slice(-1)[0])return;if(e.key==='Escape'){e.stopPropagation();close()}if(e.key==='Tab'){const els=Array.from(root?.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href],[tabindex="0"]')||[]).filter(el=>el.getClientRects().length>0);if(!els.length)return;if(e.shiftKey&&document.activeElement===els[0]){e.preventDefault();els[els.length-1]?.focus()}else if(!e.shiftKey&&document.activeElement===els[els.length-1]){e.preventDefault();els[0].focus()}}};document.addEventListener('keydown',key);return()=>{document.removeEventListener('keydown',key);document.body.style.overflow=oldOverflow;requestAnimationFrame(()=>{const active=document.activeElement as HTMLElement;const visible=(el:HTMLElement|null)=>!!el?.isConnected&&!!el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden';if(active!==document.body&&visible(active)&&!root?.contains(active))return;const target=visible(prev)?prev:document.querySelector<HTMLElement>('[aria-label="搜索工作区"]');if(target&&!document.querySelector('.modal[role="dialog"]'))target.focus()})}},[])
 return createPortal(<div className="modal-backdrop" onMouseDown={e=>{if(e.target===e.currentTarget)close()}}><div ref={ref} className={'modal '+className+' '+(wide?'wide':'')} role="dialog" aria-modal="true" aria-label={title}><div className="modal-title"><h2>{title}</h2><IconButton label="关闭窗口" onClick={close}><X size={18}/></IconButton></div>{children}</div></div>,document.body)
}
export function QueryInput({value,onChange,onSend,busy=false,placeholder='输入你的问题',large=false,label='输入问题',actions}:{value:string,onChange:(v:string)=>void,onSend:()=>void,busy?:boolean,placeholder?:string,large?:boolean,label?:string,actions?:React.ReactNode}){
 const input=useRef<HTMLTextAreaElement>(null)
 const fitHeight=()=>{const el=input.current;if(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,150)+'px'}}
 useEffect(fitHeight,[value])
 useEffect(()=>{
  const el=input.current;if(!el)return
  let frame=0,lastWidth=el.getBoundingClientRect().width
  const schedule=()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(fitHeight)}
  const resize=new ResizeObserver(()=>{const width=el.getBoundingClientRect().width;if(width!==lastWidth){lastWidth=width;schedule()}})
  resize.observe(el)
  const appearance=new MutationObserver(schedule)
  appearance.observe(document.documentElement,{attributes:true,attributeFilter:['style','class','data-density']})
  window.addEventListener('resize',schedule)
  document.fonts?.addEventListener('loadingdone',schedule)
  return()=>{cancelAnimationFrame(frame);resize.disconnect();appearance.disconnect();window.removeEventListener('resize',schedule);document.fonts?.removeEventListener('loadingdone',schedule)}
 },[])
 return <form className={'query-input focus-query '+(large?'large':'')} onSubmit={e=>{e.preventDefault();if(value.trim()&&!busy)onSend()}}><div className="query-field-row"><textarea ref={input} aria-label={label} disabled={busy} value={value} onChange={e=>onChange(e.target.value)} rows={1} placeholder={placeholder} maxLength={2000} onKeyDown={e=>{if(shouldSendKey(e.key,e.shiftKey,e.nativeEvent.isComposing,e.keyCode)){e.preventDefault();if(value.trim()&&!busy)onSend()}}}/><button className="send-button" type="submit" aria-label="发送问题" title="发送问题" disabled={!value.trim()||busy}>{busy?<LoaderCircle className="spin" size={20}/>:<Send size={19}/>}</button></div>{actions&&<div className="query-input-footer">{actions}</div>}</form>
}

export type ChartProps = {labels:string[],values:(number|null)[],kind?:'bar'|'line',height?:number,label?:string,unit?:string,secondary?:number[],selectedIndex?:number|null,onSelect?:(index:number)=>void}
const LazyChart=lazy(()=>import('./chart'))
export function Chart(props:ChartProps){
 const frame=useRef<HTMLDivElement>(null),[ready,setReady]=useState(false)
 useEffect(()=>{
  if(!frame.current)return
  if(!('IntersectionObserver' in window)){setReady(true);return}
  const observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)){setReady(true);observer.disconnect()}},{rootMargin:'80px'})
  observer.observe(frame.current);return()=>observer.disconnect()
 },[])
 return <div ref={frame} style={{height:props.height??220,minWidth:0}}>{ready&&<Suspense fallback={<div className="chart-loading" role="status">正在加载图表…</div>}><LazyChart {...props}/></Suspense>}</div>
}
export function DataTable({rows=[],columns,limit=10,selectedIndex=null,onSelectRow,raw=false}:{rows:Obj[],columns?:string[],limit?:number,selectedIndex?:number|null,onSelectRow?:(index:number)=>void,raw?:boolean}){
 const [page,setPage]=useState(0);const cols=columns||Object.keys(rows[0]||{});const pages=Math.ceil(rows.length/limit)
 useEffect(()=>setPage(0),[rows]);useEffect(()=>{if(selectedIndex!==null)setPage(Math.floor(selectedIndex/limit))},[selectedIndex,limit])
 if(!rows.length)return <Empty title="没有符合条件的数据"/>
 return <div className="data-table"><div className="table-scroll" tabIndex={0} aria-label="结果数据表"><table><thead><tr>{cols.map(c=><th key={c}>{raw?c:fieldNames[c]||c}</th>)}</tr></thead><tbody>{rows.slice(page*limit,(page+1)*limit).map((row,i)=>{const index=page*limit+i;return <tr key={index} className={selectedIndex===index?'selected-row':''}>{cols.map((c,ci)=>{const text=raw?rawTableValue(row[c]):typeof row[c]==='number'?num(row[c]):String(row[c]??'—');return <td key={c}>{onSelectRow&&ci===0?<button className="row-select" aria-pressed={selectedIndex===index} aria-label={'选中 '+(raw?text:String(row.product_name||row.region||row[c]))} onClick={()=>onSelectRow(index)}>{text}</button>:text}</td>})}</tr>})}</tbody></table></div>{pages>1&&<div className="pagination"><span>{rows.length} 行 · 第 {page+1}/{pages} 页</span><IconButton label="上一页" disabled={!page} onClick={()=>setPage(page-1)}><ChevronLeft size={16}/></IconButton><IconButton label="下一页" disabled={page===pages-1} onClick={()=>setPage(page+1)}><ChevronRight size={16}/></IconButton></div>}</div>
}
export function TextLink({children,onClick}:{children:React.ReactNode,onClick:()=>void}){return <button className="text-link" onClick={onClick}>{children}<ArrowRight size={14}/></button>}
export function SaveDialog({result,onSave,close}:{result:Obj,onSave:(s:string)=>Promise<void>,close:()=>void}){
 const [name,setName]=useState(()=>savedAnalysisName(result));const [busy,setBusy]=useState(false);const [err,setErr]=useState('')
 return <Modal title="保存分析" close={close}><form onSubmit={async e=>{e.preventDefault();setBusy(true);try{await onSave(name.trim());close()}catch(e){setErr(String(e))}finally{setBusy(false)}}}><label className="form-field">名称<input value={name} onChange={e=>setName(e.target.value)} maxLength={120} required/></label><p className="muted">查询条件、结果与来源版本将一并保存。</p>{err&&<p className="error-text">{err}</p>}<div className="modal-actions"><button type="button" className="button" onClick={close}>取消</button><button className="button primary" disabled={busy||!name.trim()}>{busy?'保存中':'保存'}</button></div></form></Modal>
}
