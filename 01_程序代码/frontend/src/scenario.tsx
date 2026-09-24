import {companionActivity} from './pet-state'
import React,{useEffect,useRef,useState} from 'react'
import {ArrowLeft,ArrowRight,ArrowUpRight,BarChart3,Check,CheckCircle2,Copy,Database,Download,FileText,FolderOpen,History,Lightbulb,LoaderCircle,MoreHorizontal,Pencil,RefreshCw,RotateCcw,Save,Trash2,Undo2} from 'lucide-react'
import {api,Obj,Chart,IconButton,Mascot,Modal,money,num,timeLabel,examples} from './ui'

const ORDER=['sales','growth','threshold','rate']
const labels:Obj={sales:'基础净销售额',growth:'增长率',threshold:'起奖门槛',rate:'奖金比例'}
const symbols:Obj={sales:'B',growth:'g',threshold:'T',rate:'r'}
const ratio=(k:string)=>k==='growth'||k==='rate'
const display=(v:any,k:string)=>ratio(k)?num(Number(v)*100)+'%':money(v)
const inputs=(p:Obj)=>Object.fromEntries(ORDER.map(k=>[k,ratio(k)?String(Number((Number(p[k].value)*100).toFixed(4))):String(p[k].value)]))
const inputParams=(v:Obj)=>Object.fromEntries(ORDER.map(k=>[k,{value:ratio(k)?(Number(v[k])/100).toFixed(6):v[k],unit:ratio(k)?'ratio':'CNY'}]))
const keyFor=(d:Obj)=>JSON.stringify([d.baselineId,d.mode,d.values])
const signed=(v:any)=>Number(v)>0?'+'+num(v):num(v)
const titleFor=(b:Obj)=>`${b.query_spec.year} 年${b.query_spec.region||'全部区域'} · ${b.query_spec.resolved_entity||b.query_spec.product||'销售'}奖金情景`
function validation(v:Obj){
 const errors:Obj={};const ranges:Obj={sales:[0,1e12],growth:[-100,200],threshold:[0,1e12],rate:[0,100]}
 ORDER.forEach(k=>{const n=Number(v[k]);if(!String(v[k]??'').trim()||!Number.isFinite(n))errors[k]='请输入有效数字';else if(n<ranges[k][0]||n>ranges[k][1])errors[k]=ratio(k)?`范围 ${ranges[k][0]}%–${ranges[k][1]}%`:'范围 0–1 万亿元';else if(Math.abs(n*10**(ratio(k)?4:2)-Math.round(n*10**(ratio(k)?4:2)))>.000001)errors[k]=ratio(k)?'最多四位小数':'最多两位小数'})
 return errors
}
function SectionTitle({n,children,extra}:{n:number,children:React.ReactNode,extra?:React.ReactNode}){return <div className="sim-section-title"><h3><span>{n}</span>{children}</h3>{extra}</div>}

export function ScenarioPage({result,run,busy,onEvidence,projectId,sessionId,notify,onOrigin,navigate,onConnection,onShare}:Obj){
 const storageKey='ef-scenario-draft-v1:'+projectId+':'+(sessionId||'new')
 const [draft,setDraft]=useState<Obj|null>(null),[base,setBase]=useState<Obj|null>(null),[calculated,setCalculated]=useState<Obj|null>(null)
 const [loading,setLoading]=useState(true),[error,setError]=useState(''),[calcError,setCalcError]=useState(''),[retry,setRetry]=useState(0),[saving,setSaving]=useState(false)
 const [dialog,setDialog]=useState(''),[percent,setPercent]=useState(false),[libraryTick,setLibraryTick]=useState(0),[pendingPlan,setPendingPlan]=useState<Obj|null>(null)
 const mounted=useRef(true),serial=useRef(0),latestDraft=useRef<Obj|null>(null),scroll=useRef(0)
 const commit=(d:Obj)=>{latestDraft.current=d;setDraft(d);try{localStorage.setItem(storageKey,JSON.stringify(d))}catch{notify('草稿暂未写入浏览器；请保存方案')}}
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
 const adopt=(b:Obj,p?:Obj)=>{
  setBase(b);setCalculated(null);setCalcError('');setError('')
  const values=p?inputs(Object.fromEntries(p.calculation.parameters.map((x:Obj)=>[x.name,x]))):inputs(b.rules.historical.parameters)
  const d:Obj={baselineId:b.id,values,mode:p?.calculation.rule_mode||'historical',name:p?.name||titleFor(b),planId:p?.id||null,revision:p?.revision||null,savedName:p?.name||null,undo:[],originRunId:b.origin_run_id}
  d.savedKey=p?.id?keyFor(d):null;commit(d)
 }
 const prepare=async(refresh=false)=>{
  if(!result?.formulas?.length&&!base)return
  const seq=++serial.current;setLoading(true);setError('')
  try{const b=await api('/api/scenarios/baselines',{run_id:refresh?base?.origin_run_id:result.run_id,project_id:projectId,session_id:refresh?base?.session_id:sessionId,refresh});if(mounted.current&&seq===serial.current){adopt(b);if(refresh)notify('已重新查询，建立新的基准草稿')}}catch(e){if(mounted.current&&seq===serial.current)setError((e as Error).message)}finally{if(mounted.current&&seq===serial.current)setLoading(false)}
 }
 useEffect(()=>{
  let cancelled=false
  const load=async()=>{let cached:Obj|null=null;try{cached=JSON.parse(localStorage.getItem(storageKey)||'null')}catch{}
   if(cached?.baselineId){try{const b=await api('/api/scenarios/baselines/'+cached.baselineId);if(!cancelled){if(b.project_id!==projectId)throw new Error('草稿与当前项目不一致');setBase(b);commit(cached);setLoading(false)}}catch(e){if(!cancelled){setError((e as Error).message);setLoading(false)}}}
   else if(result?.formulas?.length){await prepare()}else setLoading(false)
  };void load();return()=>{cancelled=true;serial.current++}
 },[])
 useEffect(()=>{if(!draft&&!loading&&result?.formulas?.length)void prepare()},[result?.run_id])
 const computeKey=draft?keyFor(draft):'';const errors=draft?validation(draft.values):{};const invalid=Object.keys(errors).length>0
 useEffect(()=>{
  if(!draft||invalid)return
  const controller=new AbortController();let cancelled=false;setCalcError('')
  const timer=window.setTimeout(async()=>{
   try{const response=await fetch('/api/scenarios/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({baseline_id:draft.baselineId,rule_mode:draft.mode,parameters:inputParams(draft.values)}),signal:controller.signal});const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'参数未通过检查');if(!cancelled){onConnection(true);setCalculated({key:computeKey,data});setBase(b=>b?{...b,stale:data.stale}:b)}}catch(e){if(!cancelled){if(e instanceof TypeError)onConnection(false);setCalcError(e instanceof TypeError?'未连接到本地服务，请恢复服务后重试':(e as Error).message)}}
  },300)
  return()=>{cancelled=true;window.clearTimeout(timer);controller.abort()}
 },[computeKey,retry,invalid])
 const calc=!invalid&&!calcError&&calculated?.key===computeKey?calculated.data:null
 const pending=!!draft&&!invalid&&!calcError&&!calc
 useEffect(()=>{companionActivity('scenario',{page:'scenario',status:pending?'running':calcError?'failed':'completed',stage:pending?'计算情景':'情景结果',scenario_id:calc?.id});return()=>companionActivity('scenario',null)},[pending,calcError,calc?.id])
 const changed=!!draft&&(draft.savedKey!==computeKey||draft.savedName!==draft.name)
 const edit=(values:Obj,mode=draft?.mode)=>{if(!draft)return;commit({...draft,values,mode,undo:[...(draft.undo||[]),{values:draft.values,mode:draft.mode}].slice(-20)})}
 const undo=()=>{if(!draft?.undo?.length)return;const previous=draft.undo[draft.undo.length-1];commit({...draft,...previous,undo:draft.undo.slice(0,-1)})}
 const switchRule=(mode:string)=>{if(!base||!draft)return;const p=base.rules[mode]?.parameters;if(p)edit({...draft.values,threshold:String(p.threshold.value),rate:String(Number(p.rate.value)*100)},mode)}
 const loadPlan=async(p:Obj)=>{try{const full=await api('/api/scenarios/plans/'+p.id);if(mounted.current){adopt(full.baseline,full);setDialog('');notify('已打开方案')}}catch(e){notify((e as Error).message)}}
 const save=async(copy=false)=>{
  if(!calc||!draft||saving)return
  const captured=draft,calculationId=calc.id;setSaving(true)
  try{const p=await api('/api/scenarios/plans'+(!copy&&captured.planId?'/'+captured.planId:''),{name:captured.name,calculation_id:calculationId,expected_revision:!copy?captured.revision:null},!copy&&captured.planId?'PUT':'POST')
   if(mounted.current&&latestDraft.current?.baselineId===captured.baselineId){commit({...latestDraft.current,planId:p.id,revision:p.revision,savedKey:keyFor(captured),savedName:captured.name});setLibraryTick(x=>x+1);notify(copy?'方案副本已保存':'方案已保存 · 第 '+p.revision+' 版')}
  }catch(e){notify((e as Error).message)}finally{if(mounted.current)setSaving(false)}
 }
 const origin=async()=>{try{await onOrigin(base)}catch(e){notify((e as Error).message)}}
 const focusParam=(key:string)=>document.getElementById('sim-input-'+key)?.focus()
 const exportCalc=()=>{if(!calc)return;const a=document.createElement('a');a.href='/api/scenarios/calculations/'+calc.id+'/export';a.download='EvidenceFlow-scenario.json';a.click()}
 useEffect(()=>{const main=document.querySelector('.content');const saved=Number(sessionStorage.getItem(storageKey+':scroll')||0);if(main){main.scrollTop=saved;scroll.current=saved}const track=()=>{scroll.current=main?.scrollTop||window.scrollY;sessionStorage.setItem(storageKey+':scroll',String(scroll.current))};main?.addEventListener('scroll',track);window.addEventListener('scroll',track);return()=>{main?.removeEventListener('scroll',track);window.removeEventListener('scroll',track)}},[loading])
 const rules=base?.rules,params=rules?.historical?.parameters
 const canSave=!!calc&&!saving&&!!draft?.name?.trim()&&!loading
 return <div className="sim-workbench page-enter">
  <header className="sim-hero"><img className="sim-hero-icon" src="/glass-05-formula.webp" alt=""/><div><h1><span>公式计算</span><span>与情景模拟</span></h1><p>奖金方案</p></div><img className="sim-handwriting" src="/handwriting.webp" alt="好问题，从证据开始。"/><Mascot pose="pointing" className="sim-hero-pet"/></header>
  <section className="sim-canvas">
   <header className="sim-toolbar"><button className="button sim-back" aria-label="返回分析" title="返回分析" onClick={()=>base?void origin():navigate('analysis')}><ArrowLeft size={16}/><span>返回分析</span></button><div className="sim-title"><h2>{draft?.name||'新建情景'}{draft&&<IconButton label="修改方案名称" onClick={()=>setDialog('rename')}><Pencil size={15}/></IconButton>}</h2><div className="sim-meta"><FolderOpen size={13}/><span>{base?`${base.query_spec.year} 年 · ${base.query_spec.region||'全部区域'}`:'选择含奖金公式的分析'}</span>{draft&&<><span className="sim-meta-divider"/><span>{draft.planId?`第 ${draft.revision} 版 · `:''}{changed?'草稿未保存':'已保存'}</span></>}</div></div><div className="sim-toolbar-actions">{calc&&<button className="button" onClick={()=>onShare(calc.id)}>分享</button>}<button className="button" onClick={()=>setDialog('library')}><FolderOpen size={16}/><span>我的方案</span></button>{draft&&<><IconButton label="方案操作" disabled={loading||saving} onClick={()=>setDialog('more')}><MoreHorizontal size={20}/></IconButton><button className="button primary" disabled={!canSave} title={canSave?'保存参数、来源与结果':invalid?'请修正参数':pending?'等待计算完成':loading?'正在读取基准':'暂无有效计算结果'} onClick={()=>void save()}>{saving?<LoaderCircle size={16} className="spin"/>:<Save size={16}/>}<span>{saving?'保存中':'保存方案'}</span></button></>}</div></header>
   {loading&&<div className="sim-placeholder" role="status"><LoaderCircle className="spin"/>正在读取基准</div>}
   {error&&<div className="error-banner" role="alert"><span>{error}</span><button className="button" onClick={()=>void prepare()}>重新载入</button></div>}
   {!draft&&!loading&&!error&&<div className="sim-placeholder"><Mascot pose="reading"/><h2>选择一项奖金分析</h2><button className="button primary" disabled={busy} onClick={()=>run(examples[0],{},false,'scenario')}>{busy?<LoaderCircle className="spin" size={17}/>:<ArrowRight size={17}/>}载入华东奖金</button></div>}
   {draft&&base&&params&&!loading&&<>
    {base.stale&&<div className="sim-notice"><History size={16}/><span>数据或规则已更新，当前按保存版本计算。</span><button className="text-link" onClick={()=>setDialog('refresh')}>刷新基准</button></div>}
    {result?.formulas?.length&&base.origin_run_id!==result.run_id&&<div className="sim-notice"><span>分析中有另一项计算。</span><button className="text-link" onClick={()=>setDialog('replace')}>使用当前分析</button></div>}
    <div className="sim-grid">
     <div className="sim-left">
      <section className="sim-block sim-formula"><SectionTitle n={1}>计算公式</SectionTitle><div className="sim-equation"><span>奖金 = max(0, </span><button onClick={()=>focusParam('sales')} title="基础净销售额">B</button><span> × (1 + </span><button onClick={()=>focusParam('growth')} title="增长率">g</button><span>) − </span><button onClick={()=>focusParam('threshold')} title="起奖门槛">T</button><span>) × </span><button onClick={()=>focusParam('rate')} title="奖金比例">r</button></div><div className="sim-rule"><label>规则<select aria-label="情景规则" value={draft.mode} onChange={e=>switchRule(e.target.value)}><option value="historical">交易当时规则</option><option value="current" disabled={!rules.current}>当前规则模拟{!rules.current?' · 不可用':''}</option></select></label><span>{rules[draft.mode]?.rule_name} · {rules[draft.mode]?.effective_from} 起</span></div>{!rules.current&&<p className="sim-warning">{base.current_rule_gap}</p>}</section>
      <section className="sim-block sim-parameters"><SectionTitle n={2} extra={<div className="inline-actions"><IconButton label="撤销参数修改" disabled={!draft.undo?.length} onClick={undo}><Undo2 size={16}/></IconButton><button className="text-link" onClick={()=>edit(inputs(params),'historical')}>恢复基准</button></div>}>参数设置</SectionTitle>
       <div className="sim-table-scroll" tabIndex={0} aria-label="情景参数表"><table className="sim-parameter-table"><thead><tr><th>参数</th><th>符号</th><th>基准</th><th>情景</th><th>单位</th><th>基准来源</th><th>说明</th></tr></thead><tbody>{ORDER.map(k=>{
        const p=params[k],reference=rules[draft.mode].parameters[k],value=Number(draft.values[k])/(ratio(k)?100:1),assumption=value!==Number(reference.value),diff=value!==Number(p.value)
        const source=rules[draft.mode].evidence.find((e:Obj)=>reference.evidence_ids?.includes(e.id))
        return <tr key={k} className={errors[k]?'sim-invalid':diff?'sim-changed':''}><th scope="row">{labels[k]}</th><td className="sim-symbol">{symbols[k]}</td><td className="sim-baseline-number">{ratio(k)?num(Number(p.value)*100)+'%':num(p.value)}</td><td><div className="sim-number-field"><input id={'sim-input-'+k} aria-label={labels[k]+'情景值'} aria-invalid={!!errors[k]} aria-describedby={errors[k]?'sim-error-'+k:undefined} inputMode="decimal" value={draft.values[k]} onChange={e=>edit({...draft.values,[k]:e.target.value})}/>{ratio(k)&&<span>%</span>}</div>{k==='growth'&&<input type="range" aria-label="增长率滑块" min="-100" max="200" step="1" value={Number.isFinite(Number(draft.values[k]))?Number(draft.values[k]):0} onChange={e=>edit({...draft.values,growth:e.target.value})}/ >}{errors[k]&&<small id={'sim-error-'+k} className="error-text">{errors[k]}</small>}</td><td>{ratio(k)?'%':'元'}</td><td>{source?<button className="sim-source" onClick={()=>onEvidence(source)} title={source.source}>{source.type==='sql_result'?<Database size={15}/>:<FileText size={15}/>}<span>{k==='sales'?'销售数据':rules[draft.mode].rule_name}<small>{k==='sales'?base.query_spec.year+' 年':'第 '+(source.locator?.page||1)+' 页'}</small></span></button>:<span className="sim-quiet">不作增长调整</span>}</td><td><span className={'sim-param-kind '+(assumption?'assumption':'')}>{assumption?'手动假设':draft.mode==='current'&&diff?'规则模拟':'沿用基准'}</span>{ratio(k)&&diff&&!errors[k]&&<small className="sim-points">{signed((value-Number(p.value))*100)} 个百分点</small>}</td></tr>
       })}</tbody></table></div><footer className="sim-parameter-footer"><span><CheckCircle2 size={14}/> 元 × 比例 = 元</span><span role="status">{invalid?'请修正参数':pending?'正在更新':calcError?'计算失败':'参数已检查'}</span></footer>
      </section>
      <section className="sim-block sim-explanation"><SectionTitle n={5}>变化说明</SectionTitle>{calc?<><div className="sim-contributions">{calc.contributions.map((x:Obj)=>{const p=calc.parameters.find((p:Obj)=>p.name===x.name);return <div key={x.name}><span className="sim-contribution-icon"><BarChart3 size={19}/></span><div><h4>{x.label}</h4><strong>{signed(x.delta)} 元</strong><p>{p.changed?display(p.baseline_value,x.name)+' → '+display(p.value,x.name):'保持不变'}</p></div></div>})}</div><details><summary>分项计算与舍入</summary><p>{calc.contribution_note}</p><p>{calc.rounding}</p><p>合计 {signed(calc.delta)} 元</p>{calc.parameters.filter((p:Obj)=>p.assumption).map((p:Obj)=><p key={p.name}>{p.label} · {p.modified_by} · {timeLabel(p.modified_at)}</p>)}</details></>:<p className="sim-quiet">{invalid?'参数修正后显示变化':'计算完成后显示变化'}</p>}</section>
     </div>
     <div className="sim-right" aria-busy={pending}>
      <section className="sim-block sim-results"><SectionTitle n={3} extra={<div className="sim-segment"><button aria-pressed={!percent} onClick={()=>setPercent(false)}>数值</button><button aria-pressed={percent} onClick={()=>setPercent(true)}>百分比</button></div>}>计算结果</SectionTitle><div className="sim-results-meta"><span>{percent?(calc?.relative_percent===null?'基准为 0 · 差额以元计':'基准 = 100% · 差额以元计'):'人民币 · 元'}</span><span className="sim-hypothesis">情景假设</span></div>
       {calc?<><div className="sim-metrics"><div><h4><img src="/glass-06-baseline.webp" alt=""/>基准结果</h4><strong>{percent?(Number(calc.baseline_value)?'100%':'—'):num(calc.baseline_value)}</strong><span>交易当时规则</span></div><div><h4><img src="/glass-07-scenario.webp" alt=""/>情景结果</h4><strong>{percent?(calc.relative_percent===null?'—':num(100+Number(calc.relative_percent))+'%'):num(calc.value)}</strong><span>调整后的奖金</span></div></div><div className={'sim-delta '+(Number(calc.delta)<0?'negative':'')}><ArrowUpRight size={19}/><strong>{signed(calc.delta)} 元</strong><span>{calc.relative_percent===null?'基准为 0，变化率不适用':signed(calc.relative_percent)+'%'}</span></div></>:<div className="sim-pending" role="status">{pending?<><LoaderCircle className="spin" size={21}/>正在更新结果</>:invalid?'请修正参数后查看结果':<><span>{calcError}</span><button className="button" onClick={()=>{setCalculated(null);setCalcError('');setRetry(x=>x+1)}}>重试计算</button></>}</div>}
      </section>
      <section className="sim-block sim-chart"><SectionTitle n={4}>结果对比</SectionTitle>{calc&&(!percent||calc.relative_percent!==null)?<Chart height={200} labels={['基准奖金','情景奖金']} values={percent?[100,100+Number(calc.relative_percent)]:[Number(calc.baseline_value),Number(calc.value)]} unit={percent?'%':'元'} label={percent?'以基准为100%的奖金对比':'奖金对比'} selectedIndex={1}/>:<div className="sim-chart-empty">{percent&&calc?.relative_percent===null?'基准为 0，百分比对比不适用':'等待有效计算结果'}</div>}<div className="sim-chart-legend"><span>基准 · 当时规则</span><span>情景 · {draft.mode==='current'?'当前规则模拟':'当时规则'}</span></div></section>
      <section className="sim-insight"><Lightbulb size={25}/><div><h3>情景结论</h3>{calc?<p>在上述假设下，净销售额为 <strong>{money(calc.adjusted_sales)}</strong>，奖金为 <strong>{money(calc.value)}</strong>，较基准{Number(calc.delta)>0?'增加':Number(calc.delta)<0?'减少':'不变'}{Number(calc.delta)!==0&&<strong> {money(Math.abs(Number(calc.delta)))}</strong>}。</p>:<p>{invalid?'请先修正参数。':calcError?'计算未完成，请重试。':'等待本次计算完成。'}</p>}<button className="text-link" disabled={!calc} onClick={exportCalc}><Download size={14}/>导出计算与来源</button></div></section>
     </div>
    </div>
   </>}
  </section>
  {dialog==='open-plan'&&pendingPlan&&<Modal title="打开方案" close={()=>setDialog('library')}><p>当前草稿尚未保存。打开方案会替换草稿，已保存的方案会保留。</p><div className="modal-actions"><button className="button" onClick={()=>setDialog('library')}>取消</button><button className="button primary" onClick={()=>void loadPlan(pendingPlan)}>打开方案</button></div></Modal>}
  {dialog==='rename'&&draft&&<RenameDialog name={draft.name} close={()=>setDialog('')} save={(name:string)=>{commit({...draft,name});setDialog('')}}/>}
  {dialog==='library'&&<PlanLibrary key={libraryTick} projectId={projectId} close={()=>setDialog('')} open={(p:Obj)=>{if(changed&&draft){setPendingPlan(p);setDialog('open-plan')}else void loadPlan(p)}} notify={notify}/>}
  {dialog==='more'&&draft&&<Modal title="方案操作" close={()=>setDialog('')}><div className="sim-menu"><button disabled={!canSave} onClick={()=>{setDialog('');void save(true)}}><Copy size={18}/>另存副本</button><button disabled={!draft.planId} title={draft.planId?'查看保存版本':'先保存方案'} onClick={()=>setDialog('versions')}><History size={18}/>查看版本</button><button onClick={()=>{edit(inputs(params),'historical');setDialog('')}}><RotateCcw size={18}/>恢复基准参数</button><button disabled={!calc} onClick={()=>{exportCalc();setDialog('')}}><Download size={18}/>导出计算与来源</button><button onClick={()=>setDialog('refresh')}><RefreshCw size={18}/>重新查询基准</button></div></Modal>}
  {dialog==='versions'&&draft?.planId&&<Versions id={draft.planId} close={()=>setDialog('')} open={(p:Obj)=>{p.id=null;p.revision=null;adopt(p.baseline,p);setDialog('');notify('历史版本已打开为新草稿')}}/>}
  {(dialog==='refresh'||dialog==='replace')&&<Modal title={dialog==='refresh'?'刷新基准':'使用当前分析'} close={()=>setDialog('')}><p>将建立新的情景草稿，已保存的方案和版本会保留。{changed?'当前草稿尚未保存。':''}</p><div className="modal-actions"><button className="button" onClick={()=>setDialog('')}>取消</button><button className="button primary" onClick={()=>{const refresh=dialog==='refresh';setDialog('');void prepare(refresh)}}>建立新基准</button></div></Modal>}
 </div>
}

function RenameDialog({name,save,close}:Obj){
 const [value,setValue]=useState(name),[working,setWorking]=useState(false),[error,setError]=useState('')
 return <Modal title="方案名称" close={close}><form onSubmit={async e=>{e.preventDefault();if(!value.trim()||working)return;setWorking(true);try{await save(value.trim())}catch(e){setError((e as Error).message)}finally{setWorking(false)}}}><label className="form-field">名称<input aria-label="方案名称" value={value} onChange={e=>setValue(e.target.value)} maxLength={120} required/></label>{error&&<p className="error-text">{error}</p>}<div className="modal-actions"><button className="button" type="button" onClick={close}>取消</button><button className="button primary" disabled={!value.trim()||working}>{working?'保存中':'确定'}</button></div></form></Modal>
}
function PlanLibrary({projectId,close,open,notify}:Obj){
 const [list,setList]=useState<Obj[]>([]),[deleted,setDeleted]=useState(false),[loading,setLoading]=useState(true),[error,setError]=useState(''),[working,setWorking]=useState(''),[rename,setRename]=useState<Obj|null>(null),[revision,setRevision]=useState(0)
 const requestSequence=useRef(0)
 const load=async()=>{const seq=++requestSequence.current;setLoading(true);try{const data=await api('/api/scenarios/plans?project_id='+encodeURIComponent(projectId)+'&deleted='+deleted);if(seq===requestSequence.current){setList(data);setError('')}}catch(e){if(seq===requestSequence.current)setError((e as Error).message)}finally{if(seq===requestSequence.current)setLoading(false)}}
 useEffect(()=>{void load();return()=>{requestSequence.current++}},[deleted,revision])
 const mutate=async(p:Obj)=>{setWorking(p.id);try{await api('/api/scenarios/plans/'+p.id+(deleted?'/restore':''),{},deleted?'POST':'DELETE');setRevision(x=>x+1);notify(deleted?'方案已恢复':'已移入回收区，可恢复')}catch(e){setError((e as Error).message)}finally{setWorking('')}}
 if(rename)return <RenameDialog name={rename.name} close={()=>setRename(null)} save={async(name:string)=>{await api('/api/scenarios/plans/'+rename.id,{name,expected_revision:rename.revision},'PATCH');setRename(null);setRevision(x=>x+1)}}/>
 return <Modal title="我的方案" close={close} wide><div className="sim-library-toolbar"><div className="tabs"><button className={!deleted?'active':''} onClick={()=>setDeleted(false)}>已保存</button><button className={deleted?'active':''} onClick={()=>setDeleted(true)}>回收区</button></div></div>{error&&<p className="error-text" role="alert">{error}<button className="text-link" onClick={()=>void load()}>重试</button></p>}{loading?<p role="status">正在读取方案</p>:!list.length?<p className="sim-library-empty">{deleted?'回收区为空':'当前项目还没有保存的方案'}</p>:<div className="sim-plan-list">{list.map(p=><div key={p.id}><button className="sim-plan-open" disabled={deleted} onClick={()=>open(p)}><span className="sim-plan-icon"><FileText size={21}/></span><span><strong>{p.name}</strong><small>第 {p.revision} 版 · {timeLabel(p.updated_at)}{p.stale?' · 来源已更新':''}</small></span><span className="sim-plan-value">{money(p.value)}<small>基准 {money(p.baseline_value)}</small></span></button><div className="inline-actions">{!deleted&&<IconButton label={'重命名 '+p.name} onClick={()=>setRename(p)}><Pencil size={16}/></IconButton>}<IconButton label={(deleted?'恢复 ':'删除 ')+p.name} disabled={working===p.id} onClick={()=>void mutate(p)}>{deleted?<RotateCcw size={16}/>:<Trash2 size={16}/>}</IconButton></div></div>)}</div>}</Modal>
}
function Versions({id,close,open}:Obj){const [list,setList]=useState<Obj[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false);useEffect(()=>{api('/api/scenarios/plans/'+id+'/versions').then(setList).catch(e=>setError(e.message))},[id]);return <Modal title="方案版本" close={close}><p className="muted">打开历史版本会建立新草稿。</p>{error&&<p className="error-text">{error}</p>}<div className="sim-version-list">{list.map(p=><button className="button" key={p.revision} disabled={busy} onClick={async()=>{setBusy(true);try{open(await api('/api/scenarios/plans/'+id+'/versions/'+p.revision))}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}><History size={16}/><span>第 {p.revision} 版 · {p.name}<small>{timeLabel(p.created_at)}</small></span><ArrowRight size={16}/></button>)}</div></Modal>}
