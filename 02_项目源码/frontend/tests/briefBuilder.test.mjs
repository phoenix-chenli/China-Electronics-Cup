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
function load(react=React,api=()=>{throw Error('Unexpected API call');}){
 const code=ts.transpileModule(fs.readFileSync(path.join(src,'BriefBuilder.tsx'),'utf8'),{fileName:'BriefBuilder.tsx',compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const mod={exports:{}};
 new Function('require','module','exports',code)(id=>id==='react'?react:id==='./api'?{api,messageOf:e=>e instanceof Error?e.message:String(e)}:id==='./ResearchDialog'?{ResearchDialog:({children})=>React.createElement('section',null,children)}:id.endsWith('.css')?{}:require(id),mod,mod.exports);
 return mod.exports;
}
const {canSelectForBrief,toggleBriefSelection,briefRequest,moveBriefSelection}=load();
const record=(id,status='completed',extra={})=>({id,question:`问题 ${id}`,status,created_at:'2026-10-18T00:00:00Z',turn_index:1,...extra});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function nodes(tree){if(tree==null||typeof tree!=='object')return [];if(Array.isArray(tree))return tree.flatMap(nodes);return [tree,...nodes(tree.props?.children)];}
function textOf(tree){if(tree==null||typeof tree==='boolean')return '';if(typeof tree!=='object')return String(tree);return Array.isArray(tree)?tree.map(textOf).join(''):textOf(tree.props?.children);}
function harness(records=[record('a'),record('b')],api=async()=>({id:'brief-1',title:'已保存标题',answer_ids:['a','b']})){
 const state=[],refs=[],effects=[];let s=0,r=0,e=0,tree,props={projectId:'p1',projectName:'项目甲',records,favorites:[],onOpenAnswer:id=>opened.push(id)};const calls=[],opened=[];
 const module=load({...React,
  useState:initial=>{const n=s++;if(!(n in state))state[n]=typeof initial==='function'?initial():initial;return [state[n],value=>{state[n]=typeof value==='function'?value(state[n]):value;}];},
  useRef:initial=>{const n=r++;return refs[n]??(refs[n]={current:initial});},
  useEffect:(effect,deps)=>{const n=e++,old=effects[n];if(!old||deps.some((v,i)=>!Object.is(v,old.deps[i]))){old?.cleanup?.();effects[n]={deps,effect,pending:true};}},
 },async(...args)=>{calls.push(args);return api(...args);});
 const render=(patch={})=>{props={...props,...patch};s=r=e=0;tree=module.BriefBuilder(props);for(const item of effects)if(item.pending){item.pending=false;item.cleanup=item.effect();}return tree;};
 render();render();
 return {render,get tree(){return tree;},calls,opened,
  button:text=>nodes(tree).find(n=>n.type==='button'&&textOf(n)===text),
  checkbox:id=>nodes(tree).find(n=>n.type==='input'&&n.props['aria-label']===`选择分析：问题 ${id}`),
  unmount:()=>effects.forEach(effect=>effect.cleanup?.()),
  get selected(){return state[1];},get saved(){return state[6];},get busy(){return state[4];},get error(){return state[5];},get open(){return state[2];},
 };
}
function select(h,...ids){h.button('选择分析，汇成简报').props.onClick();h.render();for(const id of ids){h.checkbox(id).props.onChange();h.render();}}
function open(h){h.button('整理并生成简报').props.onClick();h.render();}

test('selection requires completed status and refuses known stale answers',()=>{
 for(const status of ['running','queued','failed','cancelled','needs_clarification','insufficient_evidence'])assert.equal(canSelectForBrief(record('x',status)),false,status);
 assert.equal(canSelectForBrief(record('x','completed',{scope_stale:true})),false);
 assert.equal(canSelectForBrief(record('x')),true); // Current version is checked server-side at create time.
});
test('selection is unique, bounded at twenty, immutable and removable at limit',()=>{
 let selected=[];for(let i=0;i<21;i++)selected=toggleBriefSelection(selected,record(String(i)));
 assert.equal(selected.length,20);const before=structuredClone(selected);
 const removed=toggleBriefSelection(selected,record('5'));assert.equal(removed.length,19);assert.deepEqual(selected,before);
 assert.equal(toggleBriefSelection([],record('bad','failed')).length,0);
});
test('brief order can move up or down without mutating records or changing eligibility',()=>{
 const selected=[record('a'),record('b'),record('c')],before=structuredClone(selected);
 assert.deepEqual(moveBriefSelection(selected,'c',-1).map(item=>item.id),['a','c','b']);
 assert.deepEqual(moveBriefSelection(selected,'a',1).map(item=>item.id),['b','a','c']);
 assert.deepEqual(selected,before);
 for(const [id,direction] of [['a',-1],['c',1],['missing',1]])assert.equal(moveBriefSelection(selected,id,direction),selected);
 assert.deepEqual(briefRequest('按新顺序',moveBriefSelection(selected,'b',-1)).answer_ids,['b','a','c']);
});
test('request carries only title and selected IDs, never client source snapshots',()=>{
 const a=record('a','completed',{sources:['untrusted'],query:'untrusted'});
 assert.deepEqual(briefRequest('  标题  ',[a,record('b')]),{title:'标题',answer_ids:['a','b']});
 for(const selected of [[],[a,a],Array.from({length:21},(_,i)=>record(String(i))),[record('x','failed')],[record('x','completed',{scope_stale:true})]])assert.throws(()=>briefRequest('标题',selected));
 for(const title of ['', ' ', '字'.repeat(121)])assert.throws(()=>briefRequest(title,[a]));
 assert.equal(briefRequest('字'.repeat(120),[a]).title.length,120);
});
test('selection and deselection send no network calls and preserve normal record navigation',()=>{
 const h=harness([record('a'),record('bad','failed')]);
 nodes(h.tree).find(n=>n.type==='button'&&n.props.className==='history-row').props.onClick();assert.deepEqual(h.opened,['a']);
 select(h,'a');assert.equal(h.calls.length,0);assert.equal(h.checkbox('bad').props.disabled,true);
 h.checkbox('a').props.onChange();h.render();assert.equal(h.selected.length,0);assert.equal(h.calls.length,0);
});
test('twentieth selection disables only new additions, and does not nest buttons',()=>{
 const h=harness(Array.from({length:21},(_,i)=>record(String(i))));select(h,...Array.from({length:20},(_,i)=>String(i)));
 assert.equal(h.checkbox('20').props.disabled,true);assert.equal(h.checkbox('0').props.disabled,false);
 const walk=(node,inButton=false)=>{if(Array.isArray(node)){node.forEach(n=>walk(n,inButton));return;}if(!node||typeof node!=='object')return;assert.ok(!(inButton&&node.type==='button'),'Nested button');walk(node.props?.children,inButton||node.type==='button');};walk(h.tree);
});
test('search and favorite result changes retain selections; visible count is accurate',()=>{
 const h=harness();select(h,'a','b');h.render({records:[record('c')]});assert.deepEqual(h.selected.map(v=>v.id),['a','b']);
 assert.ok(textOf(h.tree).includes('已选 2 / 20 条'));open(h);
 assert.ok(textOf(h.tree).includes('问题 a'));nodes(h.tree).find(n=>n.props?.['aria-label']==='移除分析：问题 a').props.onClick();h.render();
 assert.deepEqual(h.selected.map(v=>v.id),['b']);assert.equal(h.calls.length,0);
});
test('title input and actual create callback send existing API payload in selection order',async()=>{
 const h=harness();select(h,'b','a');open(h);
 nodes(h.tree).find(n=>n.type==='input'&&!n.props.type).props.onChange({target:{value:'新研究简报'}});h.render();
 h.button('生成简报').props.onClick();await tick();h.render();
 assert.equal(h.calls.length,1);assert.equal(h.calls[0][0],'/projects/p1/briefs');assert.equal(h.calls[0][1].method,'POST');
 assert.deepEqual(JSON.parse(h.calls[0][1].body),{title:'新研究简报',answer_ids:['b','a']});
 assert.equal(h.saved.id,'brief-1');assert.ok(nodes(h.tree).find(n=>n.type==='a').props.href.includes('/projects/p1/briefs/brief-1'));
});
test('synchronous double clicks, loading close and stale event handler cannot duplicate submission',async()=>{
 const pending=deferred(),h=harness(undefined,()=>pending.promise);select(h,'a','b');open(h);const oldGenerate=h.button('生成简报').props.onClick;
 oldGenerate();oldGenerate();assert.equal(h.calls.length,1);h.render();assert.equal(h.button('正在保存…').props.disabled,true);
 assert.equal(nodes(h.tree).find(n=>n.type==='input'&&!n.props.type).props.disabled,true);
 const dialog=nodes(h.tree).find(n=>typeof n.type==='function'&&n.props.title==='汇集带来源的简报');dialog.props.onClose();assert.equal(h.open,true);
 pending.resolve({id:'done',title:'完成',answer_ids:['a','b']});await tick();oldGenerate();assert.equal(h.calls.length,1);h.render();
 assert.equal(h.button('生成简报'),undefined);
});
test('backend stale rejection remains visible, keeps selection, and is not auto retried',async()=>{
 const h=harness(undefined,async()=>{throw Error('所选回答来源或业务定义已过期，请先重新分析');});select(h,'a');open(h);h.button('生成简报').props.onClick();await tick();h.render();
 assert.equal(h.calls.length,1);assert.equal(h.saved,null);assert.deepEqual(h.selected.map(v=>v.id),['a']);
 assert.ok(textOf(h.tree).includes('所选回答来源或业务定义已过期'));assert.equal(nodes(h.tree).filter(n=>n.type==='a').length,0);assert.equal(h.busy,false);
});
test('project change resets selection, title, dialog and ignores old project response',async()=>{
 const pending=deferred(),h=harness(undefined,()=>pending.promise);select(h,'a');open(h);h.button('生成简报').props.onClick();
 h.render({projectId:'p2',projectName:'项目乙',records:[record('c')]});h.render();assert.equal(h.selected.length,0);assert.equal(h.open,false);assert.equal(h.busy,false);
 pending.resolve({id:'old-brief',title:'旧项目',answer_ids:['a']});await tick();h.render();assert.equal(h.saved,null);
 select(h,'c');open(h);assert.equal(nodes(h.tree).find(n=>n.type==='input'&&!n.props.type).props.value,'项目乙分析简报');
});
test('late old request cannot release lock for a new project request',async()=>{
 const first=deferred(),second=deferred();let call=0;const h=harness(undefined,()=>++call===1?first.promise:second.promise);
 select(h,'a');open(h);h.button('生成简报').props.onClick();
 h.render({projectId:'p2',projectName:'乙',records:[record('c')]});h.render();select(h,'c');open(h);const generate=h.button('生成简报').props.onClick;generate();
 first.resolve({id:'old',title:'旧',answer_ids:['a']});await tick();generate();assert.equal(h.calls.length,2);assert.equal(h.busy,true);
 second.resolve({id:'new',title:'新',answer_ids:['c']});await tick();assert.equal(h.saved.id,'new');assert.equal(h.busy,false);
});
test('unmounted project does not publish late success or failure',async()=>{
 for(const reject of [false,true]){const pending=deferred(),h=harness(undefined,()=>pending.promise);select(h,'a');open(h);h.button('生成简报').props.onClick();h.unmount();
  reject?pending.reject(Error('old failure')):pending.resolve({id:'old',answer_ids:['a']});await tick();assert.equal(h.saved,null);assert.equal(h.error,'');}
});
test('displayed source question and generated title are escaped',()=>{
 const h=harness([record('a','completed',{question:'<script>unsafe</script>'})]);
 const html=renderToStaticMarkup(h.tree);assert.ok(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));assert.ok(!html.includes('<script>'));
});
test('parent integration remounts by project and retains source/version entrypoints',()=>{
 const text=fs.readFileSync(path.join(src,'ResearchApp.tsx'),'utf8');const source=ts.createSourceFile('ResearchApp.tsx',text,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);let builder;
 const visit=node=>{if(ts.isJsxSelfClosingElement(node)&&node.tagName.getText(source)==='BriefBuilder')builder=node;ts.forEachChild(node,visit);};visit(source);assert.ok(builder);
 const props=Object.fromEntries(builder.attributes.properties.filter(ts.isJsxAttribute).map(a=>[a.name.getText(source),a.initializer?.getText(source)]));
 assert.equal(props.key,'{pid}');assert.equal(props.projectId,'{pid}');assert.ok(props.records.includes('favoriteOnly'));assert.ok(props.onOpenAnswer.includes('loadChain'));
 for(const expected of ['使用当前资料','SourceDiagnosticsDialog','SourceVersionsDialog','onVersions'])assert.ok(text.includes(expected),expected);
});

test('row click in selection mode toggles locally instead of navigating away',()=>{
 const h=harness();h.button('选择分析，汇成简报').props.onClick();h.render();
 nodes(h.tree).find(n=>n.type==='button'&&n.props.className==='history-row').props.onClick();h.render();
 assert.deepEqual(h.selected.map(item=>item.id),['a']);assert.deepEqual(h.opened,[]);assert.equal(h.calls.length,0);
});
