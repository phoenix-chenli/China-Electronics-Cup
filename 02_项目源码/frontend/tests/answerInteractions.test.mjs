/** Exercises the actual React handlers without a browser or model/network calls. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import ts from 'typescript';
import React from 'react';
const require=createRequire(import.meta.url),src=path.resolve('src');
const nodes=tree=>tree==null||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];
const label=tree=>tree==null||typeof tree==='boolean'?'':typeof tree!=='object'?String(tree):Array.isArray(tree)?tree.map(label).join(''):label(tree.props?.children);
function harness(answer,{active=true}={}){
 const store=new Map(),states=[],refs=[],effects=[];let stateIndex=0,refIndex=0,effectIndex=0,tree,apiCalls=0,branches=0;const drafts=[];
 const media=Object.assign(new EventTarget(),{matches:true});const window={matchMedia:query=>query.includes('max-width')?{...media,matches:false}:media};
 const fakeReact={...React,useId:()=>':clarification:',useState:initial=>{const i=stateIndex++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return [states[i],value=>{states[i]=typeof value==='function'?value(states[i]):value;}];},useRef:initial=>{const i=refIndex++;return refs[i]??(refs[i]={current:initial});},useEffect:(effect,deps)=>{const i=effectIndex++,old=effects[i];if(!old||!deps||deps.length!==old.deps?.length||deps.some((value,index)=>!Object.is(value,old.deps[index]))){old?.cleanup?.();effects[i]={effect,deps,pending:true};}},useLayoutEffect:()=>{}};
 const cache=new Map();const load=file=>{if(cache.has(file))return cache.get(file).exports;const module={exports:{}};cache.set(file,module);const customRequire=specifier=>{
  if(specifier==='react')return fakeReact;if(specifier.endsWith('.css'))return {};
  if(['./SafeAnswerMarkdown','./ResultPlot','./ResearchDialog','./ContextAudit','./PublishedResultTable','./AnswerNotes'].includes(specifier))return {[specifier.slice(2)]:()=>null};
  if(specifier==='./browserStorage')return {tabStorage:{getItem:key=>store.get(key)??null,setItem:(key,value)=>{store.set(key,value);return true;}}};
  if(specifier==='./api')return {api:()=>{apiCalls++;throw Error('No network expected');},messageOf:e=>String(e)};
  if(specifier.startsWith('.'))return load(path.join(src,`${specifier.slice(2)}.ts`));return require(specifier);
 };const code=ts.transpileModule(fs.readFileSync(file,'utf8'),{fileName:file,compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true,target:ts.ScriptTarget.ES2022}}).outputText;new Function('require','module','exports','window',code)(customRequire,module,module.exports,window);return module.exports;};
 const {AnswerReading}=load(path.join(src,'AnswerReading.tsx'));const noop=()=>{};
 let props={answer,active,favorite:false,onEvidence:noop,onDraft:text=>drafts.push(text),onBranch:()=>branches++,onFavorite:noop,onVersions:noop,onCancel:noop,onError:noop,onReleaseChart:noop,onScenario:noop};
 const Component=AnswerReading(props).type;
 const render=()=>{stateIndex=refIndex=effectIndex=0;tree=Component(props);for(const effect of effects)if(effect.pending){effect.pending=false;effect.cleanup=effect.effect();}return tree;};
 render();return {render,store,get tree(){return tree;},get apiCalls(){return apiCalls;},drafts,get branches(){return branches;},updateAnswer:value=>{props={...props,answer:value};render();},unmount:()=>effects.forEach(effect=>effect.cleanup?.())};
}
const answer={id:'a1',project_id:'p1',question:'统计',mode:'database',status:'completed',created_at:'2026-10-18',elapsed_ms:300,sources:[],result:{text:'',claims:[{text:'数字为 3',evidence_ids:['E1']}],columns:['对象','数量'],rows:[['甲',3]],evidence:[{id:'E1',kind:'table',source_id:'s1',source_name:'数据',table_name:'Invoice',stale:false}],notes:[],verification:{status:'passed',checks:[]}}};
const button=(h,text)=>nodes(h.tree).find(n=>n.type==='button'&&label(n)===text);
test('read/compare changes presentation only and retains explicit choice on answer polling',t=>{
 const h=harness(answer);t.after(h.unmount);assert.match(h.tree.props.className,/mode-compare/);
 button(h,'阅读').props.onClick();h.render();assert.match(h.tree.props.className,/mode-read/);
 h.updateAnswer({...answer,result:{...answer.result,text:'new poll'}});assert.match(h.tree.props.className,/mode-read/);
 assert.match([...h.store.values()][0],/"mode":"read"/);assert.equal(h.apiCalls,0);
});
test('divider keyboard supports bounded arrows, Home, End and reset with no request',t=>{
 const h=harness(answer);t.after(h.unmount);const key=value=>{const separator=nodes(h.tree).find(n=>n.props?.role==='separator');separator.props.onKeyDown({key:value,preventDefault(){},shiftKey:false});h.render();return nodes(h.tree).find(n=>n.props?.role==='separator').props['aria-valuenow'];};
 assert.equal(key('ArrowLeft'),42);assert.equal(key('End'),55);assert.equal(key('ArrowRight'),55);assert.equal(key('Home'),35);assert.equal(key('Enter'),43);assert.equal(h.apiCalls,0);
});
test('divider pointer is captured and cannot collapse either evidence or answer below bounds',t=>{
 const h=harness(answer);t.after(h.unmount);const grid=nodes(h.tree).find(n=>n.props?.className==='answer-layout answer-comparison');grid.props.ref.current={getBoundingClientRect:()=>({left:0,width:1000})};
 let captured=false;let divider=nodes(h.tree).find(n=>n.props?.role==='separator');
 divider.props.onPointerDown({pointerId:7,preventDefault(){},currentTarget:{setPointerCapture(){captured=true;}}});
 divider.props.onPointerMove({pointerId:7,clientX:980});h.render();divider=nodes(h.tree).find(n=>n.props?.role==='separator');assert.equal(divider.props['aria-valuenow'],55);assert.equal(captured,true);
 divider.props.onPointerUp({pointerId:7,currentTarget:{releasePointerCapture(){captured=false;}}});divider.props.onPointerMove({pointerId:7,clientX:10});h.render();assert.equal(nodes(h.tree).find(n=>n.props?.role==='separator').props['aria-valuenow'],55);assert.equal(captured,false);assert.equal(h.apiCalls,0);
});
for(const status of ['failed','cancelled'])test(`${status} retry fills the complete saved question without submitting or creating a false branch`,t=>{
 const question='完整原问题：统计2025年各站点的服务金额，并按合同规则复算优惠后金额。',h=harness({...answer,status,question,error:'本轮未完成'});t.after(h.unmount);
 assert.match(label(h.tree),/不会作为后续回答的上文/);assert.equal(button(h,'从这里分出新思路'),undefined);
 button(h,'编辑后重试').props.onClick();assert.deepEqual(h.drafts,[question]);assert.equal(h.apiCalls,0);assert.equal(h.branches,0);
});
test('retrying a historical failed answer first opens that turn without replacing the current branch draft',t=>{
 const h=harness({...answer,status:'failed'},{active:false});t.after(h.unmount);
 button(h,'打开本轮重试').props.onClick();assert.equal(h.branches,1);assert.deepEqual(h.drafts,[]);assert.equal(h.apiCalls,0);assert.equal(button(h,'从这一轮继续'),undefined);
});
