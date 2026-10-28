"""Tests for scripts/privacy_guard.py (marker: SYNTHETIC).

All sensitive-looking strings are assembled at run time so that this file itself passes the guard.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "privacy_guard", ROOT / "scripts" / "privacy_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["privacy_guard"] = guard
_spec.loader.exec_module(guard)

LAT, LON = "50." + "123456", "10." + "123456"  # inside the Germany/Alps box
FAR_LAT, FAR_LON = "0." + "512345", "0." + "498765"  # Gulf of Guinea, synthetic region


def rules(violations) -> set[str]:
    return {v.rule for v in violations}


class ScanTextTest(unittest.TestCase):
    def test_coordinates_in_box_are_flagged(self) -> None:
        for text in (
            f"{LAT}, {LON}",
            f'<trkpt lat="{LAT}" lon="{LON}">',
            f"lat={LAT}, lon={LON}",
            f"{LON} {LAT}",
        ):
            with self.subTest(text=text):
                self.assertIn("coordinates", rules(guard.scan_text("src/a.py", text)))

    def test_coordinates_outside_box_pass(self) -> None:
        self.assertEqual(guard.scan_text("tests/a.py", f"{FAR_LAT}, {FAR_LON}"), [])
        self.assertEqual(guard.scan_text("src/a.py", f"lat={FAR_LAT}, lon={FAR_LON}"), [])

    def test_marker_allows_coordinates_only_in_tests(self) -> None:
        text = f"# {guard.MARKER}\npoint = ({LAT}, {LON})"
        self.assertEqual(guard.scan_text("tests/data.py", text), [])
        self.assertIn("coordinates", rules(guard.scan_text("custom_components/x.py", text)))
        without = f"point = ({LAT}, {LON})"
        self.assertIn("coordinates", rules(guard.scan_text("tests/data.py", without)))

    def test_secret_patterns(self) -> None:
        samples = {
            "aws": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
            "github": "gh" + "p_" + "a" * 36,
            "slack": "xo" + "xb-" + "1234567890-abcdefghij",
            "key": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "assign": 'api_key = "' + "A1b2C3d4" * 3 + '"',
            "url": "https://user:" + "hunter2" + "@host.example.net/path",
        }
        for name, sample in samples.items():
            with self.subTest(name=name):
                self.assertIn("secret", rules(guard.scan_text("src/a.py", sample)))
        self.assertEqual(guard.scan_text("src/a.py", "token = get_token()"), [])

    def test_email_and_phone(self) -> None:
        self.assertIn("email", rules(guard.scan_text("a.md", "write to someone@" + "gmail.com")))
        self.assertEqual(guard.scan_text("a.md", "someone@example.com"), [])
        self.assertEqual(guard.scan_text("a.yml", "uses: actions/checkout@v4"), [])
        self.assertIn("phone", rules(guard.scan_text("a.md", "call +49 711 " + "9876543")))
        self.assertIn("phone", rules(guard.scan_text("a.md", "call 0711 " + "9876543")))
        self.assertEqual(guard.scan_text("a.md", "call +49" + "1701234567"), [])
        self.assertEqual(guard.scan_text("a.json", "2000-01-01T10:00:00+00:00"), [])
        self.assertEqual(guard.scan_text("a.py", "FIT_EPOCH = 631065600"), [])

    def test_denylist_matches_without_leaking_the_term(self) -> None:
        term = "Zaphod" + "Beeblebrox"
        found = guard.scan_text("a.md", f"hello {term.lower()}", guard.parse_denylist(term))
        self.assertEqual(rules(found), {"denylist"})
        self.assertNotIn(term.lower(), str(found[0]).lower())
        self.assertEqual(guard.parse_denylist("ab, ,Valid One\nother"), ["valid one", "other"])

    def test_fixture_needs_marker(self) -> None:
        self.assertIn("fixture-marker", rules(guard.scan_text("tests/fixtures/data.json", "{}")))
        self.assertEqual(guard.scan_text("tests/fixtures/data.json", '{"note": "SYNTHETIC"}'), [])


class ForbiddenNameTest(unittest.TestCase):
    def test_types(self) -> None:
        for name in (
            "a.fit",
            "x/b.GPX",
            "c.tcx",
            "d.zip",
            "e.gz",
            "f.db",
            "activities.csv",
            "strava_export_2026.csv",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(guard.forbidden_name(name))
        for name in ("README.md", "a.py", "data.csv", "hacs.json"):
            self.assertIsNone(guard.forbidden_name(name))

    def test_synthetic_text_fixtures_may_exist(self) -> None:
        self.assertIsNone(guard.forbidden_name("tests/fixtures/synthetic/ride.gpx"))
        self.assertIsNotNone(guard.forbidden_name("tests/fixtures/synthetic/ride.fit"))
        self.assertIsNotNone(guard.forbidden_name("tests/ride.gpx"))


class ScanRootTest(unittest.TestCase):
    def test_finds_problems_in_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "ok.py").write_text("print('hello')\n")
            (root / "ride.fit").write_bytes(b"\x0e\x20.FIT")
            (root / "notes.md").write_text(f"home: {LAT}, {LON}\n")
            found = guard.scan_root(root)
            self.assertEqual({v.path for v in found}, {"ride.fit", "notes.md"})
            self.assertEqual(rules(found), {"file-type", "coordinates"})

    def test_repository_itself_is_clean(self) -> None:
        findings = guard.scan_root(ROOT)
        self.assertEqual([str(f) for f in findings], [])

    def test_main_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(["--root", tmp]), 0)
            (Path(tmp) / "a.gpx").write_text("<gpx/>")
            self.assertEqual(guard.main(["--root", tmp]), 1)


if __name__ == "__main__":
    unittest.main()
