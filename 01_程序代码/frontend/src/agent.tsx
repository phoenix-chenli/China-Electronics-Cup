import React,{useEffect,useRef,useState} from 'react'
import {AlertCircle,Bot,CheckCircle2,Clock3,LoaderCircle,RefreshCw,ShieldCheck,XCircle} from 'lucide-react'
import {api,Obj,num} from './ui'
import './agent.css'

type AgentMode='auto'|'local'|'model'
export type QueryScope='sales'|'database'|'knowledge'|'hybrid'
type AgentConnection={id:string;name:string;model?:string;enabled:boolean;available:boolean;reason?:string}
type AgentDatabase={id:string;name:string;kind:string;available:boolean;reason?:string}
type AgentConfig={mode:AgentMode;connection_id:string|null;max_steps:number;connections:AgentConnection[];available:boolean;query_scope?:QueryScope;database_connection_id?:string|null;databases?:AgentDatabase[];project?:Obj;project_name?:string;profile?:Obj;source_profile?:Obj;source_label?:string;default_scope?:string;document_count?:number;starter_questions?:string[];scope_options?:Obj[]}
type AgentPlanStep={id?:string;label?:string;tool?:string;status?:string}
export type AgentSnapshot={mode?:'model'|'local';connection_id?:string;model?:string;plan?:AgentPlanStep[];review?:{status?:string;detail?:string};usage?:Obj;calls?:number|Obj[]}
export type AgentEvent={step_id?:string;id?:string;label?:string;tool?:string;status?:string;summary?:string;duration_ms?:number;started_at?:string;finished_at?:string;inputs?:Obj;outputs?:Obj}

const modeNames:Record<AgentMode,string>={auto:'自动选择',local:'本地确定性查询',model:'指定模型驱动'}
const statusNames:Record<string,string>={pending:'待执行',planned:'待执行',queued:'等待中',running:'进行中',completed:'已完成',failed:'失败',cancelled:'已取消',cancelling:'正在取消',skipped:'已跳过',passed:'通过',needs_evidence:'待补充证据',needs_review:'待复核',needs_clarification:'待确认条件',local:'本地规则检查'}
const statusName=(status?:string)=>status?statusNames[status]||'状态待确认':'未记录状态'

export function StatusIcon({status}: {status?:string}){
 if(status==='running'||status==='cancelling')return <LoaderCircle size={16} className="spin"/>
 if(status==='completed'||status==='passed')return <CheckCircle2 size={16}/>
 if(status==='failed')return <AlertCircle size={16}/>
 if(status==='cancelled')return <XCircle size={16}/>
 return <Clock3 size={16}/>
}

export function EventSummary({event}:{event:AgentEvent}){
 return <div className={'event-summary status-'+(event.status||'unknown')}><StatusIcon status={event.status}/><span>{event.label||event.tool||'执行步骤'}</span><small>{statusName(event.status)}{Number.isFinite(event.duration_ms)?' · '+num(event.duration_ms!)+' ms':''}</small></div>
}

export function ModelPackageStatus({model}:{model:Obj}){
 const active=['pending','running','downloading','installing'].includes(model.status)
 const label=model.installed&&model.verified?'文件校验通过':active?({pending:'等待安装',running:'正在安装',downloading:'正在下载',installing:'正在安装'} as Obj)[model.status]:model.status==='failed'?'安装失败':model.bundled===true?'随包可安装':model.bundled===false?'需下载模型':'安装来源尚未读取'
 return <span className={'badge '+(model.installed&&model.verified?'green':'amber')}>{label}</span>
}

export const queryScopeOptions=[{id:'sales',label:'销售示例'},{id:'database',label:'数据库问数'},{id:'knowledge',label:'知识库问答'},{id:'hybrid',label:'数据库与文档'}]
export function projectQueryProfile(config:Obj|null):Obj{return {...config,...(config?.source_profile||config?.profile||config?.project?.source_profile||{})}}
export function queryScopeOverride(value:unknown):QueryScope|undefined{return queryScopeOptions.some(option=>option.id===value)?value as QueryScope:undefined}
export function queryContextPolicy(hasResult:boolean,retainContext=true){
 const inheritContext=hasResult&&retainContext
 return {inheritContext,allowNewScope:!inheritContext}
}

export function taskFeedback(value:Obj):{kind:string;title:string;detail:string;action:string}{
 const message=typeof value.error==='string'?value.error:typeof value.text==='string'?value.text:typeof value.answer==='string'?value.answer:''
 const code=String(value.error_code||value.error_kind||value.failure_kind||'')+' '+message
 if(value.status==='cancelled')return {kind:'cancelled',title:'本轮查询已取消',detail:message||'已停止继续执行本轮任务。',action:'可以修改问题后发送，或重试这一轮。'}
 if(value.status==='needs_clarification'||value.answer_status==='needs_clarification')return {kind:'clarification',title:'需要补充问题范围',detail:value.clarification?.title||message||'请补充时间、对象或统计口径。',action:'确认条件后继续，已有资料和来源会保留。'}
 if(value.error_detail?.category==='database')return {kind:'database',title:'数据库查询未完成',detail:message||'查询未通过校验或数据库未返回结果。',action:'检查字段、筛选条件和只读连接后再试；执行记录保留了具体失败步骤。'}
 if(/timeout|timed.?out|超时|超过.*时限/i.test(code))return {kind:'timeout',title:'本轮请求超时',detail:message||'服务未在等待时限内返回结果。',action:'可缩小问题范围后重试，或检查模型连接。'}
 if(/connect|network|unreachable|连接|断开|网络|不可达|服务未启动/i.test(code))return {kind:'connection',title:'连接暂时失败',detail:message||'当前无法连接所需服务。',action:'检查本地服务或模型连接，恢复后可重试本轮。'}
 if(['refused','insufficient_evidence'].includes(value.answer_status)||/资料不足|证据不足|缺少.*资料|没有.*依据|insufficient/i.test(code))return {kind:'insufficient',title:'现有资料不足以回答',detail:value.refusal_reason||message||'未取得支持这个问题的充分资料。',action:'可补充资料，或缩小时间、对象和问题范围。'}
 return {kind:'failed',title:'本轮查询未完成',detail:message||'执行结束，但未返回可展示的答案。',action:'可以重试这一轮，并查看执行记录中的具体原因。'}
}

export function TaskProgress({task,stage}:Obj){
 const [now,setNow]=useState(Date.now)
 useEffect(()=>{setNow(Date.now());if(!task||!['queued','running','cancelling'].includes(task.status))return;const timer=window.setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[task?.id,task?.status])
 const queued=task?.status==='queued',cancelling=task?.status==='cancelling'
 const start=Date.parse(task?.created_at||''),seconds=Number.isFinite(start)?Math.max(0,Math.floor((now-start)/1000)):null
 const elapsed=seconds===null?'':seconds<60?seconds+' 秒':Math.floor(seconds/60)+' 分 '+seconds%60+' 秒'
 return <div className="task-stage-detail"><strong>{cancelling?'正在取消':queued?'正在排队':task?'正在执行':'正在提交问题'}</strong><span role="status">{stage||task?.stage||task?.progress||(queued?'等待执行资源':'准备读取问题与来源')}</span>{elapsed&&<small aria-live="off">{queued?'已等待':'提交后已过'} {elapsed}</small>}</div>
}

export function QueryContextBar({config,result:priorResult,inheritContext=true,onSettings,onNew,routePreview,scopeOverride='',onScopeChange,previewLoading=false,previewError='',busy=false}:{config:AgentConfig|null;result?:Obj|null;inheritContext?:boolean;onSettings:()=>void;onNew?:()=>void;routePreview?:Obj|null;scopeOverride?:string;onScopeChange?:(scope:string)=>void;previewLoading?:boolean;previewError?:string;busy?:boolean}){
 const result=queryContextPolicy(!!priorResult,inheritContext).inheritContext?priorResult:null
 const scopeNames:Record<string,string>={sales:'销售示例',database:'数据库问数',knowledge:'知识库问答',hybrid:'数据库与文档',document:'当前文档'}
 if(!config&&!result)return <div className="query-context"><span>问答配置尚未读取</span><button type="button" className="text-link" onClick={onSettings}>检查设置</button></div>
 const profile=projectQueryProfile(config),preview=!result?routePreview:null
 const scope=result?(result.query_spec?.metric==='document'&&result.query_spec?.document_context?'document':result.query_spec?.query_scope||'sales'):preview?.query_scope||scopeOverride||(profile.default_scope==='auto'?'auto':profile.default_scope||config?.query_scope||'sales')
 const database=result?.database_query?.source||result?.query_spec?.database_source
 const cid=result?(database?.connection_id??result.query_spec?.database_connection_id??null):config?.database_connection_id
 const projectName=config?.project_name||config?.project?.name
 const frozenDocuments=result?.rag?.scope?.documents||result?.query_spec?.rag_scope?.documents||[]
 const frozenSource=frozenDocuments.length?frozenDocuments.slice(0,2).map((document:Obj)=>document.name||'所选文档').join('、')+(frozenDocuments.length>2?' 等 '+frozenDocuments.length+' 份资料':''):''
 const source=(!result&&preview?.source_label)||(!result&&!scopeOverride&&profile.source_label)||(scope==='database'||scope==='hybrid'?database?.name||config?.databases?.find(d=>d.id===cid)?.name||(cid?'所选数据库':'本机工作库'):scope==='sales'?'本机销售示例':scope==='document'?'固定文档版本':result?frozenSource||'查询时的知识库文档':profile.source_label||'本机知识库')
 const items=(config?.connections||[]).filter(c=>c.enabled&&c.available)
 const selected=config?.connection_id?config.connections.find(c=>c.id===config.connection_id):items.length===1?items[0]:undefined
 const resolvedLocal=config?.mode==='local'||config?.mode==='auto'&&!config.connection_id&&items.length===0
 const modelReady=!!selected?.available&&!!selected.enabled
 const mode=!config?'问答模型设置尚未读取':resolvedLocal?(scope==='knowledge'||scope==='document'?'关键词原文检索':'本地确定性查询'):modelReady?selected.model||selected.name:'问答模型不可用'
 const blocked=!!config&&((scope==='database'||scope==='hybrid')&&(resolvedLocal||!modelReady)||config.mode==='model'&&!modelReady||!!config.connection_id&&!modelReady||config.mode==='auto'&&!config.connection_id&&items.length>1)
 const changed=!!result&&!!config&&profile.default_scope!=='auto'&&(scope!==(config.query_scope||'sales')||(scope==='database'||scope==='hybrid')&&(cid??null)!==(config.database_connection_id??null))
 const options=(preview?.scope_options||profile.scope_options||config?.scope_options||queryScopeOptions).filter((option:Obj)=>queryScopeOverride(option.id)&&(!profile.managed_public||option.id!=='sales'))
 const count=preview?.document_count??(!scopeOverride?profile.document_count:undefined)
 return <div className={'query-context'+(blocked?' needs-setup':'')} aria-label="当前提问范围"><div><strong>{result?'追问沿用':priorResult&&!inheritContext?'独立新问题':scopeOverride?'已选范围':preview?'推荐范围':'新问答'} · {scope==='auto'?'按问题自动匹配':scopeNames[scope]||'范围待确认'}</strong>{!result&&projectName&&<small className="query-project-name">当前项目 · {projectName}</small>}<span>{source}{!result&&Number.isInteger(count)?' · '+count+' 份文档':''} · {mode}</span>{result&&<small>来源与版本沿用本轮记录。</small>}{priorResult&&!inheritContext&&<small>已关闭保留上下文，本轮按项目重新选择资料。</small>}{!result&&onScopeChange&&<label className="query-scope-choice">本次范围<select aria-label="本次问答范围" value={scopeOverride} disabled={busy} onChange={event=>onScopeChange(event.target.value)}><option value="">自动匹配项目资料</option>{options.map((option:Obj)=><option key={option.id} value={option.id}>{option.label||scopeNames[option.id]}</option>)}</select></label>}{!result&&previewLoading&&<small role="status">正在核对项目资料范围…</small>}{!result&&!previewLoading&&preview?.reason&&<small className="query-route-reason">{preview.reason}</small>}{!result&&previewError&&<small role="status">范围预览暂不可用；提交时会重新核对所选范围。{previewError}</small>}{blocked&&<small>请先选择可用模型和数据源。</small>}{!result&&scope==='knowledge'&&resolvedLocal&&<small>当前仅返回检索原文，尚未启用综合回答。</small>}{changed&&<small>默认设置已改变；切换来源请新建问答。</small>}</div><button type="button" className="text-link" onClick={changed&&onNew?onNew:onSettings}>{changed&&onNew?'新建问答':'问答设置'}</button></div>
}

export function AgentConfigPanel({notify,updated,refreshKey=0}:Obj){
 const [config,setConfig]=useState<AgentConfig|null>(null),[mode,setMode]=useState<AgentMode>('auto'),[connectionId,setConnectionId]=useState('')
 const [queryScope,setQueryScope]=useState<QueryScope>('sales'),[databaseId,setDatabaseId]=useState('')
 const [loading,setLoading]=useState(true),[saving,setSaving]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState(false)
 const serial=useRef(0),dirty=useRef(false)
 const load=async()=>{
  const request=++serial.current;setLoading(true);setError('')
  try{const data:AgentConfig=await api('/api/agent/config');if(request!==serial.current)return;setConfig(data);if(!dirty.current){setMode(data.mode);setConnectionId(data.connection_id||'');setQueryScope(data.query_scope||'sales');setDatabaseId(data.database_connection_id||'')}}
  catch(e){if(request===serial.current)setError((e as Error).message)}
  finally{if(request===serial.current)setLoading(false)}
 }
 useEffect(()=>{void load();const changed=()=>void load();window.addEventListener('ef-connections-changed',changed);return()=>{serial.current++;window.removeEventListener('ef-connections-changed',changed)}},[refreshKey])
 const items=config?.connections||[],available=items.filter(c=>c.enabled&&c.available),selected=items.find(c=>c.id===connectionId)
 const databases=(config?.databases||[]).filter(d=>d.id),database=databases.find(d=>d.id===databaseId),isDatabase=queryScope==='database'||queryScope==='hybrid',isKnowledge=queryScope==='knowledge'||queryScope==='hybrid'
 const modelReady=!!selected?.enabled&&!!selected?.available
 const automaticModelReady=mode==='auto'&&!connectionId&&available.length===1
 const sourceReady=!isDatabase||!databaseId||database?.available===true
 const changed=!!config&&(mode!==config.mode||(mode==='local'?null:connectionId||null)!==config.connection_id||queryScope!==(config.query_scope||'sales')||(isDatabase?databaseId||null:null)!==(config.database_connection_id||null))
 const change=(nextMode:AgentMode,nextId=connectionId)=>{dirty.current=true;setMode(nextMode);setConnectionId(nextId);setSaved(false);setError('')}
 const changeScope=(scope:QueryScope)=>{dirty.current=true;setQueryScope(scope);setSaved(false);setError('');if((scope==='database'||scope==='hybrid')&&mode==='local'){setMode('model');if(!connectionId&&available.length===1)setConnectionId(available[0].id)}}
 const save=async(e:React.FormEvent)=>{
  e.preventDefault();if(!config||saving||loading||!sourceReady||(isDatabase&&(mode==='local'||!modelReady&&!automaticModelReady)))return
  setSaving(true);setError('');setSaved(false)
  try{await api('/api/agent/config',{mode,connection_id:mode==='local'?null:connectionId||null,query_scope:queryScope,database_connection_id:isDatabase?databaseId||null:null},'PUT');dirty.current=false;await load();setSaved(true);notify?.('主问答方式已保存');updated?.();window.dispatchEvent(new Event('ef-agent-config-changed'))}
  catch(e){setError((e as Error).message)}finally{setSaving(false)}
 }
 return <section className="agent-config pref-card" aria-labelledby="agent-config-title">
  <div className="agent-config-heading"><h3 id="agent-config-title"><Bot size={22}/>主问答方式</h3><button type="button" className="button small" disabled={loading||saving} onClick={()=>void load()}><RefreshCw size={14}/>刷新可用状态</button></div>
  <p className="muted">问数来源保存后用于新建问答。数据库追问沿用原来源；已有结果保留查询时的方式、范围和来源。</p>
  {error&&<p className="error-banner" role="alert">{error}</p>}
  {!config?<p role="status">{loading?'正在读取主问答设置':'主问答设置未读取，请重试。'}</p>:<form onSubmit={save}>
   <fieldset className="agent-mode-options agent-scope-options" disabled={loading||saving}><legend>问答范围</legend>{([{id:'sales',title:'销售示例',note:'既有销售口径、奖金与文档工具'},{id:'database',title:'数据库问数',note:'根据所选库结构生成只读 SQL，支持关联、聚合和嵌套查询'},{id:'knowledge',title:'知识库问答',note:'检索本机文档，综合有证据的内容；资料不足明确拒答'},{id:'hybrid',title:'数据库与文档',note:'结合所选库的只读结果与文档条件，使用有来源的计算'}] as const).map(scope=><label key={scope.id} className={queryScope===scope.id?'selected':''}><input type="radio" name="agent-query-scope" value={scope.id} checked={queryScope===scope.id} onChange={()=>changeScope(scope.id)}/><span><strong>{scope.title}</strong><small>{scope.note}</small></span></label>)}</fieldset>
   {isKnowledge&&<p className="agent-config-note">新问答检索本机共享知识库；原文页内提问仍限制本页或选区。历史追问保留提交时的文档范围与版本。未配置生成模型时仅提供原文检索。</p>}
   {isDatabase&&<><label className="form-field agent-model-picker">问数来源<select aria-label="问数数据库来源" value={databaseId} disabled={loading||saving} onChange={e=>{dirty.current=true;setDatabaseId(e.target.value);setSaved(false);setError('')}}><option value="">本机工作库（含已导入表）</option>{databaseId&&!database&&<option value={databaseId} disabled>原数据库连接已删除，请重新选择</option>}{databases.map(d=><option key={d.id} value={d.id} disabled={!d.available}>{d.name} · {d.kind}{!d.available?' · 不可用':''}</option>)}</select></label><p className="agent-config-note">数据库问数使用模型规划并执行只读查询。外部数据库需通过测试、启用且符合隐私设置；本机工作库包含导入的 CSV、表格和数据库表。</p>{!sourceReady&&<p className="agent-config-note">所选数据库不可用：{database?.reason||(!database?'连接已删除':'请检查连接测试、启用状态和隐私设置')}。</p>}{databases.some(d=>!d.available)&&<details className="agent-unavailable"><summary>查看数据库可用情况</summary>{databases.map(d=><p key={d.id}><strong>{d.name}</strong><span>{d.available?'可用':d.reason||'不可用'}</span></p>)}</details>}</>}
   <fieldset className="agent-mode-options" disabled={loading||saving}><legend>处理方式</legend>{(['auto','local','model'] as AgentMode[]).map(value=><label key={value} className={mode===value?'selected':''}><input type="radio" name="agent-mode" value={value} checked={mode===value} disabled={isDatabase&&value==='local'} onChange={()=>change(value)}/><span><strong>{modeNames[value]}</strong><small>{value==='auto'?(isDatabase?'选择唯一可用模型；不可用时明确提示':'按可用连接选择模型，未配置时使用本地查询'):value==='local'?(isDatabase?'数据库问数需要模型':isKnowledge?'检索原文并展示来源，未生成综合答案':'使用现有销售示例的规则与 SQL'):'使用选定模型规划并调用工具，失败时明确报错'}</small></span></label>)}</fieldset>
   {mode!=='local'&&<label className="form-field agent-model-picker">{mode==='auto'?'首选文本模型':'主问答文本模型'}<select aria-label="主问答文本模型" value={connectionId} disabled={loading||saving} onChange={e=>change(mode,e.target.value)}><option value="">{mode==='auto'?'自动选择唯一可用连接':'请选择可用模型连接'}</option>{connectionId&&!selected&&<option value={connectionId} disabled>原连接已删除，请重新选择</option>}{items.map(c=><option key={c.id} value={c.id} disabled={!c.enabled||!c.available}>{c.name}{c.model?' · '+c.model:''}{!c.enabled||!c.available?' · 不可用':''}</option>)}</select></label>}
   {mode==='auto'&&!connectionId&&available.length>1&&<p className="agent-config-note">当前有 {available.length} 个可用文本连接，请选择首选模型后保存。</p>}
   {mode==='auto'&&!connectionId&&available.length===0&&<p className="agent-config-note">{isDatabase?'当前没有可用文本连接，请先配置、测试并启用模型。':isKnowledge?'当前没有可用文本连接，将提供原文检索并展示来源，未生成综合答案。':'当前没有可用文本连接，自动模式将使用本地确定性查询。'}</p>}
   {mode!=='local'&&connectionId&&!modelReady&&<p className="agent-config-note">所选连接不可用：{selected?.reason||(!selected?'连接已删除':!selected.enabled?'尚未启用':'需完成连接测试并检查隐私设置')}。</p>}
   {items.some(c=>!c.available||!c.enabled)&&<details className="agent-unavailable"><summary>查看连接可用情况</summary>{items.map(c=><p key={c.id}><strong>{c.name}</strong><span>{c.enabled&&c.available?'可用':c.reason||(!c.enabled?'尚未启用':'不可用')}</span></p>)}</details>}
   <p className="agent-config-scope"><ShieldCheck size={16}/><span>保存后，模型主问答会向所选服务发送当前问题、必要上下文、{isDatabase?'所选数据库的相关结构、受限样本和本轮查询结果':'结构化条件和本轮相关证据'}，并受“仅本机处理”设置限制；不会发送整库、全部会话或凭据。查询通过执行检查不代表单位或业务语义已经核验。临时问答继续在本机处理。</span></p>
   <div className="agent-config-footer"><span role="status">{saved?'设置已保存':changed?'有未保存修改':loading?'正在刷新可用状态':'设置已同步'}</span><button className="button primary" disabled={loading||saving||!changed||!sourceReady||(mode==='model'&&!modelReady)||(mode!=='local'&&!!connectionId&&!modelReady)||(isDatabase&&(mode==='local'||!modelReady&&!automaticModelReady))}>{saving?'保存中':'保存主问答方式'}</button></div>
  </form>}
 </section>
}

export function AgentSummary({agent,onProcess}: {agent?:AgentSnapshot;onProcess?:()=>void}){
 if(!agent?.mode)return null
 const model=agent.mode==='model',calls=Array.isArray(agent.calls)?agent.calls.length:agent.calls
 return <div className="agent-summary"><span className="agent-mode-badge"><Bot size={14}/>{model?'模型驱动':'本地确定性'}</span>{model&&agent.model&&<span className="agent-model-name">{agent.model}</span>}{calls!=null&&<span className="agent-call-count">{calls} 次模型调用</span>}{onProcess&&<button className="text-link" onClick={onProcess}>查看执行过程</button>}{model&&<small>结论与来源绑定情况请逐项核对。</small>}</div>
}

export function AgentTrace({agent,events=[],compact=false}: {agent?:AgentSnapshot;events?:AgentEvent[];compact?:boolean}){
 const plan=Array.isArray(agent?.plan)?agent.plan:[],usage=agent?.usage,review=agent?.review
 if(!events.length&&!plan.length&&!review&&!usage)return null
 return <div className={'agent-trace '+(compact?'is-compact':'')}>
  {plan.length>0&&<section className="agent-plan"><h4>本轮计划</h4><ol>{plan.map((step,index)=><li key={step.id||index}><StatusIcon status={step.status}/><span>{step.label||step.tool||'步骤 '+(index+1)}</span><small>{statusName(step.status)}</small></li>)}</ol></section>}
  {events.length>0&&<section className="agent-events"><h4>实际执行记录</h4><div className="process-list">{events.map((event,index)=><details key={event.step_id||event.id||index} className={'agent-event status-'+(event.status||'unknown')}><summary><StatusIcon status={event.status}/><strong>{event.label||event.tool||'步骤 '+(index+1)}</strong><span className="agent-event-status">{statusName(event.status)}</span><span className="agent-event-duration">{event.duration_ms!=null?num(event.duration_ms)+' ms':event.status==='running'?'计时中':''}</span></summary>{event.summary&&<p>{event.summary}</p>}{event.tool&&<p className="agent-event-tool">工具：<code>{event.tool}</code></p>}{(event.started_at||event.finished_at)&&<p className="agent-event-time">{event.started_at&&'开始 '+new Date(event.started_at).toLocaleTimeString('zh-CN')}{event.finished_at&&' · 结束 '+new Date(event.finished_at).toLocaleTimeString('zh-CN')}</p>}{!compact&&Object.keys(event.inputs||{}).length>0&&<details><summary>查看输入</summary><pre>{JSON.stringify(event.inputs,null,2)}</pre></details>}{!compact&&Object.keys(event.outputs||{}).length>0&&<details><summary>查看返回内容</summary><pre>{JSON.stringify(event.outputs,null,2)}</pre></details>}</details>)}</div></section>}
  {review&&<div className="agent-review"><strong>结论检查：{statusName(review.status)}</strong>{review.detail&&<p>{review.detail}</p>}<small>执行记录和引用绑定不替代对原始证据的核对。</small></div>}
  {!compact&&usage&&Object.keys(usage).length>0&&<details className="agent-usage"><summary>模型用量记录</summary><pre>{JSON.stringify(usage,null,2)}</pre></details>}
 </div>
}
