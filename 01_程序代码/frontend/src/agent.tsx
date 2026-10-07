import React,{useEffect,useRef,useState} from 'react'
import {AlertCircle,Bot,CheckCircle2,Clock3,LoaderCircle,RefreshCw,ShieldCheck,XCircle} from 'lucide-react'
import {api,Obj,num} from './ui'
import './agent.css'

type AgentMode='auto'|'local'|'model'
type QueryScope='sales'|'database'|'knowledge'|'hybrid'
type AgentConnection={id:string;name:string;model?:string;enabled:boolean;available:boolean;reason?:string}
type AgentDatabase={id:string;name:string;kind:string;available:boolean;reason?:string}
type AgentConfig={mode:AgentMode;connection_id:string|null;max_steps:number;connections:AgentConnection[];available:boolean;query_scope?:QueryScope;database_connection_id?:string|null;databases?:AgentDatabase[]}
type AgentPlanStep={id?:string;label?:string;tool?:string;status?:string}
export type AgentSnapshot={mode?:'model'|'local';connection_id?:string;model?:string;plan?:AgentPlanStep[];review?:{status?:string;detail?:string};usage?:Obj;calls?:number|Obj[]}
export type AgentEvent={step_id?:string;id?:string;label?:string;tool?:string;status?:string;summary?:string;duration_ms?:number;started_at?:string;finished_at?:string;inputs?:Obj;outputs?:Obj}

const modeNames:Record<AgentMode,string>={auto:'自动选择',local:'本地确定性查询',model:'指定模型驱动'}
const statusNames:Record<string,string>={pending:'待执行',planned:'待执行',queued:'等待中',running:'进行中',completed:'已完成',failed:'失败',cancelled:'已取消',cancelling:'正在取消',skipped:'已跳过',passed:'通过',needs_evidence:'待补充证据',needs_review:'待复核',needs_clarification:'待确认条件',local:'本地规则检查'}
const statusName=(status?:string)=>status?statusNames[status]||'状态待确认':'未记录状态'

function StatusIcon({status}: {status?:string}){
 if(status==='running'||status==='cancelling')return <LoaderCircle size={16} className="spin"/>
 if(status==='completed'||status==='passed')return <CheckCircle2 size={16}/>
 if(status==='failed')return <AlertCircle size={16}/>
 if(status==='cancelled')return <XCircle size={16}/>
 return <Clock3 size={16}/>
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
