#!/bin/zsh
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
MAC_ENV="$HOME/Library/Application Support/EvidenceFlow/Python313"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
if [[ ! -x "$MAC_ENV/bin/python" ]]; then echo "此 Mac 尚未配置运行环境。"; exit 0; fi
"$MAC_ENV/bin/python" -X utf8 "$ROOT/01_程序代码/启动与打包/launcher.py" stop || { read "?操作未完成，按回车关闭"; exit 1; }
