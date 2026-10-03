#!/bin/bash
set -euo pipefail
SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SERVICE_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 Python 3。请先安装 Python 3.10–3.12，再双击此文件。"
  read -r -p "按回车键退出..." _
  exit 1
fi
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10) or sys.version_info >= (3, 13):
    raise SystemExit("当前配置需要 Python 3.10–3.12。请安装兼容版本后再运行。")
PY
if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
python -m pip install -r requirements.txt
echo
echo "训练服务已启动：http://127.0.0.1:8766"
echo "保持此终端窗口打开。现在不会自动开始训练。"
python -m uvicorn api:app --host "${TRAINING_HOST:-127.0.0.1}" --port "${TRAINING_PORT:-8766}"
