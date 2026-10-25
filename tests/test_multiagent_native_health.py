"""Native multiagent health: disabled legacy bridges are N/A, not outages."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentsmon import keepalive, probe


class NativeMultiagentHealth(unittest.TestCase):
    def setUp(self):
        p = keepalive.heartbeat_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(int(time.time())), encoding="utf-8")

    def test_disabled_legacy_bridge_does_not_mark_system_down(self):
        cfg = {
            "agents": [],
            "daemons": [{
                "name": "Telegram Bridge: LANA",
                "enabled": False,
                "bridge_config": "/tmp/does-not-matter.json",
                "pattern": "agent2telegram-never-match-zzz",
            }],
            "pinned_daemons": [],
            "services": [],
            "probe": {"interval_seconds": 60},
            "keepalive": {"enabled": True, "interval_seconds": 60},
        }
        with patch("agentsmon.bridges.running_pids", return_value=[]):
            up, _, detail = probe._system_health(cfg)
        self.assertTrue(up, detail)
        self.assertNotIn("Telegram Bridge: LANA", detail)

    def test_enabled_missing_bridge_still_marks_down(self):
        cfg = {
            "agents": [],
            "daemons": [{
                "name": "Telegram Bridge: Cursor",
                "enabled": True,
                "bridge_config": "/tmp/x.json",
                "pattern": "agent2telegram-never-match-zzz",
            }],
            "pinned_daemons": [],
            "services": [],
            "probe": {"interval_seconds": 60},
            "keepalive": {"enabled": True, "interval_seconds": 60},
        }
        with patch("agentsmon.bridges.running_pids", return_value=[]):
            up, _, detail = probe._system_health(cfg)
        self.assertFalse(up)
        self.assertIn("Telegram Bridge: Cursor", detail)

    def test_detail_messages_are_czech(self):
        cfg = {
            "agents": [], "daemons": [], "pinned_daemons": [], "services": [],
            "probe": {"interval_seconds": 60},
            "keepalive": {"enabled": True, "interval_seconds": 60},
        }
        up, _, detail = probe._system_health(cfg)
        self.assertTrue(up)
        self.assertIn("komponent", detail.lower())


class DashboardCzechServiceLabels(unittest.TestCase):
    def test_page_maps_multiagent_heading_and_compact_gauges(self):
        from agentsmon import dashboard
        page = dashboard.PAGE
        self.assertIn("Dostupnost multiagentního systému", page)
        self.assertIn("serviceDisplayName", page)
        self.assertIn("renderAgentGauge", page)
        self.assertIn('max-width:min(1480px,100%)', page)
        self.assertIn("quota-tip", page)
        self.assertIn('aria-describedby', page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
