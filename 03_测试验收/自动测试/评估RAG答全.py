"""Reproducible local retrieval + scripted HTTP model workflow diagnostics.

No real model quality or benchmark score is measured. No private configuration
is loaded. --output must be an external temporary directory.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();output=args.output.resolve()
    if output==ROOT or ROOT in output.parents:
        parser.error('output必须位于项目之外，诊断数据与构建缓存不写入交付源码')
    output.mkdir(parents=True,exist_ok=True)
    cache=output/'cache';cache.mkdir(exist_ok=True)
    os.environ['TMP']=os.environ['TEMP']=str(cache);tempfile.tempdir=str(cache)
    os.environ['EVIDENCEFLOW_DATA_DIR']=str(cache/'isolated-data')
    os.environ['EVIDENCEFLOW_SKIP_PUBLIC_DEMO']='1'
    sys.path[:0]=[str(ROOT/'01_程序代码/启动与打包'),str(ROOT/'01_程序代码'),str(Path(__file__).parent)]
    from runtime_bootstrap import activate
    activate()
    # Windows restricted runtimes may select an unsupported IPv6 socket pair.
    original_pair=socket.socketpair
    if sys.platform=='win32':
        socket.socketpair=lambda family=None,type=socket.SOCK_STREAM,proto=0:original_pair(socket.AF_INET,type,proto)
    from tests import test_rag_completeness as cases
    records=[];original=cases.CompletenessHTTP.fixture
    def capture(test,*args,**kwargs):
        result,seen=original(test,*args,**kwargs)
        records.append({'case':test._testMethodName,'question':result['question'],
            'answer_status':result['answer_status'],'answer':result['answer'],
            'completeness':result['rag']['completeness'],
            'repair':result['rag'].get('completion_repair'),
            'stop_reason':result['rag'].get('completion_stop_reason'),
            'model_calls':result['agent']['calls'],'latency_ms':result['latency_ms'],
            'events':result['events'],
            'source_versions':result['rag']['scope'].get('documents',[])})
        return result,seen
    cases.CompletenessHTTP.fixture=capture
    files=[ROOT/'01_程序代码/backend'/name for name in ('agent.py','rag_grounding.py','rag_completeness.py')]
    files += [Path(cases.__file__),Path(__file__)]
    hashes=lambda:{str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    before=hashes();started=time.monotonic()
    with (output/'tests.log').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(cases))
    summary={'kind':'authored_workflow_diagnostic','model':'scripted HTTP fixture; no external model',
        'retrieval':'real local lexical retrieval over authored disposable documents',
        'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
        'seconds':round(time.monotonic()-started,3),'source_sha256':hashes(),'source_unchanged':before==hashes(),
        'scenarios':records,'boundary':'验证逐项覆盖与有界补救流程，不能作为真实模型答全率、事实准确率或OHR-Bench成绩。'}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k not in {'scenarios','source_sha256'}},ensure_ascii=False))
    return 0 if result.wasSuccessful() and summary['source_unchanged'] else 1


if __name__=='__main__':raise SystemExit(main())
