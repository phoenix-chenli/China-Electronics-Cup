"""Separate first exposure of the frozen, explicitly specified complex set."""
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('complex_chinook_evaluation',Path(__file__).with_name('评估Chinook.py'))
evaluator=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluator)
evaluator.DATASET=evaluator.ROOT/'03_测试验收/测试样本/Chinook题面明确复杂新题-20261013.json'
evaluator.FIRST=evaluator.ROOT/'03_测试验收/验收证据/Chinook题面明确复杂新题首测-20261013.json'
if __name__=='__main__':evaluator.main()
