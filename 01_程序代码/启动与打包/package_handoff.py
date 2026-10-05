"""Whitelist-only clean teammate package, real RAR, extraction and SHA256 verification.
Usage: Python package_handoff.py OUTPUT.rar PATH_TO_RAR
"""
from pathlib import Path
import ast,hashlib,json,os,shutil,subprocess,sys,tempfile
ROOT=Path(__file__).resolve().parents[2]
FOLDERS=['01_程序代码','02_数据资料','03_测试验收','04_设计素材','05_比赛与交接','06_运行环境']
FILES=['启动证流_Windows.bat','停止证流_Windows.bat','启动证流_Mac.command','停止证流_Mac.command','使用说明.md','README.md','AGENTS.md','.gitignore']
REPORT='05_比赛与交接/文件清单与许可证/交接包校验.json'
MANIFEST='05_比赛与交接/文件清单与许可证/文件摘要.sha256'
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def include(relative):
    parts=relative.parts
    if any(n in parts for n in ['node_modules','__pycache__','.venv','.private','.DS_Store','.git']):return False
    if relative.as_posix()==REPORT or relative.as_posix()==MANIFEST:return False
    if relative.as_posix().startswith('02_数据资料/运行数据/'):
        return relative.as_posix()=='02_数据资料/运行数据/说明.md'
    if relative.suffix in {'.pyc','.tmp'} or relative.name.endswith(('.db-wal','.db-shm')):return False
    return True
def verify_tree(root,manifest):
    for p in root.rglob('*.py'):ast.parse(p.read_text(encoding='utf-8'),filename=str(p))
    actual={p.relative_to(root).as_posix():sha(p) for p in root.rglob('*') if p.is_file() and p.relative_to(root).as_posix()!=MANIFEST}
    assert actual==manifest,'Extracted contents differ from the manifest'
    assert max(len(p.relative_to(root).parts) for p in root.rglob('*') if p.is_dir())<=4
    assert not any(p.name=='app.db' or p.name=='connections.key' for p in root.rglob('*'))
    assert not any(p.suffix=='.db' and p.relative_to(root).as_posix()!='02_数据资料/示例数据/business.db' for p in root.rglob('*'))
    assert not any(p.is_symlink() for p in root.rglob('*'))
def main():
    if len(sys.argv)!=3:raise SystemExit('Usage: package_handoff.py OUTPUT.rar PATH_TO_RAR')
    output=Path(sys.argv[1]).resolve();rar=Path(sys.argv[2]).resolve()
    if output.suffix.lower()!='.rar':raise ValueError('Output must be .rar')
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
                if str(Path.home()) in text:
                    text=text.replace(str(Path.home()),'[原Mac用户目录]')
                    p.write_text(text,encoding='utf-8')
        manifest={p.relative_to(stage).as_posix():sha(p) for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage/MANIFEST).write_text(''.join(value+'  '+name+'\n' for name,value in manifest.items()),encoding='utf-8')
        verify_tree(stage,manifest)
        archive=base/'证流_赛题八_Windows队友交接版.rar'
        subprocess.run([str(rar),'a','-r','-m3','-idq',str(archive),'证流_赛题八'],cwd=base,check=True)
        subprocess.run([str(rar),'t','-idq',str(archive)],check=True)
        extracted=base/'解压核验';extracted.mkdir()
        subprocess.run([str(rar),'x','-idq','-o+',str(archive),str(extracted)+os.sep],check=True)
        verify_tree(extracted/'证流_赛题八',manifest)
        assert (extracted/'证流_赛题八'/MANIFEST).read_bytes()==(stage/MANIFEST).read_bytes()
        data={'archive_name':output.name,'bytes':archive.stat().st_size,'sha256':sha(archive),
              'rar_integrity':'passed','extracted_files':len(manifest)+1,'sha256_comparison':'all match',
              'max_directory_depth':max(len(p.relative_to(stage).parts) for p in stage.rglob('*') if p.is_dir()),
              'windows_runtime_tested':False,'personal_runtime_data_included':False,'mac_environment_included':False,
              'node_modules_included':False,'top_folder':'证流_赛题八'}
        output.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(archive,output)
        # Keep the workspace index tied to this verified archive, not an older release.
        shutil.copy2(stage/MANIFEST,ROOT/MANIFEST)
        (ROOT/REPORT).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(data,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
