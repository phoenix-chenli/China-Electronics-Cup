import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ArrowLeft, ArrowRight, ArrowUpRight, Check, ChevronDown, Columns3, Download, FileText, Image, List, Minus, Plus, Rows3, Search, X } from 'lucide-react';
import { api, isAbort, messageOf } from './api';
import type { AnswerEvidence, Page, PageBox, SourceDetail, TableRows } from './types';
import { exactCitationSpan, findTextMatches, readerCellText, readerCsv, resolveReaderPage } from './readerTools';
import type { TextMatch } from './readerTools';
import './ReadingPanel.css';

export type ReadingTab = 'original' | 'text' | 'tables';
export type ReaderViewState = { tab: ReadingTab; page: number | null; tableName: string; tableOffset: number; tableMode: 'data' | 'schema'; tableQuery: string; hiddenColumns: string[]; zoom: number; search: string; scrollTop: number };
export type ReadingPanelProps = {
  sourceId: string; claimText?: string; evidence?: AnswerEvidence | null; evidenceRole?: 'published' | 'retrieved';
  initialPage?: number; onPageChange?: (page: number) => void; onClose: () => void; onError?: (message: string) => void;
  fullPage?: boolean; returnLabel?: string; initialViewState?: Partial<ReaderViewState>; onViewStateChange?: (state: ReaderViewState) => void;
  citations?: AnswerEvidence[]; onEvidenceChange?: (evidence: AnswerEvidence) => void; onReviewSource?: () => void;
};
type LocatedEvidence = Omit<AnswerEvidence, 'kind'> & { kind: string; localization_scope?: string; quote_origin?: string;
  extraction?: string; visual_reading?: { localization_scope?: string } };
const SIZE = 50;
const validBox = (box: unknown): box is PageBox => Array.isArray(box) && box.length === 4 &&
  box.every(n => typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 1) && box[0] < box[2] && box[1] < box[3];
const isVisual = (e?: LocatedEvidence) => !!e && (['chart_observation','formula_structure','formula_parameter'].includes(e.kind) ||
  e.extraction === 'independently_reviewed_visual_transcription' || !!e.visual_reading);
const boundedZoom = (value: number | undefined) => Number.isFinite(value) ? Math.max(1, Math.min(3, value!)) : 1;
const citationIdentity = (e?: LocatedEvidence) => e ? JSON.stringify([e.source_id,e.id,e.sha256,e.parser_version,e.page,e.table_name,e.start,e.end,e.quote,e.row_numbers]) : '';

/** A fresh session on every citation change prevents even one frame of old source evidence. */
export function ReadingPanel(props: ReadingPanelProps) {
  const e = props.evidence as LocatedEvidence | undefined;
  const focusOrigin = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const key = JSON.stringify([props.sourceId,e?.source_id,e?.id,e?.sha256,e?.parser_version,e?.page,e?.table_name,
    e?.start,e?.end,e?.quote,e?.stale,e?.row_numbers,e?.bbox,e?.localization_scope,e?.extraction,e?.visual_reading,props.evidenceRole]);
  return <ReadingSession key={key} {...props} focusOrigin={focusOrigin.current} />;
}

function ReadingSession({ sourceId, evidence, evidenceRole = 'published', initialPage, onPageChange, onClose, onError, focusOrigin,
  claimText, fullPage = false, returnLabel = '返回回答', initialViewState, onViewStateChange, citations = [], onEvidenceChange, onReviewSource }: ReadingPanelProps & {focusOrigin:HTMLElement|null}) {
  const [warningsOpen,setWarningsOpen]=useState(false),[warningLimit,setWarningLimit]=useState(30);
  const [detail, setDetail] = useState<SourceDetail | null>(null);
  const [error, setError] = useState(''); const [attempt, setAttempt] = useState(0);
  const [tab, setTab] = useState<ReadingTab>('text'); const [pageNumber, setPageNumber] = useState<number | null>(null);
  const [tableName, setTableName] = useState('');
  const [tableOffset, setTableOffset] = useState(Math.max(0, Math.floor((initialViewState?.tableOffset ?? 0) / SIZE) * SIZE));
  const [tableMode, setTableMode] = useState<'data' | 'schema'>(initialViewState?.tableMode === 'schema' ? 'schema' : 'data');
  const [tableQuery, setTableQuery] = useState(initialViewState?.tableQuery ?? '');
  const [hiddenColumns, setHiddenColumns] = useState<string[]>(initialViewState?.hiddenColumns ?? []);
  const [zoom, setZoom] = useState(boundedZoom(initialViewState?.zoom));
  const [search, setSearch] = useState(initialViewState?.search ?? ''); const [searchOpen, setSearchOpen] = useState(!!initialViewState?.search);
  const [matchIndex, setMatchIndex] = useState(0); const [jumpNonce, setJumpNonce] = useState(0);
  const [outlineOpen, setOutlineOpen] = useState(false); const [scrollTop, setScrollTop] = useState(initialViewState?.scrollTop ?? 0);
  const [restoreScrollTop, setRestoreScrollTop] = useState(initialViewState?.scrollTop);
  const [small, setSmall] = useState(() => window.matchMedia('(max-width: 1279px)').matches);
  const overlay = !fullPage && small;
  const outline = useRef<HTMLElement>(null); const panel = useRef<HTMLElement>(null); const close = useRef<HTMLButtonElement>(null); const body = useRef<HTMLDivElement>(null);
  const searchInput = useRef<HTMLInputElement>(null); const restored = useRef(false);
  const onErrorRef = useRef(onError); onErrorRef.current = onError;
  const onPageRef = useRef(onPageChange); onPageRef.current = onPageChange;
  const onStateRef = useRef(onViewStateChange); onStateRef.current = onViewStateChange;
  const initial = useRef(initialViewState); const returnFocus = useRef(focusOrigin);
  const requestedPage = useRef(initialPage); requestedPage.current = initialPage;
  const appliedPageRequest = useRef(initialPage);
  const tabs = useRef<Partial<Record<ReadingTab, HTMLButtonElement | null>>>({});
  const uid = useId(); const citation = evidence as LocatedEvidence | undefined;
  useEffect(() => {
    const query = window.matchMedia('(max-width: 1279px)'); const update = () => setSmall(query.matches);
    query.addEventListener('change', update); return () => query.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    if (!overlay) return;
    const old = document.body.style.overflow; document.body.style.overflow = 'hidden';
    const background = document.getElementById('root'); const wasInert = background?.inert;
    if (background) background.inert = true;
    close.current?.focus({ preventScroll: true });
    return () => { document.body.style.overflow = old; if (background) background.inert = wasInert ?? false; };
  }, [overlay]);
  useEffect(() => {
    close.current?.focus({ preventScroll: true });
    return () => { const target = returnFocus.current; if (target?.isConnected && !fullPage) target.focus({ preventScroll: true }); };
  }, [fullPage]);
  useEffect(() => {
    const controller = new AbortController(); setDetail(null); setError(''); restored.current = false;
    api<SourceDetail>(`/sources/${encodeURIComponent(sourceId)}`, { signal: controller.signal }).then(value => {
      if (controller.signal.aborted) return;
      if (value.source.id !== sourceId) throw new Error('返回的资料与当前选择不一致，请重新打开。');
      const source = value.source;
      const current = citation && citation.source_id === sourceId && !citation.stale && citation.sha256 === source.sha256 && citation.parser_version === source.parser_version && (!citation.revision || citation.revision === source.content_revision);
      const pages = value.pages ?? []; const tables = value.tables ?? [];
      const selectedPage = resolveReaderPage(pages, requestedPage.current, initial.current?.page, current ? citation.page : undefined);
      appliedPageRequest.current = requestedPage.current;
      setPageNumber(selectedPage);
      if (initial.current?.page !== selectedPage && pages.length) { setRestoreScrollTop(undefined); setScrollTop(0); }
      const name = tables.some(t => t.name === initial.current?.tableName) ? initial.current!.tableName! : current && tables.some(t => t.name === citation.table_name) ? citation.table_name! : tables[0]?.name ?? '';
      setTableName(name);
      if (initial.current?.tableOffset === undefined && current && citation.table_name === name && citation.row_numbers?.length) setTableOffset(Math.floor((Math.max(1,citation.row_numbers[0])-1)/SIZE)*SIZE);
      const original = ['pdf','png','jpg','jpeg'].includes(source.format) && pages.length > 0;
      const savedTab = initial.current?.tab;
      setTab(savedTab === 'original' && original || savedTab === 'tables' && tables.length || savedTab === 'text' && pages.length ? savedTab! : current && citation.table_name && tables.length ? 'tables' : original && (fullPage || current && (citation.kind !== 'text' || isVisual(citation))) ? 'original' : pages.length ? 'text' : tables.length ? 'tables' : 'text');
      setDetail({...value,pages,tables,metadata:value.metadata ?? {}});
    }).catch(reason => { if (!controller.signal.aborted && !isAbort(reason)) { const message = messageOf(reason); setError(message); onErrorRef.current?.(message); } });
    return () => controller.abort();
  }, [sourceId, attempt]); // Session key owns the immutable citation snapshot.
  useEffect(() => {
    if (!detail || appliedPageRequest.current === initialPage) return;
    appliedPageRequest.current = initialPage;
    if (initialPage === undefined || initialPage === pageNumber || !detail.pages.some(page => page.number === initialPage)) return;
    // A URL/history navigation updates this session; an echo of a local page change leaves it untouched.
    setPageNumber(initialPage); setRestoreScrollTop(undefined); setScrollTop(0); setOutlineOpen(false);
    if (tab === 'tables') setTab(['pdf','png','jpg','jpeg'].includes(detail.source.format) ? 'original' : 'text');
    if (body.current) body.current.scrollTop = 0;
  }, [initialPage,detail]);
  useEffect(() => { if (pageNumber !== null) onPageRef.current?.(pageNumber); }, [pageNumber]);
  useEffect(() => {
    if (detail) onStateRef.current?.({tab,page:pageNumber,tableName,tableOffset,tableMode,tableQuery,hiddenColumns,zoom,search,scrollTop});
  }, [detail,tab,pageNumber,tableName,tableOffset,tableMode,tableQuery,hiddenColumns,zoom,search,scrollTop]);
  useEffect(() => {
    if (!detail || restored.current || !body.current) return;
    const frame = requestAnimationFrame(() => { if (body.current) body.current.scrollTop = restoreScrollTop ?? 0; restored.current = true; });
    return () => cancelAnimationFrame(frame);
  }, [detail]);
  useEffect(()=>{
    const container=outline.current;if(!container)return;
    const selected=container.querySelector<HTMLElement>('[aria-current]');if(!selected)return;
    const top=selected.offsetTop-container.offsetTop,bottom=top+selected.offsetHeight;
    if(top<container.scrollTop||bottom>container.scrollTop+container.clientHeight)container.scrollTop=Math.max(0,top-(container.clientHeight-selected.offsetHeight)/2);
  },[detail,pageNumber,tableName,tab,outlineOpen]);
  const source = detail?.source;
  const stale = !!citation && (!!citation.stale || citation.source_id !== sourceId || !!source && (citation.sha256 !== source.sha256 || citation.parser_version !== source.parser_version || !!citation.revision && citation.revision !== source.content_revision));
  const currentCitation = !stale && source ? citation : undefined;
  const page = detail?.pages.find(p => p.number === pageNumber);
  const pageIndex = detail?.pages.findIndex(p => p.number === pageNumber) ?? -1;
  const logical = detail?.metadata.page_unit === 'logical_section';
  const hasOriginal = !!source && ['pdf','png','jpg','jpeg'].includes(source.format) && !!detail?.pages.length;
  const choices: { id: ReadingTab; title: string; available: boolean }[] = [
    { id:'original',title:'原页',available:hasOriginal },{ id:'text',title:'文字',available:!!detail?.pages.length },
    { id:'tables',title:'表格',available:!!detail?.tables.length }];
  const available = choices.filter(c => c.available);
  const matches = useMemo(() => findTextMatches(detail?.pages ?? [], search), [detail,search]);
  const activeMatch = matches[matchIndex];
  const sourceCitations = Array.from(new Map(citations.filter(e => e.source_id === sourceId).map(e => [citationIdentity(e),e])).values());
  const citationIndex = sourceCitations.findIndex(e => citationIdentity(e) === citationIdentity(citation));
  const canReturnToCitation = !!currentCitation && (!!currentCitation.table_name && !!detail?.tables.some(t => t.name === currentCitation.table_name) || currentCitation.page !== undefined && !!detail?.pages.some(p => p.number === currentCitation.page));
  const choosePage = (number: number) => { setPageNumber(number); setRestoreScrollTop(undefined); setScrollTop(0); setOutlineOpen(false); if (body.current) body.current.scrollTop = 0; };
  const chooseTable = (name: string) => { setTableName(name); setTableOffset(0); setTableQuery(''); setHiddenColumns([]); setOutlineOpen(false); if(body.current) body.current.scrollTop = 0; };
  const jumpToCitation = () => {
    if (!currentCitation) return;
    setRestoreScrollTop(undefined);
    setSearch(''); setSearchOpen(false);
    if (currentCitation.table_name && detail?.tables.some(t => t.name === currentCitation.table_name)) {
      setTab('tables'); setTableName(currentCitation.table_name); setTableMode('data'); setTableQuery('');
      setTableOffset(currentCitation.row_numbers?.length ? Math.floor((Math.max(1,currentCitation.row_numbers[0])-1)/SIZE)*SIZE : 0);
    } else if (currentCitation.page && detail?.pages.some(p => p.number === currentCitation.page)) {
      setPageNumber(currentCitation.page); if (tab === 'tables') setTab(hasOriginal ? 'original' : 'text');
    }
    setJumpNonce(n => n+1);
  };
  const navigateMatch = (direction: number) => {
    if (!matches.length) return;
    const next = (matchIndex + direction + matches.length) % matches.length;
    setMatchIndex(next); setRestoreScrollTop(undefined); setPageNumber(matches[next].page); setTab('text');
  };
  const updateSearch = (value: string) => {
    setSearch(value); setMatchIndex(0); const found = findTextMatches(detail?.pages ?? [],value);
    if (found[0]) { setRestoreScrollTop(undefined); setPageNumber(found[0].page); setTab('text'); }
  };
  const moveTab = (event: KeyboardEvent, current: ReadingTab) => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault(); const index = available.findIndex(c => c.id === current);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? available.length - 1 :
      (index + (event.key === 'ArrowRight' ? 1 : -1) + available.length) % available.length;
    const target = available[next]?.id; if (target) { setTab(target); tabs.current[target]?.focus(); }
  };
  const panelKeys = (event: KeyboardEvent) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === 'Escape') {
      if (searchOpen) { event.preventDefault(); setSearchOpen(false); setSearch(''); return; }
      if (outlineOpen) { event.preventDefault(); setOutlineOpen(false); return; }
      if (!fullPage) { event.preventDefault(); event.stopPropagation(); onClose(); }
      return;
    }
    if (!overlay || event.key !== 'Tab') return;
    const items = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],summary,select:not(:disabled),input:not(:disabled),[tabindex="0"]') ?? []).filter(e => e.offsetParent !== null);
    const first = items[0], last = items.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  };
  const content = <div className={`reading-host${overlay ? ' is-overlay' : ''}${fullPage ? ' is-full-page' : ''}${tab === 'tables' ? ' is-table-reader' : ''}${tab === 'tables' && available.length === 1 ? ' is-table-only' : ''}`}>
    {overlay && <div className="reading-scrim" aria-hidden="true" onMouseDown={onClose} />}
    <section className="reading-panel" ref={panel} role={overlay ? 'dialog' : 'region'} aria-modal={overlay || undefined} aria-labelledby={`${uid}-title`} onKeyDown={panelKeys}>
      <header className="reading-heading">
        <button ref={close} type="button" className={fullPage ? 'reading-back' : 'reading-icon'} onClick={onClose} aria-label={fullPage ? returnLabel : '关闭资料阅读，返回回答'}>{fullPage ? <><ArrowLeft size={19} aria-hidden="true" /><span>{returnLabel}</span></> : <X size={19} aria-hidden="true" />}</button>
        <h2 id={`${uid}-title`}>{fullPage && source ? source.name : '来源核验'}</h2>
        {source && <div className="reading-heading-actions"><a className="reading-button" href={`/api/sources/${encodeURIComponent(sourceId)}/original?download=false`} target="_blank" rel="noreferrer">打开原件<ArrowUpRight size={15} aria-hidden="true" /></a>{onReviewSource && <button className="reading-button reading-review-button" type="button" onClick={onReviewSource}>解析与版本</button>}</div>}
      </header>
      {error ? <div className="reading-state" role="alert"><strong>资料没有打开</strong><p>{error}</p><button onClick={() => setAttempt(n => n+1)}>重新读取</button></div>
        : !detail ? <div className="reading-state" role="status"><span className="reading-loading" /><p>正在打开所选资料…</p></div> : <>
          <div className="reading-file">
            {!fullPage && <h3><FileText size={18} aria-hidden="true" />{source!.name}</h3>}
            <div className="reading-file-meta"><span>{source!.format.toUpperCase()}{logical ? ` · ${detail.pages.length} 个逻辑分段` : detail.pages.length ? ` · ${detail.pages.length} 页` : detail.tables.length ? ` · ${detail.tables.length} 张表` : ''}{tab === 'tables' && tableName ? ` · 当前表 ${tableName}` : ''}</span>
              <details className="reading-version"><summary>版本信息<ChevronDown size={14} aria-hidden="true" /></summary><dl><div><dt>文件摘要</dt><dd>{source!.sha256}</dd></div><div><dt>解析版本</dt><dd>{source!.parser_version}</dd></div>{source!.content_revision && <div><dt>内容版本</dt><dd>{source!.content_revision}</dd></div>}{stale && citation && <><div><dt>引用摘要</dt><dd>{citation.sha256 || '未记录'}</dd></div><div><dt>引用解析版本</dt><dd>{citation.parser_version || '未记录'}</dd></div></>}</dl></details>
              {onReviewSource && <button className="reading-review-mobile" type="button" onClick={onReviewSource}>解析与版本</button>}
            </div>
          </div>
          {source!.origin?.startsWith('project-demo:') && <p className="reading-warning">自编演示资料，数据与人物均为虚构。{source!.derived_from ? `派生自 ${source!.derived_from}，不作为独立佐证。` : ''}</p>}
          {(stale || source!.archived) && <p className="reading-warning" role="status">{stale ? '回答引用的是历史版本。以下为当前保留内容，旧引用位置不作高亮。' : '此资料已归档；正在查看保留的原件。'}{stale && onReviewSource && <button type="button" onClick={onReviewSource}>核对版本差异</button>}</p>}
          {['queued','processing','failed','cancelled'].includes(source!.status) && <div className="reading-processing" role="status"><span>{source!.status === 'failed' ? '资料处理失败，当前内容可能不完整。' : source!.status === 'cancelled' ? '资料处理已取消，保留已完成内容。' : '资料正在处理，当前内容可能不完整。'}</span><button type="button" onClick={() => setAttempt(n => n+1)}>刷新状态</button></div>}
          {claimText && <details className="reading-claim-context"><summary><strong>正在核对的结论</strong><span>{claimText}</span></summary><p>{claimText}</p></details>}
          {page?.printed_page_label&&<p className="reading-page-label">物理页 {page.number} · 原件印刷页码 {page.printed_page_label}</p>}
          {citation && <CitationNote evidence={citation} stale={stale} logical={logical} evidenceRole={evidenceRole} />}
          {!!source!.warnings?.length && <details className="reading-notes" open={warningsOpen} onToggle={e=>setWarningsOpen(e.currentTarget.open)}><summary>{source!.warnings.length} 项资料提示</summary>{warningsOpen&&<><ul>{source!.warnings.slice(0,warningLimit).map((warning,i) => <li key={i}>{warning}</li>)}</ul>{warningLimit<source!.warnings.length&&<button onClick={()=>setWarningLimit(warningLimit+50)}>继续显示（剩余 {source!.warnings.length-warningLimit} 项）</button>}</>}</details>}
          <div className={`reading-source-workspace${outlineOpen ? ' outline-open' : ''}`}>
            {fullPage && <nav ref={outline} className="reading-outline" aria-label={tab === 'tables' ? '原始数据表' : logical ? '文档分段' : '文档页面'}>
              <div className="reading-outline-label">{tab === 'tables' ? '数据表' : logical ? '文档分段' : '页面'}<button type="button" className="reading-outline-close reading-icon" onClick={() => setOutlineOpen(false)} aria-label="收起目录"><X size={17} aria-hidden="true" /></button></div>
              {tab === 'tables' ? detail.tables.map(t => <button className="reading-tree-table" type="button" key={t.name} aria-current={tableName === t.name ? 'true' : undefined} onClick={() => chooseTable(t.name)}><Rows3 size={16} aria-hidden="true" /><span>{t.name}</span><small>{t.row_count.toLocaleString()}</small></button>) : detail.pages.map(p => <button key={p.number} className={`reading-page-link${hasOriginal && tab === 'original' ? ' has-thumbnail' : ''}`} type="button" aria-current={p.number === pageNumber ? 'page' : undefined} onClick={() => choosePage(p.number)}>
                {hasOriginal && tab === 'original' && <img loading="lazy" src={`/api/sources/${encodeURIComponent(sourceId)}/pages/${p.number}/image`} alt="" />}
                <span>{logical ? `${p.number}. ${p.title || '未命名分段'}` : `第 ${p.number} 页`}</span>{!logical && tab !== 'original' && p.title && <small>{p.title}</small>}
              </button>)}
            </nav>}
            <div className="reading-viewer">
              <div className="reading-toolbar">
                {fullPage && <button className="reading-outline-toggle reading-icon" type="button" aria-expanded={outlineOpen} aria-label={tab === 'tables' ? '显示数据表列表' : '显示页面目录'} onClick={() => setOutlineOpen(value => !value)}><List size={18} aria-hidden="true" /></button>}
                {!!available.length && <div className="reading-tabs" role="tablist" aria-label="资料阅读方式">{available.map(choice => <button key={choice.id}
                  ref={element => { tabs.current[choice.id] = element; }} type="button" role="tab" id={`${uid}-${choice.id}-tab`}
                  aria-selected={tab === choice.id} aria-controls={`${uid}-${choice.id}-panel`} tabIndex={tab === choice.id ? 0 : -1}
                  onKeyDown={event => moveTab(event,choice.id)} onClick={() => setTab(choice.id)}>{choice.id === 'original' ? <Image size={16} aria-hidden="true" /> : choice.id === 'text' ? <FileText size={16} aria-hidden="true" /> : <Rows3 size={16} aria-hidden="true" />}{choice.title}</button>)}</div>}
                {tab !== 'tables' && !!detail.pages.length && <button type="button" className="reading-find-toggle" aria-expanded={searchOpen} onClick={() => { setSearchOpen(open => !open); requestAnimationFrame(() => searchInput.current?.focus()); }}><Search size={17} aria-hidden="true" />查找</button>}
                {(tab === 'text' || tab === 'original') && page && <nav className="reading-page-control" aria-label="页面导航">
                  <button type="button" className="reading-icon" aria-label={logical ? '上一段' : '上一页'} disabled={pageIndex <= 0} onClick={() => choosePage(detail.pages[pageIndex-1].number)}><ArrowLeft size={16} aria-hidden="true" /></button>
                  <label><span className="reading-sr">{logical ? '选择逻辑分段' : '选择页面'}</span><select value={page.number} onChange={event => choosePage(Number(event.target.value))}>{detail.pages.map(p => <option key={p.number} value={p.number}>{p.number}{logical ? ' 段' : ''}</option>)}</select></label>
                  <span className="reading-page-count">/ {detail.pages.length}</span>
                  <button type="button" className="reading-icon" aria-label={logical ? '下一段' : '下一页'} disabled={pageIndex < 0 || pageIndex >= detail.pages.length-1} onClick={() => choosePage(detail.pages[pageIndex+1].number)}><ArrowRight size={16} aria-hidden="true" /></button>
                </nav>}
                {tab === 'original' && <div className="reading-zoom" role="group" aria-label="原页缩放"><button type="button" className="reading-icon" disabled={zoom <= 1} aria-label="缩小原页" onClick={() => setZoom(z => Math.max(1,z-.25))}><Minus size={16} aria-hidden="true" /></button><span>{Math.round(zoom*100)}%</span><button type="button" className="reading-icon" disabled={zoom >= 3} aria-label="放大原页" onClick={() => setZoom(z => Math.min(3,z+.25))}><Plus size={16} aria-hidden="true" /></button><button type="button" className="reading-fit" onClick={() => setZoom(1)}>适合宽度</button></div>}
              </div>
              {searchOpen && tab !== 'tables' && <div className="reading-find"><label><Search size={16} aria-hidden="true" /><span className="reading-sr">查找已提取文字</span><input ref={searchInput} value={search} placeholder="查找已提取文字" onChange={event => updateSearch(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); navigateMatch(event.shiftKey ? -1 : 1); } }} /></label><span role="status">{search ? `${matches.length ? matchIndex+1 : 0} / ${matches.length}` : '全文文字'}</span><button className="reading-icon" type="button" disabled={!matches.length} onClick={() => navigateMatch(-1)} aria-label="上一处查找结果"><ArrowLeft size={16} aria-hidden="true" /></button><button className="reading-icon" type="button" disabled={!matches.length} onClick={() => navigateMatch(1)} aria-label="下一处查找结果"><ArrowRight size={16} aria-hidden="true" /></button><button className="reading-icon" type="button" onClick={() => {setSearchOpen(false); setSearch('');}} aria-label="关闭查找"><X size={16} aria-hidden="true" /></button></div>}
              <div className="reading-body" ref={body} onScroll={event => setScrollTop(event.currentTarget.scrollTop)} role={available.length ? 'tabpanel' : undefined} id={`${uid}-${tab}-panel`} aria-labelledby={available.some(c => c.id === tab) ? `${uid}-${tab}-tab` : undefined} tabIndex={0}>
                {tab === 'text' && (page ? <TextPage key={page.number} page={page} evidence={currentCitation} logical={logical} searchMatches={matches.filter(match => match.page === page.number)} activeMatch={activeMatch} jumpNonce={jumpNonce} restorePosition={restoreScrollTop !== undefined && jumpNonce === 0} onOriginal={hasOriginal ? () => setTab('original') : undefined} /> : <div className="reading-state"><p>当前资料没有已解析文字。可打开原件查看。</p></div>)}
                {tab === 'original' && page && <PageImage key={`${sourceId}-${page.number}`} sourceId={sourceId} page={page.number} name={source!.name} evidence={currentCitation?.page === page.number ? currentCitation : undefined} zoom={zoom} jumpNonce={jumpNonce} restorePosition={restoreScrollTop} />}
                {tab === 'tables' && <>{!fullPage && <label className="reading-table-picker">数据表<select value={tableName} onChange={event => chooseTable(event.target.value)}>{detail.tables.map(t => <option key={t.name} value={t.name}>{t.name} · {t.row_count.toLocaleString()} 行</option>)}</select></label>}
                  {tableName && <DataRows key={`${sourceId}-${tableName}`} sourceId={sourceId} tableName={tableName} offset={tableOffset} onOffsetChange={setTableOffset} mode={tableMode} onModeChange={setTableMode} query={tableQuery} onQueryChange={setTableQuery} hiddenColumns={hiddenColumns} onHiddenColumnsChange={setHiddenColumns} jumpNonce={jumpNonce}
                    rowNumbers={currentCitation?.table_name === tableName ? currentCitation.row_numbers ?? [] : []} onError={onErrorRef.current} />}</>}
              </div>
              {citation && <nav className="reading-citation-navigation" aria-label="引用定位">{sourceCitations.length > 1 && onEvidenceChange && <><button type="button" disabled={citationIndex <= 0} onClick={() => onEvidenceChange(sourceCitations[citationIndex-1])}><ArrowLeft size={15} aria-hidden="true" />上一处</button><label><span className="reading-sr">选择引用</span><select value={citationIndex} onChange={event => { const selected = sourceCitations[Number(event.target.value)]; if (selected) onEvidenceChange(selected); }}>{citationIndex < 0 && <option value={-1}>当前引用</option>}{sourceCitations.map((e,index) => <option key={citationIdentity(e)} value={index}>引用 {e.id}{e.page ? ` · ${e.page}${logical ? '段' : '页'}` : ''}</option>)}</select></label><button type="button" disabled={citationIndex < 0 || citationIndex >= sourceCitations.length-1} onClick={() => onEvidenceChange(sourceCitations[citationIndex+1])}>下一处<ArrowRight size={15} aria-hidden="true" /></button></>}<button className="reading-return" type="button" disabled={!canReturnToCitation} onClick={jumpToCitation}>回到引用位置</button></nav>}
            </div>
          </div>
        </>}
    </section>
  </div>;
  return fullPage ? content : createPortal(content,document.body);
}

function CitationNote({ evidence, stale, logical, evidenceRole }: { evidence: LocatedEvidence; stale: boolean; logical: boolean; evidenceRole: 'published' | 'retrieved' }) {
  const visual = isVisual(evidence),[expanded,setExpanded]=useState(false);
  const quote=evidence.quote || evidence.label || evidence.table_name || '';
  return <section className={`reading-citation${expanded ? ' is-expanded' : ''}${stale ? ' is-stale' : ''}`} aria-label="当前引用"><p className="reading-citation-heading"><strong>{evidenceRole === 'retrieved' ? '正在查看检索片段' : '对应原文依据'}</strong><span>{quote}</span><button className="reading-quote-expand" aria-expanded={expanded} onClick={()=>setExpanded(v=>!v)}>{expanded?'收起':'展开片段'}</button><b>[{evidence.id}]</b>{evidence.page ? <small>{logical ? `逻辑分段 ${evidence.page}` : `物理页 ${evidence.page}`}</small> : null}</p>
    {evidenceRole === 'retrieved' && <small>此片段尚未支持已发布结论。</small>}{visual && <small>定位范围为原始页；独立复核转录与原图分开展示。</small>}{evidence.kind === 'table' && !evidence.row_numbers?.length && <small>引用整张表，未指定独立原始行。</small>}
  </section>;
}

function TextPage({ page,evidence,logical,onOriginal,searchMatches,activeMatch,jumpNonce,restorePosition }: { page: Page; evidence?: LocatedEvidence; logical: boolean; onOriginal?: () => void; searchMatches:TextMatch[]; activeMatch?:TextMatch; jumpNonce:number; restorePosition:boolean }) {
  const citationMark = useRef<HTMLElement>(null); const searchMark = useRef<HTMLElement>(null);
  const span = evidence?.kind === 'text' && !isVisual(evidence) ? exactCitationSpan(page,evidence) : null;
  const chars = Array.from(page.text);
  const located = evidence?.kind === 'text' && !isVisual(evidence) && evidence.page === page.number;
  useEffect(() => { if (!restorePosition) citationMark.current?.scrollIntoView({block:'center',inline:'nearest'}); }, [page.number,evidence?.id,jumpNonce,restorePosition]);
  useEffect(() => { searchMark.current?.scrollIntoView({block:'center',inline:'nearest'}); }, [activeMatch]);
  const boundaries = new Set([0,chars.length,...(span ?? []),...searchMatches.flatMap(match => [match.start,match.end])]);
  const offsets = Array.from(boundaries).sort((a,b) => a-b);
  let matchCursor = 0;
  const body: ReactNode[] = offsets.slice(0,-1).map((start,index) => {
    const end = offsets[index+1], text = chars.slice(start,end).join('');
    const isCitation = !!span && start >= span[0] && end <= span[1];
    while (searchMatches[matchCursor]?.end <= start) matchCursor++;
    const candidate = searchMatches[matchCursor];
    const match = candidate && start >= candidate.start && end <= candidate.end ? candidate : undefined;
    const active = match && activeMatch?.page === page.number && match.start === activeMatch.start;
    return isCitation || match ? <mark key={start} ref={active && start === match?.start ? searchMark : isCitation && start === span?.[0] ? citationMark : undefined} className={`${isCitation ? 'is-citation' : ''}${active ? ' is-search-active' : match ? ' is-search-match' : ''}`} tabIndex={isCitation || active ? -1 : undefined}>{text}</mark> : <span key={start}>{text}</span>;
  });
  return <><div className="reading-text-note"><span>{page.extraction === 'ocr' ? '文字识别结果' : page.extraction === 'native' ? '原件文字' : '提取文字'}{logical ? ' · 逻辑分段，不是原件页码' : ''}</span>{onOriginal && <button type="button" onClick={onOriginal}>对照原页<ArrowRight size={14} aria-hidden="true" /></button>}</div>
    {located && !span && <p className="reading-warning">当前文字不能精确匹配这条引用，未猜测高亮位置；请对照原件。</p>}
    {page.text.trim() ? <article className="reading-paper">{body}</article> : <div className="reading-state"><p>这一页没有可读取文字。</p>{onOriginal && <button onClick={onOriginal}>查看原页</button>}</div>}
    {page.extraction === 'ocr' && <p className="reading-footnote">识别文字可能遗漏符号或排版关系；涉及数值、表格或公式时请同时看原页。</p>}</>;
}

function PageImage({sourceId,page,name,evidence,zoom,jumpNonce,restorePosition}:{sourceId:string;page:number;name:string;evidence?:LocatedEvidence;zoom:number;jumpNonce:number;restorePosition?:number}) {
  const [loaded,setLoaded] = useState(false); const [failed,setFailed] = useState(false); const [attempt,setAttempt] = useState(0);
  const canvas = useRef<HTMLDivElement>(null); const boxRef = useRef<HTMLSpanElement>(null);
  const box = validBox(evidence?.bbox) ? evidence!.bbox! : null;
  const wholePage = evidence?.localization_scope === 'original_page' || evidence?.visual_reading?.localization_scope === 'original_page' || !!box && box.every((v,i) => v === [0,0,1,1][i]);
  useEffect(() => {
    if (!loaded) return;
    if (!jumpNonce && restorePosition !== undefined) { const scroll = canvas.current?.closest('.reading-body'); if (scroll) scroll.scrollTop = restorePosition; return; }
    if (jumpNonce) (boxRef.current ?? canvas.current)?.scrollIntoView({block:boxRef.current ? 'center' : 'start',inline:'nearest'});
  }, [loaded,jumpNonce,restorePosition]);
  return <><p className="reading-image-note">{evidence ? wholePage ? '依据定位到原始页，未指定精确区域' : box ? '框线对应保存的原件坐标' : '依据定位到本页，未保存精确区域' : '原始页面'}</p>
    {failed ? <div className="reading-state" role="alert"><p>原页图像未能加载。可以重试或打开原件。</p><button onClick={() => {setAttempt(n => n+1);setLoaded(false);setFailed(false);}}>重新加载图像</button></div> : <>
      {!loaded && <p className="reading-image-loading" role="status">正在读取原页…</p>}
      <div className="reading-image-scroll" tabIndex={0} aria-label="原页图像，可缩放并横向滚动"><div ref={canvas} className="reading-image-canvas" style={{width:`${zoom*100}%`,visibility:loaded ? 'visible' : 'hidden'}}>
        <img key={attempt} src={`/api/sources/${encodeURIComponent(sourceId)}/pages/${page}/image${attempt ? `?retry=${attempt}` : ''}`} alt={`${name}，原始第 ${page} 页`}
          onLoad={event => {if (event.currentTarget.naturalWidth > 0 && event.currentTarget.naturalHeight > 0) setLoaded(true);else setFailed(true);}} onError={() => setFailed(true)} />
        {loaded && box && !wholePage && <span ref={boxRef} className="reading-image-box" aria-label="引用对应的原件区域" style={{left:`${box[0]*100}%`,top:`${box[1]*100}%`,width:`${(box[2]-box[0])*100}%`,height:`${(box[3]-box[1])*100}%`}} />}
      </div></div>
    </>}
  </>;
}

type DataRowsProps = { sourceId:string; tableName:string; rowNumbers:number[]; onError?:(message:string)=>void; offset:number; onOffsetChange:(offset:number)=>void; mode:'data'|'schema'; onModeChange:(mode:'data'|'schema')=>void; query:string; onQueryChange:(query:string)=>void; hiddenColumns:string[]; onHiddenColumnsChange:(columns:string[])=>void; jumpNonce:number };
function DataRows({sourceId,tableName,rowNumbers,onError,offset,onOffsetChange,mode,onModeChange,query,onQueryChange,hiddenColumns,onHiddenColumnsChange,jumpNonce}:DataRowsProps) {
  const [pageJump,setPageJump] = useState(String(Math.floor(offset/SIZE)+1));
  const [data,setData] = useState<TableRows|null>(null); const [error,setError] = useState(''); const [attempt,setAttempt] = useState(0); const [exported,setExported] = useState(false);
  const errorRef=useRef(onError);errorRef.current=onError;const tableRef=useRef<HTMLDivElement>(null);const columnMenu=useRef<HTMLDetailsElement>(null);
  const modes = useRef<Partial<Record<'data'|'schema',HTMLButtonElement|null>>>({}); const uid = useId();
  useEffect(() => { const controller=new AbortController();setData(null);setError('');setExported(false);
    api<TableRows>(`/sources/${encodeURIComponent(sourceId)}/tables/${encodeURIComponent(tableName)}?offset=${offset}&limit=${SIZE}`,{signal:controller.signal})
      .then(value => {if (!controller.signal.aborted) { if (value.name !== tableName || value.offset !== offset) throw new Error('表格响应与当前页面不一致，请重试。'); if (offset > 0 && offset >= value.row_count) { onOffsetChange(Math.floor(Math.max(0,value.row_count-1)/SIZE)*SIZE); return; } setData(value); }})
      .catch(reason => {if (!controller.signal.aborted && !isAbort(reason)) {const message=messageOf(reason);setError(message);errorRef.current?.(message);}});
    return () => controller.abort();
  },[sourceId,tableName,offset,attempt]);
  useEffect(() => {tableRef.current?.querySelector('.is-cited')?.scrollIntoView({block:'nearest',inline:'nearest'});},[data,jumpNonce]);
  useEffect(() => { const outside = (event:PointerEvent) => { if(columnMenu.current?.open && event.target instanceof Node && !columnMenu.current.contains(event.target)) columnMenu.current.open=false; }; document.addEventListener('pointerdown',outside); return () => document.removeEventListener('pointerdown',outside); },[]);
  useEffect(() => { setPageJump(String(Math.floor(offset/SIZE)+1)); }, [offset]);
  const selectedColumns = data?.columns.map((column,index) => ({...column,index})).filter(column => !hiddenColumns.includes(column.name)) ?? [];
  const visibleRows = data?.rows.map((row,index) => ({row,number:offset+index+1})).filter(({row}) => !query.trim() || row.some(cell => readerCellText(cell).toLocaleLowerCase().includes(query.toLocaleLowerCase().trim()))) ?? [];
  const exportCsv = () => {
    if (!data || !selectedColumns.length) return;
    const csv = readerCsv(selectedColumns.map(column=>column.name),visibleRows.map(({row})=>selectedColumns.map(column=>row[column.index])));
    const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));const anchor=document.createElement('a');anchor.href=url;
    anchor.download=`${tableName.replace(/[\\/:*?"<>|]/g,'_')}_第${Math.floor(offset/SIZE)+1}页${query.trim() ? '_筛选结果' : ''}.csv`;document.body.appendChild(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);setExported(true);
  };
  const changeMode = (event:KeyboardEvent,current:'data'|'schema') => {if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const next=event.key==='Home'?'data':event.key==='End'?'schema':current==='data'?'schema':'data';onModeChange(next);modes.current[next]?.focus();};
  if(error)return <div className="reading-state" role="alert"><p>{error}</p><button onClick={() => setAttempt(n => n+1)}>重试读取表格</button></div>;
  if(!data || data.offset !== offset || data.name !== tableName)return <div className="reading-state" role="status"><p>正在读取表格…</p></div>;
  return <><div className="reading-data-heading"><h3>{tableName}<span>共 {data.row_count.toLocaleString()} 行</span></h3><div className="reading-data-actions">{mode === 'data' && <><label className="reading-table-search"><Search size={16} aria-hidden="true" /><span className="reading-sr">搜索当前页</span><input value={query} placeholder="搜索当前页" onChange={event=>{onQueryChange(event.target.value);setExported(false);}} /></label>
    <details ref={columnMenu} className="reading-column-menu" onKeyDown={event=>{if(event.key==='Escape'&&columnMenu.current?.open){event.stopPropagation();columnMenu.current.open=false;columnMenu.current.querySelector('summary')?.focus();}}}><summary><Columns3 size={16} aria-hidden="true" />列显示<ChevronDown size={14} aria-hidden="true" /></summary><div>{data.columns.map(column=><label key={column.name}><input type="checkbox" checked={!hiddenColumns.includes(column.name)} disabled={selectedColumns.length===1&&!hiddenColumns.includes(column.name)} onChange={event=>{onHiddenColumnsChange(event.target.checked?hiddenColumns.filter(name=>name!==column.name):[...hiddenColumns,column.name]);setExported(false);}} />{column.name}</label>)}<button type="button" onClick={()=>onHiddenColumnsChange([])}>显示全部列</button></div></details>
    <button className="reading-button" type="button" onClick={exportCsv} disabled={!selectedColumns.length}>{exported?<Check size={16} aria-hidden="true" />:<Download size={16} aria-hidden="true" />}导出当前页 CSV</button></>}</div></div>
    <div className="reading-data-tabs" role="tablist" aria-label="数据表查看方式">{(['data','schema'] as const).map(item=><button type="button" key={item} ref={element=>{modes.current[item]=element;}} role="tab" id={`${uid}-${item}`} aria-selected={mode===item} aria-controls={`${uid}-panel`} tabIndex={mode===item?0:-1} onClick={()=>onModeChange(item)} onKeyDown={event=>changeMode(event,item)}>{item==='data'?'数据':'Schema'}</button>)}</div>
    <div role="tabpanel" id={`${uid}-panel`} aria-labelledby={`${uid}-${mode}`}>
      {mode==='schema'?<div className="reading-table-scroll" tabIndex={0} aria-label={`${tableName} Schema`}><table><caption>当前表的原始字段与类型</caption><thead><tr><th scope="col">序号</th><th scope="col">字段</th><th scope="col">类型</th></tr></thead><tbody>{data.columns.map((column,index)=><tr key={column.name}><th scope="row">{index+1}</th><td>{column.name}</td><td>{column.type || '未记录'}</td></tr>)}</tbody></table></div>:<>
        <p className="reading-data-notice">原始数据 · 未套用回答中的汇总规则。{query.trim()?` 当前页匹配 ${visibleRows.length} / ${data.rows.length} 行，未搜索其他页。`:''}</p>
        <div className="reading-table-scroll" ref={tableRef} tabIndex={0} aria-label={`${tableName}，可横向滚动`}><table><caption className="reading-sr">{tableName} · 只读原始数据</caption><thead><tr><th scope="col">行</th>{selectedColumns.map(column=><th scope="col" key={column.index} className={/INT|REAL|FLOAT|DOUBLE|DECIMAL|NUMERIC/i.test(column.type)?'is-number':undefined}>{column.name}<small>{column.type}</small></th>)}</tr></thead><tbody>{visibleRows.map(({row,number})=><tr key={number} className={rowNumbers.includes(number)?'is-cited':undefined}><th scope="row">{number}{rowNumbers.includes(number)&&<span className="reading-sr">，引用行</span>}</th>{selectedColumns.map(column=><td key={column.index} className={`${row[column.index]===null?'is-null ':''}${typeof row[column.index]==='number'||/INT|REAL|FLOAT|DOUBLE|DECIMAL|NUMERIC/i.test(column.type)?'is-number':''}`} title={row[column.index]===null?'空值（NULL），不是 0':undefined}>{readerCellText(row[column.index])}</td>)}</tr>)}</tbody></table>
          {!visibleRows.length&&<p className="reading-state">{data.rows.length?'当前页没有匹配行。可清空搜索或切换下一页。':'这张表没有数据行。'}</p>}</div>
        <nav className="reading-table-pager" aria-label="表格分页"><span>{data.row_count?`${offset+1}–${Math.min(offset+data.rows.length,data.row_count)}`:'0'} / {data.row_count.toLocaleString()} 行 · 每页 {SIZE} 行</span><div><button type="button" disabled={offset===0} onClick={()=>onOffsetChange(Math.max(0,offset-SIZE))}>上一页</button><form className="reading-page-jump" onSubmit={event=>{event.preventDefault();const value=Number(pageJump);if(Number.isInteger(value)&&value>=1&&value<=Math.max(1,Math.ceil(data.row_count/SIZE)))onOffsetChange((value-1)*SIZE);}}><label><span className="reading-sr">跳转表格页码</span><input aria-label="表格页码" type="number" min={1} max={Math.max(1,Math.ceil(data.row_count/SIZE))} value={pageJump} onChange={event=>setPageJump(event.target.value)} required /></label><span>/ {Math.max(1,Math.ceil(data.row_count/SIZE)).toLocaleString()}</span><button type="submit">跳转</button></form><button type="button" disabled={offset+data.rows.length>=data.row_count||!data.rows.length} onClick={()=>onOffsetChange(offset+SIZE)}>下一页</button></div></nav>
        <p className="reading-export-note" role={exported?'status':undefined}>{exported?'已生成当前页 CSV，包含当前筛选结果与可见列。':'CSV 仅导出当前页的筛选结果与可见列；NULL 输出为空单元格。'}</p>
      </>}
    </div>
  </>;
}
