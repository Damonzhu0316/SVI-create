#!/bin/bash
set -euo pipefail
SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SERVICE_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 Python 3；请先安装 Python 3.10 或更高版本。"
  read -r -p "按回车键退出..." _
  exit 1
fi
if [ ! -d .venv ]; then "$PYTHON_BIN" -m venv .venv; fi
source .venv/bin/activate
python -m pip install -r requirements.txt
echo
echo "本机街景采集服务已启动：http://127.0.0.1:8767"
echo "服务不需要 API Key、Cookie 或登录。遇到来源服务拒绝请求时会停止。"
python -m uvicorn api:app --host 127.0.0.1 --port 8767
