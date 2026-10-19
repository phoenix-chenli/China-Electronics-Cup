import type {ResearchAnswer} from './researchTypes';
import type {AnswerCalculation} from './types';

export type AnswerViewMode = 'read' | 'compare';
export type AnswerParagraph = {kind:'fact'|'scope_notice'|'coverage_gap'|'legacy';text:string;evidenceIds:string[]};
export type AnswerHeading = {id:string;level:number;text:string};
export const DEFAULT_SPLIT = 43;
export const clampSplit = (value:number) => Number.isFinite(value)?Math.min(55,Math.max(35,value)):DEFAULT_SPLIT;
export const answerPreferenceKey = (projectId:string,answerId:string) => `zhiheng.answer-view.${projectId}.${answerId}`;
export function readAnswerPreference(raw:string|null,fallback:AnswerViewMode):{mode:AnswerViewMode;split:number}{
 try{const parsed=JSON.parse(raw??'null');return {mode:parsed?.mode==='read'||parsed?.mode==='compare'?parsed.mode:fallback,split:clampSplit(parsed?.split)};}catch{return {mode:fallback,split:DEFAULT_SPLIT};}
}
/** Presentation never promotes rejected draft text to published answer content. */
export function answerParagraphs(answer:ResearchAnswer):AnswerParagraph[]{
 const result=answer.result;
 if(!result||!['completed','insufficient_evidence'].includes(answer.status))return [];
 const blocks=result.display_blocks??[];
 if(blocks.some(b=>b.kind==='fact'))return blocks.filter(b=>['fact','scope_notice','coverage_gap'].includes(b.kind)&&typeof b.text==='string').map(b=>({kind:b.kind as AnswerParagraph['kind'],text:b.text!,evidenceIds:b.evidence_ids??[]}));
 if(result.claims?.length)return result.claims.map(c=>({kind:'fact',text:c.text,evidenceIds:c.evidence_ids??[]}));
 // Older saved records can predate claim projection. Keep that historical text visibly qualified.
 if(answer.status==='completed'&&result.text)return [{kind:'legacy',text:result.text,evidenceIds:[]}];
 return [];
}
export function headingId(prefix:string,line:number){return `${prefix}-heading-${line}`;}
/** Used for the table of contents; code examples never become article headings. */
export function markdownHeadings(text:string,prefix:string):AnswerHeading[]{
 const lines=text.split(/\r?\n/);let fence:string|null=null;const result:AnswerHeading[]=[];
 for(let i=0;i<lines.length;i++){
  const line=lines[i];const marker=line.match(/^\s{0,3}(`{3,}|~{3,})/);
  if(marker){if(!fence)fence=marker[1];else if(marker[1][0]===fence[0]&&marker[1].length>=fence.length)fence=null;continue;}
  if(fence||/^ {4}|^\t/.test(line))continue;
  const heading=line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
  const setext=!heading&&line.trim()?lines[i+1]?.match(/^\s{0,3}(=+|-+)\s*$/):null;
  if(heading||setext){const title=heading?.[2]??line.trim();result.push({id:headingId(prefix,i+1),level:heading?heading[1].length:(setext![1][0]==='='?1:2),text:title.replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/[*_`]/g,'')});if(setext)i++;}
 }
 return result;
}
export function safeAnswerUrl(url:string):string{
 if(/^https?:\/\//i.test(url))return url;
 if(/^#citation-[A-Za-z0-9_-]+$/.test(url))return url;
 return '';
}

export function answerCoverageGaps(answer:ResearchAnswer):{text:string;status:string}[]{
 const record=(value:unknown):Record<string,unknown>|null=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:null;
 const result=record(answer.result);if(!result)return [];
 const gapBlock=(answer.result?.display_blocks??[]).find(b=>b.kind==='coverage_gap');
 const projected=record(gapBlock)?.items;
 if(Array.isArray(projected))return projected.flatMap(item=>{const entry=record(item);return typeof entry?.question_text==='string'?[{text:entry.question_text,status:typeof entry.coverage_status==='string'?entry.coverage_status:'unknown'}]:[];});
 const coverage=record(result.completeness)??record(result.coverage),items=coverage?.items;
 return Array.isArray(items)?items.flatMap(item=>{const entry=record(item);return typeof entry?.text==='string'&&entry.status!=='answered'?[{text:entry.text,status:typeof entry.status==='string'?entry.status:'unknown'}]:[];}):[];
}

/** Prefer the lossless published block without duplicating or discarding saved calculation records. */
export function answerCalculations(answer:ResearchAnswer):AnswerCalculation[]{
 const saved=answer.result?.calculations??[];
 const projected=(answer.result?.display_blocks??[]).flatMap(block=>block.kind==='calculation'&&block.calculation&&typeof block.calculation.id==='string'&&Array.isArray(block.calculation.parameters)?[block.calculation]:[]);
 const byId=new Map(saved.map(calculation=>[calculation.id,calculation]));
 for(const calculation of projected)byId.set(calculation.id,{...byId.get(calculation.id),...calculation,checks:calculation.checks??byId.get(calculation.id)?.checks??[]});
 return [...byId.values()];
}

/** Show recorded inputs and outputs; never evaluate an expression or derive a new numeric result. */
export function recordedUnitConversion(calculation:AnswerCalculation):string|null{
 const handling=calculation.unit_handling,parameter=calculation.parameters[0];
 if(handling?.pure_unit_conversion!==true||calculation.parameters.length!==1||calculation.expression.trim()!==parameter?.name||
  !parameter.unit||!calculation.unit||handling.output_unit!==calculation.unit||!calculation.display_value)return null;
 return `${String(parameter.value)} ${parameter.unit} = ${calculation.display_value} ${calculation.unit}`;
}
