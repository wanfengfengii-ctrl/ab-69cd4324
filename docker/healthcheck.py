"""Container health check: exits 0 only when the audit API is live."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PORT = int(os.environ.get("PORT", "8080"))
URL = f"http://127.0.0.1:{PORT}/health"

MIN_PLAN = {
    "initial": {"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
    "azimuth_wrap_min_mdeg": -360_000,
    "azimuth_wrap_max_mdeg": 360_000,
    "max_azimuth_speed_mdeg_per_ms": 10_000,
    "max_elevation_speed_mdeg_per_ms": 10_000,
    "segments": [
        {
            "id": 1,
            "start_ms": 1_000,
            "end_ms": 2_000,
            "azimuth_mdeg": 10_000,
            "elevation_mdeg": 0,
        }
    ],
}


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=2) as resp:
            if resp.status != 200:
                return 1
    except (OSError, urllib.error.URLError):
        return 1

    # Also exercise the audit endpoint so "healthy" means the core works.
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/tracking-plans/audit",
        data=json.dumps(MIN_PLAN).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            payload = json.loads(resp.read())
            return 0 if resp.status == 200 and payload.get("feasible") else 1
    except (OSError, urllib.error.URLError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
