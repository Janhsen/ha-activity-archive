"""Readers: FIT, GPX, TCX, gzip and the failure modes (marker: SYNTHETIC)."""

from __future__ import annotations

import contextlib
import random
import unittest

from custom_components.activity_archive.core import fit_reader, loader
from custom_components.activity_archive.core.gpx_reader import parse_gpx, parse_time
from custom_components.activity_archive.core.model import (
    BadFileError,
    IntegrityError,
    UnsafeFileError,
)
from tests.fixtures import generator as g


def n_values(raw, channel: str) -> int:
    return sum(v is not None for v in raw.channels[channel])


class FitReaderTest(unittest.TestCase):
    def test_crc_reference_value(self) -> None:
        self.assertEqual(fit_reader.fit_crc(b"123456789"), 0xBB3D)
        data = bytes(range(256)) * 3
        self.assertEqual(fit_reader.fit_crc(data), g.fit_crc(data))

    def test_roundtrip_values(self) -> None:
        samples = g.make_track(120, power=True, cadence=True)
        raw = fit_reader.parse_fit(g.encode_fit(samples))
        self.assertEqual([int(t) for t in raw.t], [int(s["t"]) for s in samples])
        self.assertEqual(raw.channels["heart_rate"], [s["hr"] for s in samples])
        self.assertEqual(raw.channels["power"], [s["power"] for s in samples])
        self.assertAlmostEqual(raw.channels["altitude"][10], samples[10]["alt"], delta=0.2)
        self.assertAlmostEqual(raw.channels["speed"][10], samples[10]["speed"], delta=0.001)
        self.assertAlmostEqual(raw.channels["distance"][10], samples[10]["dist"], delta=0.01)
        self.assertAlmostEqual(raw.channels["lat"][10], samples[10]["lat"], delta=1e-6)

    def test_session_tz_and_identity(self) -> None:
        samples = g.make_track(60)
        session = g.session_for(samples, 2, 8, ascent=123, descent=120, avg_hr=131, max_hr=150)
        data = g.encode_fit(samples, session=session, local_offset_s=7200, serial=4242)
        raw = fit_reader.parse_fit(data)
        self.assertEqual((raw.sport_raw, raw.sub_sport_raw), ("cycling", "mountain"))
        self.assertEqual(raw.session["ascent_m"], 123)
        self.assertEqual(raw.session["avg_hr"], 131)
        self.assertAlmostEqual(raw.session["distance_m"], samples[-1]["dist"], delta=0.01)
        self.assertEqual((raw.tz_offset_s, raw.tz_source), (7200, "fit"))
        self.assertIsNotNone(raw.file_id)
        self.assertEqual(raw.file_id[2], 4242)

    def test_no_session_scalars_without_session(self) -> None:
        raw = fit_reader.parse_fit(g.encode_fit(g.make_track(30)))
        self.assertEqual(raw.session, {})
        self.assertEqual(raw.tz_source, "assumed")
        self.assertIsNone(raw.tz_offset_s)

    def test_missing_channels_stay_missing(self) -> None:
        raw = fit_reader.parse_fit(g.encode_fit(g.make_track(60, gps=False, hr=False)))
        self.assertEqual(n_values(raw, "lat"), 0)
        self.assertEqual(n_values(raw, "heart_rate"), 0)
        self.assertEqual(n_values(raw, "power"), 0)
        self.assertEqual(n_values(raw, "distance"), 60)

    def test_compressed_timestamps(self) -> None:
        samples = g.make_track(300, step=3)
        raw = fit_reader.parse_fit(g.encode_fit(samples, compressed=True))
        self.assertEqual([int(t) for t in raw.t], [int(s["t"]) for s in samples])

    def test_developer_fields_are_skipped(self) -> None:
        raw = fit_reader.parse_fit(g.encode_fit(g.make_track(50), developer_fields=True))
        self.assertEqual(len(raw.t), 50)

    def test_pauses_from_timer_events(self) -> None:
        samples = g.make_track(1000)
        events = [(g.T0 + 400, 0, 4), (g.T0 + 700, 0, 0)]
        raw = fit_reader.parse_fit(g.encode_fit(samples, events=events))
        self.assertEqual(raw.pauses, [(g.T0 + 400, g.T0 + 700)])

    def test_open_pause_at_end(self) -> None:
        raw = fit_reader.parse_fit(g.encode_fit(g.make_track(600), events=[(g.T0 + 500, 0, 4)]))
        self.assertEqual(len(raw.pauses), 1)
        self.assertEqual(raw.pauses[0][0], g.T0 + 500)

    def test_pool_lengths(self) -> None:
        samples = g.make_track(240, gps=False)
        lengths = [
            {"start": g.T0 + 60 * i, "elapsed": 30.0, "timer": 29.0, "strokes": 18}
            for i in range(4)
        ]
        raw = fit_reader.parse_fit(g.encode_fit(samples, lengths=lengths))
        self.assertEqual(len(raw.lengths), 4)
        self.assertEqual(raw.lengths[0]["strokes"], 18)
        self.assertEqual(raw.lengths[0]["timer_s"], 29.0)
        self.assertEqual(n_values(raw, "lat"), 0)

    def test_origin_position_is_invalid(self) -> None:
        samples = g.make_track(30)
        samples[10]["lat"] = samples[10]["lon"] = 0.0
        raw = fit_reader.parse_fit(g.encode_fit(samples))
        self.assertIsNone(raw.channels["lat"][10])
        self.assertIsNotNone(raw.channels["lat"][11])

    def test_chained_files(self) -> None:
        data = g.encode_fit(g.make_track(100)) + g.encode_fit(g.make_track(50, start=g.T0 + 9000))
        self.assertEqual(len(fit_reader.parse_fit(data).t), 150)

    def test_time_jumps_back_are_sorted_with_warning(self) -> None:
        samples = g.make_track(100)
        samples[50]["t"] = g.T0 - 10_000  # clock jumped back for one record
        raw = fit_reader.parse_fit(g.encode_fit(samples))
        self.assertEqual(raw.t, sorted(raw.t))
        self.assertTrue(any("not monotonic" in w for w in raw.warnings))

    def test_relative_timestamps_are_ignored(self) -> None:
        samples = g.make_track(40)
        samples[5]["t"] = fit_reader.FIT_EPOCH + 1000  # device uptime, not a date
        raw = fit_reader.parse_fit(g.encode_fit(samples))
        self.assertEqual(len(raw.t), 39)
        self.assertTrue(any("absolute" in w for w in raw.warnings))

    def test_several_sessions_omit_totals(self) -> None:
        samples = g.make_track(40)
        session = g.session_for(samples, 2, 8)
        data = g.encode_fit(samples, session=session)
        # two sessions: chain two complete files
        raw = fit_reader.parse_fit(data + g.encode_fit(samples, session=session))
        self.assertEqual(raw.session, {})


class FitFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.good = g.encode_fit(g.make_track(2000))

    def test_crc_error_is_skipped_in_strict_mode(self) -> None:
        with self.assertRaises(IntegrityError):
            fit_reader.parse_fit(g.corrupt_crc(self.good))

    def test_crc_error_tolerated_when_not_strict(self) -> None:
        raw = fit_reader.parse_fit(g.corrupt_crc(self.good), strict=False)
        self.assertEqual(len(raw.t), 2000)
        self.assertTrue(any("CRC" in w for w in raw.warnings))

    def test_bit_flip_in_data_is_detected(self) -> None:
        bad = bytearray(self.good)
        bad[500] ^= 0x10
        with self.assertRaises(IntegrityError):
            fit_reader.parse_fit(bytes(bad))

    def test_header_crc_error(self) -> None:
        bad = bytearray(self.good)
        bad[12] ^= 0xFF
        with self.assertRaises(IntegrityError):
            fit_reader.parse_fit(bytes(bad))

    def test_truncated(self) -> None:
        cut = self.good[: len(self.good) // 2]
        with self.assertRaises(IntegrityError):
            fit_reader.parse_fit(cut)
        raw = fit_reader.parse_fit(cut, strict=False)
        self.assertTrue(900 < len(raw.t) < 1100)
        self.assertTrue(raw.warnings)

    def test_garbage_and_empty(self) -> None:
        for blob in (b"", b"\x00" * 500, bytes(range(256)) * 4, self.good[:14]):
            with self.subTest(size=len(blob)), self.assertRaises(BadFileError):
                fit_reader.parse_fit(blob)

    def test_garbage_inside_never_raises_unexpected(self) -> None:
        bad = bytearray(self.good)
        for i in range(60, 80):
            bad[i] = 0xAA
        with contextlib.suppress(BadFileError):  # a clean refusal is fine, anything else fails
            fit_reader.parse_fit(bytes(bad), strict=False)

    def test_fuzz_random_damage(self) -> None:
        rng = random.Random(3)
        for _ in range(60):
            bad = bytearray(g.encode_fit(g.make_track(80), compressed=True, developer_fields=False))
            for _ in range(rng.randint(1, 8)):
                bad[rng.randrange(len(bad))] = rng.randrange(256)
            with contextlib.suppress(BadFileError):
                fit_reader.parse_fit(bytes(bad), strict=False)


class GpxReaderTest(unittest.TestCase):
    def test_extensions_and_position(self) -> None:
        samples = g.make_track(200, cadence=True)
        raw = parse_gpx(g.encode_gpx(samples))
        self.assertEqual(len(raw.t), 200)
        for channel in ("heart_rate", "cadence", "temperature", "altitude", "lat", "lon"):
            self.assertEqual(n_values(raw, channel), 200, channel)
        self.assertEqual(raw.title, "Synthetic ride")

    def test_no_distance_and_no_speed_are_not_calculated(self) -> None:
        raw = parse_gpx(g.encode_gpx(g.make_track(200)))
        self.assertEqual(n_values(raw, "distance"), 0)
        self.assertEqual(n_values(raw, "speed"), 0)
        self.assertEqual(n_values(raw, "power"), 0)

    def test_without_elevation_and_extensions(self) -> None:
        raw = parse_gpx(g.encode_gpx(g.make_track(50), ele=False, ext=False))
        self.assertEqual(n_values(raw, "altitude"), 0)
        self.assertEqual(n_values(raw, "heart_rate"), 0)
        self.assertEqual(n_values(raw, "lat"), 50)

    def test_segments_become_pauses(self) -> None:
        samples = g.make_track(300)
        for s in samples[150:]:
            s["t"] += 600
        raw = parse_gpx(g.encode_gpx(samples, split_at=[150]))
        self.assertEqual(raw.pauses, [(samples[149]["t"], samples[150]["t"])])

    def test_truncated(self) -> None:
        data = g.encode_gpx(g.make_track(400))
        with self.assertRaises(IntegrityError):
            parse_gpx(data[: len(data) // 2])
        raw = parse_gpx(data[: len(data) // 2], strict=False)
        self.assertTrue(100 < len(raw.t) < 400)

    def test_entities_and_doctype_are_rejected(self) -> None:
        bomb = (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            b"<gpx>&b;</gpx>"
        )
        with self.assertRaises(UnsafeFileError):
            parse_gpx(bomb)

    def test_no_points(self) -> None:
        with self.assertRaises(BadFileError):
            parse_gpx(b'<?xml version="1.0"?><gpx><trk><trkseg></trkseg></trk></gpx>')

    def test_time_with_offsets_and_fractions(self) -> None:
        a = parse_time("2000-01-01T09:52:10.250+02:00")
        b = parse_time("2000-01-01T07:52:10.250Z")
        self.assertAlmostEqual(a, b, places=6)
        self.assertIsNone(parse_time("yesterday"))

    def test_clock_change_night_has_two_distinct_02_30(self) -> None:
        """25 Oct 2026: 02:30 local time happens twice (CEST, then CET)."""
        samples = g.make_track(4, step=1800, start=g.T0)
        texts = [
            "2026-10-25T01:30:00+02:00",
            "2026-10-25T02:30:00+02:00",
            "2026-10-25T02:30:00+01:00",
            "2026-10-25T03:30:00+01:00",
        ]
        raw = parse_gpx(g.encode_gpx(samples, time_texts=texts))
        diffs = [b - a for a, b in zip(raw.t, raw.t[1:], strict=False)]
        self.assertEqual(diffs, [3600.0, 3600.0, 3600.0])

    def test_time_jump_in_gpx_is_kept_in_order(self) -> None:
        samples = g.make_track(10)
        samples[5]["t"] += 86400  # a clock jump of one day
        raw = parse_gpx(g.encode_gpx(samples))
        self.assertEqual(len(raw.t), 10)
        self.assertEqual(max(raw.t) - min(raw.t), samples[5]["t"] - samples[0]["t"])


class TcxReaderTest(unittest.TestCase):
    def test_basic_tcx(self) -> None:
        raw = loader.load_bytes(g.encode_tcx(g.make_track(50)))
        self.assertEqual(raw.source_format, "tcx")
        self.assertEqual(raw.sport_raw, "Biking")
        self.assertEqual(n_values(raw, "heart_rate"), 50)
        self.assertEqual(n_values(raw, "distance"), 50)
        self.assertEqual(n_values(raw, "lat"), 50)


class LoaderTest(unittest.TestCase):
    def test_detects_formats(self) -> None:
        samples = g.make_track(30)
        self.assertEqual(loader.load_bytes(g.encode_fit(samples)).source_format, "fit")
        self.assertEqual(loader.load_bytes(g.encode_gpx(samples)).source_format, "gpx")
        with self.assertRaises(BadFileError):
            loader.load_bytes(b"hello world")

    def test_gzip_and_same_hash_for_same_content(self) -> None:
        data = g.encode_gpx(g.make_track(60))
        plain = loader.load_bytes(data)
        packed = loader.load_bytes(g.gzip_bytes(data))
        double = loader.load_bytes(g.gzip_bytes(g.gzip_bytes(data)))
        self.assertEqual(plain.content_hash, packed.content_hash)
        self.assertEqual(plain.content_hash, double.content_hash)
        self.assertEqual(len(packed.t), 60)

    def test_triple_gzip_is_rejected(self) -> None:
        data = g.gzip_bytes(g.gzip_bytes(g.gzip_bytes(g.encode_gpx(g.make_track(20)))))
        with self.assertRaises(UnsafeFileError):
            loader.load_bytes(data)

    def test_gzip_bomb_is_rejected(self) -> None:
        bomb = g.gzip_bytes(b"\x00" * (3 * 2**20))
        self.assertLess(len(bomb), 10_000)
        with self.assertRaises(UnsafeFileError):
            loader.load_bytes(bomb, max_unpacked=2**20)

    def test_truncated_gzip(self) -> None:
        data = g.gzip_bytes(g.encode_gpx(g.make_track(500)))
        with self.assertRaises(IntegrityError):
            loader.load_bytes(data[: len(data) // 2])

    def test_corrupt_gzip(self) -> None:
        data = bytearray(g.gzip_bytes(g.encode_gpx(g.make_track(500))))
        data[len(data) // 2] ^= 0xFF
        with self.assertRaises(BadFileError):
            loader.load_bytes(bytes(data))


if __name__ == "__main__":
    unittest.main()
