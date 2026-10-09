import React from 'react'
import {ArrowUpRight,BookOpen,Calculator,ChevronDown,History,LayoutDashboard,Menu,MessageCircle,Plus,Search,Settings,ShieldCheck,Sparkles} from 'lucide-react'
import {IconButton,Modal,Obj,Page,timeLabel} from './ui'
import {ActionMenu} from './controls'
import {PetBlob} from './pet-blob'

export function Brand({onClick}: {stacked?:boolean;onClick:()=>void}){
 return <button className="ef-brand" aria-label="证流首页" onClick={onClick}>
  <PetBlob pose="idle" size={40} label="证流"/>
  <span><strong>证流<span className="brand-period">.</span></strong><small>可解释问数问答</small></span>
 </button>
}
const titles:Obj={home:'问数问答',analysis:'问数问答',data:'资料库',evidence:'证据核验',records:'分析档案',scenario:'情景推演',settings:'设置'}
export function WorkspaceLayout({page,collapsed,setCollapsed,mobileNav,setMobileNav,navigate,health,project,sessions=[],sessionId,onSession,onProject,onNew,onSearch,onStatus,onPet,showPet,petState='idle',children}:Obj){
 const primary=[
  {id:'home',label:'问数问答',icon:LayoutDashboard,active:['home','analysis'].includes(page)},
  {id:'data',label:'资料库',icon:BookOpen,active:['data','evidence'].includes(page)},
  {id:'records',label:'分析档案',icon:History,active:page==='records'}
 ]
 const nav=<nav aria-label="主导航">{primary.map(n=><button key={n.id} className={'nav-item '+(n.active?'active':'')} aria-current={n.active?'page':undefined} title={n.label} onClick={()=>navigate(n.id as Page)}><n.icon size={19}/><span>{n.label}</span><span className="nav-active-dot"/></button>)}</nav>
 const recent=<div className="nav-recents"><div className="nav-section-label">最近分析<span>{Math.min(sessions.length,5)}</span></div>{sessions.slice(0,5).map((s:Obj)=><button key={s.id} title={s.title} className={s.id===sessionId&&page==='analysis'?'selected':''} aria-current={s.id===sessionId&&page==='analysis'?'true':undefined} onClick={()=>{setMobileNav(false);onSession(s.id)}}><MessageCircle size={14}/><span>{s.title}<small>{['queued','running','cancelling'].includes(s.task_status)?'查询中':timeLabel(s.updated_at)}</small></span></button>)}{!sessions.length&&<p>还没有分析记录，<br/>从一个问题开始。</p>}</div>
 const utilities=<div className="nav-utilities"><button className={'nav-item '+(page==='scenario'?'active':'')} title="情景推演" onClick={()=>navigate('scenario')}><Calculator size={18}/><span>情景推演</span></button><button className={'nav-item '+(page==='settings'?'active':'')} title="设置" onClick={()=>navigate('settings')}><Settings size={18}/><span>设置</span></button><button className="local-status" onClick={onStatus} title={health?'本地服务已连接':'本地服务未连接'}><span className={'status-dot '+(health?'online':'')}/><span>{health?'本地服务已连接':'服务未连接'}</span><ArrowUpRight size={13}/></button></div>
 return <div className={'workspace-shell page-'+page+(collapsed?' navigation-collapsed':'')}>
  <a className="skip-link" href="#workspace-main">跳到主要内容</a>
  <aside className="workspace-navigation" aria-label="工作区导航"><Brand onClick={()=>navigate('home')}/><button className="button primary new-question" title="新建分析" onClick={onNew}><Plus size={18}/><span>新建分析</span></button>{nav}{recent}{utilities}</aside>
  <header className="workspace-header"><IconButton label={collapsed?'展开导航':'收起或打开导航'} onClick={()=>innerWidth<=1100?setMobileNav(true):setCollapsed(!collapsed)}><Menu size={18}/></IconButton><div className="mobile-brand"><Brand onClick={()=>navigate('home')}/></div><button className="workspace-project" onClick={onProject} title="切换或管理项目">{project?.name||'工作空间'}<ChevronDown size={12}/></button><span className="workspace-location"><span>/</span>{titles[page]}</span><button className="global-search" aria-label="搜索工作区" title="搜索工作区与操作" onClick={onSearch}><Search size={16}/><span>搜索资料、会话或操作</span><kbd>{/Mac|iPhone|iPad/.test(navigator.platform)?'⌘':'Ctrl'} K</kbd></button>{showPet&&<button className="header-companion" onClick={()=>onPet()} aria-label="问小察" title="问小察"><PetBlob pose={petState} size={30} backdrop={false}/><span>问小察</span></button>}<ActionMenu label="工作区选项" iconOnly items={[{id:'status',label:'工作区状态',icon:ShieldCheck,onSelect:onStatus},{id:'settings',label:'设置',icon:Settings,onSelect:()=>navigate('settings')}]}/></header>
  <main className="workspace-main" id="workspace-main" tabIndex={-1}><div className="content">{children}</div></main>
  {mobileNav&&<Modal title="工作区导航" className="navigation-dialog" close={()=>setMobileNav(false)}><Brand onClick={()=>navigate('home')}/><button className="button primary full" onClick={()=>{setMobileNav(false);onNew()}}><Plus size={18}/>新建分析</button>{nav}{recent}{utilities}</Modal>}
 </div>
}
