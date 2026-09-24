import assert from 'node:assert/strict'
import {test} from 'node:test'
import {existsSync} from 'node:fs'
import {petStates,petAsset,resolvePetState,taskPose,fitPet,shouldSendKey} from '../../../01_程序代码/frontend/src/pet-state.ts'
test('14 distinct real assets with state keys',()=>{assert.equal(petStates.length,14);assert.equal(new Set(petStates.map(s=>petAsset(s[0]))).size,14);for(const [s] of petStates)assert.ok(existsSync('01_程序代码/frontend/public'+petAsset(s)))})
test('real task phases map to specific poses',()=>{for(const [phase,pose] of [['确认问题与条件','think'],['查找文档','search'],['读取当前文档','read'],['计算奖金','calculate']])assert.equal(taskPose(phase),pose)})
test('priority cancels fake success for errors and gaps, drag overrides',()=>{assert.equal(resolvePetState({success:true,clarify:true}),'clarify');assert.equal(resolvePetState({success:true,error:true}),'error');assert.equal(resolvePetState({dragging:true,error:true}),'drag');assert.equal(resolvePetState({error:true,working:true,stage:'查找文档'}),'search')})
test('idle sleep listening and explicit poses have reachable behavior',()=>{assert.equal(resolvePetState({}),'idle');assert.equal(resolvePetState({sleeping:true}),'sleep');assert.equal(resolvePetState({sleeping:true,listening:true}),'listen');for(const manual of ['welcome','explain_left','explain_right','read','success'] as const)assert.equal(resolvePetState({manual}),manual)})
test('drag position remains inside desktop and narrow viewports',()=>{for(const w of [390,768,1024,1280,1440,1586])for(const [x,y] of [[-100,-100],[9999,9999],[250,200]]){const p=fitPet(x,y,w,844);assert.ok(p.x>=8&&p.x+120<=w-8);assert.ok(p.y>=64&&p.y+156<=844)}})

test('IME candidate confirmation and Shift Enter never submit',()=>{assert.equal(shouldSendKey('Enter',false,true),false);assert.equal(shouldSendKey('Enter',false,false,229),false);assert.equal(shouldSendKey('Enter',true,false),false);assert.equal(shouldSendKey('Enter',false,false),true)})
