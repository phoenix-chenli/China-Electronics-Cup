import React,{useEffect,useRef,useState} from 'react'
import {AlertCircle,Bot,CheckCircle2,Clock3,LoaderCircle,RefreshCw,ShieldCheck,XCircle} from 'lucide-react'
import {api,Obj,num} from './ui'
import './agent.css'

type AgentMode='auto'|'local'|'model'
type AgentConnection={id:string;name:string;model?:string;enabled:boolean;available:boolean;reason?:string}
type AgentConfig={mode:AgentMode;connection_id:string|null;max_steps:number;connections:AgentConnection[];available:boolean}
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
 const [loading,setLoading]=useState(true),[saving,setSaving]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState(false)
 const serial=useRef(0),dirty=useRef(false)
 const load=async()=>{
  const request=++serial.current;setLoading(true);setError('')
  try{const data:AgentConfig=await api('/api/agent/config');if(request!==serial.current)return;setConfig(data);if(!dirty.current){setMode(data.mode);setConnectionId(data.connection_id||'')}}
  catch(e){if(request===serial.current)setError((e as Error).message)}
  finally{if(request===serial.current)setLoading(false)}
 }
 useEffect(()=>{void load();const changed=()=>void load();window.addEventListener('ef-connections-changed',changed);return()=>{serial.current++;window.removeEventListener('ef-connections-changed',changed)}},[refreshKey])
 const items=config?.connections||[],available=items.filter(c=>c.enabled&&c.available),selected=items.find(c=>c.id===connectionId)
 const modelReady=!!selected?.enabled&&!!selected?.available
 const changed=!!config&&(mode!==config.mode||(mode==='local'?null:connectionId||null)!==config.connection_id)
 const change=(nextMode:AgentMode,nextId=connectionId)=>{dirty.current=true;setMode(nextMode);setConnectionId(nextId);setSaved(false);setError('')}
 const save=async(e:React.FormEvent)=>{
  e.preventDefault();if(!config||saving||loading)return
  setSaving(true);setError('');setSaved(false)
  try{await api('/api/agent/config',{mode,connection_id:mode==='local'?null:connectionId||null},'PUT');dirty.current=false;await load();setSaved(true);notify?.('主问答方式已保存');updated?.();window.dispatchEvent(new Event('ef-agent-config-changed'))}
  catch(e){setError((e as Error).message)}finally{setSaving(false)}
 }
 return <section className="agent-config pref-card" aria-labelledby="agent-config-title">
  <div className="agent-config-heading"><h3 id="agent-config-title"><Bot size={22}/>主问答方式</h3><button type="button" className="button small" disabled={loading||saving} onClick={()=>void load()}><RefreshCw size={14}/>刷新可用状态</button></div>
  <p className="muted">选择后用于新的普通问答与追问。已有结果保留当时使用的方式。</p>
  {error&&<p className="error-banner" role="alert">{error}</p>}
  {!config?<p role="status">{loading?'正在读取主问答设置':'主问答设置未读取，请重试。'}</p>:<form onSubmit={save}>
   <fieldset className="agent-mode-options" disabled={loading||saving}><legend>处理方式</legend>{(['auto','local','model'] as AgentMode[]).map(value=><label key={value} className={mode===value?'selected':''}><input type="radio" name="agent-mode" value={value} checked={mode===value} onChange={()=>change(value)}/><span><strong>{modeNames[value]}</strong><small>{value==='auto'?'按可用连接选择模型，未配置时使用本地查询':value==='local'?'使用现有销售示例的规则与 SQL':'使用选定模型规划并调用工具，失败时明确报错'}</small></span></label>)}</fieldset>
   {mode!=='local'&&<label className="form-field agent-model-picker">{mode==='auto'?'首选文本模型':'主问答文本模型'}<select aria-label="主问答文本模型" value={connectionId} disabled={loading||saving} onChange={e=>change(mode,e.target.value)}><option value="">{mode==='auto'?'自动选择唯一可用连接':'请选择可用模型连接'}</option>{connectionId&&!selected&&<option value={connectionId} disabled>原连接已删除，请重新选择</option>}{items.map(c=><option key={c.id} value={c.id} disabled={!c.enabled||!c.available}>{c.name}{c.model?' · '+c.model:''}{!c.enabled||!c.available?' · 不可用':''}</option>)}</select></label>}
   {mode==='auto'&&!connectionId&&available.length>1&&<p className="agent-config-note">当前有 {available.length} 个可用文本连接，请选择首选模型后保存。</p>}
   {mode==='auto'&&!connectionId&&available.length===0&&<p className="agent-config-note">当前没有可用文本连接，自动模式将使用本地确定性查询。</p>}
   {mode!=='local'&&connectionId&&!modelReady&&<p className="agent-config-note">所选连接不可用：{selected?.reason||(!selected?'连接已删除':!selected.enabled?'尚未启用':'需完成连接测试并检查隐私设置')}。</p>}
   {items.some(c=>!c.available||!c.enabled)&&<details className="agent-unavailable"><summary>查看连接可用情况</summary>{items.map(c=><p key={c.id}><strong>{c.name}</strong><span>{c.enabled&&c.available?'可用':c.reason||(!c.enabled?'尚未启用':'不可用')}</span></p>)}</details>}
   <p className="agent-config-scope"><ShieldCheck size={16}/><span>保存后，模型主问答会向所选服务发送当前问题、结构化条件和本轮相关证据，并受“仅本机处理”设置限制；不会发送整库、全部会话或凭据。模型生成的结论仍需核对来源。临时问答继续在本机处理。</span></p>
   <div className="agent-config-footer"><span role="status">{saved?'设置已保存':changed?'有未保存修改':loading?'正在刷新可用状态':'设置已同步'}</span><button className="button primary" disabled={loading||saving||!changed||(mode==='model'&&!modelReady)||(mode!=='local'&&!!connectionId&&!modelReady)}>{saving?'保存中':'保存主问答方式'}</button></div>
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
