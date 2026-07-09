import unittest
from unittest.mock import MagicMock, patch

from gantry.docnav import _next_state, _render_via_glow, NavState, LEVEL_RUNS, LEVEL_DOCS, LEVEL_CONTENT


RUNS = [
    {"id": "run1", "title": "Run 1", "status": "shipped", "mtime": 1},
    {"id": "run2", "title": "Run 2", "status": "blocked", "mtime": 2},
]
DOCS = [("Plan", "implementation-plan.md"), ("Evidence", "evidence-report.md")]


class TestRunListLevel(unittest.TestCase):
    def test_down_moves_selection(self):
        s = _next_state(NavState(), "KEY_DOWN", RUNS, [])
        self.assertEqual(s.run_selected, 1)

    def test_down_clamps_at_end(self):
        s = _next_state(NavState(run_selected=1), "KEY_DOWN", RUNS, [])
        self.assertEqual(s.run_selected, 1)

    def test_up_clamps_at_start(self):
        s = _next_state(NavState(), "KEY_UP", RUNS, [])
        self.assertEqual(s.run_selected, 0)

    def test_drill_in_sets_run_id_and_level(self):
        s = _next_state(NavState(run_selected=1), "KEY_RIGHT", RUNS, [])
        self.assertEqual(s.level, LEVEL_DOCS)
        self.assertEqual(s.run_id, "run2")

    def test_empty_runs_ignores_navigation(self):
        s = _next_state(NavState(), "KEY_DOWN", [], [])
        self.assertEqual(s.run_selected, 0)
        self.assertEqual(s.level, LEVEL_RUNS)

    def test_quit_key(self):
        s = _next_state(NavState(), "q", RUNS, [])
        self.assertTrue(s.quit)


class TestDocListLevel(unittest.TestCase):
    def _at_docs(self, **overrides):
        base = dict(level=LEVEL_DOCS, run_selected=0, run_id="run1")
        base.update(overrides)
        return NavState(**base)

    def test_down_moves_doc_selection(self):
        s = _next_state(self._at_docs(), "KEY_DOWN", RUNS, DOCS)
        self.assertEqual(s.doc_selected, 1)

    def test_esc_backs_to_run_list(self):
        s = _next_state(self._at_docs(doc_selected=1), "\x1b", RUNS, DOCS)
        self.assertEqual(s.level, LEVEL_RUNS)

    def test_drill_in_sets_content_level_and_filename(self):
        s = _next_state(self._at_docs(doc_selected=1), "KEY_RIGHT", RUNS, DOCS)
        self.assertEqual(s.level, LEVEL_CONTENT)
        self.assertEqual(s.doc_filename, "evidence-report.md")

    def test_empty_docs_ignores_navigation_but_not_back(self):
        s = _next_state(self._at_docs(), "KEY_DOWN", RUNS, [])
        self.assertEqual(s.doc_selected, 0)
        s2 = _next_state(self._at_docs(), "KEY_LEFT", RUNS, [])
        self.assertEqual(s2.level, LEVEL_RUNS)


class TestContentLevel(unittest.TestCase):
    def _at_content(self, **overrides):
        base = dict(level=LEVEL_CONTENT, run_id="run1", doc_filename="evidence-report.md")
        base.update(overrides)
        return NavState(**base)

    def test_scroll_down(self):
        s = _next_state(self._at_content(), "KEY_DOWN", RUNS, DOCS)
        self.assertEqual(s.content_scroll, 1)

    def test_scroll_up_clamps_at_zero(self):
        s = _next_state(self._at_content(), "KEY_UP", RUNS, DOCS)
        self.assertEqual(s.content_scroll, 0)

    def test_esc_backs_to_doc_list_preserving_selection(self):
        s = _next_state(self._at_content(doc_selected=1), "\x1b", RUNS, DOCS)
        self.assertEqual(s.level, LEVEL_DOCS)
        self.assertEqual(s.doc_selected, 1)

    def test_quit_from_content_level(self):
        s = _next_state(self._at_content(), "q", RUNS, DOCS)
        self.assertTrue(s.quit)


class TestRenderViaGlow(unittest.TestCase):
    def test_falls_back_to_splitlines_when_glow_not_on_path(self):
        with patch("shutil.which", return_value=None):
            lines = _render_via_glow("line1\nline2", 40)
        self.assertEqual(lines, ["line1", "line2"])

    def test_falls_back_when_glow_exits_nonzero(self):
        with patch("shutil.which", return_value="/usr/bin/glow"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            lines = _render_via_glow("line1\nline2", 40)
        self.assertEqual(lines, ["line1", "line2"])

    def test_falls_back_when_subprocess_raises(self):
        with patch("shutil.which", return_value="/usr/bin/glow"), \
             patch("subprocess.run", side_effect=OSError("boom")):
            lines = _render_via_glow("line1\nline2", 40)
        self.assertEqual(lines, ["line1", "line2"])

    def test_strips_stray_ansi_codes_from_glow_output(self):
        with patch("shutil.which", return_value="/usr/bin/glow"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="\x1b[1mHeading\x1b[0m\nplain")
            lines = _render_via_glow("# Heading\n\nplain", 40)
        self.assertEqual(lines, ["Heading", "plain"])

    def test_empty_content_returns_single_blank_line(self):
        with patch("shutil.which", return_value=None):
            lines = _render_via_glow("", 40)
        self.assertEqual(lines, [""])


if __name__ == "__main__":
    unittest.main()
