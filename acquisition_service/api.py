from __future__ import annotations

import base64
from collections import OrderedDict
from io import BytesIO
import json
import heapq
import math
import os
import re
import shutil
import sys
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from pyproj import Geod
import pyogrio
from PIL import Image

def load_local_config() -> None:
    config_path = Path(__file__).with_name("ai_config.local.env")
    if not config_path.exists():
        return
    allowed = {"AI_BASE_URL", "AI_API_KEY", "AI_MODEL", "NOMINATIM_BASE_URL"}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key in allowed and value:
            os.environ.setdefault(key, value)


load_local_config()
AI_REQUIRED_SETTINGS = ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL")


PROJECT_DIR = Path(__file__).resolve().parent.parent
LOCAL_ROADS_PATH = PROJECT_DIR / "2025年全国道路铁路数据" / "全国道路数据.shp"
PROJECTS_DIR = Path(os.environ.get("STREETVISION_PROJECTS_DIR", Path.home() / "街景智析" / "工程")).expanduser().resolve()
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_PATH_REGISTRY = PROJECTS_DIR / ".project_paths.json"
HNU_SCRIPTS = PROJECT_DIR / "HNU_StreetView_Front_v6" / "scripts"
sys.path.insert(0, str(HNU_SCRIPTS))
import front_view as baidu  # noqa: E402
from coordinate_transform import bd09_to_wgs84, wgs84_to_gcj02  # noqa: E402

app = FastAPI(title="街景智析本地街景采集服务", version="1.0.0")
ALLOWED_ORIGINS = {
    "http://127.0.0.1:8000", "http://localhost:8000",
    "http://127.0.0.1:8765", "http://localhost:8765",
    "http://127.0.0.1:8766", "http://localhost:8766", "null",
}
app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS), allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
GEOD = Geod(ellps="WGS84")
_overpass_lock = Lock()
_last_overpass = 0.0
_geocoder_lock = Lock()
_last_geocoder = 0.0
_geocoder_cache: dict[str, list[dict[str, Any]]] = {}
_local_road_cache_lock = Lock()
_local_road_cache: dict[str, dict[str, Any]] = {}
_preview_cache_lock = Lock()
_preview_cache: OrderedDict[tuple[float, float, float], tuple[dict[str, Any], bytes]] = OrderedDict()
PREVIEW_CACHE_LIMIT = 32
ADMIN_REGIONS_API = "https://regions-data.vercel.app/api"
_admin_regions_cache_lock = Lock()
_admin_regions_cache: dict[str, dict[str, Any]] = {}


class BoundsRequest(BaseModel):
    west: float
    south: float
    east: float
    north: float
    spacing_m: int = Field(default=100, ge=30, le=1000)
    road_ids: list[str] | None = None


class PlaceSearchRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200)


class RouteRequest(BaseModel):
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    spacing_m: int = Field(default=100, ge=30, le=1000)


class PointRequest(BaseModel):
    latitude: float
    longitude: float
    heading: float = Field(default=0, ge=0, lt=360)
    point_id: str | None = None
    road_id: str | None = None


def local_bd09(lon: float, lat: float) -> tuple[float, float]:
    gx, gy = wgs84_to_gcj02(lon, lat)
    z = math.hypot(gx, gy) + 0.00002 * math.sin(gy * math.pi * 3000.0 / 180.0)
    theta = math.atan2(gy, gx) + 0.000003 * math.cos(gx * math.pi * 3000.0 / 180.0)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def validate_bounds(b: BoundsRequest) -> None:
    if not (-180 <= b.west < b.east <= 180 and -74 <= b.south < b.north <= 74):
        raise HTTPException(400, "范围经纬度无效")
    height_km = (b.north - b.south) * 111.2
    width_km = (b.east - b.west) * 111.2 * max(0.1, math.cos(math.radians((b.north + b.south) / 2)))
    if height_km > 20 or width_km > 20 or width_km * height_km > 400:
        raise HTTPException(413, "单次范围最多 20 km × 20 km、面积不超过 400 km²；请缩小范围分批查询")


def overpass_roads(b: BoundsRequest) -> list[dict[str, Any]]:
    global _last_overpass
    query = f'[out:json][timeout:25];way["highway"]({b.south},{b.west},{b.north},{b.east});out body geom;'
    with _overpass_lock:
        wait = 2.0 - (time.monotonic() - _last_overpass)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            "https://overpass-api.de/api/interpreter",
            data=urllib.parse.urlencode({"data": query}).encode(),
            headers={"User-Agent": "StreetSceneResearchWorkbench/1.0 (local research tool)", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                _last_overpass = time.monotonic()
                body = response.read(25_000_001)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            _last_overpass = time.monotonic()
            raise HTTPException(502, f"OSM 道路查询失败：{exc}") from exc
    if len(body) > 25_000_000:
        raise HTTPException(413, "道路数据过大，请缩小范围")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "OSM 返回的数据无法解析") from exc
    return [way for way in payload.get("elements", []) if way.get("type") == "way" and len(way.get("geometry", [])) >= 2]


def road_samples(ways: list[dict[str, Any]], spacing: float) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for way in ways:
        coords = way.get("geometry", [])
        tags = way.get("tags", {})
        way_id = str(way.get("id", ""))
        chainage = 0.0
        next_sample = 0.0
        for a, b in zip(coords, coords[1:]):
            lon1, lat1, lon2, lat2 = float(a["lon"]), float(a["lat"]), float(b["lon"]), float(b["lat"])
            azimuth, _, length = GEOD.inv(lon1, lat1, lon2, lat2)
            length = abs(length)
            if length < 0.01:
                continue
            while next_sample <= chainage + length and len(samples) < 5000:
                offset = max(0.0, next_sample - chainage)
                lon, lat, _ = GEOD.fwd(lon1, lat1, azimuth, offset)
                samples.append({
                    "point_id": f"osm_{way_id}_{len(samples) + 1:05d}",
                    "latitude": lat, "longitude": lon, "heading": azimuth % 360,
                    "road_id": way_id, "road_name": tags.get("name", "未命名道路"),
                    "road_type": tags.get("highway", "unknown"),
                })
                next_sample += spacing
            chainage += length
        if len(samples) >= 5000:
            break
    return samples


def route_between_points(req: RouteRequest) -> dict[str, Any]:
    if not all(math.isfinite(v) for v in (req.start_lat, req.start_lon, req.end_lat, req.end_lon)):
        raise HTTPException(400, "起点或终点坐标无效")
    if not all(-74 <= lat <= 74 and -180 <= lon <= 180 for lat, lon in ((req.start_lat, req.start_lon), (req.end_lat, req.end_lon))):
        raise HTTPException(400, "起点或终点超出有效经纬度范围")
    pad = 0.003
    bounds = BoundsRequest(west=max(-180, min(req.start_lon, req.end_lon) - pad),
                           south=max(-74, min(req.start_lat, req.end_lat) - pad),
                           east=min(180, max(req.start_lon, req.end_lon) + pad),
                           north=min(74, max(req.start_lat, req.end_lat) + pad),
                           spacing_m=req.spacing_m)
    validate_bounds(bounds)
    ways = overpass_roads(bounds)
    graph: dict[str, list[tuple[str, float, str]]] = {}
    coords: dict[str, tuple[float, float]] = {}
    way_meta: dict[str, dict[str, str]] = {}
    for way in ways:
        geometry = way.get("geometry", [])
        refs = way.get("nodes", [])
        if len(refs) != len(geometry):
            refs = [f'{float(p["lon"]):.7f},{float(p["lat"]):.7f}' for p in geometry]
        way_id = str(way.get("id", ""))
        tags = way.get("tags", {})
        way_meta[way_id] = {"road_name": tags.get("name", "未命名道路"), "road_type": tags.get("highway", "unknown")}
        keys = [str(ref) for ref in refs]
        for key, point in zip(keys, geometry):
            coords[key] = (float(point["lon"]), float(point["lat"]))
        for a, b, pa, pb in zip(keys, keys[1:], geometry, geometry[1:]):
            _, _, distance = GEOD.inv(float(pa["lon"]), float(pa["lat"]), float(pb["lon"]), float(pb["lat"]))
            if distance <= 0:
                continue
            graph.setdefault(a, []).append((b, distance, way_id))
            graph.setdefault(b, []).append((a, distance, way_id))
    if not graph:
        raise HTTPException(404, "附近没有可用于路径匹配的道路，请放大地图后重试")

    def nearest_node(lat: float, lon: float) -> str:
        key = min(graph, key=lambda candidate: GEOD.inv(lon, lat, *coords[candidate])[2])
        distance = abs(GEOD.inv(lon, lat, *coords[key])[2])
        if distance > 180:
            raise HTTPException(422, f"地图点击点距离最近道路节点 {distance:.0f} 米；请把点放在道路附近")
        return key

    start, finish = nearest_node(req.start_lat, req.start_lon), nearest_node(req.end_lat, req.end_lon)
    distances = {start: 0.0}
    previous: dict[str, tuple[str, str]] = {}
    queue = [(0.0, start)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances.get(node):
            continue
        if node == finish:
            break
        for neighbor, weight, way_id in graph.get(node, []):
            candidate = distance + weight
            if candidate < distances.get(neighbor, float("inf")):
                distances[neighbor] = candidate
                previous[neighbor] = (node, way_id)
                heapq.heappush(queue, (candidate, neighbor))
    if finish not in distances:
        raise HTTPException(404, "起点和终点之间没有找到连通道路，请调整位置或缩短路段")
    path = [finish]
    edge_ways: list[str] = []
    while path[-1] != start:
        parent, way_id = previous[path[-1]]
        edge_ways.append(way_id)
        path.append(parent)
    path.reverse()
    edge_ways.reverse()
    geometry = [coords[key] for key in path]
    selected_ways = list(dict.fromkeys(edge_ways))
    samples: list[dict[str, Any]] = []
    chainage = next_sample = 0.0
    for index, (a, b) in enumerate(zip(geometry, geometry[1:])):
        azimuth, _, length = GEOD.inv(a[0], a[1], b[0], b[1])
        length = abs(length)
        way_id = edge_ways[index]
        while next_sample <= chainage + length and len(samples) < 5000:
            lon, lat, _ = GEOD.fwd(a[0], a[1], azimuth, max(0.0, next_sample - chainage))
            meta = way_meta[way_id]
            samples.append({"point_id": f"route_{way_id}_{len(samples) + 1:05d}", "latitude": lat,
                            "longitude": lon, "heading": azimuth % 360, "road_id": way_id,
                            "road_name": meta["road_name"], "road_type": meta["road_type"], "selected": True})
            next_sample += req.spacing_m
        chainage += length
    return {"road_ids": selected_ways, "road_count": len(selected_ways), "distance_m": round(distances[finish], 1),
            "geometry": [[lat, lon] for lon, lat in geometry], "points": samples}


def extract_location_query(text: str) -> str:
    raw = " ".join(text.strip().split())
    if not raw or len(raw) > 200:
        raise HTTPException(422, "请输入一个地点名称，例如“南京市江宁区”或“长沙岳麓区大学城”。")
    unrelated_words = r"天气|论文|代码|编程|翻译|笑话|新闻|菜谱|解释|怎么做|写作|聊天"
    if re.search(unrelated_words, raw):
        raise HTTPException(422, "请输入地点名称，或描述你想定位的区域；例如“南京市江宁区”或“长沙岳麓区大学城周边”。")
    query = raw.strip(" ，,。.!！?？:：")
    prefixes = ("我想要", "我需要", "我想", "帮我", "请帮我", "请", "计划采集", "准备采集", "采集", "收集", "获取", "下载", "定位", "查询", "搜索", "我希望", "希望", "想要", "计划", "准备", "想")
    while True:
        prefix = next((item for item in prefixes if query.startswith(item)), None)
        if prefix is None:
            break
        query = query[len(prefix):].lstrip(" ，,。.!！?？:：")
    query = re.sub(r"^(?:在|去|到)+", "", query)
    query = re.sub(r"(?:街景数据|街景图片|街景|附近|周边|周围|一带|范围|区域)+$", "", query)
    query = query.removesuffix("的").strip(" ，,。.!！?？:：")
    if len(query) < 2:
        raise HTTPException(422, "请输入更具体的地点名称，例如“南京市江宁区”或“长沙岳麓区大学城”。")
    return query


def ai_range_location(text: str) -> str:
    base = os.environ["AI_BASE_URL"].rstrip("/")
    endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
    prompt = {
        "model": os.environ["AI_MODEL"], "temperature": 0,
        "messages": [
            {"role": "system", "content": "你是街景地点识别器，只处理地理地点。用户可以直接只输入地点名，无需说‘采集范围’、‘附近’或‘周边’；只要内容是可定位的地点，就返回 is_scope=true，并提取适合地图检索的简短地点名。天气、论文、闲聊、编码等非地点话题返回 is_scope=false。只输出 JSON：{\"is_scope\":true/false,\"location_query\":\"适合地图地名检索的简短地点名\"}。禁止补造用户未提及的城市或地点。"},
            {"role": "user", "content": text},
        ],
    }
    request = urllib.request.Request(endpoint, data=json.dumps(prompt, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + os.environ["AI_API_KEY"], "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read(2_000_001))
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        content = str(content).strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        result = json.loads(content)
    except urllib.error.HTTPError as exc:
        common = {
            400: "请求格式或模型参数不兼容",
            401: "API 密钥无效或未获授权",
            403: "当前 API 项目无权使用该模型",
            404: "API 地址或模型名不存在",
            429: "API 额度不足或请求频率受限",
        }
        reason = common.get(exc.code, "模型服务暂不可用")
        raise HTTPException(502, f"AI 服务返回 HTTP {exc.code}：{reason}。请检查 API 密钥、项目权限和计费设置。") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(502, "无法连接 AI 服务；请检查本机网络和 AI_BASE_URL。") from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "AI 中转服务响应格式无法识别；请确认使用 OpenAI 兼容的 Chat Completions 接口。") from exc
    if not isinstance(result, dict) or result.get("is_scope") is not True:
        raise HTTPException(422, "请输入一个具体地点，例如“南京市江宁区”或“长沙岳麓区大学城”。")
    location = result.get("location_query")
    if not isinstance(location, str):
        raise HTTPException(422, "我还没识别到具体地点，请补充城市、区县、大学城或街道名称。")
    location = " ".join(location.split()).strip(" ，,。.!！?？")
    if len(location) < 2 or len(location) > 120:
        raise HTTPException(422, "我还没识别到具体地点，请补充城市、区县、大学城或街道名称。")
    return location


def nominatim_search(query: str) -> list[dict[str, Any]]:
    global _last_geocoder
    cache_key = query.casefold()
    if cache_key in _geocoder_cache:
        return _geocoder_cache[cache_key]
    base = os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org/search")
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1, "accept-language": "zh-CN"})
    with _geocoder_lock:
        wait = 1.05 - (time.monotonic() - _last_geocoder)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(base + "?" + params, headers={"User-Agent": "StreetSceneResearchWorkbench/1.0 (single user research application)", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read(2_000_001))
                _last_geocoder = time.monotonic()
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            _last_geocoder = time.monotonic()
            raise HTTPException(502, "OpenStreetMap 地名定位暂不可用，请稍后重试或手动填写经纬度范围。") from exc
    results = []
    for item in payload if isinstance(payload, list) else []:
        try:
            south, north, west, east = map(float, item["boundingbox"])
            lat, lon = float(item["lat"]), float(item["lon"])
            if not (-74 <= south < north <= 74 and -180 <= west < east <= 180):
                continue
            results.append({"place_name": item.get("display_name", query), "latitude": lat, "longitude": lon,
                            "south": south, "north": north, "west": west, "east": east,
                            "osm_type": item.get("osm_type"), "osm_id": item.get("osm_id"),
                            "place_type": item.get("type", "")})
        except (KeyError, TypeError, ValueError):
            continue
    _geocoder_cache[cache_key] = results
    return results


@app.middleware("http")
async def local_only(request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    origin = request.headers.get("origin")
    if host not in {"127.0.0.1", "localhost"}:
        return JSONResponse({"detail": "Only loopback access is allowed"}, status_code=403)
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "local-streetview-acquisition", "credential_required": False,
            "ai_proxy_configured": all(os.environ.get(key) for key in AI_REQUIRED_SETTINGS),
            "ai_model": os.environ.get("AI_MODEL", ""),
            "provider": "Baidu public panorama metadata and pr3d image resources"}


def read_project_registry() -> dict[str, str]:
    try:
        value = json.loads(PROJECT_PATH_REGISTRY.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_project_registry(registry: dict[str, str]) -> None:
    temporary = PROJECT_PATH_REGISTRY.with_suffix(".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    temporary.replace(PROJECT_PATH_REGISTRY)


def project_folder_name(name: str) -> str:
    safe = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("._-")[:60]
    return safe or "街景工程"


def default_project_path(name: str) -> Path:
    base = PROJECTS_DIR / project_folder_name(name)
    candidate, suffix = base, 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}_{suffix}")
        suffix += 1
    return candidate


def project_path(project_id: str) -> Path:
    try:
        normalized = uuid.UUID(project_id).hex
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, "工程编号无效") from exc
    folder_value = read_project_registry().get(normalized)
    folder = Path(folder_value).expanduser().resolve() if folder_value else PROJECTS_DIR / normalized
    if not folder.is_dir():
        raise HTTPException(404, "找不到这个工程")
    return folder

def read_project(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "工程记录无法读取") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
        raise HTTPException(500, "工程记录格式无效")
    return data


def save_project(folder: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    temporary = folder / "project.json.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(folder / "project.json")


@app.get("/api/projects/default-path")
def get_default_project_path(name: str = "街景采集工程", base_path: str = "") -> dict[str, str]:
    if base_path:
        base = Path(base_path).expanduser()
        if not base.is_absolute():
            raise HTTPException(400, "选择的父目录必须是绝对路径")
        base = base.resolve()
        candidate = base / project_folder_name(name)
        suffix = 2
        while candidate.exists():
            candidate = base / f"{project_folder_name(name)}_{suffix}"
            suffix += 1
        return {"path": str(candidate)}
    return {"path": str(default_project_path(name))}


@app.post("/api/projects/choose-directory")
async def choose_project_directory() -> dict[str, str]:
    try:
        if sys.platform == "darwin":
            # Use the macOS native open panel. Tk may be absent from Homebrew Python
            # installations or fail to show a dialog when the service runs in background.
            initial = str(PROJECTS_DIR).replace("\\", "\\\\").replace('"', '\\"')
            script = f'POSIX path of (choose folder with prompt "选择或新建工程所在文件夹" default location POSIX file "{initial}")'
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=180)
            if result.returncode:
                if "User canceled" in result.stderr or "(-128)" in result.stderr:
                    raise HTTPException(400, "已取消选择文件夹")
                raise RuntimeError(result.stderr.strip() or "macOS 文件夹选择器启动失败")
            selected = result.stdout.strip()
        else:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            try:
                selected = filedialog.askdirectory(title="选择或新建工程所在文件夹", initialdir=str(PROJECTS_DIR), mustexist=True)
            finally:
                root.destroy()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(501, f"本机无法打开文件夹选择器：{exc}；也可以直接填写完整路径") from exc
    if not selected:
        raise HTTPException(400, "已取消选择文件夹")
    return {"path": str(Path(selected).expanduser().resolve())}


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    folders = {folder.resolve() for folder in PROJECTS_DIR.iterdir() if folder.is_dir() and (folder / "project.json").is_file()}
    for value in read_project_registry().values():
        try:
            candidate = Path(value).expanduser().resolve()
            if candidate.is_dir() and (candidate / "project.json").is_file():
                folders.add(candidate)
        except (OSError, RuntimeError):
            continue
    projects = []
    for folder in folders:
        try:
            data = read_project(folder)
            projects.append({"id": data["id"], "name": data["name"], "created_at": data.get("created_at"),
                             "updated_at": data.get("updated_at"), "image_count": len(data.get("items", [])),
                             "storage_path": str(folder), "description": data.get("description", "")})
        except (HTTPException, KeyError):
            continue
    return sorted(projects, key=lambda item: item.get("updated_at") or "", reverse=True)


@app.post("/api/projects")
async def create_project(request: Request) -> dict[str, Any]:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "工程信息格式无效")
    name = str(payload.get("name", "")).strip()[:80]
    if not name:
        raise HTTPException(400, "请填写工程名称")
    project_id = uuid.uuid4().hex
    raw_path = str(payload.get("storage_path", "")).strip()
    try:
        supplied_path = Path(raw_path).expanduser() if raw_path else None
        if supplied_path is not None and not supplied_path.is_absolute():
            raise HTTPException(400, "工程存储路径必须是绝对路径")
        folder = supplied_path.resolve() if supplied_path is not None else PROJECTS_DIR / project_id
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, "工程存储路径无效") from exc
    if raw_path and folder == PROJECTS_DIR:
        raise HTTPException(400, "请选择单独的工程文件夹，不能直接使用工程总目录")
    if folder.exists() and not folder.is_dir():
        raise HTTPException(400, "工程存储路径已被同名文件占用")
    if folder.exists() and any(folder.iterdir()):
        raise HTTPException(409, "所选工程文件夹不为空，请选择一个空文件夹或修改存储路径")
    try:
        (folder / "images").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"无法创建工程目录：{exc}") from exc
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data = {"schema_version": 1, "id": project_id, "name": name, "description": str(payload.get("description", "")).strip()[:1000],
            "storage_path": str(folder), "created_at": now, "updated_at": now, "acquisition": [], "items": []}
    save_project(folder, data)
    registry = read_project_registry()
    if folder != PROJECTS_DIR / project_id:
        registry[project_id] = str(folder)
        try:
            save_project_registry(registry)
        except OSError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(500, f"无法登记工程位置：{exc}") from exc
    return data


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    return read_project(project_path(project_id))


@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, request: Request) -> dict[str, Any]:
    folder = project_path(project_id)
    data = read_project(folder)
    payload = await request.json()
    if "name" in payload:
        name = str(payload["name"]).strip()[:80]
        if not name:
            raise HTTPException(400, "工程名称不能为空")
        data["name"] = name
    if isinstance(payload.get("acquisition"), list):
        data["acquisition"] = payload["acquisition"][:10000]
    if isinstance(payload.get("settings"), dict):
        data["settings"] = payload["settings"]
    if isinstance(payload.get("history"), list):
        history = []
        for record in payload["history"][:500]:
            if not isinstance(record, dict):
                continue
            saved = dict(record)
            if saved.get("batchId"):
                try:
                    saved["batchId"] = uuid.UUID(str(saved["batchId"])).hex
                except ValueError:
                    pass
            history.append(saved)
        data["history"] = history
    if isinstance(payload.get("items"), list):
        existing = {item.get("id"): item for item in data.get("items", [])}
        received = payload["items"]
        if len(received) > 20000:
            raise HTTPException(413, "一个工程最多记录 20000 张图片")
        items = []
        for item in received:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            try:
                item_id = uuid.UUID(item["id"]).hex
            except ValueError:
                continue
            saved = {key: value for key, value in item.items() if key not in {"file", "url", "imageUrl", "asset"}}
            saved["id"] = item_id
            old = existing.get(item_id, {})
            if old.get("asset"):
                saved["asset"] = old["asset"]
            items.append(saved)
        keep_assets = {item.get("asset") for item in items if item.get("asset")}
        for old in data.get("items", []):
            asset = old.get("asset")
            if asset and asset not in keep_assets:
                (folder / "images" / asset).unlink(missing_ok=True)
        data["items"] = items
    save_project(folder, data)
    return data


@app.put("/api/projects/{project_id}/items/{item_id}/image")
async def save_project_image(project_id: str, item_id: str, request: Request) -> dict[str, str]:
    folder = project_path(project_id)
    data = read_project(folder)
    try:
        normalized_id = uuid.UUID(item_id).hex
    except ValueError as exc:
        raise HTTPException(400, "图片记录编号无效") from exc
    record = next((item for item in data.get("items", []) if item.get("id") == normalized_id), None)
    if record is None:
        raise HTTPException(404, "找不到工程中的图片记录")
    extension = Path(record.get("file_name", "image.jpg")).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
        extension = ".jpg"
    target = folder / "images" / (normalized_id + extension)
    size = 0
    with target.open("wb") as output:
        async for chunk in request.stream():
            size += len(chunk)
            if size > 100 * 1024 * 1024:
                output.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, "单张图片不能超过 100 MB")
            output.write(chunk)
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "图片文件为空")
    record["asset"] = target.name
    record["file_size"] = size
    save_project(folder, data)
    return {"asset": target.name, "size": str(size)}


@app.get("/api/projects/{project_id}/items/{item_id}/image")
def get_project_image(project_id: str, item_id: str) -> FileResponse:
    folder = project_path(project_id)
    try:
        normalized_id = uuid.UUID(item_id).hex
    except ValueError as exc:
        raise HTTPException(400, "图片记录编号无效") from exc
    data = read_project(folder)
    record = next((item for item in data.get("items", []) if item.get("id") == normalized_id), None)
    if record is None or not record.get("asset"):
        raise HTTPException(404, "工程图片不存在")
    asset = Path(record["asset"]).name
    target = folder / "images" / asset
    if not target.is_file():
        raise HTTPException(404, "工程图片文件丢失")
    return FileResponse(target, filename=record.get("file_name") or asset, headers={"Cache-Control": "no-store"})


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict[str, bool]:
    folder = project_path(project_id)
    shutil.rmtree(folder)
    try:
        normalized = uuid.UUID(project_id).hex
        registry = read_project_registry()
        registry.pop(normalized, None)
        save_project_registry(registry)
    except ValueError:
        pass
    return {"ok": True}


@app.post("/api/projects/{project_id}/import/{source_id}")
def import_project_items(project_id: str, source_id: str) -> dict[str, Any]:
    destination = project_path(project_id)
    source = project_path(source_id)
    if destination == source:
        raise HTTPException(400, "请选择另一个工程作为来源")
    target_data, source_data = read_project(destination), read_project(source)
    if len(target_data.get("items", [])) + len(source_data.get("items", [])) > 20000:
        raise HTTPException(413, "一个工程最多记录 20000 张图片")
    imported = []
    for original in source_data.get("items", []):
        asset = original.get("asset")
        if not asset or not (source / "images" / Path(asset).name).is_file():
            continue
        new_id = uuid.uuid4().hex
        suffix = Path(asset).suffix.lower()
        new_asset = new_id + (suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"} else ".jpg")
        shutil.copy2(source / "images" / Path(asset).name, destination / "images" / new_asset)
        copied = {key: value for key, value in original.items() if key not in {"id", "asset", "url", "file", "imageUrl"}}
        copied.update({"id": new_id, "asset": new_asset, "imported_from": source_data.get("name", "")})
        target_data.setdefault("items", []).append(copied)
        imported.append(copied)
    save_project(destination, target_data)
    return {"items": imported, "source_name": source_data.get("name", "")}


@app.get("/api/projects/{project_id}/export")
def export_project(project_id: str) -> FileResponse:
    folder = project_path(project_id)
    data = read_project(folder)
    temporary_folder = Path(tempfile.mkdtemp(prefix="streetvision-project-"))
    archive_path = temporary_folder / "工程包.zip"
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.write(folder / "project.json", "project.json")
            for item in data.get("items", []):
                asset = item.get("asset")
                if asset:
                    image_path = folder / "images" / Path(asset).name
                    if image_path.is_file():
                        archive.write(image_path, "images/" + image_path.name)
    except OSError as exc:
        shutil.rmtree(temporary_folder, ignore_errors=True)
        raise HTTPException(500, f"工程包生成失败：{exc}") from exc
    safe_name = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", data.get("name", "街景工程"))[:60] or "街景工程"
    return FileResponse(archive_path, filename=safe_name + ".zip", media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, temporary_folder, ignore_errors=True))


@app.post("/api/projects/import")
async def import_project_archive(request: Request) -> dict[str, Any]:
    temporary_folder = Path(tempfile.mkdtemp(prefix="streetvision-import-"))
    archive_path = temporary_folder / "upload.zip"
    received = 0
    try:
        with archive_path.open("wb") as output:
            async for chunk in request.stream():
                received += len(chunk)
                if received > 20 * 1024**3:
                    raise HTTPException(413, "工程包不能超过 20 GB")
                output.write(chunk)
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            total_size = 0
            for info in infos:
                path = Path(info.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise HTTPException(400, "工程包包含不安全文件路径")
                total_size += info.file_size
                if total_size > 30 * 1024**3:
                    raise HTTPException(413, "工程解压后不能超过 30 GB")
            try:
                manifest = json.loads(archive.read("project.json").decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(400, "工程包中缺少有效的 project.json") from exc
            if not isinstance(manifest, dict) or not isinstance(manifest.get("items", []), list):
                raise HTTPException(400, "工程记录格式无效")
            new_id = uuid.uuid4().hex
            new_folder = PROJECTS_DIR / new_id
            (new_folder / "images").mkdir(parents=True)
            imported_items = []
            item_id_map: dict[str, str] = {}
            try:
                for item in manifest.get("items", [])[:20000]:
                    if not isinstance(item, dict):
                        continue
                    old_asset = Path(str(item.get("asset", ""))).name
                    if not old_asset:
                        continue
                    archive_name = "images/" + old_asset
                    try:
                        info = archive.getinfo(archive_name)
                    except KeyError:
                        continue
                    suffix = Path(old_asset).suffix.lower()
                    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
                        continue
                    item_id = uuid.uuid4().hex
                    new_asset = item_id + suffix
                    with archive.open(info) as source, (new_folder / "images" / new_asset).open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    saved = {key: value for key, value in item.items() if key not in {"id", "asset", "url", "file", "imageUrl"}}
                    saved.update({"id": item_id, "asset": new_asset})
                    imported_items.append(saved)
                    if item.get("id"):
                        item_id_map[str(item["id"]).replace("-", "")] = item_id
                now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                name = str(manifest.get("name", "导入工程")).strip()[:80] or "导入工程"
                history = []
                records = manifest.get("history", [])
                for record in records[:500] if isinstance(records, list) else []:
                    if not isinstance(record, dict):
                        continue
                    saved_record = dict(record)
                    if saved_record.get("batchId"):
                        saved_record["batchId"] = item_id_map.get(str(saved_record["batchId"]).replace("-", ""), "")
                        if not saved_record["batchId"]:
                            continue
                    history.append(saved_record)
                imported = {"schema_version": 1, "id": new_id, "name": name, "created_at": now, "updated_at": now,
                            "acquisition": manifest.get("acquisition", []) if isinstance(manifest.get("acquisition"), list) else [],
                            "settings": manifest.get("settings", {}) if isinstance(manifest.get("settings"), dict) else {},
                            "description": str(manifest.get("description", ""))[:1000], "storage_path": str(new_folder),
                            "history": history, "items": imported_items}
                save_project(new_folder, imported)
            except Exception:
                shutil.rmtree(new_folder, ignore_errors=True)
                raise
        return {"id": new_id, "name": imported["name"], "image_count": len(imported_items)}
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "工程包不是有效的 ZIP 文件") from exc
    finally:
        shutil.rmtree(temporary_folder, ignore_errors=True)


def get_admin_regions(path: str) -> dict[str, Any]:
    with _admin_regions_cache_lock:
        cached = _admin_regions_cache.get(path)
    if cached is not None:
        return cached
    request = urllib.request.Request(
        ADMIN_REGIONS_API + path,
        headers={"User-Agent": "StreetSceneResearchWorkbench/1.0 (administrative selector)", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read(2_000_001))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "行政区划列表暂不可用，请检查网络后重试。") from exc
    if isinstance(payload, list):
        payload = {"items": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise HTTPException(502, "行政区划服务返回的数据格式无法识别。")
    with _admin_regions_cache_lock:
        _admin_regions_cache[path] = payload
    return payload


@app.get("/api/regions/provinces")
def get_admin_provinces() -> dict[str, Any]:
    return get_admin_regions("/provinces")


@app.get("/api/regions/children/{parent_code}")
def get_admin_region_children(parent_code: str) -> dict[str, Any]:
    if not re.fullmatch(r"[\w-]{2,20}", parent_code, re.UNICODE):
        raise HTTPException(400, "行政区划代码无效。")
    return get_admin_regions("/children/" + urllib.parse.quote(parent_code, safe=""))


@app.post("/api/place/search")
def search_place(body: PlaceSearchRequest) -> dict[str, Any]:
    text = body.text
    ai_configured = all(os.environ.get(key) for key in AI_REQUIRED_SETTINGS)
    # Validate input locally before optionally sending it to the configured model.
    extract_location_query(text)
    query = ai_range_location(text) if ai_configured else extract_location_query(text)
    places = nominatim_search(query)
    if not places:
        raise HTTPException(404, f"没有定位到“{query}”。请补充城市、区县或地标，也可改用地图框选。")
    return {"query": query, "places": places, "provider": "OpenStreetMap Nominatim", "intent_mode": "ai_proxy" if ai_configured else "local_rules"}


@app.post("/api/roads/sample")
def sample_roads(body: BoundsRequest) -> dict[str, Any]:
    validate_bounds(body)
    if body.road_ids and all(road_id.startswith("local:") for road_id in body.road_ids):
        with _local_road_cache_lock:
            missing = [road_id for road_id in body.road_ids if road_id not in _local_road_cache]
            ways = [_local_road_cache[road_id] for road_id in body.road_ids if road_id in _local_road_cache]
        if missing:
            raise HTTPException(409, "本地道路选择已过期；请重新加载当前地图道路后再选")
        sample_ways = [
            {"id": way["id"], "tags": way["tags"], "geometry": part}
            for way in ways
            for part in (way["geometry_parts"] if "geometry_parts" in way else [way["geometry"]])
        ]
        points = road_samples(sample_ways, body.spacing_m)
        return {"road_count": len(ways), "sample_count": len(points), "points": points, "source": "local-shapefile"}
    ways = overpass_roads(body)
    if body.road_ids:
        selected = set(body.road_ids)
        ways = [way for way in ways if str(way.get("id", "")) in selected]
        if not ways:
            raise HTTPException(404, "没有找到选中的道路，请重新加载道路后选择")
    points = road_samples(ways, body.spacing_m)
    return {"road_count": len(ways), "sample_count": len(points), "points": points}


@app.post("/api/roads/preview")
def preview_roads(body: BoundsRequest) -> dict[str, Any]:
    validate_bounds(body)
    if LOCAL_ROADS_PATH.is_file():
        try:
            features = pyogrio.read_dataframe(
                LOCAL_ROADS_PATH,
                bbox=(body.west, body.south, body.east, body.north),
                columns=["name", "fclass", "fclass_cn", "type"],
                fid_as_index=True,
            )
        except Exception as exc:
            raise HTTPException(500, f"读取本地道路数据失败：{exc}") from exc

        roads: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any]] = {}
        for feature_id, feature in features.iterrows():
            geometry = feature.geometry
            if geometry is None or geometry.is_empty:
                continue
            line_geometries = list(geometry.geoms) if geometry.geom_type == "MultiLineString" else [geometry]
            coordinate_parts = [
                [{"lon": float(lon), "lat": float(lat)} for lon, lat, *_ in line.coords]
                for line in line_geometries
            ]
            coordinate_parts = [part for part in coordinate_parts if len(part) >= 2]
            if not coordinate_parts:
                continue
            road_id = f"local:{feature_id}"
            road_name = str(feature.get("name") or "未命名道路")
            road_type = str(feature.get("fclass_cn") or feature.get("type") or feature.get("fclass") or "道路")
            tags = {"name": road_name, "highway": road_type}
            cache[road_id] = {"id": road_id, "geometry_parts": coordinate_parts, "tags": tags}
            roads.append({
                "road_id": road_id,
                "road_name": road_name,
                "road_type": road_type,
                "geometry": [
                    [[point["lat"], point["lon"]] for point in part]
                    for part in coordinate_parts
                ],
            })

        with _local_road_cache_lock:
            _local_road_cache.clear()
            _local_road_cache.update(cache)
        return {"road_count": len(roads), "roads": roads, "source": "local-shapefile"}

    ways = overpass_roads(body)
    roads = []
    for way in ways[:2500]:
        tags = way.get("tags", {})
        roads.append({"road_id": str(way.get("id", "")), "road_name": tags.get("name", "未命名道路"),
                      "road_type": tags.get("highway", "unknown"),
                      "geometry": [[float(point["lat"]), float(point["lon"])] for point in way["geometry"]]})
    return {"road_count": len(ways), "roads": roads}


@app.post("/api/roads/route")
def sample_route(body: RouteRequest) -> dict[str, Any]:
    return route_between_points(body)


@app.post("/api/collect")
def collect(body: PointRequest) -> dict[str, Any]:
    lat, lon = float(body.latitude), float(body.longitude)
    if not (math.isfinite(lat) and math.isfinite(lon) and -74 <= lat <= 74 and -180 <= lon <= 180):
        raise HTTPException(400, "坐标无效")
    key = (round(lat, 7), round(lon, 7), round(float(body.heading) % 360, 1))
    with _preview_cache_lock:
        cached = _preview_cache.pop(key, None)
    if cached:
        match, image = cached
    else:
        match, image = fetch_street_image(lat, lon, body.heading)
    return collected_image_response(body, lat, lon, match, image)


def fetch_street_image(lat: float, lon: float, heading: float) -> tuple[dict[str, Any], bytes]:
    bd_lon, bd_lat = local_bd09(lon, lat)
    try:
        match = baidu.lookup({"sample_lon_bd09": bd_lon, "sample_lat_bd09": bd_lat})
        if match["distance_to_sample_m"] > 100:
            raise HTTPException(404, f"100 米内没有匹配的街景（最近 {match['distance_to_sample_m']:.1f} 米）")
        image = baidu.download(match["pano_id"], heading)
    except baidu.Failure as exc:
        status = 403 if exc.status == "BLOCKED" else 502
        raise HTTPException(status, f"采集服务返回 {exc.status}：{exc}") from exc
    return match, image


def collected_image_response(body: PointRequest, lat: float, lon: float, match: dict[str, Any], image: bytes) -> dict[str, Any]:
    pano_lon, pano_lat = match["pano_lon"], match["pano_lat"]
    wgs_lon, wgs_lat = bd09_to_wgs84(pano_lon, pano_lat)
    return {
        "ok": True, "image_base64": base64.b64encode(image).decode("ascii"),
        "image_type": "image/jpeg", "pano_id": match["pano_id"],
        "capture_date": match["capture_date"], "distance_to_sample_m": match["distance_to_sample_m"],
        "requested_latitude": lat, "requested_longitude": lon,
        "pano_latitude": wgs_lat, "pano_longitude": wgs_lon,
        "heading": float(body.heading), "road_id": body.road_id or "",
        "road_name": "", "point_id": body.point_id or "",
        "source": "Baidu public panorama service (HNU_StreetView_Front_v6 method)",
    }


@app.post("/api/preview")
def preview_street_image(body: PointRequest) -> dict[str, Any]:
    lat, lon = float(body.latitude), float(body.longitude)
    if not (math.isfinite(lat) and math.isfinite(lon) and -74 <= lat <= 74 and -180 <= lon <= 180):
        raise HTTPException(400, "坐标无效")
    key = (round(lat, 7), round(lon, 7), round(float(body.heading) % 360, 1))
    with _preview_cache_lock:
        cached = _preview_cache.get(key)
        if cached:
            _preview_cache.move_to_end(key)
    if cached:
        match, image = cached
    else:
        match, image = fetch_street_image(lat, lon, body.heading)
        with _preview_cache_lock:
            _preview_cache[key] = (match, image)
            _preview_cache.move_to_end(key)
            while len(_preview_cache) > PREVIEW_CACHE_LIMIT:
                _preview_cache.popitem(last=False)
    with Image.open(BytesIO(image)) as source:
        thumbnail = source.convert("RGB")
        thumbnail.thumbnail((240, 135), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        thumbnail.save(buffer, format="JPEG", quality=72, optimize=True)
    return {
        "ok": True,
        "image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "image_type": "image/jpeg",
        "pano_id": match["pano_id"],
        "capture_date": match["capture_date"],
        "distance_to_sample_m": match["distance_to_sample_m"],
        "heading": float(body.heading),
    }
