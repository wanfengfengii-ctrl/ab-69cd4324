"""Tracking-plan audit core.

All angles are integer milli-degrees, all times integer milliseconds and
all speeds integer milli-degrees per millisecond.

A target *logical* azimuth ``a`` (0..359999) may be observed from any
*physical* azimuth ``a + 360000 * k`` that lies inside the closed cable
wrap interval.  Choosing the nearest turn segment by segment is wrong: a
locally reachable turn can strand the antenna with no continuation.  The
audit therefore keeps, for every segment, the full set of physical turns
that are reachable from the initial pose AND that can still reach every
later segment (forward/backward propagation over turn sets).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from enum import Enum
from typing import Optional

FULL_TURN = 360_000
AZ_MAX = 359_999
# A physical wrap spanning more turns than this is rejected as invalid;
# real cable wraps only span a few turns and unbounded enumeration would
# let a huge interval exhaust memory.
MAX_TURNS_PER_SEGMENT = 4096


class AxisKind(str, Enum):
    AZIMUTH = "azimuth"
    ELEVATION = "elevation"
    BOTH = "both"


class ValidationError(ValueError):
    """Raised when the request payload is structurally invalid."""


@dataclass(frozen=True)
class Segment:
    id: int
    start_ms: int
    end_ms: int
    azimuth_mdeg: int
    elevation_mdeg: int


@dataclass(frozen=True)
class AuditResult:
    feasible: bool
    # On success: for each segment, the physical azimuths (mdeg) that take
    # part in at least one head-to-tail feasible assignment.
    reachable: list[list[int]]
    # On failure: 1-based index (into the submitted, time-ordered list) of
    # the earliest segment that loses every reachable physical azimuth.
    failed_segment_index: Optional[int] = None
    failed_segment_id: Optional[int] = None
    axis: Optional[AxisKind] = None
    reason: Optional[str] = None


def _is_int(value: object) -> bool:
    # bool is a subclass of int; reject it explicitly.
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(value: object, field: str) -> int:
    if not _is_int(value):
        raise ValidationError(f"{field} must be an integer")
    return value


def _parse_segments(raw: object) -> list[Segment]:
    if not isinstance(raw, list):
        raise ValidationError("segments must be a list")
    if not 1 <= len(raw) <= 200:
        raise ValidationError("segments must contain between 1 and 200 entries")

    segments: list[Segment] = []
    seen_ids: set[int] = set()
    for i, item in enumerate(raw, start=1):
        where = f"segments[{i - 1}]"
        if not isinstance(item, dict):
            raise ValidationError(f"{where} must be an object")

        missing = [
            key
            for key in ("id", "start_ms", "end_ms", "azimuth_mdeg", "elevation_mdeg")
            if key not in item
        ]
        if missing:
            raise ValidationError(f"{where} missing fields: {', '.join(missing)}")

        seg_id = _require_int(item["id"], f"{where}.id")
        if seg_id in seen_ids:
            raise ValidationError(f"duplicate segment id: {seg_id}")
        seen_ids.add(seg_id)

        start_ms = _require_int(item["start_ms"], f"{where}.start_ms")
        end_ms = _require_int(item["end_ms"], f"{where}.end_ms")
        azimuth = _require_int(item["azimuth_mdeg"], f"{where}.azimuth_mdeg")
        elevation = _require_int(item["elevation_mdeg"], f"{where}.elevation_mdeg")

        if start_ms < 0:
            raise ValidationError(f"{where}.start_ms must be >= 0")
        if end_ms <= start_ms:
            raise ValidationError(f"{where}.end_ms must be greater than start_ms")
        if not 0 <= azimuth <= AZ_MAX:
            raise ValidationError(
                f"{where}.azimuth_mdeg must be within [0, 359999]"
            )

        segments.append(
            Segment(
                id=seg_id,
                start_ms=start_ms,
                end_ms=end_ms,
                azimuth_mdeg=azimuth,
                elevation_mdeg=elevation,
            )
        )

    # Time ordering with non-overlap: each segment must end before (or at)
    # the start of the next one.  A zero-length common gap is allowed only
    # when consecutive segments touch; no slew is then possible between
    # them unless both poses coincide.
    ordered = sorted(segments, key=lambda s: (s.start_ms, s.end_ms))
    for prev, curr in zip(ordered, ordered[1:]):
        if curr.start_ms < prev.end_ms:
            raise ValidationError(
                "segments must not overlap: "
                f"segment {prev.id} overlaps segment {curr.id}"
            )
    return ordered


def parse_request(payload: object) -> dict:
    """Validate a decoded JSON payload and return a normalized dict."""

    if not isinstance(payload, dict):
        raise ValidationError("request body must be a JSON object")

    required = (
        "initial",
        "azimuth_wrap_min_mdeg",
        "azimuth_wrap_max_mdeg",
        "max_azimuth_speed_mdeg_per_ms",
        "max_elevation_speed_mdeg_per_ms",
        "segments",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValidationError(f"missing fields: {', '.join(missing)}")

    initial = payload["initial"]
    if not isinstance(initial, dict):
        raise ValidationError("initial must be an object")
    for key in ("time_ms", "azimuth_mdeg", "elevation_mdeg"):
        if key not in initial:
            raise ValidationError(f"initial missing field: {key}")
    t0 = _require_int(initial["time_ms"], "initial.time_ms")
    az0 = _require_int(initial["azimuth_mdeg"], "initial.azimuth_mdeg")
    el0 = _require_int(initial["elevation_mdeg"], "initial.elevation_mdeg")
    if t0 < 0:
        raise ValidationError("initial.time_ms must be >= 0")

    wrap_min = _require_int(
        payload["azimuth_wrap_min_mdeg"], "azimuth_wrap_min_mdeg"
    )
    wrap_max = _require_int(
        payload["azimuth_wrap_max_mdeg"], "azimuth_wrap_max_mdeg"
    )
    if wrap_min > wrap_max:
        raise ValidationError(
            "azimuth_wrap_min_mdeg must not exceed azimuth_wrap_max_mdeg"
        )
    if wrap_max - wrap_min > MAX_TURNS_PER_SEGMENT * FULL_TURN:
        raise ValidationError(
            "cable wrap range is implausibly wide "
            f"(max {MAX_TURNS_PER_SEGMENT} full turns)"
        )

    vaz = _require_int(
        payload["max_azimuth_speed_mdeg_per_ms"],
        "max_azimuth_speed_mdeg_per_ms",
    )
    vel = _require_int(
        payload["max_elevation_speed_mdeg_per_ms"],
        "max_elevation_speed_mdeg_per_ms",
    )
    if vaz <= 0 or vel <= 0:
        raise ValidationError("speed limits must be positive integers")

    segments = _parse_segments(payload["segments"])
    if segments[0].start_ms < t0:
        raise ValidationError(
            "first segment must not start before the initial time"
        )

    # Initial physical azimuth must itself be a legal cable position.
    if not wrap_min <= az0 <= wrap_max:
        raise ValidationError(
            "initial.azimuth_mdeg must lie within the closed cable wrap range"
        )

    return {
        "t0": t0,
        "az0": az0,
        "el0": el0,
        "wrap_min": wrap_min,
        "wrap_max": wrap_max,
        "vaz": vaz,
        "vel": vel,
        "segments": segments,
    }


def _candidate_turns(
    logical_az: int, wrap_min: int, wrap_max: int
) -> list[int]:
    """All physical azimuths congruent to ``logical_az`` within the wrap."""
    k_min = (wrap_min - logical_az + FULL_TURN - 1) // FULL_TURN
    k_max = (wrap_max - logical_az) // FULL_TURN
    return [logical_az + FULL_TURN * k for k in range(k_min, k_max + 1)]


def _azimuth_transitions(
    sources: list[int],
    targets: list[int],
    gap_ms: int,
    max_speed: int,
) -> list[bool]:
    """targets[j] is reachable from any source within ``max_speed * gap``."""
    budget = max_speed * gap_ms
    reachable = [False] * len(targets)
    # Both lists are sorted ascending; a two-pointer window keeps this linear.
    lo = 0
    hi = -1
    for src in sources:
        lower = src - budget
        upper = src + budget
        while lo < len(targets) and targets[lo] < lower:
            lo += 1
        if hi < lo:
            hi = lo - 1
        while hi + 1 < len(targets) and targets[hi + 1] <= upper:
            hi += 1
        for j in range(lo, hi + 1):
            reachable[j] = True
    return reachable


def audit(payload: object) -> AuditResult:
    data = parse_request(payload)

    segments: list[Segment] = data["segments"]
    wrap_min: int = data["wrap_min"]
    wrap_max: int = data["wrap_max"]
    vaz: int = data["vaz"]
    vel: int = data["vel"]
    n = len(segments)

    # Candidate physical azimuth per segment, sorted ascending.
    candidates: list[list[int]] = [
        _candidate_turns(seg.azimuth_mdeg, wrap_min, wrap_max)
        for seg in segments
    ]

    # Gap duration (ms) immediately preceding each segment.
    gaps: list[int] = []
    prev_end = data["t0"]
    for i, seg in enumerate(segments):
        start = seg.start_ms
        if i == 0:
            gaps.append(start - prev_end)
        else:
            gaps.append(start - segments[i - 1].end_ms)
        prev_end = seg.end_ms

    # Elevation feasibility of each preceding gap: independent of turns.
    elevation_ok: list[bool] = []
    prev_el = data["el0"]
    for i, seg in enumerate(segments):
        delta = abs(seg.elevation_mdeg - prev_el)
        elevation_ok.append(delta <= vel * gaps[i])
        prev_el = seg.elevation_mdeg

    def _fail(idx: int, axis: AxisKind, detail: str) -> AuditResult:
        seg = segments[idx]
        return AuditResult(
            feasible=False,
            reachable=[],
            failed_segment_index=idx + 1,
            failed_segment_id=seg.id,
            axis=axis,
            reason=detail,
        )

    # ---- Forward propagation over azimuth turns -----------------------
    # forward[i]: bits over candidates[i] reachable from the initial pose.
    forward: list[int] = []
    for i, cand in enumerate(candidates):
        gap = gaps[i]
        if i == 0:
            budget = vaz * gap
            bits = 0
            for j, phys in enumerate(cand):
                if abs(phys - data["az0"]) <= budget:
                    bits |= 1 << j
        else:
            prev_cand = candidates[i - 1]
            prev_mask = forward[i - 1]
            sources = [
                prev_cand[j] for j in range(len(prev_cand)) if prev_mask >> j & 1
            ]
            marks = _azimuth_transitions(sources, cand, gap, vaz)
            bits = 0
            for j, ok in enumerate(marks):
                if ok:
                    bits |= 1 << j
        forward.append(bits)

        if bits == 0:
            if not elevation_ok[i]:
                axis = AxisKind.BOTH
                detail = (
                    "segment loses every reachable physical azimuth and the "
                    "elevation slew exceeds its speed limit in the preceding gap"
                )
            else:
                axis = AxisKind.AZIMUTH
                detail = (
                    "segment loses every reachable physical azimuth: no turn "
                    "within cable wrap can be reached in the preceding gap"
                )
            return _fail(i, axis, detail)
        if not elevation_ok[i]:
            return _fail(
                i,
                AxisKind.ELEVATION,
                "elevation slew into segment exceeds the elevation speed "
                "limit in the preceding gap",
            )

    # ---- Backward propagation: a turn counts only if the rest of the
    # plan is still completable from it --------------------------------
    backward: list[int] = [0] * n
    for i in range(n - 1, -1, -1):
        cand = candidates[i]
        if i == n - 1:
            mask = (1 << len(cand)) - 1
        else:
            next_cand = candidates[i + 1]
            budget = vaz * gaps[i + 1]
            live_next = backward[i + 1]
            mask = 0
            for j, phys in enumerate(cand):
                lo = bisect_left(next_cand, phys - budget)
                hi = bisect_right(next_cand, phys + budget)
                window = live_next >> lo & ((1 << (hi - lo)) - 1) if hi > lo else 0
                if window:
                    mask |= 1 << j
        backward[i] = mask

    # Forward already guarantees non-emptiness; intersect with backward so
    # the reported set contains exactly the turns on some feasible chain.
    reachable: list[list[int]] = []
    for i, cand in enumerate(candidates):
        live = forward[i] & backward[i]
        if live == 0:
            # Defensive: forward non-empty but backward empty means the last
            # forward-reachable segment cannot be extended to the end.
            return _fail(
                i,
                AxisKind.AZIMUTH,
                "every reachable turn is a dead end: no head-to-tail "
                "assignment of physical azimuths completes the plan",
            )
        reachable.append(
            [cand[j] for j in range(len(cand)) if (live >> j) & 1]
        )

    return AuditResult(feasible=True, reachable=reachable)
