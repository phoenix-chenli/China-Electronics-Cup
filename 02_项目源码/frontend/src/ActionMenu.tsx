import {useEffect,useRef,useState} from 'react';
import type {ReactNode} from 'react';
import {MoreHorizontal} from 'lucide-react';
/** Small contextual menus close before their owner row or view disappears. */
export function ActionMenu({label,children}:{label:string;children:ReactNode}){
 const [open,setOpen]=useState(false),[above,setAbove]=useState(false);const root=useRef<HTMLDivElement>(null),trigger=useRef<HTMLButtonElement>(null);
 useEffect(()=>{if(!open)return;const outside=(event:PointerEvent)=>{if(event.target instanceof Node&&!root.current?.contains(event.target))setOpen(false);};document.addEventListener('pointerdown',outside);return()=>document.removeEventListener('pointerdown',outside);},[open]);
 return <div ref={root} className="source-menu action-menu" data-open={open} onKeyDown={event=>{
  if(event.key==='Escape'){event.stopPropagation();setOpen(false);trigger.current?.focus();}
  if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();const items=Array.from(root.current?.querySelectorAll<HTMLElement>('.action-menu-items button:not(:disabled),.action-menu-items a')??[]);if(!open){setOpen(true);requestAnimationFrame(()=>root.current?.querySelector<HTMLElement>('.action-menu-items button')?.focus());return;}const index=items.indexOf(document.activeElement as HTMLElement),next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;items[next]?.focus();}
 }}><button ref={trigger} className="source-menu-trigger icon-button" aria-label={label} aria-expanded={open} onClick={()=>{const rect=trigger.current?.getBoundingClientRect();setAbove(!!rect&&window.innerHeight-rect.bottom<300&&rect.top>300);setOpen(value=>!value);}}><MoreHorizontal size={19}/></button>{open&&<div className={`action-menu-items${above?' above':''}`} role="group" aria-label={label} onClick={event=>{if((event.target as HTMLElement).closest('button,a'))setOpen(false);}}>{children}</div>}</div>;
}
