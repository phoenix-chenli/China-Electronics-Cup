import type { AnswerCalculation, AnswerRecord, AnswerResult, AnswerSource } from './types';
export type ContextPatch = { op: 'add'|'replace'|'remove'; field:string; value?:unknown };
export type ContextSlot = { value:unknown; origin:string; confirmed_by_user:boolean; source_answer_id:string };
export type ResearchAnswer = Omit<AnswerRecord,'result'|'sources'> & {
 context_revision:string; sources:(AnswerSource & {semantic_revision?:string})[];
 context_state?:{slots:Record<string,ContextSlot>; changes:{field:string;operation:string;before?:ContextSlot|null;after?:ContextSlot|null;source_answer_id?:string}[];unresolved?:{field:string;kind:string;source_answer_id?:string}[];unknown_fields?:Record<string,{previous_source_answer_id?:string;source_answer_id?:string}>};
 result:(AnswerResult & {trace?:{stage:string;elapsed_ms:number}[];display_blocks?:{kind:string;text?:string;evidence_ids?:string[];calculation?:AnswerCalculation}[];coverage?:unknown;completeness?:unknown;calculation_completeness?:unknown;column_units?:Record<string,string|null>})|null;
};
export type SuggestRecord = {key:string;state:string;scope:{project_id:string;answer_id:string;branch_id:string;context_revision:string;source_revisions:(AnswerSource&{semantic_revision?:string})[]};suggestions:{id:string;text:string;action:string;payload:Record<string,unknown>}[];fill_text?:string;accepted_action?:string;accepted_payload?:Record<string,unknown>};
export const formatValue=(value:unknown):string => value===null||value===undefined?'—':typeof value==='object'?JSON.stringify(value):String(value);
export const formatContextValue=(value:unknown):string => {
 if(Array.isArray(value))return value.map(formatContextValue).join('；');
 if(value&&typeof value==='object'){const item=value as Record<string,unknown>;if(typeof item.table==='string'&&typeof item.column==='string'&&'value' in item)return `${item.table}.${item.column} = ${formatContextValue(item.value)}`;return Object.entries(item).map(([key,value])=>`${key}：${formatContextValue(value)}`).join('；');}
 return formatValue(value);
};
export const terminal=(a:ResearchAnswer)=>!['queued','running'].includes(a.status);
export const canContinueAnswer=(answer:Pick<ResearchAnswer,'status'>|null|undefined):boolean=>!!answer&&['completed','needs_clarification','insufficient_evidence'].includes(answer.status);
export const slotLabels:Record<string,string>={topic:'主题',entities:'对象',metric:'指标',time_range:'时间范围',dimensions:'分组',filters:'筛选',comparison_base:'比较基期',unit:'单位',grain:'统计粒度',denominator:'分母',assumptions:'假设'};
