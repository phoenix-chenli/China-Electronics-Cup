import {useEffect,useRef} from 'react';
import type {ReactNode} from 'react';
import {X} from 'lucide-react';
export function ResearchDialog({title,onClose,children,wide=false,drawer=false}:{title:string;onClose:()=>void;children:ReactNode;wide?:boolean;drawer?:boolean}){
 const ref=useRef<HTMLDialogElement>(null);const closer=useRef(onClose);closer.current=onClose;
 const origin=useRef(document.activeElement instanceof HTMLElement?document.activeElement:null);
 useEffect(()=>{
  const el=ref.current;el?.showModal();
  el?.querySelector<HTMLElement>('input:not([disabled]),textarea:not([disabled]),select:not([disabled])')?.focus({preventScroll:true});
  return()=>{el?.close();requestAnimationFrame(()=>{if(document.querySelector('dialog[open],[aria-modal="true"]'))return;const target=origin.current;if(target?.isConnected&&target!==document.body&&target.getClientRects().length)target.focus({preventScroll:true});else document.getElementById('main-content')?.focus({preventScroll:true});});};
 },[]);
 return <dialog ref={ref} className={`research-dialog ${wide?'wide':''} ${drawer?'drawer':''}`} onCancel={e=>{e.preventDefault();closer.current();}} aria-label={title}>
  <header><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={20}/></button></header>{children}
 </dialog>;
}
