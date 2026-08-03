"""Canonical path policy tests (Task 5).

Frozen after the first red run; do not weaken or delete assertions.
"""

import unittest
from pathlib import Path

from paper_notes import paths

VAULT = Path("/tmp/vault")


class LiteratureRootTest(unittest.TestCase):
    def test_literature_root(self):
        self.assertEqual(paths.literature_root(VAULT), VAULT / "05 Literature")


class CanonicalPathsTest(unittest.TestCase):
    def test_canonical_paths_for_key(self):
        key = "smithExample2026"
        self.assertEqual(
            paths.paper_directory(VAULT, key), VAULT / "05 Literature" / key
        )
        self.assertEqual(
            paths.main_note(VAULT, key),
            VAULT / "05 Literature" / key / "smithExample2026.md",
        )
        self.assertEqual(
            paths.pdf_attachment(VAULT, key),
            VAULT / "05 Literature" / key / "smithExample2026.pdf",
        )
        self.assertEqual(
            paths.mineru_markdown(VAULT, key),
            VAULT / "05 Literature" / key / "minerUmd_smithExample2026.md",
        )
        self.assertEqual(
            paths.figure_note(VAULT, key),
            VAULT / "05 Literature" / key / "Figure解读_smithExample2026.md",
        )
        self.assertEqual(
            paths.attachments_directory(VAULT, key),
            VAULT / "05 Literature" / key / "attachments",
        )
        self.assertEqual(
            paths.cards_directory(VAULT, key),
            VAULT / "05 Literature" / key / "cards",
        )
        self.assertEqual(
            paths.figures_directory(VAULT, key),
            VAULT / "05 Literature" / key / "figures",
        )

    def test_relative_paths_match_plan(self):
        key = "smithExample2026"
        expected = [
            "05 Literature/smithExample2026/smithExample2026.md",
            "05 Literature/smithExample2026/smithExample2026.pdf",
            "05 Literature/smithExample2026/minerUmd_smithExample2026.md",
            "05 Literature/smithExample2026/Figure解读_smithExample2026.md",
            "05 Literature/smithExample2026/attachments/",
            "05 Literature/smithExample2026/cards/",
            "05 Literature/smithExample2026/figures/",
        ]
        got = [
            paths.main_note(VAULT, key).relative_to(VAULT).as_posix(),
            paths.pdf_attachment(VAULT, key).relative_to(VAULT).as_posix(),
            paths.mineru_markdown(VAULT, key).relative_to(VAULT).as_posix(),
            paths.figure_note(VAULT, key).relative_to(VAULT).as_posix(),
            paths.attachments_directory(VAULT, key).relative_to(VAULT).as_posix() + "/",
            paths.cards_directory(VAULT, key).relative_to(VAULT).as_posix() + "/",
            paths.figures_directory(VAULT, key).relative_to(VAULT).as_posix() + "/",
        ]
        self.assertEqual(got, expected)


class KeyValidationTest(unittest.TestCase):
    def test_traversal_and_invalid_keys_rejected(self):
        for bad in (
            "",
            "..",
            "../evil",
            "a/b",
            "a\\b",
            "..smith2026",
            "smith 2026",
            "-smith2026",
            "1smith2026",
            "smith..2026",
        ):
            self.assertFalse(paths.is_valid_key(bad), bad)

    def test_valid_keys_accepted(self):
        for good in (
            "smithExample2026",
            "Smith_2026",
            "a.b-c+d",
            "x2026",
            "ABC",
        ):
            self.assertTrue(paths.is_valid_key(good), good)

    def test_path_functions_reject_traversal(self):
        for bad in ("..", "../evil", "a/b", "..smith2026"):
            with self.assertRaises(ValueError):
                paths.paper_directory(VAULT, bad)
            with self.assertRaises(ValueError):
                paths.main_note(VAULT, bad)
            with self.assertRaises(ValueError):
                paths.pdf_attachment(VAULT, bad)


if __name__ == "__main__":
    unittest.main()
