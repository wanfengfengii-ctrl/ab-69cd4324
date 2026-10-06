"""audit 模块单元测试；含与暴力可达性 DP 的随机对拍。"""

from __future__ import annotations

import random
import unittest

from app.audit import (
    CYCLE,
    REASON_AZIMUTH,
    REASON_BOTH,
    REASON_ELEVATION,
    ValidationError,
    _candidate_bounds,
    _expand_interval,
    _k_interval_for_physical_target,
    audit_plan,
)


def seg(sid, start, end, az, el=0):
    return {
        "id": sid,
        "start_ms": start,
        "end_ms": end,
        "azimuth_mdeg": az,
        "elevation_mdeg": el,
    }


def base_payload(**over):
    p = {
        "initial": {"time_ms": 0, "physical_azimuth_mdeg": 0, "elevation_mdeg": 0},
        "wrap_range": {"min_mdeg": -2 * CYCLE, "max_mdeg": 2 * CYCLE},
        "speed_limits": {"azimuth_mdeg_per_ms": 100, "elevation_mdeg_per_ms": 100},
        "segments": [seg(1, 1000, 2000, 0)],
    }
    p.update(over)
    return p


class TestIntervalMath(unittest.TestCase):
    def test_candidate_bounds(self):
        self.assertEqual(_candidate_bounds(270000, -720000, 0), (-2, -1))
        self.assertEqual(_candidate_bounds(10000, -720000, 0), (-2, -1))
        self.assertEqual(_candidate_bounds(0, 0, CYCLE), (0, 1))
        self.assertEqual(_candidate_bounds(0, -CYCLE, 0), (-1, 0))
        # 边界恰好落在端点上
        self.assertEqual(_candidate_bounds(0, 5, 10), (1, 0))  # 空区间

    def test_k_interval_for_physical_target(self):
        # x=0, R=400000, 网格 a=270000：k=-1 -> |-90000|=90000；k=0 -> 270000
        self.assertEqual(_k_interval_for_physical_target(0, 270000, 400000), (-1, 0))
        # R=200000：只剩 k=-1（270000 超出）
        self.assertEqual(_k_interval_for_physical_target(0, 270000, 200000), (-1, -1))
        # 边界等号闭合：x=0,a=100000,R=100000 -> k=0 恰好；k=-1 为 -260000 超预算
        self.assertEqual(_k_interval_for_physical_target(0, 100000, 100000), (0, 0))
        # 再大一周方向也能碰到：R=260000 时 k=-1 恰好闭合
        self.assertEqual(_k_interval_for_physical_target(0, 100000, 260000), (-1, 0))

    def test_expand_interval_vs_enumeration(self):
        rng = random.Random(7)
        for _ in range(500):
            a1 = rng.randrange(CYCLE)
            a2 = rng.randrange(CYCLE)
            lo = rng.randint(-3, 1)
            hi = lo + rng.randint(0, 4)
            R = rng.randint(0, 2 * CYCLE)
            src = [a1 + CYCLE * k for k in range(lo, hi + 1)]
            brute = set()
            for p in src:
                klo, khi = _k_interval_for_physical_target(p, a2, R)
                for k in range(klo, khi + 1):
                    brute.add(k)
            got = _expand_interval(lo, hi, a1, a2, R)
            got_set = set(range(got[0], got[1] + 1))
            # 区间内每个圈号都必须确有源点可达，且不得漏掉任何可达圈号
            self.assertEqual(got_set, brute)


class TestBasicAcceptance(unittest.TestCase):
    def test_single_segment_trivial(self):
        r = audit_plan(base_payload())
        self.assertTrue(r["feasible"])
        # 方位 0，wrap [-720000,720000]：候选 -720000..720000；初值 0、预算 100000
        # 可达 -0? 预算 100*1000=100000：-360000(距离360000)不可；0 可；360000 不可
        self.assertEqual(r["segments"][0]["reachable_physical_azimuth"], [0])

    def test_speed_budget_boundary_inclusive(self):
        # 预算恰好等于转角：闭合，可行
        p = base_payload(
            initial={"time_ms": 0, "physical_azimuth_mdeg": 0, "elevation_mdeg": 0},
            segments=[seg(1, 1000, 2000, 100000)],  # dt=1000, v=100 -> 100000
        )
        r = audit_plan(p)
        self.assertTrue(r["feasible"])
        self.assertIn(100000, r["segments"][0]["reachable_physical_azimuth"])

    def test_failure_reason_elevation_only(self):
        p = base_payload(
            speed_limits={"azimuth_mdeg_per_ms": 1000, "elevation_mdeg_per_ms": 1},
            segments=[seg(1, 1000, 2000, 0, el=5000)],  # 俯仰需 5000，预算 1000
        )
        r = audit_plan(p)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["blocked_index"], 1)
        self.assertEqual(r["blocked_segment_id"], 1)
        self.assertEqual(r["reason"], REASON_ELEVATION)
        self.assertEqual(r["segments"][-1]["reachable_physical_azimuth"], [])

    def test_failure_reason_azimuth_only(self):
        p = base_payload(
            initial={"time_ms": 0, "physical_azimuth_mdeg": 0, "elevation_mdeg": 0},
            speed_limits={"azimuth_mdeg_per_ms": 1, "elevation_mdeg_per_ms": 1000},
            segments=[seg(1, 1000, 2000, 200000)],  # 方位需 160000 起步，预算 1000
        )
        r = audit_plan(p)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["reason"], REASON_AZIMUTH)

    def test_failure_reason_both(self):
        p = base_payload(
            speed_limits={"azimuth_mdeg_per_ms": 1, "elevation_mdeg_per_ms": 1},
            segments=[seg(1, 1000, 2000, 200000, el=99999)],
        )
        r = audit_plan(p)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["reason"], REASON_BOTH)

    def test_earliest_blocked_segment_reported(self):
        # 第 1 段俯仰不可行；即使后续正常，也必须报第 1 段
        p = base_payload(
            speed_limits={"azimuth_mdeg_per_ms": 1000, "elevation_mdeg_per_ms": 1},
            segments=[
                seg(1, 1000, 2000, 0, el=5000),
                seg(2, 3000, 4000, 0, el=0),
            ],
        )
        r = audit_plan(p)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["blocked_index"], 1)
        self.assertEqual(r["blocked_segment_id"], 1)


class TestGreedyTrap(unittest.TestCase):
    """贪心选最近转角会误判、集合传播判可行的核心场景。"""

    def trap_payload(self):
        return {
            "initial": {
                "time_ms": 0,
                "physical_azimuth_mdeg": -90000,  # = 270000 - 360000
                "elevation_mdeg": 0,
            },
            "wrap_range": {"min_mdeg": -720000, "max_mdeg": 0},
            "speed_limits": {
                "azimuth_mdeg_per_ms": 1,
                "elevation_mdeg_per_ms": 1000,
            },
            "segments": [
                seg(1, 400000, 500000, 270000),   # 预算 400000
                seg(2, 600000, 700000, 10000),    # 空档 100000，预算 100000
            ],
        }

    def test_global_feasible_greedy_dies(self):
        r = audit_plan(self.trap_payload())
        self.assertTrue(r["feasible"], r)
        # 段0 在完整链路上的物理方位只剩 -450000（-90000 无法在 100000 内脱离）
        self.assertEqual(
            r["segments"][0]["reachable_physical_azimuth"], [-450000]
        )
        # 段1 链路上的点
        self.assertEqual(
            r["segments"][1]["reachable_physical_azimuth"], [-350000]
        )

    def test_same_shape_but_tight_first_gap_blocks(self):
        # 第一段空档只有 359999：-90000 与 -450000 都无法到达？
        # -90000 自身距离 0 可达，但第二段无法脱离 -> 受阻点在第 2 段
        p = self.trap_payload()
        p["segments"][0]["start_ms"] = 359999
        r = audit_plan(p)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["blocked_index"], 2)
        self.assertEqual(r["reason"], REASON_AZIMUTH)

    def test_shorter_wrap_removes_escape(self):
        # 缆绕范围收紧到 [-360000,0]：段0 候选只有 -90000；
        # 段1 候选只有 -350000，距离 260000 > 100000 -> 受阻
        p = self.trap_payload()
        p["wrap_range"] = {"min_mdeg": -360000, "max_mdeg": 0}
        r = audit_plan(p)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["blocked_segment_id"], 2)


class TestEdgeCases(unittest.TestCase):
    def test_zero_width_wrap_point_match(self):
        # 缆绕退化为单点，且恰为某逻辑方位的等价物理方位
        p = base_payload(
            initial={"time_ms": 0, "physical_azimuth_mdeg": 360000,
                     "elevation_mdeg": 0},
            wrap_range={"min_mdeg": 360000, "max_mdeg": 360000},
            speed_limits={"azimuth_mdeg_per_ms": 1, "elevation_mdeg_per_ms": 1},
            segments=[seg(1, 0, 100, 0)],
        )
        r = audit_plan(p)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["segments"][0]["reachable_physical_azimuth"], [360000])

    def test_zero_width_wrap_no_match_blocks_azimuth(self):
        p = base_payload(
            initial={"time_ms": 0, "physical_azimuth_mdeg": 360000,
                     "elevation_mdeg": 0},
            wrap_range={"min_mdeg": 360000, "max_mdeg": 360000},
            speed_limits={"azimuth_mdeg_per_ms": 1000,
                          "elevation_mdeg_per_ms": 1000},
            segments=[seg(1, 0, 100, 1)],  # 360001/1 等都不等于 360000
        )
        r = audit_plan(p)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["reason"], REASON_AZIMUTH)

    def test_zero_gap_requires_exact_same_position(self):
        # 段间空档为 0：两段逻辑方位必须等价于同一物理方位
        p = {
            "initial": {"time_ms": 0, "physical_azimuth_mdeg": 0,
                        "elevation_mdeg": 0},
            "wrap_range": {"min_mdeg": -CYCLE, "max_mdeg": CYCLE},
            "speed_limits": {"azimuth_mdeg_per_ms": 1000,
                             "elevation_mdeg_per_ms": 1000},
            "segments": [
                seg(1, 0, 100, 0),
                seg(2, 100, 200, 0),   # 同方位：物理 0 保持不动，可行
                seg(3, 200, 300, 180000),  # 零空档却要转半周 -> 方位受阻
            ],
        }
        r = audit_plan(p)
        self.assertFalse(r["feasible"])
        self.assertEqual(r["blocked_segment_id"], 3)
        self.assertEqual(r["reason"], REASON_AZIMUTH)
        # 前两段可达集合保留
        self.assertEqual(
            [s["reachable_physical_azimuth"] for s in r["segments"][:2]],
            [[0], [0]],
        )


class TestValidation(unittest.TestCase):
    def test_bad_types(self):
        with self.assertRaises(ValidationError):
            audit_plan(base_payload(initial="nope"))
        with self.assertRaises(ValidationError):
            audit_plan(base_payload(wrap_range={"min_mdeg": 1.5, "max_mdeg": 2}))

    def test_azimuth_range(self):
        p = base_payload(segments=[seg(1, 1000, 2000, 360000)])
        with self.assertRaises(ValidationError):
            audit_plan(p)
        p = base_payload(segments=[seg(1, 1000, 2000, -1)])
        with self.assertRaises(ValidationError):
            audit_plan(p)

    def test_duplicate_ids(self):
        p = base_payload(segments=[seg(1, 0 + 1, 10, 0), seg(1, 20, 30, 0)])
        p["initial"]["time_ms"] = 0
        with self.assertRaises(ValidationError):
            audit_plan(p)

    def test_overlap_rejected(self):
        p = base_payload(
            segments=[seg(1, 100, 300, 0), seg(2, 200, 400, 0)]
        )
        with self.assertRaises(ValidationError):
            audit_plan(p)

    def test_ids_need_not_be_sorted(self):
        # 只要求按时间排列、id 唯一；id 降序但时间升序是合法的
        p = base_payload(
            segments=[seg(2, 100, 200, 0), seg(1, 300, 400, 0)]
        )
        r = audit_plan(p)
        self.assertTrue(r["feasible"])
        self.assertEqual([s["id"] for s in r["segments"]], [2, 1])

    def test_count_limits(self):
        with self.assertRaises(ValidationError):
            audit_plan(base_payload(segments=[]))
        many = [seg(i, 1000 * i, 1000 * i + 500, 0) for i in range(1, 202)]
        with self.assertRaises(ValidationError):
            audit_plan(base_payload(segments=many))

    def test_negative_speed(self):
        p = base_payload(
            speed_limits={"azimuth_mdeg_per_ms": -1, "elevation_mdeg_per_ms": 1}
        )
        with self.assertRaises(ValidationError):
            audit_plan(p)

    def test_initial_outside_wrap(self):
        p = base_payload(
            initial={"time_ms": 0, "physical_azimuth_mdeg": 999999,
                     "elevation_mdeg": 0}
        )
        with self.assertRaises(ValidationError):
            audit_plan(p)

    def test_bool_rejected(self):
        p = base_payload()
        p["speed_limits"] = {"azimuth_mdeg_per_ms": True,
                             "elevation_mdeg_per_ms": 1}
        with self.assertRaises(ValidationError):
            audit_plan(p)


class TestRandomAgainstBruteForce(unittest.TestCase):
    """随机计划：区间传播结果必须与枚举所有物理方位的暴力 DP 完全一致。"""

    @staticmethod
    def _brute(payload):
        init = payload["initial"]
        wlo = payload["wrap_range"]["min_mdeg"]
        whi = payload["wrap_range"]["max_mdeg"]
        vaz = payload["speed_limits"]["azimuth_mdeg_per_ms"]
        vel = payload["speed_limits"]["elevation_mdeg_per_ms"]
        segs = payload["segments"]

        def cands(a):
            k0 = -((a - wlo) // CYCLE)
            k1 = (whi - a) // CYCLE
            return [a + CYCLE * k for k in range(k0, k1 + 1)]

        grids = [cands(s["azimuth_mdeg"]) for s in segs]
        dts = [segs[0]["start_ms"] - init["time_ms"]]
        for i in range(1, len(segs)):
            dts.append(segs[i]["start_ms"] - segs[i - 1]["end_ms"])

        def el_ok(i):
            prev_e = init["elevation_mdeg"] if i == 0 else segs[i - 1]["elevation_mdeg"]
            return abs(segs[i]["elevation_mdeg"] - prev_e) <= vel * dts[i]

        # 方位可达性独立传播（不受俯仰影响），用于独立判定两轴超限
        az_reach = [{p for p in grids[0]
                     if abs(p - init["physical_azimuth_mdeg"]) <= vaz * dts[0]}]
        for i in range(1, len(segs)):
            az_reach.append({p for p in grids[i]
                             if any(abs(p - q) <= vaz * dts[i]
                                    for q in az_reach[i - 1])})
        el_flags = [el_ok(i) for i in range(len(segs))]
        # 综合可达：任一轴死亡即空
        reach = [az_reach[i] if el_flags[i] else set()
                 for i in range(len(segs))]

        # 后向（仅方位；仅在两轴全程可行时链路才有意义）
        alive = [set(grids[-1])]
        for i in range(len(segs) - 2, -1, -1):
            alive.insert(0, {p for p in grids[i]
                             if any(abs(p - q) <= vaz * dts[i + 1]
                                    for q in alive[0])})
        chain = [az_reach[i] & alive[i] for i in range(len(segs))]
        return grids, reach, chain, az_reach, el_flags

    def test_random_cases(self):
        rng = random.Random(2024)
        for trial in range(3000):
            span_weeks = rng.randint(1, 3)
            wlo = rng.randint(-2, 1) * CYCLE
            whi = wlo + span_weeks * CYCLE
            n = rng.randint(1, 7)
            t = rng.randint(0, 1000)
            segs = []
            for sid in range(1, n + 1):
                dur = rng.randint(1, 500)
                gap = rng.randint(0, 5000)
                t += gap
                end = t + dur
                segs.append(seg(sid, t, end, rng.randrange(360000),
                                rng.randint(0, 50000)))
                t = end
            vaz = rng.randint(0, 200)
            vel = rng.randint(0, 50)
            p0k = rng.randint(0, span_weeks)
            p0 = wlo + p0k * CYCLE + rng.choice([0, 10000, 180000, 359999])
            p0 = min(max(p0, wlo), whi)
            payload = {
                "initial": {"time_ms": 0, "physical_azimuth_mdeg": p0,
                            "elevation_mdeg": rng.randint(0, 50000)},
                "wrap_range": {"min_mdeg": wlo, "max_mdeg": whi},
                "speed_limits": {"azimuth_mdeg_per_ms": vaz,
                                 "elevation_mdeg_per_ms": vel},
                "segments": segs,
            }
            grids, reach, chain, az_reach, el_flags = self._brute(payload)
            r = audit_plan(payload)

            # 逐段对照（受阻时实现只返回到受阻段为止）
            for i in range(len(r["segments"])):
                got = r["segments"][i]["reachable_physical_azimuth"]
                if r["feasible"]:
                    expect = sorted(chain[i])
                elif i < r["blocked_index"] - 1:
                    expect = sorted(reach[i])
                else:
                    expect = []
                self.assertEqual(sorted(got), expect,
                                 f"trial {trial} seg {i}: {payload}")
            if not r["feasible"]:
                self.assertEqual(len(r["segments"]), r["blocked_index"])

            # 可行性与失败段
            first_empty = next((i for i in range(n) if not reach[i]), None)
            if first_empty is None:
                self.assertTrue(r["feasible"], f"trial {trial}")
            else:
                self.assertFalse(r["feasible"], f"trial {trial}")
                self.assertEqual(r["blocked_index"], first_empty + 1)
                # 两轴独立判定原因
                i = first_empty
                az_fail = not az_reach[i]
                el_fail = not el_flags[i]
                expect_reason = (
                    REASON_BOTH if az_fail and el_fail else
                    REASON_AZIMUTH if az_fail else REASON_ELEVATION
                )
                self.assertEqual(r["reason"], expect_reason, f"trial {trial}")

    def test_200_segments_are_fast(self):
        import time

        n = 200
        segs = []
        t = 0
        for sid in range(1, n + 1):
            t += 1000
            segs.append(seg(sid, t, t + 100, sid * 1234 % 360000))
            t += 100
        payload = {
            "initial": {"time_ms": 0, "physical_azimuth_mdeg": 0,
                        "elevation_mdeg": 0},
            "wrap_range": {"min_mdeg": -5 * CYCLE, "max_mdeg": 5 * CYCLE},
            "speed_limits": {"azimuth_mdeg_per_ms": 400,
                             "elevation_mdeg_per_ms": 400},
            "segments": segs,
        }
        start = time.perf_counter()
        r = audit_plan(payload)
        self.assertLess(time.perf_counter() - start, 0.5)
        self.assertIn("feasible", r)


if __name__ == "__main__":
    unittest.main()
