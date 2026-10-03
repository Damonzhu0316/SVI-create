"""Low-level V5-compatible client for Baidu's public pano and pr3d resources."""
import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from PIL import Image, ImageStat

from coordinate_transform import bd09ll_to_mc, bd09mc_to_ll, distance_m

IMAGE_API = "https://mapsv0.bdimg.com/"
LOOKUP_API = IMAGE_API
CAMERA = dict(pitch=0, fovy=35, width=1024, height=512, quality=90)
REQUEST_INTERVAL_S = 1.2
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 15_000_000


class Failure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_last_request = 0.0


def get(endpoint, params):
    global _last_request
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        wait = REQUEST_INTERVAL_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(
            endpoint + "?" + urllib.parse.urlencode(params),
            headers={"User-Agent": "Mozilla/5.0 (compatible; HNU-SVI-Research/6.0)",
                     "Referer": "https://map.baidu.com/", "Accept": "*/*"},
        )
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                _last_request = time.monotonic()
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise Failure("RESPONSE_TOO_LARGE", "Unexpected response size")
                return response.headers.get("Content-Type", ""), body
        except urllib.error.HTTPError as error:
            _last_request = time.monotonic()
            if error.code in {301, 302, 303, 307, 308, 401, 403, 429}:
                raise Failure("BLOCKED", f"Service declined request (HTTP {error.code})") from None
            last_error = error
            if error.code < 500 or attempt == MAX_ATTEMPTS - 1:
                raise Failure("HTTP_ERROR", f"HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            _last_request = time.monotonic()
            last_error = True
            if attempt == MAX_ATTEMPTS - 1:
                raise Failure("NETWORK_ERROR", "Network request failed after retries") from None
        time.sleep(min(8.0, 2.0 * (2 ** attempt)))
    raise Failure("NETWORK_ERROR", "Network request failed") from last_error


def metadata(params):
    _content_type, body = get(LOOKUP_API, params)
    decoded = body.decode("utf-8", errors="ignore")
    if any(token in decoded.lower() for token in ("captcha", "验证码", "安全验证", "verify.baidu")):
        raise Failure("BLOCKED", "Metadata service returned an access challenge")
    try:
        payload = json.loads(body)
    except ValueError:
        raise Failure("LOOKUP_SCHEMA_ERROR", "Expected metadata JSON") from None
    if not isinstance(payload, dict):
        raise Failure("LOOKUP_SCHEMA_ERROR", "Expected JSON object")
    result = payload.get("result", {})
    if isinstance(result, dict) and result.get("error") not in (None, 0, "0"):
        raise Failure("LOOKUP_SCHEMA_ERROR", "Baidu metadata query returned an error")
    content = payload.get("content")
    if isinstance(content, dict):
        return [content]
    if isinstance(content, list):
        return [record for record in content if isinstance(record, dict)]
    if isinstance(result, dict) and result.get("error") in (0, "0"):
        return []
    raise Failure("LOOKUP_SCHEMA_ERROR", "Metadata service schema changed")


def position(record):
    if "X" in record and "Y" in record:
        x, y = record["X"], record["Y"]
    elif "x" in record and "y" in record:
        x, y = record["x"], record["y"]
    else:
        raise Failure("LOOKUP_SCHEMA_ERROR", "Missing pano position")
    return bd09mc_to_ll(float(x) / 100.0, float(y) / 100.0)


def capture_date(record):
    raw = str(record.get("Date", record.get("CaptureDate", "NA")))
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m"):
        try:
            date = datetime.strptime(raw, fmt)
            return date.strftime("%Y-%m" if fmt in ("%Y-%m", "%Y/%m") else "%Y-%m-%d")
        except ValueError:
            continue
    return "NA"


def lookup(point):
    lon, lat = float(point["sample_lon_bd09"]), float(point["sample_lat_bd09"])
    x, y = bd09ll_to_mc(lon, lat)
    candidates = []
    for record in metadata({"qt": "qsdata", "x": f"{x:.6f}", "y": f"{y:.6f}",
                            "time": datetime.now().strftime("%Y%m"), "mode": "day"}):
        pano_id = str(record.get("id", record.get("ID", "")))
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", pano_id):
            continue
        pano_lon, pano_lat = position(record)
        candidates.append((distance_m(lon, lat, pano_lon, pano_lat), pano_id))
    if not candidates:
        raise Failure("NO_PANO", "No pano candidate returned")
    distance, pano_id = min(candidates)
    exact = next((record for record in metadata({"qt": "sdata", "sid": pano_id, "pc": "1"})
                  if str(record.get("ID", record.get("id", ""))) == pano_id), None)
    if exact is None:
        raise Failure("LOOKUP_SCHEMA_ERROR", "Exact requested pano ID missing")
    pano_lon, pano_lat = position(exact)
    return {"pano_id": pano_id, "pano_lon": pano_lon, "pano_lat": pano_lat,
            "capture_date": capture_date(exact), "distance_to_sample_m": round(distance, 3)}


def parameters(pano_id, heading):
    value = float(heading)
    if not math.isfinite(value) or not 0 <= value < 360:
        raise ValueError("Invalid heading")
    return {"qt": "pr3d", "panoid": pano_id, "heading": f"{value:.6f}", **CAMERA}


def image_bytes(body, content_type="image/jpeg"):
    if not str(content_type).lower().startswith("image/"):
        raise Failure("INVALID_IMAGE", "Response Content-Type is not an image")
    if len(body) < 1024:
        raise Failure("INVALID_IMAGE", "Image response is smaller than 1024 bytes")
    try:
        with Image.open(io.BytesIO(body)) as image:
            image.verify()
        with Image.open(io.BytesIO(body)) as image:
            image.load()
            if image.size != (CAMERA["width"], CAMERA["height"]):
                raise Failure("INVALID_IMAGE", "Wrong image dimensions")
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise Failure("INVALID_IMAGE", "Unexpected image format")
            rgb = image.convert("RGB")
            if max(ImageStat.Stat(rgb).stddev) < 0.8:
                raise Failure("INVALID_IMAGE", "Uniform image returned")
            if image.format == "JPEG":
                return body
            output = io.BytesIO()
            rgb.save(output, "JPEG", quality=95)
            return output.getvalue()
    except Failure:
        raise
    except (OSError, ValueError):
        raise Failure("INVALID_IMAGE", "Cannot decode perspective image") from None


def download(pano_id, heading):
    content_type, body = get(IMAGE_API, parameters(pano_id, heading))
    if not content_type.lower().startswith("image/"):
        try:
            status = json.loads(body).get("status", "unknown")
        except (ValueError, AttributeError):
            status = "non-image"
        status = str(status) if str(status).isdigit() else "non-image"
        raise Failure("IMAGE_DOWNLOAD_FAILED", "pr3d returned non-image response " + status)
    return image_bytes(body, content_type)
