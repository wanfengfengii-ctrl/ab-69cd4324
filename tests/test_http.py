"""End-to-end tests against the real HTTP server (no network, in-process)."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from app.main import AUDIT_PATH, AuditHandler


def seg(seg_id, start, end, az, el=0):
    return {
        "id": seg_id,
        "start_ms": start,
        "end_ms": end,
        "azimuth_mdeg": az,
        "elevation_mdeg": el,
    }


BASE = {
    "initial": {"time_ms": 0, "azimuth_mdeg": 0, "elevation_mdeg": 0},
    "azimuth_wrap_min_mdeg": -360_000,
    "azimuth_wrap_max_mdeg": 360_000,
    "max_azimuth_speed_mdeg_per_ms": 10_000,
    "max_elevation_speed_mdeg_per_ms": 10_000,
}


class HttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AuditHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{AUDIT_PATH}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_health(self):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/health", timeout=5
        ) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read())["status"], "ok")

    def test_feasible_plan_response_shape(self):
        body = dict(BASE, segments=[seg(1, 1_000, 2_000, 350_000)])
        status, data = self._post(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["feasible"])
        self.assertEqual(
            data["segments"][0]["reachable_physical_azimuths_mdeg"],
            [-10_000, 350_000],
        )

    def test_blocked_plan_names_axis_and_segment(self):
        body = dict(
            BASE,
            max_elevation_speed_mdeg_per_ms=1,
            segments=[
                seg(1, 100, 1_000, 0, 0),
                seg(2, 2_000, 3_000, 0, 50_000),
            ],
        )
        status, data = self._post(body)
        self.assertEqual(status, 200)
        self.assertFalse(data["feasible"])
        self.assertEqual(data["failed_segment_index"], 2)
        self.assertEqual(data["failed_segment_id"], 2)
        self.assertEqual(data["axis"], "elevation")

    def test_invalid_json_returns_400(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{AUDIT_PATH}",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
