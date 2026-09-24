"""Extract Microsoft's redistributable CAB files; never execute the Windows installer.
Usage: Python vendor_vc_runtime.py CACHE DIRECTORY_CONTAINING_7ZZ_OR_7ZZ_PATH
"""
from pathlib import Path
import hashlib,json,shutil,struct,subprocess,sys,xml.etree.ElementTree as ET
from prepare_windows import fetch,ROOT
def main():
    if len(sys.argv)!=3:raise SystemExit('Usage: vendor_vc_runtime.py CACHE PATH_TO_7ZZ')
    cache=Path(sys.argv[1]);tool=Path(sys.argv[2]);cache.mkdir(parents=True,exist_ok=True)
    if tool.is_dir():tool=tool/'7zz'
    record=ROOT/'05_比赛与交接/文件清单与许可证/VC运行库来源与摘要.json'
    expected=json.loads(record.read_text(encoding='utf-8'))
    exe=cache/'vc_redist.x64.exe'
    if not exe.exists():exe.write_bytes(fetch(expected['source_url']))
    data=exe.read_bytes()
    if hashlib.sha256(data).hexdigest()!=expected['download_sha256']:
        raise ValueError('Microsoft 下载内容已更新；请重新核对版本、平台和许可证后更新来源清单')
    cabinets=[];offset=0
    while True:
        offset=data.find(b'MSCF\0\0\0\0',offset)
        if offset<0:break
        size=struct.unpack_from('<I',data,offset+8)[0]
        if 36<size<=len(data)-offset:
            cab=cache/('cab-'+str(len(cabinets))+'.cab');cab.write_bytes(data[offset:offset+size])
            output=cache/('cab-'+str(len(cabinets)))
            subprocess.run([str(tool),'x','-y',str(cab),'-o'+str(output)],check=True,stdout=subprocess.DEVNULL)
            cabinets.append(output)
        offset+=8
    manifest=next(folder/'0' for folder in cabinets if (folder/'0').exists())
    nodes=ET.fromstring(manifest.read_text(encoding='utf-8-sig'))
    payload=next(n for n in nodes.iter() if n.tag.endswith('Payload') and n.attrib.get('FilePath')=='packages\\vcRuntimeMinimum_amd64\\cab1.cab')
    inner=next(folder/payload.attrib['SourcePath'] for folder in cabinets if (folder/payload.attrib['SourcePath']).exists())
    out=cache/'vc-native'
    subprocess.run([str(tool),'x','-y',str(inner),'-o'+str(out)],check=True,stdout=subprocess.DEVNULL)
    for item in expected['files']:
        source=out/(item['file']+'_amd64')
        if hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('DLL digest mismatch')
        shutil.copy2(source,ROOT/'06_运行环境/Windows_x64'/item['file'])
    print('Microsoft x64 DLL extracted and verified; no Windows execution.')
if __name__=='__main__':main()
