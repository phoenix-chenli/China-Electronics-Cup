"""Use a running isolated instance to verify real-file import and durable API behavior.

This is an acceptance runner, not a fixture-only unit test. It writes its report
to the requested path and never contacts a model provider.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import time

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def wait_sources(client, project):
    until = time.monotonic() + 80
    while time.monotonic() < until:
        response = client.get(f'/api/projects/{project}/sources')
        response.raise_for_status()
        sources = response.json()
        if sources and not any(s['status'] in ('queued', 'processing') for s in sources):
            return sources
        time.sleep(.15)
    raise AssertionError('real import did not finish within 80 seconds')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    checks = []
    def record(name, evidence):
        checks.append({'check': name, 'passed': True, 'evidence': evidence})
        print(name, flush=True)
    with httpx.Client(base_url=args.url, timeout=90, trust_env=False) as client:
        health = client.get('/api/health').json()
        assert health['service'] == 'zhiheng'
        response = client.post('/api/projects', json={'name': '真实资料验收', 'description': '自动验收项目，与演示项目隔离'})
        response.raise_for_status()
        project = response.json()['id']
        response = client.post(f'/api/projects/{project}/demo')
        response.raise_for_status()
        sources = wait_sources(client, project)
        manifest = json.loads((ROOT / 'samples' / 'manifest.json').read_text())
        assert len(sources) == len(manifest)
        by_hash = {s['sha256']: s for s in sources}
        for entry in manifest:
            path = ROOT / 'samples' / entry['file']
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            source = by_hash[digest]
            assert source['status'] in ('ready', 'attention'), source
            detail = client.get(f'/api/sources/{source["id"]}').json()
            assert detail['metadata'].get('origin') == entry['origin'], detail['metadata']
            original = client.get(f'/api/sources/{source["id"]}/original')
            original.raise_for_status()
            assert hashlib.sha256(original.content).hexdigest() == digest
            if source['kind'] == 'image':
                assert source['status'] == 'attention'
                assert any('识别' in s for s in source['warnings']) or '识别' in source['summary']
                preview = client.get(f'/api/sources/{source["id"]}/pages/1/image')
                preview.raise_for_status()
                with Image.open(io.BytesIO(preview.content)) as image:
                    assert min(image.size) > 100
            elif path.suffix.lower() == '.pdf':
                assert source['page_count'] > 0 and any(p['text'].strip() for p in detail['pages'])
                preview = client.get(f'/api/sources/{source["id"]}/pages/1/image')
                preview.raise_for_status()
                with Image.open(io.BytesIO(preview.content)) as image:
                    assert min(image.size) > 100
            elif path.suffix.lower() == '.csv':
                with path.open(encoding='utf-8-sig', newline='') as file:
                    expected = list(csv.reader(file))
                assert source['row_count'] == len(expected) - 1
                table = detail['tables'][0]
                rows = client.get(f'/api/sources/{source["id"]}/tables/{table["name"]}', params={'limit': 50}).json()
                assert [str(v) for v in rows['rows'][0]] == expected[1]
            record('真实原件及解析: '+entry['name'], {'source_id':source['id'],'status':source['status'],'sha256':digest,'pages':source['page_count'],'rows':source['row_count']})
        duplicate = client.post(f'/api/projects/{project}/demo').json()
        assert len(duplicate['duplicates']) == len(manifest) and not duplicate['sources']
        record('相同项目按文件摘要去重', len(duplicate['duplicates']))
        other = client.post('/api/projects', json={'name': '隔离项目'}).json()['id']
        assert client.get(f'/api/projects/{other}/sources').json() == []
        usable = next(s for s in sources if s['status'] == 'ready')
        bad = client.post(f'/api/projects/{other}/drafts', json={'question':'是否串用资料？', 'source_ids':[usable['id']]})
        assert bad.status_code >= 400
        record('跨项目资料不能保存进问题草稿', bad.status_code)
        response = client.post(f'/api/projects/{project}/drafts', json={'question':'这些公开资料包含哪些产业与统计信息？','source_ids':[usable['id']]})
        response.raise_for_status()
        draft = response.json()
        assert any(d['id'] == draft['id'] for d in client.get(f'/api/projects/{project}/drafts').json())
        record('真实资料关联的问题准备持久化', draft['id'])
        image_source = next((s for s in sources if s['kind'] == 'image'), None)
        if image_source:
            response = client.post(f'/api/projects/{project}/drafts', json={'question':'图片内容是什么？','source_ids':[image_source['id']]})
            assert response.status_code >= 400
            record('未识别图像不会假装可用文字', response.status_code)
        client.post(f'/api/sources/{usable["id"]}/archive').raise_for_status()
        assert usable['id'] not in {s['id'] for s in client.get(f'/api/projects/{project}/sources').json()}
        client.post(f'/api/sources/{usable["id"]}/restore').raise_for_status()
        assert usable['id'] in {s['id'] for s in client.get(f'/api/projects/{project}/sources').json()}
        record('移出及恢复保留原件', usable['id'])
        response = client.post(f'/api/projects/{project}/import', files=[('files', ('损坏验收.pdf', b'not a pdf', 'application/pdf'))])
        response.raise_for_status()
        failed_id = response.json()['sources'][0]['id']
        failed = next(s for s in wait_sources(client, project) if s['id'] == failed_id)
        assert failed['status'] == 'failed' and failed['error']
        client.post(f'/api/sources/{failed_id}/retry').raise_for_status()
        failed = next(s for s in wait_sources(client, project) if s['id'] == failed_id)
        assert failed['status'] == 'failed'
        record('损坏文件失败可见且可重试', failed['error'])
        jobs = client.get(f'/api/projects/{project}/jobs').json()
        assert len([j for j in jobs if j['source_id'] == failed_id]) == 2
        record('重试保留每次处理记录', 2)
        report = {'service':health,'project_id':project,'draft_id':draft['id'],'checks':checks,'passed':True,'model_calls':0,'scope':'真实样本导入与状态 API 验收；浏览器交互与服务重启另行验收'}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f'{len(checks)} acceptance checks passed; report: {args.report}')


if __name__ == '__main__':
    main()
