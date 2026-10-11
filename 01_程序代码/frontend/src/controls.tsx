import React, {useEffect, useId, useLayoutEffect, useRef, useState} from 'react'
import {createPortal} from 'react-dom'
import {ArrowDown, ArrowUp, ArrowRight, Check, ChevronDown, MoreHorizontal, Plus, RefreshCw, SlidersHorizontal, Star, UploadCloud, X} from 'lucide-react'
import {IconButton, Modal, Obj} from './ui'

export type MenuAction = {id:string; label:string; icon?:React.ElementType; detail?:string; disabled?:boolean; reason?:string; onSelect:()=>void; group?:string}
const pinKey='ef-pinned-tools-v1'
const knownTools=['import','connect','text','search','scenario','evidence']
function readPins():string[]{try{const value=JSON.parse(localStorage.getItem(pinKey)||'[]');return Array.isArray(value)?[...new Set(value)].filter((id):id is string=>typeof id==='string'&&knownTools.includes(id)).slice(0,3):[]}catch{return []}}

export function ActionMenu({label='更多',icon:Icon=MoreHorizontal,items,className='',iconOnly=false,pins,onPin,footer}:{label?:string;icon?:React.ElementType;items:MenuAction[];className?:string;iconOnly?:boolean;pins?:string[];onPin?:(id:string)=>void;footer?:React.ReactNode}){
 const [open,setOpen]=useState(false),[position,setPosition]=useState({top:0,left:0,maxHeight:400})
 const trigger=useRef<HTMLButtonElement>(null),menu=useRef<HTMLDivElement>(null),initialFocus=useRef<'first'|'last'>('first'),id=useId()
 const close=(restore=false)=>{setOpen(false);if(restore)trigger.current?.focus()}
 useLayoutEffect(()=>{
  if(!open)return
  const place=()=>{const r=trigger.current?.getBoundingClientRect();if(!r)return;const width=Math.min(328,innerWidth-24),height=menu.current?.offsetHeight||350;const below=innerHeight-r.bottom-16,above=r.top-16;const up=below<Math.min(height,260)&&above>below;setPosition({left:Math.max(12,Math.min(r.left,innerWidth-width-12)),top:up?Math.max(12,r.top-Math.min(height,above)-8):r.bottom+8,maxHeight:Math.max(120,up?above:below)})}
  place();const frame=requestAnimationFrame(()=>{place();const options=Array.from(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)')||[]);(initialFocus.current==='last'?options[options.length-1]:options[0])?.focus()})
  const outside=(e:PointerEvent)=>{if(!trigger.current?.contains(e.target as Node)&&!menu.current?.contains(e.target as Node))close()}
  document.addEventListener('pointerdown',outside);window.addEventListener('resize',place);document.addEventListener('scroll',place,true)
  return()=>{cancelAnimationFrame(frame);document.removeEventListener('pointerdown',outside);window.removeEventListener('resize',place);document.removeEventListener('scroll',place,true)}
 },[open])
 const keys=(e:React.KeyboardEvent)=>{
  if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close(true);return}
  if(e.key==='Tab'){close(true);return}
  const options=Array.from(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)')||[]),index=options.indexOf(document.activeElement as HTMLElement)
  const next=e.key==='ArrowDown'?(index+1)%options.length:e.key==='ArrowUp'?(index-1+options.length)%options.length:e.key==='Home'?0:e.key==='End'?options.length-1:-1
  if(next>=0){e.preventDefault();options[next]?.focus()}
 }
 return <><button ref={trigger} type="button" className={'button action-menu-trigger '+(iconOnly?'icon-only ':'')+className} aria-label={label} title={label} aria-haspopup="menu" aria-expanded={open} aria-controls={open?id:undefined} onClick={()=>{initialFocus.current='first';setOpen(!open)}} onKeyDown={e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();initialFocus.current=e.key==='ArrowUp'?'last':'first';setOpen(true)}}}><Icon size={17}/>{!iconOnly&&<span>{label}</span>}{!iconOnly&&Icon!==MoreHorizontal&&<ChevronDown size={12}/>}</button>
 {open&&createPortal(<div ref={menu} id={id} role="menu" aria-label={label} className="action-menu-popover" style={position} onKeyDown={keys}>
  {items.map((item,i)=>{const ItemIcon=item.icon;return <React.Fragment key={item.id}>{item.group&&item.group!==items[i-1]?.group&&<div className="action-menu-group" role="presentation">{item.group}</div>}<div className="action-menu-row" role="presentation"><button type="button" role="menuitem" disabled={item.disabled} title={item.disabled?item.reason:undefined} onClick={()=>{close(true);item.onSelect()}}>{ItemIcon&&<ItemIcon size={17}/>}<span><strong>{item.label}</strong>{(item.detail||item.disabled&&item.reason)&&<small>{item.disabled&&item.reason||item.detail}</small>}</span></button>{onPin&&<button type="button" role="menuitem" className={'pin-control '+(pins?.includes(item.id)?'is-pinned':'')} aria-label={(pins?.includes(item.id)?'取消固定':'固定到常用')+'：'+item.label} title={pins?.includes(item.id)?'取消固定':'固定到常用'} onClick={()=>onPin(item.id)}><Star size={16} fill={pins?.includes(item.id)?'currentColor':'none'}/></button>}</div></React.Fragment>})}
  {footer&&<div className="action-menu-footer" onClick={()=>close(true)}>{footer}</div>}
 </div>,document.body)}</>
}

export function ToolShelf({items}:{items:MenuAction[]}){
 const [pins,setPins]=useState(readPins),[message,setMessage]=useState(''),[manage,setManage]=useState(false)
 useEffect(()=>{const refresh=()=>setPins(readPins());window.addEventListener('ef-pins-change',refresh);window.addEventListener('storage',refresh);return()=>{window.removeEventListener('ef-pins-change',refresh);window.removeEventListener('storage',refresh)}},[])
 useEffect(()=>{if(!message)return;const timer=setTimeout(()=>setMessage(''),3500);return()=>clearTimeout(timer)},[message])
 const save=(next:string[])=>{try{localStorage.setItem(pinKey,JSON.stringify(next));setPins(next);window.dispatchEvent(new Event('ef-pins-change'));return true}catch{setMessage('浏览器未能保存常用设置');return false}}
 const toggle=(id:string)=>{if(pins.includes(id)){if(save(pins.filter(p=>p!==id)))setMessage('已取消固定')}else if(pins.length>=3)setMessage('最多固定 3 项，可在“管理常用”中调整');else if(save([...pins,id]))setMessage('已固定到常用')}
 const move=(i:number,d:number)=>{const next=[...pins];[next[i],next[i+d]]=[next[i+d],next[i]];save(next)}
 const add=items.filter(x=>['import','connect'].includes(x.id)),tools=items.filter(x=>!['import','connect'].includes(x.id))
 return <div className="tool-shelf">
  <ActionMenu label="添加资料" icon={Plus} items={add} pins={pins} onPin={toggle}/>
  <ActionMenu label="工具" icon={SlidersHorizontal} items={tools} pins={pins} onPin={toggle} footer={<button role="menuitem" type="button" onClick={()=>setManage(true)}><Star size={15}/>管理常用 <span>{pins.length}/3</span></button>}/>
  {pins.map(id=>{const item=items.find(x=>x.id===id);if(!item)return null;const Icon=item.icon;return <button key={id} type="button" className="pinned-tool" disabled={item.disabled} title={item.disabled?item.reason:item.label} onClick={item.onSelect}>{Icon&&<Icon size={15}/>}<span>{item.label}</span></button>})}
  {message&&<span className="tool-feedback" role="status">{message}</span>}
  {manage&&<Modal title="常用工具" close={()=>setManage(false)}><p className="muted">固定最多 3 项，按你的使用习惯排列。</p><div className="pin-manager">{pins.map((id,i)=>{const item=items.find(x=>x.id===id);return <div key={id}><Star size={17}/><strong>{item?.label||id}</strong><IconButton label={'上移 '+(item?.label||id)} disabled={i===0} onClick={()=>move(i,-1)}><ArrowUp size={16}/></IconButton><IconButton label={'下移 '+(item?.label||id)} disabled={i===pins.length-1} onClick={()=>move(i,1)}><ArrowDown size={16}/></IconButton><IconButton label={'取消固定 '+(item?.label||id)} onClick={()=>toggle(id)}><X size={16}/></IconButton></div>})}{!pins.length&&<p className="pin-empty">打开“工具”或“添加资料”，点击操作旁的星标即可固定。</p>}</div></Modal>}
 </div>
}

export function ImportDialog({close,upload,uploading,state,onView}:Obj){
 const file=useRef<HTMLInputElement>(null),[drag,setDrag]=useState(false)
 return <Modal title="添加资料" close={close}><p className="muted">导入到本机共享资料库，供当前工作区使用。</p><div className={'import-zone import-dialog-zone '+(drag?'dragging ':'')+(uploading?'disabled':'')} onDragOver={e=>{e.preventDefault();if(!uploading)setDrag(true)}} onDragLeave={()=>setDrag(false)} onDrop={e=>{e.preventDefault();setDrag(false);if(!uploading&&e.dataTransfer.files[0])upload(e.dataTransfer.files[0])}}><UploadCloud size={38}/><strong>{uploading?'正在读取资料':'将文件拖到这里'}</strong><small>CSV、Excel、SQLite、PDF、Word、文本或图片 · ≤ 50 MB</small><button type="button" className="button primary" disabled={uploading} onClick={()=>file.current?.click()}><Plus size={16}/>选择文件</button><input ref={file} type="file" hidden aria-label="选择导入文件" accept=".csv,.xlsx,.sqlite,.db,.md,.txt,.pdf,.docx,.png,.jpg,.jpeg,.webp" onChange={e=>{if(e.target.files?.[0])upload(e.target.files[0]);e.target.value=''}}/></div>
 {state&&<div className={'import-feedback '+state.status} role={state.status==='failed'?'alert':'status'}><strong>{state.filename}</strong><span>{state.message}</span>{state.status==='failed'?<button className="text-link" disabled={uploading} onClick={()=>upload(state.file)}><RefreshCw size={14}/>重试导入</button>:state.status!=='loading'&&<button className="text-link" onClick={onView}>在资料库中查看<ArrowRight size={14}/></button>}</div>}
 </Modal>
}
