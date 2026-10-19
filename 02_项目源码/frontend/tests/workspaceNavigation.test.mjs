import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
const source=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src/workspaceNavigation.ts');
const code=ts.transpileModule(fs.readFileSync(source,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const mod={exports:{}};new Function('module','exports',code)(mod,mod.exports);
const {workspaceHash,readWorkspaceRoute,readerStateKey}=mod.exports;
const compact=value=>Object.fromEntries(Object.entries(value).filter(([,v])=>v!==undefined&&v!==false));

test('complete source deep link round-trips project, active answer and citation owning turn independently',()=>{
 const route={project:'项目甲',view:'ask',answer:'latest-answer',source:'source & = # / 中文',evidence:'E1',evidenceAnswer:'earlier-answer',page:12,settings:false};
 const encoded=workspaceHash(route);
 assert.ok(!encoded.includes('source & = # / 中文'));
 assert.deepEqual(compact(readWorkspaceRoute(encoded)),compact(route));
});
test('a new-analysis route stays distinguishable from an answer route',()=>{
 const start=readWorkspaceRoute(workspaceHash({project:'p',view:'ask'}));
 const result=readWorkspaceRoute(workspaceHash({project:'p',view:'ask',answer:'a'}));
 assert.equal(start.project,'p');assert.equal(start.view,'ask');assert.equal(start.answer,undefined);assert.equal(result.answer,'a');
});
test('settings and library routes round-trip without creating a source or answer',()=>{
 for(const route of [{project:'p',view:'history',settings:true},{project:'p',view:'sources'},{project:'p',view:'ask',settings:true}])assert.deepEqual(compact(readWorkspaceRoute(workspaceHash(route))),compact(route));
});
test('citation and reader page are not serialized without an actual source target',()=>{
 const hash=workspaceHash({project:'p',view:'ask',answer:'a',evidence:'E1',evidenceAnswer:'a',page:99});
 assert.ok(!hash.includes('evidence'));assert.ok(!hash.includes('page='));assert.equal(readWorkspaceRoute(hash).source,undefined);
});
test('hash and hash-query compatibility inputs preserve encoded professional source identifiers',()=>{
 for(const prefix of ['#','#?','#/','#/?']){const route=readWorkspaceRoute(`${prefix}project=p&view=sources&source=Album%2FInvoice.Total`);assert.equal(route.source,'Album/Invoice.Total');assert.equal(route.view,'sources');}
});
test('malformed or unsafe page numbers cannot become a reader page',()=>{
 for(const value of ['', 'NaN','Infinity','-2','0','1.5','9007199254740992','word'])assert.equal(readWorkspaceRoute(`#project=p&source=s&page=${value}`).page,undefined,value);
 assert.equal(readWorkspaceRoute('#source=s&page=50').page,50);
});
test('unknown navigation view cannot introduce an unsupported view component',()=>{
 assert.equal(readWorkspaceRoute('#view=admin').view,'ask');assert.equal(readWorkspaceRoute('').view,'ask');
});
test('reader state is isolated by project, source and owning answer even when E1 and file versions repeat',()=>{
 const evidence={id:'E1',sha256:'hash',parser_version:'v1',page:3,start:15,end:30,quote:'同一份资料中的引用'};
 const keys=[readerStateKey('p1','s1',evidence,'a1'),readerStateKey('p2','s1',evidence,'a1'),readerStateKey('p1','s2',evidence,'a1'),readerStateKey('p1','s1',evidence,'a2')];
 assert.equal(new Set(keys).size,keys.length);
});
test('different quote positions and source versions never reuse one persisted reader state',()=>{
 const evidence={id:'E1',sha256:'hash',parser_version:'v1',page:3,start:15,end:30,quote:'同一份资料中的引用',table_name:'Invoice'};
 const variants=[evidence,{...evidence,page:4},{...evidence,start:16},{...evidence,end:31},{...evidence,quote:'另一段文字'},{...evidence,table_name:'Album'},{...evidence,sha256:'new'},{...evidence,parser_version:'v2'}];
 assert.equal(new Set(variants.map(value=>readerStateKey('p','s',value,'a'))).size,variants.length);
});
test('reader state key safely handles delimiters and non-Latin identifiers',()=>{
 assert.notEqual(readerStateKey('p:s','s'),readerStateKey('p','s:s'));
 assert.equal(readerStateKey('中文项目','资料',undefined),readerStateKey('中文项目','资料',undefined));
 assert.notEqual(readerStateKey('p','s',{id:'E1',quote:'a:b'},'a'),readerStateKey('p','s',{id:'E1',quote:'a'},'a:b'));
});
