import {OcrCandidates} from '../src/document-review'
import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {DocumentStructures,OcrProviderStatus,PageProvenance,ProcessingPreview,ProcessingTaskStatus,StructuredChart,StructuredTable,documentPosition,ocrReady,processingDocumentId,processingPages,sourceHighlightRegions,submitProcessingTask,validSourceBox} from '../src/document-processing'

const request={document_id:'public-doc',version:'a'.repeat(64),parse_id:'old-parse',pages:[1,3],mode:'vision' as const,rotation:90 as const,connection_id:'vision-tested'}
test('formula preview discloses bounded extra calls before image consent',()=>{
 const html=renderToStaticMarkup(<ProcessingPreview preview={{mode:'vision',document:{name:'公式图'},pages:[1],image_count:1,total_bytes:1024,formula_reread:{max_extra_images_per_image:2,max_extra_bytes_per_image:30000000},provider:{name:'视觉模型'}}}/>)
 for(const text of ['局部放大识别','不发送其他来源','实际调用、失败和耗时','追加图片合计最多'])assert.ok(html.includes(text),text)
})
test('source-view metadata retains its document identity in the processing request',async()=>{
 const view={document:{document_id:'fixed-source',sha256:'b'.repeat(64)},parse_id:'fixed-parse'}
 const original=globalThis.fetch;let body:any
 globalThis.fetch=(async(_path:any,options:any)=>{body=JSON.parse(options.body);return new Response(JSON.stringify({id:'task',status:'queued'}),{status:200})}) as typeof fetch
 try{await submitProcessingTask({document_id:processingDocumentId(view.document),version:view.document.sha256,parse_id:view.parse_id,pages:[1],mode:'ocr',rotation:0});assert.equal(body.document_id,'fixed-source');assert.equal(body.parse_id,'fixed-parse');assert.equal(body.version,view.document.sha256)}finally{globalThis.fetch=original}
 assert.equal(processingDocumentId({id:'catalog-id'}),'catalog-id')
 assert.throws(()=>processingDocumentId({document_id:'one',id:'another'}),/标识未确认/)
})
test('vision requires a concrete preview and submits the frozen document and page selection',async()=>{
 const original=globalThis.fetch;const sent:{path:string,body:any}[]=[]
 globalThis.fetch=(async(path:any,options:any)=>{sent.push({path,body:JSON.parse(options.body)});return new Response(JSON.stringify({id:'task-1',status:'queued',processed:0,total:2}),{status:200,headers:{'Content-Type':'application/json'}})}) as typeof fetch
 try{
  await assert.rejects(()=>submitProcessingTask(request),/预览/);assert.equal(sent.length,0)
  const task=await submitProcessingTask(request,{preview_id:'one-use-preview'})
  assert.equal(task.status,'queued');assert.equal(sent[0].path,'/api/document-processing/tasks')
  assert.deepEqual(sent[0].body,{...request,preview_id:'one-use-preview',accepted:true})
 }finally{globalThis.fetch=original}
})
test('expired or changed preview rejection remains a failure instead of creating a completed state',async()=>{
 const original=globalThis.fetch
 globalThis.fetch=(async()=>new Response(JSON.stringify({detail:'发送预览已过期，请重新生成'}),{status:410})) as typeof fetch
 try{await assert.rejects(()=>submitProcessingTask(request,{preview_id:'expired'}),/已过期/)}finally{globalThis.fetch=original}
 const html=renderToStaticMarkup(<ProcessingTaskStatus task={{id:'t',status:'failed',stage:'发送前检查',error:'连接版本已变化，未发布解析',processed:0,total:2}} onRetry={()=>{}}/>)
 assert.ok(html.includes('处理失败'));assert.ok(html.includes('连接版本已变化'));assert.ok(html.includes('重试任务'));assert.ok(!html.includes('新解析已保存'));assert.ok(!html.includes('查看新解析'))
})
test('actual progress and cancellation do not claim successful publication',()=>{
 const running=renderToStaticMarkup(<ProcessingTaskStatus task={{id:'t',mode:'ocr',status:'running',stage:'读取第 2 页',processed:1,total:4}} onCancel={()=>{}}/>)
 assert.ok(running.includes('已处理 1 / 4'));assert.ok(running.includes('value="1"'));assert.ok(running.includes('max="4"'));assert.ok(running.includes('取消任务'))
 const cancelled=renderToStaticMarkup(<ProcessingTaskStatus task={{id:'t',status:'cancelled',stage:'用户取消',processed:1,total:4}} onRetry={()=>{}}/>)
 assert.ok(cancelled.includes('已取消'));assert.ok(cancelled.includes('不会发布'));assert.ok(!cancelled.includes('新解析已保存'));assert.ok(!cancelled.includes('取消任务'))
 const interrupted=renderToStaticMarkup(<ProcessingTaskStatus task={{id:'t',mode:'vision',status:'interrupted',stage:'程序重启'}} onRetry={()=>{}}/>)
 assert.ok(interrupted.includes('重新预览并重试'));assert.ok(!interrupted.includes('新解析已保存'))
})
test('completed task offers an explicit new parse while retaining historical citations',()=>{
 const html=renderToStaticMarkup(<ProcessingTaskStatus task={{id:'t',status:'completed',processed:2,total:2,result:{parse_id:'new-parse',before_parse_id:'old-parse'}}} onView={()=>{}}/>)
 assert.ok(html.includes('历史问答继续使用原解析'));assert.ok(html.includes('主动查看新解析'));assert.ok(html.includes('查看新解析'));assert.ok(!html.includes('重试任务'))
})
test('range limits reject invalid pages and never silently truncate a selection',()=>{
 assert.deepEqual(processingPages('1，3-5,3',5),[1,3,4,5])
 for(const range of ['0','6','3-2','1.5','1-999999999'])assert.throws(()=>processingPages(range,5))
 assert.throws(()=>processingPages('1-41',100),/最多/)
 assert.equal(documentPosition('docx',3),'第 3 逻辑节');assert.equal(documentPosition('pdf',3),'第 3 页')
})
test('preview renders the actual send volume and service without exposing private connection fields',()=>{
 const html=renderToStaticMarkup(<ProcessingPreview preview={{preview_id:'p',mode:'vision',document:{name:'公开图表.pdf',sha256:'a'.repeat(64)},pages:[1,3],rotation:90,image_count:2,total_bytes:2048,provider:{name:'视觉服务甲',scope:'外部服务',api_key:'NEVER_DISPLAY'},images:[{page:1,bytes:1024,preview_url:'/public-preview.png'}]}} format="pdf"/>)
 for(const text of ['公开图表.pdf','1、3','2 张','2.0 KB','视觉服务甲','确认此次发送','90°','public-preview.png'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('NEVER_DISPLAY'))
})
test('merged cells preserve original blank and zero without propagating merged text',()=>{
 const html=renderToStaticMarkup(<StructuredTable table={{title:'合并表头',rows:3,columns:2,cells:[{row:0,column:0,rowspan:1,colspan:2,text:'季度',role:'header'},{row:1,column:0,rowspan:2,colspan:1,text:'华东'},{row:1,column:1,text:'0'},{row:2,column:1,text:''}]}}/>)
 assert.ok(html.includes('colSpan="2"')||html.includes('colspan="2"'));assert.ok(html.includes('rowSpan="2"')||html.includes('rowspan="2"'))
 assert.equal((html.match(/华东/g)||[]).length,1);assert.ok(html.includes('<span>0</span>'));assert.ok(html.includes('<span></span>'));assert.ok(!html.includes('人工核验'))
})
test('charts retain axes and legend but mark estimates and unknown values for review',()=>{
 const html=renderToStaticMarkup(<StructuredChart chart={{title:'双轴图',kind:'line',axes:[{axis:'y',label:'收入',unit:'万元',scale:'log',ticks:[1,10,100]}],legend:[{label:'甲系列',color:'#f00',series:'s1'}],series:[{name:'甲系列',axis:'y',points:[{label:'2024',value:'12.3',value_source:'estimated',status:'pending'},{label:'2025',value:null,value_source:'unknown',status:'pending'}]}]}}/>)
 for(const text of ['收入','万元','对数刻度','图例','甲系列','像素估读','待复核','约 12.3','无法读取','数值来源待确认'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('精确已核验'));assert.ok(!html.includes('人工正确率'));assert.ok(!html.includes('检查通过'))
})
test('page provenance uses actual method and line geometry without fabricated character positions',()=>{
 const html=renderToStaticMarkup(<><PageProvenance page={{method:'ocr',location_precision:'line',ocr_required:false,provenance:{method:'rapidocr',model:'fixed-public-model'}}}/><DocumentStructures page={{tables:[],charts:[],issues:[{code:'unit_missing',message:'单位待确认'}]}}/></>)
 for(const text of ['本机文字识别','行级定位','不拆分成字符位置','fixed-public-model','单位待确认'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('尚未做视觉'));assert.ok(!html.includes('此页尚需文字识别'));assert.ok(!html.includes('data-start'))
 assert.equal(validSourceBox([.1,.1,.5,.5]),true)
 for(const box of [null,[0,0,0,1],[-1,0,1,1],[0,0,2,1],[0,0,NaN,1]])assert.equal(validSourceBox(box),false)
})
test('model files alone do not establish a working OCR provider',()=>{
 assert.equal(ocrReady({items:[{installed:true,verified:true}],local_ocr:{ready:false,reason:'推理依赖缺失'}}),false)
 assert.equal(ocrReady({local_ocr:{ready:true}}),true)
 const html=renderToStaticMarkup(<OcrProviderStatus models={{items:[{installed:true,verified:true}],local_ocr:{ready:false}}}/>)
 assert.ok(html.includes('尚未就绪'));assert.ok(!html.includes('本机识别已就绪'))
})
test('DOCX previews list each embedded image and task progress counts actual images',()=>{
 const html=renderToStaticMarkup(<><ProcessingPreview format="docx" preview={{mode:'vision',document:{name:'合成.docx'},pages:[2],image_count:2,total_bytes:2048,images:[{page:2,media_id:'one',bytes:1024,preview_url:'/one.png'},{page:2,media_id:'two',bytes:1024,preview_url:'/two.png'}]}}/><ProcessingTaskStatus format="docx" task={{status:'running',mode:'ocr',stage:'解析第 2 页',processed:1,total:2}}/></>)
 assert.ok(html.includes('第 2 逻辑节'));assert.ok(html.includes('one.png'));assert.ok(html.includes('two.png'));assert.ok(html.includes('1 / 2 张嵌图'));assert.ok(html.includes('实际嵌图处理进度'));assert.ok(!html.includes('第 2 页'))
})
test('native chart cache preserves axis display scale and labels values as saved data',()=>{
 const html=renderToStaticMarkup(<StructuredChart chart={{kind:'barChart',axes:[{id:'y1',label:'收入',display_unit:'tenThousands',logarithmic:false}],series:[{name:'A',axis_ids:['y1'],points:[{label:'一月',value:'12345.6789',printed_text:'12345.6789',value_source:'native_cache',status:'confirmed'}]}]}}/>)
 for(const text of ['柱状图','显示倍率：万','线性刻度','关联 收入','原生保存值：12345.6789','原生图表数据'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('原标注：'));assert.ok(!html.includes('精确已核验'))
})
test('a selection across DOCX images preserves separate media coordinates and rejects malformed boxes',()=>{
 const regions=sourceHighlightRegions({coordinate_space:'mixed',rects:[],media_regions:[{media_id:'one',rects:[[.1,.2,.3,.4]]},{media_id:'two',rects:[[.4,.5,.6,.7],[0,0,2,1]]}]})
 assert.equal(regions.length,2);assert.equal(regions[0].media_id,'one');assert.equal(regions[1].media_id,'two')
 assert.deepEqual(regions[0].rects,[[.1,.2,.3,.4]]);assert.deepEqual(regions[1].rects,[[.4,.5,.6,.7]])
 assert.deepEqual(sourceHighlightRegions({media_id:'one',coordinate_space:'media',rects:[[0,0,1,1]]})[0].rects,[[0,0,1,1]])
})

test('label reread candidates remain reference text and do not claim verified corrections',()=>{
 const html=renderToStaticMarkup(<OcrCandidates candidates={['Candidate A','<script>unsafe</script>']}/>)
 assert.ok(html.includes('候选文字可能仍有误'));assert.ok(html.includes('Candidate A'))
 assert.ok(!html.includes('<script>'));assert.ok(!html.includes('已确认'))
 assert.equal(renderToStaticMarkup(<OcrCandidates/>),'')
})
