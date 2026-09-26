import React,{useEffect,useRef} from 'react'
import type {ChartProps} from './ui'
import * as echarts from 'echarts/core'
import {BarChart,LineChart} from 'echarts/charts'
import {GridComponent,TooltipComponent} from 'echarts/components'
import {CanvasRenderer} from 'echarts/renderers'
echarts.use([BarChart,LineChart,GridComponent,TooltipComponent,CanvasRenderer])
const num=(value:any)=>Number(value).toLocaleString('zh-CN',{maximumFractionDigits:2})
export default function RenderedChart({labels,values,kind='bar',height=220,label='数据图表',unit='',secondary,selectedIndex=null,onSelect}:ChartProps){
 const ref=useRef<HTMLDivElement>(null),callback=useRef(onSelect);callback.current=onSelect
 useEffect(()=>{
  if(!ref.current)return
  const el=ref.current,root=document.documentElement
  const chart=echarts.init(el,undefined,{renderer:'canvas'})
  const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)')
  const render=()=>{
   const styles=getComputedStyle(root)
   const token=(name:string,fallback:string)=>styles.getPropertyValue(name).trim()||fallback
   const blue=token('--blue','#2765df'),blueSoft=token('--blue-soft','#eaf0fe')
   const text=token('--text','#202733'),muted=token('--muted','#677180'),line=token('--line','#e4e7ec')
   const motion=root.dataset.motion!=='off'&&!reducedMotion.matches
   chart.setOption({
    animation:motion,animationDuration:motion?350:0,animationDurationUpdate:motion?220:0,
    color:[blue,blueSoft],textStyle:{fontFamily:'-apple-system, BlinkMacSystemFont, sans-serif',color:text},
    tooltip:{trigger:'axis',confine:true,backgroundColor:token('--panel-solid','#fff'),borderColor:line,borderWidth:1,padding:[10,12],textStyle:{color:text,fontSize:12},axisPointer:{lineStyle:{color:line},shadowStyle:{color:blueSoft,opacity:.5}},valueFormatter:(v:any)=>v==null?'无数据':num(v)+unit},
    grid:{top:28,left:56,right:20,bottom:40},
    xAxis:{type:'category',data:labels,axisTick:{show:false},axisLine:{lineStyle:{color:line}},axisLabel:{color:muted,fontSize:12,interval:'auto',overflow:'truncate',width:90}},
    yAxis:{type:'value',splitNumber:3,axisLabel:{color:muted,fontSize:12,formatter:(v:number)=>Math.abs(v)>=10000?num(v/10000)+'万':num(v)},splitLine:{lineStyle:{color:line,width:1,type:'dashed',opacity:.7}}},
    series:[{
     name:label,data:values.map((value,i)=>({value,itemStyle:{color:selectedIndex===null||i===selectedIndex?blue:blueSoft}})),
     type:kind,smooth:.32,symbolSize:6,barMaxWidth:34,
     itemStyle:{borderRadius:kind==='bar'?[4,4,0,0]:0},lineStyle:{color:blue,width:2.5},
     label:{show:kind==='bar'&&(el.clientWidth-76)/Math.max(1,labels.length)>Math.max(...values.map(v=>num(v||0).length))*7,position:'top',color:text,fontSize:12,formatter:(p:any)=>p.value==null?'':num(p.value)},
     areaStyle:kind==='line'?{color:blue,opacity:.06}:undefined
    },...(secondary?[{name:'对比值',type:kind,data:secondary,barMaxWidth:34,itemStyle:{color:blueSoft,borderColor:blue,borderWidth:1,borderRadius:kind==='bar'?[4,4,0,0]:0},lineStyle:{color:muted,width:2,opacity:.7}}]:[])]
   },true)
  }
  render()
  chart.on('click',(event:any)=>{if(event.componentType==='series')callback.current?.(event.dataIndex)})
  const resize=new ResizeObserver(()=>{chart.resize();render()});resize.observe(el)
  const obs=new MutationObserver(render);obs.observe(root,{attributes:true,attributeFilter:['data-theme','data-motion','style','class']})
  reducedMotion.addEventListener('change',render)
  return()=>{resize.disconnect();obs.disconnect();reducedMotion.removeEventListener('change',render);chart.dispose()}
 },[JSON.stringify(labels),JSON.stringify(values),kind,unit,JSON.stringify(secondary),selectedIndex,label])
 return <div className="chart" ref={ref} style={{height}} role="img" aria-label={label+'：'+labels.map((v,i)=>v+' '+(values[i]==null?'无数据':num(values[i])+unit)).join('；')}/>
}
