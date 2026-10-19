import test from 'node:test';
import assert from 'node:assert/strict';
import {contextInput,parseContextInput,structuredConditions} from '../src/contextEditing.ts';
import {formatContextValue} from '../src/researchTypes.ts';
const original=[{table:'staff',column:'id',value:2,operator:'='},{table:'staff',column:'region',value:'西店'}];
test('structured context uses readable labels and preserves numeric values, field identity and extra metadata',()=>{
 assert.equal(formatContextValue(original),'staff.id = 2；staff.region = 西店');assert.equal(structuredConditions(original).length,2);
 const next=original.map(item=>({...item,value:item.column==='id'?'3':'东店'}));const parsed=parseContextInput('entities',contextInput(next),original);
 assert.equal(parsed[0].value,3);assert.equal(parsed[0].operator,'=');assert.equal(parsed[1].value,'东店');assert.equal(original[0].value,2);
});
test('malformed structures and nonnumeric identifiers cannot become silently rewritten context',()=>{
 assert.throws(()=>parseContextInput('entities','[{bad',original),/结构化/);
 assert.throws(()=>parseContextInput('entities',JSON.stringify([{...original[0],value:'三'},original[1]]),original),/有效数值/);
 assert.throws(()=>parseContextInput('entities',JSON.stringify([{...original[0],column:'salary'},original[1]]),original),/所属表和字段/);
 assert.throws(()=>parseContextInput('filters','[{bad',[{region:'东店'}]),/JSON/);
 assert.deepEqual(parseContextInput('assumptions','未知不补零；按自然月',undefined),['未知不补零','按自然月']);
});
