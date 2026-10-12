"""Read-only Chinook benchmark: the official sample database, bound as a project.

Chinook is one of the datasets the competition brief recommends, so the project
ships it unchanged and uses it only through a read-only SQLite connection. This
module never downloads anything and never writes to the file; a missing or
altered file reports a reason instead of failing startup.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import connections, store as s

PROJECT_ID = 'chinook-benchmark'
SOURCE_DIR = s.PROJECT_ROOT / '02_数据资料' / '公开基准' / 'Chinook'
DB_PATH = SOURCE_DIR / 'Chinook_Sqlite.sqlite'
MANIFEST = SOURCE_DIR / '来源清单.json'
CASES = s.PROJECT_ROOT / '03_测试验收' / '测试样本' / 'chinook验证集-20261012.json'


def manifest():
    try:
        body = json.loads(MANIFEST.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def cases():
    try:
        body = json.loads(CASES.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    items = body.get('cases') if isinstance(body, dict) else None
    return items if isinstance(items, list) else []


def verify():
    """Return (manifest, error); the shipped bytes must match the recorded digest."""
    body = manifest()
    if body is None:
        return None, '缺少或无法读取 Chinook 来源清单'
    if not DB_PATH.is_file():
        return None, '缺少随包提供的 Chinook 数据库文件'
    if body.get('file_sha256') != s.digest(DB_PATH):
        return None, 'Chinook 数据库摘要与来源清单不一致，未建立连接'
    return body, ''


def install():
    """Bind the shipped Chinook database as a read-only project. Never raises."""
    body, error = verify()
    if error:
        return {'installed': False, 'reason': error}
    try:
        target = str(DB_PATH.resolve())
        existing = [x for x in connections.listing() if x['kind'] == 'sqlite' and x.get('path') == target]
        if existing:
            config = existing[0]
        else:
            config = connections.create(connections.Config(name='Chinook 公开基准 只读', kind='sqlite', path=target))
            if not connections.test(config['id'])['last_test']['ok']:
                return {'installed': False, 'reason': 'Chinook 只读连接测试未通过'}
            config = connections.enabled(config['id'], connections.Enabled(enabled=True))
        items = cases()
        from . import query_routing
        query_routing.initialize()
        stamp = s.now()
        with s.conn() as db:
            db.execute('INSERT OR IGNORE INTO ef_projects VALUES (?,?,?,?,?)',
                       (PROJECT_ID, 'Chinook 公开基准', 'sales-demo', stamp, stamp))
        query_routing.bind(PROJECT_ID, {
            'default_scope': 'database', 'managed_public': True,
            'database_connection_id': config['id'], 'document_ids': [],
            'source_label': 'Chinook 开源示例库（MIT · lerocha/chinook-database）',
            'starter_questions': [str(c.get('question', '')) for c in items[:3] if c.get('question')]})
        return {'installed': True, 'project_id': PROJECT_ID, 'connection_id': config['id'],
                'cases': len(items), 'tables': body.get('tables'), 'rows': body.get('rows_total')}
    except Exception as exc:
        # A benchmark that cannot be bound must not stop the local service.
        return {'installed': False, 'reason': str(exc)[:200]}
