from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    "",
    ".css",
    ".desktop",
    ".in",
    ".md",
    ".py",
    ".sh",
    ".svg",
    ".txt",
    ".yaml",
    ".yml",
}


class PublicReleaseTests(unittest.TestCase):
    def test_private_machine_markers_are_not_in_the_public_tree(self) -> None:
        forbidden = (
            "server" + "mcp",
            "100.74." + "83.78",
            "100.110." + "49.12",
            "/" + "home/",
        )
        findings: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            relative = path.relative_to(ROOT)
            if any(part in {".git", ".venv", "__pycache__"} for part in relative.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for marker in forbidden:
                if marker.casefold() in text.casefold():
                    findings.append(f"{relative}: {marker}")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
