import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import type {Components} from 'react-markdown';
import type {AnswerEvidence} from './types';
import {headingId,safeAnswerUrl} from './answerPresentation';
import 'katex/dist/katex.min.css';

type TextNode={type:string;value?:string;url?:string;children?:TextNode[]};
/** Transform only text nodes, never code, links or formula contents. */
function citationLinks(evidenceIds:Set<string>){
 return ()=> (tree:TextNode)=>{
  const walk=(node:TextNode)=>{
   if(!node.children||['link','code','inlineCode','math','inlineMath'].includes(node.type))return;
   node.children=node.children.flatMap(child=>{
    if(child.type!=='text'||!child.value){walk(child);return [child];}
    const parts:TextNode[]=[];let cursor=0;
    for(const match of child.value.matchAll(/\[([A-Za-z0-9_-]+)\]/g)){
     if(!evidenceIds.has(match[1]))continue;
     if(match.index!>cursor)parts.push({type:'text',value:child.value.slice(cursor,match.index)});
     parts.push({type:'link',url:`#citation-${match[1]}`,children:[{type:'text',value:match[1]}]});cursor=match.index!+match[0].length;
    }
    if(!parts.length)return [child];if(cursor<child.value.length)parts.push({type:'text',value:child.value.slice(cursor)});return parts;
   });
  };walk(tree);
 };
}
export function SafeAnswerMarkdown({text,prefix,evidence,onEvidence}:{text:string;prefix:string;evidence:AnswerEvidence[];onEvidence:(e:AnswerEvidence)=>void}){
 const heading=(tag:'h1'|'h2'|'h3'|'h4'|'h5'|'h6'):Components['h1']=>({node,children})=>{const Tag=tag;return <Tag id={headingId(prefix,node?.position?.start.line??0)} tabIndex={-1}>{children}</Tag>;};
 return <div className="answer-markdown"><ReactMarkdown remarkPlugins={[remarkGfm,remarkMath,citationLinks(new Set(evidence.map(e=>e.id)))]} rehypePlugins={[[rehypeKatex,{strict:'ignore',trust:false,throwOnError:false,maxExpand:1000}]]} skipHtml urlTransform={safeAnswerUrl} components={{
  h1:heading('h1'),h2:heading('h2'),h3:heading('h3'),h4:heading('h4'),h5:heading('h5'),h6:heading('h6'),
  a:({href,children})=>{const ev=href?.startsWith('#citation-')?evidence.find(e=>e.id===href.slice(10)):undefined;return ev?<button className="citation" title={ev.quote||ev.source_name} aria-label={`查看来源 ${ev.id}，${ev.source_name}`} onClick={()=>onEvidence(ev)}>{children}</button>:href?<a href={href} target="_blank" rel="noopener noreferrer">{children}</a>:<span>{children}</span>;},
  img:({alt})=><span className="answer-image-note">{alt?`图片说明：${alt}`:'图片引用'}（请在来源中查看原图）</span>,
  table:({children})=><div className="table-scroll" tabIndex={0} aria-label="答案表格，可横向滚动"><table>{children}</table></div>,
 }} children={text}/></div>;
}
