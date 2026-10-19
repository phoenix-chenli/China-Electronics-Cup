/** Keep the current tab usable when browser storage is denied or full.
 * Pending values outrank older disk values; the backend workspace save remains
 * independent. The memory fallback does not promise survival after reload.
 */
export function resilientStorage(access:()=>Pick<Storage,'getItem'|'setItem'|'removeItem'>){
 const memory=new Map<string,string|null>();const pending=new Set<string>();
 return {
  getItem(key:string):string|null{
   if(pending.has(key))return memory.get(key)??null;
   try{const value=access().getItem(key);memory.set(key,value);return value;}
   catch{return memory.get(key)??null;}
  },
  setItem(key:string,value:string):boolean{
   memory.set(key,value);
   try{access().setItem(key,value);pending.delete(key);return true;}
   catch{pending.add(key);return false;}
  },
  removeItem(key:string):boolean{
   memory.set(key,null);
   try{access().removeItem(key);pending.delete(key);return true;}
   catch{pending.add(key);return false;}
  },
 };
}
export const tabStorage=resilientStorage(()=>window.sessionStorage);
export const preferenceStorage=resilientStorage(()=>window.localStorage);
