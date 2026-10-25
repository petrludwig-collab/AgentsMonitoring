"""Czech UX + truthful quota gauge semantics for AGENTSMON-UX-CZ-001."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentsmon import dashboard, plugins


class PluginCellGaugeMeta(unittest.TestCase):
    def test_preserves_remaining_state_reset_and_null_remaining(self):
        cell = plugins.cell({
            "text": "OK",
            "title": "detail",
            "tone": "ok",
            "bars": [{
                "label": "5h",
                "percent": 40,
                "remaining": 60,
                "reset_label": "14:00",
                "state": "OK",
                "title": "5h · zbývá 60 %",
            }],
        })
        bar = cell["bars"][0]
        self.assertEqual(bar["remaining"], 60)
        self.assertEqual(bar["reset_label"], "14:00")
        self.assertEqual(bar["state"], "OK")
        unknown = plugins.cell({
            "text": "UNAVAILABLE",
            "tone": "muted",
            "bars": [{"label": "5h", "percent": 0, "remaining": None, "state": "UNAVAILABLE", "title": "n/a"}],
        })
        self.assertIsNone(unknown["bars"][0]["remaining"])

    def test_rejects_remaining_over_100(self):
        with self.assertRaises(ValueError):
            plugins.cell({"text": "x", "bars": [{"label": "5h", "percent": 10, "remaining": 101}]})


class MergeHelpers(unittest.TestCase):
    def test_merge_columns_by_title(self):
        cols = [
            {"id": "codex-quota:five_hour", "title": "5h"},
            {"id": "provider-quotas:five_hour", "title": "5h"},
            {"id": "codex-quota:weekly", "title": "Týden"},
        ]
        merged = dashboard._merge_columns_by_title(cols)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["title"], "5h")
        self.assertEqual(merged[0]["aliases"],
                         ["codex-quota:five_hour", "provider-quotas:five_hour"])

    def test_merge_tables_by_title_combines_provider_rows(self):
        tables = [
            {"id": "codex-quota:providers", "title": "Kvóty poskytovatelů",
             "rows": [{"id": "a"}], "note": "Codex", "up": True, "error": ""},
            {"id": "provider-quotas:providers", "title": "Kvóty poskytovatelů",
             "rows": [{"id": "b"}], "note": "Cursor", "up": True, "error": ""},
        ]
        merged = dashboard._merge_tables_by_title(tables)
        self.assertEqual(len(merged), 1)
        self.assertEqual([r["id"] for r in merged[0]["rows"]], ["a", "b"])
        self.assertIn("Codex", merged[0]["note"])
        self.assertIn("Cursor", merged[0]["note"])


class DashboardCzechPage(unittest.TestCase):
    def test_page_is_czech_and_has_remaining_gauges(self):
        page = dashboard.PAGE
        self.assertIn('lang="cs"', page)
        self.assertIn("Persistentní agenti", page)
        self.assertIn("kvót poskytovatelů", page)
        self.assertIn("Monitorování agentů", page)
        self.assertIn("Pozastavit obnovu", page)
        self.assertIn("renderQuotaBar", page)
        self.assertIn('role="progressbar"', page)
        self.assertIn("LOCALE=\"cs-CZ\"", page)
        self.assertNotIn("Codex · dostupná kvóta", page)
        self.assertNotIn("Persistent Agents", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
