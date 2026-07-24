"""Specification-focused unit tests. Run with: python test_risk_engine.py"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import recommendation_engine
import risk_engine


class RiskEngineTests(unittest.TestCase):
    def test_exact_distance_boundaries(self):
        self.assertEqual(risk_engine.risk_level_for_distance(3.01), "SAFE")
        self.assertEqual(risk_engine.risk_level_for_distance(3.0), "CAUTION")
        self.assertEqual(risk_engine.risk_level_for_distance(1.01), "CAUTION")
        self.assertEqual(risk_engine.risk_level_for_distance(1.0), "DANGER")
        self.assertEqual(risk_engine.risk_level_for_distance(0.0), "DANGER")
        self.assertEqual(risk_engine.risk_level_for_distance(None), "OFFLINE")

    def test_legacy_level_normalization(self):
        self.assertEqual(risk_engine.normalize_risk_level("safe", 4), "SAFE")
        self.assertEqual(risk_engine.normalize_risk_level(1, 2), "CAUTION")
        self.assertEqual(risk_engine.normalize_risk_level(2, 0.8), "DANGER")
        self.assertEqual(risk_engine.normalize_risk_level(None, 0.8), "DANGER")

    def test_heat_stage_boundaries(self):
        cases = [
            (30.9, "NORMAL"),
            (31.0, "HEAT_CAUTION"),
            (32.9, "HEAT_CAUTION"),
            (33.0, "REST_REQUIRED"),
            (34.9, "REST_REQUIRED"),
            (35.0, "STOP_RECOMMENDED"),
            (37.9, "STOP_RECOMMENDED"),
            (38.0, "EMERGENCY_STOP"),
        ]
        for apparent, expected in cases:
            with self.subTest(apparent=apparent):
                self.assertEqual(risk_engine.heat_level_for(apparent), expected)

    def test_apparent_temperature_is_deterministic(self):
        value = risk_engine.apparent_temperature(31.4, 68.0)
        self.assertAlmostEqual(value, 32.5, places=1)

    def test_guidance_marks_administrative_recommendations(self):
        self.assertEqual(
            risk_engine.heat_guidance("STOP_RECOMMENDED")["legal_basis"],
            "administrative_recommendation",
        )
        self.assertEqual(
            risk_engine.heat_guidance("REST_REQUIRED")["legal_basis"],
            "legal_standard",
        )

    def test_worker_output_mapping(self):
        danger = risk_engine.proximity_alert("DANGER", 0.6)
        self.assertEqual(danger["led"], "red")
        self.assertTrue(danger["vibration"])
        self.assertEqual(danger["display"], "DANGER 0.6m")
        self.assertEqual(
            risk_engine.proximity_alert("OFFLINE")["display"], "OFFLINE"
        )


class RecommendationEngineTests(unittest.TestCase):
    def test_mock_assessment_is_fixed_to_high_major_forklift(self):
        assessment = recommendation_engine.fixed_assessment(
            "EVT-20260724-0001", "2026-07-24T18:30:00+09:00"
        )
        self.assertEqual(assessment["likelihood_label"], "상")
        self.assertEqual(assessment["likelihood_score"], 4)
        self.assertEqual(assessment["severity_label"], "대")
        self.assertEqual(assessment["severity_score"], 3)
        self.assertEqual(assessment["risk_score"], 12)
        self.assertEqual(assessment["risk_grade"], "상")
        self.assertEqual(assessment["equipment_kind"], "지게차")
        self.assertEqual(assessment["risk_type"], "지게차-근로자 충돌")

    def test_six_recommendations_include_forklift_pair(self):
        items = recommendation_engine.build_recommendations(
            "EVT-20260724-0007",
            "E01",
            "W03",
            3,
            "2026-07-24T18:30:00+09:00",
        )
        self.assertEqual(len(items), 6)
        self.assertEqual(
            [item["category"] for item in items],
            [
                "URGENT",
                "URGENT",
                "PRIORITY",
                "PRIORITY",
                "PRIORITY",
                "REGULAR",
            ],
        )
        self.assertIn("지게차 E01", items[4]["description"])
        self.assertIn("근로자 W03", items[4]["description"])
        self.assertIn("3회 감지", items[4]["legal_basis"])


class SchemaMigrationTests(unittest.TestCase):
    def test_existing_action_table_gets_recommendation_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy-safeon.db"
            legacy = sqlite3.connect(path)
            legacy.execute(
                """
                CREATE TABLE improvement_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL UNIQUE,
                    event_id TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    priority TEXT NOT NULL DEFAULT 'MEDIUM',
                    assignee TEXT,
                    due_date TEXT,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    created_ts TEXT NOT NULL,
                    updated_ts TEXT NOT NULL
                )
                """
            )
            legacy.commit()
            legacy.close()

            migrated = db.get_conn(str(path))
            columns = {
                row["name"]
                for row in migrated.execute(
                    "PRAGMA table_info(improvement_actions)"
                )
            }
            migrated.close()
            self.assertTrue(
                {"category", "legal_basis", "sort_order"} <= columns
            )


class IncidentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test-safeon.db"
        self.conn = db.get_conn(str(self.db_path))
        self.start = datetime(2026, 7, 24, 18, 30, tzinfo=timezone(timedelta(hours=9)))

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def reading(self, seconds, distance, level):
        return {
            "timestamp": (self.start + timedelta(seconds=seconds)).isoformat(),
            "worker_id": "W01",
            "equipment_id": "E01",
            "distance_m": distance,
            "risk_level": level,
            "near_miss": level == "DANGER",
            "sequence": seconds + 1,
            "battery_pct": 91,
        }

    def test_danger_opens_updates_and_ends_one_incident(self):
        started = db.record_proximity(
            self.conn, self.reading(0, 0.9, "DANGER")
        )
        self.assertEqual(started["transition"], "STARTED")
        self.assertEqual(started["incident"]["event_id"], "EVT-20260724-0001")

        updated = db.record_proximity(
            self.conn, self.reading(4, 0.42, "DANGER")
        )
        self.assertEqual(updated["transition"], "UPDATED")
        self.assertEqual(updated["incident"]["min_distance_m"], 0.42)

        ended = db.record_proximity(
            self.conn, self.reading(9, 1.4, "CAUTION")
        )
        self.assertEqual(ended["transition"], "ENDED")
        self.assertEqual(ended["incident"]["exposure_seconds"], 9.0)
        self.assertIsNotNone(ended["incident"]["end_ts"])
        self.assertEqual(len(db.list_incidents(self.conn)), 1)

    def test_action_status_updates_followup_action(self):
        item = db.record_proximity(
            self.conn, self.reading(0, 0.8, "DANGER")
        )["incident"]
        acknowledged = db.update_incident_action(
            self.conn, item["event_id"], "ACK"
        )
        self.assertEqual(acknowledged["action_status"], "ACK")
        actions = db.list_improvement_actions(self.conn)
        self.assertEqual(actions[0]["status"], "IN_PROGRESS")

        db.update_incident_action(self.conn, item["event_id"], "CLOSED")
        self.assertEqual(
            db.list_improvement_actions(self.conn)[0]["status"], "CLOSED"
        )

    def test_incident_report_includes_distance_stage_and_environment(self):
        db.record_environment(
            self.conn,
            {
                "timestamp": (self.start - timedelta(minutes=1)).isoformat(),
                "equipment_id": "E01",
                "temperature_c": 31.4,
                "humidity_pct": 68.0,
                "apparent_temperature_c": 32.5,
                "heat_level": "HEAT_CAUTION",
                "sensor_status": "NORMAL",
                "guidance": "수분 섭취와 휴식을 확인하세요.",
            },
        )
        event_id = db.record_proximity(
            self.conn, self.reading(0, 0.82, "DANGER")
        )["incident"]["event_id"]
        db.record_proximity(self.conn, self.reading(3, 0.55, "DANGER"))

        report = db.get_incident_report(self.conn, event_id)

        self.assertEqual(report["event_id"], event_id)
        self.assertEqual(report["risk_level"], "DANGER")
        self.assertEqual(report["distance_m"], 0.55)
        self.assertEqual(report["min_distance_m"], 0.55)
        self.assertEqual(report["action_status"], "OPEN")
        self.assertEqual(report["environment"]["temperature_c"], 31.4)
        self.assertEqual(report["environment"]["humidity_pct"], 68.0)
        self.assertEqual(report["environment"]["heat_level"], "HEAT_CAUTION")

    def test_incident_report_returns_none_for_unknown_id(self):
        self.assertIsNone(
            db.get_incident_report(self.conn, "EVT-20990101-9999")
        )

    def test_incident_creates_idempotent_recommendation_bundle(self):
        event_id = db.record_proximity(
            self.conn, self.reading(0, 0.8, "DANGER")
        )["incident"]["event_id"]
        bundle = db.get_recommendation_bundle(self.conn, event_id)
        self.assertEqual(bundle["assessment"]["risk_score"], 12)
        self.assertEqual(bundle["assessment"]["equipment_kind"], "지게차")
        self.assertEqual(len(bundle["actions"]), 6)

        regenerated = db.generate_recommendations(self.conn, event_id)
        self.assertEqual(len(regenerated["actions"]), 6)
        approved = db.approve_recommendations(self.conn, event_id, True)
        self.assertTrue(approved["assessment"]["approved"])

    def test_device_health_degraded_and_offline(self):
        db.record_proximity(self.conn, self.reading(0, 4.0, "SAFE"))
        epoch = self.start.timestamp()
        changed = db.evaluate_device_health(self.conn, now=epoch + 2.1)
        self.assertTrue(any(x["status"] == "DEGRADED" for x in changed))
        changed = db.evaluate_device_health(self.conn, now=epoch + 5.1)
        self.assertTrue(any(x["status"] == "OFFLINE" for x in changed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
