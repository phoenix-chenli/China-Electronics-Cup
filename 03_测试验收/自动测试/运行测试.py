"""Each module owns an isolated process, temporary database and task executor."""
from pathlib import Path
import os,subprocess,sys,unittest
# Legacy regression modules own their fixtures. Public-source installation has
# its own integration suite and must not silently replace their sales baseline.
os.environ['EVIDENCEFLOW_SKIP_PUBLIC_DEMO']='1'
root=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(root/'01_程序代码'),str(Path(__file__).resolve().parent),str(root/'01_程序代码/启动与打包')]
from runtime_bootstrap import activate
activate()
if len(sys.argv)==3 and sys.argv[1]=='--module':
    suite=unittest.defaultTestLoader.loadTestsFromName(sys.argv[2])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
failed=[]
for path in sorted((Path(__file__).parent/'tests').glob('test_*.py')):
    name='tests.'+path.stem
    result=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).resolve()),'--module',name],
                          cwd=root,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
    if result.returncode:failed.append(name)
print('未通过的模块：'+(', '.join(failed) if failed else '无'))
raise SystemExit(bool(failed))
