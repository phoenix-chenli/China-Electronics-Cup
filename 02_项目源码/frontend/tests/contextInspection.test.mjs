import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

// Compile the actual local components in memory: no browser, files or network.
const require=createRequire(import.meta.url);
const src=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src');
function load(name,overrides={}) {
  const cache=new Map();
  const read=file=>{
    if(cache.has(file))return cache.get(file).exports;
    const mod={exports:{}};cache.set(file,mod);
    const customRequire=specifier=>{
      if(specifier in overrides)return overrides[specifier];
      if(specifier.endsWith('.css'))return {};
      if(specifier==='./ResearchDialog')return {ResearchDialog:({children})=>React.createElement('section',null,children)};
      if(specifier==='./ResultPlot')return {ResultPlot:()=>null};
      if(specifier==='./api')return {api:()=>{throw Error('Unexpected network call');},isAbort:()=>false,messageOf:e=>String(e)};
      if(specifier.startsWith('.')){
        const base=path.resolve(path.dirname(file),specifier);
        const local=['.tsx','.ts','.js'].map(ext=>base+ext).find(fs.existsSync);
        if(local)return read(local);
      }
      return require(specifier);
    };
    const output=ts.transpileModule(fs.readFileSync(file,'utf8'),{fileName:file,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText;
    new Function('require','module','exports',output)(customRequire,mod,mod.exports);
    return mod.exports;
  };
  return read(path.join(src,name));
}
const {ContextAudit,slotOrigin}=load('ContextAudit.tsx');
const render=state=>renderToStaticMarkup(React.createElement(ContextAudit,{state}));
const slot=(value,user=false,id='answer-0123456789')=>({value,origin:user?'user_patch':'model_interpretation',confirmed_by_user:user,source_answer_id:id});
function nodes(tree){if(tree===null||tree===undefined||typeof tree!=='object')return [];if(Array.isArray(tree))return tree.flatMap(nodes);return [tree,...nodes(tree.props?.children)];}
function textOf(tree){if(tree===null||tree===undefined||typeof tree==='boolean')return '';if(typeof tree!=='object')return String(tree);if(Array.isArray(tree))return tree.map(textOf).join('');return textOf(tree.props?.children);}

test('full list retains all slots, values and per-slot provenance',()=>{
  const fields=['topic','entities','metric','time_range','dimensions','filters','comparison_base','unit','grain','denominator','assumptions'];
  const state={slots:Object.fromEntries(fields.map((field,i)=>[field,slot(`唯一值${i}`,i===0,`turn-${i}-full-version`)])),changes:[]};
  const original=structuredClone(state),html=render(state);
  for(let i=0;i<fields.length;i++){assert.ok(html.includes(`唯一值${i}`));assert.ok(html.includes(`title="turn-${i}-full-version"`));}
  assert.ok(html.includes('11 项'));assert.equal((html.match(/你已明确/g)||[]).length,1);
  assert.equal((html.match(/系统理解，待核对/g)||[]).length,10);assert.deepEqual(state,original);
});
test('legacy provenance is never silently promoted to user confirmation',()=>{
  assert.equal(slotOrigin({value:'甲'}),'来源未标明，待核对');
  assert.equal(slotOrigin({origin:'model_interpretation',confirmed_by_user:false}),'系统理解，待核对');
  assert.equal(slotOrigin({origin:'user_patch',confirmed_by_user:true}),'你已明确');
});
test('condition changes retain before/after, removal and source turn',()=>{
  const html=render({slots:{},changes:[
    {field:'unit',operation:'replace',before:slot('元'),after:slot('万元',true),source_answer_id:'change-123456789'},
    {field:'filters',operation:'remove',before:slot(['仅含已付款订单'],true),after:null},
  ]});
  for(const text of ['单位 · 替换','元','万元','筛选 · 撤销','仅含已付款订单','未设置','title="change-123456789"'])assert.ok(html.includes(text),text);
  assert.ok(html.includes('未据此推算数值变化'));
});
test('unknown condition is not described as user removal or confirmation',()=>{
  const html=render({slots:{},changes:[{field:'unit',operation:'mark_unknown',before:slot('亿元'),after:null}],unknown_fields:{unit:{previous_source_answer_id:'previous-full-0001'}},unresolved:[{field:'unit',kind:'explicit_patch_interpretation_unverified',source_answer_id:'turn-now-0001'}]});
  assert.ok(html.includes('单位 · 本轮未确认'));assert.ok(html.includes('未作为有效条件沿用'));
  assert.ok(html.includes('title="previous-full-0001"'));assert.ok(html.includes('title="turn-now-0001"'));
  assert.ok(html.includes('你已明确此条件，但本轮分析尚未核对其含义；明确修改仍保留。'));
  assert.ok(!html.includes('单位 · 撤销'));
});
test('missing history and unfamiliar unresolved codes stay explicit',()=>{
  assert.ok(render(undefined).includes('历史记录缺少差异时不会猜测补全'));
  const html=render({slots:{},changes:[],unresolved:[{field:'metric',kind:'future_reason'}]});
  assert.ok(html.includes('记录了尚未解决的口径问题'));assert.ok(!html.includes('future_reason'));
});
test('record text and full source identifiers are escaped',()=>{
  const html=render({slots:{topic:slot('<script>alert(1)</script>',false,'" onmouseover="bad')},changes:[]});
  assert.ok(html.includes('&lt;script&gt;'));assert.ok(html.includes('&quot; onmouseover=&quot;bad'));
  assert.ok(!html.includes('<script>'));assert.ok(!html.includes('title="" onmouseover='));
});
test('slot selection only proposes an editor field, without any write API',()=>{
  const selected=[],tree=ContextAudit({state:{slots:{unit:slot('万元')},changes:[]},onSelectField:field=>selected.push(field)});
  const button=nodes(tree).find(n=>n.type==='button');button.props.onClick();
  assert.deepEqual(selected,['unit']);assert.equal(button.props.type,'button');
});
test('existing ContextEditor really embeds current complete audit',()=>{
  const {ContextEditor}=load('AnswerReading.tsx');
  const answer={context_state:{slots:{unit:slot('万元')},changes:[{field:'unit',operation:'add',before:null,after:slot('万元')}]}};
  const html=renderToStaticMarkup(React.createElement(ContextEditor,{answer,onClose:()=>{},onApply:()=>{throw Error('SSR must not apply changes');}}));
  for(const text of ['当前完整口径','与上一轮相比','单位 · 新增','应用到下一问'])assert.ok(html.includes(text),text);
});

const editing={id:'metric-1',kind:'time',label:'月度收款',definition:'按入账时间所在月统计',references:[{table:'Payment',column:'created_at'}],aliases:['收款月份'],unit:'元',grain:'每个客户每月',time_grain:'calendar_month',timezone:'Asia/Shanghai',denominator:'有效订单',null_policy:'缺失时间不入组',status:'candidate',unexpected:'must not leak'};
const {definitionForSave}=load('SemanticEditor.tsx');
test('saving another field retains existing time grain and timezone',()=>{
  const old=structuredClone(editing),result=definitionForSave({...editing,label:'按月收款'});
  assert.equal(result.time_grain,'calendar_month');assert.equal(result.timezone,'Asia/Shanghai');
  assert.equal(result.label,'按月收款');assert.equal(result.status,undefined);assert.equal(result.unexpected,undefined);
  assert.deepEqual(editing,old);result.references[0].column='changed';result.aliases.push('new');assert.deepEqual(editing,old);
});
test('explicitly blank optional fields clear only those fields',()=>{
  const result=definitionForSave({...editing,timezone:' ',time_grain:''});
  assert.ok(!('timezone' in result));assert.ok(!('time_grain' in result));
  assert.equal(result.unit,'元');assert.equal(result.null_policy,'缺失时间不入组');
});
function semanticTree({confirmed=true}={}){
  const state={revision:7,source_revision:'actual-source-revision',confirmed:[],candidates:[editing],revoked:[]};
  const values=[state,{source:{name:'真实字段资料'},tables:[]},'',false,structuredClone(editing),confirmed],updates=[],calls=[],effects=[],refs=[];let i=0,r=0;
  const component=load('SemanticEditor.tsx',{
    react:{...React,useEffect:fn=>effects.push(fn),useRef:initial=>refs[r++]??(refs[r-1]={current:initial}),useState:initial=>{const index=i++;if(!(index in values))values[index]=typeof initial==='function'?initial():initial;return [values[index],next=>updates.push({index,next})];}},
    './api':{api:async(endpoint,options)=>{calls.push({endpoint,options});return {...state,revision:8};},isAbort:()=>false,messageOf:e=>String(e)},
  });
  return {tree:component.SemanticEditor({sourceId:'source-1',onClose:()=>{},onUpdated:()=>{}}),values,updates,calls};
}
test('rendered time inputs are editable and changing them removes confirmation',()=>{
  const {tree,values,updates}=semanticTree();
  for(const [label,key,value] of [['时间粒度','time_grain','calendar_day'],['时区','timezone','UTC']]){
    const field=nodes(tree).find(n=>n.type==='label'&&textOf(n).startsWith(label+'（选填）'));
    assert.ok(field,`UI label missing: ${label}`);
    const input=nodes(field).find(n=>n.type==='input');assert.equal(input.props.value,editing[key]);
    input.props.onChange({target:{value}});
    const update=updates.findLast(item=>item.index===4).next(values[4]);assert.equal(update[key],value);assert.equal(update.unit,'元');
    assert.deepEqual(updates.findLast(item=>item.index===5),{index:5,next:false});
  }
});
test('actual save callback uses existing PATCH and both revision guards',async()=>{
  const {tree,calls}=semanticTree();
  nodes(tree).find(n=>n.type==='button'&&textOf(n)==='确认并保存').props.onClick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,1);assert.equal(calls[0].endpoint,'/sources/source-1/semantics');assert.equal(calls[0].options.method,'PATCH');
  const body=JSON.parse(calls[0].options.body);
  assert.equal(body.action,'confirm');assert.equal(body.confirmed,true);assert.equal(body.expected_revision,7);assert.equal(body.expected_source_revision,'actual-source-revision');
  assert.equal(body.definition.time_grain,'calendar_month');assert.equal(body.definition.timezone,'Asia/Shanghai');assert.equal(body.definition.status,undefined);
});
test('unconfirmed editor is disabled and cannot send even if callback is invoked',async()=>{
  const {tree,calls}=semanticTree({confirmed:false});
  const save=nodes(tree).find(n=>n.type==='button'&&textOf(n)==='确认并保存');assert.equal(save.props.disabled,true);save.props.onClick();
  await new Promise(resolve=>setImmediate(resolve));assert.equal(calls.length,0);
});
