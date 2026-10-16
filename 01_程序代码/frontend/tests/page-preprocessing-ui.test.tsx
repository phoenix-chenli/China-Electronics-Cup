import assert from 'node:assert/strict'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {PagePreprocessing} from '../src/document-processing'

test('legacy pages do not claim preprocessing was run',()=>{
 assert.equal(renderToStaticMarkup(<PagePreprocessing page={{}}/>),'')
})
test('clean pages show the actual original-only path',()=>{
 const html=renderToStaticMarkup(<PagePreprocessing page={{page_preprocessing:[{status:'completed',attempts:[{variant:'original',status:'completed'}],duration_ms:1250,quality:{flags:[]}}]}}/> )
 for(const s of ['保留原图识别','未触发额外增强','1.25 秒','原图坐标'])assert.ok(html.includes(s),s)
 assert.ok(!html.includes('增强成功'))
})
test('failed and cancelled enhancement attempts never count as completed',()=>{
 const html=renderToStaticMarkup(<PagePreprocessing page={{page_preprocessing:[{status:'cancelled',quality:{flags:['skew']},attempts:[{variant:'original',status:'completed'},{variant:'adaptive_primary',status:'failed',duration_ms:15},{variant:'adaptive_secondary',status:'cancelled'}]}]}}/> )
 for(const s of ['增强尝试 2 次，完成 0 次','倾斜','识别失败','已取消'])assert.ok(html.includes(s),s)
 assert.ok(!html.includes('位置保留原图坐标'))
})
test('conflicts and newly recovered text remain explicit review candidates',()=>{
 const html=renderToStaticMarkup(<PagePreprocessing page={{page_preprocessing:[{status:'completed',attempts:[],decisions:[{action:'literal_conflict_requires_review'},{action:'new_observation_requires_review'}]}]}}/> )
 assert.ok(html.includes('1 处字面冲突'));assert.ok(html.includes('1 处新文字候选'))
 assert.ok(html.includes('尚未进入确定证据'));assert.ok(!html.includes('正确率'))
})
