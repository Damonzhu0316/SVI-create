from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("TRAINING_DATA_DIR", BASE_DIR / "data")).resolve()
RUNS_DIR = Path(os.getenv("TRAINING_RUNS_DIR", BASE_DIR / "runs")).resolve()
WORKER = BASE_DIR / "worker.py"
DEFAULT_CONFIG = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", DEFAULT_CONFIG.get("max_upload_mb", 2048)))
MAX_UNPACKED_GB = int(os.getenv("MAX_UNPACKED_GB", DEFAULT_CONFIG.get("max_unpacked_gb", 20)))
ALLOWED_MODELS = {"yolov8n.pt", "yolov8s.pt", "yolov8m.pt"}
DATA_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="街景智析本地训练服务", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8765", "http://localhost:8765",
        "http://127.0.0.1:8000", "http://localhost:8000",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
processes: dict[str, subprocess.Popen] = {}


def task_dir(task_id: str) -> Path:
    if not task_id.isalnum():
        raise HTTPException(400, "任务编号无效")
    path = RUNS_DIR / task_id
    if not path.is_dir():
        raise HTTPException(404, "找不到此训练任务")
    return path


def read_status(folder: Path) -> dict[str, Any]:
    status_file = folder / "status.json"
    if not status_file.exists():
        return {"status": "queued", "epoch": 0, "total_epochs": 0, "message": "等待训练进程"}
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "error", "message": "无法读取训练状态"}


def write_json(path: Path, data: dict[str, Any]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def safe_extract(archive: Path, destination: Path) -> None:
    total_size = 0
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if info.flag_bits & 0x1:
                raise HTTPException(400, "训练数据 ZIP 不支持加密文件")
            total_size += info.file_size
            if total_size > MAX_UNPACKED_GB * 1024**3:
                raise HTTPException(413, f"解压后数据超过 {MAX_UNPACKED_GB} GB 上限")
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts:
                raise HTTPException(400, "ZIP 内包含不安全路径")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise HTTPException(400, "训练数据 ZIP 不支持符号链接")
            target = (root / Path(*name.parts)).resolve()
            if root not in target.parents and target != root:
                raise HTTPException(400, "ZIP 内包含越界路径")
        bundle.extractall(root)


def locate_dataset(extracted: Path) -> Path:
    candidates = list(extracted.rglob("data.yaml")) + list(extracted.rglob("data.yml"))
    if len(candidates) != 1:
        raise HTTPException(400, "训练包中必须且只能包含一个 data.yaml")
    yaml_path = candidates[0]
    try:
        config = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HTTPException(400, f"data.yaml 无法读取：{exc}") from exc
    if not isinstance(config, dict) or not config.get("names"):
        raise HTTPException(400, "data.yaml 缺少类别 names 配置")
    root_value = config.get("path", ".")
    dataset_root = (yaml_path.parent / str(root_value)).resolve()
    if extracted.resolve() not in dataset_root.parents and dataset_root != extracted.resolve():
        raise HTTPException(400, "data.yaml 的 path 不能指向训练包外部")
    normalized: dict[str, Any] = {"path": str(dataset_root), "names": config["names"]}
    image_count = 0
    for split in ("train", "val"):
        split_image_count = 0
        split_value = config.get(split)
        if not split_value:
            raise HTTPException(400, f"data.yaml 缺少 {split} 数据路径")
        values = split_value if isinstance(split_value, list) else [split_value]
        resolved = []
        for value in values:
            location = (dataset_root / str(value)).resolve()
            if dataset_root not in location.parents and location != dataset_root:
                raise HTTPException(400, f"{split} 数据路径不能指向训练包外部")
            if not location.exists():
                raise HTTPException(400, f"找不到 {split} 图片目录：{value}")
            resolved.append(str(location))
            split_image_count += sum(1 for item in location.rglob("*") if item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"})
        if split_image_count < 1:
            raise HTTPException(400, f"{split} 划分中没有图片；至少要有 1 张训练图和 1 张验证图")
        image_count += split_image_count
        normalized[split] = resolved if isinstance(split_value, list) else resolved[0]
    if image_count < 2:
        raise HTTPException(400, "训练集和验证集至少需要合计 2 张图片；请检查数据划分")
    if isinstance(config["names"], dict):
        class_count = len(config["names"])
    elif isinstance(config["names"], list):
        class_count = len(config["names"])
    else:
        raise HTTPException(400, "类别 names 必须是列表或编号映射")
    if class_count < 1:
        raise HTTPException(400, "至少需要一个目标类别")
    if not isinstance(config["names"], (dict, list)):
        raise HTTPException(400, "类别 names 必须是列表或编号映射")
    labels_root = dataset_root / "labels"
    if not labels_root.is_dir():
        raise HTTPException(400, "数据集中缺少 labels/train 与 labels/val 标注目录")
    total_boxes = 0
    for split in ("train", "val"):
        split_labels = labels_root / split
        if not split_labels.is_dir():
            raise HTTPException(400, f"数据集中缺少 labels/{split} 标注目录")
        for label_file in split_labels.rglob("*.txt"):
            for line_number, line in enumerate(label_file.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                fields = line.split()
                if len(fields) != 5:
                    raise HTTPException(400, f"标注格式错误：{label_file.name} 第 {line_number} 行应包含 5 个 YOLO 字段")
                try:
                    category = int(fields[0])
                    coords = [float(value) for value in fields[1:]]
                except ValueError as exc:
                    raise HTTPException(400, f"标注包含非数值字段：{label_file.name} 第 {line_number} 行") from exc
                if not 0 <= category < class_count or any(not 0 <= value <= 1 for value in coords) or coords[2] <= 0 or coords[3] <= 0:
                    raise HTTPException(400, f"标注类别或框坐标超出范围：{label_file.name} 第 {line_number} 行")
                total_boxes += 1
    if total_boxes == 0:
        raise HTTPException(400, "训练标注中没有目标框；请先完成模型预标注并人工校正")
    normalized_path = yaml_path.parent / "service_data.yaml"
    normalized_path.write_text(yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return normalized_path


@app.middleware("http")
async def protect_local_training_service(request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    origin = request.headers.get("origin")
    allowed_hosts = {"127.0.0.1", "localhost"}
    allowed_origins = {"http://127.0.0.1:8765", "http://localhost:8765", "http://127.0.0.1:8000", "http://localhost:8000"}
    if host not in allowed_hosts:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Only loopback access is allowed"}, status_code=403)
    if origin and origin not in allowed_origins:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def health() -> dict[str, Any]:
    info: dict[str, Any] = {"ok": True, "service": "street-scene-training", "python": sys.version.split()[0], "device": "cpu", "torch": None, "ultralytics": None}
    try:
        import torch
        info["torch"] = torch.__version__
        if torch.cuda.is_available():
            info["device"] = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            info["device"] = "mps"
    except Exception as exc:
        info["torch_error"] = str(exc)
    try:
        import ultralytics
        info["ultralytics"] = ultralytics.__version__
    except Exception as exc:
        info["ok"] = False
        info["ultralytics_error"] = str(exc)
    info["active_task"] = next((task_id for task_id, proc in processes.items() if proc.poll() is None), None)
    return info


@app.post("/api/training/start")
async def start_training(
    dataset: UploadFile = File(...),
    model: str = Form(DEFAULT_CONFIG.get("model", "yolov8n.pt")),
    epochs: int = Form(DEFAULT_CONFIG.get("epochs", 100)),
    imgsz: int = Form(DEFAULT_CONFIG.get("imgsz", 640)),
    batch: int = Form(DEFAULT_CONFIG.get("batch", 8)),
    device: str = Form(DEFAULT_CONFIG.get("device", "auto")),
    workers: int = Form(DEFAULT_CONFIG.get("workers", 2)),
    patience: int = Form(DEFAULT_CONFIG.get("patience", 30)),
) -> dict[str, Any]:
    if model not in ALLOWED_MODELS:
        raise HTTPException(400, "基础模型仅支持 yolov8n/s/m.pt")
    if not 1 <= epochs <= 1000 or not 320 <= imgsz <= 1280 or not 1 <= batch <= 128:
        raise HTTPException(400, "训练参数超出允许范围")
    if imgsz % 32:
        raise HTTPException(400, "图像尺寸建议使用 32 的倍数")
    if not 0 <= workers <= 16 or not 1 <= patience <= 300:
        raise HTTPException(400, "workers 或 patience 参数超出允许范围")
    if device not in {"auto", "cpu", "mps", "cuda:0"}:
        raise HTTPException(400, "设备选项无效")
    active = [task_id for task_id, proc in processes.items() if proc.poll() is None]
    if active:
        raise HTTPException(409, f"训练任务 {active[0]} 正在运行；当前配置一次只允许一个训练任务")
    if not dataset.filename or not dataset.filename.lower().endswith(".zip"):
        raise HTTPException(400, "请上传 YOLO 数据集 ZIP 包")

    task_id = uuid.uuid4().hex
    folder = RUNS_DIR / task_id
    folder.mkdir(parents=True)
    archive = folder / "dataset.zip"
    written = 0
    with archive.open("wb") as output:
        while chunk := await dataset.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_MB * 1024**2:
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(413, f"上传文件超过 {MAX_UPLOAD_MB} MB 上限")
            output.write(chunk)
    extracted = folder / "dataset"
    extracted.mkdir()
    try:
        safe_extract(archive, extracted)
        data_yaml = locate_dataset(extracted)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(400, f"训练数据 ZIP 无法解压：{exc}") from exc
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    archive.unlink(missing_ok=True)

    config = {"model": model, "epochs": epochs, "imgsz": imgsz, "batch": batch, "device": device, "workers": workers, "patience": patience, "data": str(data_yaml), "task_id": task_id, "project": str(folder / "outputs"), "name": "street-scene"}
    write_json(folder / "config.json", config)
    write_json(folder / "status.json", {"status": "queued", "task_id": task_id, "epoch": 0, "total_epochs": epochs, "message": "任务已提交，正在准备训练环境", "metrics": {}})
    log_file = (folder / "train.log").open("w", encoding="utf-8")
    try:
        process = subprocess.Popen([sys.executable, "-u", str(WORKER), str(folder)], cwd=BASE_DIR, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
        processes[task_id] = process
    except OSError as exc:
        log_file.close()
        write_json(folder / "status.json", {"status": "error", "task_id": task_id, "message": f"训练进程无法启动：{exc}"})
        raise HTTPException(500, "训练进程无法启动") from exc
    log_file.close()
    return {"task_id": task_id, "status": "queued", "message": "本机训练任务已创建"}


@app.get("/api/training/tasks")
def list_tasks() -> list[dict[str, Any]]:
    tasks = []
    for folder in RUNS_DIR.iterdir():
        if not folder.is_dir() or not (folder / "config.json").exists():
            continue
        status = read_status(folder)
        try:
            config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}
        process = processes.get(folder.name)
        if process and process.poll() is not None and status.get("status") in {"queued", "running"}:
            status = {**status, "status": "error", "message": f"训练进程已退出（代码 {process.returncode}），请查看日志"}
            write_json(folder / "status.json", status)
        tasks.append({"task_id": folder.name, "status": status.get("status", "queued"), "message": status.get("message", ""), "epoch": status.get("epoch", 0), "total_epochs": status.get("total_epochs", config.get("epochs", 0)), "model": config.get("model", ""), "created_at": folder.stat().st_mtime, "weights_path": status.get("weights_path")})
    return sorted(tasks, key=lambda item: item["created_at"], reverse=True)[:30]


@app.get("/api/training/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    folder = task_dir(task_id)
    result = read_status(folder)
    result["task_id"] = task_id
    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        result["imgsz"] = config.get("imgsz", 640)
        dataset_config = yaml.safe_load(Path(config["data"]).read_text(encoding="utf-8"))
        names = dataset_config.get("names", [])
        result["class_names"] = [names[key] for key in sorted(names, key=lambda value: int(value))] if isinstance(names, dict) else names
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        result["class_names"] = []
    process = processes.get(task_id)
    if process and process.poll() is not None and result.get("status") in {"queued", "running"}:
        result = {**result, "status": "error", "message": f"训练进程已退出（代码 {process.returncode}），请查看日志"}
        write_json(folder / "status.json", result)
    log = folder / "train.log"
    if log.exists():
        try:
            result["log_tail"] = "".join(log.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-80:])
        except OSError:
            result["log_tail"] = ""
    return result


@app.post("/api/training/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, str]:
    folder = task_dir(task_id)
    process = processes.get(task_id)
    if not process or process.poll() is not None:
        raise HTTPException(409, "当前任务不在运行")
    process.terminate()
    write_json(folder / "status.json", {"status": "cancelled", "task_id": task_id, "message": "用户已停止训练"})
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/training/tasks/{task_id}/weights")
def download_weights(task_id: str):
    folder = task_dir(task_id)
    status = read_status(folder)
    if status.get("status") != "completed" or not status.get("weights_path"):
        raise HTTPException(409, "训练完成后才可以下载权重")
    weights = Path(status["weights_path"]).resolve()
    if folder.resolve() not in weights.parents or not weights.is_file():
        raise HTTPException(404, "训练权重文件不存在")
    return FileResponse(weights, filename="street-scene-best.pt", media_type="application/octet-stream")


@app.post("/api/training/tasks/{task_id}/onnx")
def export_trained_onnx(task_id: str):
    folder = task_dir(task_id)
    status = read_status(folder)
    if status.get("status") != "completed" or not status.get("weights_path"):
        raise HTTPException(409, "训练完成后才可以转换浏览器模型")
    weights = Path(status["weights_path"]).resolve()
    if folder.resolve() not in weights.parents or not weights.is_file():
        raise HTTPException(404, "训练权重文件不存在")
    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        from ultralytics import YOLO

        exported = YOLO(str(weights)).export(
            format="onnx", imgsz=int(config.get("imgsz", 640)),
            simplify=False, nms=False,
        )
        onnx_path = Path(str(exported)).resolve()
    except Exception as exc:
        raise HTTPException(500, f"ONNX 转换失败：{exc}") from exc
    if folder.resolve() not in onnx_path.parents or not onnx_path.is_file() or onnx_path.suffix.lower() != ".onnx":
        raise HTTPException(500, "转换器没有在当前训练任务目录内生成 ONNX 文件")
    return FileResponse(
        onnx_path,
        filename=f"street-scene-{task_id[:8]}.onnx",
        media_type="application/octet-stream",
    )
