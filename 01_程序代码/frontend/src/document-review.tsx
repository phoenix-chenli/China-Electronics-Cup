import React,{useEffect,useState} from 'react'
import {api,Modal,Obj,Spinner} from './ui'
import {documentPosition,processingDocumentId} from './document-processing'

export function DocumentReviewDialog({document:doc,page,parseId,close,onCompleted}:Obj){
 const documentId=processingDocumentId(doc)
 const [items,setItems]=useState<Obj[]>([]),[history,setHistory]=useState<Obj[]>([]),[loading,setLoading]=useState(true),[error,setError]=useState('')
 const [target,setTarget]=useState(''),[draft,setDraft]=useState<Obj>({}),[operator,setOperator]=useState('本机用户'),[accepted,setAccepted]=useState(false),[busy,setBusy]=useState(false),[saved,setSaved]=useState<Obj|null>(null)
 const item=items.find(x=>x.id===target)
 const select=(next:Obj)=>{setTarget(next.id);setDraft({...next});setAccepted(false);setError('')}
 useEffect(()=>{let live=true;api('/api/documents/'+documentId+'/review?page='+page+'&version='+doc.sha256+'&parse_id='+parseId).then(r=>{if(live){setItems(r.targets);setHistory(r.reviews);const first=r.targets.find((x:Obj)=>x.status==='pending')||r.targets[0];if(first)select(first)}}).catch(e=>live&&setError(e.message)).finally(()=>live&&setLoading(false));return()=>{live=false}},[documentId,doc.sha256,parseId,page])
 const change=(key:string,value:string)=>{setDraft(old=>({...old,[key]:value}));setAccepted(false)}
 const image=['pdf','png','jpg','jpeg','webp'].includes(doc.format)?'/api/documents/'+documentId+'/pages/'+page+'/image?version='+doc.sha256:item?.media_id?'/api/documents/'+documentId+'/media/'+item.media_id+'?version='+doc.sha256:null
 const submit=async(e:React.FormEvent)=>{e.preventDefault();if(!item||!accepted)return;setBusy(true);setError('');try{const value=await api('/api/documents/'+documentId+'/review',{version:doc.sha256,parse_id:parseId,page,target,operator,accepted:true,...Object.fromEntries(['text','label','series_name','unit','title','x_label','y_label'].map(k=>[k,draft[k]||'']))});setSaved(value);onCompleted(value)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
 return <Modal title="对照原件复核" close={close} className="document-review-dialog">
  <p>{doc.name} · {documentPosition(doc.format,page)}</p><p className="muted">确认的内容会保存为新解析；历史答案继续引用之前的版本。</p>
  {loading?<Spinner text="读取固定解析与待复核内容"/>:saved?<><p role="status">已保存复核记录。操作者：{operator}。</p>{saved.index_error&&<p className="amber-text">{saved.index_error}</p>}<button className="button primary" onClick={close}>返回原文</button></>:items.length?<form onSubmit={submit} onKeyDown={e=>{if(e.key==='Enter'&&e.nativeEvent.isComposing)e.preventDefault()}}>
   <label className="form-field">复核对象<select value={target} onChange={e=>select(items.find(x=>x.id===e.target.value)!)}>{items.map((x,i)=><option key={x.id} value={x.id}>{x.status==='pending'?'待复核 · ':''}{x.kind==='chart_point'?'图点':x.kind==='cell'?'单元格':'文字'} {i+1} · {x.label||String(x.text).slice(0,45)}</option>)}</select></label>
   <div className="review-compare">{image?<figure><img src={image} alt="固定版本原件，用于核对当前内容"/><figcaption>原件图像{item?.box?' · 对象有来源区域':''}</figcaption></figure>:<div><h4>修改前的固定解析</h4><pre>{item?.text||'空白'}</pre></div>}<div><label className="form-field">{item?.kind==='chart_point'?'原件中可核对的数值':'修正后的文字'}<textarea value={draft.text||''} onChange={e=>change('text',e.target.value)} rows={5} maxLength={4000}/></label>{item?.kind==='cell'&&<p className="muted">原件确为空白时保留空白；数字零请填写 0。</p>}
   {item?.kind==='chart_point'&&[['label','类别 / 时间'],['series_name','对应系列'],['unit','单位'],['title','图表标题'],['x_label','横轴'],['y_label','纵轴']].map(([key,label])=><label key={key} className="form-field">{label}<input value={draft[key]||''} required onChange={e=>change(key,e.target.value)}/></label>)}</div></div>
   <label className="form-field">操作者标识<input value={operator} onChange={e=>setOperator(e.target.value)} required maxLength={80}/></label><label className="review-confirm"><input type="checkbox" checked={accepted} onChange={e=>setAccepted(e.target.checked)}/>我已对照原件确认所填文字、数值及对应关系</label>
   <button className="button primary" disabled={busy||!accepted||!operator.trim()}>{busy?'正在保存新解析':'确认并保存新解析'}</button>
  </form>:<p className="muted">本页没有可复核对象。请先完成文字识别或结构提取。</p>}
  {error&&<p className="error-text" role="alert">{error}</p>}{history.length>0&&<details><summary>本页复核记录 · {history.length}</summary>{history.map((r,i)=><div key={i}><p>{r.operator} · {new Date(r.time).toLocaleString('zh-CN')}</p><pre>{r.before.text} → {r.after.text}</pre></div>)}</details>}
 </Modal>
}
