import curses
import unittest

from gantry.docnav import (
    _next_state, _parse_ansi_line, _render_markdown,
    NavState, LEVEL_RUNS, LEVEL_DOCS, LEVEL_CONTENT,
)


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


class TestRenderMarkdown(unittest.TestCase):
    """_render_markdown renders via rich's Console/Markdown in-process (no
    subprocess/pty, unlike the prior glow-based implementation) — these
    tests exercise the real rich import directly, no mocking needed."""

    def test_returns_segment_list_shape(self):
        lines = _render_markdown("plain text", 40)
        self.assertIsInstance(lines, list)
        self.assertTrue(all(isinstance(line, list) for line in lines))
        self.assertTrue(all(isinstance(seg, tuple) and len(seg) == 2
                            for line in lines for seg in line))

    def test_heading_produces_a_bold_segment(self):
        lines = _render_markdown("# Heading", 40)
        flat = [seg for line in lines for seg in line]
        self.assertTrue(any(attr & curses.A_BOLD for _, attr in flat),
                        f"expected at least one bold segment, got: {flat}")

    def test_bold_markdown_produces_a_bold_segment(self):
        lines = _render_markdown("plain **bold** text", 40)
        flat = [seg for line in lines for seg in line]
        self.assertTrue(any(text == "bold" and attr & curses.A_BOLD for text, attr in flat),
                        f"expected a 'bold' segment with A_BOLD, got: {flat}")

    def test_malformed_input_does_not_raise(self):
        # rich's Markdown parser is tolerant, but _render_markdown's own
        # try/except should still catch anything unexpected without
        # crashing the navigator.
        lines = _render_markdown("# unterminated `code span", 40)
        self.assertIsInstance(lines, list)

    def test_empty_content_returns_at_least_one_line(self):
        lines = _render_markdown("", 40)
        self.assertGreaterEqual(len(lines), 1)


class TestParseAnsiLine(unittest.TestCase):
    def test_no_codes_returns_single_normal_segment(self):
        segs = _parse_ansi_line("plain text")
        self.assertEqual(segs, [("plain text", curses.A_NORMAL)])

    def test_bold_segment(self):
        segs = _parse_ansi_line("\x1b[1mBold\x1b[0m")
        self.assertEqual(segs, [("Bold", curses.A_BOLD)])

    def test_italic_segment(self):
        segs = _parse_ansi_line("\x1b[3mItalic\x1b[0m")
        self.assertEqual(segs, [("Italic", curses.A_ITALIC)])

    def test_underline_segment(self):
        segs = _parse_ansi_line("\x1b[4mUnderline\x1b[0m")
        self.assertEqual(segs, [("Underline", curses.A_UNDERLINE)])

    def test_combined_bold_italic(self):
        segs = _parse_ansi_line("\x1b[1;3mBoldItalic\x1b[0m")
        self.assertEqual(segs, [("BoldItalic", curses.A_BOLD | curses.A_ITALIC)])

    def test_mixed_plain_and_styled_segments(self):
        segs = _parse_ansi_line("before \x1b[1mbold\x1b[0m after")
        self.assertEqual(segs, [("before ", curses.A_NORMAL), ("bold", curses.A_BOLD),
                                 (" after", curses.A_NORMAL)])

    def test_empty_line_returns_single_empty_segment(self):
        segs = _parse_ansi_line("")
        self.assertEqual(segs, [("", curses.A_NORMAL)])

    def test_unknown_code_defaults_to_normal(self):
        segs = _parse_ansi_line("\x1b[9mStruckthrough\x1b[0m")
        self.assertEqual(segs, [("Struckthrough", curses.A_NORMAL)])

    def test_256_color_foreground_without_real_curses_degrades_to_normal(self):
        # Outside a real curses.wrapper, curses.COLOR_PAIRS doesn't exist
        # at all — _color_pair_for degrades to A_NORMAL rather than
        # crashing. This is the actual environment plain unittest runs in.
        segs = _parse_ansi_line("\x1b[38;5;200mColored\x1b[0m")
        self.assertEqual(segs, [("Colored", curses.A_NORMAL)])

    def test_256_color_combined_with_bold_degrades_gracefully(self):
        segs = _parse_ansi_line("\x1b[38;5;228;48;5;63;1mHeader\x1b[0m")
        # Bold bit still applies even though color-pair registration is a
        # no-op here (no real curses screen) — style bits and color are
        # independent, only the latter needs curses initialization.
        self.assertEqual(segs, [("Header", curses.A_BOLD)])

    def test_basic_16_color_bold_magenta_degrades_gracefully(self):
        # rich emits both this basic-16 form ("1;35") and the 256-indexed
        # form for different elements in the same document — confirmed
        # directly against real rich output. Bold still applies without a
        # real curses screen; color-pair registration is the no-op part.
        segs = _parse_ansi_line("\x1b[1;35mBoldMagenta\x1b[0m")
        self.assertEqual(segs, [("BoldMagenta", curses.A_BOLD)])

    def test_basic_16_color_fg_and_bg_combined(self):
        segs = _parse_ansi_line("\x1b[1;36;40mBoldCyanOnBlack\x1b[0m")
        self.assertEqual(segs, [("BoldCyanOnBlack", curses.A_BOLD)])

    def test_bright_variant_aliases_to_base_color_no_crash(self):
        segs = _parse_ansi_line("\x1b[91mBrightRed\x1b[0m")
        self.assertEqual(segs, [("BrightRed", curses.A_NORMAL)])


if __name__ == "__main__":
    unittest.main()
