import type {ResearchAnswer} from './researchTypes';

export type NoteGroup={key:string;title:string;priority:boolean;notes:string[]};
export function groupAnswerNotes(answer:ResearchAnswer):NoteGroup[]{
 const notes=answer.result?.notes??[],sources=answer.sources??[],evidence=answer.result?.evidence??[];
 const groups=new Map<string,NoteGroup>();
 for(const text of notes){
  // A naked page number in a 12-source answer is not enough to infer its source.
  const named=sources.filter(source=>text.includes(source.name)||text.includes(source.id));
  const source=named.length===1?named[0]:sources.length===1?sources[0]:undefined;
  const page=Number(text.match(/第\s*(\d+)\s*[页段]/)?.[1]??0);
  const cited=!!source&&page>0&&evidence.some(e=>e.source_id===source.id&&e.page===page);
  const key=source?`${source.id}:${cited?'cited':'other'}`:'unassigned';
  const group=groups.get(key)??{key,title:source?`${source.name}${cited?' · 本次引用页提示':' · 其他解析与使用提示'}`:'未标明来源的提示',priority:cited,notes:[]};
  group.notes.push(text);groups.set(key,group);
 }
 return [...groups.values()].sort((a,b)=>Number(b.priority)-Number(a.priority));
}
