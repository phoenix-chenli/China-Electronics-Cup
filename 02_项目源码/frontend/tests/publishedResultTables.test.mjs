import test from 'node:test';
import assert from 'node:assert/strict';
import {publishedTables,resultCell,compareCells,displayTable,fixedDecimal} from '../src/resultTables.ts';
import {defaultChartOptions} from '../src/resultChart.ts';
import {groupAnswerNotes} from '../src/qualityNotes.ts';
const table={id:'published',columns:['country','total'],rows:[['甲',3],['乙',2]],display_rows:[['甲','3.00'],['乙','2.00']],provenance:'saved_completed_result'};
const answer=(result={})=>({status:'completed',scope_stale:false,sources:[],result:{verification:{status:'passed'},...result}});
test('complete published table retains all rows and exact display precision without mutating raw values',()=>{
 const rows=Array.from({length:123},(_,i)=>['国家'+i,i/10]);const a=answer({result_tables:[{...table,rows,display_rows:rows.map(r=>[r[0],r[1].toFixed(2)])}]});
 const projection=publishedTables(a)[0];assert.equal(projection.rows.length,123);assert.equal(resultCell(projection.rows[10][1],projection.display_rows[10][1]),'1.00');assert.equal(a.result.result_tables[0].rows[10][1],1);
});
test('explicit published collection wins over legacy preview and rejects execution candidates',()=>{
 assert.deepEqual(publishedTables(answer({result_tables:[],columns:['legacy'],rows:[[99]]})),[]);
 assert.equal(publishedTables(answer({result_tables:[table,{...table,id:'draft',provenance:'executed_candidate'}],columns:['legacy'],rows:[[99]]})).length,1);
 for(const status of ['failed','cancelled','running','insufficient_evidence'])assert.deepEqual(publishedTables({...answer({result_tables:[table]}),status}),[]);
});
test('malformed row shape cannot be rendered and malformed display rows do not replace raw values',()=>{
 assert.deepEqual(publishedTables(answer({result_tables:[{...table,rows:[['甲']]}]})),[]);
 assert.equal(publishedTables(answer({result_tables:[{...table,display_rows:[['甲','错误']]}]}))[0].display_rows,undefined);
 assert.equal(resultCell(null,'0.00'),'NULL');assert.equal(resultCell(0,'0.00'),'0.00');
});
test('numeric sorting handles decimals and large exact text without lexicographic or floating precision loss',()=>{
 const values=['10.00','2.00','0.001','-3','9007199254740993','9007199254740992','1e-4',null];
 assert.deepEqual(values.toSorted(compareCells),['-3','1e-4','0.001','2.00','10.00','9007199254740992','9007199254740993',null]);
 assert.equal(compareCells('1.00','1'),0);assert.ok(compareCells('1.00000000000000000001','1.00000000000000000002')<0);
});
test('chart defaults skip single values, dates as values, and textual columns',()=>{
 assert.equal(defaultChartOptions(answer({columns:['value'],rows:[[3]]})),null);
 assert.equal(defaultChartOptions(answer({columns:['name','date'],rows:[['甲','2026-01-01'],['乙','2026-01-02']]})),null);
 assert.equal(defaultChartOptions(answer({columns:['name','description','amount'],rows:[['甲','一',3],['乙','二',5]]})).yColumns[0],'amount');
});
test('quality notes prioritize only attributable citation pages and preserve ambiguous notes separately',()=>{
 const a=answer({notes:['甲.pdf 第 2 页：需核对','乙.pdf 第 7 页：有警示','第 2 页：归属未知'],evidence:[{source_id:'a',page:2}]});a.sources=[{id:'a',name:'甲.pdf'},{id:'b',name:'乙.pdf'}];
 const groups=groupAnswerNotes(a);assert.equal(groups[0].priority,true);assert.equal(groups.at(-1).key,'unassigned');assert.equal(groups.flatMap(g=>g.notes).length,3);
 assert.equal(groups.find(g=>g.key==='unassigned').notes[0],'第 2 页：归属未知');
});

test('explicit two-decimal display formats SQL money including trailing zeroes while raw rows and published display remain authoritative',()=>{
 const raw={...table,columns:['国家','销售额','员工编号'],rows:[['France',195.1,1],['Brazil',190.1,2]],display_rows:undefined};const formatted=displayTable(raw,'金额保留两位小数');
 assert.deepEqual(formatted.display_rows,[['France','195.10',1],['Brazil','190.10',2]]);assert.equal(formatted.rows,raw.rows);assert.equal(raw.display_rows,undefined);
 assert.equal(displayTable(raw,'统计销售额'),raw);assert.equal(displayTable(table,'保留两位小数'),table);
 assert.equal(fixedDecimal('9007199254740993.055',2),'9007199254740993.06');assert.equal(fixedDecimal('1e-3',2),'0.00');assert.equal(fixedDecimal('-1.005',2),'-1.01');assert.equal(fixedDecimal(null,2),null);
});
test('employee identifiers are never default metrics when known work hours are present',()=>{
 const options=defaultChartOptions(answer({columns:['地区','员工编号','已知工时合计'],rows:[['东店',1,12],['西店',2,20]]}));
 assert.equal(options.yColumns[0],'已知工时合计');assert.equal(options.xColumn,'地区');
 assert.equal(defaultChartOptions(answer({columns:['地区','员工编号'],rows:[['东店',1],['西店',2]]})),null);
});
