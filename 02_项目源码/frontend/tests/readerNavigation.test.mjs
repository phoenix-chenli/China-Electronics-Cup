/** Real reader handlers with local hooks/HTTP fakes; browser layout is checked separately. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
import React from 'react';

const require=createRequire(import.meta.url),src=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src');
const nodes=tree=>tree==null||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const detail={source:{id:'doc',name:'离线分页资料.pdf',format:'pdf',sha256:'sha',parser_version:'v1'},pages:[1,2,3].map(number=>({number,text:`第${number}页`,extraction:'native'})),tables:[],metadata:{}};
function harness(initialProps={}){
 const state=[],refs=[],effects=[],memos=[];let s=0,r=0,e=0,m=0,dirty=true,tree,key,props,requests=0;
 const body={scrollTop:0},positions=[];
 const changed=(old,deps)=>!old||deps.some((value,i)=>!Object.is(value,old.deps[i]));
 const react={...React,
  useState:initial=>{const i=s++;if(!(i in state))state[i]=typeof initial==='function'?initial():initial;return [state[i],value=>{const next=typeof value==='function'?value(state[i]):value;if(!Object.is(next,state[i])){state[i]=next;dirty=true;}}];},
  useRef:initial=>{const i=r++;return refs[i]??(refs[i]={current:initial});},
  useEffect:(effect,deps)=>{const i=e++;if(changed(effects[i],deps)){effects[i]?.cleanup?.();effects[i]={effect,deps,pending:true};}},
  useMemo:(factory,deps)=>{const i=m++;if(changed(memos[i],deps))memos[i]={value:factory(),deps};return memos[i].value;},useId:()=> 'reader-test',
 };
 const environment={window:{matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}})},document:{activeElement:null},HTMLElement:class {},requestAnimationFrame:callback=>{callback();return 1;},cancelAnimationFrame:()=>{}};
 const cache=new Map();
 const load=file=>{
  if(cache.has(file))return cache.get(file).exports;
  const mod={exports:{}};cache.set(file,mod);
  const code=ts.transpileModule(fs.readFileSync(file,'utf8'),{fileName:file,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  new Function('require','module','exports',...Object.keys(environment),code)(id=>{
   if(id==='react')return react;if(id==='./api')return {api:async()=>{requests++;return detail;},isAbort:()=>false,messageOf:e=>e.message};
   if(id.endsWith('.css'))return {};if(id.startsWith('.'))return load(path.resolve(path.dirname(file),id+'.ts'));return require(id);
  },mod,mod.exports,...Object.values(environment));return mod.exports;
 };
 const {ReadingPanel}=load(path.join(src,'ReadingPanel.tsx'));
 props={sourceId:'doc',fullPage:true,onClose:()=>{},onViewStateChange:position=>positions.push(position),...initialProps};
 const render=(patch={})=>{props={...props,...patch};dirty=true;let turns=0;while(dirty){assert.ok(++turns<20);dirty=false;s=r=e=m=0;const session=ReadingPanel(props);key=session.key;tree=session.type(session.props);const bodyNode=nodes(tree).find(node=>node.props?.className==='reading-body');if(bodyNode?.props.ref)bodyNode.props.ref.current=body;for(const effect of effects)if(effect.pending){effect.pending=false;effect.cleanup=effect.effect();}}return tree;};
 render();
 return {render,body,positions,get key(){return key;},get requests(){return requests;},get page(){return positions.at(-1)?.page;},async settle(){await tick();render();},button:label=>nodes(tree).find(node=>node.type==='button'&&node.props['aria-label']===label),unmount:()=>effects.forEach(effect=>effect.cleanup?.())};
}

test('an explicit URL page overrides saved position without applying another page scroll',async()=>{
 const h=harness({initialPage:3,initialViewState:{page:1,scrollTop:320,zoom:1.5}});await h.settle();
 assert.equal(h.page,3);assert.equal(h.body.scrollTop,0);assert.equal(h.positions.at(-1).zoom,1.5);assert.equal(h.requests,1);h.unmount();
});
test('external page changes update the existing reader session without reloading the source',async()=>{
 const h=harness({initialPage:1,initialViewState:{page:1,scrollTop:320,zoom:1.5}});await h.settle();const key=h.key;
 assert.equal(h.body.scrollTop,320);h.render({initialPage:3});assert.equal(h.page,3);assert.equal(h.body.scrollTop,0);
 assert.equal(h.key,key);assert.equal(h.requests,1);assert.equal(h.positions.at(-1).zoom,1.5);h.unmount();
});
test('echoing a locally changed page preserves its current scroll and session',async()=>{
 const h=harness({initialPage:1,initialViewState:{page:1,scrollTop:320}});await h.settle();const key=h.key;
 h.button('下一页').props.onClick();h.render();assert.equal(h.page,2);assert.equal(h.body.scrollTop,0);
 h.body.scrollTop=175;h.render({initialPage:2});assert.equal(h.body.scrollTop,175);assert.equal(h.key,key);assert.equal(h.requests,1);h.unmount();
});
test('invalid external page requests do not select nonexistent pages or reset scroll',async()=>{
 const h=harness({initialPage:1,initialViewState:{page:1,scrollTop:320}});await h.settle();h.render({initialPage:9});
 assert.equal(h.page,1);assert.equal(h.body.scrollTop,320);assert.equal(h.requests,1);h.unmount();
});
