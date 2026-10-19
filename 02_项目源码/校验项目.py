#!/usr/bin/env python3
"""核对正式交付文件；不改文件、不联网、不访问模型。"""
from pathlib import Path
import hashlib,json
root=Path(__file__).resolve().parent
manifest=json.loads((root/'项目文件清单.json').read_text(encoding='utf-8'))
errors=[]
for row in manifest['files']:
 rel=Path(row['path'])
 if rel.is_absolute() or '..' in rel.parts:
  errors.append({'path':row['path'],'reason':'路径不规范'});continue
 p=root/rel
 if not p.is_file() or p.is_symlink():
  errors.append({'path':row['path'],'reason':'缺失或意外链接'});continue
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 if p.stat().st_size!=row['bytes'] or h.hexdigest()!=row['sha256']:
  errors.append({'path':row['path'],'reason':'内容变化'})
print(json.dumps({'status':'通过' if not errors else '存在差异','checked_files':len(manifest['files']),'errors':errors,'note':'本机依赖和运行数据未列入正式交付清单；此检查不代表真实问答已通过。'},ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
