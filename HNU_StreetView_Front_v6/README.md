# 湖南师范大学街景采集范围 v6

本目录是 v6 采集范围与双向匹配版，独立保留在 `HNU_StreetView_Front_v6`。v4 原目录不作改动。

## 本版范围规则

- 黄色圈定范围内：保留范围内的道路部分，不按道路等级筛除。
- 黄色圈定范围外：保留 OSM `primary`（主干道）、`secondary`（次主干道），并例外保留“西苑路”作为麓山南路至桃花坪东侧道路的连接。`primary_link`、`secondary_link` 仅在黄色范围外不作为校区间主次干道保留；黄色范围内仍按范围规则保留。
- 按本次要求排除西二环、含浦大道、清水路、新民路。
- 道路跨越黄色边界时先按边界切开，分别应用范围内外规则。
- 连通核对：麓山南路 → OSM 无名 primary way 1031928620 → 西苑路 way 1472196926；两条平行 OSM 线形按原几何保留。
- AOI 截断修复：以 `data/network/aoi_v6_original.geojson` 保存旧范围基线，将旧 AOI 虚线边界向麓山南路缺失段两侧外扩 20 m；从原始 OSM 快照恢复 way 1030743554、1023634492、1031928621 被裁掉的共 752.378 m 道路线。道路按常规道路等级配色，HTML 可快速定位，PDF 有专门放大页。
- 原始 OSM 快照保存在 `data/raw/osm_main.json`（数据时间：2026-09-21），不覆盖旧版项目数据。

本版依据图上的黄色面进行几何判断，不把黄色面的数量等同于校区数量。黄色面数据沿用 `data/network/core_areas.geojson`，边界是原数据中的 OSM 范围，不是地籍测量边界。

## 结果

- v6 候选路网：613 段；保留路网：192 段，约 38.793 km，按黄色范围、校区间主次干道和西苑路连接段分类，详见 `docs/scope_change_report.json`。
- 30 m 间隔重新生成 1393 个采样点。点位 ID 已重新编号，不能用旧版点位 ID 对应照片。
- 14 组采样坐标在 1e-8 度精度下重合，分属不同道路记录。交叉口重合点保留各自的道路 ID 和方向，不能仅按坐标去重；明细见 `docs/scope_change_report.json`。
- 每个 `station_id` 有 `front`、`back` 两条目标记录。方向由该采样点关联的 OSM way 局部切线逐点计算，不使用固定方位类别。
- 百度前后视图必须匹配到两个不同 pano ID，且实际 pano 坐标相距至少 1 m。单 pano 请求 180°图像不能构成双向完整样本。
- 目前尚无 V6 百度双向影像或电瓶车影像验收结果；所有未采集、侧别不符或角度未核验的记录均不得计入完整样本。

## 地图与数据

- `docs/collection_scope_map.html`：可交互地图。默认可无底图离线查看，联网时可切换在线底图；提供“麓山南路 AOI 截断修复段”快速定位。
- `docs/collection_scope_map.pdf`：含道路名称标注的范围总览、AOI 修复局部放大图及待核查图。
- `data/network/roads.geojson`、`data/sampling_points.csv`：v6 保留道路和新采样点。
- `data/network/source_candidate_roads.geojson`：v4 原始候选路网副本，供本版范围重建使用。
- `data/network/scope_change_audit.csv`：源道路保留/排除审计。
- `docs/scope_change_report.json`：范围规则、长度和采样点汇总。

## 双向采集与配对

`data/sampling_points.csv` 的 `station_id` 是跨影像来源的主键；同一站点的前后向保留独立记录。`scripts/paired_views.py` 根据对应 `road_id` 的 WGS84 OSM 几何在站点前后各取 3 m 计算局部切线角，`front` 为该角度，`back` 为反向角度。弯道每个站点分别计算。

两向分别从百度公开 pano 元数据中匹配实际位置。候选 pano 必须在站点 30 m 内、距目标 OSM way 不超过 25 m，且目标 way 必须是最近道路所属 way；这样不会把 way 310762325 上更近的 pano 误分给 way 54818722，也不再用 50 m 半径单独决定匹配。两向 pano ID 必须不同，实测位置至少相隔 1 m。图片使用与 V5 相同的公开 `mapsv0.bdimg.com/?qt=pr3d` 定向图像资源，不调用需要 AK 的 `panorama/v2` API；不需要 AK、cookie 或登录。每张图片保存后，再核对文件、哈希、实际 pano 元数据与道路侧，并验证请求角度相对局部切线的方向标签。续传时会重新查询 pano 元数据并重新检查道路侧、位置、拍摄日期、角度和图像哈希；旧的单视图文件不能直接作为通过记录。电瓶车记录使用相同的 30 m 站点距离和 25 m 目标 way 距离限值。

默认五点试采会包含报告中存在平行 OSM way 错配风险的 way `54818722`，并尽量覆盖其他道路类型。按数据表顺序试采前 10 个站点使用 `--limit 10 --sequential`：

```powershell
python scripts/paired_views.py --dry-run
python scripts/paired_views.py --dry-run --limit 10 --sequential
python scripts/paired_views.py --limit 5
python scripts/paired_views.py --limit 10 --sequential
python scripts/paired_views.py --validate-only --limit 10 --sequential
```

按固定随机种子抽取最多 1,393 个采样断面中的任意数量，例如随机采集 50 个：

```powershell
python scripts/paired_views.py --limit 50 --random-seed 240924
python scripts/paired_views.py --validate-only --limit 50 --random-seed 240924
```

随机种子和所选 `station_id` 会写入核验报告，便于复查和续传。若随机样本与已有采集记录重合，续传流程会重新核验原有 pano、道路侧、位置和方向标签。

当前不运行全量采集。只有检查试采图像和道路侧后，再单独启动全量命令 `python scripts/paired_views.py --all`。用 `--all --validate-only` 可验证全量采样框架。旧 `scripts/front_view.py` 已停止单方向采集入口，只保留底层接口供双向流程调用。

电瓶车影像按站点和方向填入 `data/ebike_observations_template.csv`。一行代表一个实际拍摄位置，坐标必须标为 WGS84，并填写方向、实际拍摄角、日期、图片路径和 SHA-256。报告会按同一 `station_id`、同一 `front/back`、同一 OSM way、位置侧别和方向角生成 `data/comparison_pairs.csv`；位置差异和日期会保留，不会被强行对齐或覆盖。非 WGS84 坐标、缺失文件、缺少哈希、侧别不符或角度不符都标为 `UNVERIFIED`。

主要输出：

- `data/metadata/paired_view_metadata.csv`：每站点两行汽车影像匹配和验收元数据。
- `data/paired_views/{station_id}_front.jpg`、`{station_id}_back.jpg`：两个独立 pano 请求结果。
- `docs/paired_view_validation.json`：按站点统计前后向完整度及未通过原因。
- `data/comparison_pairs.csv`：汽车与电瓶车按站点、道路侧和方向配对的对照表。

## 重建范围和地图

在本目录运行：

```powershell
python scripts/build_scope_v6.py
python scripts/build_collection_scope_map.py
```

范围重建不会访问街景服务。范围重建会重新生成点位 ID 与 `station_id`，已有影像若采样源签名改变会被拒绝续传。修改影像采集范围后必须先重建并人工核对黄色边界、校区间道路和目标 OSM way。
