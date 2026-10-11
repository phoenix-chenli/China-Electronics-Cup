"""Run six frozen transfer questions; references stay outside model input."""
import argparse
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'transfer_validation', Path(__file__).with_name('评估回答目标泛化.py'))
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)
evaluator = validation.evaluator
evaluator.DATASET = evaluator.ROOT / '03_测试验收/测试样本/跨页口径迁移验证集-20261011.json'
evaluator.FIRST = evaluator.ROOT / '03_测试验收/验收证据/跨页口径迁移DeepSeek首测-20261011.json'
evaluator.EXPECTED_SHA = 'a23529863e4f6bf43010f6852b9460057b12d94346dcc563eaeeb3be84ad5c79'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--regression', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    data = validation.validate()
    if args.execute:
        output = args.output or (str(evaluator.FIRST) if not args.regression else None)
        if output is None:
            parser.error('回归必须指定新的结果文件')
        if args.regression and not evaluator.FIRST.exists():
            parser.error('没有封存首测，不能标记回归')
        evaluator.execute(output, args.regression)
    else:
        print(json.dumps({'frozen_sha256': evaluator.EXPECTED_SHA, 'cases': len(data['cases']),
                          'reference_sql_verified': sum(bool(c['reference'].get('sql'))
                                                        for c in data['cases']),
                          'model_calls': 0}, ensure_ascii=False))
