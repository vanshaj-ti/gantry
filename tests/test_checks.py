import unittest

from gantry.checks import _matches_any, _strip_fenced_code_blocks


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
