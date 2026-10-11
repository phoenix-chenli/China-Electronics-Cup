"""Run a separate frozen SQL-only set; old first-run evidence remains intact."""
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('basic_projection_evaluation', Path(__file__).with_name('评估比赛基础任务.py'))
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)
evaluator.DATASET = evaluator.ROOT / '03_测试验收/测试样本/明确输出列问数验证集-20261011.json'
evaluator.FIRST = evaluator.ROOT / '03_测试验收/验收证据/明确输出列问数DeepSeek首测-20261011.json'
evaluator.EXPECTED_SHA = 'd9e073d942c692197ddf02f64c0929d37488eceb527afe08b61963cae92faf29'
if __name__ == '__main__':
    evaluator.main()
