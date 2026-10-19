import type {ResearchAnswer} from './researchTypes';
import type {AnswerQuery} from './types';

export type PublishedTable = {id:string;title?:string;columns:string[];rows:unknown[][];display_rows?:unknown[][];query?:AnswerQuery;column_units?:Record<string,string|null>;evidence_ids?:string[];provenance:string;verification_status?:string};
export function fixedDecimal(value:unknown,digits:number):string|null{
 if(typeof value!=='number'&&typeof value!=='string'||!Number.isInteger(digits)||digits<0||digits>8)return null;
 const match=String(value).match(/^([+-]?)(\d+)(?:\.(\d*))?(?:e([+-]?\d+))?$/i);if(!match)return null;
 const exponent=Number(match[4]??0);if(!Number.isFinite(exponent)||Math.abs(exponent)>300)return null;
 let raw=match[2]+(match[3]??''),point=match[2].length+exponent;
 if(point<0){raw='0'.repeat(-point)+raw;point=0;}
 raw=raw.padEnd(point+digits+1,'0');
 let kept=BigInt(raw.slice(0,point+digits)||'0');if(Number(raw[point+digits])>=5)kept++;
 const text=kept.toString().padStart(digits+1,'0');return `${match[1]==='-'&&kept!==0n?'-':''}${digits?text.slice(0,-digits)+'.'+text.slice(-digits):text}`;
}
/** Formatting is a separate view layer; it never changes SQL values, sorting or export data. */
export function displayTable(table:PublishedTable,question:string):PublishedTable{
 if(table.display_rows)return table;
 const requested=question.match(/(?:保留|显示|精确到)\s*([0-8零一二两三四五六七八])\s*位小数/);if(!requested)return table;
 const numbers:Record<string,number>={零:0,一:1,二:2,两:2,三:3,四:4,五:5,六:6,七:7,八:8};const digits=numbers[requested[1]]??Number(requested[1]);
 const indexes=table.columns.map((column,i)=>({column,i})).filter(({column,i})=>!/编号|序号|代码|(?:^|_)id$|count|数量|次数|人数/i.test(column)&&(/金额|销售额|收入|价格|费用|成本|利润|总额|amount|revenue|price|cost|salary|total/i.test(column)||table.rows.some(row=>fixedDecimal(row[i],0)!==null&&String(row[i]).includes('.')))).map(item=>item.i);
 if(!indexes.length)return table;
 return {...table,display_rows:table.rows.map(row=>row.map((value,i)=>indexes.includes(i)?fixedDecimal(value,digits)??value:value))};
}
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
/** Only the server's published collection is used. Tool execution snapshots stay private. */
export function publishedTables(answer:ResearchAnswer):PublishedTable[]{
 if(answer.status!=='completed'||!answer.result)return [];
 const result=answer.result;
 const candidates:unknown[]=Array.isArray(result.result_tables)?result.result_tables:
  result.columns?.length&&Array.isArray(result.rows)?[{id:'result-main',columns:result.columns,rows:result.rows,display_rows:result.display_rows,query:result.query,column_units:result.column_units,provenance:'saved_completed_result'}]:[];
 return candidates.flatMap((value,index)=>{
  if(!record(value)||!['saved_completed_result','published_completed_result'].includes(String(value.provenance))||!Array.isArray(value.columns)||!value.columns.length||!value.columns.every(c=>typeof c==='string')||!Array.isArray(value.rows)||!value.rows.every(r=>Array.isArray(r)&&r.length===(value.columns as unknown[]).length))return [];
  const columns=value.columns as string[],rows=value.rows as unknown[][];
  const display=Array.isArray(value.display_rows)&&value.display_rows.length===rows.length&&value.display_rows.every(r=>Array.isArray(r)&&r.length===columns.length)?value.display_rows as unknown[][]:undefined;
  return [{...value,id:typeof value.id==='string'?value.id:`result-${index+1}`,columns,rows,display_rows:display,query:record(value.query)?value.query as AnswerQuery:undefined,column_units:record(value.column_units)?value.column_units as Record<string,string|null>:result.column_units,provenance:String(value.provenance)} as PublishedTable];
 });
}
export function resultCell(raw:unknown,display:unknown=raw):string{
 if(raw===null||raw===undefined)return 'NULL';
 return typeof display==='object'?JSON.stringify(display):String(display??raw);
}
/** Compare numeric cells as numbers without modifying the raw rows or fixed-decimal labels. */
export function compareCells(a:unknown,b:unknown):number{
 if(a==null||b==null)return a==null?(b==null?0:1):-1;
 const numeric=(value:unknown)=>typeof value==='number'?Number.isFinite(value):typeof value==='string'&&/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim());
 if(numeric(a)&&numeric(b)){
  const normalize=(value:unknown)=>{const text=String(value).trim().toLowerCase();const negative=text.startsWith('-');const [base,exponent='0']=text.replace(/^[+-]/,'').split('e');const [integer,fraction='']=base.split('.');const raw=(integer+fraction).replace(/^0+/,'');return {sign:raw?(negative?-1:1):0,digits:raw.replace(/0+$/,'')||'0',scale:raw.length-fraction.length+Number(exponent)};};
  const av=normalize(a),bv=normalize(b);
  if(av.sign!==bv.sign)return av.sign-bv.sign;
  if(!av.sign)return 0;
  if(av.scale!==bv.scale)return av.sign*(av.scale-bv.scale);
  const width=Math.max(av.digits.length,bv.digits.length),left=av.digits.padEnd(width,'0'),right=bv.digits.padEnd(width,'0');
  return left===right?0:av.sign*(left<right?-1:1);
 }
 return String(a).localeCompare(String(b),'zh-CN',{numeric:true});
}
