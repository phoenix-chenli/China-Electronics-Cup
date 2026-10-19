/** Actual selector hooks with fake local HTTP; not browser screenshot evidence. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
import ts from 'typescript';
import React from 'react';
const require=createRequire(import.meta.url);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const nodes=t=>t==null||typeof t!=='object'?[]:Array.isArray(t)?t.flatMap(nodes):[t,...nodes(t.props?.children)];
const text=t=>t==null||typeof t==='boolean'?'':typeof t!=='object'?String(t):Array.isArray(t)?t.map(text).join(''):text(t.props?.children);
const source=(id,extra={})=>({id,name:`${id}.pdf`,format:'pdf',sha256:id,usable:true,archived:false,stage:'处理完成',summary:'10 页',warnings:[],...extra});
function harness(handler=async()=>[source('one'),source('two')],patch={}){
 const state=[],effects=[],calls=[],added=[];let s=0,e=0,tree,props={projectId:'a',projects:[{id:'a',name:'A'},{id:'b',name:'B'},{id:'c',name:'C'}],sources:[],busy:false,onReuse:async ids=>{added.push(ids);return true;},...patch};
 const react={...React,useState:init=>{const i=s++;if(!(i in state))state[i]=typeof init==='function'?init():init;return [state[i],v=>{state[i]=typeof v==='function'?v(state[i]):v;}];},useEffect:(fn,deps)=>{const i=e++,old=effects[i];if(!old||deps.some((v,n)=>!Object.is(v,old.deps[n]))){old?.cleanup?.();effects[i]={fn,deps,pending:true};}}};
 const code=ts.transpileModule(fs.readFileSync(new URL('../src/WorkspacePanels.tsx',import.meta.url),'utf8')+'\nexport {ExistingSources};',{fileName:'WorkspacePanels.tsx',compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const mod={exports:{}};new Function('require','module','exports',code)(id=>id==='react'?react:id==='./api'?{api:async(...args)=>{calls.push(args);return handler(...args);},isAbort:err=>err?.name==='AbortError',messageOf:err=>err.message}:id.startsWith('./')?{}:require(id),mod,mod.exports);
 const render=(patch={})=>{props={...props,...patch};s=e=0;tree=mod.exports.ExistingSources(props);for(const effect of effects)if(effect.pending){effect.pending=false;effect.cleanup=effect.fn();}return tree;};render();render();
 return {render,calls,added,get tree(){return tree;},checkboxes:()=>nodes(tree).filter(n=>n.type==='input'&&n.props.type==='checkbox'),select:()=>nodes(tree).find(n=>n.type==='select'),button:label=>nodes(tree).find(n=>n.type==='button'&&text(n)===label)};
}
test('opens another project and excludes current project from choices',async()=>{const h=harness();await tick();h.render();assert.equal(h.calls[0][0],'/projects/b/sources');assert.deepEqual(nodes(h.select()).filter(n=>n.type==='option').map(n=>n.props.value),['b','c']);assert.equal(h.button('添加到当前项目').props.disabled,true);});
test('same hash and processing sources cannot be selected',async()=>{const h=harness(async()=>[source('renamed',{sha256:'same'}),source('processing',{usable:false,stage:'读取 PDF 页面'}),source('good')],{sources:[source('already',{sha256:'same'})]});await tick();h.render();assert.deepEqual(h.checkboxes().map(n=>!!n.props.disabled),[true,true,false]);assert.ok(text(h.tree).includes('当前项目已有'));});
test('selects an existing file and preserves selection on failure',async()=>{const h=harness(undefined,{onReuse:async()=>false});await tick();h.render();h.checkboxes()[0].props.onChange({target:{checked:true}});h.render();await h.button('添加到当前项目').props.onClick();h.render();assert.equal(h.checkboxes()[0].props.checked,true);assert.ok(text(h.tree).includes('添加未完成'));});
test('successful addition clears selection and target hash becomes unavailable',async()=>{const h=harness();await tick();h.render();h.checkboxes()[0].props.onChange({target:{checked:true}});h.render();await h.button('添加到当前项目').props.onClick();h.render({sources:[source('new-target',{sha256:'one'})]});assert.deepEqual(h.added,[['one']]);assert.equal(h.checkboxes()[0].props.disabled,true);assert.equal(h.checkboxes()[0].props.checked,false);});
test('late response from previous project cannot replace current list',async()=>{let resolve;const old=new Promise(r=>{resolve=r;});const h=harness(url=>url.includes('/b/')?old:Promise.resolve([source('current')]));h.select().props.onChange({target:{value:'c'}});h.render();await tick();h.render();resolve([source('old')]);await tick();h.render();assert.ok(text(h.tree).includes('current.pdf'));assert.ok(!text(h.tree).includes('old.pdf'));});
test('failed reads can refresh and recover without reopening drawer',async()=>{let attempts=0;const h=harness(async()=>{if(!attempts++)throw Error('连接中断');return [source('recovered')];});await tick();h.render();assert.ok(text(h.tree).includes('连接中断'));h.button('刷新资料列表').props.onClick();h.render();await tick();h.render();assert.ok(text(h.tree).includes('recovered.pdf'));assert.ok(!text(h.tree).includes('连接中断'));});
test('only one project shows a useful empty state without network requests',async()=>{const h=harness(undefined,{projects:[{id:'a',name:'A'}]});await tick();h.render();assert.equal(h.calls.length,0);assert.ok(text(h.tree).includes('还没有其他项目'));});
