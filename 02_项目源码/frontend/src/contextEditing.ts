type StructuredCondition=Record<string,unknown>&{table:string;column:string;value:unknown};
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value);
export function structuredConditions(value:unknown):StructuredCondition[]|null{
 return Array.isArray(value)&&value.length>0&&value.every(item=>record(item)&&typeof item.table==='string'&&typeof item.column==='string'&&['string','number','boolean'].includes(typeof item.value))?value as StructuredCondition[]:null;
}
export function contextInput(value:unknown):string{return value==null?'':typeof value==='object'?JSON.stringify(value,null,2):String(value);}
export function parseContextInput(field:string,text:string,original:unknown):unknown{
 const trimmed=text.trim();if(!trimmed)throw new Error('请填写新的条件。');
 const structured=structuredConditions(original);
 if(structured){
  let parsed:unknown;try{parsed=JSON.parse(text);}catch{throw new Error('结构化条件无法读取，请重新选择此条件。');}
  if(!Array.isArray(parsed)||parsed.length!==structured.length)throw new Error('请保留各条件的字段，逐项修改值。');
  return structured.map((item,i)=>{const next=parsed[i];if(!record(next)||next.table!==item.table||next.column!==item.column)throw new Error('条件所属表和字段不能在值编辑器中改变。');let value=next.value;
   if(typeof item.value==='number'){if(typeof value!=='number'&&typeof value!=='string'||String(value).trim()===''||!Number.isFinite(Number(value)))throw new Error(`${item.table}.${item.column} 需要有效数值。`);value=Number(value);}
   else if(typeof item.value==='boolean'){if(![true,false,'true','false'].includes(value as string|boolean))throw new Error(`${item.table}.${item.column} 只能为 true 或 false。`);value=value===true||value==='true';}
   else if(typeof value!=='string'||!value.trim())throw new Error(`${item.table}.${item.column} 不能为空。`);
   return {...item,value};
  });
 }
 const list=['entities','dimensions','filters','assumptions'].includes(field);
 if(typeof original==='object'&&original!==null||/^[\[{]/.test(trimmed)){
  let parsed:unknown;try{parsed=JSON.parse(text);}catch{throw new Error('此条件使用 JSON 结构，请检查括号、引号和值。');}
  if(list&&!Array.isArray(parsed))throw new Error('此条件需要 JSON 数组。');return parsed;
 }
 return list?trimmed.split(/[；;\n]/).map(value=>value.trim()).filter(Boolean):trimmed;
}
