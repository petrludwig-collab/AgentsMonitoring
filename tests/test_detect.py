import unittest

from agentsmon.detect import _choose_claude_model, _claude_model_from_cmds


class ClaudeModelFromCmdsTest(unittest.TestCase):
    def test_ignores_non_claude_child_model_arguments(self):
        cmds = [
            "node helper.js --model claude-haiku-3-5",
            "claude --model claude-opus-4-8 --dangerously-skip-permissions",
        ]

        self.assertEqual(_claude_model_from_cmds(cmds), "Opus 4.8")

    def test_accepts_common_claude_model_aliases(self):
        self.assertEqual(_claude_model_from_cmds(["claude --model opus"]), "Opus")
        self.assertEqual(_claude_model_from_cmds(["claude --model=sonnet"]), "Sonnet")

    def test_cli_model_family_beats_potentially_ambiguous_cwd_transcript(self):
        self.assertEqual(_choose_claude_model("Opus", "Sonnet 5"), "Opus")
        self.assertEqual(_choose_claude_model("Sonnet", "Opus 4.8"), "Sonnet")

    def test_full_cli_model_beats_stale_transcript(self):
        self.assertEqual(_choose_claude_model("Fable 5", "Opus 4.8"), "Fable 5")


if __name__ == "__main__":
    unittest.main()
