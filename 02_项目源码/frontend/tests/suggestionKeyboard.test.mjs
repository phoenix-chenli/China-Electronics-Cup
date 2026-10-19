import test from 'node:test';
import assert from 'node:assert/strict';
import {createSuggestionState,reduceSuggestion,suggestionVisible,suggestionContextKey} from '../src/suggestionKeyboard.ts';
const context={answerId:'A',branchId:'B',contextRevision:3,sourceRevisions:{S:'hash-parser-revision'}};
const suggestion={id:'N1',text:'继续核对同一年各地区收入的统计口径？',context};
function ready(){let s=createSuggestionState(context); for(const e of [{type:'focus',focused:true},{type:'prepare',requestId:'R1'},{type:'receive',suggestion,requestId:'R1'}])s=reduceSuggestion(s,e).state;return s;}
const down=(code='ShiftLeft',extra={})=>({type:'keydown',key:'Shift',code,...extra});
const up=(code='ShiftLeft',extra={})=>({type:'keyup',key:'Shift',code,...extra});
function sequence(events,state=ready()){let effects=[];for(const e of events){const r=reduceSuggestion(state,e);state=r.state;if(r.effect)effects.push(r.effect);}return {state,effects};}
for(const side of ['ShiftLeft','ShiftRight'])test(`single ${side} fills at end without submit`,()=>{const r=sequence([down(side),up(side)]);assert.equal(r.effects.length,1);assert.equal(r.effects[0].text,suggestion.text);assert.equal(r.effects[0].selectionStart,suggestion.text.length);assert.equal(r.state.phase,'accepted');assert.ok(!('submit' in r.effects[0]));});
for(const key of ['Enter','Tab','ArrowLeft','ArrowRight','a','Control','Alt','Meta'])test(`Shift+${key} never accepts`,()=>assert.equal(sequence([down(),{type:'keydown',key,shiftKey:true},up()]).effects.length,0));
for(const modifier of ['ctrlKey','altKey','metaKey'])test(`${modifier} held before shift`,()=>assert.equal(sequence([down('ShiftLeft',{[modifier]:true}),up()]).effects.length,0));
for(const modifier of ['ctrlKey','altKey','metaKey'])test(`${modifier} held at release`,()=>assert.equal(sequence([down(),up('ShiftLeft',{[modifier]:true})]).effects.length,0));
for(const event of [{type:'pointer'},{type:'selection'},{type:'focus',focused:false},{type:'visibility',visible:false},{type:'composition',active:true},{type:'input',value:'新草稿'},{type:'enabled',enabled:false},{type:'dismiss'},{type:'hide'},{type:'prepare',requestId:'R2'}])test(`armed cancellation ${JSON.stringify(event)}`,()=>assert.equal(sequence([down(),event,up()]).effects.length,0));
test('repeat/long keydown cancels until release',()=>assert.equal(sequence([down(),down('ShiftLeft',{repeat:true}),up()]).effects.length,0));
test('keyup alone never accepts',()=>assert.equal(sequence([up()]).effects.length,0));
test('wrong shift release does not accept',()=>assert.equal(sequence([down(),up('ShiftRight'),up()]).effects.length,0));
test('two shifts block both releases',()=>assert.equal(sequence([down(),down('ShiftRight'),up(),up('ShiftRight')]).effects.length,0));
test('after both shifts release a new solitary press works',()=>assert.equal(sequence([down(),down('ShiftRight'),up(),up('ShiftRight'),down(),up()]).effects.length,1));
test('existing draft including whitespace is never overwritten',()=>{for(const value of ['草稿',' ']){const r=sequence([{type:'input',value},down(),up(),{type:'accept'}]);assert.equal(r.effects.length,0);assert.equal(r.state.draft,value);}});
test('nonfocused keyboard ignored; button works on mobile',()=>{const s=sequence([{type:'focus',focused:false}]).state;assert.equal(sequence([down(),up()],s).effects.length,0);assert.equal(sequence([{type:'accept'}],s).effects.length,1);});
test('composition at down or up is rejected',()=>{assert.equal(sequence([down('ShiftLeft',{isComposing:true}),up()]).effects.length,0);assert.equal(sequence([down(),up('ShiftLeft',{isComposing:true})]).effects.length,0);});
test('composition end cannot revive armed shift',()=>assert.equal(sequence([down(),{type:'composition',active:true},{type:'composition',active:false},up()]).effects.length,0));
test('escape dismisses and delayed response cannot revive',()=>{const r=sequence([{type:'keydown',key:'Escape'},{type:'receive',suggestion,requestId:'R1'},down(),up()]);assert.equal(r.state.phase,'dismissed');assert.equal(r.effects.length,0);});
for(const key of ['answerId','branchId','contextRevision','sourceRevisions'])test(`context changes ${key} invalidate late response`,()=>{const changed={...context,[key]:key==='sourceRevisions'?{S:'new'}:'different'};const r=sequence([down(),{type:'context',context:changed},{type:'receive',suggestion,requestId:'R1'},up()]);assert.equal(r.effects.length,0);assert.equal(r.state.phase,'hidden');});
test('same context source map order is stable',()=>assert.equal(suggestionContextKey({...context,sourceRevisions:{A:'1',B:'2'}}),suggestionContextKey({...context,sourceRevisions:{B:'2',A:'1'}})));
test('replacing suggestion while armed requires new press',()=>assert.equal(sequence([down(),{type:'receive',requestId:'R1',suggestion:{...suggestion,id:'N2'}},up()]).effects.length,0));
test('single acceptance cannot repeat',()=>assert.equal(sequence([down(),up(),down(),up(),{type:'receive',suggestion,requestId:'R1'}]).effects.length,1));
test('empty and excessive suggestions do not become ready',()=>{for(const text of ['',' ','x'.repeat(1001)])assert.equal(sequence([{type:'receive',requestId:'R1',suggestion:{...suggestion,text}}],createSuggestionState(context)).state.phase,'hidden');});
test('undo only reverses own unchanged insertion',()=>{const r=sequence([down(),up(),{type:'undo'}]);assert.deepEqual(r.effects.map(e=>e.type),['fill','clear']);assert.equal(r.state.draft,'');assert.equal(r.state.phase,'dismissed');});
test('edited accepted text cannot be cleared by helper undo',()=>{const r=sequence([down(),up(),{type:'input',value:'用户修改'},{type:'undo'}]);assert.equal(r.effects.length,1);assert.equal(r.state.draft,'用户修改');});
test('all transitions preserve input objects',()=>{const state=ready(),snapshot=structuredClone(state);sequence([down(),up()],state);assert.deepEqual(state,snapshot);assert.equal(suggestionVisible(state),true);});
test('suggestions off hides both shortcut and explicit acceptance until reenabled',()=>{const s=sequence([{type:'enabled',enabled:false}]).state;assert.equal(suggestionVisible(s),false);assert.equal(sequence([down(),up()],s).effects.length,0);assert.equal(sequence([{type:'accept'}],s).effects.length,0);assert.equal(sequence([{type:'enabled',enabled:true},{type:'accept'}],s).effects.length,1);});
for(const terminal of ['dismissed','accepted'])test(`reenabling cannot revive ${terminal} suggestion`,()=>{const s=sequence([{type:terminal==='dismissed'?'dismiss':'accept'},{type:'enabled',enabled:false},{type:'enabled',enabled:true}]).state;assert.equal(suggestionVisible(s),false);assert.equal(sequence([{type:'accept'},down(),up()],s).effects.length,0);});
test('unknown shift code is ignored',()=>assert.equal(sequence([down(''),up('')]).effects.length,0));
test('ordinary Enter and Ctrl Enter are not submitted by suggestion reducer',()=>{const r=sequence([{type:'keydown',key:'Enter'},{type:'keydown',key:'Enter',ctrlKey:true}]);assert.equal(r.effects.length,0);});

test('duplicate nonrepeat keydown cancels',()=>assert.equal(sequence([down(),down(),up()]).effects.length,0));
test('shift still held at release rejects unobserved second shift',()=>assert.equal(sequence([down(),up('ShiftLeft',{shiftKey:true})]).effects.length,0));
test('hidden cancelled request cannot revive on late response',()=>{const r=sequence([{type:'hide'},{type:'receive',suggestion,requestId:'R1'}]);assert.equal(r.state.phase,'hidden');});
test('older same-context request cannot replace newer suggestion',()=>{const r=sequence([{type:'prepare',requestId:'R2'},{type:'receive',suggestion,requestId:'R1'}]);assert.equal(r.state.phase,'preparing');assert.equal(r.state.requestId,'R2');});
test('normal input echo retains undo of accepted insertion',()=>{const r=sequence([down(),up(),{type:'input',value:suggestion.text},{type:'undo'}]);assert.equal(r.state.draft,'');assert.equal(r.effects.length,2);});

test('keyboard-only disabled keeps visible suggestion and explicit acceptance',()=>{
 const s=sequence([{type:'keyboard-enabled',enabled:false}]).state;
 assert.equal(suggestionVisible(s),true);assert.equal(sequence([down(),up()],s).effects.length,0);
 const accepted=sequence([{type:'accept'}],s);assert.equal(accepted.effects[0].trigger,'explicit');assert.equal(accepted.effects[0].text,suggestion.text);
});
test('changing keyboard preference cancels the armed press even if immediately reenabled',()=>{
 const r=sequence([down(),{type:'keyboard-enabled',enabled:false},{type:'keyboard-enabled',enabled:true},up()]);
 assert.equal(r.effects.length,0);assert.equal(r.state.armed,null);assert.equal(sequence([down(),up()],r.state).effects[0].trigger,'keyboard');
});
test('local keyboard preference survives changing answer and revision',()=>{
 const other={...context,answerId:'other',contextRevision:99};
 const s=sequence([{type:'keyboard-enabled',enabled:false},{type:'context',context:other}]).state;
 assert.equal(s.keyboardEnabled,false);assert.equal(s.enabled,true);
});
test('global off still hides and prevents clicking despite keyboard preference on',()=>{
 const r=sequence([{type:'enabled',enabled:false},{type:'keyboard-enabled',enabled:true},{type:'accept'},down(),up()]);
 assert.equal(r.effects.length,0);assert.equal(suggestionVisible(r.state),false);
});
test('turning shortcut back on during composition or a chord does not arm it',()=>{
 for(const guard of [{type:'composition',active:true},{type:'keydown',key:'Control',ctrlKey:true}]){
  const r=sequence([down(),{type:'keyboard-enabled',enabled:false},guard,{type:'keyboard-enabled',enabled:true},up()]);
  assert.equal(r.effects.length,0);
 }
});
