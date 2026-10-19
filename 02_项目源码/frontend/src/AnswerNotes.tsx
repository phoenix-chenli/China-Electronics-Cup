import {useMemo,useState} from 'react';
import type {ResearchAnswer} from './researchTypes';

import {groupAnswerNotes} from './qualityNotes';
import type {NoteGroup} from './qualityNotes';

function NoteList({group}:{group:NoteGroup}){
 const [open,setOpen]=useState(group.priority),[limit,setLimit]=useState(20);
 return <details className="answer-note-group" open={open} onToggle={e=>setOpen(e.currentTarget.open)}><summary>{group.title}<span>{group.notes.length} 项</span></summary>{open&&<><ul>{group.notes.slice(0,limit).map((note,i)=><li key={i}>{note}</li>)}</ul>{limit<group.notes.length&&<button className="subtle" onClick={()=>setLimit(limit+50)}>继续显示（剩余 {group.notes.length-limit} 项）</button>}</>}</details>;
}
export function AnswerNotes({answer}:{answer:ResearchAnswer}){
 const [open,setOpen]=useState(false);const groups=useMemo(()=>groupAnswerNotes(answer),[answer.result?.notes,answer.sources,answer.result?.evidence]);
 if(!groups.length)return null;
 return <details className="method-detail answer-notes" open={open} onToggle={e=>setOpen(e.currentTarget.open)}><summary>适用范围与质量提示（{answer.result?.notes.length} 项）</summary>{open&&<><p>优先查看能够确认所属资料的引用页提示。其余按来源折叠；未标明来源的原始提示单列，不猜测归属。</p>{groups.map(group=><NoteList key={group.key} group={group}/>)}</>}</details>;
}
