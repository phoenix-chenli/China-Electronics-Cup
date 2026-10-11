import React, {useEffect, useRef, useState} from 'react'
import {FolderOpen, MessageCircle, Pencil, Plus, RotateCcw} from 'lucide-react'
import {api, IconButton, Modal, Obj, timeLabel} from './ui'
import {QueryScope,queryScopeOverride,taskFeedback} from './agent'

const activeStates = ['queued', 'running', 'cancelling']
const readTimeoutMs=12000,submitTimeoutMs=20000
export function sessionPollDelay(failures:number):number{return failures>0?Math.min(8000,1000*2**Math.min(failures-1,3)):700}
export function withSubmittedTask(session:Obj,task:Obj):Obj{
  return {...session,tasks:[...(session.tasks||[]).filter((t:Obj)=>t.id!==task.id),task]}
}
function remembered(): Obj {try {return JSON.parse(localStorage.getItem('ef-active-conversation') || '{}')} catch {return {}}}
export function initialProjectId(projects:Obj[],savedId?:string):string{
 return projects.some(project=>project.id===savedId)?savedId!:projects.some(project=>project.id==='public-statistics')?'public-statistics':projects.some(project=>project.id==='default')?'default':projects[0]?.id||'public-statistics'
}
export function taskScopeFields(scopeOverride:unknown,follow=false,mode='query',documentContext:Obj|null=null,ragScope:Obj|null=null):Obj{
 const override=!follow&&mode!=='recompute'&&!documentContext&&!ragScope?queryScopeOverride(scopeOverride):undefined
 return override?{scope_override:override}:{}
}
export function taskRequestContext(parentMessageId:string|undefined,scopeOverride:unknown,follow=false,mode='query',documentContext:Obj|null=null,ragScope:Obj|null=null):Obj{
 return {...taskScopeFields(scopeOverride,follow,mode,documentContext,ragScope),
  parent_message_id:follow||mode==='recompute'?parentMessageId||null:null,inherit_context:follow}
}
function conditionChange(conditions: Obj) {
  const labels: Obj = {year:'年份', region:'区域', metric:'指标', rule_mode:'规则', scenario_pct:'情景调整', compare_year:'对比年份'}
  const values: Obj = {net_sales:'净销售额', gross_sales:'销售额（未扣退货）', count:'客户与订单数', growth_rate:'净销售额增长率', historical:'交易当时规则', current:'当前规则模拟'}
  const changes = Object.entries(conditions).map(([key,value]) => `${labels[key] || key} ${value === null ? '全部' : values[String(value)] || String(value)}${key === 'scenario_pct' ? '%' : ''}`)
  return changes.length ? '修改条件：' + changes.join('，') : '刷新当前结果'
}

export function useConversations() {
  const [projects, setProjects] = useState<Obj[]>([])
  const [projectId, setProjectId] = useState<string>(remembered().projectId || 'public-statistics')
  const [sessions, setSessions] = useState<Obj[]>([])
  const [session, setSession] = useState<Obj | null>(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState('')
  const [error, setError] = useState('')
  const [undoId, setUndoId] = useState<string | null>(null)
  const active = useRef({projectId, sessionId: remembered().sessionId || ''})
  const sending = useRef(new Set<string>())
  const revision = useRef(0)
  const listRevision = useRef(0)
  const remember = () => localStorage.setItem('ef-active-conversation', JSON.stringify(active.current))

  const accept = (detail: Obj) => {
    if (active.current.sessionId === detail.id) setSession(detail)
  }
  const refreshList = async (pid = active.current.projectId,signal?:AbortSignal) => {
    const serial = ++listRevision.current
    const list = await api(`/api/projects/${pid}/sessions`,undefined,undefined,{timeoutMs:readTimeoutMs,signal})
    if (active.current.projectId === pid && serial === listRevision.current) setSessions(list)
  }
  const refresh = async (sid = active.current.sessionId,signal?:AbortSignal) => {
    const serial = sid === active.current.sessionId ? ++revision.current : -1
    if (!sid) return
    const detail = await api(`/api/sessions/${sid}`,undefined,undefined,{timeoutMs:readTimeoutMs,signal})
    if (serial === revision.current && active.current.sessionId === sid) {accept(detail); setError('')}
    await refreshList(detail.project_id,signal)
  }
  const openSession = async (sid: string) => {
    ++revision.current
    active.current.sessionId = sid; remember()
    setSession(null); setLoading(true); setError(''); setUndoId(null)
    try {await refresh(sid)} catch(e) {if (active.current.sessionId === sid) setError((e as Error).message)}
    finally {if (active.current.sessionId === sid) setLoading(false)}
  }
  const switchProject = async (pid: string, preferred?: string) => {
    active.current = {projectId: pid, sessionId: ''}; remember(); ++revision.current
    setProjectId(pid); setSession(null); setSessions([]); setLoading(true); setError(''); setUndoId(null)
    try {
      const list = await api(`/api/projects/${pid}/sessions`,undefined,undefined,{timeoutMs:readTimeoutMs})
      if (active.current.projectId !== pid) return
      setSessions(list)
      const sid = list.find((x: Obj) => x.id === preferred)?.id || list[0]?.id
      if (sid) await openSession(sid)
    } catch(e) {if (active.current.projectId === pid) setError((e as Error).message)}
    finally {if (active.current.projectId === pid) setLoading(false)}
  }
  const bootstrap = async () => {
    setLoading(true)
    try {
      const list = await api('/api/projects',undefined,undefined,{timeoutMs:readTimeoutMs}); setProjects(list)
      const saved = remembered()
      await switchProject(initialProjectId(list,saved.projectId), saved.sessionId)
    } catch(e) {setError((e as Error).message); setLoading(false)}
  }
  useEffect(() => {void bootstrap()}, [])
  const task = session?.tasks?.find((t: Obj) => activeStates.includes(t.status)) || null
  const taskId = task?.id
  useEffect(() => {
    if (!taskId) return
    let disposed = false, timer: number,failures=0
    const reading=new AbortController()
    const poll = async () => {
      try {if (!disposed){await refresh(undefined,reading.signal);failures=0}}
      catch(e) {if (!disposed){failures++;setError((e as Error).message+'；正在恢复已有查询状态，'+sessionPollDelay(failures)/1000+'秒后重新读取，不会再次发送问题。')}}
      if (!disposed) timer = window.setTimeout(poll, sessionPollDelay(failures))
    }
    timer = window.setTimeout(poll, 200)
    return () => {disposed = true; window.clearTimeout(timer);reading.abort()}
  }, [taskId, session?.id])
  // Other browser tabs may finish work while this one is inactive.
  useEffect(() => {
    const focus = () => {const sid=active.current.sessionId;if(sid)void refresh(sid).catch(e=>{if(active.current.sessionId===sid)setError(e.message)})}
    window.addEventListener('focus', focus)
    return () => window.removeEventListener('focus', focus)
  }, [])
  const create = async () => {
    const pid = active.current.projectId
    const serial = ++revision.current
    const detail = await api(`/api/projects/${pid}/sessions`, {})
    if (active.current.projectId === pid && serial === revision.current) {
      active.current.sessionId = detail.id; remember(); ++revision.current
      setSession(detail); setUndoId(null); setError(''); setLoading(false)
      await refreshList(pid)
    }
    return detail
  }
  const resultMessage = session?.messages?.find((m: Obj) => m.id === session.selected_message_id)
    || session?.messages?.filter((m: Obj) => m.result).at(-1)
  const select = async (mid: string) => {
    const sid = active.current.sessionId
    const detail = await api(`/api/sessions/${sid}`, {selected_message_id: mid}, 'PATCH')
    if (active.current.sessionId === sid) {++revision.current; accept(detail)}
  }
  const submit = async (text: string, confirmed: Obj = {}, follow = false, mode = 'query', fresh = false, documentContext: Obj | null = null, ragScope: Obj | null = null, scopeOverride?:QueryScope) => {
    // The guard covers session creation too, so a double click cannot create two sessions.
    const guard = fresh ? active.current.projectId : active.current.sessionId || active.current.projectId
    if (sending.current.has(guard)) return
    sending.current.add(guard)
    let sid = fresh ? '' : active.current.sessionId
    const pid = active.current.projectId
    setSubmitting(sid || pid); setError('')
    try {
      if (!sid) {
        sid = (await create()).id
        if (active.current.projectId !== pid || active.current.sessionId !== sid) return
      }
      const body = {question: text, confirmed, session_id: sid, request_id: crypto.randomUUID(), document_context: documentContext,rag_scope:ragScope,
        ...taskRequestContext(resultMessage?.id,scopeOverride,follow,mode,documentContext,ragScope),mode,
        display_question: mode === 'recompute' ? conditionChange(confirmed) : text}
      if (mode === 'recompute') setUndoId(resultMessage?.id || null)
      else setUndoId(null)
      localStorage.setItem(`ef-pending-${sid}`, JSON.stringify(body))
      const result = await api('/api/tasks', body,undefined,{timeoutMs:submitTimeoutMs})
      localStorage.removeItem(`ef-pending-${sid}`)
      // An accepted task remains visible even if the following GET fails.
      if(active.current.projectId===pid&&active.current.sessionId===sid)setSession(detail=>detail&&detail.id===sid?withSubmittedTask(detail,result):detail)
      await refresh(sid)
      return result
    } catch(e) {
      if (active.current.projectId === pid && (!sid || active.current.sessionId === sid)) setError((e as Error).message)
      throw e
    } finally {sending.current.delete(guard); setSubmitting('')}
  }
  const retrySubmission = async () => {
    const sid = active.current.sessionId
    const pending = localStorage.getItem(`ef-pending-${sid}`)
    if (!pending) {await refresh(); return}
    const result=await api('/api/tasks', JSON.parse(pending),undefined,{timeoutMs:submitTimeoutMs})
    if(active.current.sessionId===sid)setSession(detail=>detail&&detail.id===sid?withSubmittedTask(detail,result):detail)
    localStorage.removeItem(`ef-pending-${sid}`); await refresh(sid)
  }
  const cancel = async () => {if (task) {await api(`/api/tasks/${task.id}/cancel`, {},undefined,{timeoutMs:submitTimeoutMs}); await refresh()}}
  const retry = async (tid: string) => {
    if (sending.current.has(tid)) return
    sending.current.add(tid)
    const sid = active.current.sessionId
    const key = `ef-retry-${tid}`
    const requestId = localStorage.getItem(key) || crypto.randomUUID()
    localStorage.setItem(key, requestId)
    try {
      const result=await api(`/api/tasks/${tid}/retry`, {request_id: requestId},undefined,{timeoutMs:submitTimeoutMs})
      if(active.current.sessionId===sid)setSession(detail=>detail&&detail.id===sid?withSubmittedTask(detail,result):detail)
      localStorage.removeItem(key); await refresh(sid)
    } finally {sending.current.delete(tid)}
  }
  const importSaved = async (saved: Obj) => {
    const detail = await create()
    const imported = await api(`/api/sessions/${detail.id}/snapshot`, {saved_id: saved.id})
    accept(imported); await refreshList(detail.project_id)
  }
  const renameSession = async (title: string) => {
    const detail = await api(`/api/sessions/${active.current.sessionId}`, {title}, 'PATCH')
    accept(detail); await refreshList(detail.project_id)
  }
  const projectAction = async (value: string, edit: boolean) => {
    const r = await api(edit ? `/api/projects/${projectId}` : '/api/projects', {name: value}, edit ? 'PATCH' : 'POST')
    setProjects(await api('/api/projects'))
    if (!edit) await switchProject(r.id)
  }
  return {projects, projectId, sessions, session, loading, error, task, resultMessage, result: resultMessage?.result || null,
    busy: !!task || !!submitting, submitting: !!submitting, activeId: () => active.current.sessionId,
    undoId: undoId || session?.messages?.find((m:Obj)=>m.result && resultMessage?.result?.parent_run_id && m.result.run_id===resultMessage.result.parent_run_id)?.id || null, setUndoId, select, submit, cancel, retry, retrySubmission, refresh, bootstrap,
    openSession, switchProject, create, importSaved, renameSession, projectAction,
    project: projects.find(p => p.id === projectId)}
}

export function ProjectDialog({controller: c, close, notify}: Obj) {
  const [mode, setMode] = useState(''), [value, setValue] = useState(''), [busy, setBusy] = useState(false)
  return <Modal title="项目" close={close}>
    <div className="project-list">{c.projects.map((p: Obj) => <button key={p.id} aria-current={p.id === c.projectId ? 'true' : undefined} disabled={busy} onClick={async () => {setBusy(true);try{await c.switchProject(p.id);close()}catch(e){notify((e as Error).message)}finally{setBusy(false)}}}><FolderOpen size={21}/><span><strong>{p.name}</strong><small>{p.source_label||p.profile?.source_label||'按当前项目资料匹配新问题的范围'}</small></span>{p.id === c.projectId && <span className="badge">当前</span>}</button>)}</div>
    <div className="inline-actions"><button className="button" onClick={() => {setMode('create'); setValue('')}}><Plus size={16}/>新建项目</button><button className="button" onClick={() => {setMode('edit'); setValue(c.project?.name || '')}}><Pencil size={15}/>重命名当前项目</button></div>
    {mode && <form className="project-form" onSubmit={async e => {e.preventDefault(); setBusy(true); try {await c.projectAction(value, mode === 'edit'); close()} catch(e) {notify((e as Error).message)} finally {setBusy(false)}}}><label htmlFor="project-name">{mode === 'edit' ? '项目名称' : '新项目名称'}</label><input id="project-name" autoFocus maxLength={120} value={value} onChange={e => setValue(e.target.value)}/><button className="button primary" disabled={busy || !value.trim()}>{busy ? '正在保存' : '保存'}</button></form>}
  </Modal>
}

export function ThreadHistory({session, selectedId, onSelect, onRetry, busy}: Obj) {
  const messages = session?.messages || []
  const past = messages.filter((m: Obj) => m.role === 'assistant' && m.id !== selectedId && !activeStates.includes(m.status))
  if (!past.length) return null
  return <div className="thread-history" aria-label="会话记录">{past.map((m: Obj) => {
    const index = messages.indexOf(m)
    return <article className="thread-turn" key={m.id}>
      <div className="thread-question"><MessageCircle size={15}/><p>{messages[index - 1]?.text}</p><time>{timeLabel(m.created_at)}</time></div>
      <div className={'thread-answer ' + m.status}><p>{m.text||m.result?.answer||(['failed','cancelled'].includes(m.status)?taskFeedback({...m,...session.tasks?.find((t:Obj)=>t.id===m.task_id)}).detail:m.result?.claims?.length?'本轮已保存有来源的结论，可打开核对。':'本轮未返回可展示的答案，请查看执行记录。')}</p>
        {m.result ? <button className="text-link" onClick={() => onSelect(m.id)}>查看结果与来源</button> : <span>{m.status === 'cancelled' ? '已取消' : '未完成'}</span>}
        {['failed', 'cancelled'].includes(m.status) && <button className="button small" disabled={busy} onClick={() => onRetry(m.task_id)}><RotateCcw size={14}/>重试这轮</button>}
      </div>
    </article>
  })}</div>
}

export function RenameSessionDialog({session, save, close, notify}: Obj) {
  const [value, setValue] = useState(session.title), [busy, setBusy] = useState(false)
  return <Modal title="会话名称" close={close}><form className="project-form" onSubmit={async e => {e.preventDefault(); setBusy(true); try {await save(value); close()} catch(e) {notify((e as Error).message)} finally {setBusy(false)}}}><label htmlFor="session-name">名称</label><input id="session-name" maxLength={120} value={value} onChange={e => setValue(e.target.value)}/><button className="button primary" disabled={busy || !value.trim()}>{busy ? '正在保存' : '保存'}</button></form></Modal>
}
