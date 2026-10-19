#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 start.py --stop "$@"
stop_result=$?
if [[ $stop_result -ne 0 && -t 0 ]]; then
  read "?请阅读上方提示，按回车关闭窗口。"
fi
exit $stop_result
