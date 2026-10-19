import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUp, X, Undo2, Square } from 'lucide-react';
import { api, messageOf } from './api';
import type { ResearchAnswer, SuggestRecord } from './researchTypes';
import { canContinueAnswer } from './researchTypes';
import { createSuggestionState, reduceSuggestion, suggestionContextKey, suggestionVisible } from './suggestionKeyboard';
import type { SuggestionContext, SuggestionEvent } from './suggestionKeyboard';
import './suggestionSettings.css';
import './QuestionComposer.css';
import {readDesktopPreferences,shortcutMatches} from './desktopPreferences';

type Props={answer:ResearchAnswer|null;draft:string;setDraft:(value:string)=>void;onSubmit:()=>void;disabled:boolean;onError:(text:string)=>void;onAction:(action:string,payload:Record<string,unknown>)=>void;onCancel?:()=>void;suggestionsSuspended?:boolean};
const EMPTY:SuggestionContext={answerId:'',branchId:'',contextRevision:'',sourceRevisions:{}};
export function QuestionComposer({answer,draft,setDraft,onSubmit,disabled,onError,onAction,onCancel,suggestionsSuspended=false}:Props){
 const [record,setRecord]=useState<SuggestRecord|null>(null);const [enabled,setEnabled]=useState(true);const [preferenceVersion,setPreferenceVersion]=useState(0);const preferenceRequest=useRef(0);
 const suspension=useRef({active:suggestionsSuspended,revision:0});
 if(suspension.current.active!==suggestionsSuspended)suspension.current={active:suggestionsSuspended,revision:suspension.current.revision+1};
 const sourceKey=JSON.stringify(answer?.sources.map(s=>[s.id,s.sha256,s.parser_version,s.revision,s.semantic_revision])??[]);
 const machine=useRef(createSuggestionState(EMPTY));const [,redraw]=useState(0);const input=useRef<HTMLTextAreaElement>(null);
 const draftRef=useRef(draft);draftRef.current=draft;const currentRecord=useRef(record);currentRecord.current=record;
 const pending=useRef(new Set<string>());const live=useRef(true);
 const resizeInput=useCallback(()=>{
  const element=input.current;if(!element)return;
  // Reset the intrinsic row height before measuring so deleting a long draft shrinks it again.
  element.style.height='0px';
  const style=window.getComputedStyle(element),minimum=parseFloat(style.minHeight)||(answer?44:88),maximum=parseFloat(style.maxHeight)||160;
  const border=(parseFloat(style.borderTopWidth)||0)+(parseFloat(style.borderBottomWidth)||0);
  const natural=element.scrollHeight+border,height=Math.min(maximum,Math.max(minimum,natural));
  element.style.height=`${height}px`;element.style.overflowY=natural>height?'auto':'hidden';
 },[answer?.id]);
 useEffect(()=>{resizeInput();},[draft,resizeInput]);
 useEffect(()=>{
  const element=input.current;if(!element)return;
  let width=element.getBoundingClientRect().width;
  const observer=typeof ResizeObserver==='undefined'?null:new ResizeObserver(entries=>{
   const next=entries[0]?.contentRect.width;if(next!==undefined&&next!==width){width=next;resizeInput();}
  });
  observer?.observe(element);window.addEventListener('resize',resizeInput);
  return()=>{observer?.disconnect();window.removeEventListener('resize',resizeInput);};
 },[resizeInput]);
 const dispatch=useCallback((event:SuggestionEvent)=>{
  if(suspension.current.active&&['accept','keydown','keyup'].includes(event.type)){
   machine.current=reduceSuggestion(machine.current,{type:'pointer'}).state;redraw(x=>x+1);return;
  }
  const transition=reduceSuggestion(machine.current,event);machine.current=transition.state;redraw(x=>x+1);
  const effect=transition.effect;if(!effect)return;
  if(effect.type==='clear'){setDraft('');return;}
  const captured=currentRecord.current;if(!captured)return;
  const key=suggestionContextKey(effect.context),token=key+':'+effect.suggestionId,edit=machine.current.acceptedEdit,suspensionRevision=suspension.current.revision;if(pending.current.has(token))return;pending.current.add(token);
  void api<SuggestRecord>(`/projects/${captured.scope.project_id}/suggestions/${captured.key}/accept`,{method:'POST',body:JSON.stringify({suggestion_id:effect.suggestionId,expected_scope:captured.scope})}).then(accepted=>{
   if(!live.current||suspension.current.active||suspension.current.revision!==suspensionRevision||!machine.current.enabled||machine.current.phase!=='accepted'||machine.current.acceptedEdit!==edit||suggestionContextKey(machine.current.context)!==key||draftRef.current!==effect.previousText)return;
   if(accepted.state!=='accepted'||accepted.fill_text!==effect.text)throw new Error('这条建议已更新，请重新选择。');
   setDraft(effect.text);input.current?.focus();requestAnimationFrame(()=>input.current?.setSelectionRange(effect.selectionStart,effect.selectionEnd));
  }).catch(error=>{if(live.current&&!suspension.current.active&&suspension.current.revision===suspensionRevision&&suggestionContextKey(machine.current.context)===key){machine.current=reduceSuggestion(machine.current,{type:'input',value:draftRef.current}).state;machine.current=reduceSuggestion(machine.current,{type:'hide'}).state;redraw(x=>x+1);onError(messageOf(error));}}).finally(()=>{pending.current.delete(token);});
 },[setDraft,onError]);
 useEffect(()=>{live.current=true;return()=>{live.current=false;};},[]);
 useEffect(()=>{dispatch({type:'input',value:draft});},[draft,dispatch]);
 useEffect(()=>{const blur=()=>dispatch({type:'focus',focused:false});const focus=()=>dispatch({type:'focus',focused:document.activeElement===input.current});window.addEventListener('blur',blur);window.addEventListener('focus',focus);return()=>{window.removeEventListener('blur',blur);window.removeEventListener('focus',focus);};},[dispatch]);
 useEffect(()=>{const listener=()=>dispatch({type:'visibility',visible:document.visibilityState==='visible'});document.addEventListener('visibilitychange',listener);return()=>document.removeEventListener('visibilitychange',listener);},[dispatch]);
 useEffect(()=>{
  setRecord(null);const controller=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined;
  const context:SuggestionContext=answer?{answerId:answer.id,branchId:answer.id,contextRevision:answer.context_revision,sourceRevisions:Object.fromEntries(answer.sources.map(s=>[s.id,[s.sha256,s.parser_version,s.revision,s.semantic_revision].join(':')]))}:EMPTY;
  dispatch({type:'context',context});dispatch({type:'hide'});
  if(suggestionsSuspended||!answer||!['completed','insufficient_evidence'].includes(answer.status)||answer.scope_stale)return()=>controller.abort();
  const project=answer.project_id,requestId=crypto.randomUUID(),preferenceSequence=preferenceRequest.current;dispatch({type:'prepare',requestId});
  const receive=(r:SuggestRecord)=>{
   if(controller.signal.aborted)return;setRecord(r);
   if(r.state==='preparing'){timer=setTimeout(()=>void api<SuggestRecord>(`/projects/${project}/suggestions/${r.key}`,{signal:controller.signal}).then(receive).catch(()=>{}),500);return;}
   const next=r.suggestions?.find(s=>s.action==='ask');
   if(r.state==='ready'&&next)dispatch({type:'receive',requestId,suggestion:{id:next.id,text:next.text,context}});else dispatch({type:'hide'});
  };
  void api<{enabled:boolean}>(`/projects/${project}/suggestion-preference`,{signal:controller.signal}).then(p=>{if(!controller.signal.aborted&&preferenceSequence===preferenceRequest.current){setEnabled(p.enabled);dispatch({type:'enabled',enabled:p.enabled});}}).catch(()=>{});
  void api<SuggestRecord>(`/projects/${project}/answers/${answer.id}/suggestions`,{method:'POST',body:JSON.stringify({branch_id:answer.id}),signal:controller.signal}).then(receive).catch(()=>{if(!controller.signal.aborted)dispatch({type:'hide'});});
  return()=>{controller.abort();clearTimeout(timer);};
 },[answer?.id,answer?.status,answer?.context_revision,answer?.scope_stale,sourceKey,preferenceVersion,suggestionsSuspended,dispatch]);
 useEffect(()=>{const changed=(event:Event)=>{const detail=(event as CustomEvent<{projectId:string;enabled?:boolean}>).detail;if(detail.projectId===answer?.project_id&&detail.enabled!==undefined){preferenceRequest.current++;setEnabled(detail.enabled);dispatch({type:'enabled',enabled:detail.enabled});if(!detail.enabled){setRecord(null);dispatch({type:'hide'});}else setPreferenceVersion(v=>v+1);}};window.addEventListener('zhiheng-preferences-changed',changed);return()=>window.removeEventListener('zhiheng-preferences-changed',changed);},[answer?.project_id,dispatch]);
 const dismiss=()=>{dispatch({type:'dismiss'});if(record)void api(`/projects/${record.scope.project_id}/suggestions/${record.key}/dismiss`,{method:'POST',body:JSON.stringify({expected_scope:record.scope})}).catch(()=>{});};
 const toggle=async()=>{
  dispatch({type:'pointer'});
  if(!answer)return;const key=suggestionContextKey(machine.current.context),request=++preferenceRequest.current;
  try{const r=await api<{enabled:boolean}>(`/projects/${answer.project_id}/suggestion-preference`,{method:'PUT',body:JSON.stringify({enabled:!enabled})});if(!live.current||request!==preferenceRequest.current||suggestionContextKey(machine.current.context)!==key)return;setEnabled(r.enabled);dispatch({type:'enabled',enabled:r.enabled});if(!r.enabled){dispatch({type:'hide'});setRecord(null);}else setPreferenceVersion(v=>v+1);}
  catch(e){if(live.current&&request===preferenceRequest.current&&suggestionContextKey(machine.current.context)===key)onError(messageOf(e));}
 };
 const shortcut=(event:React.KeyboardEvent<HTMLTextAreaElement>,type:'keydown'|'keyup')=>{
  const edit=machine.current.acceptedEdit;
  if(type==='keydown'&&event.key.toLowerCase()==='z'&&(event.metaKey||event.ctrlKey)&&!event.shiftKey&&!event.altKey&&!event.nativeEvent.isComposing&&!machine.current.composing&&edit&&draft===edit.text){event.preventDefault();dispatch({type:'undo'});return;}
  dispatch({type,key:event.key,code:event.code,repeat:event.repeat,ctrlKey:event.ctrlKey,altKey:event.altKey,metaKey:event.metaKey,shiftKey:event.shiftKey,isComposing:event.nativeEvent.isComposing});
  const preferences=readDesktopPreferences();
  if(type==='keydown'&&preferences.shortcutsEnabled&&shortcutMatches({key:event.key,code:event.code,metaKey:event.metaKey,ctrlKey:event.ctrlKey,altKey:event.altKey,shiftKey:event.shiftKey,repeat:event.repeat,isComposing:event.nativeEvent.isComposing},preferences.shortcuts.send)&&!event.nativeEvent.isComposing&&!machine.current.composing&&!event.repeat&&!disabled&&draft.trim()){event.preventDefault();onSubmit();}
 };
 const processing=!!answer&&['queued','running'].includes(answer.status);
 const independentRetry=!!answer&&['failed','cancelled'].includes(answer.status),continuing=canContinueAnswer(answer);
 const showCount=draft.length>=3200;
 const statusText=answer?.scope_stale?'来源已变化，请先选择当前资料。':answer?.status==='queued'?'正在等待分析，可以先写下一问。':processing?'当前分析进行中，可以先写下一问。':independentRetry?'独立新问 · 不继承本轮条件':disabled?'暂时不能发送，已写内容仍可编辑。':continuing?`从第 ${answer?.turn_index??1} 轮继续`:'仅使用已选资料';
 const sendLabel=answer?.scope_stale?'先更新资料':answer?.status==='queued'?'排队中':processing?'分析中':disabled?'暂不可发送':independentRetry?'发送新问题':'发送问题';
 return <section className={`composer-wrap question-composer ${answer?'is-followup':'is-new-question'}`} aria-label="提问编辑区">
  <form className="composer" data-processing={processing||undefined} onSubmit={e=>{e.preventDefault();if(!disabled&&draft.trim()&&!machine.current.composing)onSubmit();}}>
   {independentRetry&&<p className="composer-restart-notice" id="composer-restart-notice">{answer?.status==='cancelled'?'本轮已停止':'本轮未完成'}，下一次发送会开启独立分析。请写明对象、时间、指标等完整条件；“其余不变”等省略表达不会继承本轮口径。</p>}
   {!suggestionsSuspended&&suggestionVisible(machine.current)&&<div className="suggestion-line"><button type="button" title={machine.current.suggestion?.text} onClick={()=>dispatch({type:'accept'})}>{machine.current.suggestion?.text}</button><button type="button" className="icon-button" aria-label="隐藏这条追问建议" onClick={dismiss}><X size={14}/></button></div>}
   <div className="composer-input-row">
    <label className="sr-only" htmlFor="question">你的问题</label>
    <textarea id="question" ref={input} value={draft} maxLength={4000} rows={1} aria-describedby={`composer-status${independentRetry?' composer-restart-notice':''}${showCount?' composer-count':''}`} placeholder={independentRetry?'独立新问：请填写完整问题和统计条件…':continuing?'继续追问，或调整统计口径…':processing?'可以先写下一问，待本轮完成后再发送…':'从资料出发，问一个具体的问题…'}
     onChange={e=>{dispatch({type:'input',value:e.target.value});setDraft(e.target.value);}}
     onFocus={()=>dispatch({type:'focus',focused:true})} onBlur={()=>dispatch({type:'focus',focused:false})}
     onCompositionStart={()=>dispatch({type:'composition',active:true})} onCompositionEnd={()=>dispatch({type:'composition',active:false})}
     onKeyDown={e=>shortcut(e,'keydown')} onKeyUp={e=>shortcut(e,'keyup')} onPointerDown={()=>dispatch({type:'pointer'})} onSelect={()=>dispatch({type:'selection'})}/>
    {processing&&onCancel?<button type="button" className="send composer-stop" onClick={onCancel}><Square size={15}/>停止</button>:<button type="submit" className="send" aria-label={sendLabel} title={disabled?statusText:undefined} disabled={disabled||!draft.trim()}><span className="composer-send-label">{sendLabel}</span><ArrowUp size={17} aria-hidden="true"/></button>}
   </div>
   <div className="composer-bottom">
    <div className="composer-guidance"><span id="composer-status" className="composer-context" role="status">{statusText}</span></div>
    <div className="composer-controls">
     {showCount&&<span id="composer-count" className={`composer-count${draft.length>=3900?' near-limit':''}`} aria-label={`已输入 ${draft.length}，最多 4000 字符`}>{draft.length}<span>/4000</span></span>}
     {machine.current.acceptedEdit&&<button className="subtle composer-undo" type="button" onClick={()=>dispatch({type:'undo'})}><Undo2 size={14}/>撤销填入</button>}
     {!suggestionsSuspended&&enabled&&record?.state==='ready'&&record.suggestions.some(s=>s.action!=='ask')&&<details className="composer-more-actions" onKeyDown={e=>{if(e.key==='Escape'){e.preventDefault();e.currentTarget.open=false;e.currentTarget.querySelector('summary')?.focus();}}}>
      <summary onClick={()=>dispatch({type:'pointer'})}>后续操作</summary><div className="follow-actions">{record.suggestions.filter(s=>s.action!=='ask').slice(0,3).map(s=><button key={s.id} type="button" onClick={event=>{const details=event.currentTarget.closest('details');if(details)details.open=false;onAction(s.action,s.payload);}}>{s.text.replace(/？$/,'')} <span aria-hidden="true">↗</span></button>)}</div>
     </details>}
     {continuing&&<details className="suggestion-settings" onKeyDown={e=>{if(e.key==='Escape'){e.preventDefault();e.currentTarget.open=false;e.currentTarget.querySelector('summary')?.focus();}}}>
      <summary onClick={()=>dispatch({type:'pointer'})}>追问设置</summary>
      <div className="suggestion-settings-body">
       <label><input type="checkbox" checked={enabled} onChange={()=>void toggle()}/>显示追问建议</label>
      </div>
     </details>}
    </div>
   </div>
  </form>
 </section>;
}
