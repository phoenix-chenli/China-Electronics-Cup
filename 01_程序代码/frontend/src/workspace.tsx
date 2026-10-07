import React, {useEffect, useRef, useState} from 'react'
import {AlertCircle, ArrowDown, ArrowRight, BarChart3, CheckCircle2, ChevronDown, Copy, FileCheck2, FileText, History, Layers3, Save, Share2, SlidersHorizontal, Target} from 'lucide-react'
import {ResultData, ConditionChanges, SourcePanel} from './analysis-tools'
import {ThreadHistory} from './conversations'
import {ActionMenu, MenuAction, ToolShelf} from './controls'
import {Obj, Empty, examples, Mascot, Modal, num, QueryInput, RichText, Spinner, formatValue as formatMetric} from './ui'
import {AgentSummary,AgentTrace} from './agent'
import {isDatabaseResult} from './result-presentation'
import {DatabaseQueryDetails} from './database-query'
import {isKnowledgeResult,RagOutcome,RagScopeNote,SemanticReview} from './rag'

export function Conditions({result, recompute, busy}: Obj) {
  const [open, setOpen] = useState(false), [draft, setDraft] = useState<Obj>({}), [focus, setFocus] = useState('')
  const editable = (result.conditions || []).filter((c: Obj) => c.editable)
  useEffect(() => {setDraft({}); setOpen(false)}, [result.run_id])
  const changes = Object.fromEntries(Object.entries(draft).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(editable.find((c: Obj) => c.key === key)?.value ?? null)))
  const preview = editable.filter((c: Obj) => ['year', 'region', 'metric', 'rule_mode'].includes(c.key))
  const edit = (key = '') => {setDraft({}); setFocus(key); setOpen(true)}
  const close = () => {setDraft({}); setOpen(false)}
  return <><div className="condition-summary" aria-label="当前查询条件"><span className="analysis-condition-label"><Target size={14}/>查询范围</span>{preview.map((c: Obj) => <button key={c.key} disabled={busy} title={'修改' + c.label} onClick={() => edit(c.key)}>{c.display || c.label}<ChevronDown size={12}/></button>)}{editable.length > 0 && <button className="condition-more" aria-label="调整条件" disabled={busy} onClick={() => edit()}><SlidersHorizontal size={14}/><span>调整条件</span></button>}</div>
    {open && <Modal title="调整查询条件" className="analysis-condition-dialog" close={close}><p className="muted">修改范围或计算口径，应用后会重新分析。结果返回后可撤销。</p><form onSubmit={async e => {e.preventDefault(); if (!Object.keys(changes).length || busy) return; const task = await recompute(changes); if (task) setOpen(false)}}><div className="condition-editor">{editable.map((c: Obj) => <label className="form-field" key={c.key}><span>{c.label}<small>{({default:'默认', missing:'待确认', inherited:'沿用上轮', confirmed:'已确认', explicit:'已识别'} as Obj)[c.state] || ''}</small></span><select autoFocus={focus === c.key} aria-label={c.label} value={JSON.stringify(Object.prototype.hasOwnProperty.call(draft, c.key) ? draft[c.key] : c.value ?? null)} disabled={busy} onChange={e => setDraft(old => ({...old, [c.key]:JSON.parse(e.target.value)}))}>{c.state === 'missing' && <option value="null">请选择</option>}{c.value != null && !(c.options || []).some((o: Obj) => o.value === c.value) && <option value={JSON.stringify(c.value)}>{c.display}</option>}{(c.options || []).map((o: Obj) => <option value={JSON.stringify(o.value)} key={String(o.value)}>{o.label}</option>)}</select></label>)}</div><div className="modal-actions"><button type="button" className="button" onClick={close}>取消</button><button className="button primary" disabled={busy || !Object.keys(changes).length}>应用{Object.keys(changes).length > 0 ? ' ' + Object.keys(changes).length + ' 项修改' : ''}<ArrowRight size={16}/></button></div></form></Modal>}
  </>
}

export function Clarify({result, choose, busy}: Obj) {
  return <section className="clarification"><Mascot/><div><span className="analysis-eyebrow">还差一个条件</span><h3>{result.clarification?.title || '请确认查询条件'}</h3><div className="inline-actions">{result.clarification?.options?.map((o: Obj) => <button className="button" key={o.value} disabled={busy} onClick={() => choose({[result.clarification.field]:o.value})}>{o.label}<ArrowRight size={14}/></button>)}</div></div></section>
}

export function Claims({result, onEvidence}: Obj) {
  return <div className="claims">{(result.claims || []).map((c: Obj, i: number) => <article key={c.id} className={'claim ' + (c.status === 'needs_evidence' ? 'claim-gap' : '')} style={{'--claim-order':i} as React.CSSProperties}><span className="claim-number">{String(i + 1).padStart(2, '0')}</span><div className="analysis-claim-body"><p><RichText text={c.text}/></p><SemanticReview claim={c}/><div className="analysis-claim-sources">{(c.evidence_ids || []).some((id: string) => (result.evidence || []).some((e: Obj) => e.id === id)) && <span>依据</span>}{(c.evidence_ids || []).map((id: string) => {const n = (result.evidence || []).findIndex((e: Obj) => e.id === id); return n < 0 ? null : <button className="citation" aria-label={'查看来源 ' + (n + 1)} title={result.evidence[n].source} key={id} onClick={() => onEvidence(result.evidence[n])}><FileText size={11}/>来源 {n + 1}</button>})}{c.status === 'needs_evidence' && <span className="analysis-evidence-gap"><AlertCircle size={12}/>待补充来源</span>}</div></div></article>)}</div>
}

function QuestionHeading({text = ''}: {text?: string}) {
  const [expanded, setExpanded] = useState(false)
  useEffect(() => setExpanded(false), [text])
  const long = text.length > 180
  return <div className="analysis-question"><span className="analysis-eyebrow">你的问题</span><h1>{long && !expanded ? text.slice(0, 140) + '…' : text}</h1>{long && <button className="text-link question-toggle" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起问题' : '展开完整问题'}<ChevronDown size={13}/></button>}</div>
}

export function AnalysisPage({result, question, setQuestion, run, busy, stage, recompute, undo, onUndo, onSave, onCopy, onEvidence, navigate, choose, session, projectName, loading, selectedMessageId, onSelectMessage, onRetry, onCancel, task, onRename, onShare, tools,onAgentSettings}: Obj) {
  const [sourceId, setSourceId] = useState<string | null>(null), [showProcess, setShowProcess] = useState(false), [sourceOpen, setSourceOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false), [dataOpen, setDataOpen] = useState(false)
  const focus = useRef<HTMLDivElement>(null), scroller = useRef<HTMLDivElement>(null), conclusion = useRef<HTMLElement>(null), data = useRef<HTMLDetailsElement>(null), restored = useRef(false), previous = useRef(selectedMessageId)
  const scrollKey = 'ef-analysis-scroll:' + session?.id
  useEffect(() => {
    if (loading || busy || !scroller.current) return
    const el = scroller.current
    const frame = requestAnimationFrame(() => {let saved = 0; try {saved = Number(sessionStorage.getItem(scrollKey) || 0)} catch {} if (!restored.current && previous.current === selectedMessageId) el.scrollTop = saved; else if (focus.current) el.scrollTop += focus.current.getBoundingClientRect().top - el.getBoundingClientRect().top; restored.current = true; previous.current = selectedMessageId})
    return () => cancelAnimationFrame(frame)
  }, [selectedMessageId, loading, busy, scrollKey])
  useEffect(() => {setSourceId(null); setSourceOpen(false); setDataOpen(false)}, [result?.run_id])
  const peek = (e: Obj) => {if (!e) return; setSourceId(e.id); setSourceOpen(true)}
  const openSource = (e: Obj) => {setSourceOpen(false); onEvidence(e)}
  const jump = (target: HTMLElement | null) => {
    if (!target || !scroller.current) return
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.dataset.motion === 'off'
    scroller.current.scrollTo({top:scroller.current.scrollTop + target.getBoundingClientRect().top - scroller.current.getBoundingClientRect().top - 20, behavior:reduced ? 'auto' : 'smooth'})
  }
  const viewData = () => {setDataOpen(true); requestAnimationFrame(() => jump(data.current))}
  const selectMessage = (id: string) => {setHistoryOpen(false); onSelectMessage(id)}
  const can = (id: string) => !busy && result?.actions?.find((a: Obj) => a.id === id)?.enabled === true
  const rows = result?.rows || [], documentScope = result?.query_spec?.document_context, isKnowledge=isKnowledgeResult(result),isDocument = result?.query_spec?.metric === 'document'||isKnowledge,isDatabase=isDatabaseResult(result)
  const results = (session?.messages || []).filter((m: Obj) => m.result), assistantMessages = (session?.messages || []).filter((m: Obj) => m.role === 'assistant')
  const pastCount = assistantMessages.filter((m: Obj) => m.id !== (busy ? null : selectedMessageId) && !['queued', 'running', 'cancelling'].includes(m.status)).length
  const round = assistantMessages.findIndex((m: Obj) => m.id === selectedMessageId) + 1
  const evidence = result?.evidence || [], gaps = result?.claims?.some((c: Obj) => c.status === 'needs_evidence')
  const latestTask=session?.tasks?.at(-1),failedTask=!busy&&['failed','cancelled'].includes(latestTask?.status)?latestTask:null
  const visibleTools=isDatabase||isKnowledge?(tools||[]).filter((tool:MenuAction)=>tool.id!=='scenario'):tools
  const more: MenuAction[] = [...(can('copy') ? [{id:'copy-sources', label:'复制答案与来源', icon:Copy, onSelect:() => onCopy(true)}, {id:'copy', label:'仅复制答案', icon:Copy, onSelect:() => onCopy(false)}] : []), ...(result && !busy ? [{id:'share', label:'分享结果', icon:Share2, onSelect:onShare}] : []), ...(result?.events?.length && !busy ? [{id:'process', label:'查询过程', icon:History, onSelect:() => setShowProcess(true)}] : []), ...(session ? [{id:'rename', label:'重命名会话', icon:FileText, onSelect:onRename}] : [])]
  return <div className="analysis-layout focus-analysis analysis-art page-enter rail-closed"><section className="conversation panel"><header className="conversation-head"><div className="session-heading"><span className="analysis-session-icon"><Layers3 size={18}/></span><div><small>{projectName || '工作区'}<span>/</span>分析</small><h2>{session?.title || '新问题'}</h2></div></div><div className="inline-actions">{can('save') && <button className="button small" aria-label="保存" onClick={onSave}><Save size={15}/><span>保存</span></button>}<button aria-label={'来源' + (evidence.length ? ' ' + evidence.length : '')} className={'button source-trigger ' + (sourceOpen ? 'active' : '')} disabled={!result || busy} onClick={() => setSourceOpen(true)}><FileCheck2 size={16}/><span>来源</span>{evidence.length > 0 && <small>{evidence.length}</small>}</button>{more.length > 0 && <ActionMenu label="会话更多操作" iconOnly items={more}/>}</div></header>
    <div className="conversation-content" ref={scroller} onScroll={e => {if (!restored.current) return; try {sessionStorage.setItem(scrollKey, String(e.currentTarget.scrollTop))} catch {}}}>
      {loading ? <Spinner text="正在打开会话"/> : <div className="analysis-reading">
        {pastCount > 0 && <details className="analysis-history" open={historyOpen} onToggle={e => setHistoryOpen(e.currentTarget.open)}><summary><History size={14}/><span>此前的对话</span><small>{pastCount} 轮</small><ChevronDown size={14}/></summary><ThreadHistory session={session} selectedId={busy ? null : selectedMessageId} onSelect={selectMessage} onRetry={onRetry} busy={busy}/></details>}
        <div className="analysis-reading-start" ref={focus}>
          {failedTask&&<section className="agent-failure" aria-label="上次查询状态"><p>{failedTask.status==='cancelled'?'上次查询已取消。':failedTask.error||'上次查询失败，请查看执行记录。'}{result&&' 下方保留的是此前查询的结果。'}</p><button className="text-link" onClick={onAgentSettings||(()=>navigate('settings'))}>检查主问答设置</button>{(failedTask.events?.length>0||failedTask.agent?.plan?.length>0)&&<details className="agent-live-toggle"><summary>查看失败或取消的执行过程</summary><AgentSummary agent={failedTask.agent}/><AgentTrace agent={failedTask.agent} events={failedTask.events||[]}/></details>}</section>}
          {!result && !busy ? <div className="analysis-starter"><div className="analysis-starter-mark"><img src="/brand-icon.svg" alt=""/></div><span className="analysis-eyebrow">新的探索，从一个问题开始</span><h1>让数据说清楚。</h1><p>问数据、读文档，让每一个结论都能找到来处。</p><div className="starter-options">{examples.slice(0, 3).map((x, i) => <button key={x} onClick={() => run(x)}><span>0{i + 1}</span>{x}<ArrowRight size={16}/></button>)}</div></div> : <>
            <div className="analysis-context-line"><span>{isKnowledge?(result.query_spec?.query_scope==='hybrid'?'数据库与文档':'知识库问答'):isDocument ? '文档解读' : isDatabase?'数据库问数':'数据分析'}{round > 0 && !busy ? ' · 第 ' + round + ' 轮' : ''}</span>{!busy && selectedMessageId !== results.at(-1)?.id && results.length > 0 && <button className="text-link" onClick={() => selectMessage(results.at(-1).id)}>回到最新结果<ArrowRight size={13}/></button>}</div>
            <QuestionHeading text={busy ? session?.messages?.filter((m: Obj) => m.role === 'user').at(-1)?.text : result?.question || question}/>
            {busy && <><div className="task-feedback" role="status"><div className="analysis-task-orbit" aria-hidden="true"><span/><span/><span/></div><div><span className="analysis-eyebrow">正在处理</span><Spinner text={stage || task?.progress || '正在提交问题'}/><p>结果与来源将在这里呈现。</p></div>{task && <button className="button small" disabled={task.status === 'cancelling'} onClick={onCancel}>{task.status === 'cancelling' ? '正在取消' : '取消查询'}</button>}</div><div className="agent-live"><AgentSummary agent={task?.agent}/>{(task?.events?.length>0||task?.agent?.plan?.length>0)&&<details className="agent-live-toggle" open><summary>计划与执行状态</summary><AgentTrace agent={task?.agent} events={task?.events||[]} compact/></details>}</div></>}
            {result && !busy && <div key={result.run_id} className="answer-reveal">
              {result.status === 'needs_clarification' ? <Clarify result={result} choose={choose} busy={busy}/> : isKnowledge||result.answer_status?<RagOutcome result={result} onEvidence={peek}/>:<div className={'answer-status ' + (gaps||!result.claims?.length ? 'analysis-status-gap' : '')}>{gaps||!result.claims?.length ? <AlertCircle size={15}/> : <CheckCircle2 size={15}/>}<span>{!result.claims?.length?result.answer||'未取得可核验结论':gaps ? '部分结论仍需补充来源' : '分析已完成'}</span><small>{isDocument ? '文档原文' : '本轮查询'}{result.latency_ms != null ? ' · ' + num(result.latency_ms) + ' ms' : ''}</small></div>}
              <AgentSummary agent={result.agent} onProcess={()=>setShowProcess(true)}/>
              {isKnowledge&&<RagScopeNote result={result}/>}
              {isDocument ? documentScope && <div className="document-scope-note"><FileText size={15}/><span>当前文档 · 第 {documentScope.page} 页 · 版本 {documentScope.sha256?.slice(0, 10)}{documentScope.text_range ? ' · 已选片段' : ''}</span></div> : isDatabase?<div className="database-result-source-note"><span>本轮来源 · {result.database_query?.source?.name||'所选数据库'}</span><small>追问沿用本轮来源。切换问数来源后，请新建问答。</small></div>:<Conditions result={result} recompute={recompute} busy={busy}/>}
              {undo && !isDatabase && !isKnowledge && <ConditionChanges before={undo} after={result} onUndo={onUndo}/>}
              {result.status === 'completed' && <>
                <nav className="analysis-read-nav" aria-label="结果阅读导航">{result.claims?.length>0&&<button onClick={() => jump(conclusion.current)}><span>01</span>结论概览</button>}{(rows.length > 0||isDatabase) && <button onClick={viewData}><span>02</span>{isDatabase?'数据与 SQL':'图表与数据'}<small>{rows.length}</small></button>}<button onClick={() => setSourceOpen(true)}><span>{rows.length > 0||isDatabase ? '03' : '02'}</span>来源<small>{evidence.length}</small><ArrowRight size={13}/></button></nav>
                {result.claims?.length>0&&<section className="conclusion-block" ref={conclusion} aria-label="结论概览">
                  {!isDatabase && !isKnowledge && result.metrics?.length > 0 && <div className="metrics-strip">{result.metrics.map((m: Obj, i: number) => <div key={m.label + i} className={i === 0 ? 'analysis-metric-lead' : ''}><span>{m.label}</span><button className="metric-source" disabled={!m.evidence_ids?.length} aria-label={m.label + ' ' + formatMetric(m.value, m.unit) + (m.evidence_ids?.length ? '，查看数值来源' : '')} title={m.evidence_ids?.length ? '查看数值来源' : undefined} onClick={() => peek(evidence.find((e: Obj) => e.id === m.evidence_ids?.[0]))}>{formatMetric(m.value, m.unit)}{m.evidence_ids?.length > 0 && <ArrowRight size={15}/>}</button></div>)}</div>}
                  <div className="analysis-section-heading"><h3>结论</h3><button onClick={() => setSourceOpen(true)} className={'analysis-coverage ' + (gaps ? 'has-gap' : '')}><FileCheck2 size={13}/>{result.coverage?.supported || 0}/{result.coverage?.total || 0} 条有来源<ArrowRight size={12}/></button></div>
                  <Claims result={result} onEvidence={peek}/>
                  {!isDatabase && !isKnowledge && result.formulas?.length > 0 && <div className="conclusion-footer"><span>进一步探索</span><button className="text-link" onClick={() => navigate('scenario')}>调整奖金情景<ArrowRight size={14}/></button></div>}
                </section>}
                {(rows.length > 0||isDatabase) && <details ref={data} className="result-details" open={dataOpen} onToggle={e => setDataOpen(e.currentTarget.open)}><summary><span className="analysis-data-icon"><BarChart3 size={19}/></span><span>{isDatabase?'数据与 SQL':'图表与数据'}<small>{rows.length} 行结果 · {dataOpen ? '点击收起，专注结论' : '点击展开查看'}</small></span><ChevronDown size={17}/></summary><ResultData key={result.run_id} result={result} onSource={peek}/></details>}
                {!isDocument && !isDatabase && result.query_spec?.year && <div className="analysis-next"><span className="analysis-eyebrow">沿着这个发现，继续问</span><div className="followup-options"><button onClick={() => recompute({region:result.query_spec.region === '华北' ? '华东' : '华北'})}>换成{result.query_spec.region === '华北' ? '华东' : '华北'}看看<ArrowRight size={14}/></button><button onClick={() => run('对比 ' + (result.query_spec.year - 1) + ' 年的增长率是多少？', {}, true)}>与 {result.query_spec.year - 1} 年对比<ArrowRight size={14}/></button></div></div>}
                <div className="analysis-reading-end" aria-hidden="true"><span/><ArrowDown size={12}/><span/></div>
              </>}
            </div>}
          </>}
        </div>
      </div>}
    </div>
    <footer className="conversation-composer">{['failed', 'cancelled'].includes(session?.tasks?.at(-1)?.status) && <div className="conversation-outcome" role="status"><span>{session.tasks.at(-1).status === 'cancelled' ? '上次查询已取消' : session.tasks.at(-1).error || '上次查询未完成'}</span><button className="text-link" disabled={busy} onClick={() => onRetry(session.tasks.at(-1).id)}>重试</button></div>}<QueryInput value={question} onChange={setQuestion} onSend={() => run(question, {}, true)} busy={busy} placeholder={isDocument ? '继续询问这份文档…' : result ? '继续追问，让发现更进一步…' : undefined} actions={<ToolShelf items={visibleTools}/>}/><div className="composer-hint"><span>答案可追溯，判断有依据</span><span>Enter 发送 <i>·</i> Shift + Enter 换行</span></div></footer>
  </section>
    {sourceOpen && <Modal title="结论来源" className="source-drawer analysis-source-drawer" close={() => setSourceOpen(false)}><div className="analysis-source-intro"><span className="analysis-eyebrow">循着依据，找到答案</span><p>原始查询、文档片段与结论，一一对应。</p></div><SourcePanel result={result} selected={sourceId} onSelect={setSourceId} onOpen={openSource} onProcess={() => {setSourceOpen(false); setShowProcess(true)}}/></Modal>}
    {showProcess && <Modal title="查询过程" className="analysis-process-dialog" wide close={() => setShowProcess(false)}><Process result={result}/></Modal>}
  </div>
}

export function Process({result}: Obj) {
  return <><AgentSummary agent={result?.agent}/><AgentTrace agent={result?.agent} events={result?.events||[]}/>{isDatabaseResult(result)&&<DatabaseQueryDetails query={result?.database_query}/>} {!result?.events?.length&&!result?.agent?.plan?.length&&!result?.agent?.review&&<Empty title="暂无执行记录"/>}</>
}
