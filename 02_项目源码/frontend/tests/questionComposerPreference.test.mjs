/** Actual component handlers with explicit hook/HTTP fakes; no OS IME claim. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const require=createRequire(import.meta.url),src=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src');
const KEY='zhiheng-suggestion-keyboard-enabled-v1',TEXT='接着核对这条事实的适用范围？';
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
function nodes(tree){if(tree==null||typeof tree!=='object')return [];if(Array.isArray(tree))return tree.flatMap(nodes);return [tree,...nodes(tree.props?.children)];}
function textOf(tree){if(tree==null||typeof tree==='boolean')return '';if(typeof tree!=='object')return String(tree);return Array.isArray(tree)?tree.map(textOf).join(''):textOf(tree.props?.children);}
function storage(initial={}){
 const data=new Map(Object.entries(initial));let denied=false;
 return {data,deny:()=>{denied=true;},getItem:key=>{if(denied)throw Error('Storage denied');return data.get(key)??null;},
  setItem:(key,value)=>{if(denied)throw Error('Storage full');data.set(key,value);},removeItem:key=>data.delete(key)};
}
const answer=(id='a',project_id='p')=>({id,project_id,status:'completed',context_revision:'v1',sources:[],turn_index:1});
function load(react,api){
 const cache=new Map();
 const read=file=>{
  if(cache.has(file))return cache.get(file).exports;
  const mod={exports:{}};cache.set(file,mod);
  const code=ts.transpileModule(fs.readFileSync(file,'utf8'),{fileName:file,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  new Function('require','module','exports',code)(id=>{
   if(id==='react')return react;if(id==='./api')return {api,messageOf:e=>e.message};if(id.endsWith('.css'))return {};
   if(id.startsWith('.')){const base=path.resolve(path.dirname(file),id);return read(['.ts','.tsx','.js'].map(ext=>base+ext).find(fs.existsSync));}
   return require(id);
  },mod,mod.exports);return mod.exports;
 };
 return read(path.join(src,'QuestionComposer.tsx'));
}
function harness({disk=storage(),acceptPending=null,enabled=true,suggestionsSuspended=false,initialAnswer=answer()}={}){
 const state=[],refs=[],effects=[],callbacks=[];let s=0,r=0,e=0,c=0,tree,draft='',submitted=0;
 const calls=[],errors=[];let props;
 globalThis.window=Object.assign(new EventTarget(),{localStorage:disk});
 globalThis.document=Object.assign(new EventTarget(),{visibilityState:'visible',activeElement:null});
 globalThis.requestAnimationFrame=callback=>{callback();return 1;};
 const react={...React,
  useState:initial=>{const i=s++;if(!(i in state))state[i]=typeof initial==='function'?initial():initial;return [state[i],value=>{state[i]=typeof value==='function'?value(state[i]):value;}];},
  useRef:initial=>{const i=r++;return refs[i]??(refs[i]={current:initial});},
  useCallback:(callback,deps)=>{const i=c++,old=callbacks[i];if(!old||deps.some((v,k)=>!Object.is(v,old.deps[k])))callbacks[i]={callback,deps};return callbacks[i].callback;},
  useEffect:(effect,deps)=>{const i=e++,old=effects[i];if(!old||deps.some((v,k)=>!Object.is(v,old.deps[k]))){old?.cleanup?.();effects[i]={effect,deps,pending:true};}},
 };
 const api=async(url,options={})=>{
  calls.push({url,...options});
  if(url.endsWith('suggestion-preference')){if(options.method==='PUT')enabled=JSON.parse(options.body).enabled;return {enabled};}
  const scope={project_id:props.answer.project_id},key='record-'+props.answer.id;
  if(url.endsWith('/accept'))return acceptPending?acceptPending.promise:{scope,key,state:'accepted',fill_text:TEXT};
  return {scope,key,state:'ready',suggestions:[{id:'n1',text:TEXT,action:'ask',payload:{}}]};
 };
 const component=load(react,api);
 props={answer:initialAnswer,draft,setDraft:value=>{draft=value;},onSubmit:()=>submitted++,disabled:false,onError:value=>errors.push(value),onAction:()=>{},suggestionsSuspended};
 const render=(patch={})=>{props={...props,draft,...patch};s=r=e=c=0;tree=component.QuestionComposer(props);for(const item of effects)if(item.pending){item.pending=false;item.cleanup=item.effect();}return tree;};
 render();render();
 return {render,async settle(){await tick();render();render();},calls,errors,disk,
  get tree(){return tree;},get draft(){return draft;},get submitted(){return submitted;},
  input:()=>nodes(tree).find(n=>n.type==='textarea'),
  checkbox:label=>nodes(tree).find(n=>n.type==='label'&&textOf(n)===label)?.props.children[0],
  suggestion:()=>nodes(tree).find(n=>n.type==='button'&&textOf(n)===TEXT),
  button:label=>nodes(tree).find(n=>n.type==='button'&&textOf(n)===label),
  key(type,key='Shift',extra={}){const input=nodes(tree).find(n=>n.type==='textarea');input.props[type==='down'?'onKeyDown':'onKeyUp']({key,code:key==='Shift'?'ShiftLeft':key,repeat:false,ctrlKey:false,altKey:false,metaKey:false,shiftKey:type==='down'&&key==='Shift',nativeEvent:{isComposing:false},preventDefault:()=>{},...extra});},
  unmount:()=>effects.forEach(item=>item.cleanup?.()),
 };
}
const accepts=h=>h.calls.filter(c=>c.url.endsWith('/accept'));

test('separate native checkboxes have clear labels in a keyboard-operable disclosure',async()=>{
 const h=harness();await h.settle();
 const summary=nodes(h.tree).find(n=>n.type==='summary');assert.equal(textOf(summary),'追问设置');
 assert.equal(h.checkbox('显示追问建议').props.checked,true);assert.equal(h.checkbox('Shift快捷填入'),undefined);
 assert.equal(h.checkbox('Shift 快捷填入').props.checked,true);
 const markup=renderToStaticMarkup(h.tree);assert.ok(markup.includes('<details'));assert.ok(markup.includes('type="checkbox"'));
 assert.ok(!markup.includes('自动检测'));h.unmount();
});
test('turning off keyboard is local only; click still accepts without submitting',async()=>{
 const h=harness();await h.settle();const before=h.calls.length;
 h.checkbox('Shift 快捷填入').props.onChange();h.render();assert.equal(h.calls.length,before);
 assert.equal(h.disk.data.get(KEY),'false');assert.ok(h.suggestion());assert.ok(textOf(h.tree).includes('点击填入'));
 h.input().props.onFocus();h.key('down');h.key('up');await h.settle();assert.equal(accepts(h).length,0);
 h.suggestion().props.onClick();await h.settle();assert.equal(accepts(h).length,1);assert.equal(h.draft,TEXT);assert.equal(h.submitted,0);
 h.button('撤销填入').props.onClick();h.render();assert.equal(h.draft,'');h.unmount();
});
test('global display toggle still uses backend preference and hides clickable suggestions',async()=>{
 const h=harness();await h.settle();h.checkbox('显示追问建议').props.onChange();await h.settle();
 assert.equal(h.suggestion(),undefined);assert.equal(h.checkbox('显示追问建议').props.checked,false);
 const put=h.calls.find(c=>c.method==='PUT');assert.deepEqual(JSON.parse(put.body),{enabled:false});
 assert.equal(h.checkbox('Shift 快捷填入').props.checked,true);h.input().props.onFocus();h.key('down');h.key('up');assert.equal(accepts(h).length,0);h.unmount();
});
test('keyboard choice survives context switches and a new component instance',async()=>{
 const disk=storage(),h=harness({disk});await h.settle();h.checkbox('Shift 快捷填入').props.onChange();h.render();
 h.render({answer:answer('other','project2')});await h.settle();assert.equal(h.checkbox('Shift 快捷填入').props.checked,false);assert.ok(h.suggestion());h.unmount();
 const remount=harness({disk});await remount.settle();assert.equal(remount.checkbox('Shift 快捷填入').props.checked,false);remount.unmount();
});
test('resilient storage keeps local toggle usable when writing is denied',async()=>{
 const disk=storage(),h=harness({disk});await h.settle();disk.deny();
 h.checkbox('Shift 快捷填入').props.onChange();h.render();assert.equal(h.checkbox('Shift 快捷填入').props.checked,false);
 h.render({answer:answer('another')});await h.settle();assert.equal(h.checkbox('Shift 快捷填入').props.checked,false);assert.deepEqual(h.errors,[]);h.unmount();
});
test('changing either setting cancels an armed Shift before keyup',async()=>{
 for(const label of ['Shift 快捷填入','显示追问建议']){
  const h=harness();await h.settle();h.input().props.onFocus();h.key('down');h.checkbox(label).props.onChange();h.key('up');await h.settle();assert.equal(accepts(h).length,0);h.unmount();
 }
});
test('composition and shortcut chords retain their existing no-accept rules',async()=>{
 const h=harness();await h.settle();h.input().props.onFocus();
 h.input().props.onCompositionStart();h.key('down');h.input().props.onCompositionEnd();h.key('up');assert.equal(accepts(h).length,0);
 h.key('down');h.key('down','Control',{ctrlKey:true});h.key('up');assert.equal(accepts(h).length,0);
 h.key('down');h.key('up');await h.settle();assert.equal(accepts(h).length,1);assert.equal(h.draft,TEXT);assert.equal(h.submitted,0);h.unmount();
});
test('keyboard-off blocks a delayed shortcut fill even after toggling back on',async()=>{
 const pending=deferred(),h=harness({acceptPending:pending});await h.settle();h.input().props.onFocus();h.key('down');h.key('up');assert.equal(accepts(h).length,1);
 h.checkbox('Shift 快捷填入').props.onChange();h.render();h.checkbox('Shift 快捷填入').props.onChange();h.render();
 pending.resolve({state:'accepted',fill_text:TEXT});await h.settle();assert.equal(h.draft,'');assert.equal(h.submitted,0);h.unmount();
});
test('click acceptance is unaffected by keyboard-only preference changing while pending',async()=>{
 const pending=deferred(),h=harness({acceptPending:pending});await h.settle();h.suggestion().props.onClick();
 h.checkbox('Shift 快捷填入').props.onChange();h.render();pending.resolve({state:'accepted',fill_text:TEXT});await h.settle();
 assert.equal(h.draft,TEXT);assert.equal(h.submitted,0);h.unmount();
});
test('changed scope suspends suggestion requests and aborts the previous scope request',async()=>{
 const initiallySuspended=harness({suggestionsSuspended:true});await initiallySuspended.settle();
 assert.equal(initiallySuspended.calls.length,0);assert.equal(initiallySuspended.suggestion(),undefined);initiallySuspended.unmount();
 const h=harness();await h.settle();
 const request=h.calls.find(call=>call.url.endsWith('/suggestions')),count=h.calls.length,oldClick=h.suggestion().props.onClick;
 h.input().props.onFocus();h.key('down');h.render({suggestionsSuspended:true});h.key('up');oldClick();await h.settle();
 assert.equal(request.signal.aborted,true);assert.equal(h.suggestion(),undefined);assert.equal(h.calls.length,count);assert.equal(accepts(h).length,0);
 h.render({suggestionsSuspended:false});await h.settle();assert.ok(h.suggestion());
 h.suggestion().props.onClick();await h.settle();assert.equal(h.draft,TEXT);assert.equal(h.submitted,0);h.unmount();
});
test('late acceptance from a suspended scope cannot fill after scope undo',async()=>{
 const pending=deferred(),h=harness({acceptPending:pending});await h.settle();h.suggestion().props.onClick();
 h.render({suggestionsSuspended:true});h.render({suggestionsSuspended:false});await h.settle();
 pending.resolve({state:'accepted',fill_text:TEXT});await h.settle();assert.equal(h.draft,'');assert.equal(h.submitted,0);assert.deepEqual(h.errors,[]);h.unmount();
});

for(const status of ['failed','cancelled'])test(`${status} exposes an independent question without pretending to inherit or changing the draft`,async()=>{
 const h=harness({initialAnswer:{...answer(),status,turn_index:6}});await h.settle();
 assert.match(textOf(h.tree),/独立新问/);assert.match(textOf(h.tree),/请写明对象、时间、指标等完整条件/);assert.doesNotMatch(textOf(h.tree),/从第 6 轮继续|追问设置/);
 assert.match(h.input().props.placeholder,/独立新问/);assert.match(h.input().props['aria-describedby'],/composer-restart-notice/);
 assert.equal(h.calls.length,0);assert.ok(h.button('发送新问题'));assert.equal(h.button('发送新问题').props.disabled,true);
 h.input().props.onChange({target:{value:'把年份改成2024年，其余不变'}});h.render();assert.equal(h.draft,'把年份改成2024年，其余不变');assert.equal(h.submitted,0);
 assert.equal(h.button('发送新问题').props.disabled,false);h.unmount();
});
for(const status of ['completed','needs_clarification','insufficient_evidence'])test(`${status} retains an explicit continuation hint`,async()=>{
 const h=harness({initialAnswer:{...answer(),status,turn_index:6}});await h.settle();
 assert.match(textOf(h.tree),/从第 6 轮继续/);assert.doesNotMatch(textOf(h.tree),/独立新问/);assert.match(h.input().props.placeholder,/继续追问/);assert.ok(h.button('发送问题'));
 if(status==='needs_clarification')assert.equal(h.calls.length,0);h.unmount();
});
for(const status of ['queued','running'])test(`${status} waits for the actual terminal state before promising continuation`,async()=>{
 const h=harness({initialAnswer:{...answer(),status,turn_index:6}});h.render({disabled:true});await h.settle();
 assert.doesNotMatch(textOf(h.tree),/从第 6 轮继续|独立新问/);assert.match(h.input().props.placeholder,/待本轮完成/);assert.equal(h.calls.length,0);h.unmount();
});
