"""Dedicated demo project; idempotent sample import without touching user projects."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .store import now


def load_demo(service):
    manifest=service.samples/'manifest.json'
    raw=manifest.read_bytes()
    records=json.loads(raw)
    if not isinstance(records,list) or not 1<=len(records)<=20:raise ValueError('演示样本清单无效')
    # Validate every byte before any project/import mutation.
    for row in records:
        path=(service.samples/row['file']).resolve()
        if not path.is_relative_to(service.samples) or not path.is_file():raise ValueError('演示样本路径无效')
        actual=hashlib.sha256(path.read_bytes()).hexdigest()
        if row.get('sha256')!=actual:raise ValueError('演示样本摘要不匹配，请恢复原始样本包')
        if not row.get('origin'):raise ValueError('演示样本必须说明来源')
    revision=hashlib.sha256(raw).hexdigest()
    project_id='demo-'+revision[:24]
    with service.store.transaction() as c:
        c.execute('INSERT OR IGNORE INTO projects(id,name,description,created_at,updated_at) VALUES(?,?,?,?,?)',
                  (project_id,'知衡演示工作区','独立示例数据；实时运行与历史回放分开。样本版本 '+revision,now(),now()))
    # Existing content hashes deduplicate within this dedicated project. A new
    # manifest version gets a new project, preserving previous demo provenance.
    imported=service.demo(project_id)
    return {'project':service.store.get('projects',project_id),'manifest_sha256':revision,
            'mode':'live_workspace','automatic_answers_submitted':False,'personal_projects_modified':False,**imported}
