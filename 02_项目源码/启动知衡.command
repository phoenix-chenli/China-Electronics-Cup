#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 start.py "$@"
launch_result=$?
if [[ $launch_result -ne 0 && -t 0 ]]; then
  read "?启动未完成，按回车关闭窗口。"
fi
exit $launch_result
