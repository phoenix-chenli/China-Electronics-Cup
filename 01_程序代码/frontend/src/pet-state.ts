export const petStates=[
 ['idle','待机','01-idle'],['welcome','欢迎','02-welcome'],['listen','倾听','03-listen'],['think','思考','04-thinking-v2'],
 ['search','检索','05-search'],['read','阅读','06-reading-v2'],['calculate','计算','07-calculate'],['explain_left','向左讲解','08-explain-left'],
 ['explain_right','向右讲解','09-pointing-v2'],['success','完成','10-success'],['clarify','待澄清','11-clarify'],
 ['error','未完成','12-error'],['sleep','休眠','13-sleep'],['drag','移动','14-drag']
] as const
export type PetState=typeof petStates[number][0]
export function answerNeedsContext(result?:Record<string,any>|null):boolean{
 return !!result&&(result.status==='needs_clarification'||['refused','partial','needs_clarification'].includes(result.answer_status)||result.claims?.some((claim:Record<string,any>)=>claim.kind==='gap'||claim.status==='needs_evidence')||!!result.rag&&!result.claims?.length)
}
export function petAsset(state:PetState){const item=petStates.find(s=>s[0]===state)!;return '/pet-'+item[2]+'.webp'}
export function taskPose(stage:string):PetState{
 if(/计算/.test(stage))return 'calculate'
 if(/读取|原文/.test(stage))return 'read'
 if(/查找|检索|定位数据|相关项目|匹配/.test(stage))return 'search'
 return 'think'
}
export function resolvePetState(p:{dragging?:boolean,error?:boolean,clarify?:boolean,working?:boolean,stage?:string,success?:boolean,manual?:PetState,listening?:boolean,sleeping?:boolean}):PetState{
 if(p.dragging)return 'drag'
 if(p.working)return taskPose(p.stage||'') // retry replaces an old failure, never the other way round
 if(p.error)return 'error'
 if(p.clarify)return 'clarify'
 if(p.success)return 'success'
 if(p.manual)return p.manual
 if(p.listening)return 'listen'
 return p.sleeping?'sleep':'idle'
}
export function fitPet(x:number,y:number,width:number,height:number,size=120){return {x:Math.max(8,Math.min(width-size-8,x)),y:Math.max(64,Math.min(height-size-36,y))}}
export function companionActivity(source:string,detail:Record<string,any>|null){window.dispatchEvent(new CustomEvent('ef-companion-activity',{detail:{source,...detail,clear:detail===null}}))}

export function shouldSendKey(key:string,shift:boolean,composing:boolean,keyCode=0){return key==='Enter'&&!shift&&!composing&&keyCode!==229}
