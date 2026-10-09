import React,{useEffect,useRef,useState} from 'react'
import {createRoot} from 'react-dom/client'
import {ArrowRight,BookOpen,Calculator,CheckCircle2,ChevronRight,Database,FileCheck2,FileText,History,Home,Menu,MessageCircle,Plus,Search,Settings,ShieldCheck,X} from 'lucide-react'
import {api,Obj,Page,IconButton,Mascot,Modal,SaveDialog} from './ui'
import {isDatabaseResult} from './result-presentation'
import {isKnowledgeResult} from './rag'
import {AnalysisPage} from './workspace'
import {ScenarioPage} from './scenario'
import {EvidencePage} from './evidence'
import {ChecksDialog,DataPage,RecordsPage} from './catalog'
import {HomePage} from './home'
import Pet from './Pet'
import {SettingsPage} from './settings'
import {ConnectionsPanel,ExtensionDialog,ShareDialog} from './connections'
import './connections.css'
import Temporary from './Temporary'
import {defaultPrefs,normalizePrefs,PreferencesContext} from './preferences'
import {WorkspaceLayout} from './layouts'
import {ImportDialog,MenuAction} from './controls'
import {Globe,Sparkles,UploadCloud} from 'lucide-react'
import './styles.css'
import './layout.css'
import './conversations.css'
import './home.css'
import './pages.css'
import './evidence.css'
import './scenario.css'
import './companion.css'
import {useConversations,ProjectDialog,RenameSessionDialog} from './conversations'
function App(){
 const [importOpen,setImportOpen]=useState(false)
 const [connectionOpen,setConnectionOpen]=useState(false),[extension,setExtension]=useState<Obj|null>(null),[share,setShare]=useState<Obj|null>(null)
 const [page,setPage]=useState<Page>(()=>{const p=localStorage.getItem('ef-page');return ['home','analysis','evidence','data','scenario','records','settings'].includes(p||'')?p as Page:'home'}),[collapsed,setCollapsed]=useState(()=>localStorage.getItem('ef-navigation-collapsed')==='yes'),[mobileNav,setMobileNav]=useState(false),[preferences,setPreferences]=useState<Obj>(()=>{try{return normalizePrefs(JSON.parse(localStorage.getItem('ef-preferences')||'{}'))}catch{return defaultPrefs}})
  const [health,setHealth]=useState<Obj|null>(null),[tables,setTables]=useState<Obj[]>([]),[docs,setDocs]=useState<Obj[]>([]),[saved,setSaved]=useState<Obj[]>([]),[dashboard,setDashboard]=useState<Obj|null>(null),[selected,setSelected]=useState<Obj|null>(null),[question,setQuestion]=useState(''),[uploading,setUploading]=useState(false)
  const [agentConfig,setAgentConfig]=useState<Obj|null>(null)
 const [temporaryOpen,setTemporaryOpen]=useState(false),[settingsTab,setSettingsTab]=useState('appearance'),[petState,setPetState]=useState('idle')
 const [toast,setToast]=useState(''),[saveOpen,setSaveOpen]=useState(false),[checksOpen,setChecksOpen]=useState(false),[searchOpen,setSearchOpen]=useState(false),[statusOpen,setStatusOpen]=useState(false),[petRequest,setPetRequest]=useState<Obj|null>(null),[projectOpen,setProjectOpen]=useState(false),[renameOpen,setRenameOpen]=useState(false)
 const [catalogReady,setCatalogReady]=useState(false),[catalogError,setCatalogError]=useState('')
 const [dashboardError,setDashboardError]=useState(''),[importState,setImportState]=useState<Obj|null>(null),[dataTarget,setDataTarget]=useState<Obj|null>(null)
 const [readerState,setReaderState]=useState<Obj>(()=>{try{return JSON.parse(sessionStorage.getItem('ef-reader-state')||'{}')}catch{return {}}})
 useEffect(()=>{sessionStorage.setItem('ef-reader-state',JSON.stringify(readerState))},[readerState])
 useEffect(()=>{localStorage.setItem('ef-navigation-collapsed',collapsed?'yes':'no')},[collapsed])
 const c=useConversations(); const result=c.result,running=c.busy,queryError=c.error,stale=!!result&&(isDatabaseResult(result)||isKnowledgeResult(result)?c.session?.result_stale===true:result.data_version!==c.session?.data_version); const undo=c.session?.messages.find((m:Obj)=>m.id===c.undoId)?.result; const scenarioBase=c.session?.messages.filter((m:Obj)=>m.result?.formulas?.[0]&&!m.result.formulas[0].simulation).at(-1)?.result; const toastTimer=useRef(0)
 const notify=(text:string,kind='success')=>{if((kind==='success'&&!preferences.notifySuccess)||(kind==='error'&&!preferences.notifyFailure)||(kind==='import'&&!preferences.notifyImport))return;setToast(text);window.clearTimeout(toastTimer.current);toastTimer.current=window.setTimeout(()=>setToast(''),5500)}
 const navigationRevision=useRef(0)
 const navigate=(p:Page)=>{navigationRevision.current++;setPage(p);try{localStorage.setItem('ef-page',p)}catch{}setMobileNav(false);window.scrollTo({top:0});document.querySelector('.content')?.scrollTo(0,0)}
 const dashSerial=useRef(0),projectRef=useRef(c.projectId);projectRef.current=c.projectId
 const loadDashboard=async()=>{const serial=++dashSerial.current,pid=c.projectId;try{const d=await api('/api/dashboard?project_id='+encodeURIComponent(pid));if(serial===dashSerial.current&&pid===projectRef.current){setDashboard(d);setDashboardError('')}}catch(e){if(serial===dashSerial.current&&pid===projectRef.current){setDashboard(null);setDashboardError((e as Error).message)}}}
 const load=async()=>{const responses=await Promise.allSettled([api('/api/health'),api('/api/tables'),api('/api/documents'),api('/api/saved')]);const problem=responses.slice(1,3).find(r=>r.status==='rejected');setCatalogReady(!problem);setCatalogError(problem?'数据目录暂未读取':'');const setters=[setHealth,setTables,setDocs,setSaved];responses.forEach((r,i)=>{if(r.status==='fulfilled')setters[i](r.value);else if(i===0){setHealth(null);notify('本地服务未连接，请双击“启动证流.command”','error')}});await loadDashboard()}
 useEffect(()=>{setDashboard(null);void loadDashboard()},[c.projectId,c.sessions.map((s:Obj)=>s.id+s.updated_at+s.task_status).join('|')])
  useEffect(()=>{void load()},[])
  useEffect(()=>{let live=true,serial=0;const refresh=async()=>{const request=++serial;try{const config=await api('/api/agent/config');if(live&&request===serial)setAgentConfig(config)}catch{if(live&&request===serial)setAgentConfig(null)}};void refresh();window.addEventListener('ef-agent-config-changed',refresh);window.addEventListener('ef-connections-changed',refresh);return()=>{live=false;window.removeEventListener('ef-agent-config-changed',refresh);window.removeEventListener('ef-connections-changed',refresh)}},[])
 useEffect(()=>{const mq=matchMedia('(prefers-color-scheme: dark)');const apply=()=>{document.documentElement.dataset.theme=preferences.theme==='system'?(mq.matches?'dark':'light'):preferences.theme};apply();mq.addEventListener('change',apply);document.documentElement.dataset.density=preferences.density;document.documentElement.dataset.motion=preferences.motion?'on':'off';document.documentElement.style.setProperty('--base-font',preferences.fontSize+'px');document.documentElement.style.setProperty('--font-scale',String(preferences.fontSize/14));localStorage.setItem('ef-preferences',JSON.stringify(preferences));return()=>mq.removeEventListener('change',apply)},[preferences])
 useEffect(()=>{const handler=(e:KeyboardEvent)=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();if(!document.querySelector('[aria-modal="true"]'))setSearchOpen(true)}if(e.key==='Escape')setMobileNav(false)};window.addEventListener('keydown',handler);return()=>window.removeEventListener('keydown',handler)},[])
 const safe=async(action:()=>Promise<any>)=>{try{return await action()}catch(e){notify((e as Error).message,'error')}}
 const run=async(text:string,confirmed:Obj={},follow=false,destination:Page='analysis')=>{const q=text.trim();if(!q||running||c.loading)return;navigate(destination);setSelected(null);const r=await safe(()=>c.submit(q,confirmed,follow&&preferences.context));if(r){if(c.activeId()===r.session_id)setQuestion('');localStorage.removeItem(draftKey)}return r}
 const petQuery=async(text:string,confirmed:Obj={},follow=false)=>{const fresh=!c.session;const response=await run(text,confirmed,follow);if(fresh&&response&&c.activeId()===response.session_id)setPetRequest({id:Date.now(),submitted:{question:text,task_id:response.id}});return response}
 const recompute=async(overrides:Obj)=>{if(!result||running)return;setSelected(null);return safe(()=>c.submit(result.question,overrides,false,'recompute'))}
 const upload=async(file:File)=>{if(uploading)return;const startedAtNavigation=navigationRevision.current;setUploading(true);setImportState({status:'loading',filename:file.name,message:'正在读取文件',file});const form=new FormData();form.append('file',file);try{const r=await api('/api/import',form);await load();const message=r.ok===false?(r.message||'解析未完成，原文件已保留'):r.kind==='table'?r.message+'。可在资料库预览；在主问答设置中启用“数据库问数”并选择本机工作库后，可询问已导入表。':r.document?.status==='needs_review'?'原件已保存，文字识别待配置。':r.message||'文件已导入';setImportState({status:r.ok===false||r.document?.status==='needs_review'?'review':'done',filename:file.name,message,kind:r.kind,tables:r.tables,document:r.document,file});notify(message,'import');if(page!=='home'&&navigationRevision.current===startedAtNavigation)navigate('data')}catch(e){const message=(e as Error).message;setImportState({status:'failed',filename:file.name,message,file});notify(message,'error')}finally{setUploading(false)}}
 const homeRun=async(text:string)=>{const q=text.trim();if(!q||c.loading||running)return;navigate('analysis');setSelected(null);const r=await safe(()=>c.submit(q,{},false,'query',true));if(r){localStorage.removeItem(draftKey);if(c.activeId()===r.session_id)setQuestion('')}return r}
 const resumeSession=(sid:string)=>{navigate('analysis');void safe(()=>c.openSession(sid))}
 const resumeQuestion=(r:Obj)=>{navigate('analysis');void safe(async()=>{await c.openSession(r.session_id);if(r.result_message_id&&c.activeId()===r.session_id)await c.select(r.result_message_id)})}
 const openSaved=(s:Obj)=>{navigate('analysis');void safe(()=>c.importSaved(s))}
 const adopt=(r:Obj)=>{const m=c.session?.messages.find((m:Obj)=>m.result?.run_id===r.run_id);if(m)void safe(()=>c.select(m.id));navigate('analysis')}
 const onEvidence=(e:Obj)=>{if(e){setReaderState(old=>({...old,target:e,page:e.locator?.page||1,focus:null,view:'reader'}));setSelected(e);navigate('evidence')}}
 const askDocument=async(text:string,scope:Obj)=>{if(!text.trim()||running||c.loading)return;navigate('analysis');return safe(()=>c.submit(text.trim(),{},false,'query',false,scope.rag_scope?null:scope,scope.rag_scope||null))}
 const openCatalog=(tab:string,table?:string)=>{setDataTarget({tab,table});navigate('data')}
 const openDoc=(d:Obj)=>onEvidence({id:d.id,type:'document',source:d.name,locator:{document_id:d.id,page:1,sha256:d.sha256,...(d.parse_id?{parse_id:d.parse_id}:{}),source_url:'/api/documents/'+d.id+'/original?version='+d.sha256}})
 const copy=async(sources:boolean)=>{if(!result)return;try{await navigator.clipboard.writeText(result.answer+(sources?'\n\n'+result.evidence.map((e:Obj,i:number)=>`[${i+1}] ${e.source}${e.locator.page?' · 第 '+e.locator.page+' 页':''}\n${e.locator.source_url?location.origin+e.locator.source_url:e.locator.sql||''}`).join('\n'):''));notify('已复制'+(sources?'答案与来源':'答案'))}catch{notify('复制未成功，请手动选择文字','error')}}
 const pet=(text?:string)=>{setPreferences((old:Obj)=>({...old,pet:true}));if(page==='settings'&&!['appearance','assistant'].includes(settingsTab))setSettingsTab('assistant');setPetRequest({id:Date.now(),text})}
 const newAnalysis=()=>{navigate('home');editQuestion('');requestAnimationFrame(()=>document.querySelector<HTMLTextAreaElement>('.query-input textarea')?.focus())}
 const draftKey='ef-draft-'+c.projectId+'-'+(c.session?.id||'new')
 useEffect(()=>{setQuestion(localStorage.getItem(draftKey)||'');setSelected(null);setSaveOpen(false)},[draftKey])
 const editQuestion=(text:string)=>{setQuestion(text);localStorage.setItem(draftKey,text)}
 useEffect(()=>{setSelected(null);if(result)void load()},[result?.run_id])
 const selectMessage=(id:string)=>{setSelected(null);void safe(()=>c.select(id))}
 const workTools:MenuAction[]=[
  {id:'import',label:'导入文件',icon:UploadCloud,detail:'添加到本机共享资料库',disabled:uploading,reason:'资料正在导入',onSelect:()=>setImportOpen(true)},
  {id:'connect',label:'连接数据库',icon:Database,detail:'配置与测试外部数据源',onSelect:()=>setConnectionOpen(true)},
  {id:'text',label:'模型补充',icon:Sparkles,detail:'配置后使用；调用前预览内容',disabled:running||c.loading,reason:'请等待当前查询完成',onSelect:()=>setExtension({mode:'text'})},
  {id:'search',label:'联网检索',icon:Globe,detail:'使用已配置的检索服务',disabled:running||c.loading,reason:'请等待当前查询完成',onSelect:()=>setExtension({mode:'search'})},
  {id:'scenario',label:'情景推演',icon:Calculator,detail:'调整参数，对比奖金方案',onSelect:()=>navigate('scenario')},
  {id:'evidence',label:'阅读原文',icon:BookOpen,detail:'文档与逐结论来源',onSelect:()=>navigate('evidence')}
 ]
 const viewImport=()=>{setDataTarget({tab:importState?.kind==='table'?'tables':'docs',table:importState?.tables?.[0],document:importState?.document?.name});setImportOpen(false);navigate('data')}
  const common={tools:workTools,onShare:()=>result&&setShare({kind:'analysis',id:result.run_id}),onExtension:(mode:string)=>setExtension({mode}),onAgentSettings:()=>{setSettingsTab('models');navigate('settings')},result,baseline:scenarioBase,run,recompute,busy:running||c.loading,navigate,onEvidence,onSave:()=>setSaveOpen(true)}
 return <PreferencesContext.Provider value={preferences as typeof defaultPrefs}><WorkspaceLayout petState={petState} page={page} collapsed={collapsed} setCollapsed={setCollapsed} mobileNav={mobileNav} setMobileNav={setMobileNav} navigate={navigate} health={health} saved={saved} tables={tables} openSaved={openSaved} onNew={newAnalysis} onSearch={()=>setSearchOpen(true)} onStatus={()=>setStatusOpen(true)} onPet={pet} showPet={preferences.pet} project={c.project} sessions={c.sessions.map((s:Obj)=>s.id===c.session?.id?{...s,task_status:c.session?.tasks.at(-1)?.status}:s)} sessionId={c.session?.id} onProject={()=>{setMobileNav(false);setProjectOpen(true)}} onSession={(sid:string)=>{navigate('analysis');void safe(()=>c.openSession(sid))}}> {queryError&&<div className="error-banner" role="alert"><span>{queryError}</span><button className="text-link" onClick={()=>void safe(c.session?c.retrySubmission:c.bootstrap)}>重新连接</button></div>}{stale&&page==='analysis'&&<div className="change-notice"><span>{isKnowledgeResult(result)?'知识库或连接已变化，当前保留原查询范围与版本。':isDatabaseResult(result)?'问数来源结构或连接已变化，当前为原查询快照。':'这是保存时的快照，数据已更新。'}</span><button className="text-link" onClick={()=>isDatabaseResult(result)||isKnowledgeResult(result)?newAnalysis():recompute({})}>{isDatabaseResult(result)||isKnowledgeResult(result)?'新建问答':'刷新结果'}<ArrowRight size={14}/></button></div>}
 {page==='home'&&<HomePage tools={workTools} onImport={()=>setImportOpen(true)} onCatalog={openCatalog} onConnect={()=>setConnectionOpen(true)} onExtension={(mode:string)=>setExtension({mode})} tables={tables} docs={docs} dashboard={dashboard} dataReady={catalogReady} dashboardError={dashboardError||catalogError} reloadDashboard={load} question={question} setQuestion={editQuestion} run={homeRun} busy={running||c.loading} navigate={navigate} importFile={upload} uploading={uploading} importState={importState} onViewImport={()=>{setDataTarget({tab:importState?.kind==='table'?'tables':'docs',table:importState?.tables?.[0],document:importState?.document?.name});navigate('data')}} onPet={pet} showPet={preferences.pet} project={c.project} sessions={c.sessions} onSession={resumeSession} onQuestion={resumeQuestion} onProjects={()=>setProjectOpen(true)}/>}
 {page==='analysis'&&<AnalysisPage key={c.session?.id||'empty'} {...common} question={question} setQuestion={editQuestion} undo={undo} onUndo={()=>{if(c.undoId)void safe(async()=>{await c.select(c.undoId!);c.setUndoId(null);notify('已恢复修改前的结果')})}} choose={(conditions:Obj)=>recompute(conditions)} onCopy={copy} session={c.session} projectName={c.project?.name} loading={c.loading} selectedMessageId={c.resultMessage?.id} onSelectMessage={selectMessage} onRetry={(tid:string)=>void safe(()=>c.retry(tid))} onCancel={()=>void safe(c.cancel)} task={c.task} stage={c.task?.stage} onRename={()=>setRenameOpen(true)}/>}
 {page==='evidence'&&<EvidencePage onShare={()=>result&&setShare({kind:'analysis',id:result.run_id})} onVision={(document:Obj)=>setExtension({mode:'vision',document})} result={result} docs={docs} selected={selected} onSelect={setSelected} navigate={navigate} notify={notify} state={readerState} onState={setReaderState} onAsk={askDocument} busy={running||c.loading}/>}
 {page==='scenario'&&<ScenarioPage key={c.projectId+'-'+(c.session?.id||'new')} {...common} onShare={(id:string)=>setShare({kind:'scenario',id})} projectId={c.projectId} sessionId={c.session?.id} notify={notify} onConnection={(connected:boolean)=>{if(!connected)setHealth(null);else if(!health)void load()}} onOrigin={async(base:Obj)=>{if(base.session_id){await c.openSession(base.session_id);const detail=await api('/api/sessions/'+base.session_id);const msg=detail.messages.find((m:Obj)=>m.result?.run_id===base.origin_run_id);if(msg)await c.select(msg.id)}navigate('analysis')}}/>}
 {page==='data'&&<DataPage onConnect={()=>setConnectionOpen(true)} initialTarget={dataTarget} consumeTarget={()=>setDataTarget(null)} tables={tables} docs={docs} upload={upload} uploading={uploading} refresh={load} onDocument={openDoc} notify={notify}/>}
 {page==='records'&&<RecordsPage saved={saved} openSaved={openSaved} refresh={load} notify={notify} onNew={newAnalysis}/>}
 {page==='settings'&&<SettingsPage onConnectionsUpdated={load} preferences={preferences} setPreferences={setPreferences} health={health} onPet={pet} onCheck={()=>setChecksOpen(true)} notify={notify} onTemporary={()=>setTemporaryOpen(true)} settingsTab={settingsTab} setSettingsTab={setSettingsTab} petState={petState}/>}
 </WorkspaceLayout>
 {!c.loading&&<Pet compact key={c.projectId+':'+(c.session?.id||'new')} preferences={preferences} page={page} projectId={c.projectId} sessionId={c.session?.id} sessionName={c.session?.title} mainResult={result} task={c.task||c.session?.tasks?.at(-1)} onQuery={petQuery} onEvidence={onEvidence} navigate={navigate} request={petRequest} resetRequest={()=>setPetRequest(null)} onState={setPetState} settingsTab={settingsTab} setPreferences={setPreferences} sourceContext={{evidence_id:selected?.id}}/>}
  {importOpen&&<ImportDialog close={()=>setImportOpen(false)} upload={upload} uploading={uploading} state={importState} onView={viewImport}/>}
 {connectionOpen&&<Modal title="数据库连接" wide close={()=>setConnectionOpen(false)}><ConnectionsPanel filter="database" notify={notify} updated={load}/></Modal>}
 {extension&&<ExtensionDialog {...extension} result={result} close={()=>setExtension(null)} onConfigure={()=>{setSettingsTab('models');navigate('settings')}}/>}
 {share&&<ShareDialog {...share} close={()=>setShare(null)}/>}
 {temporaryOpen&&<Temporary close={()=>setTemporaryOpen(false)} context={preferences.context}/>}

 {toast&&<div className="toast" role="status"><CheckCircle2 size={18}/><span>{toast}</span><IconButton label="关闭提示" onClick={()=>setToast('')}><X size={15}/></IconButton></div>}
 {saveOpen&&result&&<SaveDialog result={result} close={()=>setSaveOpen(false)} onSave={async(name)=>{await api('/api/saved',{name,run_id:result.run_id});await load();notify('分析已保存到历史记录')}}/>}
 {projectOpen&&<ProjectDialog controller={c} notify={notify} close={()=>setProjectOpen(false)}/>}
 {renameOpen&&c.session&&<RenameSessionDialog session={c.session} save={c.renameSession} notify={notify} close={()=>setRenameOpen(false)}/>}
 {checksOpen&&<ChecksDialog close={()=>setChecksOpen(false)}/>}
 {searchOpen&&<SearchDialog tools={workTools} openTable={(table:string)=>openCatalog('tables',table)} sessions={c.sessions} onSession={resumeSession} docs={docs} tables={tables} saved={saved} close={()=>setSearchOpen(false)} openDoc={openDoc} openSaved={openSaved} navigate={navigate} run={run}/>}
  {statusOpen&&<Modal title="本地工作区" close={()=>setStatusOpen(false)}><div className="status-summary"><ShieldCheck size={36}/><h3>{health?'服务已连接':'服务未连接'}</h3><p>127.0.0.1:8765</p></div><div className="key-value"><span>工作区</span><strong>本机资料与数据库</strong></div><div className="key-value"><span>主问答设置</span><strong>{agentConfig?({auto:'自动选择',local:'本地确定性查询',model:'指定模型驱动'} as Obj)[agentConfig.mode]||'未读取':'未读取'}</strong></div><div className="key-value"><span>问数范围</span><strong>{agentConfig?(({sales:'销售示例',database:'数据库问数',knowledge:'知识库问答',hybrid:'数据库与文档'} as Obj)[agentConfig.query_scope]||'范围待确认'):'未读取'}</strong></div><p className="muted">问答可选择销售示例、所选数据库、知识库文档或数据库与文档联合回答。每轮实际方式和来源显示在分析结果中，连接测试状态可在设置中查看。</p><button className="button full" onClick={()=>{setStatusOpen(false);setSettingsTab('models');navigate('settings')}}>打开主问答设置<ArrowRight size={16}/></button></Modal>}
 </PreferencesContext.Provider>
}
function SearchDialog({tools,openTable,docs,tables,saved,sessions,onSession,close,openDoc,openSaved,navigate,run}:Obj){const [term,setTerm]=useState('');const data=[...tools.map((t:MenuAction)=>({name:t.label,type:'操作',item:t,icon:t.icon||Settings})),...sessions.map((s:Obj)=>({name:s.title,type:'会话',item:s,icon:MessageCircle})),...docs.map((d:Obj)=>({name:d.name,type:'文档',item:d,icon:FileText})),...tables.map((t:Obj)=>({name:t.name,type:'数据表',item:t,icon:Database})),...saved.map((s:Obj)=>({name:s.name,type:'已保存分析',item:s,icon:History}))].filter(x=>x.name.toLowerCase().includes(term.toLowerCase())).slice(0,8);return <Modal title="搜索工作区" close={close}><label className="search-field search-large"><Search size={19}/><input autoFocus aria-label="搜索工作区内容" placeholder="搜索资料、会话或操作；也可直接提问" value={term} onChange={e=>setTerm(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.nativeEvent.isComposing&&e.keyCode!==229&&term.trim()){close();run(term)}}}/></label><div className="search-results">{data.map((d,i)=><button key={i} disabled={d.type==='操作'&&d.item.disabled} title={d.item.disabled?d.item.reason:undefined} onClick={()=>{close();if(d.type==='操作'){if(!d.item.disabled)d.item.onSelect()}else if(d.type==='文档')openDoc(d.item);else if(d.type==='已保存分析')openSaved(d.item);else if(d.type==='会话')onSession(d.item.id);else openTable(d.item.name)}}><d.icon size={18}/><strong>{d.name}</strong><small>{d.type}</small><ArrowRight size={14}/></button>)}</div>{term.trim()&&<button className="button primary full" onClick={()=>{close();run(term)}}>询问“{term}”<ArrowRight size={16}/></button>}</Modal>}
createRoot(document.getElementById('root')!).render(<App/> )
