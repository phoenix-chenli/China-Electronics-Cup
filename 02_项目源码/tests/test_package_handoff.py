"""Handoff boundaries are checked without packaging real private runtime or model data."""
from pathlib import Path
import tempfile
import unittest
import zipfile

import package_handoff as handoff


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'source'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, relative, data='safe data'):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding='utf-8')
        return path

    def test_windows_entries_and_scripts_are_selected(self):
        names = ['首次准备.cmd', '启动知衡.cmd', '停止知衡.cmd', '环境检查.cmd', 'scripts/windows.ps1', 'scripts/prepare_inputs.py', 'backend/requirements.windows.lock.txt']
        for name in names:
            self.put(name)
        self.assertEqual({p.relative_to(self.root).as_posix() for p in handoff.selected_files(self.root)}, set(names))

    def test_runtime_dependencies_and_private_directories_never_enter_archive(self):
        self.put('README.md')
        for name in ['.runtime/model.json', '.runtime-old/store.sqlite', '.venv/bin/python', 'frontend/node_modules/package/a.js', 'backend/private/key.json', 'scripts/.env', 'backend/__pycache__/a.pyc', '验收证据/旧截图.png']:
            self.put(name)
        self.assertEqual([p.relative_to(self.root).as_posix() for p in handoff.selected_files(self.root)], ['README.md'])

    def test_unexpected_credential_and_symlink_fail_closed(self):
        secret = self.put('backend/model.json')
        with self.assertRaises(ValueError):
            handoff.selected_files(self.root)
        secret.unlink()
        self.put('README.md')
        (self.root / 'backend' / 'linked.py').symlink_to(self.root / 'README.md')
        with self.assertRaises(ValueError):
            handoff.selected_files(self.root)

    def test_answer_key_is_separate_from_business_sources(self):
        self.put('评测材料/可迁移复现/离线评分/冻结答案键.json')
        self.assertEqual(len(handoff.selected_files(self.root)), 1)
        self.put('知识库样本/错误答案键.json')
        with self.assertRaises(ValueError):
            handoff.selected_files(self.root)

    def test_archive_has_single_root_fresh_manifest_and_verified_hashes(self):
        self.put('README.md', '中文交付')
        files = handoff.selected_files(self.root)
        manifest = handoff.inventory(self.root, files)
        target = self.base / 'source.zip'
        result = handoff.write_archive(self.root, target, files, manifest)
        self.assertTrue(result['crc_and_all_file_hashes_verified'])
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {'知衡作品/README.md', '知衡作品/项目文件清单.json'})
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            handoff.write_archive(self.root, target, files, manifest)
        self.assertEqual(target.read_bytes(), before)

    def test_changed_source_aborts_and_removes_only_new_incomplete_zip(self):
        source = self.put('README.md', 'before')
        files = handoff.selected_files(self.root)
        manifest = handoff.inventory(self.root, files)
        source.write_text('after', encoding='utf-8')
        target = self.base / 'changed.zip'
        with self.assertRaises(ValueError):
            handoff.write_archive(self.root, target, files, manifest)
        self.assertFalse(target.exists())
        self.assertEqual(source.read_text(encoding='utf-8'), 'after')

    def test_paths_and_required_assets_are_validated(self):
        for value in ['../escape', '/absolute', 'C:/drive', r'folder\file', 'folder/../file']:
            with self.assertRaises(ValueError):
                handoff.checked_relative(value)
        self.put('README.md')
        files = handoff.selected_files(self.root)
        with self.assertRaisesRegex(ValueError, '必要交付文件'):
            handoff.preflight(self.root, files, handoff.inventory(self.root, files))


if __name__ == '__main__':
    unittest.main()
