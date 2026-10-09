import React from 'react'
import {PetState} from './pet-state'
import './pet-blob.css'

/** 证流伙伴「小察」：SVG 绘制，CSS 驱动姿态动画。 */
export function PetBlob({pose='idle',size=120,backdrop=true,label}:{pose?:PetState|string,size?:number,backdrop?:boolean,label?:string}){
 return <span className={'pet-blob pose-'+pose+(backdrop?' with-backdrop':'')} style={{width:size,height:size}} role="img" aria-label={label||'证流伙伴'}>
  {backdrop&&<span className="pet-blob-bg"/>}
  <svg viewBox="0 0 120 120" width={size} height={size} aria-hidden="true">
   <ellipse className="pet-shadow" cx="60" cy="103" rx="26" ry="5"/>
   <g className="pet-body-wrap">
    {/* 触角 */}
    <path className="pet-antenna" d="M60 34 C 58 22 50 20 46 14 C 42 8 48 2 54 6 C 58 9 56 15 51 14" fill="none" strokeWidth="6.5" strokeLinecap="round"/>
    {/* 身体 */}
    <path className="pet-body" d="M60 32 C 82 32 96 48 96 68 C 96 90 80 102 60 102 C 40 102 24 90 24 68 C 24 48 38 32 60 32 Z"/>
    {/* 眼睛 */}
    <g className="pet-eyes">
     <ellipse className="pet-eye pet-eye-l" cx="48" cy="66" rx="5.4" ry="9"/>
     <ellipse className="pet-eye pet-eye-r" cx="72" cy="66" rx="5.4" ry="9"/>
    </g>
    {/* 思考时的省略号 */}
    <g className="pet-think-dots">
     <circle cx="46" cy="88" r="3"/><circle cx="60" cy="90" r="3.6"/><circle cx="74" cy="88" r="3"/>
    </g>
    {/* 成功时的弧线嘴 */}
    <path className="pet-smile" d="M48 82 Q 60 92 72 82" fill="none" strokeWidth="4" strokeLinecap="round"/>
    {/* 错误时的皱眉 */}
    <path className="pet-frown" d="M48 88 Q 60 80 72 88" fill="none" strokeWidth="4" strokeLinecap="round"/>
   </g>
  </svg>
 </span>
}
