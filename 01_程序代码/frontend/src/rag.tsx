import React,{useEffect,useState} from 'react'
import {AlertCircle,BookOpen,CheckCircle2,Download,FileText,LoaderCircle,RefreshCw,ShieldCheck} from 'lucide-react'
import {api,Obj,Modal,ListPagination} from './ui'
import {sourcePosition} from './result-presentation'
import './rag.css'
import {ModelPackageStatus} from './agent'

const indexNames:Record<string,string>={pending:'等待建立',indexing:'建立中',lexical_ready:'关键词就绪',ready:'混合检索就绪',degraded:'检索降级',failed:'建立失败',cancelled:'已取消',interrupted:'已中断'}
const answerNames:Record<string,string>={supported:'已生成有依据的回答',partial:'仅回答有依据的部分',refused:'证据不足，未生成确定结论',insufficient_evidence:'现有资料不足以给出确定值',needs_clarification:'需要确认问题范围',failed:'本轮查询未完成'}
export const isKnowledgeResult=(result?:Obj)=>!!result?.rag||['knowledge','hybrid'].includes(result?.query_spec?.query_scope)
const notes=(values:unknown):string[]=>Array.isArray(values)?values.map(v=>typeof v==='string'?v:typeof v==='object'&&v!==null?String((v as Obj).detail||(v as Obj).requirement||(v as Obj).text||''):String(v)).filter(Boolean):[]

export function latestRetrieval(result:Obj):Obj|null{
 const records=result.rag?.retrieval
 return Array.isArray(records)?records[records.length-1]||null:records&&typeof records==='object'?records:null
}

export function RetrievalStatus({result}:Obj){
 const record=latestRetrieval(result)
 if(!record)return null
 const channels=record.channels||{},has=(key:string)=>Object.prototype.hasOwnProperty.call(channels,key),mode=record.mode||(channels.selection?'selection':has('bm25')&&has('dense')?'hybrid':has('bm25')?'lexical':has('dense')?'dense':'')
 const label=({hybrid:'关键词与向量',lexical:'仅关键词',dense:'仅向量',selection:'固定选中原文'} as Obj)[mode]
 const errors=Array.isArray(record.errors)?record.errors.map((item:unknown)=>typeof item==='string'?item:typeof item==='object'&&item!==null?String((item as Obj).error||''):String(item)).filter(Boolean):[]
 const gaps=Array.isArray(result.rag?.source_gaps)?result.rag.source_gaps:[]
 return <div className="rag-retrieval-status">{label&&<small>本轮检索：{label}</small>}{typeof record.reranked==='boolean'&&mode!=='selection'&&<small>候选重排：{record.reranked?'实际完成':'未完成，沿用召回排序'}</small>}{gaps.length>0&&<details><summary>有 {gaps.length} 处来源缺失或结构待复核</summary>{gaps.map((gap:Obj,i:number)=><p key={i}>{gap.document_name||'文档'} · 第 {gap.page} 页：{gap.reason}。{gap.instruction}</p>)}</details>}{errors.length>0&&<details><summary>本轮检索降级说明</summary>{errors.map((error:string,i:number)=><p key={i}>{error}</p>)}</details>}</div>
}

export function documentQuestionScope(mode:string,document:Obj,page:number,selection:Obj|null=null):Obj{
 if(mode==='document'||mode==='corpus'){
  if(!document.is_current)throw new Error('历史版本仅支持当前页或选区提问，请先打开当前版本。')
  return {rag_scope:{scope_type:mode,...(mode==='document'?{document_ids:[document.document_id]}:{})}}
 }
 if(!['focused','page'].includes(mode))throw new Error('提问范围无效，请重新选择。')
 return {document_id:document.document_id,sha256:document.sha256,parse_id:document.parse_id,page,
  ...(mode==='focused'&&selection?{text_range:{start:selection.start,end:selection.end},quote:selection.text}:{})}
}

export function RagCompleteness({result}:Obj){
 const coverage=result.rag?.completeness
 if(!coverage||!Array.isArray(coverage.requirements)||!Number.isInteger(coverage.total)||coverage.total<=0)return null
 const rows:Obj[]=coverage.requirements,repair=result.rag?.completion_repair
 return <div className="rag-completeness"><strong>逐项回答：{coverage.answered} / {coverage.total}</strong><small>依据模型支持检查统计，仍需核对原文；不代表事实准确率。</small><details open={!coverage.complete}><summary>{coverage.complete?'查看已回答分项':'查看已回答与未解决分项'}</summary><ul>{rows.map((row:Obj)=><li key={row.id}><span className={'badge '+(row.answer_status==='answered'?'green':'amber')}>{row.answer_status==='answered'?'已回答':row.answer_status==='conflicted'?'证据冲突':row.source_status==='supported'?'已有证据，尚未答出':'仍缺证据'}</span> {row.text}{row.answer_status!=='answered'&&row.reason&&<p>{row.reason}</p>}</li>)}</ul></details>{repair&&<small>本轮已尝试{repair.action==='retrieve'?'定向补查并补写':'依据已有证据补写'}一次；{coverage.complete?'分项已覆盖。':'仍有未解决分项。'}</small>}</div>
}

export function RagOutcome({result,onEvidence}:Obj){
 const status=result.answer_status==='supported'&&!result.claims?.length?'refused':result.answer_status||(!result.claims?.length?'refused':'supported'),missing=notes(result.missing_requirements),conflicts=notes(result.conflicts)
 const local=result.agent?.mode==='local',caution=status!=='supported'||local,text=result.refusal_reason||(['refused','failed','insufficient_evidence'].includes(status)?result.answer:!result.claims?.length?result.answer:'')
 const evidence:Obj[]=result.evidence||[],related=['refused','partial','insufficient_evidence'].includes(status)||!result.claims?.length
 return <section className={'rag-outcome '+(caution?'is-caution':'')} aria-label="本轮证据回答状态" role="status"><div className="rag-outcome-heading">{caution?<AlertCircle size={18}/>:<CheckCircle2 size={18}/>}<strong>{local?'原文检索，未生成综合答案':answerNames[status]||'本轮回答状态待确认'}</strong></div>{text&&<p className="preserve-text">{text}</p>}{missing.length>0&&<div><h4>仍缺少的信息</h4><ul>{missing.map((x,i)=><li key={i}>{x}</li>)}</ul></div>}{conflicts.length>0&&<div><h4>资料中的冲突</h4><ul>{conflicts.map((x,i)=><li key={i}>{x}</li>)}</ul></div>}{related&&evidence.length>0&&<details><summary>可查看的相关{evidence.some(e=>e.type!=='document')?'依据':'原文'} · {evidence.length} 处</summary><p className="muted">相关材料供核对，不能据此补出尚未获支持的结论。</p><div className="rag-related">{evidence.map(e=><button type="button" className="text-link" key={e.id} onClick={()=>onEvidence?.(e)}><FileText size={14}/>{e.source}{e.locator?.page?' · '+sourcePosition(e.locator):''}</button>)}</div></details>}<RagCompleteness result={result}/><RetrievalStatus result={result}/></section>
}

export function RagScopeNote({result}:Obj){
 const scope=result.rag?.scope||result.query_spec?.rag_scope
 if(!scope)return null
 const documents:Obj[]=scope.documents||[],kind=scope.scope_type||scope.type
 const label=({selection:'选中片段',page:'当前页',document:'整份文档',corpus:'所选知识库文档'} as Obj)[kind]||'本轮文档范围'
 const names=documents.map(d=>d.name||result.evidence?.find((e:Obj)=>e.locator?.document_id===d.document_id)?.source||'所选资料')
 const description=documents.length>3?documents.length+' 份文档':names.join('、')
 return <div className="document-scope-note"><BookOpen size={15}/><span>{label}{description?' · '+description:scope.document_ids?.length?' · '+scope.document_ids.length+' 份文档':''}{scope.page?' · 第 '+scope.page+' 页':''}</span>{documents.length>0&&<details><summary>查询时的版本</summary>{documents.map((d,i)=><p key={d.document_id||i}>{names[i]} · {(d.sha256||'').slice(0,12)}{d.parse_id?' · 解析 '+d.parse_id:''}</p>)}</details>}</div>
}

export function SemanticReview({claim}:Obj){
 const check=claim?.semantic_check
 if(!check)return null
 const status=typeof check==='string'?check:check.status,detail=typeof check==='object'?check.detail||check.reason:'',model=typeof check==='object'?check.model:''
 return <div className="rag-semantic-review"><span className={'badge '+(status==='supported'?'green':'amber')}>模型支持检查：{({supported:'有支持',unsupported:'支持不足',contradicted:'存在矛盾',unknown:'未能判断'} as Obj)[status]||'未核验'}</span>{model&&<small>{model}</small>}{detail&&<p>{detail}</p>}<small>这是模型对引用支持度的检查，原文定位另行核对。</small></div>
}

export function IndexStatus({item}:Obj){
 const status=item?.status,label=status?indexNames[status]||'索引状态待确认':'未建索引',active=status==='pending'||status==='indexing'
 const hasCount=Number.isInteger(item?.processed)&&Number.isInteger(item?.total)
 return <div className="rag-index-status"><span className={'badge '+(status==='ready'?'green':['failed','degraded','interrupted'].includes(status)?'amber':'')}>{active&&<LoaderCircle size={12} className="spin"/>}{label}</span>{hasCount&&<small>{item.processed} / {item.total} 个单元</small>}{item?.error&&<span className="error-text">{item.error}</span>}{item?.sha256&&<small>索引版本 {item.sha256.slice(0,10)}</small>}</div>
}

function useRagStatus(refreshKey:unknown=0){
 const [status,setStatus]=useState<Obj|null>(null),[error,setError]=useState('')
 useEffect(()=>{let live=true,serial=0;const load=async()=>{const current=++serial;try{const data=await api('/api/rag/status');if(live&&current===serial){setStatus(data);setError('')}}catch(e){if(live&&current===serial){setStatus(null);setError((e as Error).message)}}};void load();const timer=window.setInterval(()=>{if(!document.hidden)void load()},3000);const changed=()=>void load();window.addEventListener('ef-rag-changed',changed);window.addEventListener('ef-connections-changed',changed);return()=>{live=false;window.clearInterval(timer);window.removeEventListener('ef-rag-changed',changed);window.removeEventListener('ef-connections-changed',changed)}},[refreshKey])
 return {status,error}
}

export function RagIndexPanel({docs,notify,filter=''}:Obj){
 const [page,setPage]=useState(0);const filtered:Obj[]=docs.filter((d:Obj)=>d.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase())),currentPage=Math.min(page,Math.max(0,Math.ceil(filtered.length/10)-1));
 useEffect(()=>setPage(0),[filter])
 const {status,error}=useRagStatus(docs.map((d:Obj)=>d.id+':'+d.sha256).join('|'))
 const [selected,setSelected]=useState<string[]>([]),[busy,setBusy]=useState(false),[failure,setFailure]=useState(''),[notice,setNotice]=useState(''),[preview,setPreview]=useState<Obj|null>(null)
 const provider=status?.providers?.embedding,remote=provider?.local===false,items:Obj[]=status?.indexes||[]
 useEffect(()=>setSelected(ids=>ids.filter(id=>docs.some((d:Obj)=>d.id===id))),[docs.map((d:Obj)=>d.id).join('|')])
 const build=async()=>{if(!selected.length||busy)return;setBusy(true);setFailure('');setNotice('');try{if(remote){setPreview(await api('/api/rag/reindex/preview',{document_ids:selected}));return}await api('/api/rag/reindex',{document_ids:selected,remote_opt_in:false});setNotice('索引任务已提交，完成情况以下方实际状态为准。');window.dispatchEvent(new Event('ef-rag-changed'));notify?.('索引任务已提交')}catch(e){setFailure((e as Error).message)}finally{setBusy(false)}}
 const confirmRemote=async()=>{if(!preview||busy)return;setBusy(true);setFailure('');try{await api('/api/rag/reindex',{document_ids:preview.document_ids,preview_id:preview.preview_id,remote_opt_in:true});setPreview(null);setNotice('已确认的远程索引任务已提交，完成情况以下方实际状态为准。');window.dispatchEvent(new Event('ef-rag-changed'))}catch(e){setFailure((e as Error).message)}finally{setBusy(false)}}
 return <section className="rag-index-panel" aria-label="文档检索索引"><div className="section-title"><h3><BookOpen size={18}/>检索索引</h3><button type="button" className="text-link" onClick={()=>window.dispatchEvent(new Event('ef-rag-changed'))}><RefreshCw size={14}/>刷新</button></div><p className="muted">解析原件与建立检索索引分别记录。未建立向量时可使用实际可用的关键词检索。</p>{(error||failure)&&<p role="alert" className="error-text">{failure||error}</p>}{notice&&<p role="status">{notice}</p>}{!status&&!error&&<p role="status">读取索引状态…</p>}<div className="rag-index-list">{filtered.slice(currentPage*10,(currentPage+1)*10).map((d:Obj)=>{const index=items.find(i=>i.document_id===d.id&&i.sha256===d.sha256)||items.find(i=>i.document_id===d.id);return <article key={d.id}><label className="check-line"><input type="checkbox" checked={selected.includes(d.id)} disabled={busy||d.status==='failed'} onChange={e=>setSelected(ids=>e.target.checked?[...ids,d.id]:ids.filter(id=>id!==d.id))}/><span>{d.name}</span></label>{!status?<small>索引状态尚未读取</small>:index&&index.sha256!==d.sha256?<div><span className="badge amber">新版本尚未建立索引</span><small>旧版本索引不用于冒充新版就绪。</small></div>:<IndexStatus item={index}/>}</article>})}</div><ListPagination total={filtered.length} page={currentPage} onPage={setPage} pageSize={10} label="检索索引"/><p className="muted tiny">已选 {selected.length} 份文档，翻页或筛选后保留选择。</p><div className="rag-index-actions"><button type="button" className="button" disabled={busy||!filtered.length} onClick={()=>setSelected(ids=>Array.from(new Set([...ids,...filtered.filter(d=>d.status!=='failed').map(d=>d.id)])))}>{filter?'选择筛选结果':'选择可解析文档'}</button>{selected.length>0&&<button type="button" className="button" disabled={busy} onClick={()=>setSelected([])}>清空选择</button>}<button type="button" className="button primary" disabled={busy||!selected.length||!status} title={remote?'先预览具体文档与发送量，再确认远程索引':'建立所选文档的索引'} onClick={build}>{busy?<LoaderCircle size={15} className="spin"/>:<RefreshCw size={15}/>}{remote?'预览远程索引':'建立所选索引'}</button></div>{remote&&<p className="muted">当前嵌入使用远程服务。只有预览具体文档与发送量，并点击确认后才提交批量索引。</p>}{preview&&<Modal title="远程索引发送预览" close={()=>{if(!busy)setPreview(null)}}><p>将以下文档的原文切片发送给所选嵌入服务，用于建立向量索引。</p><div className="outbound-preview"><p>{preview.endpoint} · {preview.model}</p><p>{preview.chunk_count} 个切片 · {preview.character_count} 字符</p>{(preview.documents||[]).map((d:Obj)=><p key={d.id||d.document_id}>{d.name} · {(d.sha256||'').slice(0,12)}{d.chunks!=null?' · '+d.chunks+' 个切片':''}</p>)}</div><p className="muted">此确认仅针对预览中的文档版本与服务配置。提交时版本或连接改变，服务端会要求重新预览。</p>{failure&&<p className="error-text" role="alert">{failure}</p>}<div className="modal-actions"><button className="button" disabled={busy} onClick={()=>setPreview(null)}>取消</button><button className="button primary" disabled={busy||!preview.preview_id||!Number.isInteger(preview.chunk_count)||!Number.isInteger(preview.character_count)} onClick={confirmRemote}>{busy?'正在提交':'确认发送这些切片'}</button></div></Modal>}</section>
}

export function ProviderStatus({name,item}:Obj){return <div className="pref-row"><div><strong>{name}</strong><small>{item?.model||'尚未读取模型'}</small></div><div><span className={'badge '+(item?.ready?'green':'amber')}>{item?.ready?'实际就绪':'未就绪'}</span>{item?.reason&&<small>{item.reason}</small>}{item&&<small>{item.local?'本机处理':'外部服务'}</small>}</div></div>}

export function RagConfigPanel({notify,refreshKey=0}:Obj){
 const {status,error:statusError}=useRagStatus(refreshKey)
 const [config,setConfig]=useState<Obj|null>(null),[draft,setDraft]=useState<Obj|null>(null),[connections,setConnections]=useState<Obj[]>([]),[models,setModels]=useState<Obj[]>([])
 const [error,setError]=useState(''),[loading,setLoading]=useState(false),[saving,setSaving]=useState(false),[installing,setInstalling]=useState(''),[notice,setNotice]=useState('')
 const load=async()=>{setLoading(true);setError('');try{const [r,c,m]=await Promise.all([api('/api/rag/config'),api('/api/connections'),api('/api/rag/models')]);setConfig(r);setDraft(r);setConnections(c.items||[]);setModels(m.items||[])}catch(e){setError((e as Error).message)}finally{setLoading(false)}}
 useEffect(()=>{void load()},[refreshKey])
 useEffect(()=>{if(!models.some(m=>['pending','running','downloading','installing'].includes(m.status)))return;let live=true;const timer=window.setInterval(async()=>{try{const response=await api('/api/rag/models');if(live)setModels(response.items||[])}catch(e){if(live)setError((e as Error).message)}},2000);return()=>{live=false;window.clearInterval(timer)}},[models.map(m=>m.id+':'+m.status).join('|')])
 const activeInstall=models.some(m=>['pending','running','downloading','installing'].includes(m.status))
 const edit=(key:string,value:unknown)=>{setDraft(old=>old?{...old,[key]:value}:old);setNotice('')}
 const fields=['embedding_mode','embedding_connection_id','rerank_mode','rerank_connection_id','embedding_model','rerank_model','remote_index_allowed']
 const changed=!!draft&&!!config&&fields.some(k=>draft[k]!==config[k])
 const save=async(e:React.FormEvent)=>{e.preventDefault();if(!draft||!config||saving)return;setSaving(true);setError('');try{const body=Object.fromEntries(fields.filter(k=>draft[k]!==undefined).map(k=>[k,draft[k]]));await api('/api/rag/config',{...body,expected_revision:config.revision},'PUT');await load();setNotice('检索模型配置已保存；就绪情况以实际能力检查为准。');notify?.('检索模型配置已保存');window.dispatchEvent(new Event('ef-rag-changed'))}catch(e){setError((e as Error).message)}finally{setSaving(false)}}
 const install=async(model:Obj)=>{setInstalling(model.id);setError('');setNotice('');try{const response=await api('/api/rag/models/install',{model_id:model.id,confirm_download:model.bundled===false});setNotice(response.status==='failed'?response.error||'模型安装未完成':response.status==='pending'||response.status==='running'?'安装任务已提交，模型通过实际文件校验后才可用。':'安装请求已完成，以下方文件校验及模型能力状态为准。');await load();window.dispatchEvent(new Event('ef-rag-changed'))}catch(e){setError((e as Error).message)}finally{setInstalling('')}}
 return <section className="pref-card rag-config-panel" aria-labelledby="rag-config-title"><div className="section-title"><h3 id="rag-config-title"><BookOpen size={22}/>知识库检索模型</h3><button type="button" className="text-link" disabled={loading||saving||!!installing} onClick={load}><RefreshCw size={14}/>重新读取</button></div><p className="muted">嵌入与重排独立于生成模型。缺少模型包或连接未通过测试时，不显示为可用。</p>{(error||statusError)&&<p role="alert" className="error-banner">{error||statusError}</p>}{notice&&<p role="status">{notice}</p>}<ProviderStatus name="语义向量" item={status?.providers?.embedding}/><ProviderStatus name="候选重排" item={status?.providers?.rerank}/>{!draft?<p role="status">{loading?'读取检索配置…':'配置尚未读取，请重试。'}</p>:<form onSubmit={save}>{(['embedding','rerank'] as const).map(role=><fieldset key={role} disabled={saving||loading||!!installing} className="rag-provider-field"><legend>{role==='embedding'?'语义向量处理':'候选重排处理'}</legend><label className="form-field">处理方式<select aria-label={role==='embedding'?'嵌入处理方式':'重排处理方式'} value={draft[role+'_mode']} onChange={e=>edit(role+'_mode',e.target.value)}><option value="local">本机模型包</option><option value="connection">已配置连接</option><option value="disabled">停用此通道</option></select></label>{draft[role+'_mode']==='connection'&&<label className="form-field">连接<select aria-label={role==='embedding'?'嵌入连接':'重排连接'} value={draft[role+'_connection_id']||''} onChange={e=>edit(role+'_connection_id',e.target.value||null)}><option value="">请选择对应类型的连接</option>{connections.filter(c=>c.kind===role).map(c=><option key={c.id} value={c.id} disabled={!c.enabled||!c.last_test?.ok}>{c.name}{!c.enabled||!c.last_test?.ok?' · 尚未测试启用':''}</option>)}</select></label>}</fieldset>)}<label className="check-line"><input type="checkbox" disabled={saving||loading||!!installing} checked={!!draft.remote_index_allowed} onChange={e=>edit('remote_index_allowed',e.target.checked)}/>允许预览并确认后的远程索引</label><p className="muted tiny"><ShieldCheck size={14}/> 默认本机建立索引。允许远程聊天不等于允许批量外发文档；远程建索引另需文档与发送量预览。</p><button className="button primary" disabled={saving||loading||!!installing||!changed}>{saving?'保存中':'保存检索配置'}</button></form>}<div className="rag-local-models"><h4>本机模型包</h4><p className="muted">仅在点击时安装审定模型，优先读取随包文件；缺包才请求固定来源下载，不发送你的文档。</p>{models.map(model=><article key={model.id}><div><strong>{model.name||model.id}</strong><small>{model.license||'许可信息见模型清单'}{model.size_bytes?' · '+(model.size_bytes/1048576).toFixed(1)+' MB':''}</small><ModelPackageStatus model={model}/>{['pending','running','downloading','installing'].includes(model.status)&&Number.isFinite(model.downloaded_bytes)&&Number.isFinite(model.total_bytes)&&model.total_bytes>0&&<><small>实际处理 {(model.downloaded_bytes/1048576).toFixed(1)} / {(model.total_bytes/1048576).toFixed(1)} MB</small>{['pending','running','downloading','installing'].includes(model.status)&&<progress aria-label={(model.name||model.id)+' 安装字节进度'} value={model.downloaded_bytes} max={model.total_bytes}/>}</>}{model.error&&<p className="error-text" role="alert">{model.error}</p>}</div><button type="button" className="button" disabled={!!installing||activeInstall||saving||model.installed&&model.verified||typeof model.bundled!=='boolean'} onClick={()=>install(model)}>{installing===model.id?<LoaderCircle size={15} className="spin"/>:<Download size={15}/>} {installing===model.id?'正在请求安装':['pending','running','downloading','installing'].includes(model.status)?'安装任务进行中':model.installed&&model.verified?'已安装':model.bundled===true?'安装随包模型':model.bundled===false?'下载并安装公开模型':'重新读取安装来源'}</button></article>)}{models.length===0&&<p className="muted">尚未读到可安装的审定模型清单。</p>}</div></section>
}
