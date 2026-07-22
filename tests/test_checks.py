import tempfile
import unittest
from pathlib import Path

import json

from gantry.checks import (
    _allowed_paths,
    _extract_code_spans,
    _matches_any,
    _scope_additions_section,
    _strip_fenced_code_blocks,
    check_spec_artifacts,
    run_repo_checks,
    run_scope_guard,
)
from gantry.config import ChecksConfig, ScopeConfig
from gantry.state import RunStore


class TestStripFencedCodeBlocks(unittest.TestCase):
    def test_removes_fenced_block_entirely(self):
        text = "before\n```ts\nconst x = `a`;\n```\nafter `path/to/file.ts` mention"
        stripped = _strip_fenced_code_blocks(text)
        self.assertNotIn("const x", stripped)
        self.assertIn("path/to/file.ts", stripped)

    def test_single_backtick_inside_fence_does_not_swallow_later_path(self):
        # Regression: a stray backtick inside a fenced snippet (e.g. an
        # apostrophe-adjacent backtick in embedded SQL/TS) must not pair
        # across the fence boundary with a real single-line path mention.
        text = (
            "```sql\n"
            "SELECT * FROM t WHERE name = `it's a test`\n"
            "```\n"
            "Allowed: `apps/core/src/main.ts`\n"
        )
        stripped = _strip_fenced_code_blocks(text)
        self.assertIn("apps/core/src/main.ts", stripped)

    def test_no_fences_leaves_text_unchanged(self):
        text = "Allowed: `apps/core/src/main.ts` and `apps/admin/src/App.tsx`"
        self.assertEqual(_strip_fenced_code_blocks(text), text)

    def test_multiple_fenced_blocks_all_removed(self):
        text = "```py\nx = 1\n```\nmiddle `real/path.py`\n```py\ny = 2\n```"
        stripped = _strip_fenced_code_blocks(text)
        self.assertNotIn("x = 1", stripped)
        self.assertNotIn("y = 2", stripped)
        self.assertIn("real/path.py", stripped)

    def test_prose_and_path_between_two_independent_fences_is_kept(self):
        # Regression: a real production plan had a fence pair close (e.g.
        # ```ts ... ```) and, well after it, a wholly separate fence pair
        # open (```sql ... ```). A naive non-greedy `.*?` DOTALL regex pairs
        # the closing ``` of the FIRST block with the OPENING ``` of the
        # SECOND, treating everything between two independent fenced blocks
        # (real prose, including a `path/to/module.ts`-style backtick-quoted
        # file path) as if it were itself inside a fence, and deletes it.
        # This caused a real scope-guard false positive: a plan section
        # headed with a backtick-quoted file path sitting between two
        # unrelated fences elsewhere in the doc was silently dropped from
        # the allowlist, and the scope guard then flagged that legitimately
        # planned file as "unexpected".
        text = (
            "```ts\n"
            "someConfigField: string | null;\n"
            "```\n"
            "\n"
            "### C1. `path/to/module.ts`\n"
            "\n"
            "Add to `initSchema`'s `db.exec`, after the `existingTable` table.\n"
            "\n"
            "```sql\n"
            "CREATE TABLE IF NOT EXISTS example_table (...);\n"
            "```\n"
        )
        stripped = _strip_fenced_code_blocks(text)
        self.assertIn("path/to/module.ts", stripped)
        self.assertNotIn("someConfigField", stripped)
        self.assertNotIn("CREATE TABLE", stripped)


class TestExtractCodeSpans(unittest.TestCase):
    def test_simple_span(self):
        self.assertEqual(_extract_code_spans("see `src/foo.ts` here"), ["src/foo.ts"])

    def test_nested_template_literal_does_not_desync_later_paths(self):
        # Regression: a JS template literal (itself backtick-delimited)
        # written inside a single-backtick markdown span produces four
        # backticks in a row: 1, 1, 2 run lengths in sequence. Naive
        # first-backtick/next-backtick pairing (re.findall(r"`([^`]+)`"))
        # pairs across run-length boundaries and desyncs every subsequent
        # path mention in the document. Run-length-aware matching must
        # keep every span after it independent.
        text = (
            "- `url = `${config.embeddingsBaseUrl}${config.embeddingsPath}``.\n"
            "\n"
            "Also touches `src/db/schema.ts` and `src/extract/recurrence.ts`.\n"
        )
        spans = _extract_code_spans(text)
        self.assertIn("src/db/schema.ts", spans)
        self.assertIn("src/extract/recurrence.ts", spans)

    def test_unmatched_run_is_skipped_not_swallowing(self):
        # The stray `` (length 2) has no same-length partner anywhere in
        # the text, so per CommonMark it's literal and must be skipped
        # rather than pairing with an unrelated single backtick — the
        # real `src/real.ts` span (length-1 backticks) must still resolve.
        text = "stray `` unmatched marker. Real: `src/real.ts` done."
        spans = _extract_code_spans(text)
        self.assertIn("src/real.ts", spans)


class TestScopeAdditionsSection(unittest.TestCase):
    def test_extracts_section_body_up_to_next_heading(self):
        text = (
            "# Build summary\n\nDid stuff.\n\n"
            "## Scope additions\n\n"
            "- `src/fixtures/mock.json` — needed by new parser test\n\n"
            "## Tests run\n\nAll green.\n"
        )
        section = _scope_additions_section(text)
        self.assertIn("src/fixtures/mock.json", section)
        self.assertNotIn("Tests run", section)
        self.assertNotIn("All green", section)

    def test_no_section_returns_empty(self):
        text = "# Build summary\n\nNo additions here.\n"
        self.assertEqual(_scope_additions_section(text), "")

    def test_section_at_end_of_document(self):
        text = "# Build summary\n\n## Scope additions\n\n- `src/new.ts` — reason\n"
        section = _scope_additions_section(text)
        self.assertIn("src/new.ts", section)


class TestAllowedPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name))
        self.run_id = self.store.create("test-run", "Test run")

    def tearDown(self):
        self._tmp.cleanup()

    def test_unions_plan_and_build_summary_additions(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self.store.artifact_path(self.run_id, "build-summary.md").write_text(
            "# Build summary\n\n## Scope additions\n\n"
            "- `src/discovered.ts` — needed by new fixture\n")
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertIn("src/planned.ts", allowed)
        self.assertIn("src/discovered.ts", allowed)

    def test_no_build_summary_falls_back_to_plan_only(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertEqual(allowed, ["src/planned.ts"])

    def test_build_summary_without_additions_section_adds_nothing(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self.store.artifact_path(self.run_id, "build-summary.md").write_text(
            "# Build summary\n\nDid the plan, nothing extra.\n")
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertEqual(allowed, ["src/planned.ts"])

    def test_valid_allowed_files_json_used_instead_of_plan_prose(self):
        # Prose is malformed/different on purpose — if allowed-files.json is
        # honored, the prose must never even be consulted.
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "This prose mentions no backtick paths at all, and even if it did "
            "they'd be irrelevant here.\n")
        self.store.write_result(self.run_id, "allowed-files.json", {
            "allowed_globs": ["src/json_declared.ts", "docs/**/*.md"],
            "notes": {"src/json_declared.ts": "structured scope"},
        })
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertEqual(allowed, ["src/json_declared.ts", "docs/**/*.md"])

    def test_missing_allowed_files_json_falls_back_to_prose(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertEqual(allowed, ["src/planned.ts"])

    def test_malformed_allowed_files_json_falls_back_to_prose(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        # Not valid JSON at all.
        path = self.store.run_dir(self.run_id) / "allowed-files.json"
        path.write_text("not json{{{")
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertEqual(allowed, ["src/planned.ts"])

    def test_allowed_files_json_missing_key_falls_back_to_prose(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self.store.write_result(self.run_id, "allowed-files.json", {"notes": {}})
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertEqual(allowed, ["src/planned.ts"])

    def test_allowed_files_json_empty_list_falls_back_to_prose(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self.store.write_result(self.run_id, "allowed-files.json", {"allowed_globs": []})
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertEqual(allowed, ["src/planned.ts"])

    def test_valid_allowed_files_json_still_unions_build_declared_additions(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "irrelevant prose\n")
        self.store.write_result(self.run_id, "allowed-files.json", {
            "allowed_globs": ["src/json_declared.ts"],
        })
        self.store.artifact_path(self.run_id, "build-summary.md").write_text(
            "# Build summary\n\n## Scope additions\n\n"
            "- `src/discovered.ts` — needed by new fixture\n")
        allowed = _allowed_paths(self.store, self.run_id)
        self.assertIn("src/json_declared.ts", allowed)
        self.assertIn("src/discovered.ts", allowed)


class TestCheckSpecArtifacts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name))
        self.run_id = self.store.create("test-run", "Test run")

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_fails(self):
        result = check_spec_artifacts(self.store, self.run_id)
        self.assertFalse(result["pass"])
        self.assertIn("missing", result["reason"])

    def test_invalid_json_fails(self):
        self.store.artifact_path(self.run_id, "acceptance-criteria.json").write_text(
            "not valid json {{{")
        result = check_spec_artifacts(self.store, self.run_id)
        self.assertFalse(result["pass"])

    def test_not_a_json_object_fails(self):
        self.store.artifact_path(self.run_id, "acceptance-criteria.json").write_text(
            json.dumps(["AC-1"]))
        result = check_spec_artifacts(self.store, self.run_id)
        self.assertFalse(result["pass"])

    def test_missing_criteria_key_fails(self):
        self.store.artifact_path(self.run_id, "acceptance-criteria.json").write_text(
            json.dumps({"not_criteria": []}))
        result = check_spec_artifacts(self.store, self.run_id)
        self.assertFalse(result["pass"])

    def test_empty_criteria_list_fails(self):
        self.store.artifact_path(self.run_id, "acceptance-criteria.json").write_text(
            json.dumps({"criteria": []}))
        result = check_spec_artifacts(self.store, self.run_id)
        self.assertFalse(result["pass"])

    def test_valid_criteria_passes(self):
        self.store.artifact_path(self.run_id, "acceptance-criteria.json").write_text(
            json.dumps({"criteria": [
                {"id": "AC-1", "text": "Does the thing", "verifiable_by": "test"},
                {"id": "AC-2", "text": "Doesn't break the other thing", "verifiable_by": "manual"},
            ]}))
        result = check_spec_artifacts(self.store, self.run_id)
        self.assertTrue(result["pass"])
        self.assertEqual(result["criteria_count"], 2)


class TestRunScopeGuardModes(unittest.TestCase):
    """End-to-end: git repo + a real diff, exercising mode/require_declared_additions."""

    def setUp(self):
        import subprocess
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(self.repo), check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "planned.ts").write_text("planned\n")
        subprocess.run(["git", "add", "-A"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(self.repo), check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=str(self.repo), check=True)
        self.store = RunStore(self.repo)
        self.run_id = self.store.create("test-run", "Test run")

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, rel_path: str, content: str = "x\n") -> None:
        import subprocess
        p = self.repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        # `git diff <base> --` (what _changed_files uses) doesn't show
        # untracked files unless staged.
        subprocess.run(["git", "add", rel_path], cwd=str(self.repo), check=True)

    def test_declared_addition_passes_in_block_mode(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self.store.artifact_path(self.run_id, "build-summary.md").write_text(
            "## Scope additions\n\n- `src/discovered.ts` — needed it\n")
        self._touch("src/discovered.ts")
        result = run_scope_guard(self.store, self.run_id, ScopeConfig(), self.repo, "main")
        self.assertTrue(result["pass"])
        self.assertEqual(result["unexpected_files"], [])

    def test_undeclared_new_file_fails_in_block_mode_default(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self._touch("src/surprise.ts")
        result = run_scope_guard(self.store, self.run_id, ScopeConfig(), self.repo, "main")
        self.assertFalse(result["pass"])
        self.assertIn("src/surprise.ts", result["unexpected_files"])

    def test_undeclared_new_file_warns_but_passes_in_warn_mode(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self._touch("src/surprise.ts")
        cfg = ScopeConfig(mode="warn")
        result = run_scope_guard(self.store, self.run_id, cfg, self.repo, "main")
        self.assertTrue(result["pass"])
        self.assertEqual(result["unexpected_files"], [])
        self.assertTrue(any("src/surprise.ts" in w for w in result["warnings"]))

    def test_mode_off_disables_plan_scope_entirely(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self._touch("src/surprise.ts")
        cfg = ScopeConfig(mode="off")
        result = run_scope_guard(self.store, self.run_id, cfg, self.repo, "main")
        self.assertTrue(result["pass"])
        self.assertEqual(result["unexpected_files"], [])
        self.assertEqual(result["warnings"], [])

    def test_require_declared_additions_false_warns_without_declaration(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self._touch("src/surprise.ts")
        cfg = ScopeConfig(mode="warn", require_declared_additions=False)
        result = run_scope_guard(self.store, self.run_id, cfg, self.repo, "main")
        self.assertTrue(result["pass"])
        self.assertTrue(any("src/surprise.ts" in w for w in result["warnings"]))

    def test_forbid_paths_still_blocks_regardless_of_mode(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        self._touch(".env", "SECRET=1\n")
        cfg = ScopeConfig(mode="off")
        result = run_scope_guard(self.store, self.run_id, cfg, self.repo, "main")
        self.assertFalse(result["pass"])
        self.assertIn(".env", result["forbidden_files"])

    def test_high_risk_files_recorded_without_affecting_pass(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`, `auth/login.ts`.\n")
        self._touch("auth/login.ts")
        cfg = ScopeConfig(high_risk_paths=["auth/"])
        result = run_scope_guard(self.store, self.run_id, cfg, self.repo, "main")
        self.assertIn("auth/login.ts", result["high_risk_files"])
        self.assertTrue(result["pass"])

    def test_no_high_risk_match_leaves_high_risk_files_empty(self):
        self.store.artifact_path(self.run_id, "implementation-plan.md").write_text(
            "Touch `src/planned.ts`.\n")
        cfg = ScopeConfig(high_risk_paths=["auth/"])
        result = run_scope_guard(self.store, self.run_id, cfg, self.repo, "main")
        self.assertEqual(result["high_risk_files"], [])
        self.assertTrue(result["pass"])


class TestMatchesAny(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(_matches_any(".env", [".env", "**/*.pem"]))

    def test_prefix_directory_match(self):
        self.assertTrue(_matches_any("secrets/prod.json", ["**/secrets/**", "secrets/"]))

    def test_glob_match(self):
        self.assertTrue(_matches_any("keys/server.pem", ["**/*.pem"]))

    def test_no_match(self):
        self.assertFalse(_matches_any("apps/core/src/main.ts", [".env", "**/*.pem"]))

    def test_double_star_matches_nested_path(self):
        # Already-working case: fnmatch's regex conversion of "**/auth/**"
        # requires a literal "/auth/" substring, which a nested path has.
        self.assertTrue(_matches_any("apps/auth/x.ts", ["**/auth/**"]))

    def test_double_star_matches_top_level_path(self):
        # Previously-broken case: "auth/login.ts" has no leading "/" before
        # "auth", so fnmatch.fnmatch("auth/login.ts", "**/auth/**") failed —
        # the fix strips the leading "**/" and retries.
        self.assertTrue(_matches_any("auth/login.ts", ["**/auth/**"]))

    def test_double_star_general_pattern_top_level(self):
        # Not a one-off special case for exactly "**/auth/**" — any
        # "**/foo/**"-shaped pattern gets the same treatment.
        self.assertTrue(_matches_any("migrations/0001_init.sql", ["**/migrations/**"]))


class TestRunRepoChecks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bare_string_commands_run_serially_preserve_order(self):
        cfg = ChecksConfig(commands=["echo a", "echo b", "false"])
        result = run_repo_checks(cfg, self.cwd)
        self.assertFalse(result["pass"])
        self.assertEqual([r["command"] for r in result["results"]], ["echo a", "echo b", "false"])
        self.assertTrue(result["results"][0]["pass"])
        self.assertTrue(result["results"][1]["pass"])
        self.assertFalse(result["results"][2]["pass"])

    def test_table_commands_with_parallel_flag_all_run_and_preserve_order(self):
        cfg = ChecksConfig(commands=[
            {"command": "echo first", "parallel": True},
            {"command": "echo second", "parallel": True},
            "echo third",
        ])
        result = run_repo_checks(cfg, self.cwd)
        self.assertTrue(result["pass"])
        self.assertEqual([r["command"] for r in result["results"]],
                        ["echo first", "echo second", "echo third"])

    def test_per_command_timeout_overrides_default(self):
        cfg = ChecksConfig(commands=[{"command": "sleep 5", "timeout": 1}], timeout=900)
        with self.assertRaises(Exception):
            run_repo_checks(cfg, self.cwd)

    def test_duplicate_command_strings_both_run_independently(self):
        cfg = ChecksConfig(commands=[
            {"command": "true", "parallel": True},
            {"command": "true", "parallel": True},
        ])
        result = run_repo_checks(cfg, self.cwd)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all(r["pass"] for r in result["results"]))

    def test_max_parallel_bounds_concurrency_still_runs_all(self):
        cfg = ChecksConfig(
            commands=[{"command": "echo x", "parallel": True} for _ in range(6)],
            max_parallel=2,
        )
        result = run_repo_checks(cfg, self.cwd)
        self.assertEqual(len(result["results"]), 6)
        self.assertTrue(all(r["pass"] for r in result["results"]))

    def test_empty_commands_passes(self):
        cfg = ChecksConfig(commands=[])
        result = run_repo_checks(cfg, self.cwd)
        self.assertTrue(result["pass"])
        self.assertEqual(result["results"], [])


class TestFlakyRetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self._target_tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._target_tmp.name))

    def tearDown(self):
        self._tmp.cleanup()
        self._target_tmp.cleanup()

    def test_flaky_retry_attempts_zero_is_byte_identical_to_today(self):
        # A failing command still fails immediately with zero retry attempts
        # (the default) — no marker keys, no flake log entry.
        cfg = ChecksConfig(commands=["false"], flaky_retry_attempts=0)
        result = run_repo_checks(cfg, self.cwd, store=self.store, run_id="run-1")
        self.assertFalse(result["pass"])
        self.assertFalse(result["results"][0]["pass"])
        self.assertNotIn("flaky", result["results"][0])
        self.assertEqual(self.store.read_flake_log(), [])

    def test_command_that_fails_once_then_passes_on_bare_retry_is_treated_as_passed(self):
        marker = self.cwd / "attempt-marker"
        # Fails on the very first invocation, passes every time after —
        # simulates a command that fails once then passes on retry.
        cmd = (
            f'if [ -f "{marker}" ]; then exit 0; else touch "{marker}"; exit 1; fi'
        )
        cfg = ChecksConfig(commands=[cmd], flaky_retry_attempts=2)
        result = run_repo_checks(cfg, self.cwd, store=self.store, run_id="run-2")
        self.assertTrue(result["pass"])
        self.assertTrue(result["results"][0]["pass"])
        self.assertTrue(result["results"][0]["flaky"])
        self.assertEqual(result["results"][0]["attempts_before_pass"], 1)
        flake_log = self.store.read_flake_log()
        self.assertEqual(len(flake_log), 1)
        self.assertEqual(flake_log[0]["command"], cmd)
        self.assertEqual(flake_log[0]["run_id"], "run-2")
        self.assertEqual(flake_log[0]["attempts_before_pass"], 1)

    def test_command_that_fails_every_retry_still_escalates_like_today(self):
        cfg = ChecksConfig(commands=["false"], flaky_retry_attempts=3)
        result = run_repo_checks(cfg, self.cwd, store=self.store, run_id="run-3")
        self.assertFalse(result["pass"])
        self.assertFalse(result["results"][0]["pass"])
        self.assertNotIn("flaky", result["results"][0])
        # A fully-failing command must NOT be silently marked passed, and
        # must not write a flake record either (it never actually flaked).
        self.assertEqual(self.store.read_flake_log(), [])

    def test_flake_log_accumulates_and_respects_cap(self):
        for i in range(RunStore.FLAKE_LOG_MAX_ENTRIES + 10):
            self.store.record_flake(f"cmd-{i}", f"run-{i}", 1)
        log = self.store.read_flake_log()
        self.assertEqual(len(log), RunStore.FLAKE_LOG_MAX_ENTRIES)
        # Oldest entries are pruned first, newest kept.
        self.assertEqual(log[-1]["command"], f"cmd-{RunStore.FLAKE_LOG_MAX_ENTRIES + 9}")

    def test_flake_log_prunes_entries_older_than_30_days(self):
        from datetime import datetime, timedelta, timezone

        path = self.store._flake_log_path()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.store._write(path, [{"command": "old", "run_id": "run-old",
                                  "timestamp": old_ts, "attempts_before_pass": 1}])
        self.store.record_flake("new", "run-new", 1)
        log = self.store.read_flake_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["command"], "new")


if __name__ == "__main__":
    unittest.main()
