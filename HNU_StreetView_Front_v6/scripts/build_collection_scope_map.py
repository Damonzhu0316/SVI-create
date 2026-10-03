"""Build read-only HTML/PDF maps from the current v6 collection dataset."""
from __future__ import annotations

import csv
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
NETWORK = ROOT / "data" / "network"
DOCS = ROOT / "docs"
HTML_OUT = DOCS / "collection_scope_map.html"
PDF_OUT = DOCS / "collection_scope_map.pdf"
REPORT_OUT = DOCS / "collection_scope_map_report.json"
TO_METRES = Transformer.from_crs(4326, 32649, always_xy=True).transform

SOURCES = {
    "aoi": NETWORK / "aoi.geojson",
    "roads": NETWORK / "roads.geojson",
    "aoi_extensions": NETWORK / "aoi_restored_road_segments.geojson",
    "points": NETWORK / "sampling_points.geojson",
    "campuses": NETWORK / "core_areas.geojson",
    "excluded": NETWORK / "excluded_objects.csv",
    "audit": NETWORK / "road_selection_audit.csv",
}

COLORS = {
    "trunk": "#8b1e3f", "trunk_link": "#a83b5d",
    "primary": "#d73027", "primary_link": "#ef6548",
    "secondary": "#fc8d59", "secondary_link": "#fdbb84",
    "tertiary": "#fdbf6f", "tertiary_link": "#fed98e",
    "unclassified": "#7b3294", "residential": "#4575b4",
    "living_street": "#74add1", "service": "#5aae61",
    "cycleway": "#008837", "footway": "#8073ac", "path": "#b35806",
    "pedestrian": "#c51b7d", "track": "#8c510a", "road": "#666666",
}

WIDTHS = {
    "trunk": 3.4, "trunk_link": 2.7, "primary": 3.1, "primary_link": 2.5,
    "secondary": 2.7, "secondary_link": 2.2, "tertiary": 2.3,
    "tertiary_link": 2.0, "unclassified": 1.9, "residential": 1.8,
    "living_street": 1.7, "service": 1.45, "cycleway": 1.7,
    "footway": 1.15, "path": 1.15, "pedestrian": 1.3, "track": 1.2,
    "road": 1.4,
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def review_value(value):
    value = str(value or "").strip()
    return value not in {"", "NA", "no_explicit_bicycle_prohibition"}


def feature_length_m(feature):
    # Recompute from the current WGS84 geometry in a Changsha metric CRS.
    # Stored per-feature length_m values are rounded and accumulate a small drift.
    return transform(TO_METRES, shape(feature["geometry"])).length


def escape(value):
    if value is None or value == "":
        return "NA"
    return html.escape(str(value), quote=True)


def compact_road_feature(feature):
    p = feature.get("properties", {})
    selected = {key: p.get(key, "NA") for key in [
        "road_id", "osm_way_id", "road_name", "road_type", "length_m",
        "access_review", "heading_basis", "bicycle", "access", "scope_role", "scope_rule",
    ] if key in p or key in {"road_id", "osm_way_id", "road_name", "road_type", "length_m", "access_review"}}
    selected["is_review"] = review_value(p.get("access_review"))
    return {"type": "Feature", "geometry": feature["geometry"], "properties": selected}


def compact_road_name_labels(roads):
    best={}
    for feature in roads:
        p=feature.get("properties",{}); name=str(p.get("road_name") or "").strip()
        if not name or name=="NA": continue
        geom=shape(feature["geometry"]); length=geom.length
        if name not in best or length>best[name][0]:
            point=geom.interpolate(.5,normalized=True)
            best[name]=(length,{"type":"Feature","geometry":{"type":"Point","coordinates":[point.x,point.y]},"properties":{"road_name":name,"road_type":p.get("road_type","NA"),"length_m":length}})
    return {"type":"FeatureCollection","features":[v[1] for v in best.values()]}


def compact_point_feature(feature):
    p = feature.get("properties", {})
    selected = {key: p.get(key, "NA") for key in [
        "station_id", "point_id", "road_name", "road_type", "sample_lon", "sample_lat",
        "travel_heading", "road_heading", "access_review", "area_role",
    ] if key in p}
    selected["review_status"] = p.get("access_review", "NA")
    selected["is_review"] = review_value(p.get("access_review"))
    return {"type": "Feature", "geometry": feature["geometry"], "properties": selected}


def build_statistics(aoi, roads, points, campuses, excluded, audit):
    road_stats = defaultdict(lambda: {"segments": 0, "length_m": 0.0})
    for feature in roads:
        road_type = feature.get("properties", {}).get("road_type", "NA") or "NA"
        road_stats[road_type]["segments"] += 1
        road_stats[road_type]["length_m"] += feature_length_m(feature)
    review_roads = [f for f in roads if review_value(f.get("properties", {}).get("access_review"))]
    review_points = [f for f in points if review_value(f.get("properties", {}).get("access_review"))]
    excluded_counts = Counter(row.get("reason", "NA") or "NA" for row in excluded)
    area_roles = Counter(f.get("properties", {}).get("area_role", "NA") or "NA" for f in points)
    bounds = shape(aoi["features"][0]["geometry"]).bounds
    if len(aoi["features"]) > 1:
        from shapely.ops import unary_union
        bounds = unary_union([shape(f["geometry"]) for f in aoi["features"]]).bounds
    reported_inside = sum(1 for row in excluded if float(row.get("length_m") or 0) > 0)
    named_features=[f for f in roads if (f.get("properties",{}).get("road_name") or "").strip() not in {"", "NA"}]
    named_roads={f["properties"]["road_name"] for f in named_features}
    return {
        "study_area": "湖南师范大学及校区间连接道路",
        "candidate_road_length_km": sum(v["length_m"] for v in road_stats.values()) / 1000,
        "candidate_road_segments": len(roads),
        "sample_interval_m": 30,
        "sample_points": len(points),
        "named_road_segments": len(named_features),
        "named_road_count": len(named_roads),
        "review_roads": len(review_roads),
        "review_road_length_km": sum(feature_length_m(f) for f in review_roads) / 1000,
        "review_points": len(review_points),
        "sample_point_area_roles": dict(sorted(area_roles.items())),
        "excluded_records": len(excluded),
        "excluded_within_aoi_records": reported_inside,
        "excluded_reason_counts": dict(sorted(excluded_counts.items())),
        "road_types": {
            key: {"segments": value["segments"], "length_km": value["length_m"] / 1000}
            for key, value in sorted(road_stats.items())
        },
        "bounds_wgs84": {"west": bounds[0], "south": bounds[1], "east": bounds[2], "north": bounds[3]},
        "yellow_scope_polygons": len(campuses),
        "audit_records": len(audit),
        "classification_note": "黄色范围内保留；范围外保留 primary / secondary，并保留连接麓山南路与桃花坪东侧的西苑路；西二环、含浦大道、清水路、新民路排除。麓山南路被旧 AOI 裁掉的线段已恢复，范围边界向道路两侧外扩 20 m。",
        "scope_audit_note": "逐路筛选及范围切分结果见 data/network/scope_change_audit.csv。",
    }


def road_popup(properties):
    labels = [
        ("OSM way ID", "osm_way_id"), ("道路名称", "road_name"),
        ("道路类型", "road_type"), ("长度（m）", "length_m"),
        ("审核状态", "access_review"), ("方向依据", "heading_basis"), ("v6 范围归属", "scope_role"),
        ("bicycle", "bicycle"), ("access", "access"),
    ]
    rows = "".join(f"<tr><th>{label}</th><td>{escape(properties.get(key, '字段不存在'))}</td></tr>" for label, key in labels)
    return f"<table class='popup-table'>{rows}</table>"


def point_popup(properties):
    labels = [
        ("采样断面", "station_id"), ("采样点", "point_id"), ("道路名称", "road_name"), ("道路类型", "road_type"),
        ("经度", "sample_lon"), ("纬度", "sample_lat"),
        ("骑行方向", "travel_heading"), ("审核状态", "review_status"),
        ("范围归属", "scope_role"),
    ]
    rows = "".join(f"<tr><th>{label}</th><td>{escape(properties.get(key, '字段不存在'))}</td></tr>" for label, key in labels)
    return f"<table class='popup-table'>{rows}</table>"


def build_html(aoi, roads, points, campuses, stats, aoi_extensions):
    compact_roads = [compact_road_feature(f) for f in roads]
    compact_points = [compact_point_feature(f) for f in points]
    road_stats = stats["road_types"]
    legend_rows = "".join(
        f"<div class='legend-row'><span class='line-swatch' style='background:{COLORS.get(kind, '#666')}'></span>"
        f"<span>{escape(kind)}</span><span>{value['segments']} 段 / {value['length_km']:.2f} km</span></div>"
        for kind, value in road_stats.items()
    )
    restored_m=sum(float(f.get('properties',{}).get('restored_length_m',0)) for f in aoi_extensions.get('features',[]))
    excluded_rows = "".join(
        f"<li>{escape(reason)}：{count}</li>" for reason, count in stats["excluded_reason_counts"].items()
    )
    bounds = stats["bounds_wgs84"]
    area_roles = stats["sample_point_area_roles"]
    template = Path(__file__).with_name('collection_scope_template.html').read_text(encoding='utf-8')
    summary = (
        f"<h1>湖南师范大学街景采集范围</h1>"
        f"<p>候选道路：<strong>{stats['candidate_road_length_km']:.3f} km</strong> / {stats['candidate_road_segments']} 段</p>"
        f"<p>采样间隔：{stats['sample_interval_m']} m；采样点：<strong>{stats['sample_points']}</strong></p>"
        f"<p>待核查道路：{stats['review_roads']} 段 / {stats['review_road_length_km']:.3f} km</p>"
        f"<p>待核查采样点：<strong>{stats['review_points']}</strong></p>"
        f"<p>黄色范围内点：{area_roles.get('yellow_area',0)}；校区间主次干道点：{area_roles.get('intercampus_major_secondary',0)}；西苑路连接点：{area_roles.get('intercampus_east_campus_connector',0)}</p>"
        f"<p>道路名称：{stats.get('named_road_count',0)} 个标签（代表点）；放大地图查看。无名称道路不加标签。</p>"
        f"<p>排除道路：西二环、含浦大道、清水路、新民路。</p>"
        f"<p><strong>麓山南路范围修复：</strong>已从 OSM 原始快照恢复旧 AOI 裁掉的 {restored_m:.0f} m 道路线，并把虚线范围向路段外扩 20 m。</p>"

        f"<p class='note'>{escape(stats['classification_note'])}</p>"
        f"<p class='note'>底图需联网；研究图层已嵌入本文件。</p>"
    )
    legend = f"<h2>道路类型 / 段数 / 长度</h2>{legend_rows}<p class='note'>黄色范围内按范围保留；范围外保留主次干道及西苑路连接段，按要求排除西二环、含浦大道、清水路、新民路。</p>"
    replacements = {
        "__LEAFLET_CSS__": (Path(__file__).parent / 'vendor/leaflet.css').read_text(encoding='utf-8'),
        "__LEAFLET_JS__": (Path(__file__).parent / 'vendor/leaflet.js').read_text(encoding='utf-8').replace('//# sourceMappingURL=leaflet.js.map',''),
        "__KM__": f"{stats['candidate_road_length_km']:.3f}",
        "__COUNT__": str(stats['sample_points']),
        "__ROAD_COUNT__": str(stats['candidate_road_segments']),
        "__REVIEW_ROADS__": str(stats['review_roads']),
        "__REVIEW_POINTS__": str(stats['review_points']),
        "__LEGEND_HTML__": legend_rows,
        "__SUMMARY_HTML__": summary,
        "__ROADS__": json.dumps({"type": "FeatureCollection", "features": compact_roads}, ensure_ascii=False, separators=(",", ":")),
        "__AOI_EXTENSIONS__": json.dumps(aoi_extensions, ensure_ascii=False, separators=(",", ":")),
        "__POINTS__": json.dumps({"type": "FeatureCollection", "features": compact_points}, ensure_ascii=False, separators=(",", ":")),
        "__ROAD_LABELS__": json.dumps(compact_road_name_labels(roads), ensure_ascii=False, separators=(",", ":")),
        "__AOI__": json.dumps(aoi, ensure_ascii=False, separators=(",", ":")),
        "__CAMPUSES__": json.dumps(campuses, ensure_ascii=False, separators=(",", ":")),
        "__COLORS__": json.dumps(COLORS, separators=(",", ":")),
        "__WIDTHS__": json.dumps(WIDTHS, separators=(",", ":")),
        "__SUMMARY__": json.dumps(summary, ensure_ascii=False),
        "__LEGEND__": json.dumps(legend, ensure_ascii=False),
        "__SOUTH__": str(bounds["south"]), "__WEST__": str(bounds["west"]),
        "__NORTH__": str(bounds["north"]), "__EAST__": str(bounds["east"]),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    HTML_OUT.write_text(template, encoding="utf-8")


def line_parts(geometry):
    if geometry.geom_type == "LineString":
        return [list(geometry.coords)]
    if geometry.geom_type == "MultiLineString":
        return [list(part.coords) for part in geometry.geoms]
    if geometry.geom_type == "GeometryCollection":
        return [coords for part in geometry.geoms for coords in line_parts(part)]
    return []


def polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        return [list(geometry.exterior.coords)]
    if geometry.geom_type == "MultiPolygon":
        return [list(part.exterior.coords) for part in geometry.geoms]
    return []


def configure_font():
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyh.ttf"),
        Path("C:/Windows/Fonts/simhei.ttf"), Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    font = next((path for path in candidates if path.exists()), None)
    if font:
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["pdf.fonttype"] = 42


def add_scalebar(ax, bounds):
    width = bounds[2] - bounds[0]
    candidates = [100, 200, 500, 1000, 2000, 5000]
    target = width * 0.16
    scale = min(candidates, key=lambda value: abs(value - target))
    x = bounds[0] + width * 0.05
    y = bounds[1] + (bounds[3] - bounds[1]) * 0.055
    ax.plot([x, x + scale], [y, y], color="#111", lw=3, solid_capstyle="butt", zorder=20)
    ax.plot([x, x], [y - width * .005, y + width * .005], color="#111", lw=1, zorder=20)
    ax.plot([x + scale, x + scale], [y - width * .005, y + width * .005], color="#111", lw=1, zorder=20)
    ax.text(x + scale / 2, y + width * .012, f"{scale/1000:g} km" if scale >= 1000 else f"{scale} m", ha="center", va="bottom", fontsize=8)


def add_north(ax, bounds):
    x = bounds[2] - (bounds[2] - bounds[0]) * .06
    y = bounds[3] - (bounds[3] - bounds[1]) * .07
    dy = (bounds[3] - bounds[1]) * .07
    ax.annotate("N", xy=(x, y), xytext=(x, y - dy), ha="center", va="center", fontsize=12, fontweight="bold", arrowprops=dict(facecolor="#111", width=3, headwidth=10))


def draw_page(pdf, title, subtitle, aoi_m, roads_m, points_m, campuses_m, stats, extent=None, review_only=False, extensions_m=None):
    overview = extent is None
    fig, ax = plt.subplots(figsize=(11.69, 8.27), dpi=160)
    fig.patch.set_facecolor("#f7f8fa"); ax.set_facecolor("#fcfcfb")
    if extent is None:
        all_bounds = [geom.bounds for geom, _ in aoi_m]
        extent = (min(b[0] for b in all_bounds), min(b[1] for b in all_bounds), max(b[2] for b in all_bounds), max(b[3] for b in all_bounds))
    for geom, _ in aoi_m:
        for coords in polygon_parts(geom):
            x, y = zip(*coords); ax.fill(x, y, color="#d8dee8", alpha=.24, zorder=0); ax.plot(x, y, color="#4b5563", lw=1.2, ls="--", zorder=1)
    if review_only:
        selected = [(geom, p) for geom, p in roads_m if review_value(p.get("access_review"))]
        segments = [coords for geom, _ in selected for coords in line_parts(geom)]
        ax.add_collection(LineCollection(segments, colors="#df087f", linewidths=1.8, alpha=.92, zorder=5))
    else:
        for kind in stats["road_types"]:
            segments = [coords for geom, p in roads_m if (p.get("road_type") or "NA") == kind for coords in line_parts(geom)]
            if segments:
                ax.add_collection(LineCollection(segments, colors=COLORS.get(kind, "#666"), linewidths=max(.55, WIDTHS.get(kind, 1.4) * .55), alpha=.92, zorder=4))
    point_selection = [(geom, p) for geom, p in points_m if (review_value(p.get("access_review")) if review_only else True)]
    if point_selection:
        ax.scatter([geom.x for geom, _ in point_selection], [geom.y for geom, _ in point_selection], s=5 if review_only else 2.1, c="#ff2d95" if review_only else "#172b4d", alpha=.82 if review_only else .48, linewidths=0, zorder=6)
    for geom, p in campuses_m:
        if geom.bounds[2] < extent[0] or geom.bounds[0] > extent[2] or geom.bounds[3] < extent[1] or geom.bounds[1] > extent[3]:
            continue
        for coords in polygon_parts(geom):
            x, y = zip(*coords); ax.fill(x, y, color="#f6bd60", alpha=.28, zorder=2); ax.plot(x, y, color="#b45309", lw=1.1, zorder=3)
        point = geom.representative_point()
        name = p.get("name") or p.get("NAME") or p.get("id") or "校区"
        ax.text(point.x, point.y, str(name), fontsize=7, ha="center", va="center", color="#713f12", zorder=10, bbox=dict(boxstyle="round,pad=.18", fc="white", ec="#d97706", alpha=.84, lw=.5))
    # One static label per named road, using the midpoint of its longest mapped segment.
    if not review_only:
        named={}
        for geom,p in roads_m:
            name=str(p.get("road_name") or "").strip()
            if not name or name=="NA": continue
            if overview and p.get("road_type") not in {"primary","secondary","primary_link","secondary_link"}: continue
            mid=geom.interpolate(.5,normalized=True)
            if not (extent[0]<=mid.x<=extent[2] and extent[1]<=mid.y<=extent[3]): continue
            if name not in named or geom.length>named[name][0]: named[name]=(geom.length,mid)
        occupied=[]
        for name,(_,mid) in sorted(named.items()):
            if any(mid.distance(prev)<(220 if overview else 110) for prev in occupied): continue
            occupied.append(mid)
            ax.text(mid.x,mid.y,name,fontsize=7.2,ha="center",va="center",color="#182b39",zorder=12,
                    bbox=dict(boxstyle="round,pad=.16",fc="white",ec="#7b8790",alpha=.88,lw=.35))
    pad_x=(extent[2]-extent[0])*.035; pad_y=(extent[3]-extent[1])*.035
    ax.set_xlim(extent[0]-pad_x,extent[2]+pad_x); ax.set_ylim(extent[1]-pad_y,extent[3]+pad_y); ax.set_aspect("equal",adjustable="box")
    ax.set_title(title, loc="left", fontsize=18, fontweight="bold", color="#17212b", pad=15)
    ax.text(0,1.005,subtitle,transform=ax.transAxes,ha="left",va="bottom",fontsize=9,color="#56616d")
    ax.set_xlabel("Easting (m), WGS 84 / UTM zone 49N"); ax.set_ylabel("Northing (m)")
    ax.grid(color="#d9dee5",lw=.45,alpha=.55); ax.ticklabel_format(style="plain",useOffset=False)
    ax.tick_params(labelsize=7)
    add_scalebar(ax, extent); add_north(ax, extent)
    if review_only:
        handles=[Line2D([0],[0],color="#df087f",lw=2,label=f"待核查道路 {stats['review_roads']} 段 / {stats['review_road_length_km']:.2f} km"),Line2D([0],[0],marker='o',color='none',markerfacecolor='#ff2d95',markersize=5,label=f"待核查采样点 {stats['review_points']} 个"),Patch(facecolor='#f6bd60',edgecolor='#b45309',alpha=.5,label='黄色范围')]
    else:
        handles=[Line2D([0],[0],color=COLORS.get(kind,"#666"),lw=2,label=f"{kind}: {value['segments']} 段 / {value['length_km']:.2f} km") for kind,value in stats['road_types'].items()]
        handles += [Line2D([0],[0],marker='o',color='none',markerfacecolor='#172b4d',markersize=4,label=f"采样点 {stats['sample_points']} 个"),Patch(facecolor='#f6bd60',edgecolor='#b45309',alpha=.5,label='黄色范围')]
    ax.legend(handles=handles,loc="upper left",bbox_to_anchor=(1.01,1),borderaxespad=0,frameon=True,fontsize=7,title="图例",title_fontsize=9)
    fig.text(.985,.02,f"候选道路 {stats['candidate_road_length_km']:.3f} km | {stats['candidate_road_segments']} 段 | 采样间隔 {stats['sample_interval_m']} m | 数据为当前 v6 范围",ha="right",fontsize=7,color="#5b6570")
    if (extent[2] - extent[0]) / max(1, extent[3] - extent[1]) > 2.4:
        fig.text(.075,.18,"阅读提示",fontsize=12,fontweight="bold",color="#17212b")
        fig.text(.075,.145,"本页按真实米制比例显示东西向连接带，因此地图呈狭长形。请结合第 1 页总览和 HTML 交互地图放大检查。",fontsize=9,color="#56616d")
        fig.text(.075,.115,f"当前完整候选集：{stats['candidate_road_segments']} 段 / {stats['candidate_road_length_km']:.3f} km；待核查：{stats['review_roads']} 段 / {stats['review_points']} 个采样点。",fontsize=9,color="#56616d")
    fig.subplots_adjust(left=.075,right=.77,top=.89,bottom=.10)
    pdf.savefig(fig); plt.close(fig)


def expanded_bounds(geometries, fraction=.20):
    bounds=[geometry.bounds for geometry in geometries]
    box=(min(b[0] for b in bounds),min(b[1] for b in bounds),max(b[2] for b in bounds),max(b[3] for b in bounds))
    dx=(box[2]-box[0])*fraction; dy=(box[3]-box[1])*fraction
    return box[0]-dx,box[1]-dy,box[2]+dx,box[3]+dy


def build_pdf(aoi, roads, points, campuses, stats, aoi_extensions):
    configure_font()
    aoi_m=[(transform(TO_METRES,shape(f['geometry'])),f.get('properties',{})) for f in aoi['features']]
    roads_m=[(transform(TO_METRES,shape(f['geometry'])),f.get('properties',{})) for f in roads]
    points_m=[(transform(TO_METRES,shape(f['geometry'])),f.get('properties',{})) for f in points]
    campuses_m=[(transform(TO_METRES,shape(f['geometry'])),f.get('properties',{})) for f in campuses]
    extensions_m=[(transform(TO_METRES,shape(f['geometry'])),f.get('properties',{})) for f in aoi_extensions.get('features',[])]
    centre_y=sorted(geom.centroid.y for geom,_ in campuses_m)[len(campuses_m)//2]
    north=[geom for geom,_ in campuses_m if geom.centroid.y>=centre_y]
    south=[(geom, props) for geom,props in campuses_m if geom.centroid.y<centre_y]
    with PdfPages(PDF_OUT,metadata={'Title':'湖南师范大学街景采集范围总览','Author':'HNU Street View Front v6','Subject':'Current candidate roads and sample points'}) as pdf:
        draw_page(pdf,'湖南师范大学街景采集范围总览','研究 AOI、v6 保留路网、更新版采样点与黄色范围。',aoi_m,roads_m,points_m,campuses_m,stats,extensions_m=extensions_m)
        if north:
            draw_page(pdf,'北部黄色范围与连接道路放大图','放大显示黄色范围、范围内道路、主次干道、西苑路连接段及新版采样点。',aoi_m,roads_m,points_m,campuses_m,stats,expanded_bounds(north,.22),extensions_m=extensions_m)
        if len(south)>=3:
            south_west=sorted(south,key=lambda item:item[0].centroid.x)[:2]
            south_east=sorted(south,key=lambda item:item[0].centroid.x)[1:]
            west_geoms=[geom for geom,_ in south_west]; east_geoms=[geom for geom,_ in south_east]
            west_extent=expanded_bounds(west_geoms,.38); east_extent=expanded_bounds(east_geoms,.38)
            draw_page(pdf,'南部西段黄色范围与连接道路放大图','桃花坪校区至南院校区一带；按常规道路类型显示候选道路与采样点。',aoi_m,roads_m,points_m,campuses_m,stats,west_extent,extensions_m=extensions_m)
            draw_page(pdf,'南部东段黄色范围与连接道路放大图','南院校区至天马学生公寓一带；按常规道路类型显示候选道路与采样点。',aoi_m,roads_m,points_m,campuses_m,stats,east_extent,extensions_m=extensions_m)
        elif south:
            draw_page(pdf,'南部黄色范围与连接道路放大图','放大显示南部黄色范围及保留道路、采样点。',aoi_m,roads_m,points_m,campuses_m,stats,expanded_bounds([geom for geom,_ in south],.18),extensions_m=extensions_m)
        draw_page(pdf,'待核查道路与采样点','洋红色对象仍有骑行权限或道路相关性需要核查；路名排除及西苑路连接例外按图示执行。',aoi_m,roads_m,points_m,campuses_m,stats,review_only=True,extensions_m=extensions_m)


def main():
    missing=[str(path) for path in SOURCES.values() if not path.exists()]
    if missing: raise FileNotFoundError("Missing current project data: "+", ".join(missing))
    aoi=load_json(SOURCES['aoi']); road_fc=load_json(SOURCES['roads']); point_fc=load_json(SOURCES['points']); campus_fc=load_json(SOURCES['campuses']); aoi_extensions=load_json(SOURCES['aoi_extensions'])
    roads=road_fc['features']; points=point_fc['features']; campuses=campus_fc['features']; excluded=load_csv(SOURCES['excluded']); audit=load_csv(SOURCES['audit'])
    stats=build_statistics(aoi,roads,points,campuses,excluded,audit)
    stats['excluded_records']=0
    stats['excluded_within_aoi_records']=0
    stats['excluded_reason_counts']={}
    stats['audit_records']=len(load_csv(NETWORK / 'scope_change_audit.csv'))
    stats['source_files']=['data/raw/osm_main.json','data/network/aoi_v6_original.geojson','data/network/aoi.geojson','data/network/aoi_restored_road_segments.geojson','data/network/source_candidate_roads.geojson','data/network/roads.geojson','data/network/sampling_points.geojson','data/network/core_areas.geojson','data/network/scope_change_audit.csv']
    stats['generated_files']=[str(HTML_OUT.relative_to(ROOT)).replace('\\','/'),str(PDF_OUT.relative_to(ROOT)).replace('\\','/')]
    stats['v4_baseline_before_scope_change']={'road_length_km':91.141,'sample_points':3344}
    stats['difference_from_v4_baseline']={'road_length_km':stats['candidate_road_length_km']-91.141,'sample_points':stats['sample_points']-3344}
    stats['aoi_revision']=load_json(DOCS/'scope_change_report.json').get('aoi_revision',{})
    stats['aoi_restored_road_length_m']=round(sum(float(f.get('properties',{}).get('restored_length_m',0)) for f in aoi_extensions.get('features',[])),3)
    DOCS.mkdir(parents=True,exist_ok=True)
    build_html(aoi,roads,points,campuses,stats,aoi_extensions); build_pdf(aoi,roads,points,campuses,stats,aoi_extensions)
    REPORT_OUT.write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(stats,ensure_ascii=False,indent=2))
    print(f"HTML: {HTML_OUT}"); print(f"PDF:  {PDF_OUT}")


if __name__=='__main__':
    main()
