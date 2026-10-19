import test from 'node:test';
import assert from 'node:assert/strict';
import {answerPublicationStatus} from '../src/answerPublication.ts';

const complete=()=>({status:'assessed',complete:true,items:[{id:'Q1',text:'完整要求',status:'answered',claim_ids:['C1']}]});
const audit=(status='not_required')=>({version:'test',status,requirements:status==='not_required'?[]:[{id:'Q1'}],issues:[]});
const answer=()=>({status:'completed',scope_stale:false,result:{text:'保留的原回答',claims:[{id:'C1',text:'保留的原回答',evidence_ids:['E1']}],evidence:[{id:'E1'}],verification:{status:'passed'},completeness:complete(),calculation_completeness:audit()}});

test('only explicit complete coverage plus a successful calculation audit permits the checked label',()=>{
 for(const status of ['not_required','passed']){
  const value=answer();value.result.calculation_completeness=audit(status);
  assert.deepEqual(answerPublicationStatus(value),{checked:true,label:'结论已核对'});
 }
 const legacyAlias=answer();legacyAlias.result.coverage=legacyAlias.result.completeness;delete legacyAlias.result.completeness;
 assert.equal(answerPublicationStatus(legacyAlias).checked,true);
});

for(const [name,change] of [
 ['missing coverage',a=>delete a.result.completeness],
 ['coverage has no items',a=>a.result.completeness.items=[]],
 ['coverage is malformed',a=>a.result.completeness='assessed'],
 ['coverage completion is unknown',a=>a.result.completeness.complete=null],
 ['coverage assessment is missing',a=>delete a.result.completeness.status],
 ['coverage contains an unknown item',a=>a.result.completeness.items[0].status='unknown'],
 ['coverage question ID is missing',a=>delete a.result.completeness.items[0].id],
 ['coverage question ID is blank',a=>a.result.completeness.items[0].id=' '],
 ['coverage question ID is duplicated',a=>a.result.completeness.items.push({...a.result.completeness.items[0]})],
 ['answered question has no claim IDs',a=>a.result.completeness.items[0].claim_ids=[]],
 ['answered question has an unknown claim ID',a=>a.result.completeness.items[0].claim_ids=['unknown']],
 ['published claims are empty',a=>a.result.claims=[]],
 ['published claim text is blank',a=>a.result.claims[0].text=' '],
 ['published claim IDs are duplicated',a=>a.result.claims.push({...a.result.claims[0]})],
 ['published claim has no actual evidence or calculation',a=>a.result.claims[0].evidence_ids=[]],
 ['published claim points to unknown evidence',a=>a.result.claims[0].evidence_ids=['unknown']],
 ['published claim points to an unknown calculation',a=>a.result.claims[0].calculation_ids=['K-missing']],
 ['published evidence IDs are ambiguous',a=>a.result.evidence.push({id:'E1'})],
 ['coverage records validation errors',a=>a.result.completeness.validation_errors=[{code:'missing_question'}]],
 ['coverage records a positive error count',a=>a.result.completeness.error_count=1],
 ['missing calculation audit',a=>delete a.result.calculation_completeness],
 ['calculation audit is malformed',a=>a.result.calculation_completeness=['passed']],
 ['calculation audit status is unknown',a=>a.result.calculation_completeness.status='unknown'],
 ['calculation audit lacks issue inventory',a=>delete a.result.calculation_completeness.issues],
 ['calculation audit lacks requirement inventory',a=>delete a.result.calculation_completeness.requirements],
 ['calculation audit has unresolved issues',a=>a.result.calculation_completeness.issues=[{code:'missing_calculation'}]],
 ['calculation marked unnecessary still has requirements',a=>a.result.calculation_completeness.requirements=[{id:'Q1'}]],
])test(`${name} cannot become a historical success`,()=>{
 const value=answer();change(value);const before=structuredClone(value),state=answerPublicationStatus(value);
 assert.equal(state.checked,false);assert.match(state.label,/完整性待核对/);assert.deepEqual(value,before,'presentation must not rewrite stored history');
});

test('a recorded calculation omission overrides old complete and passed fields',()=>{
 const value=answer();value.question='1000分是多少钱';value.result.calculation_completeness={...audit('incomplete'),issues:[{code:'missing_calculation'}]};
 assert.deepEqual(answerPublicationStatus(value),{checked:false,label:'计算尚未完整核对'});
});

for(const status of ['evidence_available','missing_evidence'])test(`${status} overrides an overall complete flag`,()=>{
 const value=answer();value.result.completeness.items[0].status=status;
 assert.deepEqual(answerPublicationStatus(value),{checked:false,label:'回答未完整 · 仍需核对'});
});

test('explicit partial, projected gaps and conflicting audits never receive a success label',()=>{
 const partial=answer();partial.result.completeness.complete=false;assert.equal(answerPublicationStatus(partial).checked,false);
 const projected=answer();projected.result.display_blocks=[{kind:'coverage_gap',text:'还未回答费用'}];assert.match(answerPublicationStatus(projected).label,/回答未完整/);
 for(const change of [
  a=>a.result.completeness.status='conflict',
  a=>a.result.completeness.items[0].status='conflict',
  a=>a.result.coverage={status:'conflict',complete:false,items:[]},
  a=>a.result.verification.conflict=true,
 ]){const value=answer();change(value);assert.deepEqual(answerPublicationStatus(value),{checked:false,label:'依据存在冲突 · 结论待核对'});}
});

test('both coverage aliases must bind the same question and claim sets, regardless of ordering',()=>{
 const value=answer();value.result.claims.push({id:'C2',text:'另一处已保存事实',evidence_ids:['E1']});
 value.result.completeness.items=[{id:'Q1',status:'answered',claim_ids:['C1','C2']},{id:'Q2',status:'answered',claim_ids:['C2']}];
 value.result.coverage={...complete(),items:[{id:'Q2',status:'answered',claim_ids:['C2']},{id:'Q1',status:'answered',claim_ids:['C2','C1']}]};
 assert.equal(answerPublicationStatus(value).checked,true);
 value.result.coverage.items[0].id='Q3';assert.equal(answerPublicationStatus(value).checked,false);
 value.result.coverage.items[0].id='Q2';value.result.coverage.items[1].claim_ids=['C1'];assert.equal(answerPublicationStatus(value).checked,false);
 value.result.coverage.items[1].claim_ids=['C1','C2'];value.result.coverage.items[0].status='unknown';assert.equal(answerPublicationStatus(value).checked,false);
});

test('a claim may reference a real saved calculation but never a fabricated one',()=>{
 const value=answer();value.result.claims[0].evidence_ids=[];value.result.claims[0].calculation_ids=['K1'];value.result.calculations=[{id:'K1'}];
 value.result.calculation_completeness=audit('passed');assert.equal(answerPublicationStatus(value).checked,true);
 value.result.calculations=[];assert.equal(answerPublicationStatus(value).checked,false);
});

test('execution and limited verification remain distinguishable from a checked conclusion',()=>{
 const value=answer();value.result.verification.status='limited';value.result.query={sql:'SELECT 1'};
 assert.deepEqual(answerPublicationStatus(value),{checked:false,label:'查询已执行 · 结论待核对'});
 delete value.result.completeness;assert.equal(answerPublicationStatus(value).label,'查询已执行 · 完整性待核对');
});

test('only a fully audited stale record can retain an explicitly historical checked label',()=>{
 const value=answer();value.scope_stale=true;
 assert.deepEqual(answerPublicationStatus(value),{checked:true,label:'回答时已核对 · 来源已变化'});
 delete value.result.calculation_completeness;assert.equal(answerPublicationStatus(value).checked,false);
});

for(const status of ['queued','running','failed','cancelled','needs_clarification','insufficient_evidence'])test(`${status} cannot be promoted by leftover successful result fields`,()=>{
 const value=answer();value.status=status;value.stage='处理中的真实阶段';
 const state=answerPublicationStatus(value);assert.equal(state.checked,false);assert.doesNotMatch(state.label,/结论已核对|回答时已核对/);
 if(['queued','running'].includes(status))assert.equal(state.label,value.stage);
});

test('absent results fail closed without throwing',()=>{
 for(const result of [undefined,null])assert.deepEqual(answerPublicationStatus({...answer(),result}).checked,false);
});
