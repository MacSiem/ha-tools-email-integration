"""Pure rendering tests for server-side email composition."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
import importlib.util
from pathlib import Path


def _load_composer():
    path = Path(__file__).resolve().parents[1] / "custom_components/ha_tools_email/composer.py"
    spec = importlib.util.spec_from_file_location("ha_tools_email_composer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


composer = _load_composer()
build_energy_report_payload = composer.build_energy_report_payload
build_log_digest_payload = composer.build_log_digest_payload


class ComposerRenderingTest(unittest.TestCase):
    """Verify HTML output from deterministic sample data."""

    def test_log_digest_escapes_entries_and_counts_by_level_logger(self) -> None:
        entries = [
            {
                "level": "ERROR",
                "name": "custom_components.demo",
                "message": "<script>alert(1)</script>",
                "timestamp": 1_780_000_000,
                "count": 2,
            },
            {
                "level": "WARNING",
                "name": "homeassistant.helpers",
                "message": ["Bad value", "second line"],
                "timestamp": 1_780_000_100,
            },
        ]

        payload = build_log_digest_payload(
            entries,
            cadence="daily",
            now=datetime(2026, 6, 12, 7, 30, tzinfo=timezone.utc),
        )

        self.assertIn("HA Log Digest", payload["subject"])
        self.assertIn("custom_components.demo", payload["body"])
        self.assertIn("ERROR", payload["html"])
        self.assertIn("WARNING", payload["html"])
        self.assertIn("x2", payload["html"])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", payload["html"])
        self.assertNotIn("<script>alert(1)</script>", payload["html"])
        self.assertIn("Bad value second line", payload["html"])

    def test_energy_report_renders_totals_top_consumers_and_escapes_names(self) -> None:
        devices = [
            {"name": "Heat Pump", "kwh": 12.25, "entity_id": "sensor.heat_pump_energy"},
            {"name": "EV <charger>", "kwh": 3.5, "entity_id": "sensor.ev_energy"},
        ]

        payload = build_energy_report_payload(
            devices,
            cadence="weekly",
            now=datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc),
            currency="PLN",
            price_per_kwh=0.8,
        )

        self.assertIn("Weekly Energy Report", payload["subject"])
        self.assertIn("15.75 kWh", payload["body"])
        self.assertIn("Heat Pump", payload["html"])
        self.assertIn("EV &lt;charger&gt;", payload["html"])
        self.assertIn("12.60 PLN", payload["html"])
        self.assertNotIn("EV <charger>", payload["html"])


if __name__ == "__main__":
    unittest.main()
