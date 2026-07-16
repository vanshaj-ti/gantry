import tempfile
import unittest
from pathlib import Path

from gantry.checks import (
    _allowed_paths,
    _extract_code_spans,
    _matches_any,
    _scope_additions_section,
    _strip_fenced_code_blocks,
    run_scope_guard,
)
from gantry.config import ScopeConfig
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


class TestMatchesAny(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(_matches_any(".env", [".env", "**/*.pem"]))

    def test_prefix_directory_match(self):
        self.assertTrue(_matches_any("secrets/prod.json", ["**/secrets/**", "secrets/"]))

    def test_glob_match(self):
        self.assertTrue(_matches_any("keys/server.pem", ["**/*.pem"]))

    def test_no_match(self):
        self.assertFalse(_matches_any("apps/core/src/main.ts", [".env", "**/*.pem"]))


if __name__ == "__main__":
    unittest.main()
