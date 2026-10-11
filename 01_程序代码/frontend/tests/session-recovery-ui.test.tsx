import assert from 'node:assert/strict'
import {test} from 'node:test'
import {api} from '../src/ui'
import {sessionPollDelay,withSubmittedTask} from '../src/conversations'

function stalled(signal?:AbortSignal|null):Promise<never>{return new Promise((_,reject)=>{
 const abort=()=>reject(new DOMException('aborted','AbortError'))
 if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true})
})}

test('read timeout aborts a stalled response without replaying any request',async()=>{
 const original=globalThis.fetch;let calls=0
 globalThis.fetch=(async(_url,init)=>{calls++;return stalled(init?.signal)}) as typeof fetch
 try{
  await assert.rejects(api('/api/sessions/one',undefined,undefined,{timeoutMs:15}),/响应超时/)
  assert.equal(calls,1)
 }finally{globalThis.fetch=original}
})

test('deadline includes body reading and preserves a submitted task request id',async()=>{
 const original=globalThis.fetch;const calls:RequestInit[]=[]
 globalThis.fetch=(async(_url,init)=>{
  calls.push(init||{});return {ok:true,json:()=>stalled(init?.signal)} as unknown as Response
 }) as typeof fetch
 try{
  await assert.rejects(api('/api/tasks',{request_id:'same-request',question:'public question'},undefined,{timeoutMs:15}),/响应超时/)
  assert.equal(calls.length,1)
  assert.equal(calls[0].method,'POST');assert.equal(JSON.parse(String(calls[0].body)).request_id,'same-request')
 }finally{globalThis.fetch=original}
})

test('leaving a session aborts only its read and does not claim a connection failure',async()=>{
 const original=globalThis.fetch;const controller=new AbortController();let calls=0
 globalThis.fetch=(async(_url,init)=>{calls++;return stalled(init?.signal)}) as typeof fetch
 try{
  const reading=api('/api/sessions/old',undefined,undefined,{timeoutMs:5000,signal:controller.signal})
  controller.abort()
  await assert.rejects(reading,(error:unknown)=>error instanceof DOMException&&error.name==='AbortError')
  assert.equal(calls,1)
 }finally{globalThis.fetch=original}
})

test('successful JSON and server errors preserve the API contract',async()=>{
 const original=globalThis.fetch
 try{
  globalThis.fetch=(async()=>new Response(JSON.stringify({id:'accepted'}))) as typeof fetch
  assert.equal((await api('/api/tasks',{},undefined,{timeoutMs:100})).id,'accepted')
  globalThis.fetch=(async()=>new Response(JSON.stringify({detail:'任务队列已满'}),{status:429})) as typeof fetch
  await assert.rejects(api('/api/tasks',{},undefined,{timeoutMs:100}),/任务队列已满/)
  globalThis.fetch=(async()=>{throw new TypeError('network down')}) as typeof fetch
  await assert.rejects(api('/api/sessions/one',undefined,undefined,{timeoutMs:100}),/本地服务未连接/)
 }finally{globalThis.fetch=original}
})

test('reconnect waits grow to a bound and reset after a successful read',()=>{
 assert.deepEqual([0,1,2,3,4,8,100].map(sessionPollDelay),[700,1000,2000,4000,8000,8000,8000])
 assert.equal(sessionPollDelay(0),700)
})

test('accepted task survives a later failed read while previous answers remain intact',()=>{
 const old={id:'session',selected_message_id:'previous-answer',messages:[{id:'previous-answer',result:{answer:'prior supported answer'}}],tasks:[{id:'old',status:'completed'}]}
 const task={id:'new',request_id:'same-request',status:'running',stage:'读取原件'}
 const next=withSubmittedTask(old,task)
 assert.equal(next.messages,old.messages);assert.equal(next.selected_message_id,'previous-answer')
 assert.equal(next.tasks.find((t:any)=>t.id==='new'),task)
 assert.equal(old.tasks.length,1)
 assert.equal(withSubmittedTask(next,{...task,status:'cancelling'}).tasks.length,2)
})
