# 本机 YOLO 训练服务

本服务供“街景智析”训练数据页面调用，只监听 `127.0.0.1:8766`。双击 `start_training_service.command` 会创建独立 `.venv`、安装 `requirements.txt` 并启动 API；它**不会自行开始训练**。在前端点“检查服务状态”只做依赖/设备探测。只有点击“开始真实训练”并在确认框同意后，前端才会将当前训练 ZIP 发到本机服务并启动任务。

## 环境

- macOS / Linux，Python 3.10–3.12
- CPU 可运行；支持环境下自动使用 NVIDIA CUDA 或 Apple MPS
- 训练权重首次运行时由 Ultralytics 下载。当前没有安装依赖或下载权重。

## 页面配置

基础模型：`yolov8n.pt` / `yolov8s.pt` / `yolov8m.pt`；轮次 1–1000；图像尺寸 320–1280 且需为 32 的倍数；批次 1–128；设备自动、CPU、MPS 或 CUDA；workers 0–16；early-stopping patience 1–300。服务一次只接受一个训练任务。

## 数据格式

API 接收前端导出的 YOLO ZIP，要求唯一的 `data.yaml`、`names`、`train` 和 `val`，并且训练/验证路径必须在压缩包目录内。训练与验证图片合计至少 2 张。前端按所选验证比例拆分图片，将 `bbox_percent` 转成 YOLO `class cx cy width height` 格式。

## API

- `GET /api/health`：Python、PyTorch、Ultralytics 与默认设备
- `POST /api/training/start`：验证数据并创建后台训练任务
- `GET /api/training/tasks/{task_id}`：任务状态、轮次、指标和日志末尾
- `POST /api/training/tasks/{task_id}/cancel`：停止任务
- `GET /api/training/tasks/{task_id}/weights`：下载 `best.pt`
- `POST /api/training/tasks/{task_id}/onnx`：在本机把完成训练的 `best.pt` 转为不带 NMS 的 ONNX 并下载，供浏览器自定义模型入口使用

任务目录与模型权重写入 `training_service/runs/<task_id>/`。训练过程遵循 Ultralytics 的 Python `YOLO(...).train(...)` 接口，完成后记录验证指标与权重路径。该服务默认允许本机前端来源，并拒绝非 loopback Host/Origin；不要把监听地址改为 `0.0.0.0`。
