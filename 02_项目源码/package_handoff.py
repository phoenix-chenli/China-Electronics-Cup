#!/usr/bin/env python3
"""Build only the portable source ZIP. Never run models, OCR, or delete old packages."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT = Path(__file__).resolve().parent
ARCHIVE_ROOT = '知衡作品'
ROOT_FILES = {
    '.gitignore', 'README.md', 'start.py', '校验项目.py', 'package_handoff.py',
    '启动知衡.command', '停止知衡.command', '首次准备.cmd', '启动知衡.cmd',
    '停止知衡.cmd', '环境检查.cmd',
}
ROOT_DIRS = {
    'backend', 'frontend', 'scripts', 'models', 'samples', 'schemas',
    'evaluation', 'tests', '知识库样本', '评测材料', '说明文档', '第三方许可', 'docs',
}
EXCLUDED = {
    'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache',
    '.ruff_cache', '.git', '.ds_store', 'private', '.private', 'secrets',
    'credentials', '.ssh', '.aws', '.azure', '.cache', '.mcp',
}
REQUIRED = ROOT_FILES | {
    'scripts/windows.ps1', 'scripts/prepare_inputs.py',
    'backend/platform_io.py', 'backend/local_server.py',
    'backend/requirements.lock.txt', 'backend/requirements.retrieval.lock.txt',
    'backend/requirements.windows.lock.txt', 'frontend/dist/index.html',
    'frontend/package.json', 'frontend/package-lock.json',
    'models/multilingual-e5-small/manifest.json',
    'models/multilingual-e5-small/LICENSE',
    '知识库样本/真实业务文档/资料清单.json',
    '评测材料/可迁移复现/输入清单.json',
    '评测材料/可迁移复现/输入清单.json.sha256',
    '评测材料/可迁移复现/数据与模型清单.json',
    '评测材料/可迁移复现/阅读说明.md',
    '说明文档/快速启动与环境说明.md', '说明文档/源码打包说明.md',
    '说明文档/运行配置示例_无密钥.json',
    '第三方许可/第三方许可证/psutil-7.1.0_LICENSE.txt',
} | {f'schemas/{name}_建表结构.sql' for name in (
    'Chinook', 'Northwind', '站点巡检数据库', '边界测试数据库',
    '性能受控记录_1000', '性能受控记录_10000',
)} | {f'评测材料/可迁移复现/离线评分/{name}.json' for name in (
    '冻结答案键', '补充冻结答案键', '多轮补充冻结答案键', '错别字补充冻结答案键',
)}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def excluded(name: str) -> bool:
    lower = name.lower()
    return lower in EXCLUDED or lower.startswith(('.runtime', '.env'))


def sensitive(name: str) -> bool:
    lower = name.lower()
    return lower in {'model.json', '.npmrc', '.pypirc', '.netrc', 'id_rsa', 'id_ed25519', 'id_ecdsa'} or lower.endswith(('.pem', '.key', '.p12', '.pfx'))


def checked_relative(value: str) -> str:
    rel = PurePosixPath(value)
    if not value or rel.is_absolute() or '..' in rel.parts or '\\' in value or ':' in value or str(rel) != value:
        raise ValueError('不规范的包内路径：' + value)
    return value


def selected_files(root: Path) -> list[Path]:
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = sorted(name for name in dirs if not excluded(name) and (base != root or name in ROOT_DIRS))
        for name in dirs:
            if (base / name).is_symlink():
                raise ValueError('交付目录不能是符号链接：' + str((base / name).relative_to(root)))
        for name in sorted(names):
            path = base / name
            if base == root and name not in ROOT_FILES:
                continue
            if excluded(name) or path.suffix in {'.pyc', '.pyo', '.tsbuildinfo'} or name.endswith(('.sqlite-wal', '.sqlite-shm')):
                continue
            relative = path.relative_to(root).as_posix()
            checked_relative(relative)
            if path.is_symlink() or sensitive(name):
                raise ValueError('发现不应交付的链接或凭据文件：' + relative)
            if path.is_file():
                if relative.startswith(('知识库样本/', 'samples/')) and any(token in name.lower() for token in ('答案', 'answer_key', 'reference_answer')):
                    raise ValueError('答案键不能放入业务知识库：' + relative)
                files.append(path)
    return sorted(files)


def inventory(root: Path, files: list[Path]) -> dict:
    return {
        'schema_version': 'zhiheng-source-handoff.v1',
        'purpose': '知衡完整可迁移源码、构建、模型许可、业务资料及隔离评测输入',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'archive_root': ARCHIVE_ROOT,
        'verification_boundary': '文件完整性与可迁移资产校验，不证明Windows实机、模型准确率或本轮端到端成绩。',
        'excluded': ['私人运行目录与密钥', '虚拟环境与node_modules', '维护用AGENTS.md', '旧验收证据目录', '外层报告和RAR'],
        'answer_key_boundary': '答案键只在评测材料/离线评分相关目录，不作为知识库导入来源。',
        'files': [{'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in files],
    }


def preflight(root: Path, files: list[Path], manifest: dict) -> dict:
    rows = {row['path']: row for row in manifest['files']}
    missing = REQUIRED - rows.keys()
    if missing:
        raise ValueError('缺少必要交付文件：' + ', '.join(sorted(missing)))

    def read(relative):
        return json.loads((root / checked_relative(relative)).read_text(encoding='utf-8'))

    def verify_asset(item):
        relative = checked_relative(item['path'])
        row = rows.get(relative)
        if row is None or row['sha256'] != item['sha256'] or ('bytes' in item and row['bytes'] != item['bytes']):
            raise ValueError('资产缺失或摘要不符：' + relative)

    portable = '评测材料/可迁移复现/'
    plan = read(portable + '输入清单.json')
    expected = (root / (portable + '输入清单.json.sha256')).read_text(encoding='utf-8').split()[0]
    if rows[portable + '输入清单.json']['sha256'] != expected or plan.get('schema_version') != 'zhiheng-portable-inputs.v1':
        raise ValueError('可迁移输入清单校验失败')
    assets = read(portable + '数据与模型清单.json')['assets']
    counts = dict(Counter(item['kind'] for item in assets))
    if counts != {'business_or_teaching': 4, 'controlled_document': 18, 'controlled_input': 3, 'model_or_license': 7, 'real_document': 12}:
        raise ValueError('当前交付数据规模与冻结清单不符：' + str(counts))
    if len(plan['cases']) != 45 or len(plan['sources']) != 21:
        raise ValueError('45场景/21来源输入不完整')
    for item in [*assets, *plan['sources']]:
        verify_asset(item)
    for source in plan['sources']:
        if '离线评分' in source['path'] or '答案' in source['path']:
            raise ValueError('复现导入来源不能包含答案键')
    real = read('知识库样本/真实业务文档/资料清单.json')['files']
    if len(real) != 12:
        raise ValueError('真实资料必须保留12份冻结原件')
    for item in real:
        verify_asset({**item, 'path': '知识库样本/真实业务文档/' + item['file']})
    model = read('models/multilingual-e5-small/manifest.json')
    for name, metadata in model['files'].items():
        verify_asset({**metadata, 'path': 'models/multilingual-e5-small/' + name})
    performance = [item for item in assets if item['path'].endswith('.sqlite') and '/性能尺度/' in item['path']]
    if len(performance) != 2:
        raise ValueError('缺少两个独立性能数据库')
    index = root / 'frontend/dist/index.html'
    for asset in re.findall(r'(?:src|href)="(/[^"?#]+)', index.read_text(encoding='utf-8')):
        if 'frontend/dist/' + asset.lstrip('/') not in rows:
            raise ValueError('前端构建引用文件缺失：' + asset)
    if any(p.stat().st_mtime > index.stat().st_mtime + 2 for p in files if p.relative_to(root).parts[:2] == ('frontend', 'src')):
        raise ValueError('前端源码比dist新；请先完成构建，再打包。此检查不会自动安装Node或重建。')
    return {'asset_counts': counts, 'portable_cases': 45, 'portable_source_ids': 21, 'performance_databases': 2, 'schema_files': 6, 'model_revision': model['revision']}


def write_archive(root: Path, output: Path, files: list[Path], manifest: dict) -> dict:
    if output.is_symlink() or output.exists():
        raise FileExistsError('拒绝覆盖已有压缩包：' + str(output))
    if output.suffix.lower() != '.zip' or output.resolve().is_relative_to(root.resolve()):
        raise ValueError('输出必须为源码目录之外的.zip文件')
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            created = True
            for path in files:
                archive.write(path, ARCHIVE_ROOT + '/' + path.relative_to(root).as_posix())
            archive.writestr(ARCHIVE_ROOT + '/项目文件清单.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        with zipfile.ZipFile(output) as archive:
            expected = {ARCHIVE_ROOT + '/' + row['path'] for row in manifest['files']} | {ARCHIVE_ROOT + '/项目文件清单.json'}
            if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected or archive.testzip() is not None:
                raise ValueError('ZIP结构或CRC校验失败')
            for row in manifest['files']:
                hasher = hashlib.sha256()
                count = 0
                with archive.open(ARCHIVE_ROOT + '/' + row['path']) as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        hasher.update(block)
                        count += len(block)
                if count != row['bytes'] or hasher.hexdigest() != row['sha256']:
                    raise ValueError('打包期间文件变化或ZIP摘要不一致：' + row['path'])
        return {'path': str(output), 'bytes': output.stat().st_size, 'sha256': digest(output), 'entries': len(expected), 'crc_and_all_file_hashes_verified': True}
    except BaseException:
        if created:
            output.unlink(missing_ok=True)  # Only this invocation's incomplete, newly-created ZIP.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-only', action='store_true', help='只校验和列出，不生成ZIP（默认）')
    mode.add_argument('--output', type=Path, help='源码目录之外的新ZIP路径；拒绝覆盖')
    parser.add_argument('--inventory', type=Path, help='另存本次预检清单；拒绝覆盖，不修改源码中的旧清单')
    args = parser.parse_args()
    files = selected_files(ROOT)
    manifest = inventory(ROOT, files)
    summary = preflight(ROOT, files, manifest)
    if args.inventory and (args.inventory.exists() or args.inventory.is_symlink() or args.inventory.resolve().is_relative_to(ROOT)):
        raise ValueError('预检清单必须使用源码之外的新路径')
    result = {'status': 'preflight_passed', 'source_files': len(files), 'archive_entries': len(files) + 1, 'uncompressed_bytes': sum(row['bytes'] for row in manifest['files']), **summary, 'model_requests': 0, 'ocr_runs': 0}
    if args.output:
        result['archive'] = write_archive(ROOT, args.output.expanduser().absolute(), files, manifest)
        result['status'] = 'source_zip_verified'
    if args.inventory:
        args.inventory.parent.mkdir(parents=True, exist_ok=True)
        with args.inventory.open('x', encoding='utf-8') as handle:
            json.dump({'summary': result, 'manifest': manifest}, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        raise SystemExit(str(exc))
