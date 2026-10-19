/** Actual TSX handlers with local hook/HTTP/clock fakes; not browser or OS IME evidence. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
import React from 'react';

const require=createRequire(import.meta.url),src=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
const nodes=tree=>tree==null||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];
const textOf=tree=>tree==null||typeof tree==='boolean'?'':typeof tree!=='object'?String(tree):Array.isArray(tree)?tree.map(textOf).join(''):textOf(tree.props?.children);
const source=(id,name=id,project_id='p')=>({id,project_id,name,kind:'spreadsheet',format:'csv',bytes:30,status:'ready',usable:true,archived:false,warnings:[],stage:'ready'});
const record=(id,question)=>({id,question,status:'completed',created_at:'2026-10-18',turn_index:1});
function storage(){const data=new Map();return {data,getItem:key=>data.get(key)??null,setItem:(key,value)=>{data.set(key,value);return true;},removeItem:key=>{data.delete(key);return true;}};}
class InputTarget {}
function stub(name){return Object.defineProperty(()=>null,'name',{value:name});}
const children=Object.fromEntries(['QuestionComposer','AnswerReading','ContextEditor','ScenarioDialog','ResearchDialog','ReadingPanel','SemanticEditor','SourceDiagnosticsDialog','SourceVersionsDialog','BriefBuilder','WorkbenchSidebar','WorkbenchSearch','ScopePicker'].map(name=>[name,stub(name)]));

function harness(file,exportName,initialProps,{api=async()=>{throw Error('Unexpected local API call');},modules={}}={}){
 const state=[],refs=[],effects=[],callbacks=[],timers=new Map();let s=0,r=0,e=0,c=0,nextTimer=0,dirty=true,tree,props=initialProps;
 const location={hash:''};const history={pushState(_state,_unused,url){location.hash=String(url);},replaceState(_state,_unused,url){location.hash=String(url);}};
 const environment={
  window:Object.assign(new EventTarget(),{location,history,matchMedia:()=>Object.assign(new EventTarget(),{matches:false})}),
  document:Object.assign(new EventTarget(),{activeElement:null,getElementById:()=>null,querySelector:()=>null}),
  HTMLInputElement:InputTarget,requestAnimationFrame:callback=>{callback();return 1;},
  setTimeout:callback=>{const id=++nextTimer;timers.set(id,callback);return id;},clearTimeout:id=>timers.delete(id),
  setInterval:()=>{throw Error('Unexpected recurring timer');},clearInterval:()=>{},
 };
 const changed=(old,deps)=>!old||!deps||deps.length!==old.deps?.length||deps.some((v,k)=>!Object.is(v,old.deps[k]));
 const react={...React,
  useState:initial=>{const i=s++;if(!(i in state))state[i]=typeof initial==='function'?initial():initial;return [state[i],value=>{const next=typeof value==='function'?value(state[i]):value;if(!Object.is(next,state[i])){state[i]=next;dirty=true;}}];},
  useRef:initial=>{const i=r++;return refs[i]??(refs[i]={current:initial});},
  useCallback:(callback,deps)=>{const i=c++;if(changed(callbacks[i],deps))callbacks[i]={callback,deps};return callbacks[i].callback;},
  useEffect:(effect,deps)=>{const i=e++;if(changed(effects[i],deps)){effects[i]?.cleanup?.();effects[i]={effect,deps,pending:true};}},
 };
 const code=ts.transpileModule(fs.readFileSync(path.join(src,file),'utf8'),{fileName:file,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const navigation={exports:{}};
 const navigationCode=ts.transpileModule(fs.readFileSync(path.join(src,'workspaceNavigation.ts'),'utf8'),{fileName:'workspaceNavigation.ts',compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
 new Function('module','exports','window',navigationCode)(navigation,navigation.exports,environment.window);
 const researchTypes={exports:{}};
 const researchTypesCode=ts.transpileModule(fs.readFileSync(path.join(src,'researchTypes.ts'),'utf8'),{fileName:'researchTypes.ts',compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
 new Function('module','exports',researchTypesCode)(researchTypes,researchTypes.exports);
 const mod={exports:{}};
 new Function('require','module','exports',...Object.keys(environment),code)(id=>{
  if(id==='react')return react;
  if(id==='./api')return {api,messageOf:error=>error.message,isAbort:error=>error.name==='AbortError'};
  if(id==='./sourceDisplay')return {sourceExtent:()=> '1 页',sourceSize:()=> '30 B'};
  if(id in modules)return modules[id];
  if(id==='./workspaceNavigation')return navigation.exports;
  if(id.startsWith('./')&&id!=='./researchTypes')return children;
  if(id==='./researchTypes')return researchTypes.exports;
  if(id.endsWith('.css'))return {};
  return require(id);
 },mod,mod.exports,...Object.values(environment));
 const component=mod.exports[exportName];
 const render=(patch={})=>{props={...props,...patch};dirty=true;let passes=0;while(dirty){assert.ok(++passes<30,'component effects must settle');dirty=false;s=r=e=c=0;tree=component(props);for(const item of effects)if(item.pending){item.pending=false;item.cleanup=item.effect();}}return tree;};
 render();
 return {render,async settle(){for(let i=0;i<4;i++){await tick();render();}},
  async timers(){const pending=[...timers.values()];timers.clear();pending.forEach(callback=>callback());await tick();render();},
  globalKey(key,extra={},editing=false,blocked=false){const previous=environment.document.querySelector;environment.document.querySelector=()=>blocked?{}:null;const event=new Event('keydown',{cancelable:true});Object.defineProperties(event,Object.fromEntries(Object.entries({key,repeat:false,isComposing:false,ctrlKey:false,metaKey:false,shiftKey:false,altKey:false,target:{closest:()=>editing?{}:null},...extra}).map(([key,value])=>[key,{value}])));environment.window.dispatchEvent(event);environment.document.querySelector=previous;render();return event.defaultPrevented;},
  get tree(){return tree;},node:predicate=>nodes(tree).find(predicate),
  button:label=>nodes(tree).find(n=>n.type==='button'&&textOf(n)===label),
  input:label=>nodes(tree).find(n=>n.type==='input'&&n.props['aria-label']===label),
  component:name=>nodes(tree).find(n=>n.type===children[name]),
  key(key,target=new InputTarget(),extra={}){let prevented=false;nodes(tree).find(n=>n.props?.className==='wb-command').props.onKeyDown({key,target,nativeEvent:{isComposing:false},preventDefault:()=>{prevented=true;},...extra});render();return prevented;},
  unmount(){effects.forEach(item=>item.cleanup?.());timers.clear();},
 };
}

function scopeHarness(scope=['A'],sources=[source('A','甲资料'),source('B','乙资料')]){
 const applied=[],closed=[];
 const h=harness('Workbench.tsx','ScopePicker',{sources,scope,onApply:ids=>applied.push([...ids]),onClose:()=>closed.push(true),onImport:()=>{}});
 return {h,applied,closed,scope,checkbox:name=>nodes(h.node(n=>n.type==='label'&&textOf(n).includes(name))).find(n=>n.type==='input')};
}

test('scope changes remain transactional when cancelled',()=>{
 const {h,applied,closed,scope,checkbox}=scopeHarness();
 checkbox('乙资料').props.onChange();h.render();assert.equal(checkbox('乙资料').props.checked,true);
 assert.deepEqual(scope,['A']);assert.deepEqual(applied,[]);
 h.button('取消').props.onClick();assert.equal(closed.length,1);assert.deepEqual(applied,[]);h.unmount();
});

for(const [change,sources] of [
 ['unusable',[{...source('A','甲资料'),usable:false},source('B','乙资料')]],
 ['archived',[{...source('A','甲资料'),archived:true},source('B','乙资料')]],
 ['removed',[source('B','乙资料')]],
])test(`scope cannot apply a selection that became ${change} while open`,()=>{
 const {h,applied,checkbox}=scopeHarness();h.render({sources});
 assert.equal(h.button('使用这 1 份资料').props.disabled,true);
 assert.ok(h.node(n=>n.props?.role==='alert'),'invalid selection must be explained');
 h.button('移除失效资料').props.onClick();h.render();
 checkbox('乙资料').props.onChange();h.render();
 assert.equal(h.button('使用这 1 份资料').props.disabled,false);
 h.button('使用这 1 份资料').props.onClick();assert.deepEqual(applied,[['B']]);h.unmount();
});

function searchHarness(){
 const calls=[],opened=[],views=[],newCalls=[];
 const h=harness('Workbench.tsx','WorkbenchSearch',{pid:'p',sources:[],onClose:()=>{},onView:view=>views.push(view),onNew:()=>newCalls.push(true),onRead:()=>{},onRecord:id=>opened.push(id),onImport:()=>{}},{api:(url,options)=>{const pending=deferred();calls.push({url,options,...pending});return pending.promise;}});
 return {h,calls,opened,views,newCalls,change:value=>{h.input('查找资料、分析或操作').props.onChange({target:{value}});h.render();}};
}

test('command search never activates a previous query result while the next query is pending',async()=>{
 const {h,calls,opened,change}=searchHarness();change('甲');await h.timers();
 calls[0].resolve({items:[record('old','甲分析')]});await h.settle();assert.ok(textOf(h.tree).includes('甲分析'));
 change('乙');assert.ok(!textOf(h.tree).includes('甲分析'));
 h.key('Enter');assert.deepEqual(opened,[]);
 await h.timers();calls[1].resolve({items:[record('new','乙分析')]});await h.settle();
 h.key('Enter');assert.deepEqual(opened,['new']);h.unmount();
});

test('late results from an aborted project search cannot replace the current project results',async()=>{
 const {h,calls,opened,change}=searchHarness();change('报告');await h.timers();
 h.render({pid:'p2'});await h.timers();assert.equal(calls[0].options.signal.aborted,true);
 calls[1].resolve({items:[record('current','当前项目报告')]});await h.settle();
 calls[0].resolve({items:[record('old','旧项目报告')]});await h.settle();
 assert.ok(!textOf(h.tree).includes('旧项目报告'));h.key('Enter');assert.deepEqual(opened,['current']);h.unmount();
});

test('Enter on a focused command button retains native button activation and is not hijacked',()=>{
 const {h,views,newCalls}=searchHarness();const button=h.node(n=>n.type==='button'&&textOf(n).startsWith('打开资料库'));
 assert.ok(button);assert.equal(h.key('Enter',{}),false);assert.deepEqual(newCalls,[]);assert.deepEqual(views,[]);
 button.props.onClick();assert.deepEqual(views,['sources']);assert.deepEqual(newCalls,[]);h.unmount();
});

test('command input keeps composition Enter inert but accepts an ordinary input Enter',()=>{
 const {h,newCalls}=searchHarness();assert.equal(h.key('Enter',new InputTarget(),{nativeEvent:{isComposing:true}}),false);
 assert.deepEqual(newCalls,[]);assert.equal(h.key('Enter'),true);assert.equal(newCalls.length,1);h.unmount();
});

for(const editWhilePending of [false,true])test(`a delayed answer POST ${editWhilePending?'preserves a newer draft':'consumes only its submitted draft'}`,async()=>{
 const tab=storage(),preference=storage(),pending=deferred(),calls=[];
 const h=harness('ResearchApp.tsx','default',{}, {modules:{'./browserStorage':{tabStorage:tab,preferenceStorage:preference}},api:async(url,options={})=>{
  calls.push({url,...options});
  if(url==='/projects')return [{id:'p',name:'离线项目'}];
  if(url==='/model')return {configured:true,available_models:[],models:{}};
  if(url==='/projects/p/sources?archived=false')return [source('A','甲资料')];
  if(url==='/projects/p/favorites')return [];
  if(url.startsWith('/projects/p/workspace/'))return {revision:0,state:{source_ids:['A']}};
  if(url==='/projects/p/answers'&&options.method==='POST')return pending.promise;
  throw Error(`Unexpected local API route: ${url}`);
 }});
 await h.settle();let composer=h.component('QuestionComposer');assert.ok(composer);assert.equal(composer.props.disabled,false);
 composer.props.setDraft('原本要发送的问题');h.render();
 const submit=h.component('QuestionComposer').props.onSubmit;submit();submit();h.render();
 const posts=calls.filter(call=>call.method==='POST');assert.equal(posts.length,1);assert.equal(JSON.parse(posts[0].body).question,'原本要发送的问题');
 if(editWhilePending){h.component('QuestionComposer').props.setDraft('等待时继续编辑的新草稿');h.render();}
 pending.resolve({id:'new-answer',project_id:'p',status:'queued',question:'原本要发送的问题',source_ids:['A'],sources:[],context_revision:'v1',turn_index:1,result:null});
 await h.settle();composer=h.component('QuestionComposer');assert.equal(composer.props.answer.id,'new-answer');
 assert.equal(composer.props.draft,editWhilePending?'等待时继续编辑的新草稿':'');
 if(editWhilePending)assert.equal(tab.getItem('zhiheng-draft:p:new'),'等待时继续编辑的新草稿','receiving the old POST must not erase the newer tab draft');
 else assert.equal(tab.getItem('zhiheng-draft:p:new'),null);
 assert.equal(calls.filter(call=>call.method==='POST').length,1);h.unmount();
});

test('configured navigation shortcuts execute actual actions while typing and overlays remain protected',async()=>{
 const mod={exports:{}};new Function('require','module','exports',ts.transpileModule(fs.readFileSync(path.join(src,'desktopPreferences.ts'),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText)(()=>({preferenceStorage:{}}),mod,mod.exports);
 let prefs={...mod.exports.defaultDesktopPreferences(),shortcuts:{search:'Mod+K',sources:'Mod+Alt+S',history:'Mod+Alt+H',import:'Mod+Alt+E',newAnalysis:'Mod+Alt+N'}};
 const h=harness('ResearchApp.tsx','default',{}, {modules:{'./browserStorage':{tabStorage:storage(),preferenceStorage:storage()},'./desktopPreferences':{...mod.exports,readDesktopPreferences:()=>prefs}},api:async(url)=>{
  if(url==='/projects')return [{id:'p',name:'项目'}];if(url==='/model')return {configured:true,available_models:[],models:{}};
  if(url==='/projects/p/sources?archived=false')return [source('A','资料')];if(url==='/projects/p/favorites')return [];
  if(url.startsWith('/projects/p/workspace/'))return {revision:0,state:{source_ids:['A']}};
  if(url.startsWith('/projects/p/answer-index'))return {items:[],next_before:null};throw Error(url);
 }});await h.settle();
 assert.equal(h.globalKey('s',{ctrlKey:true,altKey:true},true),false);assert.equal(h.globalKey('s',{ctrlKey:true,altKey:true},false,true),false);
 assert.equal(h.globalKey('s',{ctrlKey:true,altKey:true,isComposing:true}),false);
 assert.equal(h.globalKey('s',{ctrlKey:true,altKey:true}),true);assert.ok(h.node(n=>n.props?.className==='library'));
 assert.equal(h.globalKey('h',{ctrlKey:true,altKey:true}),true);assert.ok(h.node(n=>n.props?.className==='history-page'));
 assert.equal(h.globalKey('n',{ctrlKey:true,altKey:true}),true);assert.ok(h.component('QuestionComposer'));
 assert.equal(h.globalKey('e',{ctrlKey:true,altKey:true}),true);const drawer=h.node(n=>n.props?.projectId==='p'&&typeof n.props.onReuse==='function');assert.ok(drawer);drawer.props.onClose();h.render();
 prefs={...prefs,shortcutsEnabled:false};assert.equal(h.globalKey('s',{ctrlKey:true,altKey:true}),false);h.unmount();
});
