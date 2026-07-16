import tempfile
import unittest
from pathlib import Path

from gantry.cost import accumulate, extract_usage, report_for_run, total_all_runs
from gantry.state import RunStore


class TestExtractUsage(unittest.TestCase):
    def test_extracts_claude_code_shaped_fields(self):
        raw = {"total_cost_usd": 0.0123, "duration_ms": 4200,
               "usage": {"input_tokens": 100, "output_tokens": 50}}
        usage = extract_usage(raw)
        self.assertEqual(usage["cost_usd"], 0.0123)
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertEqual(usage["duration_ms"], 4200)

    def test_missing_fields_yield_none_not_zero(self):
        usage = extract_usage({"result": "done"})
        self.assertIsNone(usage["cost_usd"])
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])

    def test_non_dict_raw_does_not_raise(self):
        usage = extract_usage("not a dict")
        self.assertIsNone(usage["cost_usd"])

    def test_usage_block_not_a_dict_does_not_raise(self):
        usage = extract_usage({"total_cost_usd": 1.0, "usage": "garbage"})
        self.assertEqual(usage["cost_usd"], 1.0)
        self.assertIsNone(usage["input_tokens"])


class TestAccumulate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name))
        self.run_id = "test-run"
        self.store.create(self.run_id, "Test run")

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_call_initializes_report(self):
        usage = {"cost_usd": 0.5, "input_tokens": 1000, "output_tokens": 200, "duration_ms": 3000}
        report = accumulate(self.store, self.run_id, "build", usage)
        self.assertEqual(report["total_cost_usd"], 0.5)
        self.assertEqual(report["total_input_tokens"], 1000)
        self.assertEqual(report["total_output_tokens"], 200)
        self.assertEqual(report["by_stage"]["build"]["calls"], 1)

    def test_accumulates_across_multiple_stages(self):
        accumulate(self.store, self.run_id, "plan", {"cost_usd": 0.1, "input_tokens": 10,
                                                       "output_tokens": 5, "duration_ms": 100})
        report = accumulate(self.store, self.run_id, "build", {"cost_usd": 0.2, "input_tokens": 20,
                                                                "output_tokens": 10, "duration_ms": 200})
        self.assertAlmostEqual(report["total_cost_usd"], 0.3)
        self.assertEqual(report["total_input_tokens"], 30)
        self.assertIn("plan", report["by_stage"])
        self.assertIn("build", report["by_stage"])

    def test_same_stage_called_twice_accumulates_not_overwrites(self):
        accumulate(self.store, self.run_id, "build", {"cost_usd": 0.1, "input_tokens": 10,
                                                        "output_tokens": 5, "duration_ms": 100})
        report = accumulate(self.store, self.run_id, "build", {"cost_usd": 0.2, "input_tokens": 20,
                                                                "output_tokens": 10, "duration_ms": 100})
        self.assertAlmostEqual(report["by_stage"]["build"]["cost_usd"], 0.3)
        self.assertEqual(report["by_stage"]["build"]["calls"], 2)

    def test_none_usage_fields_do_not_corrupt_totals(self):
        usage = {"cost_usd": None, "input_tokens": None, "output_tokens": None, "duration_ms": None}
        report = accumulate(self.store, self.run_id, "resolve", usage)
        self.assertEqual(report["total_cost_usd"], 0.0)
        self.assertEqual(report["by_stage"]["resolve"]["calls"], 1)

    def test_mirrors_total_onto_state(self):
        accumulate(self.store, self.run_id, "build", {"cost_usd": 1.5, "input_tokens": 1,
                                                        "output_tokens": 1, "duration_ms": 1})
        self.assertEqual(self.store.state(self.run_id)["total_cost_usd"], 1.5)


class TestTotalAllRuns(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_sums_across_runs_and_ranks_top_runs(self):
        self.store.create("run-1", "Run one")
        self.store.create("run-2", "Run two")
        accumulate(self.store, "run-1", "build", {"cost_usd": 0.5, "input_tokens": 1,
                                                   "output_tokens": 1, "duration_ms": 1})
        accumulate(self.store, "run-2", "build", {"cost_usd": 2.0, "input_tokens": 1,
                                                   "output_tokens": 1, "duration_ms": 1})
        total = total_all_runs(self.store)
        self.assertAlmostEqual(total["total_cost_usd"], 2.5)
        self.assertEqual(total["top_runs"][0]["run_id"], "run-2")

    def test_no_runs_yields_zero_totals(self):
        total = total_all_runs(self.store)
        self.assertEqual(total["total_cost_usd"], 0.0)
        self.assertEqual(total["top_runs"], [])

    def test_report_for_run_defaults_when_no_cost_recorded(self):
        run_id = "run-x"
        self.store.create(run_id, "Run x")
        report = report_for_run(self.store, run_id)
        self.assertEqual(report["total_cost_usd"], 0.0)
        self.assertEqual(report["by_stage"], {})


if __name__ == "__main__":
    unittest.main()
