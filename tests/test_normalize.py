import unittest
from datetime import datetime, timezone

from app.normalize import normalize_record, normalize_gps_record, split_by_vehicle


class TestNormalize(unittest.TestCase):
    def test_normalize_record_basic(self):
        record = {
            "start_time": 1722470400000,
            "end_time": 1722470405000,
            "vehicle_id": "1234567890123456789",
            "tracker_id": 999,
            "pids": [
                {
                    "84": {"value": [55.2]},
                    "190": {"value": [1450.0]},
                    "92": {"value": [78.0]},
                    "183": {"value": [14.5]},
                    "mhn_veh_load_cond": {"value": ["10"]},
                }
            ],
        }
        res = normalize_record(record)
        self.assertIsNotNone(res)
        self.assertEqual(res["vehicle_id"], 1234567890123456789)
        self.assertEqual(res["speed"], 55.2)
        self.assertEqual(res["rpm"], 1450.0)
        self.assertEqual(res["engine_load"], 78.0)
        self.assertEqual(res["fuel_rate"], 14.5)
        self.assertEqual(res["vehicle_load_cond"], "10")

    def test_normalize_record_fallback_speed(self):
        record = {
            "start_time": 1722470400000,
            "vehicle_id": 100,
            "pids": [
                {
                    "mhn_84a": {"value": [42.1]},
                    "spn_190_avg": {"value": [1200.0]},
                }
            ],
        }
        res = normalize_record(record)
        self.assertIsNotNone(res)
        self.assertEqual(res["speed"], 42.1)
        self.assertEqual(res["rpm"], 1200.0)

    def test_normalize_record_missing_start_time(self):
        record = {"vehicle_id": 100}
        self.assertIsNone(normalize_record(record))

    def test_normalize_gps_record(self):
        record = {
            "intangle_id": 100,
            "vehicle_plate": "KA01AB1234",
            "lat": 12.9716,
            "lng": 77.5946,
            "timestamp": 1722470400000,
        }
        res = normalize_gps_record(record)
        self.assertIsNotNone(res)
        self.assertEqual(res["vehicle_no"], "KA01AB1234")
        self.assertEqual(res["lat"], 12.9716)
        self.assertEqual(res["lon"], 77.5946)

    def test_split_by_vehicle(self):
        records = [
            {"vehicle_id": "1", "start_time": 1000},
            {"vehicle_id": "2", "start_time": 2000},
            {"vehicle_id": "1", "start_time": 3000},
        ]
        groups = split_by_vehicle(records)
        self.assertEqual(len(groups[1]), 2)
        self.assertEqual(len(groups[2]), 1)


if __name__ == "__main__":
    unittest.main()
