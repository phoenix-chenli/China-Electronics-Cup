import {preferenceStorage} from './browserStorage';

export const DESKTOP_PREF_KEY='zhiheng-desktop-preferences-v1';
export const DESKTOP_PREF_EVENT='zhiheng-desktop-preferences-changed';
export const themes=[
 {id:'umber',name:'暖棕',color:'#98532d',hover:'#814424',soft:'#f5eee8',border:'#bd967a'},
 {id:'blue',name:'深蓝',color:'#245ba6',hover:'#19467f',soft:'#edf3fa',border:'#8aa9d0'},
 {id:'forest',name:'墨绿',color:'#2d6c56',hover:'#21523f',soft:'#edf5f0',border:'#8eb4a3'},
 {id:'violet',name:'紫灰',color:'#6b4c98',hover:'#533875',soft:'#f3eff8',border:'#b29bcf'},
 {id:'graphite',name:'石墨',color:'#4b5866',hover:'#35414d',soft:'#eff2f5',border:'#9ba6b3'},
] as const;
export const shortcutActions={search:'搜索工作台',send:'发送问题',newAnalysis:'新建分析',import:'导入资料',sources:'打开资料库',history:'打开分析记录'} as const;
export type ShortcutAction=keyof typeof shortcutActions;
export type DesktopPreferences={theme:typeof themes[number]['id'];density:'comfortable'|'compact';reduceMotion:boolean;shortcutsEnabled:boolean;shortcuts:Partial<Record<ShortcutAction,string>>};
export const defaultDesktopPreferences=():DesktopPreferences=>({theme:'umber',density:'comfortable',reduceMotion:false,shortcutsEnabled:true,shortcuts:{search:'Mod+K',send:'Mod+Enter'}});
const reserved=new Set(['Mod+A','Mod+C','Mod+V','Mod+X','Mod+Z','Mod+Y','Mod+B','Mod+D','Mod+F','Mod+G','Mod+H','Mod+L','Mod+N','Mod+O','Mod+P','Mod+Q','Mod+R','Mod+S','Mod+T','Mod+U','Mod+W','Mod+0','Mod+1','Mod+2','Mod+3','Mod+4','Mod+5','Mod+6','Mod+7','Mod+8','Mod+9','Mod+Shift+N','Mod+Shift+T','Mod+Shift+W','Mod+Shift+P','Mod+Shift+Z','Mod+Shift+R','Mod+Shift+I','Mod+Shift+J','Mod+Alt+I','Mod+Alt+J','Mod+Alt+U']);
export function shortcutProblem(binding:string):string|null{
 if(!/^Mod\+(?:Alt\+)?(?:Shift\+)?(?:[A-Z0-9]|Enter)$/.test(binding))return '请使用 ⌘ / Ctrl 加字母、数字或 Enter，可搭配 Alt、Shift。';
 if(reserved.has(binding))return '这个组合键用于浏览器或文本编辑，请换一个。';
 return null;
}
export type ShortcutStroke={key:string;code?:string;ctrlKey?:boolean;metaKey?:boolean;altKey?:boolean;shiftKey?:boolean;repeat?:boolean;isComposing?:boolean;keyCode?:number;getModifierState?:(key:string)=>boolean};
export function captureShortcut(event:ShortcutStroke):string|null{
 if(event.isComposing||event.keyCode===229||event.repeat||event.getModifierState?.('AltGraph')||(!event.ctrlKey&&!event.metaKey)||event.ctrlKey&&event.metaKey)return null;
 const physical=/^Key([A-Z])$/.exec(event.code??'')?.[1]??/^Digit([0-9])$/.exec(event.code??'')?.[1];
 const key=event.key==='Enter'?'Enter':physical??(/^[a-z0-9]$/i.test(event.key)?event.key.toUpperCase():null);
 return key?`Mod+${event.altKey?'Alt+':''}${event.shiftKey?'Shift+':''}${key}`:null;
}
export function shortcutMatches(event:ShortcutStroke,binding:string|undefined):boolean{return !!binding&&captureShortcut(event)===binding;}
export function formatShortcut(binding:string|undefined):string{return binding?binding.replace('Mod','⌘ / Ctrl').split('+').join(' + '):'未设置';}
export function bindShortcut(prefs:DesktopPreferences,action:ShortcutAction,binding:string):DesktopPreferences{
 const problem=shortcutProblem(binding);if(problem)throw new Error(problem);
 const duplicate=Object.entries(prefs.shortcuts).find(([key,value])=>key!==action&&value===binding);
 if(duplicate)throw new Error(`与“${shortcutActions[duplicate[0] as ShortcutAction]}”重复，请换一个。`);
 return {...prefs,shortcuts:{...prefs.shortcuts,[action]:binding}};
}
export function normalizeDesktopPreferences(value:unknown):DesktopPreferences{
 const defaults=defaultDesktopPreferences();if(!value||typeof value!=='object'||Array.isArray(value))return defaults;
 const raw=value as Record<string,unknown>;
 const result:DesktopPreferences={...defaults,theme:themes.some(t=>t.id===raw.theme)?raw.theme as DesktopPreferences['theme']:defaults.theme,density:raw.density==='compact'?'compact':'comfortable',reduceMotion:raw.reduceMotion===true,shortcutsEnabled:typeof raw.shortcutsEnabled==='boolean'?raw.shortcutsEnabled:true};
 if(raw.shortcuts&&typeof raw.shortcuts==='object'&&!Array.isArray(raw.shortcuts)){
  result.shortcuts={};
  for(const action of Object.keys(shortcutActions) as ShortcutAction[]){
   const binding=(raw.shortcuts as Record<string,unknown>)[action];
   if(binding===''){result.shortcuts[action]='';continue;}
   if(typeof binding==='string'&&!shortcutProblem(binding)&&!Object.values(result.shortcuts).includes(binding))result.shortcuts[action]=binding;
  }
 }
 return result;
}
export function readDesktopPreferences():DesktopPreferences{
 try{return normalizeDesktopPreferences(JSON.parse(preferenceStorage.getItem(DESKTOP_PREF_KEY)??'null'));}catch{return defaultDesktopPreferences();}
}
export function applyDesktopPreferences(prefs:DesktopPreferences):void{
 if(typeof document==='undefined')return;
 const root=document.documentElement;if(!root?.style)return;
 const theme=themes.find(t=>t.id===prefs.theme)??themes[0];
 root.dataset.uiTheme=theme.id;root.dataset.uiDensity=prefs.density;root.dataset.uiMotion=prefs.reduceMotion?'reduced':'system';
 for(const [key,value] of Object.entries({accent:theme.color,hover:theme.hover,soft:theme.soft,border:theme.border}))root.style.setProperty(`--ui-${key}`,value);
}
export function saveDesktopPreferences(prefs:DesktopPreferences):boolean{
 const value=normalizeDesktopPreferences(prefs),persisted=preferenceStorage.setItem(DESKTOP_PREF_KEY,JSON.stringify(value));
 applyDesktopPreferences(value);if(typeof window!=='undefined')window.dispatchEvent(new Event(DESKTOP_PREF_EVENT));return persisted;
}
export function observeDesktopPreferences(listener:()=>void):()=>void{
 const sync=()=>{applyDesktopPreferences(readDesktopPreferences());listener();};
 const storage=(event:StorageEvent)=>{if(event.key===DESKTOP_PREF_KEY||event.key===null)sync();};
 window.addEventListener(DESKTOP_PREF_EVENT,sync);window.addEventListener('storage',storage);
 return()=>{window.removeEventListener(DESKTOP_PREF_EVENT,sync);window.removeEventListener('storage',storage);};
}
