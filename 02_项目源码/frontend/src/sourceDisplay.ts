import type {Source} from './types';

// Source lists have format metadata but do not carry the parsed page objects.
export function sourcePositionUnit(source:Pick<Source,'format'|'kind'>){
 if(['docx','txt','md'].includes(source.format.toLowerCase()))return '段';
 return source.kind==='image'?'张':'页';
}
export function sourceExtent(source:Pick<Source,'format'|'kind'|'page_count'|'table_count'>){
 return [source.page_count?`${source.page_count} ${sourcePositionUnit(source)==='段'?'个逻辑分段':sourcePositionUnit(source)}`:'',source.table_count?`${source.table_count} 张表`:''].filter(Boolean).join(' · ')||'内容处理中';
}
export function sourceSize(bytes:number){
 if(bytes<1024)return `${bytes} B`;
 if(bytes<1024*1024)return `${(bytes/1024).toFixed(1)} KB`;
 return `${(bytes/(1024*1024)).toFixed(1)} MB`;
}
