import unittest
from unittest.mock import patch

from pyproj import Transformer
from shapely.geometry import LineString
from shapely.ops import transform

import paired_views as pv


class PairedViewTests(unittest.TestCase):
    def test_angles_are_numeric_and_curved_labels_are_checked(self):
        self.assertEqual(pv.directional_label(358, 1), "front")
        self.assertEqual(pv.directional_label(181, 1), "back")
        self.assertEqual(pv.directional_label(30, 1), "UNVERIFIED")
        self.assertAlmostEqual(pv.angle_delta(1, 359), 2)

    def test_completion_requires_two_verified_directions_and_different_panos(self):
        lon, lat = pv.wgs84_to_bd09(112.95, 28.18)
        lon2, lat2 = pv.wgs84_to_bd09(112.95002, 28.18)
        rows = [dict(direction="front", status="VERIFIED", pano_id="p1", pano_lon_bd09=lon, pano_lat_bd09=lat),
                dict(direction="back", status="VERIFIED", pano_id="p2", pano_lon_bd09=lon2, pano_lat_bd09=lat2)]
        self.assertTrue(pv.station_is_complete(rows))
        rows[1]["pano_id"] = "p1"
        self.assertFalse(pv.station_is_complete(rows))
        rows[1]["pano_id"] = "p2"
        rows[1]["status"] = "UNVERIFIED"
        self.assertFalse(pv.station_is_complete(rows))

    def test_pilot_selection_spreads_across_road_types(self):
        points = [{"point_id": f"P{i}", "road_type": kind, "road_id": kind, "osm_way_id": str(100+i)}
                  for i, kind in enumerate(("primary", "primary", "secondary", "service", "residential", "footway"))]
        points[1].update(point_id="HNU_000003", osm_way_id="54818722")
        selected = pv.select_stations(points, 5)
        self.assertEqual(len(selected), 5)
        self.assertIn("HNU_000003", [point["point_id"] for point in selected])

    def test_sequential_selection_returns_first_ten_source_stations(self):
        points = [{"point_id": f"HNU_{i:06d}"} for i in range(1, 16)]
        selected = pv.select_stations(points, 10, sequential=True)
        self.assertEqual([p["point_id"] for p in selected], [f"HNU_{i:06d}" for i in range(1, 11)])

    def test_seeded_random_selection_is_reproducible_and_not_sequential(self):
        points = [{"point_id": f"HNU_{i:06d}"} for i in range(1, 101)]
        first = pv.select_stations(points, 50, random_seed=240924)
        second = pv.select_stations(points, 50, random_seed=240924)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 50)
        self.assertNotEqual(first, points[:50])

    def test_parallel_way_mismatch_rejects_nearer_opposite_carriageway(self):
        to_m = Transformer.from_crs(4326, 32649, always_xy=True).transform
        to_wgs = Transformer.from_crs(32649, 4326, always_xy=True).transform
        base_lon, base_lat = 112.95, 28.18
        x, y = to_m(base_lon, base_lat)
        target_geom = LineString([to_wgs(x - 100, y), to_wgs(x + 100, y)])
        opposite_geom = LineString([to_wgs(x - 100, y + 12), to_wgs(x + 100, y + 12)])
        roads = {
            "target": {"geometry": target_geom, "road_id": "target", "osm_way_id": "54818722"},
            "opposite": {"geometry": opposite_geom, "road_id": "opposite", "osm_way_id": "310762325"},
        }
        pano_lon, pano_lat = pv.wgs84_to_bd09(*to_wgs(x, y + 9))
        result = pv.road_side_audit(pano_lon, pano_lat, "54818722", roads)
        self.assertFalse(result["ok"])
        self.assertEqual(result["nearest_way_id"], "310762325")

    def test_forward_and_reverse_queries_choose_distinct_actual_panos(self):
        to_m = Transformer.from_crs(4326, 32649, always_xy=True).transform
        to_wgs = Transformer.from_crs(32649, 4326, always_xy=True).transform
        x, y = to_m(112.95, 28.18)
        line = LineString([to_wgs(x - 100, y), to_wgs(x + 100, y)])
        roads = {"r": {"geometry": transform(Transformer.from_crs(4326, 32649, always_xy=True).transform, line),
                        "road_id": "r", "osm_way_id": "54818722"}}
        sample_lon, sample_lat = to_wgs(x, y)
        sample_bd = pv.wgs84_to_bd09(sample_lon, sample_lat)
        front_lon, front_lat = to_wgs(x + 10, y)
        back_lon, back_lat = to_wgs(x - 10, y)
        front_bd = pv.wgs84_to_bd09(front_lon, front_lat)
        back_bd = pv.wgs84_to_bd09(back_lon, back_lat)
        point = {"sample_lon": sample_lon, "sample_lat": sample_lat,
                 "sample_lon_bd09": sample_bd[0], "sample_lat_bd09": sample_bd[1],
                 "osm_way_id": "54818722"}
        with patch.object(pv, "_candidate_ids", side_effect=[(["FRONT_PANO"], *sample_bd), (["BACK_PANO"], *sample_bd)]) as query, \
             patch.object(pv, "exact_pano", side_effect=[({"Date": "2026-09-24"}, *front_bd), ({"Date": "2026-09-24"}, *back_bd)]):
            matched = pv.find_two_panos(point, roads, 90)
        self.assertEqual(matched["front"][1], "FRONT_PANO")
        self.assertEqual(matched["back"][1], "BACK_PANO")
        self.assertEqual(query.call_args_list[0].args[2], "front")
        self.assertEqual(query.call_args_list[1].args[2], "back")


if __name__ == "__main__":
    unittest.main()
