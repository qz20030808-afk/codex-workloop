import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from demo import DAY_ONE, DAY_TWO, MIDNIGHT, finish, scheduled_report, seed
from workloop import Workloop, parse_time


class WorkloopTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_cross_day_replay_uses_frozen_reports_and_actual_artifacts(self):
        self.assertTrue(seed(self.root)["completion_blocked"])
        self.assertEqual(len(scheduled_report(self.root)["reports"]), 2)
        result = finish(self.root)
        self.assertEqual(result["handoff"]["pending"], ["result"])
        self.assertEqual(result["handoff"]["from_report"], "daily-2026-11-22")
        self.assertEqual(len(result["receipt"]["evidence"]), 2)
        files = self.root / "reports"
        sunday = (files / "weekly-2026-11-22.md").read_text()
        monday = (files / "daily-2026-11-23.md").read_text()
        self.assertIn("result: Generate and verify", sunday)
        self.assertNotIn("Completed with receipt", sunday)
        self.assertIn(result["receipt"]["sha256"], monday)
        self.assertEqual((self.root / "artifacts/reorder.json").read_text(), '["PAPER"]\n')

    def test_midnight_closes_sunday_and_replays_are_idempotent(self):
        seed(self.root)
        with Workloop(self.root) as loop:
            self.assertEqual(loop.report(MIDNIGHT - timedelta(seconds=1)), [])
            self.assertTrue(all(r["new"] for r in loop.report(MIDNIGHT)))
            self.assertTrue(all(not r["new"] for r in loop.report(MIDNIGHT)))

    def test_resume_requires_the_frozen_daily_report(self):
        seed(self.root)
        with Workloop(self.root) as loop:
            with self.assertRaisesRegex(ValueError, "Generate a closed-day report"):
                loop.resume(DAY_TWO)
        scheduled_report(self.root)
        report = self.root / "reports/daily-2026-11-22.md"
        report.write_text("User changed the report.")
        with Workloop(self.root) as loop:
            with self.assertRaisesRegex(ValueError, "matching report"):
                loop.resume(DAY_TWO)
            with self.assertRaisesRegex(ValueError, "differs"):
                loop.report(MIDNIGHT)
        self.assertEqual(report.read_text(), "User changed the report.")

    def test_repeated_resume_does_not_duplicate_the_event(self):
        seed(self.root)
        scheduled_report(self.root)
        with Workloop(self.root) as loop:
            self.assertEqual(loop.resume(DAY_TWO), loop.resume(DAY_TWO))
            self.assertEqual(sum(e["kind"] == "resumed" for e in loop.events()), 1)

    def test_completion_rechecks_evidence_digest(self):
        seed(self.root)
        scheduled_report(self.root)
        with Workloop(self.root) as loop:
            output = self.root / "artifacts/reorder.json"
            output.write_text('["PAPER"]')
            loop.accept("result", "reorder.json", "Verified synthetic result.", DAY_TWO)
            output.write_text("tampered")
            with self.assertRaisesRegex(ValueError, "Accepted artifact changed"):
                loop.complete(DAY_TWO)
            self.assertIsNone(loop.snapshot(loop.events())["receipt"])

    def test_committed_week_cannot_be_changed_by_a_late_event(self):
        seed(self.root)
        scheduled_report(self.root)
        with Workloop(self.root) as loop:
            with self.assertRaisesRegex(ValueError, "committed report"):
                loop.accept("result", "inventory.csv", "Attempted backdate.", DAY_ONE)

    def test_cannot_register_artifact_outside_state_directory(self):
        seed(self.root)
        outside = self.root / "outside.txt"
        outside.write_text("outside")
        with Workloop(self.root) as loop:
            with self.assertRaisesRegex(ValueError, "outside"):
                loop.accept("result", "../outside.txt", "No scope.", DAY_TWO)

    def test_retry_recovers_report_from_committed_snapshot(self):
        seed(self.root)
        scheduled_report(self.root)
        report = self.root / "reports/weekly-2026-11-22.md"
        before = report.read_bytes()
        report.unlink()
        with Workloop(self.root) as loop:
            self.assertTrue(all(not r["new"] for r in loop.report(MIDNIGHT)))
        self.assertEqual(report.read_bytes(), before)

    def test_timestamps_must_include_an_offset(self):
        with self.assertRaisesRegex(ValueError, "time zone offset"):
            parse_time("2026-11-23T00:00:00")


if __name__ == "__main__":
    unittest.main()
