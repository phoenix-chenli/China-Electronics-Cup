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
 const mod={exports:{}};
 new Function('require','module','exports',...Object.keys(environment),code)(id=>{
  if(id==='react')return react;
  if(id==='./api')return {api,messageOf:error=>error.message,isAbort:error=>error.name==='AbortError'};
  if(id==='./sourceDisplay')return {sourceExtent:()=> '1 页',sourceSize:()=> '30 B'};
  if(id in modules)return modules[id];
  if(id==='./workspaceNavigation')return navigation.exports;
  if(id.startsWith('./')&&id!=='./researchTypes')return children;
  if(id==='./researchTypes')return {terminal:a=>!['queued','running'].includes(a.status),formatValue:String,formatContextValue:String,slotLabels:{}};
  if(id.endsWith('.css'))return {};
  return require(id);
 },mod,mod.exports,...Object.values(environment));
 const component=mod.exports[exportName];
 const render=(patch={})=>{props={...props,...patch};dirty=true;let passes=0;while(dirty){assert.ok(++passes<30,'component effects must settle');dirty=false;s=r=e=c=0;tree=component(props);for(const item of effects)if(item.pending){item.pending=false;item.cleanup=item.effect();}}return tree;};
 render();
 return {render,async settle(){for(let i=0;i<4;i++){await tick();render();}},
  async timers(){const pending=[...timers.values()];timers.clear();pending.forEach(callback=>callback());await tick();render();},
  get tree(){return tree;},node:predicate=>nodes(tree).find(predicate),
  button:label=>nodes(tree).find(n=>n.type==='button'&&textOf(n)===label),
  input:label=>nodes(tree).find(n=>n.type==='input'&&n.props['aria-label']===label),
  component:name=>nodes(tree).find(n=>n.type===children[name]),
  key(key,target=new InputTarget(),extra={}){let prevented=false;nodes(tree).find(n=>n.props?.className==='wb-command').props.onKeyDown({key,target,nativeEvent:{isComposing:false},preventDefault:()=>{prevented=true;},...extra});render();return prevented;},
  unmount(){effects.forEach(item=>item.cleanup?.());timers.clear();},
 };
}


const doc=(id='a')=>({...source(id,id+'.txt'),kind:'document',format:'txt',sha256:'a'.repeat(64),parser_version:'test',content_revision:'b'.repeat(64),page_count:1});
const done=(id='job')=>({id,status:'completed',stage:'检索完成',question:'pump',completed_sources:1,total_sources:1,error:null,result:{results:[{source_id:'a',source_name:'a.txt',source_sha256:'a'.repeat(64),source_revision:'b'.repeat(64),parser_version:'test',chunk_id:'p1:0-10',page:1,page_unit:'logical_section',start:0,end:10,quote:'pump value',heading_path:[]}],audit:{mode_used:'lexical',fallback_reason:'vectors_unavailable',library_complete:true,source_count:1,indexed_chunks:1,source_coverage:[]}}});
function libraryHarness(){const calls=[],reads=[],asks=[];const h=harness('LibrarySearch.tsx','LibrarySearch',{projectId:'p',sources:[doc()],open:true,onClose:()=>{},onRead:e=>reads.push(e),onAsk:(sourceId,question)=>asks.push({sourceId,question})},{api:(url,options)=>{const pending=deferred();calls.push({url,options,...pending});return pending.promise;}});h.button('选择筛选结果').props.onClick();h.render();h.node(n=>n.props?.id==='library-search-query').props.onChange({target:{value:'pump'}});h.render();return {h,calls,reads,asks,submit:()=>h.node(n=>n.type==='form').props.onSubmit({preventDefault(){}})};}

test('library submit synchronously blocks duplicate Enter and sends only selected revision',async()=>{
 const {h,calls,submit}=libraryHarness();submit();submit();assert.equal(calls.length,1);const payload=JSON.parse(calls[0].options.body);assert.equal(payload.sources.length,1);assert.equal(payload.sources[0].revision,'b'.repeat(64));calls[0].resolve(done());await h.settle();assert.ok(textOf(h.tree).includes('BM25 词法检索'));assert.ok(textOf(h.tree).includes('pump value'));h.unmount();
});
test('library clears completed candidates when the source revision changes',async()=>{
 const {h,calls,submit}=libraryHarness();submit();calls[0].resolve(done());await h.settle();assert.ok(textOf(h.tree).includes('pump value'));h.render({sources:[{...doc(),content_revision:'c'.repeat(64)}]});assert.ok(!textOf(h.tree).includes('pump value'));assert.ok(textOf(h.tree).includes('版本已改变'));h.unmount();
});
test('library reader cannot open a late response after component unmount',async()=>{
 const {h,calls,reads,submit}=libraryHarness();submit();calls[0].resolve(done());await h.settle();h.button('打开原文').props.onClick();assert.equal(calls.length,2);h.unmount();calls[1].resolve(done());await tick();assert.deepEqual(reads,[]);
});
test('library reader refuses a result invalidated by backend and composition Enter is inert',async()=>{
 const {h,calls,reads,submit}=libraryHarness();let prevented=false;h.node(n=>n.props?.id==='library-search-query').props.onKeyDown({key:'Enter',nativeEvent:{isComposing:true},preventDefault(){prevented=true;}});assert.equal(prevented,true);assert.equal(calls.length,0);submit();calls[0].resolve(done());await h.settle();h.button('打开原文').props.onClick();calls[1].resolve({...done(),status:'failed',result:null,error:'资料已变化'});await h.settle();assert.deepEqual(reads,[]);assert.ok(!textOf(h.tree).includes('pump value'));h.unmount();
});


test('late library submission is cancelled after the panel unmounts',async()=>{
 const {h,calls,submit}=libraryHarness();submit();h.unmount();calls[0].resolve({...done(),status:'running',result:null});await tick();assert.equal(calls.length,2);assert.equal(calls[1].url,'/projects/p/library-search/job/cancel');calls[1].resolve({...done(),status:'cancelled',result:null});
});
test('late cancel response cannot replace a newer library search',async()=>{
 const {h,calls,submit}=libraryHarness();submit();calls[0].resolve({...done('old'),status:'running',result:null});await h.settle();h.button('取消检索').props.onClick();assert.equal(calls[1].url,'/projects/p/library-search/old/cancel');await h.timers();calls[2].resolve({...done('old'),status:'cancelled',result:null});await h.settle();submit();calls[3].resolve(done('new'));await h.settle();calls[1].resolve({...done('old'),status:'cancelled',result:null});await h.settle();assert.ok(textOf(h.tree).includes('pump value'));assert.ok(!textOf(h.tree).includes('检索已取消'));h.unmount();
});


test('library question action revalidates source and transfers only source id and original search question',async()=>{
 const {h,calls,asks,submit}=libraryHarness();submit();calls[0].resolve(done());await h.settle();h.button('基于此资料提问').props.onClick();assert.equal(asks.length,0);assert.equal(calls.length,2);calls[1].resolve(done());await h.settle();assert.deepEqual(asks,[{sourceId:'a',question:'pump'}]);assert.equal(calls.filter(c=>c.options?.method==='POST').length,1);h.unmount();
});
test('library question cannot use a retrieved hit after current source revision changes',async()=>{
 const {h,calls,asks,submit}=libraryHarness();submit();calls[0].resolve(done());await h.settle();h.button('基于此资料提问').props.onClick();h.render({sources:[{...doc(),content_revision:'c'.repeat(64)}]});calls[1].resolve(done());await h.settle();assert.deepEqual(asks,[]);h.unmount();
});
