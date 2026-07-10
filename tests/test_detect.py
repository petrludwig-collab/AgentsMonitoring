import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentsmon import detect, wizard


class HermesModelDetectionTests(unittest.TestCase):
    def test_hermes_daemon_uses_hermes_config_instead_of_codex_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".hermes").mkdir()
            (home / ".hermes" / "config.yaml").write_text(
                "model:\n  default: gpt-5.6-sol\n  provider: openai-codex\n",
                encoding="utf-8",
            )
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                'model = "gpt-5.5"\n', encoding="utf-8"
            )

            with patch.object(detect.Path, "home", return_value=home):
                self.assertEqual(detect.daemon_model("Hermes"), "GPT-5.6-SOL")

    def test_wizard_does_not_freeze_auto_detected_daemon_model_in_config(self):
        daemon = {
            "name": "Hermes",
            "pattern": "hermes gateway run",
            "health_url": "http://127.0.0.1:8642/health",
        }

        _, pinned = wizard._daemon_entries(daemon)

        self.assertNotIn("tag", pinned)
        self.assertNotIn("vendor", pinned)


if __name__ == "__main__":
    unittest.main()
