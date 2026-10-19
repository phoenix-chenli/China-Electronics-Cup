#!/usr/bin/env python3
"""离线核对冻结输入和参考 SQL。不会启动问答服务，不读取模型配置，不联网。"""
from pathlib import Path
import hashlib
import json
import sqlite3
import sys

BASE = Path(__file__).resolve().parent
PROJECT = BASE.parents[1]
sys.path.insert(0, str(PROJECT))
from evaluation.core import load_suite, equal_rows


def main():
    path = BASE / '输入/suite.json'
    suite = load_suite(path)
    key = json.loads((BASE / '离线评分/参考答案.json').read_text(encoding='utf-8'))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert key['suite_sha256'] == digest, '答案键与题集不一致'
    assert len(suite['cases']) == 20 and len(key['cases']) == 20
    assert {x['id'] for x in suite['cases']} == set(key['cases'])
    sql_cases = [c for c in suite['cases'] if c['mode'] == 'database']
    rag_cases = [c for c in suite['cases'] if c['mode'] == 'document']
    assert len(sql_cases) == len(rag_cases) == 10
    checks = []
    database = BASE / '输入/chinook.sqlite'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        for case in sql_cases:
            expected = key['cases'][case['id']]
            rows = [list(r) for r in db.execute(expected['reference_sql'], expected.get('reference_parameters', []))]
            matched = equal_rows(rows, expected['rows'], expected.get('ordered', False), expected.get('tolerance', 0))
            checks.append({'id': case['id'], 'reference_rows': len(rows), 'reference_matches': matched})
            assert matched, '参考 SQL 与答案键不一致：' + case['id']
    print(json.dumps({'status': '通过', 'suite_sha256': digest, 'sources_verified': len(suite['sources']),
                      'sql_reference_checks': checks, 'rag_cases': len(rag_cases), 'model_calls': 0,
                      'scope': '仅核对文件和参考答案；未执行自然语言问答，不产生准确率或比赛分数。'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
