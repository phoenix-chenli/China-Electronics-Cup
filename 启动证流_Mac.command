#!/bin/zsh
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
MAC_ENV="$HOME/Library/Application Support/EvidenceFlow/Python313"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
if [[ ! -x "$MAC_ENV/bin/python" ]]; then
  PY="$(command -v python3.13 || true)"
  if [[ -z "$PY" ]]; then
    echo "此 Mac 需要先安装 Python 3.13。Windows 版已内置运行环境。"
    read "?按回车关闭"
    exit 1
  fi
  "$PY" -m venv "$MAC_ENV"
  "$MAC_ENV/bin/python" -m pip install -r "$ROOT/01_程序代码/依赖清单/后端依赖锁定.txt" || { echo "依赖安装失败，请按开发说明重试。"; exit 1; }
fi
"$MAC_ENV/bin/python" -X utf8 "$ROOT/01_程序代码/启动与打包/launcher.py" start || { read "?操作未完成，按回车关闭"; exit 1; }
