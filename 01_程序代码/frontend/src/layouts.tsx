import React from 'react'
import {ArrowLeft, Calculator, ChevronRight, Database, FileCheck2, FileText, History, Home, Menu, MessageCircle, Plus, Search, Settings, ShieldCheck} from 'lucide-react'
import {IconButton, Mascot, Modal, Obj, Page, timeLabel} from './ui'

const navigation: {id: Page; label: string; icon: React.ElementType}[] = [
  {id: 'home', label: '首页', icon: Home},
  {id: 'analysis', label: '问答', icon: MessageCircle},
  {id: 'evidence', label: '证据', icon: FileCheck2},
  {id: 'data', label: '数据', icon: Database},
  {id: 'scenario', label: '情景', icon: Calculator},
  {id: 'records', label: '记录', icon: History},
  {id: 'settings', label: '设置', icon: Settings},
]

export function Brand({stacked = false, onClick}: {stacked?: boolean; onClick: () => void}) {
  return <button className={'ef-brand ' + (stacked ? 'stacked' : '')} aria-label="证流首页" onClick={onClick}>
    <img src={'/brand-' + (stacked ? 'stacked' : 'horizontal') + '.svg'} alt="证流 EvidenceFlow"/>
    <img className="brand-compact" src="/brand-icon.svg" alt=""/>
  </button>
}

/** Three page shells share navigation and controls; business state stays in App. */
export function WorkspaceLayout({page, collapsed, setCollapsed, mobileNav, setMobileNav, navigate,
  health, saved, tables, openSaved, project, sessions=[], sessionId, onSession, onProject, onNew, onSearch, onStatus, onPet, showPet, children}: Obj) {
  const conversation = page === 'analysis'
  const document = page === 'evidence'
  const kind = conversation ? 'conversation' : document ? 'document' : 'overview'
  const nav = <nav aria-label="主导航">{navigation.map(n => <button key={n.id}
    className={'nav-item ' + (page === n.id ? 'active' : '')}
    title={n.label} aria-label={n.label} aria-current={page === n.id ? 'page' : undefined}
    onClick={() => navigate(n.id)}><n.icon size={20}/><span>{n.label}</span></button>)}</nav>
  const context = <div className="context-navigation">
    <button className="context-project" onClick={onProject} title="切换或管理项目"><Database size={18}/><span>{project?.name||'项目'}</span><ChevronRight size={15}/></button>
    <button className="button primary full new-analysis" onClick={onNew}><Plus size={19}/>新建会话</button>
    <button className="context-return" onClick={() => navigate('home')}><ArrowLeft size={17}/>返回首页</button>
    <div className="context-section"><div className="section-title"><h3>最近会话</h3><button className="text-link" onClick={() => navigate('records')}>已保存</button></div>
      <div className="context-records">{sessions.length ? sessions.map((s: Obj) =>
        <button key={s.id} aria-current={s.id===sessionId?'true':undefined} onClick={() => onSession(s.id)} title={s.title}><MessageCircle size={17}/><span>{s.title}<small>{['queued','running','cancelling'].includes(s.task_status)?'查询中 · ':''}{s.turn_count} 轮 · {timeLabel(s.updated_at)}</small></span></button>
      ) : <p className="muted context-empty">暂无会话</p>}</div>
    </div>
    <div className="context-section context-data"><div className="section-title"><h3>当前数据</h3>
      <button className="text-link" onClick={() => navigate('data')}>管理</button></div>
      {tables.slice(0, 4).map((t: Obj) => <button key={t.name} onClick={() => navigate('data')}><Database size={18}/><span>{t.name}</span><small>{t.row_count ?? t.rows ?? '—'} 行</small></button>)}
    </div>
    {showPet && <button className="context-pet" onClick={()=>onPet()} aria-label="问证流" title="问证流"><Mascot pose="pointing" className="context-mascot"/></button>}
    <button className="context-settings" onClick={() => navigate('settings')}><Settings size={18}/>设置</button>
  </div>
  return <div className={`workspace-shell layout-${kind} ${collapsed ? 'navigation-collapsed' : ''}`}>
    <a className="skip-link" href="#workspace-main">跳到主要内容</a>
    <aside className="workspace-navigation" aria-label={conversation ? '分析导航' : '工作区导航'}>
      {!conversation && !document && <Brand stacked onClick={() => navigate('home')}/>}
      {conversation ? context : <>{nav}<div className="navigation-landscape" aria-hidden="true"/>
        {document && showPet && <button className="navigation-pet" onClick={()=>onPet()} aria-label="问证流" title="问证流"><Mascot pose="pointing" className="navigation-mascot"/></button>}
        <button className="local-status" onClick={onStatus}><span className={'status-dot ' + (health ? 'online' : '')}/><span>{health ? '服务已连接' : '服务未连接'}</span></button>
      </>}
    </aside>
    <header className="workspace-header">
      <IconButton label={collapsed ? '展开导航' : '收起或打开导航'} onClick={() => innerWidth <= 1100 ? setMobileNav(true) : setCollapsed(!collapsed)}><Menu size={20}/></IconButton>
      {(conversation || document) && <Brand onClick={() => navigate('home')}/>}
      <button className="workspace-project" onClick={onProject} title="切换或管理项目">{project?.name||'项目'}<ChevronRight size={13}/></button>
      <button className="global-search" aria-label="搜索工作区" title="搜索工作区（⌘K）" onClick={onSearch}><Search size={18}/><span>搜索文件、记录或问题</span><kbd>⌘ K</kbd></button>
      {showPet && <button className="header-companion" onClick={()=>onPet()} aria-label="问证流" title="问证流"><Mascot pose="reading"/></button>}<span className="workspace-data-label">示例数据</span>
      <IconButton label="连接状态" onClick={onStatus}><ShieldCheck size={21}/></IconButton>
      <button className="profile" onClick={onStatus} aria-label="本地工作区状态"><span className="avatar">本</span><span>本地<small>个人工作区</small></span><ChevronRight size={15}/></button>
    </header>
    <main className="workspace-main" id="workspace-main" tabIndex={-1}><div className="content">{children}</div></main>
    {mobileNav && <Modal title="工作区导航" className="navigation-dialog" close={() => setMobileNav(false)}>
      {nav}{conversation && context}
    </Modal>}
  </div>
}
