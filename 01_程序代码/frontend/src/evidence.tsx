import {ActionMenu, MenuAction} from './controls'
import {companionActivity} from './pet-state'
import React, {useEffect,useMemo,useRef,useState} from 'react'
import {ArrowLeft,CheckCircle2,ChevronDown,ChevronLeft,ChevronRight,Database,ExternalLink,FileText,Folder,Hand,Image as ImageIcon,ListTree,MapPin,Maximize2,MessageCircle,MousePointer2,RefreshCw,Search,ShieldCheck,TextCursor,X} from 'lucide-react'
import {api,DataTable,Empty,IconButton,Modal,Obj,QueryInput,Spinner,useMedia} from './ui'
import {Process} from './workspace'

const cp=(text:string,start:number,end?:number)=>Array.from(text).slice(start,end).join('')
const size=(n:number)=>n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB'
const sourceKey=(e:Obj)=>[e?.id,e?.locator?.sha256,e?.locator?.parse_id,e?.locator?.text_range?.start].join(':')
const position=(box:number[])=>({left:box[0]*100+'%',top:box[1]*100+'%',width:(box[2]-box[0])*100+'%',height:(box[3]-box[1])*100+'%'})

function Outline({items,onSelect}:Obj){
 const tree=useMemo(()=>{const roots:Obj[]=[];const stack:Obj[]=[];items.forEach((h:Obj,i:number)=>{const node={...h,key:i,children:[]};while(stack.length&&stack[stack.length-1].level>=h.level)stack.pop();(stack.length?stack[stack.length-1].children:roots).push(node);stack.push(node)});return roots},[items])
 const branch=(nodes:Obj[])=>nodes.map(h=><div key={h.key} className="outline-node">{h.children.length?<details open><summary><ChevronRight size={12} className="outline-chevron"/><Folder size={14}/><button onClick={e=>{e.preventDefault();onSelect(h)}}>{h.title}</button></summary><div className="outline-children">{branch(h.children)}</div></details>:<button className="outline-leaf" onClick={()=>onSelect(h)}><FileText size={13}/><span>{h.title}</span><small>{h.page}</small></button>}</div>)
 return <div className="outline-tree">{branch(tree)}</div>
}

function MarkedText({block,ranges,onSource}:Obj){
 const prefix=block.kind==='heading'?(block.text.match(/^#{1,6}\s+/)?.[0].length||0):0
 const start=block.start+prefix,end=block.end
 const points=[...new Set([start,end,...ranges.flatMap((r:Obj)=>[Math.max(start,Math.min(end,r.start)),Math.max(start,Math.min(end,r.end))])])].sort((a,b)=>a-b)
 const children=points.slice(0,-1).map((a,i)=>{const z=points[i+1];const r=ranges.find((r:Obj)=>r.start<z&&r.end>a);const text=cp(block.text,a-block.start,z-block.start)
   return <span key={a} data-start={a} data-end={z}>{r?<mark className={r.kind==='search'?'search-mark':'source-mark'} onClick={()=>{if(!window.getSelection()?.toString())onSource(r)}}>{text}</mark>:text}</span>})
 const className='source-text-block '+(block.kind==='table_row'?'source-table-row':'')
 return block.kind==='heading'?<h3 id={block.id} className={className} data-level={block.level}>{children}</h3>:<p id={block.id} className={className}>{children}</p>
}

export function EvidencePage({result,docs,selected,onSelect,navigate,notify,state,onState,onAsk,busy,onShare,onVision}:Obj){
 const compact=useMedia('(max-width:1024px)')
 const evidence:Obj[]=result?.evidence||[]
 const first=selected||state?.target||evidence[0]||(docs[0]?{id:docs[0].id,type:'document',source:docs[0].name,locator:{document_id:docs[0].id,sha256:docs[0].sha256,page:1}}:null)
 const restored=!selected||sourceKey(selected)===sourceKey(state?.target||{})
 const [current,setCurrent]=useState<Obj|null>(first),[page,setPage]=useState((restored?state?.page:null)||first?.locator?.page||1),[zoom,setZoom]=useState(state?.zoom||'fit')
 const [view,setView]=useState(state?.view||'reader'),[tab,setTab]=useState('files'),[filter,setFilter]=useState('all'),[term,setTerm]=useState(''),[searchDocs,setSearchDocs]=useState<Obj[]>([])
 const [meta,setMeta]=useState<Obj|null>(null),[data,setData]=useState<Obj|null>(null),[bound,setBound]=useState<Obj|null>(null),[loading,setLoading]=useState(false),[error,setError]=useState(''),[reload,setReload]=useState(0)
 const [mode,setMode]=useState('select'),[textLayer,setTextLayer]=useState(true),[imageLayer,setImageLayer]=useState(false),[full,setFull]=useState(false)
 const [searchOpen,setSearchOpen]=useState(false),[query,setQuery]=useState(''),[hits,setHits]=useState<Obj[]>([]),[hit,setHit]=useState(0),[searchBusy,setSearchBusy]=useState(false)
 const [focus,setFocus]=useState<Obj|null>(restored?state?.focus||null:null),[selection,setSelection]=useState<Obj|null>(null),[question,setQuestion]=useState(''),[pageDraft,setPageDraft]=useState(String(page)),[pageError,setPageError]=useState('')
 const [claimId,setClaimId]=useState(''),[verification,setVerification]=useState<Obj|null>(null),[verifyError,setVerifyError]=useState(''),[process,setProcess]=useState(false),[reparseBusy,setReparseBusy]=useState(false),[reparse,setReparse]=useState<Obj|null>(null)
 const reader=useRef<HTMLDivElement>(null),surface=useRef<HTMLDivElement>(null),drag=useRef<Obj|null>(null),lastSelected=useRef(sourceKey(selected||{}))
 const loc=current?.locator||{},did=loc.document_id,isSQL=current?.type==='sql_result',isDefinition=current?.type==='definition'
 const key=sourceKey(current||{}),fmt=meta?.document?.format||docs.find((d:Obj)=>d.id===did)?.format
 const visibleClaims:Obj[]=result?.claims||[];const activeClaim=visibleClaims.find(c=>c.id===claimId)
 const related:Obj[]=activeClaim?evidence.filter(e=>activeClaim.evidence_ids?.includes(e.id)):current?[current]:[]
 const isPdf=fmt==='pdf',isImage=['png','jpg','jpeg','webp'].includes(fmt)
 const maxPage=meta?.page_count||1
 const queryString=(p:Obj=loc)=>'?version='+encodeURIComponent(p.sha256||'')+(p.parse_id?'&parse_id='+encodeURIComponent(p.parse_id):'')
 const choose=(e:Obj)=>{setCurrent(e);onSelect(e);setPage(e.locator?.page||1);setFocus(null);setSelection(null);setQuestion('');setView('reader');setQuery('');setHits([]);setPageError('')}
 useEffect(()=>{if(!current&&first)choose(first)},[current,first?.id])
 useEffect(()=>{if(selected&&sourceKey(selected)!==lastSelected.current){lastSelected.current=sourceKey(selected);choose(selected)}},[sourceKey(selected||{})])
 useEffect(()=>{onState({target:current,page,zoom,view,focus})},[key,page,zoom,view,JSON.stringify(focus)])
 useEffect(()=>{setPageDraft(String(page));setPageError('');setSelection(null)},[page])
 useEffect(()=>{
   setError('');setMeta(null);setData(null);setBound(null);setSelection(null)
   if(!did||isSQL||isDefinition){setLoading(false);return}
   let live=true;setLoading(true)
   Promise.all([api('/api/documents/'+did+'/view'+queryString()),current?.snippet||loc.text_range?api('/api/evidence/resolve',{locator:loc,quote:current?.snippet||''}):Promise.resolve(null)])
   .then(([m,b])=>{if(live){setMeta(m);setBound(b);if(b?.status==='located')setFocus({page:b.locator.page,...b.locator.text_range,rects:b.locator.rects,evidenceId:current?.id});setPage((n:number)=>Math.max(1,Math.min(n,m.page_count)))}})
   .catch(e=>live&&setError(e.message)).finally(()=>live&&setLoading(false));return()=>{live=false}
 },[key,reload])
 useEffect(()=>{if(!meta||!did)return;let live=true;setLoading(true);setData(null)
   api('/api/documents/'+did+'/pages/'+page+queryString({sha256:meta.document.sha256,parse_id:meta.parse_id})).then(d=>{if(live){setData(d);reader.current?.scrollTo(0,0)}}).catch(e=>live&&setError(e.message)).finally(()=>live&&setLoading(false));return()=>{live=false}
 },[meta?.parse_id,page])
 useEffect(()=>{const relevant=visibleClaims.filter(c=>c.evidence_ids?.includes(current?.id));setClaimId(old=>relevant.some(c=>c.id===old)?old:relevant[0]?.id||'')},[key,result?.run_id])
 useEffect(()=>{setVerification(null);setVerifyError('');if(!claimId||!result?.run_id)return;let live=true;api('/api/evidence/verify',{run_id:result.run_id,claim_id:claimId}).then(r=>live&&setVerification(r)).catch(e=>live&&setVerifyError(e.message));return()=>{live=false}},[claimId,result?.run_id,reload])
 useEffect(()=>{if(!term.trim()){setSearchDocs([]);return}let live=true;const timer=setTimeout(()=>{api('/api/documents/search?q='+encodeURIComponent(term)).then(r=>live&&setSearchDocs(r.items)).catch(e=>live&&notify(e.message))},250);return()=>{live=false;clearTimeout(timer)}},[term])
 useEffect(()=>{if(!query.trim()||!meta){setHits([]);setSearchBusy(false);return}let live=true;setSearchBusy(true);const timer=setTimeout(()=>{api('/api/documents/'+did+'/search'+queryString({sha256:meta.document.sha256,parse_id:meta.parse_id})+'&q='+encodeURIComponent(query)).then(r=>{if(live){setHits(r.matches);setHit(0);if(r.matches[0]){setPage(r.matches[0].page);setFocus({...r.matches[0],kind:'search'})}}}).catch(e=>live&&notify(e.message)).finally(()=>live&&setSearchBusy(false))},220);return()=>{live=false;clearTimeout(timer)}},[query,meta?.parse_id])
 useEffect(()=>{companionActivity('document',{page:'evidence',status:loading?'running':error?'failed':'completed',stage:loading?'读取原文':'阅读原文',evidence_id:current?.id});return()=>companionActivity('document',null)},[loading,error,current?.id])
 const groups=useMemo(()=>{
   const map=new Map<string,Obj>();docs.forEach((d:Obj)=>map.set(d.id,{...d,evidence:[],target:{id:d.id,type:'document',source:d.name,locator:{document_id:d.id,sha256:d.sha256,page:1,source_url:d.source_url}}}))
   evidence.filter(e=>e.locator?.document_id).forEach(e=>{const d=e.locator.document_id;const g=map.get(d)||{id:d,name:e.source,format:e.source.split('.').at(-1),evidence:[]};g.evidence.push(e);g.target=g.evidence[0];map.set(d,g)})
   if(current?.locator?.document_id&&!map.has(did))map.set(did,{id:did,name:current.source,format:fmt,evidence:[current],target:current})
   return [...map.values()]
 },[docs,result?.run_id,key])
 useEffect(()=>{const list=document.querySelector<HTMLElement>('.source-files'),item=list?.querySelector<HTMLElement>('.evidence-entry.selected');if(list&&item){const a=item.getBoundingClientRect(),b=list.getBoundingClientRect();if(a.top<b.top||a.bottom>b.bottom)list.scrollTop+=a.top-b.top-Math.max(0,(b.height-a.height)/2)}},[did,tab,filter,view,groups.length])
 const filtered=groups.filter(g=>(filter==='all'||(filter==='image'?['png','jpg','jpeg','webp'].includes(g.format):filter==='text'?['md','txt'].includes(g.format):g.format===filter))&&(!term||g.name.toLowerCase().includes(term.toLowerCase())||searchDocs.some(d=>d.document_id===g.id)))
 const ranges:Obj[]=[]
 if(bound?.status==='located'&&bound.locator.page===page)ranges.push({...bound.locator.text_range,rects:bound.locator.rects,evidenceId:current?.id,kind:'source'})
 if(focus&&focus.page===page&&!ranges.some(r=>r.start===focus.start&&r.end===focus.end))ranges.push({...focus,kind:focus.kind||'source'})
 if(query&&hits[hit]?.page===page&&!ranges.some(r=>r.start===hits[hit].start&&r.end===hits[hit].end))ranges.push({...hits[hit],kind:'search'})
 useEffect(()=>{if(!data||!focus||focus.page!==page||!reader.current)return;const segment=surface.current?.querySelector('[data-start="'+focus.start+'"]')||surface.current?.querySelector('.source-mark,.search-mark');if(segment){const a=segment.getBoundingClientRect(),b=reader.current.getBoundingClientRect();reader.current.scrollTo({top:reader.current.scrollTop+a.top-b.top-100,behavior:'instant'})}else if(focus?.rects?.[0]&&surface.current){reader.current.scrollTo({top:focus.rects[0][1]*surface.current.clientHeight-80,behavior:'instant'})}},[data?.parse_id,data?.number,JSON.stringify(focus),zoom])
 const selectSource=(r:Obj)=>{const e=evidence.find(e=>e.id===r.evidenceId);if(e){const c=visibleClaims.find(c=>c.evidence_ids?.includes(e.id));if(c)setClaimId(c.id);notify('已显示这处引用对应的结论')}}
 const selectText=()=>{if(mode!=='select'||!data)return;const sel=window.getSelection();if(!sel||sel.isCollapsed||!sel.rangeCount)return;const range=sel.getRangeAt(0)
   const offset=(node:Node,n:number)=>{const el=(node.nodeType===Node.ELEMENT_NODE?node as Element:node.parentElement)?.closest('[data-start]');if(!el||!surface.current?.contains(el))return null;const r=document.createRange();r.selectNodeContents(el);try{r.setEnd(node,n)}catch{return null}return Number(el.getAttribute('data-start'))+Array.from(r.toString()).length}
   const a=offset(range.startContainer,range.startOffset),b=offset(range.endContainer,range.endOffset);if(a!==null&&b!==null&&b>a){const text=cp(data.text,a,b);setSelection({start:a,end:b,text,page,parse_id:meta?.parse_id})}
 }
 const moveHit=(n:number)=>{if(!hits.length)return;const idx=(n+hits.length)%hits.length;setHit(idx);setPage(hits[idx].page);setFocus({...hits[idx],kind:'search'})}
 const jumpPage=()=>{const n=Number(pageDraft);if(!/^\d+$/.test(pageDraft)||!Number.isInteger(n)||n<1||n>maxPage){setPageError('页码应为 1 至 '+maxPage);return}setPage(n);setPageError('');setFocus(null)}
 const outline=meta?.outline?.length?meta.outline:Array.from({length:maxPage},(_,i)=>({title:'第 '+(i+1)+' 页',level:1,page:i+1}))
 const sectionJump=(h:Obj)=>{setPage(h.page);setView('reader');setFocus(h.start!==undefined?{page:h.page,start:h.start,end:h.end}:null)}
 const sourceUrl=meta?.document?.source_url||loc.source_url

 const readerTools:MenuAction[]=[
  {id:'mode',label:mode==='pan'?'切换到文字选择':'切换到拖动阅读',icon:mode==='pan'?MousePointer2:Hand,detail:mode==='pan'?'当前可拖动画布':'当前可选取文字并提问',onSelect:()=>setMode(mode==='pan'?'select':'pan')},
  ...(isPdf?[{id:'text',label:textLayer?'隐藏文字选择层':'显示文字选择层',icon:TextCursor,disabled:!data?.text_available,reason:'当前页没有可提取文字',onSelect:()=>setTextLayer(!textLayer)},{id:'image',label:imageLayer?'隐藏图像区域':'显示图像区域',icon:ImageIcon,disabled:!data?.images?.length,reason:'当前页没有可标记图像',onSelect:()=>setImageLayer(!imageLayer)}]:[])
 ]
 const readerPanel=<section id="workbench-reader" className="document-view panel source-reader" role={compact?'tabpanel':undefined} aria-labelledby={compact?'workbench-tab-reader':undefined}>
   <div className="document-toolbar source-toolbar">
    {!isSQL&&!isDefinition&&current?<><div className="page-switch"><IconButton label="上一页原文" disabled={page<=1||loading} onClick={()=>{setPage(page-1);setFocus(null)}}><ChevronLeft size={16}/></IconButton><form onSubmit={e=>{e.preventDefault();jumpPage()}}><input aria-label="原文页码" inputMode="numeric" value={pageDraft} onChange={e=>setPageDraft(e.target.value)} onBlur={jumpPage}/></form><span>/ {maxPage}</span><IconButton label="下一页原文" disabled={page>=maxPage||loading} onClick={()=>{setPage(page+1);setFocus(null)}}><ChevronRight size={16}/></IconButton></div>
    <select aria-label="原文缩放" value={zoom} onChange={e=>setZoom(e.target.value)}><option value="fit">适应宽度</option>{[75,100,125,150,200].map(n=><option key={n} value={n}>{n}%</option>)}</select>

    <IconButton label="搜索原文" aria-pressed={searchOpen} onClick={()=>setSearchOpen(!searchOpen)}><Search size={16}/></IconButton></>:<span className="truncate">{current?.source||'原文'}</span>}
    <IconButton label={full?'退出全屏阅读':'全屏阅读'} onClick={()=>setFull(!full)}>{full?<X size={16}/>:<Maximize2 size={16}/>}</IconButton>
    {!isSQL&&!isDefinition&&current&&<ActionMenu label="阅读工具" iconOnly items={readerTools} footer={sourceUrl?<a role="menuitem" target="_blank" rel="noreferrer" href={sourceUrl}><ExternalLink size={16}/>打开同版本原文件</a>:undefined}/>}
    {(isSQL||isDefinition)&&sourceUrl&&<a className="icon-button" title="打开同版本原文件" aria-label="打开同版本原文件" target="_blank" rel="noreferrer" href={sourceUrl}><ExternalLink size={16}/></a>}
   </div>
   {pageError&&<div className="reader-warning" role="alert">{pageError}</div>}
   {searchOpen&&!isSQL&&<div className="reader-search"><Search size={15}/><input aria-label="搜索原文内容" placeholder="在这份文档中查找" value={query} onChange={e=>setQuery(e.target.value)} maxLength={200} onKeyDown={e=>{if(e.key==='Enter')moveHit(hit+(e.shiftKey?-1:1))}}/><span>{searchBusy?'查找中':hits.length?(hit+1)+' / '+hits.length:query?'无匹配':'0 / 0'}</span><IconButton label="上个命中" disabled={!hits.length} onClick={()=>moveHit(hit-1)}><ChevronLeft size={15}/></IconButton><IconButton label="下个命中" disabled={!hits.length} onClick={()=>moveHit(hit+1)}><ChevronRight size={15}/></IconButton><IconButton label="关闭原文搜索" onClick={()=>{setSearchOpen(false);setQuery('');setFocus(null)}}><X size={15}/></IconButton></div>}
   {meta&&<div className="reader-document-meta"><span>{meta.document.name}</span><small>{meta.document.is_current?'当前版本':'历史版本'} · {meta.document.sha256.slice(0,10)}</small>{!meta.document.is_current&&<button className="text-link" onClick={()=>choose({id:did,type:'document',source:meta.document.name,locator:{document_id:did,sha256:meta.document.current_sha256,page:1}})}>查看新版</button>}</div>}
   <div className={'document-page source-page '+(mode==='pan'?'pan-mode':'')} ref={reader} onMouseUp={selectText} onPointerDown={e=>{if(mode==='pan'&&e.button===0&&reader.current){e.preventDefault();drag.current={x:e.clientX,y:e.clientY,left:reader.current.scrollLeft,top:reader.current.scrollTop};e.currentTarget.setPointerCapture(e.pointerId)}}} onPointerMove={e=>{if(drag.current&&reader.current){reader.current.scrollLeft=drag.current.left+drag.current.x-e.clientX;reader.current.scrollTop=drag.current.top+drag.current.y-e.clientY}}} onPointerUp={()=>drag.current=null} onPointerCancel={()=>drag.current=null}>
   {!current?<Empty title="选择一份文档"/>:isSQL?<div className="sql-reader"><h2>查询与返回结果</h2><span className="badge green">已执行 · 只读查询</span><pre className="sql-code">{loc.sql}</pre><h3>参数</h3><code>{JSON.stringify(loc.params)}</code><h3>返回行 · {loc.row_count}</h3><DataTable rows={loc.rows||[]} columns={loc.columns} limit={15}/><p className="muted">数据版本 {loc.version}</p></div>:isDefinition?<article className="text-document"><h3>{current.source}</h3><pre>{current.snippet}</pre><p>用户确认的规则定义</p></article>:error?<div className="reader-error" role="alert"><p>{error}</p><button className="button" onClick={()=>setReload(reload+1)}>重新读取</button></div>:loading?<Spinner text="读取原文"/>:data&&<>
    {!data.text_available&&<p className="reader-warning">{data.ocr_required?'此页没有可提取文字，待文字识别；尚未做视觉复核。':'原文没有文字。'}</p>}
    <div ref={surface} className={'source-page-surface '+(isPdf?'pdf-surface':isImage?'image-surface':'text-surface')} style={{width:zoom==='fit'?'100%':data.width*Number(zoom)/100,maxWidth:'none',aspectRatio:isPdf||isImage?data.width+'/'+data.height:undefined}} onClick={e=>{const el=(e.target as Element).closest('[data-start]');if(el&&!window.getSelection()?.toString()){const n=Number(el.getAttribute('data-start'));const r=ranges.find(r=>r.start<=n&&n<r.end);if(r)selectSource(r)}}}>
     {isPdf||isImage?<><img className="page-raster" src={isPdf?'/api/documents/'+did+'/pages/'+page+'/image?version='+meta!.document.sha256:sourceUrl} alt={current.source+' · 第 '+page+' 页原件'} draggable={false} onError={()=>setError('原页图像读取失败，请重试')}/>
       {ranges.flatMap((r,i)=>(r.rects||[]).map((box:number[],n:number)=><span key={i+'-'+n} className={'page-highlight '+(r.kind==='search'?'secondary':'')} style={position(box)} aria-hidden="true"/>))}
       {imageLayer&&data.images.map((img:Obj,i:number)=><span key={i} className="image-region" style={position(img.box)} title="原件图像区域 · 未做视觉识别"/>)}
       {isPdf&&textLayer&&data.text_available&&<div className="pdf-text-layer" aria-label="PDF 原文文字层">{data.blocks.flatMap((b:Obj)=>b.chars.map((c:Obj,i:number)=><span key={b.id+'-'+i} data-start={c.start} data-end={c.end} style={{...position(c.box),fontSize:Math.max(1,(c.box[3]-c.box[1])*data.height)+'px'}}>{c.text}</span>))}</div>}
     </>:<article className="located-text" style={{fontSize:zoom==='fit'?16:16*Number(zoom)/100}}>{data.blocks.map((b:Obj)=><MarkedText key={b.id} block={b} ranges={ranges} onSource={selectSource}/>)}</article>}
    </div>
   </>}
   </div>
   {!isSQL&&!isDefinition&&meta&&<div className="document-question">{selection&&<div className="selection-chip"><span>已选 {Array.from(selection.text).length} 字：{selection.text.slice(0,40)}</span><IconButton label="清除选中片段" onClick={()=>{setSelection(null);window.getSelection()?.removeAllRanges()}}><X size={14}/></IconButton></div>}<QueryInput label="询问当前文档" value={question} onChange={setQuestion} placeholder={selection?'询问这段原文…':'询问当前文档第 '+page+' 页…'} busy={busy||loading} onSend={()=>{const scope={document_id:did,sha256:meta.document.sha256,parse_id:meta.parse_id,page,...(selection?{text_range:{start:selection.start,end:selection.end},quote:selection.text}:{})};void onAsk(question,scope)}}/></div>}
 </section>

 return <div className="evidence-page page-enter stage6-workbench"><div className="page-heading"><h1>证据溯源 <span>/ 文档核验</span></h1><div className="source-progress" aria-label="当前来源状态"><span className={meta||isSQL?'done':''}>原件{meta||isSQL?'已读取':'待选择'}</span><ChevronRight size={14}/><span className={bound?.status==='located'?'done':''}>{bound?.status==='located'?'文字已定位':'定位待确认'}</span><ChevronRight size={14}/><span>{verification?'核验详情':'尚未核验'}</span></div><div className="inline-actions">{(result||meta&&['pdf','png','jpg','jpeg','webp'].includes(meta.document.format))&&<ActionMenu label="文档更多操作" iconOnly items={[...(result?[{id:'share',label:'分享结果',onSelect:onShare}]:[]),...(meta&&['pdf','png','jpg','jpeg','webp'].includes(meta.document.format)?[{id:'vision',label:'视觉复核',icon:ImageIcon,onSelect:()=>onVision({document_id:did,sha256:meta.document.sha256,page})}]:[])]}/>}<button className="button" onClick={()=>navigate('analysis')}><MessageCircle size={16}/>返回问答</button></div></div>
 <div className="workbench-tabs" role="tablist" aria-label="文档工作台视图">{['files','reader','details'].map((id,i)=><button key={id} role="tab" id={'workbench-tab-'+id} aria-controls={'workbench-'+id} tabIndex={view===id?0:-1} aria-selected={view===id} onKeyDown={e=>{const ids=['files','reader','details'];const n=e.key==='ArrowRight'?(i+1)%3:e.key==='ArrowLeft'?(i+2)%3:e.key==='Home'?0:e.key==='End'?2:-1;if(n>=0){e.preventDefault();setView(ids[n]);document.getElementById('workbench-tab-'+ids[n])?.focus()}}} onClick={()=>setView(id)}>{['文档','原文','核验'][i]}</button>)}</div>
 <div className={'evidence-layout view-'+view}><aside id="workbench-files" className="evidence-catalog" role={compact?'tabpanel':undefined} aria-labelledby={compact?'workbench-tab-files':undefined}>
  <section className="panel evidence-list"><div className="source-catalog-tabs"><button aria-pressed={tab==='files'} onClick={()=>setTab('files')}>文档 <span>{groups.length}</span></button><button aria-pressed={tab==='claims'} onClick={()=>setTab('claims')}><ListTree size={15}/>证据树</button></div>
   {tab==='files'?<><label className="search-field"><Search size={16}/><input aria-label="搜索文档或内容" placeholder="搜索文件或内容" value={term} onChange={e=>setTerm(e.target.value)} maxLength={200}/></label><div className="source-filters">{[['all','全部'],['pdf','PDF'],['text','文字'],['image','图片']].map(([v,label])=><button key={v} aria-pressed={filter===v} onClick={()=>setFilter(v)}>{label}</button>)}</div><div className="source-files">{filtered.map(g=><button key={g.id} className={'evidence-entry '+(g.id===did?'selected':'')} onClick={()=>choose(g.target)}><span className={'file-format file-'+g.format}><FileText size={22}/><small>{g.format?.toUpperCase()}</small></span><span><strong>{g.name}</strong><small>{g.pages||'—'} 页 · {g.evidence.length} 处引用</small><small>{g.status==='failed'?'解析失败':g.status==='needs_review'?'待识别或检查':g.sha256?.slice(0,10)||'历史版本'}</small></span></button>)}{!filtered.length&&<Empty title="没有匹配文档"/>}</div>{evidence.some(e=>e.type==='sql_result')&&<details className="query-sources"><summary>表格查询 <span>{evidence.filter(e=>e.type==='sql_result').length}</span></summary>{evidence.filter(e=>e.type==='sql_result').map(e=><button key={e.id} className="text-link" onClick={()=>choose(e)}><Database size={15}/>{e.source}</button>)}</details>}</>:<div className="claim-tree">{visibleClaims.length?visibleClaims.map((c,i)=><details key={c.id} open={c.id===claimId}><summary><span>{i+1}</span>{c.text}</summary>{evidence.filter(e=>c.evidence_ids?.includes(e.id)).map(e=><button key={e.id} onClick={()=>{choose(e);setClaimId(c.id)}}><FileText size={14}/>{e.source}</button>)}{!c.evidence_ids?.length&&<p className="muted">没有匹配来源</p>}</details>):<Empty title="暂无分析结论"/>}</div>}
  </section>
  <section className="panel document-tree"><h3 className="tree-heading">章节结构 <small>{meta?.structure}</small></h3>{meta?<Outline items={outline} onSelect={sectionJump}/>:<p className="muted">{isSQL?'当前为表格查询':'选择文档后显示章节'}</p>}</section>
 </aside>
 {full?<div className="reader-placeholder"/>:readerPanel}
 <aside id="workbench-details" className="evidence-detail" role={compact?'tabpanel':undefined} aria-labelledby={compact?'workbench-tab-details':undefined}>
  <section className="panel claim-panel"><div className="section-title"><h3><CheckCircle2 size={18}/>结论</h3>{activeClaim&&<span className={'badge '+(activeClaim.evidence_ids?.length?'green':'amber')}>{activeClaim.evidence_ids?.length||0} 处来源</span>}</div>{visibleClaims.length>1&&<select aria-label="选择核验结论" value={claimId} onChange={e=>{const c=visibleClaims.find(c=>c.id===e.target.value);const src=evidence.find(e=>e.id===c?.evidence_ids?.find((id:string)=>evidence.find(x=>x.id===id)?.type==='document'))||evidence.find(e=>c?.evidence_ids?.includes(e.id));if(src)choose(src);setClaimId(e.target.value)}}><option value="">查看原文</option>{visibleClaims.map((c,i)=><option key={c.id} value={c.id}>结论 {i+1} · {c.text.slice(0,28)}</option>)}</select>}{activeClaim?<p className="bound-claim">{activeClaim.text}</p>:<p className="muted">当前仅查看原文。</p>}</section>
  <section className="panel source-location-panel"><div className="section-title"><h3>来源定位</h3><span className="badge">{related.length}</span></div>{related.map((e,i)=><button key={e.id} className={'location-row '+(e.id===current?.id?'selected':'')} onClick={()=>choose(e)}><span className="citation-number">{i+1}</span><span><strong>{e.source}</strong><small>{e.locator?.document_id?'第 '+e.locator.page+' 页 · '+e.locator.sha256?.slice(0,10):'查询返回快照'}</small><p>{e.snippet?.slice(0,78)}</p></span><MapPin size={17}/></button>)}
   {did&&<><div className="source-location-state"><span className={'badge '+(bound?.status==='located'?'green':'')}>{bound?.status==='located'?'文字范围已定位':bound?.status==='ambiguous'?'多处相同文字，需确认':bound?.status==='unlocated'?'未匹配到引用文字':'原文浏览'}</span><small>未做视觉复核</small></div>{meta&&<><div className="key-value"><span>文件大小</span><strong>{size(meta.document.size_bytes)}</strong></div><details className="hash-detail"><summary>版本与摘要</summary><p>{meta.document.is_current?'当前版本':'历史版本，新版本已存在'}</p><code>{meta.document.sha256}</code><p>解析版本 {meta.parse_id}</p><select aria-label="选择文档版本" value={meta.document.sha256} onChange={e=>choose({id:did,type:'document',source:meta.document.name,locator:{document_id:did,sha256:e.target.value,page:1}})}>{meta.versions.map((v:Obj)=><option key={v.sha256} value={v.sha256}>{v.sha256.slice(0,10)}{v.sha256===meta.document.current_sha256?' · 当前':''}</option>)}</select></details></>}</>}
  </section>
  {activeClaim&&<section className="panel claim-checks"><h3><ShieldCheck size={18}/>当前结论核验</h3>{verifyError?<><p className="error-text">{verifyError}</p><button className="text-link" onClick={()=>setReload(reload+1)}>重新核验</button></>:!verification?<Spinner text="核对引用"/>:verification.checks.map((c:Obj)=><details key={c.name}><summary><strong>{c.name}</strong><span className={'badge '+(c.status==='passed'?'green':c.status==='needs_review'?'amber':'')}>{c.status==='passed'?'通过':c.status==='needs_review'?'需检查':'未核验'}</span></summary><p>{c.detail}</p></details>)}</section>}
  <section className="panel source-process"><div className="section-title"><h3>过程</h3>{result?.events?.length>0&&<button className="text-link" onClick={()=>setProcess(true)}>查看详情</button>}</div>{result?.events?.slice(-4).map((event:Obj)=><div key={event.step_id}><CheckCircle2 size={14}/><span>{event.label}</span><small>{event.duration_ms} ms</small></div>)}{!result?.events?.length&&<p className="muted">当前未执行问答</p>}{did&&meta&&<button className="button full" disabled={reparseBusy||!data?.text_available||loading} title={!data?.text_available?'没有可提取文字，未连接 OCR':undefined} onClick={async()=>{setReparseBusy(true);try{setReparse(await api('/api/documents/'+did+'/reparse',{page,version:meta.document.sha256,parse_id:meta.parse_id}))}catch(e){notify((e as Error).message)}finally{setReparseBusy(false)}}}><RefreshCw size={15} className={reparseBusy?'spin':''}/>{reparseBusy?'重读中':'重读当前页'}</button>}</section>
 </aside></div>
 {full&&<Modal title="原文阅读" close={()=>setFull(false)} className="reader-fullscreen">{readerPanel}</Modal>}
 {process&&<Modal title="本次查询过程" close={()=>setProcess(false)}><Process result={result}/></Modal>}
 {reparse&&<Modal title="当前页重读结果" close={()=>setReparse(null)}><p>{reparse.changed?'提取文字有变化':'提取文字没有变化'} · {reparse.duration_ms} ms</p><p className="muted">{reparse.method}</p><details><summary>对比提取内容</summary><h4>重读前</h4><pre className="reparse-text">{reparse.before}</pre><h4>重读后</h4><pre className="reparse-text">{reparse.after}</pre></details><p className="muted">原引用仍使用之前的解析版本。</p><button className="button primary" onClick={()=>{choose({...current,locator:{...loc,parse_id:reparse.after_parse_id,page:reparse.page}});setReparse(null)}}>查看重读版本</button></Modal>}
 </div>
}
