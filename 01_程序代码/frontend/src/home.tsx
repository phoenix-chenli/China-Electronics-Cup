import React,{useEffect,useRef,useState} from 'react'
import {ArrowUpRight,BookOpen,ChartNoAxesColumnIncreasing,ChevronDown,Database,FileText,GitBranch,History,MessageCircle,Plus,ScanSearch,Search,Settings2,ShieldCheck} from 'lucide-react'
import {Chart,examples,Modal,Obj,QueryInput,timeLabel} from './ui'
import {ToolShelf} from './controls'
import {PetBlob} from './pet-blob'

const names:Obj={queued:'等待中',running:'查询中',cancelling:'取消中',completed:'已完成',failed:'未完成',cancelled:'已取消'}
const capabilities=[
 {icon:Database,title:'精准问数',text:'用大白话查询数据库，自动生成并校验 SQL，多表关联也不怕。',tint:'lime'},
 {icon:BookOpen,title:'知识问答',text:'从 PDF 与文档里找答案，逐句挂接原文与页码，拒绝编造。',tint:'sky'},
 {icon:GitBranch,title:'跨源推理',text:'先查数、再搜文档、最后综合，复杂问题自动拆解执行。',tint:'peach'},
 {icon:ShieldCheck,title:'全程可解释',text:'要素识别、SQL、检索片段、执行链路全部展示，可回溯复算。',tint:'lime'}
]
/** 示例问题打字机：逐字打出、停留、删除、切换下一条。 */
function useTypewriter(lines:string[],active:boolean){
 const [text,setText]=useState('')
 const state=useRef({line:0,char:0,phase:'typing' as 'typing'|'holding'|'deleting'})
 useEffect(()=>{
  if(!active){setText('');return}
  let timer=0
  const tick=()=>{
   const s=state.current,line=lines[s.line%lines.length]
   if(s.phase==='typing'){
    s.char++
    setText(line.slice(0,s.char))
    if(s.char>=line.length){s.phase='holding';timer=window.setTimeout(tick,2400);return}
    timer=window.setTimeout(tick,55+Math.random()*70)
   }else if(s.phase==='holding'){
    s.phase='deleting';timer=window.setTimeout(tick,320)
   }else{
    s.char-=2
    if(s.char<=0){s.char=0;s.line++;s.phase='typing';setText('');timer=window.setTimeout(tick,420);return}
    setText(line.slice(0,s.char))
    timer=window.setTimeout(tick,24)
   }
  }
  timer=window.setTimeout(tick,500)
  return()=>clearTimeout(timer)
 },[active,lines])
 return text
}

export function HomePage({tables,docs,dataReady,dashboard,dashboardError,reloadDashboard,question,setQuestion,run,busy,importFile,uploading,project,sessions,onSession,tools,onImport,onCatalog,presented=true}:Obj){
 const [allSessions,setAllSessions]=useState(false),[filter,setFilter]=useState(''),[drag,setDrag]=useState(false),[focused,setFocused]=useState(false)
 const typed=useTypewriter(examples,presented&&!question)
 const ready=docs.filter((d:Obj)=>d.status==='ready'),pending=docs.length-ready.length
 const row=(s:Obj)=><button className="resume-row" key={s.id} onClick={()=>{setAllSessions(false);onSession(s.id)}}><span className="resume-icon"><MessageCircle size={17}/></span><span className="row-grow"><strong>{s.title}</strong><small>{s.turn_count} 轮对话 · {timeLabel(s.updated_at)}</small></span><span className={'resume-state '+(['queued','running'].includes(s.task_status)?'working':'')}>{names[s.task_status]||(s.turn_count?'继续':'新会话')}</span><ArrowUpRight size={16}/></button>
 return <div className={'home-page'+(drag?' drop-active':'')} onDragOver={e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();setDrag(true)}}} onDragLeave={e=>{if(!e.currentTarget.contains(e.relatedTarget as Node))setDrag(false)}} onDrop={e=>{e.preventDefault();setDrag(false);if(!uploading&&e.dataTransfer.files[0]){onImport();importFile(e.dataTransfer.files[0])}}}>
  {drag&&<div className="home-drop-cue"><Plus size={34}/><strong>松开以导入资料</strong><span>添加到本机资料库</span></div>}

  <section className="home-hero rise" aria-label="开始分析">
   <div className="home-hero-pet"><PetBlob pose={busy?'think':focused?'listen':'idle'} size={148}/></div>
   <div className="home-hero-copy">
    <div className="home-hero-kicker"><ScanSearch size={14}/>多模态数据驱动 · 可解释精准问数问答</div>
    <h1>每个答案，<br/>都有据可循<span className="home-period">。</span></h1>
    <p>问数据库，也问文档。结论标注来源，过程可以复算。</p>
   </div>
  </section>

  <section className={'home-ask rise rise-1 '+(question.trim()?'has-draft':'')} aria-label="提问" onFocusCapture={()=>setFocused(true)} onBlurCapture={e=>{if(!e.currentTarget.contains(e.relatedTarget as Node))setFocused(false)}}>
   <div className="home-typewriter" aria-hidden="true">{!question&&<><span>{typed}</span><i className="typewriter-caret"/></>}</div>
   <QueryInput value={question} onChange={setQuestion} onSend={()=>run(question)} busy={busy} large placeholder="输入你的问题，例如：2024 年华东净销售额是多少？" actions={<ToolShelf items={tools}/>}/>
   <div className="home-ask-foot"><span>Enter 发送 · Shift + Enter 换行</span><span>支持拖拽文件到本页导入</span></div>
  </section>

  <section className="home-capabilities rise rise-2" aria-label="核心能力">
   {capabilities.map((c,i)=><div key={c.title} className={'home-capability tint-'+c.tint}>
    <span className="home-capability-icon"><c.icon size={19}/></span>
    <strong>{c.title}</strong><p>{c.text}</p>
   </div>)}
  </section>

  {dashboardError&&<div className="error-banner" role="alert"><span>工作区概览暂未读取：{dashboardError}</span><button className="text-link" onClick={reloadDashboard}>重试</button></div>}

  <div className="home-lower rise rise-3">
   <section className="continue-work panel">
    <div className="section-title"><h2>接着上次的思路</h2>{sessions.length>0&&<button className="text-link" onClick={()=>setAllSessions(true)}>全部分析<ArrowUpRight size={14}/></button>}</div>
    {sessions.length?sessions.slice(0,3).map(row):<div className="quiet-empty"><History size={24}/><div><strong>留住每一次发现</strong><p>开始提问后，分析会自动保存在这里。</p></div></div>}
   </section>
   <aside className="home-library panel">
    <div className="section-title"><h2>答案的起点</h2><ShieldCheck size={18}/></div>
    <button className="library-signal" onClick={()=>onCatalog('tables')}><Database size={18}/><span>数据表</span><strong>{dataReady?tables.length:'—'}</strong><ArrowUpRight size={14}/></button>
    <button className="library-signal" onClick={()=>onCatalog('docs')}><FileText size={18}/><span>已解析文档{pending>0&&<small>{pending} 份待处理</small>}</span><strong>{dataReady?ready.length:'—'}</strong><ArrowUpRight size={14}/></button>
    <div className="library-foot"><span>{project?.name||'当前项目'}</span><span>今日提问 <b>{dashboard?.today_count??'—'}</b></span></div>
   </aside>
  </div>

  <details className="home-overview"><summary><span>工作区概览 <small>销售汇总与提问趋势</small></span><ChevronDown size={15}/></summary><div className="overview-charts"><div><h3>净销售额</h3><p>共享示例数据 · 按年汇总 · 元</p><Chart labels={dashboard?.sales?.map((s:Obj)=>s.year)||[]} values={dashboard?.sales?.map((s:Obj)=>s.amount)||[]} height={180} label="净销售额" unit="元"/></div><div><h3>提问趋势</h3><p>近 30 天 · 当前项目</p><Chart kind="line" labels={dashboard?.trend?.map((s:Obj)=>s.day.slice(5))||[]} values={dashboard?.trend?.map((s:Obj)=>s.count)||[]} height={180} label="提问次数" unit="次"/></div></div></details>

  {allSessions&&<Modal title="全部分析" close={()=>setAllSessions(false)}><label className="search-field"><Search size={16}/><input aria-label="搜索会话" placeholder="搜索会话" value={filter} onChange={e=>setFilter(e.target.value)}/></label><div className="home-session-list">{sessions.filter((s:Obj)=>s.title.includes(filter)).map(row)}{!sessions.some((s:Obj)=>s.title.includes(filter))&&<p className="muted">没有匹配的会话</p>}</div></Modal>}
 </div>
}
