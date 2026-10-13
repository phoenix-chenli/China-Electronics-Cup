"""Frozen multi-document provider baseline; no reference content reaches the model."""
import argparse
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('multi_document_evaluation',Path(__file__).with_name('评估真实文档.py'))
evaluator=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluator)
evaluator.DATASET=evaluator.ROOT/'03_测试验收/测试样本/多文档真实原件验证集-20261013.json'
evaluator.FIRST=evaluator.ROOT/'03_测试验收/验收证据/多文档真实原件TokenRhythm首测-20261013.json'
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--regression',action='store_true')
    parser.add_argument('--output',type=Path,default=evaluator.FIRST);parser.add_argument('--case',action='append')
    parser.add_argument('--dataset',type=Path);args=parser.parse_args()
    if args.dataset:evaluator.DATASET=args.dataset.resolve()
    if args.execute:evaluator.execute(args.output,args.regression,args.case)
    else:print(evaluator.digest(evaluator.DATASET))
