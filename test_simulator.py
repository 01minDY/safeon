"""Tests for the deterministic 30-second SafeON demo scenario."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
import risk_engine
import simulator
from mqtt_ingest import Ingest


class DemoScenarioTests(unittest.TestCase):
    def test_demo_is_exactly_thirty_seconds(self):
        self.assertEqual(simulator.DEMO_DURATION_SECONDS, 30.0)
        self.assertEqual(
            simulator.DISTANCE_KEYFRAMES[-1].second,
            simulator.DEMO_DURATION_SECONDS,
        )

    def test_all_three_distance_stages_are_clear(self):
        expected = {
            4: "SAFE",
            10: "CAUTION",
            18: "DANGER",
            23: "CAUTION",
            28: "SAFE",
        }
        for second, level in expected.items():
            with self.subTest(second=second):
                distance = simulator.scenario_at(second)
                self.assertEqual(
                    risk_engine.risk_level_for_distance(distance),
                    level,
                )

    def test_distance_curve_is_continuous_at_keyframes(self):
        for keyframe in simulator.DISTANCE_KEYFRAMES:
            self.assertAlmostEqual(
                simulator.scenario_at(keyframe.second),
                keyframe.distance_m,
                places=6,
            )

    def test_environment_changes_gradually(self):
        start_temperature, start_humidity = simulator.environment_at(0)
        end_temperature, end_humidity = simulator.environment_at(30)
        self.assertLess(abs(end_temperature - start_temperature), 1.0)
        self.assertLess(abs(end_humidity - start_humidity), 3.0)


class DashboardIntegrationTests(unittest.TestCase):
    def test_scenario_creates_closed_clickable_report_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = db.get_conn(str(Path(temp_dir) / "demo.db"))
            ingest = Ingest(conn)
            started = datetime(
                2026,
                7,
                25,
                14,
                0,
                tzinfo=timezone(timedelta(hours=9)),
            )
            transitions = []
            event_id = None

            for second in range(31):
                timestamp = (started + timedelta(seconds=second)).isoformat()
                distance = round(simulator.scenario_at(second), 2)
                level = risk_engine.risk_level_for_distance(distance)
                if second in {0, 16, 24}:
                    ingest.handle_environment(
                        simulator._environment_payload(
                            timestamp=timestamp,
                            elapsed=second,
                            heat=False,
                        ),
                        transport="http",
                    )
                result = ingest.handle_proximity(
                    simulator._proximity_payload(
                        timestamp=timestamp,
                        sequence=10_000 + second,
                        distance_m=distance,
                        level=level,
                        elapsed=second,
                    ),
                    transport="http",
                )
                if result["incident_transition"]:
                    transitions.append(result["incident_transition"])
                if result.get("incident"):
                    event_id = result["incident"]["event_id"]

            report = db.get_incident_report(conn, event_id)
            conn.close()

        self.assertIn("STARTED", transitions)
        self.assertIn("ENDED", transitions)
        self.assertIsNotNone(report["end_ts"])
        self.assertEqual(report["risk_level"], "DANGER")
        self.assertLessEqual(report["min_distance_m"], 0.6)
        self.assertIsNotNone(report["environment"])
        self.assertIsNotNone(report["environment"]["temperature_c"])
        self.assertIsNotNone(report["environment"]["humidity_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
