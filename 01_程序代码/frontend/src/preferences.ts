import {createContext,useContext} from 'react'
export const defaultPrefs={theme:'light',density:'standard',fontSize:14,pet:true,motion:true,context:true,proactive:false,tone:'concise',evidence:'standard',notifySuccess:true,notifyFailure:true,notifyImport:true}
export function normalizePrefs(raw:Record<string,any>={}){
 const p={...defaultPrefs,...raw};
 if(!['light','dark','system'].includes(p.theme))p.theme='light'
 if(!['comfortable','standard','compact'].includes(p.density))p.density='standard'
 p.fontSize=[12,14,16,18].reduce((a,b)=>Math.abs(b-Number(p.fontSize))<Math.abs(a-Number(p.fontSize))?b:a,14)
 if(!['concise','friendly'].includes(p.tone))p.tone='concise'
 if(!['brief','standard','detailed'].includes(p.evidence))p.evidence='standard'
 for(const key of ['pet','motion','context','proactive','notifySuccess','notifyFailure','notifyImport'] as const)p[key]=typeof p[key]==='boolean'?p[key]:defaultPrefs[key]
 return p
}
export const PreferencesContext=createContext(defaultPrefs)
export const usePreferences=()=>useContext(PreferencesContext)
