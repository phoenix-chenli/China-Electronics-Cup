import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {StructuredTable} from '../src/document-processing'

const table={id:'INTERNAL_TABLE_ID',title:'销售表',status:'confirmed',rows:3,columns:2,cells:[
 {id:'INTERNAL_HEADER_ID',row:0,column:0,colspan:2,text:'年度收入',role:'column_header'},
 {row:1,column:0,rowspan:2,text:'华北',role:'row_header'},
 {row:1,column:1,text:'0'},
 {row:2,column:1,text:''}
]}

test('actual pending table relationships explain how to resolve positional conflicts',()=>{
 const html=renderToStaticMarkup(<StructuredTable table={{...table,relation_status:'pending',relation_audit:{geometry:{issues:[{axis:'x',boundary:1,reason:'INTERNAL_GEOMETRY_DETAIL'}]}}}}/> )
 for(const text of ['行列对应关系待复核','行列位置存在不一致','对照原表或重新识别','只修正文字不会调整行列位置'])assert.ok(html.includes(text),text)
 assert.ok(html.includes('rowSpan="2"')||html.includes('rowspan="2"'))
 assert.ok(html.includes('colSpan="2"')||html.includes('colspan="2"'))
 assert.equal((html.match(/华北/g)||[]).length,1)
 assert.ok(html.includes('<span>0</span>'));assert.ok(html.includes('<span></span>'))
 for(const privateDetail of ['INTERNAL_TABLE_ID','INTERNAL_HEADER_ID','INTERNAL_GEOMETRY_DETAIL','relation_status'])assert.ok(!html.includes(privateDetail),privateDetail)
})

test('partial observed coverage displays a local missing-grid warning independently of other checks',()=>{
 const html=renderToStaticMarkup(<StructuredTable table={{...table,relation_status:'consistent',relation_audit:{coverage_status:'partial',missing_slots:1}}}/> )
 for(const text of ['表格局部网格缺失','部分表头、行名与数值的对应关系尚未确认','对照原表核对缺失部分'])assert.ok(html.includes(text),text)
 assert.ok(!html.includes('行列位置存在不一致'))
 assert.ok(!html.includes('missing_slots'))
})

test('complete ordinary tables and pending text alone do not fabricate geometric problems',()=>{
 for(const state of [undefined,'confirmed','pending']){
  const html=renderToStaticMarkup(<StructuredTable table={{...table,status:state,relation_status:'consistent',relation_audit:{coverage_status:'complete'}}}/> )
  for(const text of ['行列位置存在不一致','表格局部网格缺失','当前仅为候选'])assert.ok(!html.includes(text),text)
 }
})

test('cross-page candidates never display an established merge or internal matching details',()=>{
 const html=renderToStaticMarkup(<StructuredTable table={{...table,continuations:[{status:'candidate',previous_table_id:'PRIVATE_PREVIOUS_ID',reason:'PRIVATE_MATCHING_DETAIL'}]}}/> )
 for(const text of ['可能的跨页续表','当前仅为候选，尚未确认','各段保留各自的来源'])assert.ok(html.includes(text),text)
 for(const text of ['已合并','PRIVATE_PREVIOUS_ID','PRIVATE_MATCHING_DETAIL'])assert.ok(!html.includes(text),text)
 const absent=renderToStaticMarkup(<StructuredTable table={{...table,continuations:[]}}/> )
 assert.ok(!absent.includes('当前仅为候选'))
})
