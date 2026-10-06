"""verify 一次性服务。

职责（见 README）：

1. **核对代码**：编译全部源码并运行单元测试；
2. **核对构建/运行态**：轮询服务健康检查；
3. **提交可行计划与受阻计划**：通过 HTTP 调用
   ``POST /api/tracking-plans/audit``，断言审计结论、各段仍可达物理方位集合、
   最早受阻段号以及方位/俯仰/双轴原因；
4. 以进程退出码报告结果：0 全部通过，1 存在失败项。

环境变量：

* ``BASE_URL``：被测服务地址，默认 ``http://localhost:8080``
  （docker compose 中为 ``http://api:8080``）；
* ``HEALTH_TIMEOUT_S``：等待健康检查的最长秒数，默认 30。
"""

from __future__ import annotations

import compileall
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

# 以脚本方式运行（python scripts/verify.py）时，把项目根加入 sys.path，
# 使 tests 能够 import app、compileall 能定位目录。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
HEALTH_TIMEOUT_S = float(os.environ.get("HEALTH_TIMEOUT_S", "30"))

CYCLE = 360_000

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)


def check_code() -> None:
    ok = compileall.compile_dir("app", quiet=1) and compileall.compile_dir(
        "scripts", quiet=1
    )
    record("代码编译（compileall）", bool(ok))

    loader = unittest.TestLoader()
    suite = loader.discover("tests")
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    test_ok = runner.run(suite).wasSuccessful()
    record("单元测试（含暴力对拍）", test_ok)


def wait_healthy() -> bool:
    deadline = time.time() + HEALTH_TIMEOUT_S
    last_err = "尚未尝试"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
                last_err = f"HTTP {resp.status}"
        except (urllib.error.URLError, OSError) as exc:
            last_err = str(exc)
        time.sleep(1)
    record("服务健康检查（GET /health）", False, last_err)
    return False


def post_audit(payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/api/tracking-plans/audit",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def seg(sid, start, end, az, el=0):
    return {
        "id": sid,
        "start_ms": start,
        "end_ms": end,
        "azimuth_mdeg": az,
        "elevation_mdeg": el,
    }


def feasible_greedy_trap():
    """贪心选最近转角会失败、但存在全局首尾相接链路的计划。"""
    return {
        "initial": {
            "time_ms": 0,
            "physical_azimuth_mdeg": -90000,
            "elevation_mdeg": 0,
        },
        "wrap_range": {"min_mdeg": -720000, "max_mdeg": 0},
        "speed_limits": {"azimuth_mdeg_per_ms": 1, "elevation_mdeg_per_ms": 1000},
        "segments": [
            seg(101, 400000, 500000, 270000),
            seg(102, 600000, 700000, 10000),
        ],
    }


def blocked_azimuth_plan():
    """同陷阱但第一段空档不足：段2 方位轴无法到达。"""
    p = feasible_greedy_trap()
    p["segments"][0]["start_ms"] = 359999
    return p


def blocked_elevation_plan():
    return {
        "initial": {"time_ms": 0, "physical_azimuth_mdeg": 0,
                    "elevation_mdeg": 0},
        "wrap_range": {"min_mdeg": -CYCLE, "max_mdeg": CYCLE},
        "speed_limits": {"azimuth_mdeg_per_ms": 1000,
                         "elevation_mdeg_per_ms": 1},
        "segments": [seg(1, 1000, 2000, 0, el=5000)],
    }


def blocked_both_plan():
    return {
        "initial": {"time_ms": 0, "physical_azimuth_mdeg": 0,
                    "elevation_mdeg": 0},
        "wrap_range": {"min_mdeg": -CYCLE, "max_mdeg": CYCLE},
        "speed_limits": {"azimuth_mdeg_per_ms": 1,
                         "elevation_mdeg_per_ms": 1},
        "segments": [seg(1, 1000, 2000, 200000, el=99999)],
    }


def check_feasible() -> None:
    status, body = post_audit(feasible_greedy_trap())
    ok = (
        status == 200
        and body.get("feasible") is True
        and [s["reachable_physical_azimuth"] for s in body["segments"]]
        == [[-450000], [-350000]]
    )
    record("提交可行计划（贪心陷阱，集合传播放行）", ok, json.dumps(body, ensure_ascii=False))


def check_blocked(name: str, payload: dict, expect_id: object,
                  expect_index: int, expect_reason: str) -> None:
    status, body = post_audit(payload)
    ok = (
        status == 200
        and body.get("feasible") is False
        and body.get("blocked_segment_id") == expect_id
        and body.get("blocked_index") == expect_index
        and body.get("reason") == expect_reason
    )
    record(name, ok, json.dumps(body, ensure_ascii=False))


def check_bad_request() -> None:
    status, body = post_audit({"segments": []})
    record("非法计划返回 400", status == 400 and "error" in body,
           f"HTTP {status} {body}")


def main() -> int:
    print(f"== verify 开始，目标服务 {BASE_URL} ==", flush=True)
    check_code()
    if not wait_healthy():
        return summarize()
    check_feasible()
    check_blocked("提交方位轴受阻计划", blocked_azimuth_plan(),
                  expect_id=102, expect_index=2, expect_reason="azimuth")
    check_blocked("提交俯仰轴受阻计划", blocked_elevation_plan(),
                  expect_id=1, expect_index=1, expect_reason="elevation")
    check_blocked("提交双轴共同受阻计划", blocked_both_plan(),
                  expect_id=1, expect_index=1, expect_reason="both")
    check_bad_request()
    return summarize()


def summarize() -> int:
    total = len(results)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"== verify 汇总：{total - failed}/{total} 通过 ==", flush=True)
    if failed:
        print(f"失败 {failed} 项，退出码 1", flush=True)
        return 1
    print("全部通过，退出码 0", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
