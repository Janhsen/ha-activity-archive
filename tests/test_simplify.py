"""GPS simplification and resampling: property and measurement tests (marker: SYNTHETIC)."""

from __future__ import annotations

import random
import time
import unittest

from custom_components.activity_archive.core import resample, simplify
from custom_components.activity_archive.core.model import RawActivity, empty_channels
from tests.fixtures import generator as g
from tests.measure import ascent_hysteresis, time_weighted_mean


def raw_from(samples, pauses=()) -> RawActivity:
    ch = empty_channels()
    names = {
        "heart_rate": "hr", "power": "power", "cadence": "cadence", "temperature": "temp",
        "altitude": "alt", "speed": "speed", "distance": "dist", "lat": "lat", "lon": "lon",
    }  # fmt: skip
    for name, key in names.items():
        ch[name] = [s.get(key) for s in samples]
    return RawActivity("fit", [float(s["t"]) for s in samples], ch, pauses=list(pauses))


def gps_of(samples):
    return [s["t"] for s in samples], [s["lat"] for s in samples], [s["lon"] for s in samples]


class GpsGuaranteeTest(unittest.TestCase):
    def check(self, seed: int, n: int, eps: float, time_eps: float | None, noise: float) -> None:
        samples = g.make_track(n, wander=True, seed=seed, noise_m=noise)
        t, lat, lon = gps_of(samples)
        track = simplify.simplify_gps(
            t, lat, lon, epsilon_m=eps, max_gap_s=30, time_epsilon_m=time_eps, spike_filter=False
        )
        cross, sync = simplify.line_deviations(t, lat, lon, track.points)
        self.assertLessEqual(cross, eps + 1e-6, f"seed {seed}")
        if time_eps:
            self.assertLessEqual(sync, time_eps + 1e-6, f"seed {seed}")
        self.assertLess(len(track.points), n * 0.5)
        gaps = [b[0] - a[0] for a, b in zip(track.points, track.points[1:], strict=False)]
        self.assertLessEqual(max(gaps), 30)

    def test_line_within_five_metres(self) -> None:
        for seed in range(12):
            with self.subTest(seed=seed):
                self.check(seed, 1500, 5.0, None, noise=0.8)

    def test_cycling_time_position_within_ten_metres(self) -> None:
        for seed in range(12):
            with self.subTest(seed=seed):
                self.check(seed, 1500, 5.0, 10.0, noise=0.8)

    def test_plain_douglas_peucker_can_exceed_time_limit(self) -> None:
        """Shows why cycling needs the time-synchronous limit: without it 10 m is not kept."""
        worst = 0.0
        for seed in range(12):
            samples = g.make_track(1500, wander=True, seed=seed, noise_m=0.8)
            t, lat, lon = gps_of(samples)
            track = simplify.simplify_gps(t, lat, lon, epsilon_m=5.0, spike_filter=False)
            worst = max(worst, simplify.line_deviations(t, lat, lon, track.points)[1])
        self.assertGreater(worst, 10.0)

    def test_output_is_subset_of_input(self) -> None:
        samples = g.make_track(800, wander=True, seed=4)
        t, lat, lon = gps_of(samples)
        track = simplify.simplify_gps(t, lat, lon, time_epsilon_m=10.0)
        raw = {(int(a), b, c) for a, b, c in zip(t, lat, lon, strict=True)}
        self.assertTrue(set(track.points) <= raw)
        self.assertEqual(track.points[0][0], int(t[0]))
        self.assertEqual(track.points[-1][0], int(t[-1]))

    def test_random_tracks_property(self) -> None:
        rng = random.Random(11)
        for _ in range(15):
            n = rng.randint(3, 600)
            samples = g.make_track(n, wander=True, seed=rng.randint(0, 10_000), noise_m=1.5)
            t, lat, lon = gps_of(samples)
            eps = rng.choice([2.0, 5.0, 10.0])
            track = simplify.simplify_gps(
                t, lat, lon, epsilon_m=eps, time_epsilon_m=2 * eps, spike_filter=False
            )
            cross, sync = simplify.line_deviations(t, lat, lon, track.points)
            self.assertLessEqual(cross, eps + 1e-6)
            self.assertLessEqual(sync, 2 * eps + 1e-6)

    def test_straight_line_keeps_only_forced_points(self) -> None:
        samples = g.make_track(305, step=1)
        for i, s in enumerate(samples):  # exactly straight and uniform
            s["lat"], s["lon"] = g.BASE_LAT, g.BASE_LON + i * 1e-5
        t, lat, lon = gps_of(samples)
        track = simplify.simplify_gps(t, lat, lon, time_epsilon_m=10.0)
        gaps = [b[0] - a[0] for a, b in zip(track.points, track.points[1:], strict=False)]
        self.assertLessEqual(max(gaps), 30)
        # bisection: 304 s needs 2**4 segments of at most 30 s
        self.assertLessEqual(len(track.points), 17)

    def test_spikes_are_removed_before_simplification(self) -> None:
        samples = g.make_track(600)
        samples[100]["lat"] += 0.005  # about 550 m away for one second
        samples[300]["lon"] -= 0.01
        t, lat, lon = gps_of(samples)
        track = simplify.simplify_gps(t, lat, lon)
        self.assertEqual(track.spikes_removed, 2)
        kept_ts = {p[0] for p in track.points}
        self.assertNotIn(int(t[100]), kept_ts)
        self.assertNotIn(int(t[300]), kept_ts)
        without = simplify.simplify_gps(t, lat, lon, spike_filter=False)
        self.assertIn(int(t[100]), {p[0] for p in without.points})

    def test_gap_splits_line_and_sets_markers(self) -> None:
        samples = g.make_track(900)
        for s in samples[300:420]:
            s["lat"] = s["lon"] = None  # tunnel of 120 s
        t, lat, lon = gps_of(samples)
        track = simplify.simplify_gps(t, lat, lon)
        inside = [p for p in track.points if t[300] <= p[0] < t[420]]
        self.assertEqual(inside, [])
        self.assertEqual(track.markers, (int(t[299]) + 1, int(t[899]) + 1))
        self.assertLessEqual(
            max(
                b[0] - a[0]
                for a, b in zip(track.points, track.points[1:], strict=False)
                if a[0] < t[300] and b[0] < t[300]
            ),
            30,
        )

    def test_no_position_and_tiny_inputs(self) -> None:
        empty = simplify.simplify_gps([1.0, 2.0], [None, None], [None, None])
        self.assertEqual((empty.points, empty.markers), ((), ()))
        one = simplify.simplify_gps([5.0], [0.5], [0.5])
        self.assertEqual(len(one.points), 1)
        two = simplify.simplify_gps([5.0, 6.0], [0.5, 0.5001], [0.5, 0.5])
        self.assertEqual(len(two.points), 2)

    def test_twelve_hours_is_fast_enough(self) -> None:
        samples = g.make_track(43_200, wander=True, seed=2, noise_m=0.8)
        t, lat, lon = gps_of(samples)
        started = time.perf_counter()
        track = simplify.simplify_gps(t, lat, lon, time_epsilon_m=10.0)
        self.assertLess(time.perf_counter() - started, 30)
        self.assertLess(len(track.points), 43_200 // 4)


class ResampleTest(unittest.TestCase):
    def curve(self, samples, key: str, profile: str = "precise", pauses=(), **kw):
        cleaned = resample.clean(raw_from(samples, pauses))
        spec = resample.SPEC_BY_KEY[key]
        step = resample.get_profile(profile).steps[key]
        return resample.build_curve(spec, cleaned, step, **kw)

    def test_window_mean_and_epoch_alignment(self) -> None:
        samples = g.make_track(20, start=1_000_000_003)
        for i, s in enumerate(samples):
            s["hr"] = 100 + i
        curve = self.curve(samples, "heart_rate")
        self.assertTrue(all(ts % 5 == 0 for ts, _ in curve.points[1:]))
        # window [1_000_000_005, ..10): samples with i = 2..6 -> 102..106 -> mean 104
        self.assertIn((1_000_000_005, 104.0), curve.points)

    def test_distance_takes_last_value(self) -> None:
        samples = g.make_track(30, start=1_000_000_000)
        curve = self.curve(samples, "distance")
        window = [s for s in samples if 1_000_000_010 <= s["t"] < 1_000_000_020]
        self.assertIn((1_000_000_010, round(window[-1]["dist"], 1)), curve.points)

    def test_missing_channel_makes_no_curve(self) -> None:
        samples = g.make_track(50)
        for s in samples:
            s["speed"] = None
        self.assertIsNone(self.curve(samples, "speed"))
        self.assertIsNone(self.curve(samples, "power"))

    def test_gpx_without_distance_and_speed_gives_no_curves(self) -> None:
        from custom_components.activity_archive.core.gpx_reader import parse_gpx

        raw = parse_gpx(g.encode_gpx(g.make_track(120)))
        cleaned = resample.clean(raw)
        built = {spec.key: resample.build_curve(spec, cleaned, 10) for spec in resample.CURVE_SPECS}
        self.assertIsNone(built["distance"])
        self.assertIsNone(built["speed"])
        self.assertIsNone(built["power"])
        self.assertIsNotNone(built["heart_rate"])
        self.assertIsNotNone(built["altitude"])

    def test_gaps_stay_gaps_and_get_markers(self) -> None:
        samples = g.make_track(1800, gps=False)
        for i in range(600, 660):
            samples[i]["hr"] = 0  # strap dropout recorded as zero
        for i in range(900, 930):
            samples[i]["hr"] = None
        cleaned = resample.clean(raw_from(samples))
        self.assertEqual(cleaned.discarded["heart_rate"], 60)
        curve = resample.build_curve(resample.SPEC_BY_KEY["heart_rate"], cleaned, 5)
        stamps = {ts for ts, _ in curve.points}
        for ts in range(int(g.T0) + 600, int(g.T0) + 660):
            self.assertNotIn(ts, stamps)
        self.assertEqual(len(curve.markers), 3)
        self.assertEqual(curve.markers[-1], int(samples[-1]["t"]) + 1)

    def test_short_dropout_below_three_steps_has_no_marker(self) -> None:
        samples = g.make_track(300, gps=False)
        for i in range(100, 108):
            samples[i]["hr"] = None  # 8 s with a 5 s grid: one empty window only
        curve = self.curve(samples, "heart_rate")
        self.assertEqual(len(curve.markers), 1)

    def test_pauses_remove_samples(self) -> None:
        samples = g.make_track(1200)
        pauses = [(g.T0 + 400, g.T0 + 700)]
        cleaned = resample.clean(raw_from(samples, pauses))
        self.assertEqual(cleaned.in_pause, 300)
        curve = resample.build_curve(resample.SPEC_BY_KEY["heart_rate"], cleaned, 5)
        self.assertEqual(len(curve.markers), 2)
        self.assertTrue(all(not (g.T0 + 400 <= ts < g.T0 + 700) for ts, _ in curve.points))

    def test_duplicate_timestamps_merge_later_value_wins(self) -> None:
        samples = g.make_track(10)
        samples.insert(5, dict(samples[4], hr=199, power=None))
        cleaned = resample.clean(raw_from(samples))
        self.assertEqual(cleaned.duplicates, 1)
        self.assertEqual(len(cleaned.t), 10)
        self.assertEqual(cleaned.channels["heart_rate"][4], 199)

    def test_implausible_values_are_discarded_not_replaced(self) -> None:
        samples = g.make_track(40)
        samples[10]["hr"] = 255
        samples[11]["hr"] = 3
        cleaned = resample.clean(raw_from(samples))
        self.assertIsNone(cleaned.channels["heart_rate"][10])
        self.assertIsNone(cleaned.channels["heart_rate"][11])
        self.assertEqual(cleaned.discarded["heart_rate"], 2)

    def test_first_point_is_not_before_activity_start(self) -> None:
        samples = g.make_track(30, start=1_000_000_003)
        curve = self.curve(samples, "heart_rate", not_before=1_000_000_003)
        self.assertEqual(curve.points[0][0], 1_000_000_003)

    def test_heart_rate_mean_with_smart_recording(self) -> None:
        """Dense sampling while 'moving' must not bias the mean (within 1 bpm)."""
        rng = random.Random(5)
        t, hr, now = [], [], g.T0
        for i in range(6000):
            moving = (i // 400) % 2 == 0
            now += rng.choice([1, 1, 2]) if moving else rng.choice([4, 5, 6])
            t.append(float(now))
            hr.append(150 + rng.gauss(0, 3) if moving else 100 + rng.gauss(0, 3))
        samples = [{"t": a, "hr": round(b)} for a, b in zip(t, hr, strict=True)]
        raw_unweighted = sum(s["hr"] for s in samples) / len(samples)
        truth = time_weighted_mean(t, [s["hr"] for s in samples])
        self.assertGreater(abs(raw_unweighted - truth), 3)  # the trap is real
        for profile in ("precise", "standard", "coarse"):
            curve = self.curve(samples, "heart_rate", profile)
            mean = sum(v for _, v in curve.points) / len(curve.points)
            self.assertLess(abs(mean - truth), 1.0, profile)

    def test_heart_rate_mean_matches_raw_mean_on_even_sampling(self) -> None:
        samples = g.make_track(7200, gps=False)
        raw_mean = sum(s["hr"] for s in samples) / len(samples)
        curve = self.curve(samples, "heart_rate")
        self.assertLess(abs(sum(v for _, v in curve.points) / len(curve.points) - raw_mean), 0.5)

    def test_ascent_after_thinning_stays_close(self) -> None:
        for amplitude in (3, 8):
            samples = g.make_track(3600, gps=False, hills=amplitude, noise_m=0)
            truth = ascent_hysteresis([s["alt"] for s in samples], 3)
            for profile, tolerance in (("precise", 0.05), ("standard", 0.10)):
                curve = self.curve(samples, "altitude", profile)
                got = ascent_hysteresis([v for _, v in curve.points], 3)
                self.assertLess(abs(got - truth) / truth, tolerance, (amplitude, profile))

    def test_profile_grids(self) -> None:
        precise = resample.get_profile("precise")
        self.assertEqual(precise.steps["heart_rate"], 5)
        self.assertEqual(precise.steps["power"], 5)
        for key in ("altitude", "speed", "distance", "cadence", "temperature"):
            self.assertEqual(precise.steps[key], 10)
        self.assertEqual(set(resample.get_profile("standard").steps.values()), {15})
        self.assertEqual(set(resample.get_profile("coarse").steps.values()), {30})
        self.assertEqual(precise.gps_time_epsilon_m(True), 10.0)
        self.assertIsNone(precise.gps_time_epsilon_m(False))


if __name__ == "__main__":
    unittest.main()
