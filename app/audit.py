"""观测计划可达性审计的核心算法（无第三方依赖）。

问题模型
--------
每个定点观测段 *i* 给出目标的逻辑方位 ``a_i``（0..359999 毫度）。天线物理方位
``p_i`` 与逻辑方位等价当且仅当::

    p_i = a_i + CYCLE * k,      k 为整数,  CYCLE = 360000

且 ``p_i`` 必须处于闭合的缆绕范围 ``[wrap_min, wrap_max]``。因此每段的候选物理
方位构成**等差网格**::

    G_i = { a_i + CYCLE*k | wrap_min <= a_i + CYCLE*k <= wrap_max }

相邻两段之间存在空档 ``dt``（毫秒），方位轴最大转速 ``v_az``（毫度/毫秒）与
俯仰轴最大转速 ``v_el`` 互相独立：

* 俯仰角必须满足 ``|e_j - e_i| <= v_el*dt``，否则该转向俯仰轴不可行；
* 圈号 k -> k' 可行当且仅当 ``|g_j(k') - g_i(k)| <= v_az*dt``。

贪心（每段选最近转角）会把天线带进缆绕死角：某段局部可达却无法在后续空档内脱离。
因此这里采用**可达集合的前向/后向传播**：

* 前向传播：从初始物理方位出发，逐段求"存在一条从起点到该段路径"的圈号集合；
* 后向传播：从最后一段反向求"存在一条从该段到终点路径"的圈号集合；
* 两者交集才是可行首尾相接链路上、该段**仍然可到达**的物理方位集合。
  计划放行当且仅当每个交集非空（等价于前向传播一路存活到末段）。

整数圈号集合始终保持为**单个连续整数区间**：网格等距（间距 CYCLE），源圈号每增加
1，目标可达窗口在圈号空间中恰好平移 1，因此连续圈号区间膨胀后与等差网格求交仍为
连续区间。表示精确、无离散枚举，与 1..200 段规模无关。

所有量均为整数，全程整数运算，无浮点误差。
"""

from __future__ import annotations

from typing import Optional

CYCLE = 360_000  # 毫度表示下一周的大小

# 失败原因
REASON_AZIMUTH = "azimuth"
REASON_ELEVATION = "elevation"
REASON_BOTH = "both"


class ValidationError(ValueError):
    """请求体语义校验失败（映射为 HTTP 400）。"""


def _require_int(value, name: str) -> int:
    # 拒绝 bool（它是 int 的子类）及非整数
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} 必须是整数")
    return value


def _div_floor(x: int, y: int) -> int:
    """向下取整的整数除法（y > 0）。"""
    return x // y


def _div_ceil(x: int, y: int) -> int:
    """向上取整的整数除法（y > 0）。"""
    return -((-x) // y)


class _Grid:
    """段 i 的候选物理方位网格 a + CYCLE*k，k in [lo, hi]。"""

    __slots__ = ("a", "lo", "hi")

    def __init__(self, a: int, lo: int, hi: int):
        self.a = a
        self.lo = lo
        self.hi = hi

    def point(self, k: int) -> int:
        return self.a + CYCLE * k

    def values_in(self, lo: int, hi: int) -> list[int]:
        lo = max(lo, self.lo)
        hi = min(hi, self.hi)
        if lo > hi:
            return []
        return [self.point(k) for k in range(lo, hi + 1)]


def _candidate_bounds(a: int, wrap_min: int, wrap_max: int) -> tuple[int, int]:
    """求满足 wrap_min <= a + CYCLE*k <= wrap_max 的整数 k 范围。"""
    lo = _div_ceil(wrap_min - a, CYCLE)
    hi = _div_floor(wrap_max - a, CYCLE)
    return lo, hi


def _k_interval_for_physical_target(x: int, grid_a: int, R: int) -> tuple[int, int]:
    """以物理方位 x 为起点、方位可走预算 R（毫度）时，网格 grid_a 上可达圈号范围。

    可达条件 |grid_a + CYCLE*k - x| <= R。
    """
    lo = _div_ceil(x - R - grid_a, CYCLE)
    hi = _div_floor(x + R - grid_a, CYCLE)
    return lo, hi


def _expand_interval(
    lo: int, hi: int, grid_a_src: int, grid_a_dst: int, R: int
) -> tuple[int, int]:
    """源圈号区间 [lo,hi]（网格 grid_a_src）在方位预算 R 下可到的目标圈号区间。

    源、目标同为 CYCLE 等距网格，相对几何与源圈号无关：若圈号 0 的源点
    （物理方位 grid_a_src）可达目标窗口 ``[A, B]``（``A>B`` 表示为空），
    则源圈号 k 的窗口恰为 ``[k+A, k+B]``（整体平移 k）。k 取遍连续区间时
    相邻窗口首尾相接（``B>=A`` 时），故并集为 ``[lo+A, hi+B]``；
    若 ``[A,B]`` 本身为空，则预算 R 小到任何源点都碰不到目标网格，返回空。
    """
    a0, b0 = _k_interval_for_physical_target(grid_a_src, grid_a_dst, R)
    if a0 > b0:
        return 1, 0
    return lo + a0, hi + b0


def _intersect(
    a: tuple[int, int], b: tuple[int, int]
) -> Optional[tuple[int, int]]:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return (lo, hi) if lo <= hi else None


def _failure_reason(az_ok: bool, el_ok: bool) -> str:
    """根据两轴是否满足限制给出失败原因；调用方保证至少一个为 False。"""
    if not az_ok and not el_ok:
        return REASON_BOTH
    if not az_ok:
        return REASON_AZIMUTH
    return REASON_ELEVATION


def audit_plan(payload: object) -> dict:
    """审计一条观测计划。

    成功::

        {"feasible": true,
         "segments": [{"id": ..., "reachable_physical_azimuth": [物理方位毫度...]}, ...]}

    失败::

        {"feasible": false, "blocked_index": <1-based>, "blocked_segment_id": <id>,
         "reason": "azimuth" | "elevation" | "both",
         "segments": [ ... 已到达段的前向可达集合；受阻段为空列表 ... ]}

    请求非法时抛出 :class:`ValidationError`。
    """
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    init = payload.get("initial")
    if not isinstance(init, dict):
        raise ValidationError("缺少 initial（初始时刻与姿态）")
    t0 = _require_int(init.get("time_ms"), "initial.time_ms")
    p0 = _require_int(
        init.get("physical_azimuth_mdeg"), "initial.physical_azimuth_mdeg"
    )
    e0 = _require_int(init.get("elevation_mdeg"), "initial.elevation_mdeg")

    wrap = payload.get("wrap_range")
    if not isinstance(wrap, dict):
        raise ValidationError("缺少 wrap_range（闭合物理方位缆绕范围）")
    wrap_min = _require_int(wrap.get("min_mdeg"), "wrap_range.min_mdeg")
    wrap_max = _require_int(wrap.get("max_mdeg"), "wrap_range.max_mdeg")
    if wrap_min > wrap_max:
        raise ValidationError("wrap_range.min_mdeg 不得大于 wrap_range.max_mdeg")
    if wrap_max - wrap_min > 10 * CYCLE:
        # 防御性上限：缆绕范围不得超过 10 周
        raise ValidationError("缆绕范围过大（不得超过 10 周）")

    limits = payload.get("speed_limits")
    if not isinstance(limits, dict):
        raise ValidationError("缺少 speed_limits（双轴最大转速）")
    v_az = _require_int(
        limits.get("azimuth_mdeg_per_ms"), "speed_limits.azimuth_mdeg_per_ms"
    )
    v_el = _require_int(
        limits.get("elevation_mdeg_per_ms"), "speed_limits.elevation_mdeg_per_ms"
    )
    if v_az < 0 or v_el < 0:
        raise ValidationError("转速不得为负")

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise ValidationError("缺少 segments（观测段数组）")
    if not (1 <= len(raw_segments) <= 200):
        raise ValidationError("观测段数量必须在 1 至 200 之间")

    parsed: list[dict] = []
    seen_ids: set = set()
    prev_end: Optional[int] = None
    for i, seg in enumerate(raw_segments):
        if not isinstance(seg, dict):
            raise ValidationError(f"第 {i + 1} 个观测段必须是对象")
        sid = _require_int(seg.get("id"), f"segments[{i}].id")
        if sid in seen_ids:
            raise ValidationError(f"观测段 id 必须唯一：重复 id {sid}")
        seen_ids.add(sid)

        st = _require_int(seg.get("start_ms"), f"segments[{i}].start_ms")
        en = _require_int(seg.get("end_ms"), f"segments[{i}].end_ms")
        if st >= en:
            raise ValidationError(f"段 {sid} 的 start_ms 必须早于 end_ms")

        az = _require_int(seg.get("azimuth_mdeg"), f"segments[{i}].azimuth_mdeg")
        el = _require_int(seg.get("elevation_mdeg"), f"segments[{i}].elevation_mdeg")
        if not 0 <= az <= CYCLE - 1:
            raise ValidationError(f"段 {sid} 的逻辑方位必须在 0..359999 毫度之间")

        if prev_end is not None and st < prev_end:
            raise ValidationError(
                f"段 {sid} 与前一段时间重叠（观测段不得重叠，须按时间排列）"
            )
        prev_end = en
        parsed.append({"id": sid, "start": st, "end": en, "az": az, "el": el})

    if not wrap_min <= p0 <= wrap_max:
        raise ValidationError("初始物理方位不在缆绕范围内")

    n = len(parsed)

    # 转向空档：初始姿态 -> 段0 用段0起点与初始时刻之差；其余用相邻段 start/end 之差。
    # 观测段内天线定点不动，转向只允许发生在空档内。
    dts = [parsed[0]["start"] - t0]
    for i in range(1, n):
        dts.append(parsed[i]["start"] - parsed[i - 1]["end"])
    for i, dt in enumerate(dts):
        if dt < 0:
            where = "初始时刻到首段" if i == 0 else f"段 {parsed[i - 1]['id']} 与 {parsed[i]['id']} 之间"
            raise ValidationError(f"{where}的空档为负（时间安排不可行）")

    # 每段的候选网格
    grids: list[_Grid] = []
    for s in parsed:
        klo, khi = _candidate_bounds(s["az"], wrap_min, wrap_max)
        grids.append(_Grid(s["az"], klo, khi))

    # ---- 前向传播 ----
    reach_fwd: list[Optional[tuple[int, int]]] = []

    dt = dts[0]
    lo, hi = _k_interval_for_physical_target(p0, grids[0].a, v_az * dt)
    cur = _intersect((lo, hi), (grids[0].lo, grids[0].hi))
    el_ok = abs(parsed[0]["el"] - e0) <= v_el * dt
    if cur is None or not el_ok:
        return _blocked(parsed, grids, reach_fwd, 0,
                        _failure_reason(cur is not None, el_ok))
    reach_fwd.append(cur)

    for i in range(1, n):
        dt = dts[i]
        prev_lo, prev_hi = reach_fwd[i - 1]  # type: ignore[misc]
        lo, hi = _expand_interval(
            prev_lo, prev_hi, grids[i - 1].a, grids[i].a, v_az * dt
        )
        cur = _intersect((lo, hi), (grids[i].lo, grids[i].hi))
        el_ok = abs(parsed[i]["el"] - parsed[i - 1]["el"]) <= v_el * dt
        if cur is None or not el_ok:
            return _blocked(parsed, grids, reach_fwd, i,
                            _failure_reason(cur is not None, el_ok))
        reach_fwd.append(cur)

    # ---- 后向传播 ----
    # 末段之后不再转向，故末段后向集合即其全部候选网格。
    reach_bwd: list[Optional[tuple[int, int]]] = [None] * n
    reach_bwd[-1] = (grids[-1].lo, grids[-1].hi)
    for i in range(n - 2, -1, -1):
        dt = dts[i + 1]
        nxt_lo, nxt_hi = reach_bwd[i + 1]  # type: ignore[misc]
        # 速度约束对称：把段 i+1 的可达区间当作"源"，反向展开到段 i 网格。
        lo, hi = _expand_interval(
            nxt_lo, nxt_hi, grids[i + 1].a, grids[i].a, v_az * dt
        )
        reach_bwd[i] = _intersect((lo, hi), (grids[i].lo, grids[i].hi))

    # ---- 前向 ∩ 后向：位于至少一条完整首尾相接链路上的物理方位 ----
    segment_results = []
    for i in range(n):
        joint = _intersect(reach_fwd[i], reach_bwd[i])  # type: ignore[arg-type]
        if joint is None:
            # 理论不可达：前向一路存活即存在完整链路，各段交必非空。
            return _blocked(parsed, grids, reach_fwd, i, REASON_AZIMUTH)
        segment_results.append(
            {
                "id": parsed[i]["id"],
                "reachable_physical_azimuth": grids[i].values_in(*joint),
            }
        )

    return {"feasible": True, "segments": segment_results}


def _blocked(
    parsed: list[dict],
    grids: list[_Grid],
    reach_fwd: list[Optional[tuple[int, int]]],
    i: int,
    reason: str,
) -> dict:
    """构造受阻响应：已到达段给出前向可达集合，受阻段为空。"""
    segments = []
    for j, iv in enumerate(reach_fwd):
        values = grids[j].values_in(*iv) if iv is not None else []
        segments.append(
            {"id": parsed[j]["id"], "reachable_physical_azimuth": values}
        )
    segments.append({"id": parsed[i]["id"], "reachable_physical_azimuth": []})
    return {
        "feasible": False,
        "blocked_index": i + 1,
        "blocked_segment_id": parsed[i]["id"],
        "reason": reason,
        "segments": segments,
    }
