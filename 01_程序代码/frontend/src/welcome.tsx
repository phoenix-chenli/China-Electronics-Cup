import React,{useEffect,useRef,useState} from 'react'
import {ArrowRight,ArrowUpRight} from 'lucide-react'

export function welcomeWasSeen(){try{return localStorage.getItem('ef-welcome-seen-v1')==='yes'}catch{return false}}

export function Welcome({motion,onClose}:{motion:boolean;onClose:()=>void}){
 const [leaving,setLeaving]=useState(false)
 const [reduced,setReduced]=useState(()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches)
 const gate=useRef<HTMLDivElement>(null),mark=useRef<HTMLImageElement>(null),entry=useRef<HTMLButtonElement>(null)
 const timer=useRef(0),frame=useRef(0),exiting=useRef(false),exitRef=useRef<()=>void>(()=>{}),closeRef=useRef(onClose)
 closeRef.current=onClose
 const animated=motion&&!reduced

 const enter=()=>{
  if(exiting.current)return
  exiting.current=true
  try{localStorage.setItem('ef-welcome-seen-v1','yes')}catch{}
  if(!animated){closeRef.current();return}
  const destination=Array.from(document.querySelectorAll<HTMLElement>('[data-brand-destination], .ef-brand .brand-symbol')).find(el=>{
   const rect=el.getBoundingClientRect()
   return rect.width>0&&rect.height>0&&rect.top>=0&&rect.left>=0
  })
  const from=mark.current?.getBoundingClientRect(),to=destination?.getBoundingClientRect()
  if(from&&to&&gate.current){
   gate.current.style.setProperty('--ef-arrival-mark-x',`${to.left+to.width/2-from.left-from.width/2}px`)
   gate.current.style.setProperty('--ef-arrival-mark-y',`${to.top+to.height/2-from.top-from.height/2}px`)
   gate.current.style.setProperty('--ef-arrival-mark-scale',String(to.width/from.width))
  }
  setLeaving(true)
  timer.current=window.setTimeout(()=>closeRef.current(),720)
 }
 exitRef.current=enter

 useEffect(()=>{
  const previous=document.activeElement as HTMLElement|null,overflow=document.body.style.overflow
  document.body.style.overflow='hidden'
  entry.current?.focus({preventScroll:true})
  const mq=window.matchMedia('(prefers-reduced-motion: reduce)'),change=()=>setReduced(mq.matches)
  mq.addEventListener('change',change)
  const key=(e:KeyboardEvent)=>{
   if(e.key==='Escape'){e.preventDefault();e.stopPropagation();exitRef.current()}
   if(e.key==='Tab'){e.preventDefault();entry.current?.focus({preventScroll:true})}
  }
  document.addEventListener('keydown',key,true)
  return()=>{
   document.body.style.overflow=overflow
   clearTimeout(timer.current)
   cancelAnimationFrame(frame.current)
   mq.removeEventListener('change',change)
   document.removeEventListener('keydown',key,true)
   requestAnimationFrame(()=>{
    const mobile=window.matchMedia('(max-width:760px), (pointer:coarse)').matches
    const visiblePrevious=previous?.isConnected&&previous!==document.body&&!previous.closest('[inert]')&&!previous.matches(':disabled')&&previous.getClientRects().length>0&&getComputedStyle(previous).visibility!=='hidden'
    const previousEditsText=previous?.matches('input,textarea,select,[contenteditable="true"]')
    const main=document.querySelector<HTMLElement>('#workspace-main')
    const target=visiblePrevious&&(!mobile||!previousEditsText)?previous:mobile?main:document.querySelector<HTMLTextAreaElement>('.query-input textarea:not(:disabled)')||main
    target?.focus({preventScroll:true})
   })
  }
 },[])

 useEffect(()=>{
  if(!animated){
   gate.current?.style.setProperty('--ef-arrival-scene-x','0px')
   gate.current?.style.setProperty('--ef-arrival-scene-y','0px')
   if(leaving){clearTimeout(timer.current);closeRef.current()}
  }
 },[animated,leaving])

 const move=(e:React.PointerEvent<HTMLDivElement>)=>{
  if(!animated||exiting.current||e.pointerType!=='mouse')return
  const rect=e.currentTarget.getBoundingClientRect(),x=(e.clientX-rect.left)/rect.width-.5,y=(e.clientY-rect.top)/rect.height-.5
  cancelAnimationFrame(frame.current)
  frame.current=requestAnimationFrame(()=>{
   gate.current?.style.setProperty('--ef-arrival-scene-x',`${x*18}px`)
   gate.current?.style.setProperty('--ef-arrival-scene-y',`${y*14}px`)
  })
 }
 const reset=()=>{
  cancelAnimationFrame(frame.current)
  gate.current?.style.setProperty('--ef-arrival-scene-x','0px')
  gate.current?.style.setProperty('--ef-arrival-scene-y','0px')
 }

 return <div ref={gate} className={'ef-arrival'+(leaving?' ef-arrival--leaving':'')+(!animated?' ef-arrival--still':'')} onPointerMove={move} onPointerLeave={reset} role="dialog" aria-modal="true" aria-labelledby="ef-arrival-title" aria-describedby="ef-arrival-description">
  <div className="ef-arrival-surface" aria-hidden="true"/>
  <header className="ef-arrival-header">
   <div className="ef-arrival-brand"><img ref={mark} src="/brand-icon.svg" alt=""/><span>证流 <i>/</i> EvidenceFlow</span></div>
   <span className="ef-arrival-local"><span/>本地分析工作空间<ArrowUpRight size={13}/></span>
  </header>
  <main className="ef-arrival-main">
   <div className="ef-arrival-story">
    <div className="ef-arrival-eyebrow"><span>01 — 从事实出发</span><i/></div>
    <h1 id="ef-arrival-title"><span>让每个答案，</span><span>都有据可循<span className="ef-arrival-period">。</span></span></h1>
    <p id="ef-arrival-description">把数据与规则连接起来。<br/>从一个问题，走向清晰的判断。</p>
   </div>
   <figure className="ef-arrival-visual" aria-hidden="true">
    <div className="ef-arrival-aura"/>
    <div className="ef-arrival-orbit ef-arrival-orbit--outer"/>
    <div className="ef-arrival-orbit ef-arrival-orbit--inner"/>
    <div className="ef-arrival-sculpture"><img src="/prism-knot.webp" alt="" fetchPriority="high"/></div>
    <div className="ef-arrival-cross ef-arrival-cross--one"/>
    <div className="ef-arrival-cross ef-arrival-cross--two"/>
    <figcaption><span>联结事实</span><i/><span>看见答案</span></figcaption>
   </figure>
   <div className="ef-arrival-action">
    <button ref={entry} className="ef-arrival-enter" onClick={enter} disabled={leaving}><span>进入工作台</span><span className="ef-arrival-enter-icon"><ArrowRight size={19}/></span></button>
    <span className="ef-arrival-hint"><kbd>Esc</kbd> 随时跳过开场</span>
   </div>
  </main>
  <footer className="ef-arrival-footer">
   <div className="ef-arrival-connections"><span><i>01</i> 数据</span><b/><span><i>02</i> 规则</span><b/><span><i>03</i> 来源</span></div>
   <span className="ef-arrival-footnote">每一次探索，都有迹可循。</span>
  </footer>
 </div>
}
