import React, {useRef, useState} from 'react'
import {ArrowRight, ArrowUpRight, ChartNoAxesColumnIncreasing, ChevronDown, Database, FileText, History, MessageCircle, Plus, Search, ShieldCheck} from 'lucide-react'
import {Chart, Modal, Obj, QueryInput, timeLabel} from './ui'
import {ToolShelf} from './controls'
import {projectQueryProfile,QueryContextBar} from './agent'

const stateNames:Obj = {queued:'等待中', running:'查询中', cancelling:'取消中', completed:'已完成', failed:'未完成', cancelled:'已取消'}
const starters:Obj = {
 sales: [
  {label:'看一组数据', question:'2024 年华东净销售额是多少？', icon:ChartNoAxesColumnIncreasing},
  {label:'发现关键人物', question:'2024 年华东销售冠军是谁？', icon:Search},
  {label:'把结论说清楚', question:'2024 年华东销售冠军是谁？结合项目复盘说明方法和奖金。', icon:FileText}
 ],
 database: [
  {label:'了解数据结构', question:'当前数据库有哪些主要数据表？它们之间有什么关系？', icon:Database},
  {label:'检查数据质量', question:'当前数据库中有哪些字段存在空值？请列出数据来源。', icon:Search},
  {label:'找到分析入口', question:'根据当前数据库的实际字段，可以开展哪些统计分析？', icon:ChartNoAxesColumnIncreasing}
 ],
 knowledge: [
  {label:'梳理资料', question:'知识库中有哪些主要主题？请列出对应资料来源。', icon:FileText},
  {label:'理解规则', question:'现有文档中有哪些奖金计算规则？请标注适用条件和原文来源。', icon:Search},
  {label:'核对适用条件', question:'奖金规则适用于哪些人员和时间范围？资料缺失的部分请明确说明。', icon:ShieldCheck}
 ],
 hybrid: [
  {label:'连接数据与资料', question:'当前数据库与知识库中，有哪些能够相互核对的业务信息？', icon:Database},
  {label:'解释统计口径', question:'知识库如何定义净销售额？数据库中有哪些对应字段？', icon:Search},
  {label:'追溯计算依据', question:'2024 年华东销售冠军是谁？结合奖金规则说明计算依据和来源。', icon:FileText}
 ]
}

function EvidenceIllustration(){
 return <div className="evidence-illustration" aria-hidden="true">
  <div className="illustration-grid"/>
  <svg className="illustration-paths" viewBox="0 0 320 200" fill="none">
   <path d="M56 60H119Q136 60 136 77V89Q136 105 153 105H205"/>
   <path d="M64 157H112Q136 157 136 133V124Q136 105 155 105"/>
   <circle cx="136" cy="105" r="4"/>
  </svg>
  <div className="illustration-source source-table"><Database size={19}/><span>数据</span><i/></div>
  <div className="illustration-source source-document"><FileText size={19}/><span>文档</span><i/></div>
  <div className="illustration-answer"><span className="illustration-answer-label">有据可循</span><div className="illustration-answer-line"/><div className="illustration-answer-line short"/><span className="illustration-citation"><ShieldCheck size={13}/>原文与数据来源</span></div>
  <span className="illustration-caption">CONNECT THE EVIDENCE</span>
 </div>
}

export function HomePage({tables,docs,dataReady,dashboard,dashboardError,reloadDashboard,question,setQuestion,run,busy,importFile,uploading,project,sessions,onSession,tools,onImport,onCatalog,agentConfig,onAgentSettings,routePreview,scopeOverride,onScopeChange,previewLoading,previewError}:Obj){
 const [allSessions,setAllSessions] = useState(false)
 const [filter,setFilter] = useState('')
 const [drag,setDrag] = useState(false)
 const composer = useRef<HTMLElement>(null)
 const ready = docs.filter((d:Obj)=>d.status==='ready')
 const pending = docs.length-ready.length
 const profile=projectQueryProfile(agentConfig),projectQuestions=profile.starter_questions||agentConfig?.starter_questions
 const suggestions = Array.isArray(projectQuestions)&&projectQuestions.length?projectQuestions.slice(0,3).map((question:string,i:number)=>({label:['从一组数据开始','比较与发现','追溯统计依据'][i],question,icon:[ChartNoAxesColumnIncreasing,Search,FileText][i]})):agentConfig?(starters[agentConfig.query_scope||'sales']||starters.sales):[]
 const projectDocumentCount=Number.isInteger(profile.document_count)?profile.document_count:null
 const salesOverview=!!agentConfig&&profile.default_scope!=='auto'&&(profile.default_scope||agentConfig.query_scope||'sales')==='sales'
 const fillQuestion = (text:string)=>{setQuestion(text);requestAnimationFrame(()=>composer.current?.querySelector('textarea')?.focus())}
 const rows = (session:Obj)=><button className="resume-row" key={session.id} onClick={()=>{setAllSessions(false);onSession(session.id)}}>
  <span className="resume-icon"><MessageCircle size={17}/></span>
  <span className="row-grow"><strong>{session.title}</strong><small>{session.turn_count} 轮对话<span>·</span>{timeLabel(session.updated_at)}</small></span>
  <span className={'resume-state '+(['queued','running','cancelling'].includes(session.task_status)?'working':'')}>{stateNames[session.task_status]||(session.turn_count?'继续分析':'新会话')}</span>
  <ArrowUpRight size={16}/>
 </button>
 return <div className={'home-page'+(drag?' drop-active':'')}
  onDragOver={event=>{if(event.dataTransfer.types.includes('Files')){event.preventDefault();setDrag(true)}}}
  onDragLeave={event=>{if(!event.currentTarget.contains(event.relatedTarget as Node))setDrag(false)}}
  onDrop={event=>{event.preventDefault();setDrag(false);if(!uploading&&event.dataTransfer.files[0]){onImport();importFile(event.dataTransfer.files[0])}}}>
  {drag&&<div className="home-drop-cue"><Plus size={34}/><strong>松开以导入资料</strong><span>添加到本机资料库</span></div>}

  <section className="home-hero rise" aria-label="开始分析">
   <div className="home-hero-copy">
    <div className="home-eyebrow"><span/>EVIDENCEFLOW<span className="eyebrow-separator">/</span>从问题到证据</div>
    <h1>让每一次提问，<br/><em>都有据可循。</em></h1>
    <p>连接数据与文档，让结论、过程和来源一起呈现。</p>
   </div>
   <EvidenceIllustration/>
  </section>

  <section ref={composer} className={'home-ask rise rise-1 '+(question.trim()?'has-draft':'')} aria-label="提问">
   <div className="home-composer">
    <QueryContextBar config={agentConfig||null} onSettings={onAgentSettings||(()=>{})} routePreview={routePreview} scopeOverride={scopeOverride} onScopeChange={onScopeChange} previewLoading={previewLoading} previewError={previewError} busy={busy}/>
    <QueryInput value={question} onChange={setQuestion} onSend={()=>run(question)} busy={busy} large submitLabel="开始分析"
     placeholder="今天，想从数据中了解什么？" actions={<ToolShelf items={tools}/>}/>
   </div>
   <div className="home-ask-foot"><span><kbd>↵</kbd>发送<span className="hint-separator">·</span><kbd>Shift ↵</kbd>换行</span><span><Plus size={12}/>也可以拖入文件，补充你的资料</span></div>
  </section>

  {suggestions.length>0&&<section className="home-starters rise rise-2" aria-label="示例问题">
   <div className="starter-heading">从这里开始<span>点击填入，编辑后发送</span></div>
   <div className="starter-grid">{suggestions.map((item:Obj)=><button className="starter-question" key={item.label} disabled={busy} onClick={()=>fillQuestion(item.question)}>
    <span className="starter-label"><item.icon size={15}/>{item.label}<ArrowUpRight size={14}/></span><span>{item.question}</span>
   </button>)}</div>
  </section>}

  {dashboardError&&<div className="error-banner" role="alert"><span>工作区概览暂未读取：{dashboardError}</span><button className="text-link" onClick={reloadDashboard}>重试</button></div>}

  <div className="home-lower rise rise-3">
   <section className="continue-work">
    <div className="section-title"><div><span className="section-overline">RECENT ACTIVITY</span><h2>继续上次的思路</h2></div>{sessions.length>0&&<button className="text-link" onClick={()=>setAllSessions(true)}>全部分析<ArrowRight size={14}/></button>}</div>
    <div className="recent-analysis-list">{sessions.length?sessions.slice(0,3).map(rows):<div className="quiet-empty"><History size={24}/><div><strong>你的下一次发现，从这里开始</strong><p>提出问题后，分析过程会自动保存在这里。</p></div></div>}</div>
   </section>
   <aside className="home-library">
    <div className="section-title"><div><span className="section-overline">YOUR KNOWLEDGE</span><h2>资料，让答案有根有据</h2></div><span className="library-emblem"><Database size={18}/></span></div>
    <div className="library-metrics">
     <button onClick={()=>onCatalog('tables')}><span>项目数据表<ArrowUpRight size={13}/></span><strong>{Number.isInteger(profile.table_count)?profile.table_count:dataReady?tables.length:'—'}<small>张</small></strong></button>
     <button onClick={()=>onCatalog('docs')}><span>{projectDocumentCount!==null?'项目关联文档':'已解析文档'}<ArrowUpRight size={13}/></span><strong>{projectDocumentCount!==null?projectDocumentCount:dataReady?ready.length:'—'}<small>份</small></strong></button>
    </div>
    <div className="library-foot"><span>{profile.source_label|| (dataReady?(pending>0?pending+' 份文档待处理':'资料库已读取'):'正在读取资料库')}</span><button className="text-link" onClick={onImport}><Plus size={14}/>添加资料</button></div>
   </aside>
  </div>

  <details className="home-overview"><summary><span><ChartNoAxesColumnIncreasing size={16}/>工作区概览<small>{salesOverview?'销售汇总与提问趋势':'当前项目的提问趋势'}</small></span><ChevronDown size={15}/></summary><div className={'overview-charts'+(salesOverview?'':' trend-only')}>{salesOverview&&<div><h3>净销售额</h3><p>共享示例数据 · 按年汇总 · 元</p><Chart labels={dashboard?.sales?.map((s:Obj)=>s.year)||[]} values={dashboard?.sales?.map((s:Obj)=>s.amount)||[]} height={180} label="净销售额" unit="元"/></div>}<div><h3>提问趋势</h3><p>近 30 天 · 当前项目</p><Chart kind="line" labels={dashboard?.trend?.map((s:Obj)=>s.day.slice(5))||[]} values={dashboard?.trend?.map((s:Obj)=>s.count)||[]} height={180} label="提问次数" unit="次"/></div></div></details>
  <footer className="home-footer"><span><ShieldCheck size={13}/>提问 · 分析 · 来源核验</span><span>{project?.name||'当前项目'}</span></footer>

  {allSessions&&<Modal title="全部分析" close={()=>setAllSessions(false)}><label className="search-field"><Search size={16}/><input aria-label="搜索会话" placeholder="按问题搜索分析" value={filter} onChange={event=>setFilter(event.target.value)}/></label><div className="home-session-list">{sessions.filter((session:Obj)=>session.title.includes(filter)).map(rows)}{!sessions.some((session:Obj)=>session.title.includes(filter))&&<p className="muted">没有匹配的会话</p>}</div></Modal>}
 </div>
}
