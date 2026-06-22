import unittest

from az_backlog_migrator import (
    next_quarter,
    normalize_quarter,
    render_iteration_path,
    replace_quarter_in_title,
    title_matches_prefix_quarter,
)


class QuarterTests(unittest.TestCase):
    def test_normalize_quarter(self):
        self.assertEqual(normalize_quarter("2026Q1"), "2026q1")
        self.assertEqual(normalize_quarter(" 2026q4 "), "2026q4")

    def test_next_quarter_same_year(self):
        self.assertEqual(next_quarter("2026q1"), "2026q2")

    def test_next_quarter_rollover(self):
        self.assertEqual(next_quarter("2026q4"), "2027q1")

    def test_replace_quarter_with_space(self):
        self.assertEqual(
            replace_quarter_in_title("app1 2026q1", "2026q1", "2026q2"),
            "app1 2026q2",
        )

    def test_replace_quarter_without_space(self):
        self.assertEqual(
            replace_quarter_in_title("app32026q4", "2026q4", "2027q1"),
            "app32027q1",
        )

    def test_match_prefix_quarter(self):
        self.assertTrue(title_matches_prefix_quarter("app1 2026q1", "app1", "2026q1"))
        self.assertTrue(title_matches_prefix_quarter("app32026q4", "app3", "2026q4"))
        self.assertFalse(title_matches_prefix_quarter("app2 2026q1", "app1", "2026q1"))

    def test_render_iteration_path(self):
        self.assertEqual(
            render_iteration_path("Project\\{year}\\{quarter_upper}", "2026q1"),
            "Project\\2026\\2026Q1",
        )


if __name__ == "__main__":
    unittest.main()
