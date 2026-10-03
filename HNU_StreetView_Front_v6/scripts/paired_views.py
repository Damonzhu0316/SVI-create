"""Collect and audit distinct Baidu pano positions for each station's front/back views."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
import random
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pyproj import Geod, Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform

import front_view as api
from common import DATA, ROOT, atomic, csvwrite, read, rows, sha
from coordinate_transform import bd09_to_wgs84, distance_m, wgs84_to_gcj02

NETWORK = DATA / "network"
POINTS = DATA / "sampling_points.csv"
ROADS = NETWORK / "roads.geojson"
META = DATA / "metadata" / "paired_view_metadata.csv"
OUT = DATA / "paired_views"
REPORT = ROOT / "docs" / "paired_view_validation.json"
EBIKE_TEMPLATE = DATA / "ebike_observations_template.csv"
PAIR_EXPORT = DATA / "comparison_pairs.csv"
MAX_STATION_DISTANCE_M = 30.0
MAX_TARGET_WAY_DISTANCE_M = 25.0
WGS_TO_METRES = Transformer.from_crs(4326, 32649, always_xy=True).transform
GEOD = Geod(ellps="WGS84")
FIELDS = [
    "station_id", "point_id", "road_id", "osm_way_id", "direction", "status",
    "request_heading_deg", "local_tangent_deg", "heading_error_deg", "heading_check",
    "pano_id", "pano_lon_bd09", "pano_lat_bd09", "pano_lon_wgs84", "pano_lat_wgs84",
    "pano_crs", "nearest_road_id", "nearest_osm_way_id", "target_way_distance_m",
    "nearest_road_distance_m", "distance_to_station_m", "capture_date", "image_path",
    "image_sha256", "sample_signature", "road_signature", "failure_reason",
]
EBIKE_FIELDS = [
    "station_id", "direction", "image_path", "capture_date", "longitude", "latitude",
    "coordinate_crs", "heading_deg", "heading_source", "image_sha256",
]


class MatchError(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


def angle_delta(a: float, b: float) -> float:
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def wgs84_to_bd09(lon: float, lat: float):
    gx, gy = wgs84_to_gcj02(float(lon), float(lat))
    z = math.hypot(gx, gy) + 0.00002 * math.sin(gy * math.pi * 3000.0 / 180.0)
    theta = math.atan2(gy, gx) + 0.000003 * math.cos(gx * math.pi * 3000.0 / 180.0)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def directional_label(request_heading: float, tangent: float, tolerance_deg: float = 15.0) -> str:
    if angle_delta(request_heading, tangent) <= tolerance_deg:
        return "front"
    if angle_delta(request_heading, (tangent + 180.0) % 360.0) <= tolerance_deg:
        return "back"
    return "UNVERIFIED"


def load_roads(path: Path = ROADS):
    fc = json.loads(path.read_text(encoding="utf-8-sig"))
    by_road = {}
    for feature in fc["features"]:
        props = feature["properties"]
        geom = transform(WGS_TO_METRES, shape(feature["geometry"]))
        by_road[str(props["road_id"])] = {
            "geometry": geom,
            "road_id": str(props["road_id"]),
            "osm_way_id": str(props.get("osm_way_id", "")),
            "properties": props,
        }
    return by_road


def local_tangent(point: dict, roads: dict, span_m: float = 3.0) -> float:
    road = roads.get(str(point["road_id"]))
    if not road:
        raise MatchError("ROAD_GEOMETRY_MISSING", "station road_id not present in current road network")
    geom = road["geometry"]
    sample = transform(WGS_TO_METRES, Point(float(point["sample_lon"]), float(point["sample_lat"])))
    station_m = geom.project(sample)
    if geom.distance(sample) > 2.0:
        raise MatchError("STATION_OFF_ROAD", "sample coordinate is more than 2 m from its target road geometry")
    start, end = max(0.0, station_m - span_m), min(geom.length, station_m + span_m)
    if end - start < 0.25:
        raise MatchError("TANGENT_UNAVAILABLE", "road geometry is too short to estimate local tangent")
    a, b = geom.interpolate(start), geom.interpolate(end)
    lon1, lat1 = Transformer.from_crs(32649, 4326, always_xy=True).transform(a.x, a.y)
    lon2, lat2 = Transformer.from_crs(32649, 4326, always_xy=True).transform(b.x, b.y)
    return GEOD.inv(lon1, lat1, lon2, lat2)[0] % 360.0


def road_signature(path: Path = ROADS) -> str:
    return sha(path)


def sample_signature(path: Path = POINTS) -> str:
    return sha(path)


def point_signature(point: dict) -> str:
    payload = "|".join(str(point.get(k, "")) for k in ("station_id", "point_id", "road_id", "osm_way_id", "sample_lon", "sample_lat"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pano_position(record: dict):
    lon, lat = api.position(record)
    return float(lon), float(lat)


def road_side_audit(lon_bd09: float, lat_bd09: float, target_way_id: str, roads: dict):
    lon, lat = bd09_to_wgs84(lon_bd09, lat_bd09)
    projected = transform(WGS_TO_METRES, Point(lon, lat))
    ranked = sorted(
        (entry["geometry"].distance(projected), entry["road_id"], entry["osm_way_id"])
        for entry in roads.values()
    )
    target_matches = [row for row in ranked if row[2] == str(target_way_id)]
    if not target_matches:
        return {"ok": False, "status": "ROAD_SIDE_MISMATCH", "lon": lon, "lat": lat,
                "target_distance_m": None, "nearest_road_id": ranked[0][1] if ranked else "",
                "nearest_way_id": ranked[0][2] if ranked else "", "nearest_distance_m": ranked[0][0] if ranked else None}
    target_distance = target_matches[0][0]
    nearest_distance, nearest_road_id, nearest_way_id = ranked[0]
    # Allow parallel scope fragments for the same OSM way, but never accept a pano
    # whose nearest mapped road belongs to the opposite carriageway/OSM way.
    ok = target_distance <= MAX_TARGET_WAY_DISTANCE_M and str(nearest_way_id) == str(target_way_id)
    return {"ok": ok, "status": "ROAD_SIDE_MATCH" if ok else "ROAD_SIDE_MISMATCH",
            "lon": lon, "lat": lat, "target_distance_m": target_distance,
            "nearest_road_id": nearest_road_id, "nearest_way_id": nearest_way_id,
            "nearest_distance_m": nearest_distance}


def _candidate_ids(point: dict, tangent: float, direction: str):
    sample_lon, sample_lat = float(point["sample_lon"]), float(point["sample_lat"])
    bearing = float(tangent) if direction == "front" else (float(tangent) + 180.0) % 360.0
    query_lon, query_lat, _ = GEOD.fwd(sample_lon, sample_lat, bearing, 12.0)
    query_bd_lon, query_bd_lat = wgs84_to_bd09(query_lon, query_lat)
    x, y = api.bd09ll_to_mc(query_bd_lon, query_bd_lat)
    records = api.metadata(dict(qt="qsdata", x=f"{x:.6f}", y=f"{y:.6f}",
                                time=datetime.now().strftime("%Y%m"), mode="day"))
    ids = []
    for record in records:
        pano_id = str(record.get("id", record.get("ID", "")))
        if re.fullmatch(r"[A-Za-z0-9_-]{6,80}", pano_id) and pano_id not in ids:
            ids.append(pano_id)
    return ids, query_bd_lon, query_bd_lat


def exact_pano(pano_id: str):
    result = next((r for r in api.metadata(dict(qt="sdata", sid=pano_id, pc=1))
                   if str(r.get("ID", r.get("id", ""))) == pano_id), None)
    if result is None:
        raise MatchError("PANO_METADATA_MISSING", "exact pano metadata did not return the requested pano ID")
    lon, lat = _pano_position(result)
    return result, lon, lat


def find_two_panos(point: dict, roads: dict, tangent: float):
    candidates_by_direction = {}
    diagnostics = {}
    for direction in ("front", "back"):
        pano_ids, query_lon, query_lat = _candidate_ids(point, tangent, direction)
        candidates = []
        counts = {"qsdata_ids": len(pano_ids), "metadata_ok": 0, "wrong_way": 0, "wrong_way_ids": [],
                  "target_way_too_far": 0, "station_too_far": 0}
        for pano_id in pano_ids:
            try:
                detail, lon, lat = exact_pano(pano_id)
            except MatchError:
                continue
            counts["metadata_ok"] += 1
            side = road_side_audit(lon, lat, str(point["osm_way_id"]), roads)
            if not side["ok"]:
                if side["nearest_way_id"] != str(point["osm_way_id"]):
                    counts["wrong_way"] += 1
                    counts["wrong_way_ids"].append({"pano_id": pano_id,
                                                     "nearest_osm_way_id": side["nearest_way_id"],
                                                     "nearest_road_id": side["nearest_road_id"]})
                else:
                    counts["target_way_too_far"] += 1
                continue
            station_distance = distance_m(float(point["sample_lon_bd09"]), float(point["sample_lat_bd09"]), lon, lat)
            query_distance = distance_m(query_lon, query_lat, lon, lat)
            if station_distance <= MAX_STATION_DISTANCE_M:
                candidates.append((station_distance, pano_id, detail, lon, lat, side, query_distance))
            else:
                counts["station_too_far"] += 1
        candidates.sort(key=lambda item: (item[6], item[0], item[1]))
        candidates_by_direction[direction] = candidates
        diagnostics[direction] = {**counts, "valid_candidates": len(candidates),
                                  "valid_pano_ids": [candidate[1] for candidate in candidates]}
    # Find the lowest-distance independent match for each along-road query.
    for front in candidates_by_direction["front"]:
        for back in candidates_by_direction["back"]:
            if front[1] != back[1] and distance_m(front[3], front[4], back[3], back[4]) >= 1.0:
                return {"front": front, "back": back}
    shared = sorted({candidate[1] for candidate in candidates_by_direction["front"]}
                    & {candidate[1] for candidate in candidates_by_direction["back"]})
    reason = ("No distinct forward/reverse pano pair on target way; "
              f"front={diagnostics['front']}; back={diagnostics['back']}; shared_ids={shared}")
    raise MatchError("TWO_DISTINCT_PANOS_REQUIRED", reason)


def _date(detail: dict) -> str:
    return api.capture_date(detail)


def _metadata_row(point: dict, direction: str, tangent: float, signature: str, roads_hash: str):
    heading = tangent if direction == "front" else (tangent + 180.0) % 360.0
    record = {field: "NA" for field in FIELDS}
    record.update({"station_id": point["station_id"], "point_id": point["point_id"],
                   "road_id": point["road_id"], "osm_way_id": point["osm_way_id"],
                   "direction": direction, "status": "NOT_PROCESSED",
                   "request_heading_deg": round(heading, 6), "local_tangent_deg": round(tangent, 6),
                   "heading_error_deg": round(angle_delta(heading, tangent if direction == "front" else (tangent + 180) % 360), 6),
                   "heading_check": "PASS", "pano_crs": "BD09LL", "sample_signature": signature,
                   "road_signature": roads_hash, "failure_reason": "NA"})
    return record


def _record_ok(row: dict, point: dict, tangent: float, roads: dict, roads_hash: str,
               signature: str, image_path: Path, recheck_metadata: bool, allow_unverified: bool = False):
    if row.get("status") != "VERIFIED" and not allow_unverified:
        return False, "OLD_RECORD_NOT_VERIFIED"
    if row.get("station_id") != point.get("station_id") or row.get("road_id") != point.get("road_id"):
        return False, "STATION_OR_ROAD_CHANGED"
    if row.get("sample_signature") != signature or row.get("road_signature") != roads_hash:
        return False, "SOURCE_GEOMETRY_CHANGED"
    request_heading = float(row["request_heading_deg"])
    expected = tangent if row["direction"] == "front" else (tangent + 180) % 360
    if (angle_delta(request_heading, expected) > 0.1
            or directional_label(request_heading, tangent) != row["direction"]
            or row.get("heading_check") != "PASS"):
        return False, "HEADING_LABEL_MISMATCH"
    try:
        lon, lat = float(row["pano_lon_bd09"]), float(row["pano_lat_bd09"])
        side = road_side_audit(lon, lat, str(point["osm_way_id"]), roads)
        if not side["ok"] or side["nearest_way_id"] != str(row.get("nearest_osm_way_id")):
            return False, "ROAD_SIDE_MISMATCH"
        if abs(float(side["target_distance_m"]) - float(row["target_way_distance_m"])) > 0.5:
            return False, "TARGET_WAY_DISTANCE_CHANGED"
        station_distance = distance_m(float(point["sample_lon_bd09"]), float(point["sample_lat_bd09"]), lon, lat)
        if station_distance > MAX_STATION_DISTANCE_M or abs(station_distance - float(row["distance_to_station_m"])) > 0.5:
            return False, "STATION_DISTANCE_MISMATCH"
        if recheck_metadata:
            detail, fresh_lon, fresh_lat = exact_pano(str(row["pano_id"]))
            if distance_m(lon, lat, fresh_lon, fresh_lat) > 1.0:
                return False, "PANO_POSITION_CHANGED"
            if _date(detail) != row.get("capture_date"):
                return False, "PANO_DATE_CHANGED"
    except (ValueError, KeyError, MatchError):
        return False, "PANO_RECHECK_FAILED"
    if not image_path.is_file() or sha(image_path) != row.get("image_sha256"):
        return False, "IMAGE_HASH_MISMATCH"
    try:
        api.image_bytes(image_path.read_bytes())
    except MatchError:
        return False, "IMAGE_INVALID"
    except api.Failure:
        return False, "IMAGE_INVALID"
    return True, "PASS"


def station_is_complete(rows_for_station: list[dict]) -> bool:
    by_direction = {row.get("direction"): row for row in rows_for_station}
    if len(rows_for_station) != 2:
        return False
    try:
        distinct_positions = distance_m(float(by_direction["front"]["pano_lon_bd09"]), float(by_direction["front"]["pano_lat_bd09"]),
                                        float(by_direction["back"]["pano_lon_bd09"]), float(by_direction["back"]["pano_lat_bd09"])) >= 1.0
    except (KeyError, ValueError):
        distinct_positions = False
    return (set(by_direction) == {"front", "back"}
            and all(by_direction[d].get("status") == "VERIFIED" for d in ("front", "back"))
            and by_direction["front"].get("pano_id") != by_direction["back"].get("pano_id")
            and distinct_positions)


def select_stations(points: list[dict], limit: int | None, sequential: bool = False, random_seed: int | None = None):
    if random_seed is not None:
        return random.Random(random_seed).sample(points, min(limit or len(points), len(points)))
    if sequential:
        return points if limit is None else points[:limit]
    if limit is None or limit >= len(points):
        return points
    chosen = []
    # Keep the reported parallel-way mismatch in every default pilot so the
    # revised road-side gate is exercised before any larger collection.
    audit_pool = [point for point in points if str(point.get("osm_way_id")) == "54818722"]
    if audit_pool:
        chosen.append(next((p for p in audit_pool if p.get("point_id") == "HNU_000003"), audit_pool[len(audit_pool) // 2]))
        if len(chosen) == limit:
            return chosen
    for road_type in ("primary", "secondary", "service", "residential", "footway", "living_street", "tertiary", "unclassified"):
        pool = [point for point in points if point.get("road_type") == road_type and point not in chosen]
        if pool:
            chosen.append(pool[len(pool) // 2])
        if len(chosen) == limit:
            return chosen
    if len(chosen) < limit:
        remaining = [point for point in points if point not in chosen]
        slots = limit - len(chosen)
        for i in range(slots):
            index = round((i + 1) * (len(remaining) - 1) / (slots + 1))
            chosen.append(remaining[index])
    return chosen[:limit]


def _collect_point(point: dict, old_rows: dict, roads: dict, signature: str, roads_hash: str):
    tangent = local_tangent(point, roads)
    rows_out = []
    for direction in ("front", "back"):
        old = old_rows.get((point["station_id"], direction))
        expected = _metadata_row(point, direction, tangent, signature, roads_hash)
        if old:
            target = ROOT / old.get("image_path", "")
            valid, reason = _record_ok(old, point, tangent, roads, roads_hash, signature, target, recheck_metadata=True)
            if valid:
                rows_out.append(old)
                continue
            expected["failure_reason"] = "RESUME_REJECTED:" + reason
        rows_out.append(expected)

    if all(r["status"] == "VERIFIED" for r in rows_out):
        if rows_out[0]["pano_id"] != rows_out[1]["pano_id"]:
            return rows_out
        for r in rows_out:
            r.update(status="UNVERIFIED", failure_reason="FRONT_BACK_PANO_ID_MUST_DIFFER")
        return rows_out

    try:
        matches = find_two_panos(point, roads, tangent)
    except MatchError as error:
        for row in rows_out:
            if row["status"] != "VERIFIED":
                row.update(status=error.status, failure_reason=str(error))
        return rows_out
    except api.Failure as error:
        for row in rows_out:
            if row["status"] != "VERIFIED":
                row.update(status=error.status, failure_reason=str(error))
        return rows_out

    used = {r.get("pano_id") for r in rows_out if r["status"] == "VERIFIED"}
    for row in rows_out:
        if row["status"] == "VERIFIED":
            continue
        direction = row["direction"]
        match = matches[direction]
        if match[1] in used:
            match = None
        if match is None:
            row.update(status="UNVERIFIED", failure_reason="NO_UNUSED_DISTINCT_PANO")
            continue
        distance, pano_id, detail, lon, lat, side, _query_distance = match
        used.add(pano_id)
        image_rel = Path("data") / "paired_views" / f"{point['station_id']}_{direction}.jpg"
        image_path = ROOT / image_rel
        heading = float(row["request_heading_deg"])
        try:
            image = api.download(pano_id, heading)
            atomic(image_path, image)
            label = directional_label(heading, tangent)
            expected_label = direction
            row.update({
                "status": "UNVERIFIED", "pano_id": pano_id,
                "pano_lon_bd09": round(lon, 9), "pano_lat_bd09": round(lat, 9),
                "pano_lon_wgs84": round(side["lon"], 9), "pano_lat_wgs84": round(side["lat"], 9),
                "nearest_road_id": side["nearest_road_id"], "nearest_osm_way_id": side["nearest_way_id"],
                "target_way_distance_m": round(side["target_distance_m"], 3),
                "nearest_road_distance_m": round(side["nearest_distance_m"], 3),
                "distance_to_station_m": round(distance, 3), "capture_date": _date(detail),
                "image_path": str(image_rel).replace("\\", "/"), "image_sha256": sha(image_path),
                "failure_reason": "POST_SAVE_QC_PENDING",
            })
            if label == expected_label and row["nearest_osm_way_id"] == str(point["osm_way_id"]):
                row["heading_check"] = "PASS"
                ok, reason = _record_ok(row, point, tangent, roads, roads_hash, signature, image_path, recheck_metadata=True, allow_unverified=True)
                row.update(status="VERIFIED" if ok else "UNVERIFIED", failure_reason="NA" if ok else reason)
            else:
                row["heading_check"] = "FAIL"
                row.update(status="UNVERIFIED", failure_reason="POST_SAVE_DIRECTION_OR_SIDE_MISMATCH")
        except api.Failure as error:
            row.update(status=error.status, failure_reason=str(error))
    if rows_out[0].get("pano_id") == rows_out[1].get("pano_id") and rows_out[0].get("pano_id") not in (None, "", "NA"):
        for row in rows_out:
            row.update(status="UNVERIFIED", failure_reason="FRONT_BACK_PANO_ID_MUST_DIFFER")
    return rows_out


def build_validation(points: list[dict], metadata_rows: list[dict], roads: dict, signature: str,
                     roads_hash: str, source_station_count: int | None = None):
    grouped = defaultdict(list)
    lookup = {(p["station_id"], p["point_id"]): p for p in points}
    failed = []
    seen = set()
    for row in metadata_rows:
        point = lookup.get((row.get("station_id"), row.get("point_id")))
        if point is None:
            failed.append({"station_id": row.get("station_id"), "reason": "UNKNOWN_STATION"})
            continue
        unique_key = (row.get("station_id"), row.get("direction"))
        if unique_key in seen:
            failed.append({"station_id": row.get("station_id"), "direction": row.get("direction"), "reason": "DUPLICATE_DIRECTION_RECORD"})
        seen.add(unique_key)
        try:
            tangent = local_tangent(point, roads)
            image_path = ROOT / row.get("image_path", "")
            ok, reason = _record_ok(row, point, tangent, roads, roads_hash, signature, image_path, recheck_metadata=False)
        except (ValueError, MatchError):
            ok, reason = False, "GEOMETRY_OR_METADATA_INVALID"
        if row.get("status") != "VERIFIED":
            failed.append({"station_id": row.get("station_id"), "direction": row.get("direction"),
                           "reason": row.get("failure_reason") or row.get("status")})
        elif not ok:
            failed.append({"station_id": row.get("station_id"), "direction": row.get("direction"), "reason": reason})
        grouped[row.get("station_id")].append(row)
    complete = sum(station_is_complete(group) for group in grouped.values())
    for point in points:
        station_rows = grouped.get(point["station_id"], [])
        directions = {row.get("direction") for row in station_rows}
        for missing in sorted({"front", "back"} - directions):
            failed.append({"station_id": point["station_id"], "direction": missing, "reason": "MISSING_DIRECTION_RECORD"})
    failure_counts = defaultdict(int)
    for item in failed:
        failure_counts[item["reason"]] += 1
    full = complete == len(points)
    source_count = source_station_count if source_station_count is not None else len(points)
    whole_source_scope = full and source_count == len(points)
    return {"status": "SCOPE_COMPLETE" if full else "INCOMPLETE",
            "stations_in_validation_scope": len(points), "stations_in_full_sampling_frame": source_count,
            "stations_with_records": len(grouped), "complete_verified_stations": complete,
            "front_back_pano_distinct_required": True, "front_back_positions_must_differ_by_m": 1.0,
            "max_pano_to_station_distance_m": MAX_STATION_DISTANCE_M,
            "max_pano_to_target_way_distance_m": MAX_TARGET_WAY_DISTANCE_M,
            "verified_direction_rows": sum(r.get("status") == "VERIFIED" for r in metadata_rows),
            "unverified_or_failed_records": len(failed), "failure_counts": dict(sorted(failure_counts.items())),
            "failure_examples": failed[:25], "validation_scope_complete": full,
            "full_collection_complete": whole_source_scope}


def write_pair_export(ebike_rows: list[dict], car_rows: list[dict], points: list[dict], roads: dict):
    by_car = defaultdict(dict)
    for row in car_rows:
        by_car[row.get("station_id")][row.get("direction")] = row
    by_ebike = defaultdict(dict)
    for row in ebike_rows:
        if row.get("station_id") and row.get("direction") in {"front", "back"}:
            by_ebike[row["station_id"]][row["direction"]] = row
    point_by_station = {p["station_id"]: p for p in points}
    fields = ["station_id", "point_id", "road_id", "osm_way_id", "direction", "sample_lon_wgs84", "sample_lat_wgs84",
              "local_tangent_deg", "car_status", "car_pano_id", "car_lon_bd09", "car_lat_bd09", "car_distance_m", "car_distance_to_way_m", "car_nearest_osm_way_id", "car_date", "car_heading_deg", "car_image_path", "car_image_sha256",
              "ebike_status", "ebike_lon_wgs84", "ebike_lat_wgs84", "ebike_distance_to_station_m", "ebike_distance_to_way_m", "ebike_nearest_osm_way_id", "ebike_date", "ebike_heading_deg", "ebike_heading_source", "ebike_image_path", "ebike_image_sha256", "pair_status"]
    output = []
    for station_id, point in point_by_station.items():
        tangent = local_tangent(point, roads)
        for direction in ("front", "back"):
            car = by_car.get(station_id, {}).get(direction, {})
            bike = by_ebike.get(station_id, {}).get(direction, {})
            bike_status, bike_distance, bike_nearest_way = "MISSING", "NA", "NA"
            bike_station_distance = "NA"
            try:
                if bike:
                    if bike.get("coordinate_crs") != "WGS84":
                        raise ValueError("coordinate_crs must be WGS84")
                    bx, by = float(bike["longitude"]), float(bike["latitude"])
                    candidate = transform(WGS_TO_METRES, Point(bx, by))
                    ranked = sorted((item["geometry"].distance(candidate), item["osm_way_id"]) for item in roads.values())
                    target_d = min((d for d, way in ranked if way == str(point["osm_way_id"])), default=float("inf"))
                    bike_distance, bike_nearest_way = round(target_d, 3), ranked[0][1] if ranked else "NA"
                    bike_station_distance = round(distance_m(float(point["sample_lon_bd09"]), float(point["sample_lat_bd09"]), *wgs84_to_bd09(bx, by)), 3)
                    requested = tangent if direction == "front" else (tangent + 180) % 360
                    heading_ok = bike.get("heading_deg") not in (None, "", "NA") and angle_delta(float(bike["heading_deg"]), requested) <= 15
                    bike_image = ROOT / str(bike.get("image_path", ""))
                    image_ok = bike_image.is_file() and sha(bike_image) == bike.get("image_sha256")
                    bike_ok = (target_d <= MAX_TARGET_WAY_DISTANCE_M
                               and bike_station_distance <= MAX_STATION_DISTANCE_M
                               and str(bike_nearest_way) == str(point["osm_way_id"])
                               and heading_ok and image_ok)
                    bike_status = "VERIFIED" if bike_ok else "UNVERIFIED"
            except (ValueError, KeyError):
                bike_status = "UNVERIFIED"
            car_ok = car.get("status") == "VERIFIED"
            output.append({"station_id": station_id, "point_id": point["point_id"], "road_id": point["road_id"], "osm_way_id": point["osm_way_id"],
                           "direction": direction, "sample_lon_wgs84": point["sample_lon"], "sample_lat_wgs84": point["sample_lat"],
                           "local_tangent_deg": round(tangent, 6), "car_status": car.get("status", "MISSING"), "car_pano_id": car.get("pano_id", "NA"),
                           "car_lon_bd09": car.get("pano_lon_bd09", "NA"), "car_lat_bd09": car.get("pano_lat_bd09", "NA"),
                           "car_distance_m": car.get("distance_to_station_m", "NA"), "car_distance_to_way_m": car.get("target_way_distance_m", "NA"),
                           "car_nearest_osm_way_id": car.get("nearest_osm_way_id", "NA"), "car_date": car.get("capture_date", "NA"),
                           "car_heading_deg": car.get("request_heading_deg", "NA"), "car_image_path": car.get("image_path", "NA"),
                           "car_image_sha256": car.get("image_sha256", "NA"),
                           "ebike_status": bike_status, "ebike_lon_wgs84": bike.get("longitude", "NA"), "ebike_lat_wgs84": bike.get("latitude", "NA"),
                           "ebike_distance_to_station_m": bike_station_distance,
                           "ebike_distance_to_way_m": bike_distance, "ebike_nearest_osm_way_id": bike_nearest_way,
                           "ebike_date": bike.get("capture_date", "NA"), "ebike_heading_deg": bike.get("heading_deg", "NA"),
                           "ebike_heading_source": bike.get("heading_source", "NA"),
                           "ebike_image_path": bike.get("image_path", "NA"), "ebike_image_sha256": bike.get("image_sha256", "NA"),
                           "pair_status": "PAIRED_VERIFIED" if car_ok and bike_status == "VERIFIED" else "UNVERIFIED"})
    csvwrite(PAIR_EXPORT, output, fields)
    return output


def run(limit: int | None = 5, validate_only: bool = False, dry_run: bool = False, sequential: bool = False,
        random_seed: int | None = None):
    points = rows(POINTS)
    for point in points:
        if not point.get("station_id"):
            point["station_id"] = "ST_" + point["point_id"]
    if len({p["station_id"] for p in points}) != len(points):
        raise ValueError("station_id values must be unique in the target-road station table")
    selected = select_stations(points, limit, sequential=sequential, random_seed=random_seed)
    roads = load_roads()
    signature, roads_hash = sample_signature(), road_signature()
    if dry_run:
        plan = []
        for point in selected:
            tangent = local_tangent(point, roads)
            plan.append({"station_id": point["station_id"], "point_id": point["point_id"], "road_id": point["road_id"],
                         "osm_way_id": point["osm_way_id"], "road_type": point["road_type"], "front_heading_deg": round(tangent, 6),
                         "back_heading_deg": round((tangent + 180) % 360, 6),
                         "candidate_query_offset_m": 12, "required_distinct_pano_positions": 2})
        result = {"status": "DRY_RUN", "http_requests": 0, "images_written": 0,
                  "source_signature": signature, "road_signature": roads_hash, "station_count": len(plan), "stations": plan}
        atomic(ROOT / "docs" / "paired_view_dry_run.json", json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        print(json.dumps({k: v for k, v in result.items() if k != "stations"}, ensure_ascii=False, indent=2))
        return 0
    old_records = rows(META) if META.exists() else []
    old_groups = defaultdict(list)
    for record in old_records:
        old_groups[(record.get("station_id"), record.get("direction"))].append(record)
    duplicate_old_keys = {key for key, values in old_groups.items() if len(values) > 1}
    old = {}
    for key, values in old_groups.items():
        record = values[-1].copy()
        if key in duplicate_old_keys:
            record.update(status="UNVERIFIED", failure_reason="DUPLICATE_OLD_DIRECTION_RECORD")
        old[key] = record
    if not validate_only:
        output_by_key = dict(old)
        for point in selected:
            try:
                records = _collect_point(point, old, roads, signature, roads_hash)
            except MatchError as error:
                records = []
                tangent = float(point["travel_heading"])
                for direction in ("front", "back"):
                    row = _metadata_row(point, direction, tangent, signature, roads_hash)
                    row.update(status=error.status, failure_reason=str(error))
                    records.append(row)
            except api.Failure as error:
                records = []
                tangent = local_tangent(point, roads)
                for direction in ("front", "back"):
                    row = _metadata_row(point, direction, tangent, signature, roads_hash)
                    row.update(status=error.status, failure_reason=str(error))
                    records.append(row)
            for row in records:
                output_by_key[(row["station_id"], row["direction"])] = row
            csvwrite(META, list(output_by_key.values()), FIELDS)
            print(point["station_id"], "COMPLETE" if station_is_complete(records) else "INCOMPLETE")
            restricted = any(r.get("status") in {"BLOCKED", "LOOKUP_SCHEMA_ERROR"} for r in records)
            if restricted:
                break
    current_records = rows(META) if META.exists() else []
    validation_points = points if limit is None else selected
    validation_ids = {point["station_id"] for point in validation_points}
    scoped_records = [row for row in current_records if row.get("station_id") in validation_ids]
    report = build_validation(validation_points, scoped_records, roads, signature, roads_hash,
                              source_station_count=len(points))
    if random_seed is not None:
        report["random_sampling"] = {"seed": random_seed,
                                     "station_ids": [point["station_id"] for point in selected]}
    report["duplicate_direction_groups_found_on_resume"] = len(duplicate_old_keys)
    bike_rows = rows(EBIKE_TEMPLATE) if EBIKE_TEMPLATE.exists() else []
    pair_rows = write_pair_export(bike_rows, current_records, points, roads)
    report["comparison_pair_rows"] = len(pair_rows)
    report["verified_comparison_pairs"] = sum(row["pair_status"] == "PAIRED_VERIFIED" for row in pair_rows)
    atomic(REPORT, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    print(json.dumps({k: v for k, v in report.items() if k != "failure_examples"}, ensure_ascii=False, indent=2))
    return 0 if report["validation_scope_complete"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="select the full sampling frame")
    parser.add_argument("--validate-only", action="store_true", help="validate saved rows without collecting")
    parser.add_argument("--limit", type=int, default=5, help="pilot station count (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="write a heading plan without network requests")
    parser.add_argument("--sequential", action="store_true", help="select the first N stations in source order")
    parser.add_argument("--random-seed", type=int, help="randomly sample stations reproducibly using this seed")
    args = parser.parse_args()
    if not args.all and not args.validate_only and not 1 <= args.limit <= (len(rows(POINTS)) if args.random_seed is not None else 10):
        parser.error("limit must be 1..10 for pilot collection, or within the sampling frame with --random-seed")
    lock = ROOT / ".paired_views.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit("Another paired-view run holds the lock")
    try:
        return run(None if args.all else args.limit, validate_only=args.validate_only,
                   dry_run=args.dry_run, sequential=args.sequential, random_seed=args.random_seed)
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
