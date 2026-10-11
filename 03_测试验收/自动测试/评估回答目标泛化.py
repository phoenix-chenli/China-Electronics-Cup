"""Evaluate fresh developer-authored questions without sending their references."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

spec = importlib.util.spec_from_file_location('answer_goal_evaluation', Path(__file__).with_name('评估公开统计.py'))
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)
evaluator.DATASET = evaluator.ROOT / '03_测试验收/测试样本/回答目标泛化验证集-20261011.json'
evaluator.FIRST = evaluator.ROOT / '03_测试验收/验收证据/回答目标泛化DeepSeek首测-20261011.json'
evaluator.EXPECTED_SHA = 'a3e08291a9fb76cf5c9b790f07af24b2dbd9d6c7ba40903d0670eaa34c4486ce'


def validate():
    raw = evaluator.DATASET.read_bytes()
    if hashlib.sha256(raw).hexdigest() != evaluator.EXPECTED_SHA:
        raise ValueError('泛化题集偏离调用前冻结版本')
    data = json.loads(raw)
    if not data['frozen_before_model_execution']:
        raise ValueError('题集没有在执行前冻结')
    path = evaluator.ROOT / data['database']['path']
    if hashlib.sha256(path.read_bytes()).hexdigest() != data['database']['sha256']:
        raise ValueError('公开库偏离冻结版本')
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        for case in data['cases']:
            reference = case['reference']
            if reference.get('sql'):
                rows = [list(row) for row in db.execute(reference['sql'], reference.get('params', []))]
                if rows != reference['expected_rows']:
                    raise ValueError('独立SQL参考与实际库不符：' + case['id'])
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--regression', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    data = validate()
    if args.execute:
        output = args.output or (str(evaluator.FIRST) if not args.regression else None)
        if output is None:
            parser.error('回归必须指定新的结果文件')
        if args.regression and not evaluator.FIRST.exists():
            parser.error('没有封存首测，不能标记回归')
        evaluator.execute(output, args.regression)
    else:
        print(json.dumps({'frozen_sha256': evaluator.EXPECTED_SHA, 'cases': len(data['cases']),
                          'reference_sql_verified': sum(bool(c['reference'].get('sql')) for c in data['cases']),
                          'model_calls': 0}, ensure_ascii=False))
