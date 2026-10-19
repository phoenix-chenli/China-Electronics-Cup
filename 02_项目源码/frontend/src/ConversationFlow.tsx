import {useEffect,useState} from 'react';
import type {ReactNode} from 'react';
import {ChevronDown,ChevronRight,ArrowDown} from 'lucide-react';
import type {ResearchAnswer} from './researchTypes';
import {canContinueAnswer,terminal} from './researchTypes';
import {tabStorage} from './browserStorage';
import {answerPublicationStatus} from './answerPublication';
export function ConversationFlow({chain,activeId,children}:{chain:ResearchAnswer[];activeId?:string;children:(answer:ResearchAnswer)=>ReactNode}){
 const key=`zhiheng.turns:${chain[0]?.project_id}:${chain[0]?.id}`;
 const [expanded,setExpanded]=useState<string[]>(()=>{try{const value=JSON.parse(tabStorage.getItem(key)??'[]');return Array.isArray(value)?value.filter(item=>typeof item==='string'):[];}catch{return [];}});
 const [jump,setJump]=useState(activeId??'');
 useEffect(()=>{tabStorage.setItem(key,JSON.stringify(expanded));},[key,expanded]);
 useEffect(()=>setJump(activeId??''),[activeId]);
 const openTurn=(id:string)=>{setExpanded(current=>Array.from(new Set([...current,id])));setJump(id);requestAnimationFrame(()=>document.getElementById(`turn-${id}`)?.scrollIntoView({block:'start',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'}));};
 const allOpen=chain.every(answer=>answer.id===activeId||expanded.includes(answer.id));
 const current=chain.find(answer=>answer.id===activeId)??chain.at(-1),nextTurnHint=canContinueAnswer(current)?`下一问仍接续第 ${current?.turn_index??chain.length} 轮`:current&&!terminal(current)?'当前轮仍在处理中':'下一问将作为独立问题，需写明完整条件';
 return <div className="conversation-flow">
  {chain.length>1&&<nav className="conversation-controls" aria-label="本次对话轮次"><span>{chain.length} 轮分析</span><label><span className="sr-only">跳转到分析轮次</span><select aria-label="跳转到分析轮次" value={jump} onChange={event=>openTurn(event.target.value)}>{chain.map(answer=><option key={answer.id} value={answer.id}>第 {answer.turn_index??1} 轮 · {answer.question.slice(0,40)}</option>)}</select></label><button className="subtle" onClick={()=>setExpanded(allOpen?[]:chain.map(answer=>answer.id))}>{allOpen?'收起历史回答':'展开全部回答'}</button><button className="subtle" onClick={()=>{openTurn(activeId??'');document.getElementById('question')?.focus({preventScroll:true});}}><ArrowDown size={14}/>当前轮</button></nav>}
  <div className="answer-flow">{chain.map(answer=>{
   const active=answer.id===activeId,open=active||expanded.includes(answer.id),rows=answer.result?.rows?.length??0,evidence=answer.result?.evidence?.length??0,publication=answerPublicationStatus(answer);
   return <section id={`turn-${answer.id}`} className={`conversation-turn${open?' is-open':' is-collapsed'}${active?' is-current':''}`} key={answer.id}>
    {!open?<button className="turn-summary" aria-expanded={false} onClick={()=>openTurn(answer.id)}><span className="turn-summary-index">{String(answer.turn_index??1).padStart(2,'0')}</span><span className="turn-summary-main"><strong>{answer.question}</strong><small>{publication.label}{answer.scope_stale&&!publication.checked?' · 资料已变化':''}{rows?` · ${rows} 行结果`:''}{evidence?` · ${evidence} 处依据`:''}</small></span><span className="turn-summary-action">展开回答<ChevronRight size={15}/></span></button>:<>{!active&&<div className="turn-expanded-heading"><span>正在回看第 {answer.turn_index??1} 轮 · {nextTurnHint}</span><button className="subtle" aria-label={`收起第 ${answer.turn_index??1} 轮回答`} onClick={()=>setExpanded(current=>current.filter(id=>id!==answer.id))}>收起<ChevronDown size={14}/></button></div>}{children(answer)}</>}
   </section>;
  })}</div>
 </div>;
}
