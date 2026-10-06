"""Unit tests for the tracking-plan audit core."""

from __future__ import annotations

import unittest

from app.plan import AxisKind, ValidationError, audit


def base_payload(**overrides):
    payload = {
        "initial": {"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
        "azimuth_wrap_min_mdeg": -360_000,
        "azimuth_wrap_max_mdeg": 360_000,
        "max_azimuth_speed_mdeg_per_ms": 100,
        "max_elevation_speed_mdeg_per_ms": 100,
        "segments": [],
    }
    payload.update(overrides)
    return payload


def seg(seg_id, start, end, az, el=0):
    return {
        "id": seg_id,
        "start_ms": start,
        "end_ms": end,
        "azimuth_mdeg": az,
        "elevation_mdeg": el,
    }


class SingleSegmentTests(unittest.TestCase):
    def test_nearest_turn_both_directions(self):
        # Target 350000 is -10000 physically; both turns are in wrap and
        # reachable with a generous gap.
        payload = base_payload(
            max_azimuth_speed_mdeg_per_ms=10_000,
            segments=[seg(1, 1_000, 2_000, 350_000)],
        )
        result = audit(payload)
        self.assertTrue(result.feasible)
        self.assertEqual(result.reachable[0], [-10_000, 350_000])

    def test_tight_gap_selects_single_turn(self):
        payload = base_payload(
            max_azimuth_speed_mdeg_per_ms=10,
            segments=[seg(1, 1_000, 2_000, 350_000)],
        )
        result = audit(payload)
        self.assertTrue(result.feasible)
        self.assertEqual(result.reachable[0], [-10_000])

    def test_azimuth_out_of_range_rejected(self):
        payload = base_payload(segments=[seg(1, 0, 10, 360_000)])
        with self.assertRaises(ValidationError):
            audit(payload)

    def test_no_turn_reaches_first_segment(self):
        payload = base_payload(
            initial={"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
            azimuth_wrap_min_mdeg=-10_000,
            azimuth_wrap_max_mdeg=10_000,
            max_azimuth_speed_mdeg_per_ms=1,
            segments=[seg(7, 10, 20, 180_000)],
        )
        result = audit(payload)
        self.assertFalse(result.feasible)
        self.assertEqual(result.failed_segment_index, 1)
        self.assertEqual(result.failed_segment_id, 7)
        self.assertEqual(result.axis, AxisKind.AZIMUTH)


class CableWrapTrapTests(unittest.TestCase):
    """A nearest-turn greedy must be rejected as the decision rule.

    Wrap [380000, 760000]; start is forced to physical 400000.
    Segment 2 logical 30000 offers turns 390000 (10000 away: the greedy
    favourite) and 750000 (350000 away).  Segment 3 logical 350000 has
    only turn 710000 inside the wrap; from 390000 it is 320000 away, from
    750000 only 40000.  A wide second gap and a 40 ms third gap makes the
    near turn a dead end while the far turn completes the plan.
    """

    def trap_payload(self, gap3_ms):
        return base_payload(
            initial={"time_ms": 0, "azimuth_mdeg": 400_000, "elevation_mdeg": 0},
            azimuth_wrap_min_mdeg=380_000,
            azimuth_wrap_max_mdeg=760_000,
            max_azimuth_speed_mdeg_per_ms=1_000,
            max_elevation_speed_mdeg_per_ms=1_000_000,
            segments=[
                # gap 0: only 400000 (not 760000) is reachable.
                seg(1, 0, 1_000, 40_000),
                # gap 350 ms: both 390000 and 750000 are forward-reachable.
                seg(2, 1_350, 1_700, 30_000),
                # final gap decides whether the chain can finish.
                seg(3, 1_700 + gap3_ms, 2_000 + gap3_ms, 350_000),
            ],
        )

    def test_global_assignment_feasible_greedy_trap(self):
        result = audit(self.trap_payload(40))
        self.assertTrue(result.feasible, result.reason)
        self.assertEqual(result.reachable[0], [400_000])
        # The near turn 390000 must be pruned: it cannot reach segment 3.
        self.assertEqual(result.reachable[1], [750_000])
        self.assertEqual(result.reachable[2], [710_000])

    def test_trap_blocks_when_far_turn_just_out_of_reach(self):
        result = audit(self.trap_payload(30))
        self.assertFalse(result.feasible)
        self.assertEqual(result.failed_segment_index, 3)
        self.assertEqual(result.axis, AxisKind.AZIMUTH)


class ElevationTests(unittest.TestCase):
    def test_elevation_limit_blocks(self):
        payload = base_payload(
            max_azimuth_speed_mdeg_per_ms=10_000,
            max_elevation_speed_mdeg_per_ms=10,
            segments=[
                seg(1, 0, 1_000, 0, 0),
                seg(2, 2_000, 3_000, 0, 20_000),
            ],
        )
        result = audit(payload)
        self.assertFalse(result.feasible)
        self.assertEqual(result.failed_segment_index, 2)
        self.assertEqual(result.axis, AxisKind.ELEVATION)

    def test_both_axes_block_same_gap(self):
        payload = base_payload(
            initial={"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
            azimuth_wrap_min_mdeg=-10_000,
            azimuth_wrap_max_mdeg=10_000,
            max_azimuth_speed_mdeg_per_ms=1,
            max_elevation_speed_mdeg_per_ms=1,
            segments=[seg(1, 10, 20, 180_000, 50_000)],
        )
        result = audit(payload)
        self.assertFalse(result.feasible)
        self.assertEqual(result.failed_segment_index, 1)
        self.assertEqual(result.axis, AxisKind.BOTH)

    def test_elevation_feasible_at_exact_limit(self):
        payload = base_payload(
            max_elevation_speed_mdeg_per_ms=10,
            segments=[
                seg(1, 0, 1_000, 0, 0),
                seg(2, 2_000, 3_000, 0, 10_000),
            ],
        )
        result = audit(payload)
        self.assertTrue(result.feasible)


class ZeroGapTests(unittest.TestCase):
    def test_touching_segments_same_pose_ok(self):
        payload = base_payload(
            max_azimuth_speed_mdeg_per_ms=1_000,
            segments=[
                seg(1, 100, 1_000, 10_000),
                seg(2, 1_000, 2_000, 10_000),
            ],
        )
        result = audit(payload)
        self.assertTrue(result.feasible)

    def test_touching_segments_different_pose_blocks(self):
        payload = base_payload(
            max_azimuth_speed_mdeg_per_ms=1_000,
            segments=[
                seg(1, 100, 1_000, 10_000),
                seg(2, 1_000, 2_000, 20_000),
            ],
        )
        result = audit(payload)
        self.assertFalse(result.feasible)
        self.assertEqual(result.failed_segment_index, 2)
        self.assertEqual(result.axis, AxisKind.AZIMUTH)


class FullTurnDirectionTests(unittest.TestCase):
    def test_physical_azimuth_can_decrease_and_increase_turns(self):
        payload = base_payload(
            initial={"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
            azimuth_wrap_min_mdeg=-720_000,
            azimuth_wrap_max_mdeg=720_000,
            max_azimuth_speed_mdeg_per_ms=1_000,
            max_elevation_speed_mdeg_per_ms=1_000_000,
            segments=[
                # gap 360 ms: can move one full turn but not two.
                seg(1, 360, 400, 0),
                seg(2, 760, 800, 0),
            ],
        )
        result = audit(payload)
        self.assertTrue(result.feasible)
        self.assertEqual(result.reachable[0], [-360_000, 0, 360_000])
        # From +/-360000 the second move can reach +/-720000 as well.
        self.assertEqual(
            result.reachable[1],
            [-720_000, -360_000, 0, 360_000, 720_000],
        )


class ValidationTests(unittest.TestCase):
    def test_overlapping_segments_rejected(self):
        payload = base_payload(
            segments=[seg(1, 0, 2_000, 0), seg(2, 1_000, 3_000, 0)]
        )
        with self.assertRaises(ValidationError):
            audit(payload)

    def test_duplicate_ids_rejected(self):
        payload = base_payload(
            segments=[seg(1, 0, 10, 0), seg(1, 10, 20, 0)]
        )
        with self.assertRaises(ValidationError):
            audit(payload)

    def test_too_many_segments_rejected(self):
        segments = [
            seg(i + 1, i * 100, i * 100 + 50, 0) for i in range(201)
        ]
        with self.assertRaises(ValidationError):
            audit(base_payload(segments=segments))

    def test_empty_segments_rejected(self):
        with self.assertRaises(ValidationError):
            audit(base_payload())

    def test_non_integer_rejected(self):
        payload = base_payload(segments=[seg(1, 0, 10, 0.5)])
        with self.assertRaises(ValidationError):
            audit(payload)

    def test_initial_outside_wrap_rejected(self):
        payload = base_payload(
            initial={"time_ms": 0, "azimuth_mdeg": 400_000, "elevation_mdeg": 0},
        )
        payload["segments"] = [seg(1, 0, 10, 0)]
        with self.assertRaises(ValidationError):
            audit(payload)

    def test_non_positive_speed_rejected(self):
        payload = base_payload(segments=[seg(1, 0, 10, 0)])
        payload["max_azimuth_speed_mdeg_per_ms"] = 0
        with self.assertRaises(ValidationError):
            audit(payload)

    def test_unordered_input_is_sorted_but_reports_sequence(self):
        payload = base_payload(
            max_azimuth_speed_mdeg_per_ms=10,
            segments=[
                seg(2, 3_000, 4_000, 350_000),
                seg(1, 1_000, 2_000, 0),
            ],
        )
        result = audit(payload)
        self.assertTrue(result.feasible)
        # Results follow the submitted time order after sorting: seg1 then 2.
        self.assertEqual(len(result.reachable), 2)


if __name__ == "__main__":
    unittest.main()
