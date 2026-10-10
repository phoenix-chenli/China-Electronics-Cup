import React from 'react'
export class PageLoadBoundary extends React.Component<{children:React.ReactNode},{failed:boolean}>{
 state={failed:false}
 static getDerivedStateFromError(){return {failed:true}}
 render(){return this.state.failed?<div className="panel" role="alert"><h2>页面暂时无法打开</h2><p>请刷新以重新读取当前页面。已保存的会话和任务可继续查看。</p><button className="button" onClick={()=>window.location.reload()}>刷新页面</button></div>:this.props.children}
}
