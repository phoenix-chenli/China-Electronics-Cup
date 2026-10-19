import type {ResearchAnswer} from './researchTypes';

type PublicationStatus = {checked:boolean;label:string;detail?:string};
const record=(value:unknown):Record<string,unknown>|null=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:null;
const terminalLabels:Record<string,string>={needs_clarification:'请确认分析条件',failed:'这次分析未完成',cancelled:'分析已停止'};
const identifier=(value:unknown):value is string=>typeof value==='string'&&value.trim().length>0;

function publishedClaimIds(result:Record<string,unknown>|null):Set<string>|null{
 const ids=(values:unknown):Set<string>|null=>{
  if(values===undefined||values===null)return new Set();
  if(!Array.isArray(values))return null;
  const entries=values.map(value=>record(value)?.id);
  return entries.every(identifier)&&new Set(entries).size===entries.length?new Set(entries as string[]):null;
 };
 const evidence=ids(result?.evidence),calculations=ids(result?.calculations),claims=result?.claims;
 if(!evidence||!calculations||!Array.isArray(claims)||!claims.length)return null;
 const seen=new Set<string>();
 for(const value of claims){
  const claim=record(value);if(!claim||!identifier(claim.id)||seen.has(claim.id)||!identifier(claim.text))return null;
  const references=(raw:unknown,available:Set<string>):string[]|null=>raw===undefined||raw===null?[]:
   Array.isArray(raw)&&raw.every(id=>identifier(id)&&available.has(id))?raw as string[]:null;
  const refs=references(claim.evidence_ids,evidence),calcs=references(claim.calculation_ids,calculations);
  if(!refs||!calcs||!refs.length&&!calcs.length)return null;
  seen.add(claim.id);
 }
 return seen;
}

function completeCoverage(value:unknown,claims:Set<string>):boolean{
 const coverage=record(value);
 const seen=new Set<string>();
 return coverage?.status==='assessed'&&coverage.complete===true&&Array.isArray(coverage.items)&&coverage.items.length>0&&
  coverage.items.every(value=>{const item=record(value);if(!item||!identifier(item.id)||seen.has(item.id)||item.status!=='answered'||
   !Array.isArray(item.claim_ids)||!item.claim_ids.length||!item.claim_ids.every(id=>identifier(id)&&claims.has(id)))return false;
   seen.add(item.id);return true;})&&
  !(Array.isArray(coverage.validation_errors)&&coverage.validation_errors.length)&&
  !(typeof coverage.error_count==='number'&&coverage.error_count>0);
}

function coverageSignature(value:unknown):string{
 const items=record(value)!.items as Record<string,unknown>[];
 return JSON.stringify(items.map(item=>[item.id,item.status,[...new Set(item.claim_ids as string[])].sort()])
  .sort((a,b)=>String(a[0]).localeCompare(String(b[0]))));
}

/** A terminal job and checked individual claims do not establish a complete answer. */
export function answerPublicationStatus(answer:ResearchAnswer):PublicationStatus{
 if(['queued','running'].includes(answer.status))return {checked:false,label:answer.stage||'正在处理问题'};
 if(terminalLabels[answer.status])return {checked:false,label:terminalLabels[answer.status]};
 const result=record(answer.result),verification=record(result?.verification);
 // Accept the saved legacy alias, but never let it override a contradictory newer audit.
 const coverageValues=[result?.completeness,result?.coverage].filter(value=>value!==undefined&&value!==null);
 const coverage=coverageValues.map(record),items=coverage.flatMap(item=>Array.isArray(item?.items)?item.items:[]).map(record);
 const projectedGaps=Array.isArray(result?.display_blocks)&&result.display_blocks.some(block=>record(block)?.kind==='coverage_gap');
 const conflict=result?.conflict===true||verification?.conflict===true||verification?.status==='conflict'||
  coverage.some(item=>item?.status==='conflict')||items.some(item=>item?.status==='conflict');
 if(conflict)return {checked:false,label:'依据存在冲突 · 结论待核对'};
 const calculation=record(result?.calculation_completeness);
 if(calculation?.status==='incomplete')return {checked:false,label:'计算尚未完整核对'};
 const partial=answer.status==='insufficient_evidence'||projectedGaps||
  coverage.some(item=>item?.complete===false)||items.some(item=>['evidence_available','missing_evidence'].includes(String(item?.status)));
 if(partial)return {checked:false,label:answer.status==='insufficient_evidence'?'依据不足 · 回答未完整':'回答未完整 · 仍需核对'};
 const claims=publishedClaimIds(result);
 const covered=!!claims&&coverageValues.length>0&&coverageValues.every(value=>completeCoverage(value,claims))&&
  new Set(coverageValues.map(coverageSignature)).size===1;
 // This is a backend audit, not a client-side guess from question wording or a numeric paragraph.
 const calculationsChecked=!!calculation&&['passed','not_required'].includes(String(calculation.status))&&
  Array.isArray(calculation.requirements)&&Array.isArray(calculation.issues)&&calculation.issues.length===0&&
  (calculation.status!=='not_required'||calculation.requirements.length===0);
 if(!covered||!calculationsChecked)return {checked:false,
  label:typeof record(result?.query)?.sql==='string'?'查询已执行 · 完整性待核对':'完整性待核对',
  detail:'这份记录缺少完整的逐项覆盖或计算核验记录，不能据执行完成认定回答完整。'};
 if(answer.status==='completed'&&verification?.status==='passed')return {checked:true,
  label:answer.scope_stale?'回答时已核对 · 来源已变化':'结论已核对'};
 return {checked:false,label:typeof record(result?.query)?.sql==='string'?'查询已执行 · 结论待核对':'结论待核对'};
}
