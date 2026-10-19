"""Verify portable assets, optionally create a fresh project without model calls.

Only the input manifest is read. Answer keys are deliberately outside this code
path. Long PDF/image parsing is never performed by this preparation command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MANIFEST = ROOT / '评测材料/可迁移复现/输入清单.json'


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def asset_path(relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('输入路径必须位于作品目录内')
    path = (ROOT / path).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError('输入文件不能通过符号链接越过作品目录')
    return path


def verify():
    expected = MANIFEST.with_suffix('.json.sha256').read_text(encoding='utf-8').split()[0]
    if digest(MANIFEST) != expected:
        raise ValueError('可迁移输入清单内容已改变')
    plan = json.loads(MANIFEST.read_text(encoding='utf-8'))
    if plan.get('schema_version') != 'zhiheng-portable-inputs.v1':
        raise ValueError('输入清单版本不受支持')
    inventory = json.loads((MANIFEST.parent / '数据与模型清单.json').read_text(encoding='utf-8'))
    for item in [*plan['sources'], *inventory['assets']]:
        if digest(asset_path(item['path'])) != item['sha256']:
            raise ValueError('资产摘要不符：' + item['path'])
    return plan, inventory


def prepare(plan, selected, runtime):
    sources = {item['id']:item for item in plan['sources']}
    if not selected or set(selected) - sources.keys():
        raise ValueError('请使用输入清单中真实的来源ID')
    runtime = runtime.resolve()
    if runtime.exists() and any(runtime.iterdir()):
        raise ValueError('目标运行目录不是空目录；为避免改动已有资料，请指定新的 --runtime')
    from fastapi.testclient import TestClient
    from backend.main import create_app
    def no_model(*_):
        raise RuntimeError('资料准备禁止调用模型')
    app = create_app(runtime, ROOT/'samples', enable_workers=False, answer_gateway_factory=no_model)
    records = []
    with TestClient(app) as client:
        response = client.post('/api/projects', json={'name':'可迁移复现实例'})
        response.raise_for_status()
        project = response.json()
        for input_id in selected:
            item = sources[input_id]; path = asset_path(item['path'])
            with path.open('rb') as handle:
                response = client.post(f"/api/projects/{project['id']}/import", files=[('files',(path.name,handle))])
            response.raise_for_status(); imported = response.json()
            source = (imported['sources'] + imported['duplicates'])[0]
            # Database/text/small Office imports exercise real parsers. Scanned
            # documents remain queued for an explicit future user import run.
            if path.suffix.lower() in {'.sqlite','.txt','.md','.csv','.docx'} and path.stat().st_size <= 2_000_000:
                for job in imported['jobs']:
                    app.state.service.process_job(job['id'])
            current = app.state.service.store.get('sources',source['id'])
            records.append({'input_id':input_id,'source_id':source['id'],
                            'sha256':item['sha256'],'status':current['status'],
                            'package_path':item['path']})
    mapping = {'schema_version':'zhiheng-portable-map.v1','project_id':project['id'],
               'sources':records,'model_requests':0,'historical_answers_imported':False,
               'note':'由当前应用真实创建的新ID；不沿用开发者project-id。未导入答案键或历史会话。'}
    (runtime/'可迁移来源映射.json').write_text(json.dumps(mapping,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return mapping


def main():
    parser = argparse.ArgumentParser(description='校验随包数据；显式 --prepare 才创建独立项目，永不调用模型')
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--sources',default='chinook,definitions')
    parser.add_argument('--runtime',type=Path,default=ROOT/'.runtime')
    args = parser.parse_args()
    plan, inventory = verify()
    result = {'verified':True,'cases':len(plan['cases']),'source_ids':len(plan['sources']),
              'asset_files':len(inventory['assets']),'model_requests':0}
    if args.prepare:
        result['created'] = prepare(plan,list(dict.fromkeys(args.sources.split(','))),args.runtime)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError,OSError) as exc:
        raise SystemExit(str(exc))
