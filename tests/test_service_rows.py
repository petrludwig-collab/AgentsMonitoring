"""Module rows: a check that covers several things (websites, tunnel ends) may print
``rows`` and each row becomes its own line in the card with its own uptime history (2026-09-26).

Contract: ``rows`` is optional; a row needs ``name`` and ``up``; ``latency_ms``, ``detail`` and
``url`` are optional; junk rows are skipped. Rows are recorded as ``"<service> › <row>"`` samples,
their order and urls survive in ``service_rows.json``, and the card state stays the module's
overall ``up``.
"""
import json
import os
import tempfile
import unittest

os.environ.setdefault("AGENTSMON_STATE", tempfile.mkdtemp(prefix="agentsmon-test-state-"))
from agentsmon import dashboard, db, probe   # noqa: E402


ROWS_CMD = ("echo '" + json.dumps({"up": False, "latency_ms": 7, "detail": "prod up · staging DOWN", "rows": [
    {"name": "prod", "url": "http://127.0.0.1:8790/", "up": True, "latency_ms": 7, "detail": "200"},
    {"name": "staging", "url": "http://127.0.0.1:8791/", "up": False, "detail": "connection refused"},
    {"name": "junk without up"}, "not a dict"]}) + "'")


class RowsParsing(unittest.TestCase):
    def test_invalid_legacy_metrics_cannot_poison_dashboard_json_or_fake_health(self):
        for payload in ({'up':'false'},{'up':True,'latency_ms':'nan'},{'up':True,'latency_ms':-1}):
            ok,lat,detail,rows=probe._run("echo '"+json.dumps(payload)+"'")
            self.assertFalse(ok);self.assertIsNone(lat);self.assertIn('Invalid',detail)
        rows=probe._rows_from({'rows':[{'name':'bad','up':'false'},
                                      {'name':'safe','up':True,'latency_ms':'nan'}]})
        self.assertEqual([r['name'] for r in rows],['safe']);self.assertIsNone(rows[0]['latency'])

    def test_rows_are_parsed_and_junk_skipped(self):
        ok, lat, detail, rows = probe._run(ROWS_CMD)
        self.assertFalse(ok); self.assertEqual(lat, 0.007); self.assertEqual(detail, "prod up · staging DOWN")
        self.assertEqual([r["name"] for r in rows], ["prod", "staging"])
        self.assertEqual(rows[0], {"name": "prod", "up": True, "latency": 0.007, "detail": "200", "url": "http://127.0.0.1:8790/"})
        self.assertIsNone(rows[1]["latency"]); self.assertFalse(rows[1]["up"])

    def test_old_three_tuple_helper_and_modules_without_rows_keep_working(self):
        self.assertEqual(len(probe._command("echo fine")), 3)
        ok, lat, detail, rows = probe._run('echo \'{"up": true, "latency_ms": 12}\'')
        self.assertTrue(ok); self.assertEqual(rows, [])
        ok, lat, detail, rows = probe._run("sleep 5", timeout=0.3)
        self.assertFalse(ok); self.assertEqual(rows, [])


class RowsRecordedAndShown(unittest.TestCase):
    def test_probe_records_each_row_and_the_card_lists_them(self):
        cfg = {"services": [{"name": "Websites", "command": ROWS_CMD, "timeout_seconds": 5}],
               "probe": {"sla_window_days": 1, "timeline_days": 1, "min_outage_samples": 3}}
        probe.probe_once(cfg)
        self.assertEqual(db.last("Websites")["up"], 0, "card state = module's overall up")
        self.assertEqual(db.last("Websites › prod")["up"], 1)
        self.assertEqual(db.last("Websites › staging")["up"], 0)
        self.assertEqual(db.last("Websites › staging")["detail"], "connection refused")
        self.assertEqual([r["name"] for r in probe.service_rows("Websites")], ["prod", "staging"])
        card = [s for s in dashboard._service_state(cfg) if s["name"] == "Websites"][0]
        self.assertEqual(card["state"], "outage")
        rows = card["rows"]
        self.assertEqual([(r["name"], r["state"]) for r in rows], [("prod", "operational"), ("staging", "outage")])
        self.assertEqual(rows[0]["latency_ms"], 7); self.assertEqual(rows[0]["url"], "http://127.0.0.1:8790/")
        self.assertIsNotNone(rows[0]["sla"])

    def test_a_module_without_rows_has_no_table(self):
        cfg = {"services": [{"name": "Plain", "command": "echo ok"}],
               "probe": {"sla_window_days": 1, "timeline_days": 1}}
        probe.probe_once(cfg)
        card = [s for s in dashboard._service_state(cfg) if s["name"] == "Plain"][0]
        self.assertEqual(card["rows"], [])

    def test_page_has_the_rows_table_and_renderer(self):
        self.assertIn('class="svc-rows-wrap', dashboard.PAGE)
        self.assertIn("function renderRows(root, rows, details=false)", dashboard.PAGE)
        self.assertIn("renderRows(root, s.rows||[], s.row_details===true)", dashboard.PAGE)
        # Responsive layout: tables scroll horizontally instead of a fixed 680px min-width.
        self.assertGreaterEqual(dashboard.PAGE.count("overflow-x-auto"), 2)
        self.assertIn("agents-table", dashboard.PAGE)
        self.assertIn("svc-rows", dashboard.PAGE)


if __name__ == "__main__":
    unittest.main()
