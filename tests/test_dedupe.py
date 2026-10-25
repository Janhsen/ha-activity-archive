"""Duplicate decision tree and source rule (marker: SYNTHETIC)."""

from __future__ import annotations

import itertools
import random
import time
import unittest
from dataclasses import replace

from custom_components.activity_archive.core import dedupe, sports
from custom_components.activity_archive.core.dedupe import (
    GARMIN_FIT,
    STRAVA_API,
    STRAVA_CSV,
    STRAVA_FILE,
    ActivityFacts,
    Decision,
)

START = 946_731_600.0  # 2000-01-01T23:00:00Z, fixed
MTB = sports.from_fit("cycling", "mountain")
RIDE = sports.from_strava("Ride")
RUN = sports.from_strava("Run")


def facts(key: str, source: str, **kw) -> ActivityFacts:
    base = {
        "start": START,
        "duration_s": 3600.0,
        "distance_m": 20_000.0,
        "sport": MTB,
        "source_id": key,
    }
    return ActivityFacts(key=key, source=source, **(base | kw))


class CompareTest(unittest.TestCase):
    def decide(self, a: ActivityFacts, b: ActivityFacts) -> Decision:
        forward, backward = dedupe.compare(a, b), dedupe.compare(b, a)
        self.assertEqual(forward.decision, backward.decision, "comparison must be symmetric")
        return forward.decision

    def test_same_source_id_and_same_content(self) -> None:
        a = facts("g1", GARMIN_FIT, source_id="42", start=START)
        b = facts("g2", GARMIN_FIT, source_id="42", start=START + 99999)
        self.assertEqual(self.decide(a, b), Decision.IDENTICAL)
        c = facts("g3", STRAVA_FILE, content_hash="abc", start=START + 5000)
        d = facts("g4", GARMIN_FIT, content_hash="abc")
        self.assertEqual(self.decide(c, d), Decision.IDENTICAL)

    def test_same_fit_file_id(self) -> None:
        a = facts("a", GARMIN_FIT, file_id=(1, 2, 3, 4), source_id="1", duration_s=1.0)
        b = facts("b", STRAVA_FILE, file_id=(1, 2, 3, 4), source_id="2")
        self.assertEqual(self.decide(a, b), Decision.IDENTICAL)

    def test_export_against_garmin_with_90_second_offset(self) -> None:
        garmin = facts("g", GARMIN_FIT)
        strava = facts(
            "s", STRAVA_FILE, start=START + 90, duration_s=3560.0, distance_m=20_300.0, sport=RIDE
        )
        self.assertEqual(self.decide(garmin, strava), Decision.IDENTICAL)

    def test_start_tolerance_edges(self) -> None:
        garmin = facts("g", GARMIN_FIT)
        edge = facts("s", STRAVA_FILE, start=START + 120)
        self.assertEqual(self.decide(garmin, edge), Decision.IDENTICAL)
        outside = facts("s2", STRAVA_FILE, start=START + 121)
        self.assertNotEqual(self.decide(garmin, outside), Decision.IDENTICAL)

    def test_duration_and_distance_edges(self) -> None:
        garmin = facts("g", GARMIN_FIT)
        ok = facts("a", STRAVA_FILE, duration_s=3600 * 1.05, distance_m=20_000 * 0.97)
        self.assertEqual(self.decide(garmin, ok), Decision.IDENTICAL)
        borderline = facts("b", STRAVA_FILE, duration_s=3600 * 1.08)
        self.assertEqual(self.decide(garmin, borderline), Decision.UNCLEAR)
        far = facts("c", STRAVA_FILE, duration_s=3600 * 1.5)
        self.assertEqual(self.decide(garmin, far), Decision.DIFFERENT)
        distance_off = facts("d", STRAVA_FILE, distance_m=24_000.0)
        self.assertEqual(self.decide(garmin, distance_off), Decision.DIFFERENT)

    def test_back_to_back_four_minutes_apart_stay_separate(self) -> None:
        first = facts("a", GARMIN_FIT, duration_s=1800.0, distance_m=8000.0)
        second = facts(
            "b", GARMIN_FIT, start=START + 1800 + 240, duration_s=1800.0, distance_m=8000.0
        )
        self.assertEqual(self.decide(first, second), Decision.DIFFERENT)
        grouping = dedupe.group_activities([first, second])
        self.assertEqual([len(g.members) for g in grouping.groups], [1, 1])

    def test_without_distance_start_duration_and_family_decide(self) -> None:
        trainer = sports.from_fit("cycling", "indoor_cycling")
        a = facts("a", GARMIN_FIT, distance_m=None, sport=trainer)
        b = facts("b", STRAVA_CSV, distance_m=0.0, sport=sports.from_strava("VirtualRide"))
        self.assertEqual(self.decide(a, b), Decision.IDENTICAL)
        c = facts("c", STRAVA_CSV, distance_m=None, sport=sports.from_strava("WeightTraining"))
        self.assertEqual(self.decide(a, c), Decision.UNCLEAR)

    def test_family_is_only_a_cross_check(self) -> None:
        mtb = facts("a", GARMIN_FIT, sport=MTB)
        ride = facts("b", STRAVA_FILE, sport=RIDE)
        verdict = dedupe.compare(mtb, ride)
        self.assertEqual(verdict.decision, Decision.IDENTICAL)
        self.assertTrue(verdict.notes)  # sport changed note
        run = facts("c", STRAVA_FILE, sport=RUN)
        self.assertEqual(self.decide(mtb, run), Decision.UNCLEAR)
        unknown = facts("d", STRAVA_FILE, sport=sports.from_strava("Kitesurf"))
        self.assertEqual(self.decide(mtb, unknown), Decision.IDENTICAL)

    def test_missing_duration_is_unclear(self) -> None:
        self.assertEqual(
            self.decide(facts("a", GARMIN_FIT), facts("b", STRAVA_CSV, duration_s=None)),
            Decision.UNCLEAR,
        )

    def test_crop_candidate_is_unclear(self) -> None:
        full = facts("a", GARMIN_FIT, duration_s=3600.0, distance_m=20_000.0)
        cropped = facts("b", STRAVA_FILE, start=START + 600, duration_s=2400.0, distance_m=13_300.0)
        self.assertEqual(self.decide(full, cropped), Decision.UNCLEAR)
        slower = facts("c", STRAVA_FILE, start=START + 600, duration_s=2400.0, distance_m=5000.0)
        self.assertEqual(self.decide(full, slower), Decision.DIFFERENT)

    def test_min_slack_options(self) -> None:
        a = facts("a", GARMIN_FIT, duration_s=100.0, distance_m=None)
        b = facts("b", STRAVA_CSV, duration_s=108.0, distance_m=None)
        self.assertEqual(dedupe.compare(a, b).decision, Decision.UNCLEAR)
        relaxed = dedupe.DedupeConfig(min_duration_slack_s=10.0)
        self.assertEqual(dedupe.compare(a, b, relaxed).decision, Decision.IDENTICAL)


class GroupingTest(unittest.TestCase):
    def scenario(self) -> list[ActivityFacts]:
        return [
            facts("g1", GARMIN_FIT, title=None, curves=frozenset({"heart_rate", "position"})),
            facts(
                "s1",
                STRAVA_FILE,
                start=START + 40,
                title="Evening loop",
                sport=RIDE,
                curves=frozenset({"heart_rate", "power"}),
                file_format="gpx",
            ),
            facts(
                "c1",
                STRAVA_CSV,
                start=START + 90,
                title="Evening loop",
                sport=RIDE,
                curves=frozenset(),
            ),
            facts(
                "solo",
                STRAVA_CSV,
                start=START + 86_400,
                title="Only on Strava",
                sport=RUN,
                distance_m=5000.0,
                duration_s=1700.0,
                curves=frozenset(),
            ),
            facts("u1", GARMIN_FIT, start=START + 2 * 86_400),
            facts("u2", STRAVA_FILE, start=START + 2 * 86_400 + 30, duration_s=3600 * 1.08),
        ]

    def test_decision_tree_end_to_end(self) -> None:
        grouping = dedupe.group_activities(self.scenario())
        by_first = {g.members[0].key: g for g in grouping.groups}
        triple = next(
            g for g in grouping.groups if {"g1", "s1", "c1"} <= {m.key for m in g.members}
        )
        self.assertEqual(len(triple.members), 3)
        self.assertIn("solo", by_first)
        self.assertEqual(len(by_first["solo"].members), 1)
        # unclear pair: both stay, listed for review
        self.assertIn("u1", by_first)
        self.assertIn("u2", by_first)
        self.assertEqual([(p.first, p.second) for p in grouping.review], [("u1", "u2")])

    def test_garmin_wins_and_strava_supplies_title_and_sport(self) -> None:
        grouping = dedupe.group_activities(self.scenario())
        triple = next(g for g in grouping.groups if len(g.members) == 3)
        merged = dedupe.merge_group(triple)
        self.assertEqual(merged.primary, "g1")
        self.assertEqual(merged.title, "Evening loop")
        self.assertEqual(merged.curve_sources["heart_rate"], "g1")
        self.assertEqual(merged.curve_sources["position"], "g1")
        self.assertEqual(merged.curve_sources["power"], "s1")  # missing kind from a lower source
        self.assertEqual(merged.sport.sport, "cycling")
        self.assertEqual(set(merged.member_keys), {"g1", "s1", "c1"})

    def test_csv_ride_does_not_displace_garmin_mountain(self) -> None:
        csv_ride = sports.from_strava("Ride", csv=True)
        self.assertIsNone(csv_ride.sport_type)
        group = dedupe.Group(
            (facts("g", GARMIN_FIT, sport=MTB), facts("c", STRAVA_CSV, sport=csv_ride))
        )
        self.assertEqual(dedupe.merge_group(group).sport.sport_type, "mountain")
        api_ride = facts("a", STRAVA_API, sport=sports.from_strava("Ride"))
        group = dedupe.Group((facts("g", GARMIN_FIT, sport=MTB), api_ride))
        self.assertEqual(dedupe.merge_group(group).sport.sport_type, "road")  # Strava decides

    def test_strava_only_activity_keeps_everything_from_strava(self) -> None:
        only = facts(
            "s",
            STRAVA_FILE,
            title="Hike",
            description="Cloudy",
            sport=sports.from_strava("Hike"),
            curves=frozenset({"heart_rate"}),
        )
        merged = dedupe.merge_group(dedupe.Group((only,)))
        self.assertEqual(
            (merged.primary, merged.title, merged.description), ("s", "Hike", "Cloudy")
        )
        self.assertEqual(merged.sport.sport, "hiking")

    def test_strautomator_copies_prefer_file_fit_and_longer(self) -> None:
        original = facts("o", STRAVA_FILE, file_format="fit", curves=frozenset({"heart_rate"}))
        copy_gpx = facts(
            "c", STRAVA_FILE, file_format="gpx", start=START + 20, curves=frozenset({"heart_rate"})
        )
        summary = facts("m", STRAVA_CSV, start=START + 5)
        merged = dedupe.merge_group(dedupe.Group((summary, copy_gpx, original)))
        self.assertEqual(merged.primary, "o")
        self.assertEqual(merged.member_keys, ("o", "c", "m"))

    def test_order_independence(self) -> None:
        records = [
            *self.scenario(),
            facts("x1", GARMIN_FIT, start=START + 5 * 86_400, duration_s=900.0, distance_m=3000.0),
            facts(
                "x2",
                STRAVA_FILE,
                start=START + 5 * 86_400 + 4 * 60,
                duration_s=900.0,
                distance_m=3000.0,
            ),
        ]
        reference = dedupe.group_activities(records)
        signature = [sorted(m.key for m in g.members) for g in reference.groups]
        rng = random.Random(1)
        for _ in range(25):
            shuffled = records[:]
            rng.shuffle(shuffled)
            again = dedupe.group_activities(shuffled)
            self.assertEqual([sorted(m.key for m in g.members) for g in again.groups], signature)
            self.assertEqual(again.review, reference.review)

    def test_transitive_conflict_merges_nothing(self) -> None:
        """a~b and b~c but a and c clearly differ: keep all three, list the pairs."""
        short = {"duration_s": 600.0, "distance_m": 2000.0}
        a = facts("a", GARMIN_FIT, **short)
        b = facts("b", STRAVA_FILE, start=START + 120, **short)
        c = facts("c", STRAVA_API, start=START + 240, **short)
        self.assertEqual(dedupe.compare(a, b).decision, Decision.IDENTICAL)
        self.assertEqual(dedupe.compare(b, c).decision, Decision.IDENTICAL)
        self.assertEqual(dedupe.compare(a, c).decision, Decision.DIFFERENT)
        grouping = dedupe.group_activities([a, b, c])
        self.assertEqual([len(g.members) for g in grouping.groups], [1, 1, 1])
        self.assertEqual([(p.first, p.second) for p in grouping.review], [("a", "b"), ("b", "c")])

    def test_large_input_is_fast(self) -> None:
        rng = random.Random(9)
        records = [
            facts(
                f"r{i}",
                STRAVA_CSV,
                start=START + i * 7200 + rng.randint(0, 600),
                duration_s=float(rng.randint(600, 6000)),
                distance_m=float(rng.randint(2000, 90000)),
            )
            for i in range(2500)
        ]
        started = time.perf_counter()
        grouping = dedupe.group_activities(records)
        self.assertLess(time.perf_counter() - started, 5)
        self.assertEqual(len(grouping.groups), 2500)

    def test_every_permutation_of_three_gives_same_groups(self) -> None:
        trio = [
            facts("g", GARMIN_FIT, curves=frozenset({"heart_rate"})),
            facts("s", STRAVA_FILE, start=START + 30),
            facts("c", STRAVA_CSV, start=START - 30, duration_s=3590.0),
        ]
        outcomes = set()
        for perm in itertools.permutations(trio):
            grouping = dedupe.group_activities(perm)
            outcomes.add(tuple(tuple(sorted(m.key for m in g.members)) for g in grouping.groups))
        self.assertEqual(len(outcomes), 1)

    def test_replace_helper_keeps_facts_immutable(self) -> None:
        base = facts("a", GARMIN_FIT)
        changed = replace(base, title="x")
        self.assertIsNone(base.title)
        self.assertEqual(changed.title, "x")


if __name__ == "__main__":
    unittest.main()
