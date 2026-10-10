"""Verify source split archives share the canonical handoff identity."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'01_程序代码/启动与打包'))
from runtime_bootstrap import load_bundle
from package_handoff import include,verify_runtime


class RuntimeBundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='evidenceflow-bundle-')
        self.root=Path(self.tmp.name)
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:archive.writestr('example/module.py','value=42')
        self.blob=buffer.getvalue();self.sha=hashlib.sha256(self.blob).hexdigest()
    def tearDown(self):self.tmp.cleanup()
    def parts(self):
        middle=len(self.blob)//2
        (self.root/'第三方依赖.zip.001').write_bytes(self.blob[:middle])
        (self.root/'第三方依赖.zip.002').write_bytes(self.blob[middle:])
    def test_canonical_handoff(self):
        (self.root/'第三方依赖.zip').write_bytes(self.blob)
        self.assertEqual(load_bundle(self.root,self.sha),self.blob)
    def test_split_source_reassembles_identical_zip(self):
        self.parts();blob=load_bundle(self.root,self.sha)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:self.assertEqual(archive.read('example/module.py'),b'value=42')
    def test_missing_split_rejected(self):
        self.parts();(self.root/'第三方依赖.zip.001').unlink()
        with self.assertRaises(RuntimeError):load_bundle(self.root,self.sha)
    def test_corrupt_split_rejected(self):
        self.parts();(self.root/'第三方依赖.zip.002').write_bytes(b'corrupt')
        with self.assertRaises(RuntimeError):load_bundle(self.root,self.sha)

    def test_handoff_keeps_all_dependency_parts_and_excludes_credentials(self):
        for name in ['第三方依赖.zip.001','第三方依赖.zip.002','第三方依赖.zip.003']:
            self.assertTrue(include(Path('06_运行环境/Windows_x64')/name))
        for name in ['.env','.env.production','connections.key','private.pem','api.key']:
            self.assertFalse(include(Path('01_程序代码/backend')/name))
        self.assertFalse(include(Path('02_数据资料/运行数据/uploads/private.pdf')))
        self.assertFalse(include(Path('01_程序代码/frontend/node_modules/example.js')))
        self.assertTrue(include(Path('01_程序代码/backend/.env.example')))

    def test_verify_shipped_runtime_detects_missing_payload(self):
        rt=self.root/'06_运行环境/Windows_x64';rt.mkdir(parents=True)
        (rt/'第三方依赖.sha256').write_text(self.sha)
        with self.assertRaises(RuntimeError):verify_runtime(self.root)
        middle=len(self.blob)//2
        (rt/'第三方依赖.zip.001').write_bytes(self.blob[:middle])
        (rt/'第三方依赖.zip.002').write_bytes(self.blob[middle:])
        result=verify_runtime(self.root)
        self.assertEqual(result['sha256'],self.sha)
        self.assertEqual(result['parts'],2)
        (rt/'第三方依赖.zip.002').write_bytes(b'broken')
        with self.assertRaises(RuntimeError):verify_runtime(self.root)


if __name__=='__main__':unittest.main()
