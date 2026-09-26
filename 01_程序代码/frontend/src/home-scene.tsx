import React,{useEffect,useRef,useState} from 'react'
import {Database,FileText,Waypoints} from 'lucide-react'
import './home-scene.css'

/** A decorative scene: no requests, business state, or interactive controls. */
export function HomeScene({motion,focused=false}:{motion:boolean;focused?:boolean}){
 const scene=useRef<HTMLDivElement>(null)
 const [reduced,setReduced]=useState(()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches)
 const [finePointer,setFinePointer]=useState(()=>window.matchMedia('(hover: hover) and (pointer: fine)').matches)
 const [inView,setInView]=useState(false)
 const [visible,setVisible]=useState(()=>document.visibilityState==='visible')
 const animated=motion&&!reduced,active=animated&&inView&&visible

 useEffect(()=>{
  const reduction=window.matchMedia('(prefers-reduced-motion: reduce)'),pointer=window.matchMedia('(hover: hover) and (pointer: fine)')
  const onReduction=()=>setReduced(reduction.matches),onPointer=()=>setFinePointer(pointer.matches),onVisibility=()=>setVisible(document.visibilityState==='visible')
  reduction.addEventListener('change',onReduction)
  pointer.addEventListener('change',onPointer)
  document.addEventListener('visibilitychange',onVisibility)
  let observer:IntersectionObserver|undefined
  if(typeof IntersectionObserver==='undefined')setInView(true)
  else{
   observer=new IntersectionObserver(entries=>setInView(entries.some(entry=>entry.isIntersecting)),{threshold:0})
   if(scene.current)observer.observe(scene.current)
  }
  return()=>{
   reduction.removeEventListener('change',onReduction)
   pointer.removeEventListener('change',onPointer)
   document.removeEventListener('visibilitychange',onVisibility)
   observer?.disconnect()
  }
 },[])

 useEffect(()=>{
  const el=scene.current
  if(!el)return
  let frame=0
  const reset=()=>{
   cancelAnimationFrame(frame)
   el.style.setProperty('--home-scene-x','0px')
   el.style.setProperty('--home-scene-y','0px')
   el.style.setProperty('--home-scene-rx','0deg')
   el.style.setProperty('--home-scene-ry','0deg')
  }
  if(!active||!finePointer){reset();return}
  const move=(event:PointerEvent)=>{
   if(event.pointerType!=='mouse')return
   const box=el.getBoundingClientRect(),x=(event.clientX-box.left)/box.width-.5,y=(event.clientY-box.top)/box.height-.5
   cancelAnimationFrame(frame)
   frame=requestAnimationFrame(()=>{
    el.style.setProperty('--home-scene-x',`${x*10}px`)
    el.style.setProperty('--home-scene-y',`${y*8}px`)
    el.style.setProperty('--home-scene-rx',`${y*-7}deg`)
    el.style.setProperty('--home-scene-ry',`${x*8}deg`)
   })
  }
  el.addEventListener('pointermove',move,{passive:true})
  el.addEventListener('pointerleave',reset)
  return()=>{
   el.removeEventListener('pointermove',move)
   el.removeEventListener('pointerleave',reset)
   reset()
  }
 },[active,finePointer])

 return <div ref={scene} className={'home-art-scene '+(animated?'has-motion':'is-static')+(active?' is-active':' is-paused')+(focused?' is-focused':'')} aria-hidden="true">
  <div className="home-art-scene__reveal">
   <div className="home-art-scene__ambient"><div className="home-art-scene__aura"/></div>
   <div className="home-art-scene__shadow"/>
   <div className="home-art-scene__parallax">
    <div className="home-art-scene__orbit home-art-scene__orbit--one"><span className="home-art-scene__track"/><span className="home-art-scene__light"/><span className="home-art-scene__satellite"/></div>
    <div className="home-art-scene__orbit home-art-scene__orbit--two"><span className="home-art-scene__track"/><span className="home-art-scene__light"/></div>
    <div className="home-art-scene__sculpture"><img src="/prism-knot.webp" alt="" draggable={false}/></div>
    <div className="home-art-scene__tag home-art-scene__tag--data"><span className="home-art-scene__tag-inner"><Database size={11}/><span>数据</span></span></div>
    <div className="home-art-scene__tag home-art-scene__tag--rules"><span className="home-art-scene__tag-inner"><Waypoints size={11}/><span>规则</span></span></div>
    <div className="home-art-scene__tag home-art-scene__tag--sources"><span className="home-art-scene__tag-inner"><FileText size={11}/><span>来源</span></span></div>
   </div>
  </div>
 </div>
}
