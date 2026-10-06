"""基于标准库的 HTTP 服务：POST /api/tracking-plans/audit。

* 计划可行 / 受阻都是业务层面的审计结果，返回 200（body 中 ``feasible`` 区分）；
* 请求格式或语义非法返回 400；
* GET /health 供容器健康检查使用。
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .audit import ValidationError, audit_plan

AUDIT_PATH = "/api/tracking-plans/audit"
HEALTH_PATH = "/health"
MAX_BODY_BYTES = 1_000_000

logger = logging.getLogger("tracking-plan")


class Handler(BaseHTTPRequestHandler):
    server_version = "TrackingPlanAudit/1.0"

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
        if self.path.split("?", 1)[0] == HEALTH_PATH:
            self._write_json(200, {"status": "ok"})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != AUDIT_PATH:
            self._write_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(400, {"error": "非法的 Content-Length"})
            return
        if length <= 0:
            self._write_json(400, {"error": "请求体为空"})
            return
        if length > MAX_BODY_BYTES:
            self._write_json(413, {"error": "请求体过大"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": f"请求体不是合法 JSON：{exc}"})
            return

        try:
            result = audit_plan(payload)
        except ValidationError as exc:
            self._write_json(400, {"error": str(exc)})
            return
        except Exception:  # noqa: BLE001 - 兜底，保证服务不被单请求打崩
            logger.exception("审计过程中发生未预期错误")
            self._write_json(500, {"error": "服务器内部错误"})
            return

        self._write_json(200, result)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    logger.info("审计服务监听 %s:%s", host, port)
    return server


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = create_server(host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
