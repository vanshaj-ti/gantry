import unittest

from gantry.cli.watch import _watch_color_family
from gantry.labels import short_label


class TestVerificationStageWatchPresentation(unittest.TestCase):
    def test_running_verification_is_yellow(self):
        self.assertEqual(_watch_color_family("checks_running"), "yellow")
        self.assertEqual(_watch_color_family("e2e_running"), "yellow")

    def test_terminal_verification_colors_are_explicit(self):
        self.assertEqual(_watch_color_family("checks_passed"), "green")
        self.assertEqual(_watch_color_family("e2e_skipped"), "green")
        self.assertEqual(_watch_color_family("e2e_failed"), "red")

    def test_verification_statuses_have_short_labels(self):
        self.assertEqual(short_label("checks_passed"), "Checks passed")
        self.assertEqual(short_label("e2e_skipped"), "E2E skipped")


if __name__ == "__main__":
    unittest.main()
