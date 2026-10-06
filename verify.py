"""One-shot verification job.

Steps:
  1. "Check the code": run the bundled unit-test suite.
  2. "Check the build": the fact that this code runs proves the image was
     built; we additionally wait for the API health endpoint.
  3. Submit a FEASIBLE plan and assert the reachable turn sets, then submit
     BLOCKED plans (azimuth-only, elevation-only and both-axes failures) and
     assert the reported earliest segment and offending axis.

Exit code 0 means everything matched; non-zero means verification failed.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import unittest

SERVICE_URL = os.environ.get("SERVICE_URL", "http://127.0.0.1:8080")
AUDIT_URL = f"{SERVICE_URL}/api/tracking-plans/audit"
HEALTH_URL = f"{SERVICE_URL}/health"


def seg(seg_id, start, end, az, el=0):
    return {
        "id": seg_id,
        "start_ms": start,
        "end_ms": end,
        "azimuth_mdeg": az,
        "elevation_mdeg": el,
    }


def post_audit(body: dict) -> dict:
    req = urllib.request.Request(
        AUDIT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status != 200:
            raise RuntimeError(f"audit returned HTTP {resp.status}")
        return json.loads(resp.read())


def wait_for_healthy(timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"service never became healthy: {last_error}")


def check(name: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    return condition


def run_unit_tests() -> bool:
    print("== 1/3 running unit-test suite ==", flush=True)
    loader = unittest.TestLoader()
    suite = loader.discover("tests")
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    return result.wasSuccessful()


def main() -> int:
    all_ok = True

    all_ok &= run_unit_tests()

    print(f"== 2/3 waiting for API at {SERVICE_URL} ==", flush=True)
    try:
        wait_for_healthy()
        print("[PASS] API healthy", flush=True)
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", flush=True)
        return 1

    print("== 3/3 submitting plans ==", flush=True)

    # --- Feasible plan: logical 350000 has two physical turns ----------
    feasible_body = {
        "initial": {"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
        "azimuth_wrap_min_mdeg": -360_000,
        "azimuth_wrap_max_mdeg": 360_000,
        "max_azimuth_speed_mdeg_per_ms": 10_000,
        "max_elevation_speed_mdeg_per_ms": 10_000,
        "segments": [seg(1, 1_000, 2_000, 350_000)],
    }
    data = post_audit(feasible_body)
    ok = data.get("feasible") is True and data["segments"][0][
        "reachable_physical_azimuths_mdeg"
    ] == [-10_000, 350_000]
    all_ok &= check(
        "feasible plan accepted with both reachable physical turns",
        ok,
        json.dumps(data),
    )

    # --- Cable-wrap trap: nearest-turn greedy would accept, global view
    # must still find the chain; tighten the final gap to block it. -----
    def trap(gap3_ms: int) -> dict:
        return {
            "initial": {
                "time_ms": 0,
                "azimuth_mdeg": 400_000,
                "elevation_mdeg": 0,
            },
            "azimuth_wrap_min_mdeg": 380_000,
            "azimuth_wrap_max_mdeg": 760_000,
            "max_azimuth_speed_mdeg_per_ms": 1_000,
            "max_elevation_speed_mdeg_per_ms": 1_000_000,
            "segments": [
                seg(1, 0, 1_000, 40_000),
                seg(2, 1_350, 1_700, 30_000),
                seg(3, 1_700 + gap3_ms, 2_000 + gap3_ms, 350_000),
            ],
        }

    data = post_audit(trap(40))
    ok = (
        data.get("feasible") is True
        and data["segments"][1]["reachable_physical_azimuths_mdeg"] == [750_000]
    )
    all_ok &= check(
        "wrap-trap plan feasible only via the far (non-greedy) turn",
        ok,
        json.dumps(data),
    )

    data = post_audit(trap(30))
    ok = (
        data.get("feasible") is False
        and data.get("failed_segment_index") == 3
        and data.get("failed_segment_id") == 3
        and data.get("axis") == "azimuth"
    )
    all_ok &= check(
        "blocked plan reports earliest dead segment with axis=azimuth",
        ok,
        json.dumps(data),
    )

    # --- Elevation-only failure ---------------------------------------
    elevation_body = {
        "initial": {"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
        "azimuth_wrap_min_mdeg": -360_000,
        "azimuth_wrap_max_mdeg": 360_000,
        "max_azimuth_speed_mdeg_per_ms": 10_000,
        "max_elevation_speed_mdeg_per_ms": 1,
        "segments": [
            seg(1, 100, 1_000, 0, 0),
            seg(2, 2_000, 3_000, 0, 50_000),
        ],
    }
    data = post_audit(elevation_body)
    ok = (
        data.get("feasible") is False
        and data.get("failed_segment_index") == 2
        and data.get("axis") == "elevation"
    )
    all_ok &= check(
        "elevation overrun reports axis=elevation at segment 2",
        ok,
        json.dumps(data),
    )

    # --- Both axes fail in the same gap -------------------------------
    both_body = {
        "initial": {"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
        "azimuth_wrap_min_mdeg": -10_000,
        "azimuth_wrap_max_mdeg": 10_000,
        "max_azimuth_speed_mdeg_per_ms": 1,
        "max_elevation_speed_mdeg_per_ms": 1,
        "segments": [seg(1, 10, 20, 180_000, 50_000)],
    }
    data = post_audit(both_body)
    ok = (
        data.get("feasible") is False
        and data.get("failed_segment_index") == 1
        and data.get("axis") == "both"
    )
    all_ok &= check("both-axes overrun reports axis=both", ok, json.dumps(data))

    print("=" * 60, flush=True)
    if all_ok:
        print("VERIFY RESULT: SUCCESS (exit 0)", flush=True)
        return 0
    print("VERIFY RESULT: FAILURE (exit 1)", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
