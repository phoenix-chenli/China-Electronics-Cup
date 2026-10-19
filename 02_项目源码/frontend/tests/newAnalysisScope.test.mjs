/** Regression checks execute the real ResearchApp callbacks without browser/model requests. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
const require=createRequire(import.meta.url),file=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src/ResearchApp.tsx');
const source=ts.createSourceFile(file,fs.readFileSync(file,'utf8'),ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
function callback(name,environment){let expression;const visit=node=>{if(ts.isVariableDeclaration(node)&&node.name.getText(source)===name)expression=ts.isCallExpression(node.initializer)?node.initializer.arguments[0]:node.initializer;if(ts.isFunctionDeclaration(node)&&node.name?.text===name)expression=node;if(name==='restoreRoute'&&ts.isCallExpression(node)&&node.expression.getText(source)==='useEffect'&&node.arguments[0]?.getText(source).includes('if(loading||!pid||!hydrated||hydratedProject.current!==pid||routeReady)'))expression=node.arguments[0];ts.forEachChild(node,visit);};visit(source);assert.ok(expression,`${name} exists`);const raw=ts.isFunctionDeclaration(expression)?`${expression.getText(source)};module.exports=${name};`:`module.exports=(${expression.getText(source)});`;const code=ts.transpileModule(raw,{fileName:'callback.tsx',compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;const mod={exports:{}};new Function('require','module','exports',...Object.keys(environment),code)(require,mod,mod.exports,...Object.values(environment));return mod.exports;}
const sourceRecord=(id,project_id='p',extra={})=>({id,project_id,usable:true,archived:false,...extra});
function harness({projectId='p',storage=new Map(),sources=[sourceRecord('md'),sourceRecord('sql')]}={}){
 const tabStorage={getItem:key=>storage.get(key)??null,setItem:(key,value)=>{storage.set(key,value);return true;}};
 const availableNewAnalysisScope=callback('availableNewAnalysisScope',{});
 const helpers={availableNewAnalysisScope,tabStorage};
 const readNewAnalysisScope=callback('readNewAnalysisScope',helpers),saveNewAnalysisScope=callback('saveNewAnalysisScope',helpers);
 const state={scope:[],chain:[],draft:'',errors:[],announcements:[]},projectRef={current:projectId};
 const set=key=>value=>{state[key]=value;};
 const environment={requestAnimationFrame:fn=>fn(),document:{getElementById:()=>null},pid:projectId,sources,projectRef,tabStorage,readNewAnalysisScope,saveNewAnalysisScope,requestGeneration:{current:0},draftRevision:{current:0},setScope:set('scope'),setChain:set('chain'),setDraftUndo:set('undo'),setPatch:set('patch'),setDraft:set('draft'),setBusy:set('busy'),setReader:set('reader'),setView:set('view'),setChartId:set('chart'),setScopeOpen:set('scopeOpen'),pendingPatch:()=>[],showError:error=>state.errors.push(error),announce:text=>state.announcements.push(text),messageOf:error=>error.message,api:async url=>{const id=url.split('/').at(-2);return [{id,project_id:projectId,source_ids:[id==='sql-answer'?'sql':'md']}];}};
 environment.setNewScope=callback('setNewScope',environment);
 const invoke=(name,...args)=>callback(name,{...environment,scope:state.scope,chain:state.chain,active:state.chain.at(-1)??null})(...args);
 const restoreRoute=()=>callback('restoreRoute',{...environment,loading:false,hydrated:true,hydratedProject:{current:projectId},routeReady:false,route:{project:projectId,view:'ask'},projects:[],chain:state.chain,active:state.chain.at(-1)??null,routeApplying:{current:false},setRouteReady:set('routeReady'),setViewState:set('view'),setSettings:set('settings'),requestAnimationFrame:fn=>fn(),main:{current:null},returnScroll:{current:0},readerOrigin:{current:null}})();
 return {state,storage,projectRef,readNewAnalysisScope,saveNewAnalysisScope,invoke,restoreRoute};
}
test('back to new analysis restores its MD scope and draft after opening a SQL history answer',async()=>{
 const h=harness();h.state.scope=['md'];h.state.chain=[{id:'failed-md',project_id:'p',source_ids:['md']}];h.storage.set('zhiheng-draft:p:new','保留的新草稿');
 h.invoke('startNew');assert.deepEqual(h.state.scope,['md']);assert.equal(h.state.draft,'保留的新草稿');
 assert.equal(await h.invoke('loadChain','sql-answer'),true);assert.deepEqual(h.state.scope,['sql']);
 h.restoreRoute();assert.deepEqual(h.state.chain,[]);assert.deepEqual(h.state.scope,['md']);assert.equal(h.state.draft,'保留的新草稿');assert.equal(h.state.routeReady,true);
});
test('explicit new analysis inherits the current answer scope and updates its independent saved scope',()=>{
 const h=harness();h.saveNewAnalysisScope('p',['md'],[sourceRecord('md'),sourceRecord('sql')]);h.state.scope=['sql'];h.state.chain=[{id:'sql-answer'}];h.invoke('startNew');
 assert.deepEqual(h.state.scope,['sql']);assert.deepEqual(JSON.parse(h.storage.get('zhiheng-scope:p:new')),['sql']);assert.deepEqual(h.state.chain,[]);
});
test('manual scope adjustment starts a new analysis and persists the adjusted source list',()=>{
 const h=harness();h.state.scope=['sql'];h.state.chain=[{id:'sql-answer'}];h.invoke('applyScope',['md','md']);
 assert.deepEqual(h.state.scope,['md']);assert.deepEqual(h.state.chain,[]);assert.deepEqual(JSON.parse(h.storage.get('zhiheng-scope:p:new')),['md']);assert.equal(h.state.announcements.length,1);
});
test('confirming unchanged historical scope does not overwrite an independent new-analysis scope',()=>{
 const h=harness();h.saveNewAnalysisScope('p',['md'],[sourceRecord('md'),sourceRecord('sql')]);h.state.scope=['sql'];h.state.chain=[{id:'sql-answer'}];h.invoke('applyScope',['sql']);
 assert.deepEqual(JSON.parse(h.storage.get('zhiheng-scope:p:new')),['md']);assert.equal(h.state.chain[0].id,'sql-answer');
});
test('a single-source question overrides the inherited selection in the new-analysis cache',()=>{
 const h=harness();h.state.scope=['md'];h.invoke('startNew');h.invoke('setNewScope',['sql']);h.state.scope=['md'];h.restoreRoute();assert.deepEqual(h.state.scope,['sql']);
});
test('source limits, order, deduplication and current-project availability survive storage restore',()=>{
 const sources=Array.from({length:14},(_,i)=>sourceRecord(`s${i}`)).concat(sourceRecord('archived','p',{archived:true}),sourceRecord('failed','p',{usable:false}),sourceRecord('other','q'));
 const h=harness({sources});h.storage.set('zhiheng-scope:p:new',JSON.stringify(['archived','failed','other','missing',4,'s2','s2',...sources.map(s=>s.id)]));h.restoreRoute();
 assert.deepEqual(h.state.scope,['s2','s0','s1','s3','s4','s5','s6','s7','s8','s9','s10','s11']);
});
test('manual selection over twelve sources is rejected without changing scope or saved state',()=>{
 const sources=Array.from({length:13},(_,i)=>sourceRecord(`s${i}`)),h=harness({sources});h.state.scope=['s0'];h.invoke('applyScope',sources.map(s=>s.id));
 assert.deepEqual(h.state.scope,['s0']);assert.equal(h.storage.has('zhiheng-scope:p:new'),false);assert.match(h.state.errors[0],/最多选择 12/);
});
test('project-scoped selections survive remount and never migrate to another project',()=>{
 const storage=new Map(),p=harness({storage}),q=harness({storage,projectId:'q',sources:[sourceRecord('q-source','q')]});p.invoke('setNewScope',['md']);q.invoke('setNewScope',['q-source']);
 const remounted=harness({storage});remounted.state.scope=['sql'];remounted.restoreRoute();q.restoreRoute();assert.deepEqual(remounted.state.scope,['md']);assert.deepEqual(q.state.scope,['q-source']);
 p.projectRef.current='q';p.invoke('setNewScope',['sql']);assert.deepEqual(JSON.parse(storage.get('zhiheng-scope:p:new')),['md']);
});
test('missing or damaged cache uses only a supplied valid legacy scope; explicit empty selection stays empty',()=>{
 const h=harness(),sources=[sourceRecord('md')];assert.deepEqual(h.readNewAnalysisScope('p',sources,['md']),['md']);h.storage.set('zhiheng-scope:p:new','broken-json');assert.deepEqual(h.readNewAnalysisScope('p',sources,['md']),['md']);h.storage.set('zhiheng-scope:p:new','[]');assert.deepEqual(h.readNewAnalysisScope('p',sources,['md']),[]);
});
