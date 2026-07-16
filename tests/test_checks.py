import unittest

from gantry.checks import _extract_code_spans, _matches_any, _strip_fenced_code_blocks


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
