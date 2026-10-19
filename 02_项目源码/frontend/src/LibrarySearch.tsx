import {useEffect,useRef,useState} from 'react';
import {Search,ArrowUpRight,X} from 'lucide-react';
import {api,isAbort,messageOf} from './api';
import type {Source,AnswerEvidence} from './types';
import './LibrarySearch.css';

type Hit={source_id:string;source_name:string;source_sha256:string;source_revision:string;parser_version:string;chunk_id:string;page:number;page_unit:string;start:number;end:number;quote:string;heading_path:string[]};
type SearchJob={id:string;status:'queued'|'running'|'completed'|'failed'|'cancelled';stage:string;question:string;completed_sources:number;total_sources:number;error:string|null;result:null|{results:Hit[];audit:{mode_used:string;fallback_reason:string|null;library_complete:boolean;source_count:number;indexed_chunks:number;source_coverage:{source_id:string;pending_pages:number[];parser_processing_truncated:boolean;scan_truncated:boolean}[]}}};
const active=(job:SearchJob|null)=>job?.status==='queued'||job?.status==='running';
const stamp=(s:Source)=>`${s.id}:${s.sha256}:${s.parser_version}:${s.content_revision}:${s.status}:${s.archived}`;

export function LibrarySearch({projectId,sources,open,onClose,onRead,onAsk}:{projectId:string;sources:Source[];open:boolean;onClose:()=>void;onRead:(evidence:AnswerEvidence)=>void;onAsk?:(sourceId:string,question:string)=>void}){
 const eligible=sources.filter(s=>s.project_id===projectId&&s.usable&&!s.archived&&!['database','spreadsheet'].includes(s.kind));
 const [scopeOpen,setScopeOpen]=useState(true);const [selected,setSelected]=useState<string[]>([]);const [question,setQuestion]=useState('');const [job,setJob]=useState<SearchJob|null>(null);const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [name,setName]=useState('');
 const posting=useRef(false);const generation=useRef(0),jobRef=useRef<SearchJob|null>(null),controller=useRef<AbortController|null>(null),selectionStamp=useRef('');
 const currentStamp=selected.map(id=>sources.find(s=>s.id===id)).map(s=>s?stamp(s):'missing').join('|');
 const setCurrent=(value:SearchJob|null)=>{jobRef.current=value;setJob(value);};
 useEffect(()=>()=>{generation.current++;controller.current?.abort();const current=jobRef.current;if(current&&active(current))void api(`/projects/${projectId}/library-search/${current.id}/cancel`,{method:'POST'}).catch(()=>{});},[projectId]);
 useEffect(()=>{
  if(selectionStamp.current&&selectionStamp.current!==currentStamp&&jobRef.current){const old=jobRef.current;generation.current++;controller.current?.abort();if(active(old))void api(`/projects/${projectId}/library-search/${old.id}/cancel`,{method:'POST'}).catch(()=>{});setCurrent(null);setBusy(false);posting.current=false;setError('所选资料或版本已改变，请确认范围后重新检索。');}
  selectionStamp.current=currentStamp;
 },[currentStamp,projectId]);
 useEffect(()=>{
  if(!job||!active(job))return;
  const id=job.id,request=++generation.current,abort=new AbortController();controller.current=abort;let timer:ReturnType<typeof setTimeout>;
  const poll=async()=>{try{const next=await api<SearchJob>(`/projects/${projectId}/library-search/${id}`,{signal:abort.signal});if(request!==generation.current)return;setCurrent(next);if(next.error)setError(next.error);if(active(next))timer=setTimeout(()=>void poll(),700);}catch(e){if(!isAbort(e)&&request===generation.current){setError(messageOf(e));timer=setTimeout(()=>void poll(),2500);}}};
  timer=setTimeout(()=>void poll(),400);return()=>{clearTimeout(timer);abort.abort();};
 },[job?.id,job?.status,projectId]);
 const changeSelection=(ids:string[])=>{if(ids.length>128){setError('一次最多检索 128 份资料，请缩小范围。');return;}setSelected(ids);setError('');};
 const submit=async()=>{
  if(posting.current||active(jobRef.current)||!question.trim()||!selected.length)return;
  const chosen=selected.map(id=>eligible.find(s=>s.id===id));if(chosen.some(s=>!s?.content_revision)){setError('资料状态已变化，请刷新列表后重试。');return;}
  posting.current=true;setBusy(true);setError('');setCurrent(null);const request=++generation.current;
  try{const result=await api<SearchJob>(`/projects/${projectId}/library-search`,{method:'POST',body:JSON.stringify({question:question.trim(),sources:chosen.map(s=>({id:s!.id,sha256:s!.sha256,parser_version:s!.parser_version,revision:s!.content_revision})),limit:10})});if(request===generation.current)setCurrent(result);else if(active(result))void api(`/projects/${projectId}/library-search/${result.id}/cancel`,{method:'POST'}).catch(()=>{});}
  catch(e){if(!isAbort(e)&&request===generation.current)setError(messageOf(e));}finally{if(request===generation.current){posting.current=false;setBusy(false);}}
 };
 const cancel=async()=>{if(!job)return;const request=generation.current,id=job.id;try{const next=await api<SearchJob>(`/projects/${projectId}/library-search/${id}/cancel`,{method:'POST'});if(request===generation.current&&jobRef.current?.id===id)setCurrent(next);}catch(e){if(request===generation.current&&jobRef.current?.id===id)setError(messageOf(e));}};
 const read=async(hit:Hit,index:number,ask=false)=>{if(!job)return;const request=generation.current;try{const fresh=await api<SearchJob>(`/projects/${projectId}/library-search/${job.id}`);if(request!==generation.current)return;if(fresh.status!=='completed'){setCurrent(fresh);setError(fresh.error??'结果已失效，请重新检索。');return;}const current=eligible.find(s=>s.id===hit.source_id);if(!current||current.sha256!==hit.source_sha256||current.parser_version!==hit.parser_version||current.content_revision!==hit.source_revision){setError('这份资料的版本已变化，请重新检索后继续。');return;}if(ask){onAsk?.(hit.source_id,fresh.question);return;}onRead({id:`L${index+1}`,label:`${hit.source_name} · ${hit.page_unit==='physical_page'?'物理页':'逻辑段'} ${hit.page}`,kind:'text',source_id:hit.source_id,source_name:hit.source_name,sha256:hit.source_sha256,parser_version:hit.parser_version,revision:hit.source_revision,page:hit.page,quote:hit.quote,start:hit.start,end:hit.end,stale:false});}catch(e){setError(messageOf(e));}};
 const result=job?.result;const running=busy||active(job);const filtered=eligible.filter(s=>s.name.toLowerCase().includes(name.toLowerCase()));
 return <section className="library-search-panel" hidden={!open} aria-labelledby="library-search-title">
  <header><div><h2 id="library-search-title">检索资料</h2><p>在所选文档中查找原文，直接核对出处。</p></div><button className="icon-button" onClick={onClose} aria-label="收起资料检索"><X size={18}/></button></header>
  <details className="library-search-scope" open={scopeOpen} onToggle={e=>setScopeOpen(e.currentTarget.open)}><summary>检索范围 · 已选择 {selected.length} 份</summary><div className="library-search-scope-tools"><input aria-label="筛选可检索资料" placeholder="按文件名筛选" value={name} onChange={e=>setName(e.target.value)}/><button disabled={running||!filtered.length||new Set([...selected,...filtered.map(s=>s.id)]).size>128} onClick={()=>changeSelection(Array.from(new Set([...selected,...filtered.map(s=>s.id)])))}>选择筛选结果</button><button disabled={running||!selected.length} onClick={()=>changeSelection([])}>清空</button></div><div className="library-search-options">{filtered.map(s=><label key={s.id}><input type="checkbox" checked={selected.includes(s.id)} disabled={running||(!selected.includes(s.id)&&selected.length>=128)} onChange={e=>changeSelection(e.target.checked?[...selected,s.id]:selected.filter(id=>id!==s.id))}/><span>{s.name}</span><small>{s.page_count} 页／段</small></label>)}{!filtered.length&&<p>{eligible.length?'没有匹配的文档。':'还没有可检索正文，请先导入并完成解析。'}</p>}</div></details>
  <form onSubmit={e=>{e.preventDefault();void submit();}}><label htmlFor="library-search-query">查找内容</label><div className="library-search-input"><input id="library-search-query" onKeyDown={e=>{if(e.key==='Enter'&&e.nativeEvent.isComposing)e.preventDefault();}} value={question} onChange={e=>setQuestion(e.target.value)} maxLength={2000} placeholder="例如：膜过滤反洗周期的限制条件" disabled={running}/><button className="primary" disabled={running||!question.trim()||!selected.length}><Search size={16}/>{running?'正在检索…':'检索'}</button></div></form>
  <p className="library-search-mode">本地 E5 与 BM25 检索，不调用问答 API；首次使用需要准备索引。</p>
  {running&&<div className="library-search-progress" role="status"><span>{job?.stage??'正在提交本地检索…'}</span>{job&&<progress value={job.completed_sources} max={job.total_sources} aria-label="本地索引准备进度"/>}{job&&<button onClick={()=>void cancel()}>取消检索</button>}</div>}
  {error&&<p role="alert" className="library-search-error">{error}</p>}
  {job?.status==='cancelled'&&<p role="status">检索已取消，已完成的索引可以复用。</p>}
  {result&&<div className="library-search-results"><p className="library-search-mode">本次检索：{job?.question}</p><div className="library-search-result-heading"><h3>原文片段 <span>{result.results.length} 条</span></h3><span>{result.audit.source_count} 份资料 · {result.audit.mode_used==='lexical'?'BM25 词法检索':'本地 E5 混合检索'}</span></div>{result.audit.mode_used==='lexical'&&<p className="library-search-warning">本地向量索引尚不可用，本次已降级为 BM25；中文问题检索英文原件可能漏项，可补充原文术语后重试。</p>}{!result.audit.library_complete&&<p className="library-search-warning">部分资料尚有未处理页或未索引范围，本次仅检索已读取内容。请在资料的“处理与诊断”中查看。</p>}{!result.results.length&&<p>已读取范围内没有找到匹配片段。可以调整关键词；这不表示原件没有答案。</p>}{result.results.map((hit,i)=><article key={`${hit.source_id}:${hit.chunk_id}`}><div><strong>{i+1}. {hit.source_name}</strong><span>{hit.page_unit==='physical_page'?'物理页':'逻辑段'} {hit.page}</span></div>{hit.heading_path.length>0&&<p className="library-search-path">{hit.heading_path.join(' / ')}</p>}<blockquote>{hit.quote}</blockquote><div className="library-hit-actions"><button onClick={()=>void read(hit,i)}>打开原文<ArrowUpRight size={16}/></button>{onAsk&&<button onClick={()=>void read(hit,i,true)}>基于此资料提问</button>}</div></article>)}</div>}
 </section>;
}
