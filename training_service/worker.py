from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path


def save_status(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main(task_folder: Path) -> int:
    status_file = task_folder / "status.json"
    config = json.loads((task_folder / "config.json").read_text(encoding="utf-8"))
    status = {"status": "running", "task_id": config["task_id"], "epoch": 0, "total_epochs": config["epochs"], "message": "正在载入 YOLO 模型", "metrics": {}}
    save_status(status_file, status)
    try:
        import torch
        from ultralytics import YOLO

        requested = config["device"]
        if requested == "auto":
            device = "0" if torch.cuda.is_available() else "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
        elif requested == "cuda:0":
            if not torch.cuda.is_available():
                raise RuntimeError("已选择 CUDA，但当前 PyTorch 未检测到 NVIDIA CUDA 设备")
            device = "0"
        elif requested == "mps":
            if not hasattr(torch.backends, "mps") or not torch.backends.mps.is_available():
                raise RuntimeError("已选择 Apple MPS，但当前 PyTorch 未检测到可用 MPS 设备")
            device = "mps"
        else:
            device = "cpu"
        status.update({"device": device, "message": f"正在使用 {device} 设备训练"})
        save_status(status_file, status)
        model = YOLO(config["model"])

        def on_fit_epoch_end(trainer):
            metrics = {}
            for key, value in getattr(trainer, "metrics", {}).items():
                try:
                    metrics[str(key)] = round(float(value), 6)
                except (TypeError, ValueError):
                    continue
            state = {"status": "running", "task_id": config["task_id"], "epoch": int(trainer.epoch) + 1, "total_epochs": config["epochs"], "message": f"训练中：第 {int(trainer.epoch) + 1} / {config['epochs']} 轮", "metrics": metrics, "device": device}
            save_status(status_file, state)

        model.add_callback("on_fit_epoch_end", on_fit_epoch_end)
        result = model.train(
            data=config["data"], epochs=config["epochs"], imgsz=config["imgsz"],
            batch=config["batch"], device=device, workers=config["workers"],
            patience=config["patience"], project=config["project"], name=config["name"],
            exist_ok=True, plots=True, save=True, verbose=True,
        )
        trainer = getattr(model, "trainer", None)
        save_dir = Path(getattr(trainer, "save_dir", Path(config["project"]) / config["name"])).resolve()
        best = save_dir / "weights" / "best.pt"
        metric_source = getattr(trainer, "metrics", None)
        if not isinstance(metric_source, Mapping):
            metric_source = result if isinstance(result, Mapping) else {}
        metrics = {}
        for key, value in metric_source.items():
            try:
                metrics[str(key)] = round(float(value), 6)
            except (TypeError, ValueError):
                pass
        final = {"status": "completed", "task_id": config["task_id"], "epoch": config["epochs"], "total_epochs": config["epochs"], "message": "训练完成", "metrics": metrics, "device": device, "output_dir": str(save_dir), "weights_path": str(best) if best.is_file() else None}
        save_status(status_file, final)
        return 0
    except Exception as exc:
        save_status(status_file, {"status": "error", "task_id": config.get("task_id"), "epoch": status.get("epoch", 0), "total_epochs": config.get("epochs", 0), "message": str(exc), "traceback": traceback.format_exc(), "metrics": {}})
        raise


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
