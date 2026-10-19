import {useEffect,useState} from 'react';
import {buildResultChart,defaultChartOptions} from './resultChart';
import type {ResultChartKind} from './resultChart';
import type {ResearchAnswer} from './researchTypes';
import {formatValue} from './researchTypes';
import {tabStorage} from './browserStorage';

export function ResultPlot({answer}:{answer:ResearchAnswer}){
 const defaults=defaultChartOptions(answer);const columns=answer.result?.columns??[],key=`zhiheng.chart.${answer.project_id}.${answer.id}`;
 const [options,setOptions]=useState(()=>{let saved:{kind?:ResultChartKind;x?:string;y?:string}={};try{saved=JSON.parse(tabStorage.getItem(key)??'{}');}catch{/* Default to the saved result's first fields. */}return {kind:(['bar','line','scatter'].includes(saved?.kind??'')?saved.kind:'bar') as ResultChartKind,x:columns.includes(saved?.x??'')?saved.x!:defaults?.xColumn??columns[0]??'',y:columns.includes(saved?.y??'')?saved.y!:defaults?.yColumns[0]??columns[1]??''};});
 const {kind,x,y}=options;
 useEffect(()=>{tabStorage.setItem(key,JSON.stringify(options));},[key,options]);
 const chart=buildResultChart(answer,{kind,xColumn:x,yColumns:[y],allowHistorical:true});
 const displayValue=(rowIndex:number,value:unknown)=>formatValue(answer.result?.display_rows?.[rowIndex]?.[columns.indexOf(y)]??value);
 const width=680,height=300,left=65,right=25,top=30,bottom=65;
 return <div className="result-plot"><details className="answer-chart-toolbar"><summary>图表设置</summary><div className="plot-controls"><label>图形<select value={kind} onChange={e=>setOptions(v=>({...v,kind:e.target.value as ResultChartKind}))}><option value="bar">柱状图</option><option value="line">折线图</option><option value="scatter">散点图</option></select></label><label>横轴<select value={x} onChange={e=>setOptions(v=>({...v,x:e.target.value}))}>{columns.map(c=><option key={c}>{c}</option>)}</select></label><label>数值<select value={y} onChange={e=>setOptions(v=>({...v,y:e.target.value}))}>{columns.map(c=><option key={c}>{c}</option>)}</select></label></div></details>
 {chart.status==='unavailable'?<p className="notice">{chart.reason}</p>:(()=>{
 const span=chart.yDomain[1]-chart.yDomain[0];const py=(v:number)=>height-bottom-(v-chart.yDomain[0])/span*(height-top-bottom);
 const px=(i:number)=>kind==='scatter'?left+(Number(chart.points[i].x)-chart.xDomain![0])/(chart.xDomain![1]-chart.xDomain![0])*(width-left-right):left+(i+.5)/chart.points.length*(width-left-right);
 const bw=Math.min(48,(width-left-right)/chart.points.length*.62);const zero=py(0);
 const horizontal=kind==='bar'&&chart.yDomain[0]>=0&&chart.points.length<=20;
 return <>{chart.historical&&<p className="notice warning">历史图表 · 回答时保存的结果</p>}{horizontal?<div className="answer-horizontal-chart" role="img" aria-label={`${y}，按${x}展示，共${chart.rowCount}行。每一行的类别和实际值均列在图中。`}>{chart.points.map((p,i)=><div className="answer-bar-row" key={i}><span className="answer-bar-label">{String(p.originalX)}</span><span className="answer-bar-track" aria-hidden="true">{p.values[0]!==null&&<span className="answer-bar" style={{display:'block',width:`${p.values[0]/chart.yDomain[1]*100}%`}}/>}</span><span className="answer-bar-value">{displayValue(p.rowIndex,p.originalValues[0])}</span></div>)}</div>:<svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${y}，按${x}展示，共${chart.rowCount}行。完整精确数值见查询结果表。`}>
 {[0,.25,.5,.75,1].map(t=>{const v=chart.yDomain[0]+span*t;return <g key={t}><line x1={left} x2={width-right} y1={py(v)} y2={py(v)} stroke="var(--line)"/><text x={left-9} y={py(v)+4} textAnchor="end" fontSize="12" fill="var(--muted)">{Number(v.toPrecision(4))}</text></g>;})}
 <line x1={left} x2={width-right} y1={zero} y2={zero} stroke="var(--muted)"/>
 {chart.points.map((p,i)=>{const v=p.values[0];return <g key={i}>{v!==null&&(kind!=='scatter'||p.x!==null)&&(kind==='bar'?<rect x={px(i)-bw/2} y={Math.min(py(v),zero)} width={bw} height={Math.abs(zero-py(v))} rx="2" fill={i===0?'var(--accent)':'#a9b4ab'}><title>{String(p.originalX)}：{displayValue(p.rowIndex,p.originalValues[0])}</title></rect>:<><circle cx={px(i)} cy={py(v)} r="4" fill="var(--accent)"><title>{String(p.originalX)}：{displayValue(p.rowIndex,p.originalValues[0])}</title></circle>{kind==='line'&&i>0&&chart.points[i-1].values[0]!==null&&<line x1={px(i-1)} y1={py(chart.points[i-1].values[0]!)} x2={px(i)} y2={py(v)} stroke="var(--accent)" strokeWidth="2"/>}</>)}{p.x!==null&&(chart.points.length<=12||i%Math.ceil(chart.points.length/8)===0)&&<text x={px(i)} y={height-bottom+22} textAnchor="middle" fontSize="12" fill="var(--muted)"><title>{String(p.x)}</title>{String(p.x).length>12?`${String(p.x).slice(0,11)}…`:String(p.x)}</text>}</g>;})}
 <text x={left} y="15" fill="var(--muted)" fontSize="12">{y}{chart.series[0].unit?` / ${chart.series[0].unit}`:''}</text></svg>}
 <p className="answer-chart-meta">{chart.series[0].unit?`单位：${chart.series[0].unit}`:'来源未标注单位'}{chart.rowsTruncated?` · 仅显示返回的 ${chart.rowCount} 行`:''}</p><details className="answer-chart-notes"><summary>绘图与统计说明</summary>{chart.notes.map((note,i)=><p key={i}>{note}</p>)}</details></>;
 })()}
 </div>;
}
