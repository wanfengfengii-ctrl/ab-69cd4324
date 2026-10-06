"""HTTP service exposing POST /api/tracking-plans/audit.

Pure standard library so the image is tiny and reproducible.  The listen
host/port come from the HOST/PORT environment variables.
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.plan import AxisKind, ValidationError, audit

AUDIT_PATH = "/api/tracking-plans/audit"
MAX_BODY_BYTES = 2 * 1024 * 1024


def _audit_response(payload: Any) -> tuple[int, dict]:
    try:
        result = audit(payload)
    except ValidationError as exc:
        return HTTPStatus.BAD_REQUEST, {
            "feasible": None,
            "error": "invalid_request",
            "message": str(exc),
        }

    if result.feasible:
        return HTTPStatus.OK, {
            "feasible": True,
            "segments": [
                {
                    "index": i + 1,
                    "reachable_physical_azimuths_mdeg": turns,
                }
                for i, turns in enumerate(result.reachable)
            ],
            "reachable_physical_azimuths_mdeg": result.reachable,
        }

    return HTTPStatus.OK, {
        "feasible": False,
        "failed_segment_index": result.failed_segment_index,
        "failed_segment_id": result.failed_segment_id,
        "axis": result.axis.value if result.axis else None,
        "message": result.reason,
    }


class AuditHandler(BaseHTTPRequestHandler):
    server_version = "TrackingPlanAudit/1.0"

    def _write_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz", "/"):
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(
            HTTPStatus.NOT_FOUND, {"error": "not_found", "message": self.path}
        )

    def do_POST(self) -> None:
        if self.path != AUDIT_PATH:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "message": self.path},
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "feasible": None,
                    "error": "invalid_request",
                    "message": "request body must be a JSON object",
                },
            )
            return
        if length > MAX_BODY_BYTES:
            self._write_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "body_too_large", "message": "max 2 MiB"},
            )
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "feasible": None,
                    "error": "invalid_request",
                    "message": f"body is not valid JSON: {exc}",
                },
            )
            return

        status, body = _audit_response(payload)
        self._write_json(status, body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep container logs concise; the audit endpoint is noisy otherwise.
        if self.path not in ("/health", "/healthz"):
            super().log_message(fmt, *args)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), AuditHandler)
    print(f"tracking-plan audit listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
