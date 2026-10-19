import test from 'node:test';
import assert from 'node:assert/strict';
import {answerCalculations,answerCoverageGaps,answerParagraphs,answerPreferenceKey,clampSplit,markdownHeadings,readAnswerPreference,recordedUnitConversion,safeAnswerUrl} from '../src/answerPresentation.ts';
const answer=(status='completed',result={})=>({status,result:{text:'rejected candidate',claims:[],...result}});
test('published paragraphs preserve literal facts and evidence while excluding audit content',()=>{
 const a=answer('completed',{claims:[{text:'原始结论 3.00 万元',evidence_ids:['E1']}],display_blocks:[{kind:'fact',text:'原始结论 3.00 万元',evidence_ids:['E1']},{kind:'audit',text:'候选 9.99 万元'}]});
 assert.deepEqual(answerParagraphs(a),[{kind:'fact',text:'原始结论 3.00 万元',evidenceIds:['E1']}]);
});
for(const status of ['failed','cancelled','running','queued','needs_clarification'])test(`${status} never promotes a candidate body`,()=>assert.deepEqual(answerParagraphs(answer(status)),[]));
test('partial answers preserve supported claims only and do not fall back to a draft',()=>{
 assert.deepEqual(answerParagraphs(answer('insufficient_evidence')),[]);
 assert.equal(answerParagraphs(answer('insufficient_evidence',{claims:[{text:'可支持的部分',evidence_ids:['E1']}]}))[0].text,'可支持的部分');
});
test('legacy completed content remains identified, not silently discarded',()=>assert.equal(answerParagraphs(answer())[0].kind,'legacy'));
test('table of contents uses real headings and excludes fenced and indented code',()=>{
 const text='# 标题\n\n```md\n## 不是章节\n```\n\n## 第二部分\n\n    ## 代码\n\n末节\n----';
 assert.deepEqual(markdownHeadings(text,'a').map(h=>[h.id,h.text]),[['a-heading-1','标题'],['a-heading-7','第二部分'],['a-heading-11','末节']]);
});
test('view preference is per project and answer and handles invalid browser storage',()=>{
 assert.notEqual(answerPreferenceKey('p1','a'),answerPreferenceKey('p2','a'));
 assert.deepEqual(readAnswerPreference('{broken','read'),{mode:'read',split:43});
 assert.deepEqual(readAnswerPreference('{"mode":"compare","split":99}','read'),{mode:'compare',split:55});
 assert.deepEqual(readAnswerPreference('{"mode":"fake","split":-1}','read'),{mode:'read',split:35});
 assert.equal(clampSplit(NaN),43);assert.equal(clampSplit(43),43);
});
test('rendered links reject script, file, network-path and data URLs',()=>{
 for(const url of ['javascript:alert(1)','data:text/html,hello','file:///etc/passwd','//evil.test','mailto:secret@example.org','/api/private'])assert.equal(safeAnswerUrl(url),'');
 assert.equal(safeAnswerUrl('https://example.com/document'),'https://example.com/document');assert.equal(safeAnswerUrl('#citation-E1'),'#citation-E1');
});


test('coverage gaps preserve actual missing and pending items without asserting source absence',()=>{
 const a=answer('insufficient_evidence',{coverage:{items:[{text:'退款口径',status:'missing_evidence'},{text:'收入',status:'answered'},{text:'税费',status:'evidence_available'}]}});
 assert.deepEqual(answerCoverageGaps(a),[{text:'退款口径',status:'missing_evidence'},{text:'税费',status:'evidence_available'}]);
 const projected=answer('insufficient_evidence',{display_blocks:[{kind:'coverage_gap',items:[{question_text:'销量',coverage_status:'unknown'}]}]});
 assert.deepEqual(answerCoverageGaps(projected),[{text:'销量',status:'unknown'}]);
});

test('calculation projection enriches the same saved record without duplicates or losing pending records',()=>{
 const saved={id:'K1',expression:'a',parameters:[{name:'a',value:'1000',unit:'分',evidence_ids:[]}],display_value:'10.00',unit:'元',checks:['保留的原核对项']};
 const pending={...saved,id:'K2',display_value:'待核对的保存值'};
 const projected={...saved,checks:undefined,unit_handling:{pure_unit_conversion:true,output_unit:'元',output_unit_factor:'1'}};
 const value=answer('completed',{calculations:[saved,pending],display_blocks:[{kind:'calculation',calculation:projected}]}),before=structuredClone(value);
 const calculations=answerCalculations(value);
 assert.equal(calculations.length,2);assert.deepEqual(calculations[0].checks,saved.checks);assert.deepEqual(calculations[1],pending);
 assert.equal(recordedUnitConversion(calculations[0]),'1000 分 = 10.00 元');assert.deepEqual(value,before);
});

test('unit conversion presentation uses the recorded result and does not calculate or infer missing units',()=>{
 const calculation={id:'K1',expression:'a',parameters:[{name:'a',value:'1000',unit:'分',evidence_ids:[]}],display_value:'保存的结果字符串',unit:'元',unit_handling:{pure_unit_conversion:true,output_unit:'元'}};
 assert.equal(recordedUnitConversion(calculation),'1000 分 = 保存的结果字符串 元');
 for(const patch of [{unit:undefined},{expression:'a/100'},{unit_handling:undefined},{unit_handling:{pure_unit_conversion:true,output_unit:'美元'}}])assert.equal(recordedUnitConversion({...calculation,...patch}),null);
});
