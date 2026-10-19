import test from 'node:test';
import assert from 'node:assert/strict';
import {createSuggestionState,reduceSuggestion,suggestionVisible,suggestionContextKey} from '../src/suggestionKeyboard.ts';
const context={answerId:'A',branchId:'B',contextRevision:3,sourceRevisions:{S:'hash-parser-revision'}};
const suggestion={id:'N1',text:'继续核对同一年各地区收入的统计口径？',context};
function ready(){let s=createSuggestionState(context); for(const e of [{type:'focus',focused:true},{type:'prepare',requestId:'R1'},{type:'receive',suggestion,requestId:'R1'}])s=reduceSuggestion(s,e).state;return s;}
const down=(code='ShiftLeft',extra={})=>({type:'keydown',key:'Shift',code,...extra});
const up=(code='ShiftLeft',extra={})=>({type:'keyup',key:'Shift',code,...extra});
function sequence(events,state=ready()){let effects=[];for(const e of events){const r=reduceSuggestion(state,e);state=r.state;if(r.effect)effects.push(r.effect);}return {state,effects};}
for(const side of ['ShiftLeft','ShiftRight'])test(`retired ${side} never fills`,()=>{const r=sequence([down(side),up(side)]);assert.equal(r.effects.length,0);assert.equal(r.state.draft,'');assert.ok(suggestionVisible(r.state));});
for(const key of ['Enter','Tab','ArrowLeft','ArrowRight','a','Control','Alt','Meta'])test(`keyboard ${key} never accepts`,()=>assert.equal(sequence([down(),{type:'keydown',key,shiftKey:true},up()]).effects.length,0));
test('explicit acceptance fills at end without submitting',()=>{const r=sequence([{type:'accept'}]);assert.equal(r.effects.length,1);assert.equal(r.effects[0].text,suggestion.text);assert.equal(r.effects[0].selectionStart,suggestion.text.length);assert.ok(!('submit' in r.effects[0]));});
test('composition blocks explicit acceptance',()=>assert.equal(sequence([{type:'composition',active:true},{type:'accept'}]).effects.length,0));
test('display off blocks explicit acceptance',()=>assert.equal(sequence([{type:'enabled',enabled:false},{type:'accept'}]).effects.length,0));
test('escape dismisses and delayed response cannot revive',()=>{const r=sequence([{type:'keydown',key:'Escape'},{type:'receive',suggestion,requestId:'R1'},down(),up()]);assert.equal(r.state.phase,'dismissed');assert.equal(r.effects.length,0);});
for(const key of ['answerId','branchId','contextRevision','sourceRevisions'])test(`context changes ${key} invalidate late response`,()=>{const changed={...context,[key]:key==='sourceRevisions'?{S:'new'}:'different'};const r=sequence([down(),{type:'context',context:changed},{type:'receive',suggestion,requestId:'R1'},up()]);assert.equal(r.effects.length,0);assert.equal(r.state.phase,'hidden');});
test('same context source map order is stable',()=>assert.equal(suggestionContextKey({...context,sourceRevisions:{A:'1',B:'2'}}),suggestionContextKey({...context,sourceRevisions:{B:'2',A:'1'}})));
test('replacing suggestion while armed requires new press',()=>assert.equal(sequence([down(),{type:'receive',requestId:'R1',suggestion:{...suggestion,id:'N2'}},up()]).effects.length,0));
test('single acceptance cannot repeat',()=>assert.equal(sequence([{type:'accept'},down(),up(),{type:'receive',suggestion,requestId:'R1'}]).effects.length,1));
test('empty and excessive suggestions do not become ready',()=>{for(const text of ['',' ','x'.repeat(1001)])assert.equal(sequence([{type:'receive',requestId:'R1',suggestion:{...suggestion,text}}],createSuggestionState(context)).state.phase,'hidden');});
test('undo only reverses own unchanged insertion',()=>{const r=sequence([{type:'accept'},{type:'undo'}]);assert.deepEqual(r.effects.map(e=>e.type),['fill','clear']);assert.equal(r.state.draft,'');assert.equal(r.state.phase,'dismissed');});
test('edited accepted text cannot be cleared by helper undo',()=>{const r=sequence([{type:'accept'},{type:'input',value:'用户修改'},{type:'undo'}]);assert.equal(r.effects.length,1);assert.equal(r.state.draft,'用户修改');});
test('all transitions preserve input objects',()=>{const state=ready(),snapshot=structuredClone(state);sequence([down(),up()],state);assert.deepEqual(state,snapshot);assert.equal(suggestionVisible(state),true);});
test('suggestions off hides both shortcut and explicit acceptance until reenabled',()=>{const s=sequence([{type:'enabled',enabled:false}]).state;assert.equal(suggestionVisible(s),false);assert.equal(sequence([down(),up()],s).effects.length,0);assert.equal(sequence([{type:'accept'}],s).effects.length,0);assert.equal(sequence([{type:'enabled',enabled:true},{type:'accept'}],s).effects.length,1);});
for(const terminal of ['dismissed','accepted'])test(`reenabling cannot revive ${terminal} suggestion`,()=>{const s=sequence([{type:terminal==='dismissed'?'dismiss':'accept'},{type:'enabled',enabled:false},{type:'enabled',enabled:true}]).state;assert.equal(suggestionVisible(s),false);assert.equal(sequence([{type:'accept'},down(),up()],s).effects.length,0);});
test('unknown shift code is ignored',()=>assert.equal(sequence([down(''),up('')]).effects.length,0));
test('ordinary Enter and Ctrl Enter are not submitted by suggestion reducer',()=>{const r=sequence([{type:'keydown',key:'Enter'},{type:'keydown',key:'Enter',ctrlKey:true}]);assert.equal(r.effects.length,0);});

test('duplicate nonrepeat keydown cancels',()=>assert.equal(sequence([down(),down(),up()]).effects.length,0));
test('shift still held at release rejects unobserved second shift',()=>assert.equal(sequence([down(),up('ShiftLeft',{shiftKey:true})]).effects.length,0));
test('hidden cancelled request cannot revive on late response',()=>{const r=sequence([{type:'hide'},{type:'receive',suggestion,requestId:'R1'}]);assert.equal(r.state.phase,'hidden');});
test('older same-context request cannot replace newer suggestion',()=>{const r=sequence([{type:'prepare',requestId:'R2'},{type:'receive',suggestion,requestId:'R1'}]);assert.equal(r.state.phase,'preparing');assert.equal(r.state.requestId,'R2');});
test('normal input echo retains undo of accepted insertion',()=>{const r=sequence([{type:'accept'},{type:'input',value:suggestion.text},{type:'undo'}]);assert.equal(r.state.draft,'');assert.equal(r.effects.length,2);});

