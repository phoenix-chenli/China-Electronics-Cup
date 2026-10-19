import {createContext,useContext} from 'react';
import type {ContextSlot, ResearchAnswer} from './researchTypes';
import {formatContextValue, slotLabels} from './researchTypes';
import './ContextAudit.css';

const SourceTurns=createContext<Record<string,number>>({});
function SourceTurn({id}:{id:string}){const turns=useContext(SourceTurns);return <code title={id}>{turns[id]!==undefined?`第 ${turns[id]} 轮`:id.slice(0,10)}</code>;}

const operationLabels:Record<string,string>={add:'新增',replace:'替换',remove:'撤销',mark_unknown:'本轮未确认'};
const unresolvedLabels:Record<string,string>={
  interpretation_differs_from_user_patch:'系统理解与本轮明确修改不一致，请核对。',
  removed_condition_reintroduced:'分析中再次出现已撤销条件，尚未采纳为当前口径。',
  field_not_verified_for_current_question:'本轮未能确认此条件，不应沿用为已确认口径。',
  explicit_patch_interpretation_unverified:'你已明确此条件，但本轮分析尚未核对其含义；明确修改仍保留。',
};

export function slotOrigin(slot:Partial<ContextSlot>):string {
  if(slot.confirmed_by_user===true)return '你已明确';
  if(slot.origin==='model_interpretation'||slot.confirmed_by_user===false)return '系统理解，待核对';
  return '来源未标明，待核对';
}

function Origin({slot}:{slot:Partial<ContextSlot>}){
  return <span className="context-origin"><span>{slotOrigin(slot)}</span>{slot.source_answer_id&&
    <span>来源轮次 <SourceTurn id={slot.source_answer_id}/></span>}</span>;
}

function SlotValue({slot}:{slot?:ContextSlot|null}){
  return slot?<><span>{formatContextValue(slot.value)}</span><Origin slot={slot}/></>:<span>未设置</span>;
}

export function ContextAudit({state,onSelectField,sourceTurns={}}:{state:ResearchAnswer['context_state'];onSelectField?:(field:string)=>void;sourceTurns?:Record<string,number>}){
  const slots=Object.entries(state?.slots??{});
  const changes=state?.changes??[];
  const unknown=Object.entries(state?.unknown_fields??{});
  const unresolved=state?.unresolved??[];
  return <SourceTurns.Provider value={sourceTurns}><div className="context-audit">
    <section aria-label="当前完整口径"><h3>当前完整口径 <span>{slots.length} 项</span></h3>
      <p className="caption">下列是本分支保存的条件。系统理解与用户明确条件分别标明，不代表已核验事实。</p>
      {slots.length?<dl className="context-slot-list">{slots.map(([field,slot])=><div key={field}>
        <dt>{onSelectField?<button type="button" onClick={()=>onSelectField(field)} aria-label={`编辑${slotLabels[field]??field}`}>{slotLabels[field]??field}</button>:slotLabels[field]??field}</dt>
        <dd><SlotValue slot={slot}/></dd></div>)}</dl>:<p className="caption">当前没有已保存的有效条件；这不代表此前条件已被重新确认。</p>}
    </section>
    <section aria-label="本轮条件变化"><h3>与上一轮相比</h3>
      {changes.length?<ol className="context-change-list">{changes.map((change,index)=><li key={`${change.field}-${index}`}>
        <strong>{slotLabels[change.field]??change.field} · {operationLabels[change.operation]??'条件变化'}</strong>
        <dl><div><dt>之前</dt><dd><SlotValue slot={change.before}/></dd></div>
          <div><dt>本轮</dt><dd>{change.operation==='mark_unknown'?<span>未确认，未作为有效条件沿用</span>:<SlotValue slot={change.after}/>}</dd></div></dl>
        {change.source_answer_id&&<small>变更轮次 <SourceTurn id={change.source_answer_id}/></small>}
      </li>)}</ol>:<p className="caption">本轮没有记录到条件变化；历史记录缺少差异时不会猜测补全。</p>}
      <p className="caption">这里只展示保存的条件差异，未据此推算数值变化。</p>
    </section>
    {(unknown.length>0||unresolved.length>0)&&<section className="context-unresolved" aria-label="尚未确认的口径"><h3>尚待核对</h3>
      <ul>{unknown.map(([field,item])=><li key={`unknown-${field}`}><strong>{slotLabels[field]??field}</strong>：本轮未确认，未作为有效条件沿用。
        {item.previous_source_answer_id&&<small>此前来源轮次 <SourceTurn id={item.previous_source_answer_id}/></small>}</li>)}
      {unresolved.map((item,index)=><li key={`unresolved-${index}`}><strong>{slotLabels[item.field]??item.field}</strong>：{unresolvedLabels[item.kind]??'记录了尚未解决的口径问题，请在下一问中明确。'}
        {item.source_answer_id&&<small>相关轮次 <SourceTurn id={item.source_answer_id}/></small>}</li>)}</ul>
    </section>}
  </div></SourceTurns.Provider>;
}
