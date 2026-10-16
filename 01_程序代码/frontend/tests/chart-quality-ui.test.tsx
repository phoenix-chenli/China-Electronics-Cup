import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {StructuredChart,chartLabelCandidates,chartPointReason,chartSourceReferences} from '../src/document-processing'
import {chartReviewAxesRequired,chartReviewFields,documentReviewValues} from '../src/document-review'

const known={id:'point-one',label:'一月',value:'120',printed_text:'120',value_source:'printed',status:'confirmed',association_status:'confirmed'}
const chart={id:'INTERNAL_CHART_ID',title:'收入',kind:'bar',unit:'元',x_label:'月份',y_label:'收入',status:'pending',series:[{name:'甲系列',axis:'primary',points:[known]}]}

test('partial charts report observed points and keep estimated and unread values pending',()=>{
 const html=renderToStaticMarkup(<StructuredChart chart={{...chart,series:[{...chart.series[0],points:[known,{...known,id:'estimate',label:'二月',value:'15',value_source:'estimated'},{...known,id:'unknown',label:'三月',value:null,value_source:'unknown'}]}]}}/>)
 for(const text of ['局部图点待复核','1 / 3 个图点有文字或结构依据','2 个图点待复核','约 15','像素估读不能作为精确数值','无法读取','数值尚未读取','不能作为确定答案'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('精确已核验'));assert.ok(!html.includes('全部图点可用'))
})

test('blurry label alternatives remain candidates and do not replace the original label',()=>{
 const point={...known,label:'原标签',label_review:{status:'pending',candidates:['候选甲','候选甲','<script>候选乙</script>']},source_binding:{category:{candidates:[' 候选丙 ','']}}}
 assert.deepEqual(chartLabelCandidates(point),['候选甲','<script>候选乙</script>','候选丙'])
 const html=renderToStaticMarkup(<StructuredChart chart={{...chart,series:[{...chart.series[0],points:[point]}]}}/>)
 for(const text of ['原标签','类别标签候选 · 3 项','候选可能仍有误','不自动替换原标签','类别标签模糊或复读结果不一致','0 / 1 个图点有文字或结构依据'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('<script>'));assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('INTERNAL_CHART_ID'))
})

test('association and metadata gaps have specific readable explanations',()=>{
 assert.match(chartPointReason({...known,association_status:'pending'}),/类别、数值与系列的对应关系尚未核验/)
 assert.match(chartPointReason({...known,status:'pending',verification:'chart_axis_unverified'}),/单位或坐标轴依据不足/)
 assert.equal(chartPointReason(known),'')
 const html=renderToStaticMarkup(<StructuredChart chart={{...chart,metadata_status:{unit:'pending',x_label:'pending',y_label:'pending'},series:[{...chart.series[0],name_status:'pending',points:[{...known,status:'pending',association_status:'pending'}]}]}}/>)
 for(const text of ['单位待复核','横轴：月份（待复核）','纵轴：收入（待复核）','系列标签待复核','对应关系尚未核验'])assert.ok(html.includes(text),text)
})

test('chart source controls use real field boxes and preserve embedded-image identity',()=>{
 const value={...chart,media_id:'public-image',box:[0,0,1,1],metadata_evidence:{unit:{text:'元',box:[.7,.1,.8,.2]},x_label:{text:'月份',box:[.1,.8,.8,.9]},y_label:{text:'收入',box:[.01,.2,.1,.7]}}}
 const point={...known,source_binding:{label:'一月',value:'120',category:{text:'一月',box:[.1,.7,.2,.8],media_id:'category-image'},printed_value:{text:'120',box:[.1,.2,.2,.3]},series:{text:'甲系列',box:[.4,.1,.6,.2]}}}
 const references=chartSourceReferences(value,value.series[0],point)
 assert.deepEqual(references.map(item=>item.label),['类别标签','数值标注','系列名称','单位','横轴','纵轴'])
 assert.equal(references[0].media_id,'category-image');assert.equal(references[1].media_id,'public-image')
 assert.deepEqual(references[0].box,[.1,.7,.2,.8])
 const clicked:any[]=[]
 const tree=StructuredChart({chart:{...value,series:[{...value.series[0],points:[point]}]},onLocate:(...args:any[])=>clicked.push(args)})
 const elements:any[]=[]
 const walk=(node:any):void=>{if(Array.isArray(node)){node.forEach(walk);return}if(!React.isValidElement(node))return;elements.push(node);walk((node.props as any).children)}
 walk(tree)
 const button=elements.find(node=>node.type==='button'&&node.props['aria-label']==='定位类别标签 一月')
 assert.ok(button);button.props.onClick()
 assert.deepEqual(clicked,[[[.1,.7,.2,.8],'一月','category-image']])
 const html=renderToStaticMarkup(tree)
 for(const text of ['定位类别标签','定位数值标注','定位系列名称','定位单位','定位横轴','定位纵轴'])assert.ok(html.includes(text),text)
})

test('missing and malformed field coordinates never manufacture source controls',()=>{
 const value={...chart,box:[0,0,1,1],metadata_evidence:{unit:{text:'元'},x_label:{text:'月份',box:[0,0,2,1]},y_label:{text:'收入',box:[0,0,0,1]}}}
 const point={...known,source_binding:{category:{text:'一月'},value:{text:'120',box:[NaN,0,1,1]}}}
 assert.deepEqual(chartSourceReferences(value,value.series[0],point),[])
 const html=renderToStaticMarkup(<StructuredChart chart={{...value,series:[{...value.series[0],points:[point]}]}} onLocate={()=>{}}/>)
 assert.ok(!html.includes('<button'));assert.ok(!html.includes('undefined'))
})

test('shared metadata changes explain the need to review dependent points again',()=>{
 const html=renderToStaticMarkup(<StructuredChart chart={{...chart,series:[{...chart.series[0],points:[{...known,status:'pending',verification:'shared_metadata_changed'}]}]}}/>)
 for(const text of ['共享标题、单位或坐标轴已修改','此图点需要重新复核','修改共享的系列名、单位、标题或坐标轴','逐点对照原图确认'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('已自动确认'))
})

test('native chart cache retains actual zero and scale without claiming printed labels',()=>{
 const html=renderToStaticMarkup(<StructuredChart chart={{kind:'barChart',axes:[{id:'y1',label:'收入',display_unit:'tenThousands',logarithmic:false}],series:[{name:'甲',axis_ids:['y1'],points:[{label:'一月',value:'0',printed_text:'0',value_source:'native_cache',status:'confirmed'}]}]}}/>)
 for(const text of ['柱状图','显示倍率：万','线性刻度','关联 收入','原生保存值：0','原生图表数据','1 / 1 个图点有文字或结构依据'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('原标注：'));assert.ok(!html.includes('约 0'));assert.ok(!html.includes('精确已核验'))
})

test('empty chart categories are shown as missing instead of a blank label',()=>{
 const html=renderToStaticMarkup(<StructuredChart chart={{...chart,series:[{...chart.series[0],points:[{...known,label:'',status:'pending'}]}]}}/>)
 assert.ok(html.includes('未标明'));assert.ok(html.includes('0 / 1 个图点有文字或结构依据'))
 assert.ok(!html.includes('类别标签候选'))
})

test('backend point bindings expose actual axes and distinguish scalar values from source anchors',()=>{
 const point={...known,source_binding:{label:'一月',value:'120',unit_text:'元',category:{observation_id:1,text:'一月',box:[.2,.7,.3,.8]},printed_value:{observation_id:2,text:'120',box:[.2,.3,.3,.4]},x_axis:{observation_id:3,text:'月份',box:[.3,.9,.5,.95]},y_axis:{observation_id:4,text:'收入（元）',box:[.01,.2,.1,.6]},unit:{observation_id:4,text:'收入（元）',box:[.01,.2,.1,.6]},title:{observation_id:5,text:'收入',box:[.3,.01,.6,.1]}}}
 const sources=chartSourceReferences(chart,chart.series[0],point)
 for(const name of ['类别标签','数值标注','单位','图表标题','横轴','纵轴'])assert.ok(sources.some(item=>item.label===name),name)
 const html=renderToStaticMarkup(<StructuredChart chart={{...chart,relation_status:'partial',relation_audit:{issues:[{series_index:0,point_index:0,reason:'unverified_chart_metadata',missing_metadata:['unit_axis_attribution']}]},series:[{...chart.series[0],points:[{...known,status:'pending'}]}]}}/>)
 for(const text of ['局部图点待复核','单位或坐标轴依据不足'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('unit_axis_attribution'));assert.ok(!html.includes('unverified_chart_metadata'))
})

test('pie and donut review remove inapplicable axes while retaining the required fact context',()=>{
 for(const kind of ['pie','donut','pieChart','doughnutChart','pieChart,doughnutChart']){
  const item={kind:'chart_point',chart_kind:kind}
  assert.equal(chartReviewAxesRequired(item),false,kind)
  assert.deepEqual(chartReviewFields(item).map(([key])=>key),['label','series_name','unit','title'])
  const values=documentReviewValues(item,{text:'0',label:'公开类别',series_name:'销售',unit:'%',title:'销售占比',x_label:'历史横轴',y_label:'历史纵轴',api_key:'PRIVATE_NOT_SENT'})
  assert.deepEqual(values,{text:'0',label:'公开类别',series_name:'销售',unit:'%',title:'销售占比',x_label:'',y_label:''})
  assert.ok(!Object.hasOwn(values,'api_key'))
 }
})

test('axis charts and unrecognized chart kinds keep both axes in the review request',()=>{
 for(const kind of ['bar','line','scatter','stacked_bar','unknown','','pie,line']){
  const item={kind:'chart_point',chart_kind:kind}
  assert.equal(chartReviewAxesRequired(item),true,kind)
  assert.ok(chartReviewFields(item).some(([key])=>key==='x_label'))
  assert.ok(chartReviewFields(item).some(([key])=>key==='y_label'))
  const values=documentReviewValues(item,{text:'120',label:'一月',series_name:'销售',unit:'元',title:'收入',x_label:'月份',y_label:'收入'})
  assert.equal(values.x_label,'月份');assert.equal(values.y_label,'收入')
 }
})
