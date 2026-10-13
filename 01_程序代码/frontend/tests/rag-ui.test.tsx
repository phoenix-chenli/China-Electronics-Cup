import {SourceProvenance} from '../src/evidence'
import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {documentQuestionScope,IndexStatus,ProviderStatus,RagOutcome,RagScopeNote,SemanticReview,RetrievalStatus,latestRetrieval} from '../src/rag'
import {answerNeedsContext,resolvePetState} from '../src/pet-state'
import {AnalysisPage,Claims} from '../src/workspace'
import {savedAnalysisName} from '../src/result-presentation'

const noop=()=>{}
const evidence=[{id:'doc-1',type:'document',source:'2024售后条款.pdf',snippet:'甲产品限额为100元。',locator:{document_id:'d1',sha256:'a'.repeat(64),parse_id:'p1',page:2}},{id:'doc-2',type:'document',source:'补充条件.md',snippet:'正常使用适用。',locator:{document_id:'d2',sha256:'b'.repeat(64),parse_id:'p2',page:1}}]
const base={run_id:'rag-fixture',question:'2025年甲产品限额及例外条件？',status:'completed',answer_status:'refused',answer:'现有资料没有2025年适用条款，无法给出赔付结论。',query_spec:{metric:'document',query_scope:'knowledge'},claims:[],evidence,coverage:{supported:0,total:0},rows:[],columns:[],events:[],actions:[],rag:{retrieval:{mode:'lexical'}},missing_requirements:['2025年有效条款','例外条件'],conflicts:['两份旧资料的适用期不同'],agent:{mode:'model',calls:3}}

test('completed retrieval refusal shows reason, missing facts, conflicts and inspectable originals',()=>{
 const html=renderToStaticMarkup(<RagOutcome result={base} onEvidence={noop}/>)
 for(const text of ['证据不足，未生成确定结论','现有资料没有2025年适用条款','2025年有效条款','例外条件','两份旧资料的适用期不同','可查看的相关原文','2024售后条款.pdf','仅关键词'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('已生成有依据的回答'))
})

test('partial output preserves supported content while exposing the unanswered requirement',()=>{
 const result={...base,answer_status:'partial',claims:[{id:'c1',text:'甲产品2024年限额为100元。',evidence_ids:['doc-1']}],missing_requirements:['2025年的例外条件'],conflicts:[]}
 const html=renderToStaticMarkup(<><RagOutcome result={result} onEvidence={noop}/><Claims result={result} onEvidence={noop}/></>)
 for(const text of ['仅回答有依据的部分','2025年的例外条件','甲产品2024年限额为100元。','查看来源 1'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('全部有依据'))
})

test('local excerpt retrieval never presents itself as generated synthesis',()=>{
 const html=renderToStaticMarkup(<RagOutcome result={{...base,answer_status:'supported',agent:{mode:'local'},claims:[{text:'甲产品限额为100元。'}]}}/>)
 assert.ok(html.includes('原文检索，未生成综合答案'))
 assert.ok(!html.includes('已生成有依据的回答'))
})

test('each synthesis claim retains multiple citations and separates model review from original location',()=>{
 const claim={id:'c1',kind:'synthesis',text:'限额100元，仅正常使用适用。',evidence_ids:['doc-1','doc-2'],semantic_check:{status:'supported',detail:'额度与条件分别由两处原文支持。',model:'review-fixture'}}
 const html=renderToStaticMarkup(<Claims result={{claims:[claim],evidence}} onEvidence={noop}/>)
 for(const text of ['查看来源 1','查看来源 2','模型支持检查：有支持','原文定位另行核对','review-fixture'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('真实性'))
 const unknown=renderToStaticMarkup(<SemanticReview claim={{semantic_check:{status:'unknown',detail:'模型请求超时'}}}/>)
 assert.ok(unknown.includes('未能判断'));assert.ok(unknown.includes('模型请求超时'));assert.ok(!unknown.includes('模型支持检查：有支持'))
})

test('a supported explanation comes before expandable facts without hiding evidence gaps',()=>{
 const claims=[
  {id:'raw',kind:'database_row',status:'supported',text:'查询原始数值 91.8',evidence_ids:['doc-1']},
  {id:'gap',kind:'excerpt',status:'needs_evidence',text:'差异原因尚缺证据',evidence_ids:['doc-2']},
  {id:'missing-source',kind:'excerpt',status:'supported',text:'该条还有一处未找到的引用',evidence_ids:['doc-1','missing-id']},
  {id:'explanation',kind:'synthesis',status:'supported',text:'两项合计与第二产业不一致，原因待核实。',evidence_ids:['doc-1','doc-2']}
 ]
 const html=renderToStaticMarkup(<Claims result={{claims,evidence}} onEvidence={noop}/>)
 const toggle=html.indexOf('<details class="claim-supporting"')
 assert.ok(toggle>html.indexOf('两项合计与第二产业不一致'))
 assert.ok(toggle>html.indexOf('差异原因尚缺证据'))
 assert.ok(toggle>html.indexOf('该条还有一处未找到的引用'))
 assert.ok(html.indexOf('查询原始数值 91.8')>toggle)
 assert.ok(html.includes('查看来源 1'));assert.ok(html.includes('查看来源 2'));assert.ok(!html.includes(' open=""'))
 const factsOnly=renderToStaticMarkup(<Claims result={{claims:claims.slice(0,1),evidence}} onEvidence={noop}/>)
 assert.ok(!factsOnly.includes('claim-supporting'));assert.ok(factsOnly.includes('查询原始数值 91.8'))
})

test('index states reflect actual progress and degradation without claiming mixed readiness',()=>{
 const lexical=renderToStaticMarkup(<IndexStatus item={{status:'lexical_ready',processed:7,total:10,sha256:'a'.repeat(64)}}/>)
 assert.ok(lexical.includes('关键词就绪'));assert.ok(lexical.includes('7 / 10'));assert.ok(!lexical.includes('混合检索就绪'))
 const failed=renderToStaticMarkup(<IndexStatus item={{status:'failed',error:'向量维度不匹配'}}/>)
 assert.ok(failed.includes('建立失败'));assert.ok(failed.includes('向量维度不匹配'));assert.ok(!failed.includes('就绪'))
 const missing=renderToStaticMarkup(<IndexStatus/>)
 assert.ok(missing.includes('未建索引'));assert.ok(!missing.includes('混合检索就绪'))
})

test('provider status never infers readiness from the presence of a model name',()=>{
 const html=renderToStaticMarkup(<ProviderStatus name="语义向量" item={{ready:false,local:true,model:'bge-small-zh-v1.5',reason:'模型包尚未安装'}}/>)
 assert.ok(html.includes('未就绪'));assert.ok(html.includes('模型包尚未安装'));assert.ok(html.includes('本机处理'));assert.ok(!html.includes('实际就绪'))
})

test('scope preview displays frozen versions and knowledge saved names avoid sales years',()=>{
 const html=renderToStaticMarkup(<RagScopeNote result={{rag:{scope:{scope_type:'document',documents:[{document_id:'d1',name:'售后条款.pdf',sha256:'abcdef0123456789',parse_id:'parse-old'}]}}}}/>)
 assert.ok(html.includes('整份文档'));assert.ok(html.includes('abcdef012345'));assert.ok(html.includes('parse-old'))
 assert.equal(savedAnalysisName(base),base.question)
})

test('page and selection submission preserve version and offsets instead of silently expanding retrieval',()=>{
 const document={document_id:'d1',sha256:'a'.repeat(64),parse_id:'old-parse',is_current:false}
 const selection={start:3,end:9,text:'限定😀原文'}
 assert.deepEqual(documentQuestionScope('focused',document,2,selection),{document_id:'d1',sha256:'a'.repeat(64),parse_id:'old-parse',page:2,text_range:{start:3,end:9},quote:'限定😀原文'})
 const page=documentQuestionScope('page',document,2,selection)
 assert.equal(page.page,2);assert.equal(page.parse_id,'old-parse');assert.ok(!('text_range' in page));assert.ok(!('rag_scope' in page))
 assert.throws(()=>documentQuestionScope('document',document,2),/历史版本/)
})

test('whole-document and corpus retrieval require deliberate scope and never carry a hidden page restriction',()=>{
 const document={document_id:'d1',sha256:'a'.repeat(64),parse_id:'new-parse',is_current:true}
 assert.deepEqual(documentQuestionScope('document',document,2),{rag_scope:{scope_type:'document',document_ids:['d1']}})
 assert.deepEqual(documentQuestionScope('corpus',document,2),{rag_scope:{scope_type:'corpus'}})
 assert.throws(()=>documentQuestionScope('anything',document,2),/范围无效/)
})

test('analysis view shows completed refusal without green completion, empty coverage or sales shortcuts',()=>{
 const storage=globalThis.localStorage
 Object.defineProperty(globalThis,'localStorage',{value:{getItem:()=>JSON.stringify(['scenario'])},configurable:true})
 try{
  const html=renderToStaticMarkup(<AnalysisPage result={base} question="" setQuestion={noop} run={noop} busy={false} recompute={noop} onSave={noop} onCopy={noop} onEvidence={noop} navigate={noop} choose={noop} session={{id:'rag',messages:[],tasks:[]}} tools={[{id:'scenario',label:'情景推演',onSelect:noop}]}/>)
  assert.ok(html.includes('证据不足，未生成确定结论'));assert.ok(html.includes('现有资料没有2025年适用条款'))
  for(const text of ['分析已完成','condition-summary','情景推演','followup-options'])assert.ok(!html.includes(text),text)
 }finally{if(storage)Object.defineProperty(globalThis,'localStorage',{value:storage,configurable:true});else delete (globalThis as any).localStorage}
})

test('retrieval history displays the latest real channels and records failed reranking',()=>{
 const result={rag:{retrieval:[{channels:{bm25:[{id:'old'}],dense:[{id:'old'}]},reranked:true},{channels:{bm25:[{id:'new'}]},reranked:false,errors:[{stage:'dense',error:'嵌入连接已停用'},{stage:'rerank',error:'重排请求超时'}]}]}}
 assert.equal(latestRetrieval(result)?.channels.bm25[0].id,'new')
 const html=renderToStaticMarkup(<RetrievalStatus result={result}/>)
 for(const text of ['仅关键词','未完成，沿用召回排序','嵌入连接已停用','重排请求超时'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('关键词与向量'));assert.ok(!html.includes('候选重排：实际完成'))
})

test('selected evidence and empty retrieval history do not invent dense or reranking success',()=>{
 const html=renderToStaticMarkup(<RetrievalStatus result={{rag:{retrieval:[{channels:{selection:[{id:'selected'}]},reranked:false}]}}}/>)
 assert.ok(html.includes('固定选中原文'));assert.ok(!html.includes('候选重排'));assert.ok(!html.includes('向量'))
 assert.equal(renderToStaticMarkup(<RetrievalStatus result={{rag:{retrieval:[]}}}/>),'')
})

test('an empty claim list cannot display generated answer success even when status is inconsistent',()=>{
 const html=renderToStaticMarkup(<RagOutcome result={{...base,answer_status:'supported'}}/>)
 assert.ok(html.includes('证据不足，未生成确定结论'));assert.ok(!html.includes('已生成有依据的回答'))
})

test('companion refuses celebration for insufficient or partially answered completed results',()=>{
 for(const answer_status of ['refused','partial','needs_clarification']){
  const gap=answerNeedsContext({...base,answer_status,claims:[{status:'supported'}]})
  assert.equal(gap,true)
  assert.equal(resolvePetState({clarify:gap,manual:'success'}),'clarify')
 }
 assert.equal(answerNeedsContext({...base,answer_status:'supported',claims:[{status:'supported'}]}),false)
 assert.equal(answerNeedsContext({...base,answer_status:'supported',claims:[]}),true)
})


test('official native Word and unknown provenance cannot be mislabeled as a scanned PDF',()=>{
 const source={institution:'官方机构',origin:'original_docx_from_official_site',document_year:2024,publication_status:'public_consultation_draft',published_at:'2024-08-28'}
 const html=renderToStaticMarkup(<SourceProvenance provenance={source} format="docx" officialUrl="https://example.gov/source.docx"/>)
 for(const text of ['官方 Word 原件','征求意见稿','不代表正式生效或实际完成','2024-08-28','https://example.gov/source.docx'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('扫描 PDF'));assert.ok(!html.includes('PDF 原文转换'))
 assert.equal(renderToStaticMarkup(<SourceProvenance/>),'')
 const unknown=renderToStaticMarkup(<SourceProvenance provenance={{origin:'unrecognized'}} format="docx"/>)
 assert.ok(unknown.includes('来源登记'));assert.ok(!unknown.includes('官方扫描'))
})

test('partial structures identify the document without describing a readable page as empty',()=>{
 const html=renderToStaticMarkup(<RetrievalStatus result={{rag:{retrieval:[{channels:{bm25:[],dense:[]}}],source_gaps:[{document_name:'公式说明.docx',page:1,reason:'页面局部结构尚待核验',instruction:'请对照原件人工复核'}]}}}/>)
 for(const text of ['公式说明.docx','页面局部结构尚待核验','人工复核'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('页缺少可用文字'))
})
