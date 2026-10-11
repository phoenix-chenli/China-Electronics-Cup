export function isDatabaseResult(result?:Record<string,any>|null):boolean{
 return !!result&&(result.query_spec?.metric==='database'||result.query_spec?.query_scope==='database'||!!result.database_query)
}

export function rawTableValue(value:unknown):string{
 if(value===null)return 'NULL'
 if(value===undefined)return ''
 if(typeof value==='object')return JSON.stringify(value)??''
 return String(value)
}

export function sourcePosition(locator?:Record<string,any>|null):string{
 if(!locator?.page)return ''
 const word=locator.extraction_method==='docx_native'||locator.source_refs?.some((r:Record<string,any>)=>r.source_method==='docx_native')
 return word?'逻辑节 '+locator.page:'第 '+locator.page+' 页'
}

export function savedAnalysisName(result:Record<string,any>):string{
 if(isDatabaseResult(result)||result.rag||['knowledge','hybrid'].includes(result.query_spec?.query_scope))return String(result.question||result.database_query?.source?.name||'数据库查询').slice(0,120)
 return (result.query_spec?.year??'')+' 年'+(result.query_spec?.region||'')+'经营分析'
}
