"""Clean ZIP/RAR handoff with runtime, extraction and SHA256 verification.
Usage: Python package_handoff.py OUTPUT.zip
       Python package_handoff.py OUTPUT.rar PATH_TO_RAR
"""
from pathlib import Path
import ast,hashlib,io,json,os,shutil,subprocess,sys,tempfile,zipfile
ROOT=Path(__file__).resolve().parents[2]
FOLDERS=['01_程序代码','02_数据资料','03_测试验收','04_设计素材','05_比赛与交接','06_运行环境']
FILES=['启动证流_Windows.bat','停止证流_Windows.bat','启动证流_Mac.command','停止证流_Mac.command','使用说明.md','README.md','提升需求.md','AGENTS.md','.gitignore']
REPORT='05_比赛与交接/文件清单与许可证/交接包校验.json'
MANIFEST='05_比赛与交接/文件清单与许可证/文件摘要.sha256'
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def include(relative):
    parts=relative.parts
    if any(n in parts for n in ['node_modules','__pycache__','.venv','.private','.DS_Store','.git']):return False
    if relative.as_posix()==REPORT or relative.as_posix()==MANIFEST:return False
    name=relative.name.lower()
    if name=='connections.key' or name=='.env' or (name.startswith('.env.') and name!='.env.example'):return False
    if relative.suffix.lower() in {'.pem','.key'}:return False
    if relative.as_posix().startswith('02_数据资料/运行数据/'):
        return relative.as_posix()=='02_数据资料/运行数据/说明.md'
    if relative.suffix in {'.pyc','.tmp'} or relative.name.endswith(('.db-wal','.db-shm')):return False
    return True
def verify_runtime(root):
    # Verify the payload actually shipped, including contiguous split archives.
    from runtime_bootstrap import load_bundle
    runtime=root/'06_运行环境/Windows_x64'
    expected=(runtime/'第三方依赖.sha256').read_text(encoding='utf-8').split()[0]
    blob=load_bundle(runtime,expected)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        bad=archive.testzip()
        if bad:raise ValueError('Dependency ZIP integrity failed: '+bad)
    return {'sha256':expected,'bytes':len(blob),'integrity':'passed',
            'format':'complete' if (runtime/'第三方依赖.zip').is_file() else 'split',
            'parts':len(list(runtime.glob('第三方依赖.zip.[0-9][0-9][0-9]')))}
def verify_tree(root,manifest):
    for p in root.rglob('*.py'):ast.parse(p.read_text(encoding='utf-8'),filename=str(p))
    actual={p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file() and p.relative_to(root).as_posix()!=MANIFEST}
    assert actual==manifest,'Extracted contents differ from the manifest'
    assert max(len(p.relative_to(root).parts) for p in root.rglob('*') if p.is_dir())<=4
    assert not any(p.name=='app.db' or p.name=='connections.key' for p in root.rglob('*'))
    assert not any(p.suffix=='.db' and p.relative_to(root).as_posix()!='02_数据资料/示例数据/business.db' for p in root.rglob('*'))
    assert not any(p.is_symlink() for p in root.rglob('*'))
    assert not any(not include(p.relative_to(root)) for p in root.rglob('*')
                   if p.is_file() and p.relative_to(root).as_posix()!=MANIFEST)
    return verify_runtime(root)
def main():
    if len(sys.argv) not in {2,3}:raise SystemExit(__doc__)
    output=Path(sys.argv[1]).resolve()
    kind=output.suffix.lower()
    if kind not in {'.rar','.zip'}:raise ValueError('Output must be .rar or .zip')
    if kind=='.rar' and len(sys.argv)!=3:raise SystemExit('RAR output needs PATH_TO_RAR')
    rar=Path(sys.argv[2]).resolve() if kind=='.rar' else None
    with tempfile.TemporaryDirectory(prefix='evidenceflow-release-') as tmp:
        base=Path(tmp);stage=base/'证流_赛题八';stage.mkdir()
        for name in FILES:
            shutil.copy2(ROOT/name,stage/name)
        for name in FOLDERS:
            for p in (ROOT/name).rglob('*'):
                relative=p.relative_to(ROOT)
                if p.is_file() and include(relative):
                    if p.is_symlink():raise ValueError('Unexpected symlink: '+str(relative))
                    dest=stage/relative;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
        receipt=stage/'05_比赛与交接/文件清单与许可证/整理清单.json'
        local=json.loads(receipt.read_text(encoding='utf-8'))
        receipt.write_text(json.dumps({'说明':'备份、个人数据与具体恢复位置仅留在作者电脑；交接包不含它们。','已清空废纸篓':False,
            '清理项':[{k:v for k,v in item.items() if k!='恢复位置'} for item in local['清理项']],
            'Mac环境':'仅保留在作者电脑用户目录，不在包内'},ensure_ascii=False,indent=2),encoding='utf-8')
        # Final historic evidence retains numerical results, but not the author's home path.
        for p in stage.rglob('*'):
            if p.is_file() and p.suffix in {'.md','.json','.log','.txt','.jsonl'}:
                try:text=p.read_text(encoding='utf-8')
                except UnicodeError:continue
                homes={str(Path.home()),Path.home().as_posix(),json.dumps(str(Path.home()),ensure_ascii=False)[1:-1]}
                if any(home in text for home in homes):
                    for home in sorted(homes,key=len,reverse=True):
                        text=text.replace(home,'[原作者用户目录]')
                    p.write_text(text,encoding='utf-8')
        manifest={p.relative_to(stage).as_posix():sha(p) for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage/MANIFEST).write_text(''.join(value+'  '+name+'\n' for name,value in manifest.items()),encoding='utf-8')
        runtime=verify_tree(stage,manifest)
        archive=base/('证流_赛题八'+kind)
        if kind=='.rar':
            subprocess.run([str(rar),'a','-r','-m3','-idq',str(archive),'证流_赛题八'],cwd=base,check=True)
            subprocess.run([str(rar),'t','-idq',str(archive)],check=True)
        else:
            with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
                for p in sorted(stage.rglob('*')):
                    if p.is_file():z.write(p,p.relative_to(base))
            with zipfile.ZipFile(archive) as z:assert z.testzip() is None
        extracted=base/'解压核验';extracted.mkdir()
        if kind=='.rar':
            subprocess.run([str(rar),'x','-idq','-o+',str(archive),str(extracted)+os.sep],check=True)
        else:
            with zipfile.ZipFile(archive) as z:z.extractall(extracted)
        verify_tree(extracted/'证流_赛题八',manifest)
        assert (extracted/'证流_赛题八'/MANIFEST).read_bytes()==(stage/MANIFEST).read_bytes()
        data={'archive_name':output.name,'bytes':archive.stat().st_size,'sha256':sha(archive),
              'archive_format':kind[1:],'archive_integrity':'passed',
              **({'rar_integrity':'passed'} if kind=='.rar' else {'zip_integrity':'passed'}),
              'runtime_payload':runtime,
              'model_bundles':{name:(stage/'06_运行环境'/name).is_file() for name in ['RAG模型包.zip','OCR模型包.zip']},
              'extracted_files':len(manifest)+1,'sha256_comparison':'all match',
              'max_directory_depth':max(len(p.relative_to(stage).parts) for p in stage.rglob('*') if p.is_dir()),
              'windows_runtime_tested':False,
              'windows_runtime_tested_scope':'Startup of this freshly extracted archive has not been executed; current-machine OCR/RAG tests are recorded separately.',
              'personal_runtime_data_included':False,'mac_environment_included':False,
              'node_modules_included':False,'top_folder':'证流_赛题八'}
        output.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(archive,output)
        # Keep the workspace index tied to this verified archive, not an older release.
        shutil.copy2(stage/MANIFEST,ROOT/MANIFEST)
        (ROOT/REPORT).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(data,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
