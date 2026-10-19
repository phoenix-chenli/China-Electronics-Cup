'use strict';
(() => {
  const D = window.REVIEW_DATA, A = window.REVIEW_ATTEMPTS, M = window.REVIEW_META;
  const $ = (id) => document.getElementById(id);
  const cases = new Map(D.cases.map(c => [c.case_id, c]));
  const attempts = new Map(A.attempts.map(a => [a.attempt_id, a]));
  const chainIds = new Set(D.chains.flatMap(c => c.turns.map(t => t.case_id)));
  const stringify = x => JSON.stringify(x, null, 2);
  const text = x => x == null ? '未记录' : typeof x === 'object' ? stringify(x) : String(x);
  const el = (tag, cls, value) => { const n = document.createElement(tag); if (cls) n.className = cls; if (value != null) n.textContent = text(value); return n; };
  const add = (parent, ...children) => { children.filter(Boolean).forEach(x => parent.append(x)); return parent; };
  const paragraph = (value, cls) => el('p', cls || '', value);
  const badge = (value, cls) => el('span', 'tag '+(cls || ''), value);
  const statusNames={completed:'已完成',needs_clarification:'需澄清',needs_evidence:'证据不足',insufficient_evidence:'依据不足',output_truncated:'输出截断',timeout:'超时',partial:'部分完成',partial_completed:'部分完成',cancelled:'已取消',failed:'失败',error:'错误',completed_partial:'部分完成'};
  const statusName=s=>statusNames[s]||s;
  function statusBadge(code){const b=badge(statusName(code),code==='completed'?'good':['failed','error','timeout'].includes(code)?'bad':'copper');b.title='真实记录状态：'+code;b.setAttribute('aria-label',statusName(code)+'；原始状态 '+code);return b;}
  const formatMs = n => typeof n === 'number' ? (n / 1000).toLocaleString('zh-CN', {maximumFractionDigits: 3})+' 秒' : '耗时未记录';
  const label = value => paragraph(value, 'body-label');
  const raw = value => el('pre', 'raw', stringify(value));
  function disclosure(title, build, opened=false) {
    const d = el('details', 'detail-block'); const summary=el('summary', '', title); add(d,summary); let ready=false;
    const fill=()=>{if(!ready){const body=el('div','detail-body');build(body);add(d,body);ready=true;}};
    d.addEventListener('toggle',()=>{if(d.open)fill();}); if(opened){fill();d.open=true;} return d;
  }
  const jsonBlock = (title, object) => disclosure(title, body=>add(body,raw(object)));
  function table(columns, rows, caption) {
    const wrap=el('div','table-wrap'), t=el('table','result-table');
    if(caption)add(t,el('caption','',caption));const head=el('thead'), tr=el('tr');
    columns.forEach(c=>{const th=el('th','',c);th.scope='col';add(tr,th);});add(head,tr);add(t,head);
    const body=el('tbody');(rows||[]).forEach(r=>{const row=el('tr');columns.forEach((_,i)=>{const value=Array.isArray(r)?r[i]:r[columns[i]];add(row,el('td',value===null?'null':'',value===null?'NULL':text(value)));});add(body,row);});add(t,body);return add(wrap,t);
  }
  function resultTables(parent, values) { (values||[]).forEach((t,i)=>{add(parent,label('完整结果表 '+(i+1)),table(t.columns||[],t.rows||[],t.title||''),paragraph(`已展示 ${t.rows?.length||0} 行；记录返回 ${t.returned_rows ?? t.rows?.length ?? 0} 行，总行数 ${t.total_rows ?? '未记录'}，截断：${t.truncated?'是':'否'}。`,'source-note'));}); }
  function queryList(p) {
    const candidates=[p?.query,...(p?.evidence||[]).map(e=>e.query)].filter(Boolean), seen=new Set();
    return candidates.filter(q=>{const key=stringify(q);if(seen.has(key)||!q.sql)return false;seen.add(key);return true;});
  }
  function queries(parent,p) {queryList(p).forEach((q,i)=>add(parent,disclosure('实际 SQL '+(i+1),body=>{add(body,el('pre','sql',q.sql),paragraph('绑定参数：'+stringify(q.parameters||[]),'source-meta'));if(q.query_plan)add(body,label('执行计划'),el('pre','',Array.isArray(q.query_plan)?q.query_plan.join('\n'):stringify(q.query_plan)));add(body,jsonBlock('完整查询记录',q));},true)));}
  function evidence(parent, values) {
    (values||[]).forEach(e=>{const box=el('article','evidence-item');add(box,el('h4','',`${e.id||'引用'} · ${e.source_name||e.label||e.kind||'依据'}`));
      const loc=[e.kind,e.page_label?e.page_label:e.page!=null?'物理页 '+e.page:null,e.table_name?'表 '+e.table_name:null].filter(Boolean).join(' · ');
      add(box,paragraph(loc,'source-meta'));if(e.quote)add(box,el('blockquote','quote',e.quote));if(e.note)add(box,paragraph(e.note));
      if(e.columns&&e.rows)add(box,table(e.columns,e.rows));if(e.query?.sql)add(box,el('pre','sql',e.query.sql));
      if(e.query_result?.columns&&e.query_result?.rows)add(box,table(e.query_result.columns,e.query_result.rows));
      if(e.sha256)add(box,paragraph('原件 SHA-256：'+e.sha256,'source-meta'));add(box,jsonBlock('该引用完整字段（定位、绑定与来源身份）',e));add(parent,box);
    });if(!values?.length)add(parent,paragraph('该发布记录没有独立 evidence 数组；请查看完整发布对象中的来源与澄清字段。','source-note'));
  }
  function calculations(parent, values) {(values||[]).forEach((v,i)=>{const box=el('article','evidence-item');add(box,el('h4','',`计算 ${i+1}：${v.expression||'实际记录'}`));
    if(Array.isArray(v.parameters))add(box,table(['参数','值','单位','依据'],v.parameters.map(p=>[p.name,p.value,p.unit,p.evidence_ids||p.binding||'见完整记录'])));
    add(box,paragraph('结果：'+text(v.value)+' '+(v.unit||'')),paragraph('单位处理：'+text(v.unit_handling),'source-note'),jsonBlock('计算规则、假设与逐项检查',v));add(parent,box);});}
  function contextView(c) {
    const state=c.context_state||{};
    return disclosure('本轮条件及变化',body=>{
      const names={topic:'主题',entities:'对象',metric:'指标',dimensions:'维度',filters:'筛选',time_range:'时间',unit:'单位',assumptions:'假设/约定',comparison_base:'比较基准',source_scope:'来源范围'};
      const rows=Object.entries(state.slots||{}).map(([k,v])=>[names[k]||k,v?.value??v,v?.origin==='model_interpretation'?'模型解释（不冒充用户确认）':v?.origin||'未记录',v?.confirmed_by_user?'有确认标记':'无确认标记']);
      if(rows.length)add(body,table(['条件','当前值','来源性质','确认边界'],rows));
      const changes=state.changes||[];
      if(changes.length)add(body,label('本轮变更记录'),table(['条件','操作','变更前','变更后'],changes.map(v=>[names[v.field]||v.field,({add:'加入',replace:'替换',remove:'移除',mark_unknown:'标为未知'})[v.operation]||v.operation,v.before?.value??v.before,v.after?.value??v.after])));
      else add(body,paragraph('该轮未保存单独 changes 项；不以缺失值推断无条件变化。','source-note'));
      add(body,jsonBlock('完整上下文原件',{context_state:state,context_patch:c.context_patch}));
    });
  }
  function identities(c) {return {answer_id:c.answer_id,parent_answer_id:c.parent_answer_id,conversation_id:c.conversation_id,turn_index:c.turn_index,selected_attempt_id:c.selected_attempt_id,record_sha256:c.record_sha256,code_sha256:c.code_sha256,config_sha256:c.config_sha256,sources:c.sources,source_revisions:c.source_revisions,context_state:c.context_state,context_patch:c.context_patch};}
  function renderAttempts(parent,c) {c.attempt_history.forEach((h,i)=>{const a=attempts.get(h.attempt_id)||h, selected=h.attempt_id===c.selected_attempt_id;
    const item=disclosure(`尝试 ${i+1} · ${selected?'本页选用的有效记录':'保留的其他历史记录'} · ${statusName(a.record_status||a.status)}（${a.record_status||a.status}） · 该次 ${a.requests ?? h.requests} 次请求`,body=>{
      add(body,paragraph('attempt_id：'+h.attempt_id,'source-meta'),paragraph(`耗时：${formatMs(a.elapsed_ms)}；开始：${a.started_at||'未记录'}；结束：${a.finished_at||'未记录'}`,'source-meta'),paragraph('复测原因：'+(a.retry_reason||'首次记录或原件未记载原因')));
      if(a.error)add(body,paragraph('记录错误：'+text(a.error),'caveat'));const p=a.published_result;
      if(p){add(body,label('该次保存的发布文本'),paragraph(selected?'与本页选用的有效记录一致。':'这段文本属于该次历史记录，不因此认定有效或通过。','source-note'),el('div','answer-original',p.text||'(该对象没有 text 字段)'));resultTables(body,p.result_tables||[]);queries(body,p);add(body,disclosure('该次引用（'+(p.evidence?.length||0)+'项）',b=>evidence(b,p.evidence)));}
      else add(body,paragraph('该次未保存发布对象，错误/中断/澄清边界以原始记录为准。'));
      add(body,jsonBlock('完整尝试原件，包括请求列表和上下文',a));
    });item.classList.add('attempt');if(selected)item.classList.add('selected');add(parent,item);
  });}
  function renderCase(c,parent,featured=false) {
    const p=c.published_result||{}, meta=el('div','case-meta');
    add(meta,badge(c.case_id),statusBadge(c.summary?.record_status||c.reference?.status||'有效记录'),badge(c.attempt_count+' 次尝试'),badge('有效记录 '+formatMs(c.elapsed_ms)),badge('有效记录 '+c.requests+' 次模型请求'));
    if(c.is_original20)add(meta,badge('原20题'));if(c.is_supplementary_single_table)add(meta,badge('补充单表','copper'));add(parent,meta);
    add(parent,el(featured?'h3':'h4','case-question',c.actual_question||c.question),paragraph('来源：'+c.sources.map(s=>s.name).join('；'),'source-note'));
    add(parent,paragraph('独立核验摘要','body-label'),paragraph(c.summary?.result_summary||'详见独立参考记录','review-summary'));
    if(c.summary?.caveat)add(parent,paragraph(c.summary.caveat,'caveat'));
    add(parent,label('实际发布答案原文'),el('div','answer-original',c.answer_text));resultTables(parent,c.result_tables||[]);queries(parent,p);
    if(p.calculations?.length)add(parent,disclosure('可执行计算与参数（'+p.calculations.length+'项）',b=>calculations(b,p.calculations),featured));
    add(parent,disclosure('完整引用依据（'+(p.evidence?.length||0)+'项）',b=>evidence(b,p.evidence),featured));
    add(parent,disclosure('独立参考与逐项核验',b=>{if(c.reference?.reference_sql)add(b,label('独立参考 SQL'),el('pre','sql',c.reference.reference_sql));if(c.reference?.rows)add(b,table(c.reference.reference_columns||c.result_tables?.[0]?.columns||[],c.reference.rows));const expected=[...(c.reference?.expected_points||[]),...(c.reference?.review_requirements?.facts||[])];if(expected.length){add(b,label('独立参考要点'));const list=el('ul');expected.forEach(v=>add(list,el('li','',v)));add(b,list);}const qualifiers=[...(c.reference?.conditions||[]),...(c.reference?.review_requirements?.qualifiers||[]),...(c.reference?.review_requirements?.context_conditions||[])];if(qualifiers.length)add(b,paragraph('条件边界：'+qualifiers.join('；'),'caveat'));add(b,jsonBlock('完整独立参考',c.reference),jsonBlock('核验摘要与审阅身份',c.summary));}));
    add(parent,disclosure('全部尝试历史（'+c.attempt_count+'次）',b=>renderAttempts(b,c)),jsonBlock('来源快照、父链身份与条件状态',identities(c)),jsonBlock('完整发布对象（保留所有字段）',p));
    add(parent,paragraph('原始记录 SHA-256：'+c.record_sha256,'source-meta'));
  }
  const titles={'sql-051':'先筛选“卖出过”，再统计全部曲目','RD-B08-01':'设计能力、实测流量与研究窗口分别回答','acceptance-cross-t1':'先查询原始分值，再应用文档口径'};
  const featureTabs=Array.from(document.querySelectorAll('[data-feature]'));
  function feature(id){const c=cases.get(id),box=$('feature-case');box.replaceChildren();const a=el('article','feature');add(a,el('h3','feature-header',titles[id]));renderCase(c,a,true);add(box,a);featureTabs.forEach(b=>{const active=b.dataset.feature===id;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;if(active)box.setAttribute('aria-labelledby',b.id);});}
  featureTabs.forEach((b,i)=>{b.id='feature-tab-'+i;b.setAttribute('aria-controls','feature-case');b.addEventListener('click',()=>feature(b.dataset.feature));b.addEventListener('keydown',e=>{let next;if(e.key==='ArrowRight')next=(i+1)%featureTabs.length;else if(e.key==='ArrowLeft')next=(i+featureTabs.length-1)%featureTabs.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=featureTabs.length-1;else return;e.preventDefault();feature(featureTabs[next].dataset.feature);featureTabs[next].focus();});});feature('sql-051');
  const caseNodes=new Map();D.cases.forEach(c=>{const d=el('details','case-row');d.id='case-'+c.case_id;const s=el('summary');add(s,el('span','case-id',c.case_id),el('span','case-question',c.actual_question||c.question),el('span','case-summary-meta',`${c.attempt_count} 次尝试 · 有效记录 ${c.requests} 次请求`));add(d,s);let rendered=false;
    d.addEventListener('toggle',()=>{if(d.open&&!rendered){const b=el('div','case-content');renderCase(c,b);add(d,b);rendered=true;}});add($('case-list'),d);caseNodes.set(c.case_id,d);});
  function selectedFirst(c){return c.attempt_history[0]?.attempt_id===c.selected_attempt_id;}
  function filter(){const q=$('case-search').value.trim().toLocaleLowerCase(),set=$('set-filter').value,state=$('state-filter').value;let n=0;
    D.cases.forEach(c=>{let include=!q||[c.case_id,c.actual_question,c.question,c.answer_text,...c.sources.map(s=>s.name)].join(' ').toLocaleLowerCase().includes(q);
      if(set!=='all')include=include&&(set==='original20'?c.is_original20:set==='supplement'?c.is_supplementary_single_table:set==='chain'?chainIds.has(c.case_id):set==='complex'?D.sets.complex_specialty.includes(c.case_id):(D.sets[set]||[]).includes(c.case_id));
      if(state!=='all')include=include&&(state==='first'?selectedFirst(c):state==='later'?!selectedFirst(c):(c.summary?.record_status||c.reference?.status)===state);
      caseNodes.get(c.case_id).hidden=!include;if(include)n++;});$('results-count').textContent=`显示 ${n} / 45 场景 · 每条保留完整原文和所有尝试`;$('empty-state').hidden=n>0;
  }
  ['case-search','set-filter','state-filter'].forEach(id=>$(id).addEventListener(id==='case-search'?'input':'change',filter));
  function reset(){ $('case-search').value='';$('set-filter').value='all';$('state-filter').value='all';filter();}
  $('clear-filters').addEventListener('click',()=>{reset();$('case-search').focus({preventScroll:true});});$('expand-visible').addEventListener('click',()=>caseNodes.forEach(d=>{if(!d.hidden)d.open=true;}));$('collapse-visible').addEventListener('click',()=>caseNodes.forEach(d=>d.open=false));filter();
  function openCase(id){if(!caseNodes.has(id))return;reset();const d=caseNodes.get(id);d.open=true;d.scrollIntoView({block:'start'});d.querySelector('summary').focus({preventScroll:true});}
  document.addEventListener('click',event=>{const b=event.target.closest('[data-open-case]');if(b){event.preventDefault();openCase(b.dataset.openCase);history.replaceState(null,'','#case-'+encodeURIComponent(b.dataset.openCase));}});
  const caseLink=(id,labelText)=>{const a=el('a','text-link',labelText||'打开完整表、SQL、引用及全部尝试');a.href='#case-'+encodeURIComponent(id);a.dataset.openCase=id;return a;};
  D.chains.forEach(chain=>{const article=el('article','chain');add(article,el('h3','chain-heading',chain.title),paragraph('6 / 6 轮独立通过，6 / 6 轮父子关系连续核验。','review-summary'),paragraph(chain.code_revision_note,'caveat'));
    const flow=el('div','chain-flow');chain.turns.forEach(t=>{const a=el('a','',String(t.turn_index)+' · '+t.case_id);a.href='#chain-'+t.case_id;a.addEventListener('click',()=>{const d=$('chain-'+t.case_id);if(d){d.open=true;d.querySelector('summary').focus({preventScroll:true});}});add(flow,a);});add(article,flow);
    chain.turns.forEach(t=>{const c=cases.get(t.case_id),d=el('details','chain-turn');d.id='chain-'+t.case_id;add(d,el('summary','',`第 ${t.turn_index} 轮 · ${t.actual_question||c.actual_question||t.question}`));let ready=false;d.addEventListener('toggle',()=>{if(d.open&&!ready){const body=el('div','detail-body');add(body,paragraph('实际发布答案原文','body-label'),el('div','answer-original',c.answer_text),label('轮次身份'),table(['字段','原记录值'],[['case_id',t.case_id],['answer_id',t.answer_id],['parent_answer_id',t.parent_answer_id||'起点（无父轮）'],['conversation_id',t.conversation_id],['turn_index',t.actual_turn_index ?? t.turn_index],['有效尝试',t.verified_attempt_id]]),contextView(c),jsonBlock('父链验证与完整轮次记录',t),caseLink(t.case_id));add(d,body);ready=true;}});add(article,d);});add(article,jsonBlock('整链核验与修订身份',chain));add($('chain-list'),article);});
  D.corpus.files.forEach(f=>{const r=el('tr');const a=el('td');add(a,el('strong','',f.id),paragraph(f.title),paragraph(f.file,'source-meta'));const b=el('td');add(b,paragraph(f.document_date),paragraph(f.business_domain,'source-note'));const c=el('td','',f.pages);const d=el('td');add(d,paragraph(f.status_at_source,'source-note'),paragraph(f.package_path,'source-meta'));if(/^https:\/\//.test(f.url)){const link=el('a','', '官方来源（联网）');link.href=f.url;link.target='_blank';link.rel='noopener noreferrer';add(d,link);}add(d,jsonBlock('SHA-256、来源组及随包许可边界',f));add(r,a,b,c,d);add($('corpus-table').querySelector('tbody'),r);});
  const captions=[
    '按任务选择SQL、文档或跨源路径。证据对象与来源版本经过发布检查，用户可回到页码、字段或计算参数核对。',
    'Schema linking使用词项、别名和有界值探测，大表集合筛选连接桥。SQL经AST、严格双引号语义、只读连接和权限检查执行，再做独立语义复核。',
    '文档形成带来源定位的片段；BM25与E5精确向量分批扫描经RRF融合和规则重排。不是ANN索引或神经重排器，按已有保护做有界补检。',
    '数据库数值、文档规则、用户假设分别绑定。Decimal计算前先归一参数单位；六轮末次10000分按100.00元参数进入表达式a，不能再除100。',
    '来源事实、用户假设与模型解释分开管理。澄清、修改、撤销沿真实父链保留状态；撤销不重写来源事实。',
    '45个有效场景包含三条六轮链，86次尝试与545次请求单独统计。29个场景选用首次记录不等于模型首答准确率。'
  ];
  const gallery=[];let galleryIndex=0,lightboxOpener=null;
  function setImageScale(actual){$('lightbox-image').classList.toggle('actual-size',actual);$('image-size').setAttribute('aria-pressed',String(actual));$('image-size').textContent=actual?'适应窗口':'查看原始尺寸';$('lightbox-viewport').scrollTo(0,0);}
  function showImage(index){galleryIndex=(index+gallery.length)%gallery.length;const entry=gallery[galleryIndex],zoom=$('lightbox-image');zoom.src=entry.item.path;zoom.alt=entry.alt;$('lightbox-title').textContent=entry.item.title||entry.item.name;$('lightbox-caption').textContent=entry.caption||'';$('image-position').textContent=(galleryIndex+1)+' / '+gallery.length;setImageScale(false);}
  function figure(item,caption){const f=el('figure','figure');add(f,el('h3','figure-title',item.title||item.name));const b=el('button','figure-button');b.type='button';b.setAttribute('aria-label','放大：'+(item.title||item.name));const img=el('img');img.src=item.path;img.alt=item.alt||item.title||item.name;img.loading='lazy';add(b,img);const index=gallery.length;gallery.push({item,caption,alt:img.alt,opener:b});b.addEventListener('click',()=>{lightboxOpener=b;showImage(index);$('lightbox').showModal();$('close-lightbox').focus();});add(f,b,el('figcaption','',caption));if(item.answer_id&&M.demo?.rounds?.some(r=>r.answer_id===item.answer_id)){const link=el('a','text-link','查看本轮记录');link.href='#demo-'+item.answer_id;link.addEventListener('click',()=>{const d=$('demo-'+item.answer_id);if(d){d.open=true;d.querySelector('summary').focus({preventScroll:true});}});add(f,link);}return f;}
  M.figures.forEach((f,i)=>add($('method-figures'),figure(f,captions[i])));
  if(M.screenshots?.length){const grid=el('div','screenshot-grid');M.screenshots.forEach(f=>add(grid,figure(f,f.caption)));add($('screenshots'),grid);}else{ $('workbench').hidden=true;document.querySelector('.rail a[href="#workbench"]').hidden=true; }
  function demoView(parent,compact=false){const demo=M.demo;if(!demo){add(parent,paragraph('本次展示数据未加载，请查看随附的本次演示明细。','source-note'));return;}
    const summary=demo.summary||{},rounds=demo.rounds||[];add(parent,el('h3','','本次真实展示 · 单独记账'));
    if(summary.narrative)add(parent,paragraph(summary.narrative,'review-summary'));
    const metrics=[['用户问答轮数',summary.user_rounds],['模型请求次数',summary.model_requests],['有usage的请求',summary.requests_with_usage],['缺usage的请求',summary.requests_without_usage],['累计total_tokens',summary.total_tokens]].filter(v=>v[1]!=null);
    if(metrics.length)add(parent,table(['本次口径','实际记录'],metrics));if(summary.high_requests!=null)add(parent,paragraph('模式分布：high '+summary.high_requests+' 次，ordinary '+summary.ordinary_requests+' 次。请求终态：完成 '+summary.request_statuses.completed+' 次、输出截断 '+summary.request_statuses.output_truncated+' 次、超时 '+summary.request_statuses.timeout+' 次。','source-note'));
    add(parent,paragraph(summary.boundary||'本次展示与45个历史场景分别记录；展示覆盖不能代替最终版本45题统一复测、盲测或同条件重复稳定性实验。','caveat'));
    if(summary.anomalies?.length){const list=el('ul','demo-anomalies');summary.anomalies.forEach(v=>add(list,el('li','',v)));add(parent,label('本次异常及处理边界'),list);}
    if(compact){const a=el('a','text-link','回到本次展示逐轮记录');a.href='#demo-summary';add(parent,a);return;}
    if(rounds.length){add(parent,table(['轮次','真实问题','状态','耗时','模型请求'],rounds.map((r,i)=>[i+1,r.question,statusName(r.status),formatMs(r.elapsed_ms),r.requests])));
      rounds.forEach((r,i)=>{const card=disclosure(`本次第 ${i+1} 轮 · ${r.question}`,body=>{const p=r.published_result||{};const meta=el('div','case-meta');add(meta,badge(r.case_id||'展示轮次'),statusBadge(r.status),badge(formatMs(r.elapsed_ms)),badge(r.requests+' 次模型请求'));add(body,meta,paragraph('answer_id：'+r.answer_id+'；会话内轮次：'+r.turn_index+'；父轮：'+(r.parent_answer_id||'起点（无父轮）'),'source-meta'));
        if(r.independent_review){add(body,label('独立核验'));const summaryText=M.demo.summary.round_review_notes?.[r.case_id]||r.independent_review.overall_result||r.independent_review.result_boundary;if(summaryText)add(body,paragraph(summaryText,'review-summary'));add(body,typeof r.independent_review==='string'?paragraph(r.independent_review,'review-summary'):jsonBlock('逐项核验与结论（含表达缺口）',r.independent_review));}if(r.error)add(body,paragraph('该轮记录错误：'+r.error,'caveat'));
        add(body,label('本次实际发布答案原文'),el('div','answer-original',r.answer_text??p.text??'该轮未保存发布文本。'));resultTables(body,r.result_tables||p.result_tables||[]);queries(body,p);if(p.calculations?.length)add(body,disclosure('实际计算与单位',b=>calculations(b,p.calculations)));add(body,disclosure('本次完整引用（'+(p.evidence?.length||0)+'项）',b=>evidence(b,p.evidence)));
        if(r.anomalies?.length){add(body,label('本轮异常与处理'));r.anomalies.forEach(v=>add(body,paragraph(v,'caveat')));}
        if(r.request_summary?.length)add(body,disclosure('本轮模型请求明细（含失败/截断）',b=>{add(b,table(['请求序号','角色/阶段','模式','结果','耗时'],r.request_summary.map((v,j)=>[v.sequence??v.index??j+1,v.task_stage||v.role||v.stage||v.purpose||'未记录',v.effort||v.reasoning_mode||v.mode||'未记录',statusName(v.status||v.error_code||'未记录'),formatMs(v.request_elapsed_ms??v.elapsed_ms)])),jsonBlock('请求完整脱敏字段',r.request_summary));}));
        add(body,jsonBlock('本轮完整脱敏记录',r));
      });card.id='demo-'+r.answer_id;card.classList.add('demo-turn');add(parent,card);});
    }
    const a=el('a','text-link','下载本次演示明细JSON');a.href='评审资源/本次演示明细.json';a.download='';add(parent,a,jsonBlock('本次范围、用量与身份汇总',summary));
  }
  demoView($('demo-summary'));demoView($('strategy-demo'),true);
  const perf=$('performance-content'),sql=D.performance.sql,sqlRows=[];
  sql.scales.forEach(s=>s.queries.forEach(q=>sqlRows.push([s.rows.toLocaleString('zh-CN'),q.name==='full_aggregate'?'全表 COUNT＋SUM':'复合索引筛选',q.first_invocation_ms.toFixed(3),q.warm_median_ms.toFixed(3),q.warm_min_ms.toFixed(3)+'–'+q.warm_max_ms.toFixed(3),q.samples[0].query_plan.join('; ')])));
  add(perf,el('h3','','SQLite产品执行器：毫秒级本地步骤'),table(['输入行数','查询','首次(ms)','后5次中位数(ms)','后5次范围(ms)','实际计划'],sqlRows),paragraph(sql.timing_boundary,'source-note'),paragraph(sql.cache_boundary,'caveat'),jsonBlock('SQL全部样本、独立计算与环境',sql));
  const rr=[];for(const [key,scope] of Object.entries(D.performance.retrieval.scopes)){const summary=scope;rr.push([key==='all_documents'?'全库12份':'同系列范围',scope.measured_queries??scope.samples.length,summary.median_ms??summary.elapsed_ms_median??'见完整样本',summary.min_ms??'见样本',summary.max_ms??'见样本']);}
  add(perf,el('h3','','本地检索：范围、覆盖与耗时一起看'),table(['范围','问题数','中位数(ms)','最短(ms)','最长(ms)'],rr),paragraph('24个开发问题、每个范围各一次本地检索，不含模型生成和答案正确性。全部命中与找齐所有标注页是不同口径。','source-note'),jsonBlock('检索完整耗时、范围与环境',D.performance.retrieval),jsonBlock('Top5 / Top10逐范围证据页召回',D.retrieval));
  const er=[];Object.entries(D.performance.end_to_end.groups).forEach(([k,v])=>er.push([k==='single_table_sql'?'单表SQL':'单文档RAG',v.n,(v.median_ms/1000).toFixed(3),(v.min_ms/1000).toFixed(3)+'–'+(v.max_ms/1000).toFixed(3),(D.performance.stage_breakdown.groups[k].request_elapsed_ms_sum_share*100).toFixed(2)+'%']));
  add(perf,el('h3','','问答端到端：模型请求段占主要时间'),table(['组别','有效场景数','中位数(s)','范围(s)','请求段累计占比'],er),paragraph('API提交至完整答案导出，不是首字或浏览器绘制时间。每题取一个有效历史尝试，不是重复稳定性样本。请求段含网络和网关，其余时间不能单独解释为产品计算。','caveat'),jsonBlock('端到端逐题和阶段拆分',{end_to_end:D.performance.end_to_end,stage_breakdown:D.performance.stage_breakdown}));
  add(perf,el('h3','','费用保留缺失值'),paragraph('545次模型请求中535次报告usage，累计8,844,583 total_tokens。目录价未扣缓存估算约10.572514元；10次无usage不能按零计，实扣账单未知。','cost-note'),jsonBlock('完整用量与目录价估算账本',D.usage),jsonBlock('复杂度理论分析（不替代测量）',sql.theory));
  D.limitations.slice(0,8).forEach(v=>add($('limitations'),el('li','',v)));add($('limitations'),el('li','','Windows已完成代码适配、依赖可解析性与Mac运行检查，尚无Windows实机和OCR质量验收。包内不携带私人runtime、密钥或可复用的个人服务配置。'));
  $('close-lightbox').addEventListener('click',()=>$('lightbox').close());$('lightbox').addEventListener('click',e=>{if(e.target===$('lightbox'))$('lightbox').close();});$('lightbox').addEventListener('close',()=>{setImageScale(false);lightboxOpener?.focus({preventScroll:true});});
  $('previous-image').addEventListener('click',()=>showImage(galleryIndex-1));$('next-image').addEventListener('click',()=>showImage(galleryIndex+1));$('image-size').addEventListener('click',()=>setImageScale(!$('lightbox-image').classList.contains('actual-size')));
  $('lightbox').addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();showImage(galleryIndex+(e.key==='ArrowLeft'?-1:1));}else if(e.key==='Home'||e.key==='End'){e.preventDefault();showImage(e.key==='Home'?0:gallery.length-1);}else if(['+','=','-','0'].includes(e.key)){e.preventDefault();setImageScale(e.key==='+'||e.key==='=');}});
  $('print').addEventListener('click',()=>window.print());
  if('IntersectionObserver' in window){const observer=new IntersectionObserver(entries=>entries.forEach(e=>{if(e.isIntersecting){document.querySelectorAll('.rail nav a').forEach(a=>a.classList.toggle('active',a.hash==='#'+e.target.id));}}),{rootMargin:'-15% 0px -65% 0px'});document.querySelectorAll('main section').forEach(s=>observer.observe(s));}
  function openHash(){try{if(location.hash.startsWith('#case-'))openCase(decodeURIComponent(location.hash.slice(6)));else if(location.hash.startsWith('#chain-')||location.hash.startsWith('#demo-')){const d=$(decodeURIComponent(location.hash.slice(1)));if(d?.classList.contains('chain-turn')||d?.classList.contains('demo-turn')){d.open=true;d.scrollIntoView({block:'start'});d.querySelector('summary').focus({preventScroll:true});}}}catch(_){/* Invalid fragment does not affect reading. */}}
  window.addEventListener('hashchange',openHash);openHash();
})();
