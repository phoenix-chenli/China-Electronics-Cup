import {useEffect,useRef,useState} from 'react';
import {ArrowDown,ArrowRight,ArrowUp,Bookmark,Check,FileText,Trash2} from 'lucide-react';
import {api,isAbort,messageOf} from './api';
import {ResearchDialog} from './ResearchDialog';
import './BriefBuilder.css';

export type BriefRecord={id:string;question:string;status:string;created_at:string;turn_index?:number;scope_stale?:boolean;parent_answer_id?:string|null};
type SavedBrief={id:string;title:string;answer_ids:string[];created_at:string;html_sha256:string};
type BriefSummary=Omit<SavedBrief,'answer_ids'>;
type Props={projectId:string;projectName:string;records:BriefRecord[];favorites:string[];onOpenAnswer:(id:string)=>void;onContinueAnswer?:(id:string)=>void;onToggleFavorite?:(id:string)=>void};
const MAX_SELECTION=20;
const statusTitle:Record<string,string>={completed:'已完成',needs_clarification:'待澄清',insufficient_evidence:'依据不足',failed:'未完成',cancelled:'已取消',queued:'排队中',running:'处理中'};
export function canSelectForBrief(record:BriefRecord){return record.status==='completed'&&record.scope_stale!==true;}
export function toggleBriefSelection(selected:BriefRecord[],record:BriefRecord):BriefRecord[]{
 if(selected.some(item=>item.id===record.id))return selected.filter(item=>item.id!==record.id);
 if(!canSelectForBrief(record)||selected.length>=MAX_SELECTION)return selected;
 return [...selected,{...record}];
}
export function briefRequest(title:string,selected:BriefRecord[]){
 const clean=title.trim();
 if(!clean||clean.length>120)throw new Error('请填写 1 至 120 字的简报标题。');
 if(!selected.length||selected.length>MAX_SELECTION||new Set(selected.map(item=>item.id)).size!==selected.length||selected.some(item=>!item.id||!canSelectForBrief(item)))throw new Error('请选择 1 至 20 条不同且已完成的分析。');
 return {title:clean,answer_ids:selected.map(item=>item.id)};
}
export function moveBriefSelection(selected:BriefRecord[],id:string,direction:-1|1):BriefRecord[]{
 const index=selected.findIndex(item=>item.id===id),next=index+direction;
 if(index<0||next<0||next>=selected.length)return selected;
 const reordered=[...selected];[reordered[index],reordered[next]]=[reordered[next],reordered[index]];return reordered;
}

/** Preview the exact immutable export; HTML never executes inside the workbench. */
function BriefDocument({projectId,brief}:{projectId:string;brief:BriefSummary}){
 const [html,setHtml]=useState('');const [url,setUrl]=useState('');const [error,setError]=useState('');const [attempt,setAttempt]=useState(0);
 useEffect(()=>{
  const controller=new AbortController();let objectUrl='';setHtml('');setUrl('');setError('');
  void fetch(`/api/projects/${encodeURIComponent(projectId)}/briefs/${encodeURIComponent(brief.id)}`,{signal:controller.signal,credentials:'same-origin'}).then(async response=>{
   if(!response.ok){let detail='无法读取已保存简报';try{detail=(await response.json()).detail??detail;}catch{/* Preserve a useful non-JSON error. */}throw new Error(detail);}
   const contentType=response.headers.get('content-type')??'';
   if(!contentType.includes('text/html'))throw new Error('简报内容格式不正确，未显示预览。');
   const content=await response.text();if(controller.signal.aborted)return;
   objectUrl=URL.createObjectURL(new Blob([content],{type:'text/html;charset=utf-8'}));setHtml(content);setUrl(objectUrl);
  }).catch(e=>{if(!controller.signal.aborted&&!isAbort(e))setError(messageOf(e));});
  return()=>{controller.abort();if(objectUrl)URL.revokeObjectURL(objectUrl);};
 },[projectId,brief.id,attempt]);
 return <section className="brief-document" aria-label="已保存简报预览">
  <div className="brief-document-toolbar"><span><Check size={16}/>已保存的完整内容</span>{url&&<div><a className="brief-link-button" href={url} download={`${brief.title}.html`}>下载 HTML</a><a className="brief-link-button primary" href={url} target="_blank" rel="noopener noreferrer">打开并打印 PDF</a></div>}</div>
  {error?<div className="brief-preview-message"><p role="alert">{error}</p><button onClick={()=>setAttempt(value=>value+1)}>重新读取</button></div>:html?<iframe title={`${brief.title}：导出内容预览`} sandbox="" srcDoc={html}/>:<p className="brief-preview-message" role="status">正在读取已保存的简报…</p>}
 </section>;
}

export function BriefBuilder({projectId,projectName,records,favorites,onOpenAnswer,onContinueAnswer,onToggleFavorite}:Props){
 const [selecting,setSelecting]=useState(false);const [selected,setSelected]=useState<BriefRecord[]>([]);const [open,setOpen]=useState(false);
 const [title,setTitle]=useState(`${projectName}分析简报`.slice(0,120));const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [saved,setSaved]=useState<SavedBrief|null>(null);
 const [library,setLibrary]=useState<BriefSummary[]|null>(null);const [libraryBusy,setLibraryBusy]=useState(false);const [viewing,setViewing]=useState<BriefSummary|null>(null);
 const mounted=useRef(true);const scope=useRef(projectId);scope.current=projectId;const sending=useRef(false);const requestGeneration=useRef(0);const completed=useRef(false);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
 // The parent keys this component by project ID. Keep a defensive reset for
 // other callers too; a late response never opens a different project's brief.
 useEffect(()=>{requestGeneration.current++;sending.current=false;completed.current=false;setBusy(false);setSelecting(false);setSelected([]);setOpen(false);setError('');setSaved(null);setLibrary(null);setLibraryBusy(false);setViewing(null);setTitle(`${projectName}分析简报`.slice(0,120));},[projectId]);
 const choose=(record:BriefRecord)=>{if(sending.current)return;setSelected(current=>toggleBriefSelection(current,record));completed.current=false;setSaved(null);setError('');};
 const remove=(id:string)=>{if(sending.current)return;setSelected(current=>current.filter(item=>item.id!==id));completed.current=false;setSaved(null);setError('');};
 const close=()=>{if(!sending.current)setOpen(false);};
 const showSaved=async()=>{const requestProject=projectId,generation=requestGeneration.current;setLibraryBusy(true);setError('');try{const items=await api<BriefSummary[]>(`/projects/${encodeURIComponent(requestProject)}/briefs`);if(mounted.current&&scope.current===requestProject&&generation===requestGeneration.current)setLibrary(items);}catch(e){if(mounted.current&&scope.current===requestProject&&generation===requestGeneration.current)setError(messageOf(e));}finally{if(mounted.current&&scope.current===requestProject&&generation===requestGeneration.current)setLibraryBusy(false);}};
 const generate=async()=>{
  if(sending.current||completed.current||saved)return;let body:ReturnType<typeof briefRequest>;
  try{body=briefRequest(title,selected);}catch(e){setError(messageOf(e));return;}
  const requestProject=projectId,generation=++requestGeneration.current;sending.current=true;setBusy(true);setError('');
  try{
   const result=await api<SavedBrief>(`/projects/${encodeURIComponent(requestProject)}/briefs`,{method:'POST',body:JSON.stringify(body)});
   if(mounted.current&&scope.current===requestProject&&generation===requestGeneration.current){completed.current=true;setSaved(result);}
  }catch(e){if(mounted.current&&scope.current===requestProject&&generation===requestGeneration.current)setError(messageOf(e));}
  finally{if(generation===requestGeneration.current){sending.current=false;if(mounted.current&&scope.current===requestProject)setBusy(false);}}
 };
 return <>
  <div className={`brief-selection-toolbar${selecting?' is-selecting':''}`}>
   <button type="button" aria-pressed={selecting} onClick={()=>{if(sending.current)return;setSelecting(!selecting);setSelected([]);completed.current=false;setSaved(null);setError('');}}>{selecting?'取消选择':'选择分析，汇成简报'}</button>
   {!selecting&&<button type="button" disabled={libraryBusy} onClick={()=>void showSaved()}><FileText size={16}/>{libraryBusy?'正在读取…':'已保存简报'}</button>}
   {selecting&&<><span role="status">已选 {selected.length} / {MAX_SELECTION} 条</span><button type="button" className="primary" disabled={!selected.length} onClick={()=>setOpen(true)}>整理并生成简报</button></>}
  </div>
  {selecting&&<p className="caption brief-selection-note">选择已完成且来源有效的分析，最多 20 条；稍后可调整顺序。</p>}
  {!open&&error&&<p className="notice warning" role="alert">{error}</p>}
  <div className="history-list">{records.map(record=>{
   const checked=selected.some(item=>item.id===record.id),eligible=canSelectForBrief(record);
   return <div className={`brief-history-row status-${record.status}${checked?' is-selected':''}${record.parent_answer_id?' is-followup':''}`} key={record.id}>
    {selecting&&<label className="brief-pick" title={!eligible?(record.scope_stale?'来源已变化，请重新分析':'仅可选择已完成的分析'):!checked&&selected.length>=MAX_SELECTION?'最多选择 20 条分析':undefined}>
     <input type="checkbox" checked={checked} disabled={!eligible||(!checked&&selected.length>=MAX_SELECTION)} onChange={()=>choose(record)} aria-label={`选择分析：${record.question}`}/>
    </label>}
    <button type="button" className="history-row" aria-pressed={selecting?checked:undefined} disabled={selecting&&(!eligible||(!checked&&selected.length>=MAX_SELECTION))} onClick={()=>selecting?choose(record):onOpenAnswer(record.id)}>
     <span className="history-date" title={new Date(record.created_at).toLocaleString('zh-CN')}>{new Date(record.created_at).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'})}</span>
     <span><strong>{record.question}</strong><small><i className={`history-status-dot ${record.status}`} aria-hidden="true"/>{record.parent_answer_id?'基于上一轮追问 · ':''}第 {record.turn_index??1} 轮 · {statusTitle[record.status]??record.status}{record.scope_stale?' · 资料已更新':selecting&&!eligible?' · 不可加入简报':''}</small></span>
     {!onToggleFavorite&&(favorites.includes(record.id)?<Bookmark size={17}/>:<ArrowRight size={17}/>)}
    </button>
    {!selecting&&<div className="brief-record-actions">{onToggleFavorite&&<button aria-label={`${favorites.includes(record.id)?'取消收藏':'收藏'}：${record.question}`} aria-pressed={favorites.includes(record.id)} onClick={()=>onToggleFavorite(record.id)}><Bookmark size={16} fill={favorites.includes(record.id)?'currentColor':'none'}/><span>{favorites.includes(record.id)?'已收藏':'收藏'}</span></button>}<button onClick={()=>onOpenAnswer(record.id)}>查看回答</button>{onContinueAnswer&&<button className="brief-continue" onClick={()=>onContinueAnswer(record.id)}>基于此回答追问</button>}</div>}
   </div>;
  })}</div>
  {open&&<ResearchDialog title="汇集带来源的简报" onClose={close} wide>
   <div className="brief-workspace"><aside className="brief-outline"><h3>已选分析（{selected.length}）</h3>
   <ol className="brief-selected-list">{selected.map((record,index)=><li key={record.id}><span><b>{index+1}.</b> {record.question}</span><div><button type="button" disabled={busy||!!saved||index===0} onClick={()=>setSelected(current=>moveBriefSelection(current,record.id,-1))} aria-label={`上移分析：${record.question}`} title="上移"><ArrowUp size={16}/></button><button type="button" disabled={busy||!!saved||index===selected.length-1} onClick={()=>setSelected(current=>moveBriefSelection(current,record.id,1))} aria-label={`下移分析：${record.question}`} title="下移"><ArrowDown size={16}/></button><button type="button" disabled={busy||!!saved} onClick={()=>remove(record.id)} aria-label={`移除分析：${record.question}`} title="移除"><Trash2 size={16}/></button></div></li>)}</ol>
   <label className="field">简报标题<input value={title} maxLength={120} disabled={busy||!!saved} onChange={e=>setTitle(e.target.value)}/></label>
   <div className="brief-includes"><p><Check size={16}/>保留来源引用与版本</p><p><Check size={16}/>保留实际结果、SQL 与核对说明</p></div><p className="caption">先保存完整 HTML，再打开打印为 PDF。保存时核对所选答案版本。</p>
   {!selected.length&&<p className="notice">已移除全部分析，请关闭后重新选择。</p>}
   {busy&&<p role="status">正在核对所选答案版本并保存简报…</p>}
   {error&&<p className="notice warning" role="alert">{error}</p>}
   {saved&&<div className="brief-saved" role="status"><strong>已保存 {saved.answer_ids.length} 条分析</strong><p>这份简报保留当前快照，后续资料更新不会改写。</p><a href={`/api/projects/${encodeURIComponent(projectId)}/briefs/${encodeURIComponent(saved.id)}`} download>下载已保存简报</a></div>}
   </aside>{saved?<BriefDocument projectId={projectId} brief={saved}/>:<section className="brief-draft" aria-label="简报结构"><div className="brief-paper"><h1>{title.trim()||'分析简报'}</h1><p className="brief-draft-label">{projectName} · {selected.length} 项分析</p><ol>{selected.map(record=><li key={record.id}>{record.question}</li>)}</ol><div className="brief-draft-note"><FileText size={22}/><p>保存后在此预览完整简报</p><span>内容由已保存的回答、查询结果和来源生成。</span></div></div></section>}</div>
   <footer><button type="button" disabled={busy} onClick={close}>{saved?'关闭':'返回选择'}</button>{!saved&&<button type="button" className="primary" disabled={busy||!selected.length||!title.trim()} onClick={()=>void generate()}>{busy?'正在保存…':'生成简报'}</button>}</footer>
  </ResearchDialog>}
  {library!==null&&!viewing&&<ResearchDialog title="已保存简报" onClose={()=>setLibrary(null)} wide><div className="saved-brief-list">{library.length?library.map(brief=><button key={brief.id} onClick={()=>setViewing(brief)}><FileText size={20}/><span><strong>{brief.title}</strong><small>{new Date(brief.created_at).toLocaleString('zh-CN')}</small></span><ArrowRight size={18}/></button>):<p className="muted">还没有保存的简报。选择分析记录后即可整理。</p>}</div></ResearchDialog>}
  {viewing&&<ResearchDialog title={viewing.title} onClose={()=>setViewing(null)} wide><div className="brief-workspace saved-only"><BriefDocument projectId={projectId} brief={viewing}/></div></ResearchDialog>}
 </>;
}
