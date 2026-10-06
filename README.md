# Tracking Plan Audit

射电地面站连续观测计划的物理方位（缆绕）可行性审计服务。

逻辑方位只有 `0..359999` 毫度，而物理方位可以在闭合缆绕范围内增减整周
（`a + 360000·k`）。逐段选择"最近转角"是错误判据：一个局部可达的转角可能让
天线在后续空档无法脱离缆绕极限。本服务对每段维护**同时满足"从初始姿态可达"
与"仍能走完全部后续观测段"**的物理转角集合（前向 + 后向传播），只有存在一组
首尾相接的物理方位时才放行。

## 接口

`POST /api/tracking-plans/audit`

| 字段 | 含义 |
| --- | --- |
| `initial.time_ms` | 初始时刻（整数毫秒，≥0） |
| `initial.azimuth_mdeg` | 初始物理方位（整数毫度，须在缆绕范围内） |
| `initial.elevation_mdeg` | 初始俯仰（整数毫度） |
| `azimuth_wrap_min_mdeg` / `azimuth_wrap_max_mdeg` | 闭合物理方位范围 |
| `max_azimuth_speed_mdeg_per_ms` | 方位轴最大转速（正整数） |
| `max_elevation_speed_mdeg_per_ms` | 俯仰轴最大转速（正整数） |
| `segments[]` | 1–200 个定点段，`id` 唯一、按时间排列且互不重叠 |

段字段：`id`、`start_ms`、`end_ms`（须大于开始时刻）、`azimuth_mdeg`
（0–359999）、`elevation_mdeg`。每次转向在相邻空档内须同时满足两轴速度
（`|Δ角度| ≤ 转速 × 空档毫秒数`，闭区间）。

**成功响应**（`feasible: true`）：按时间顺序给出每段仍可到达的物理方位集合
`segments[].reachable_physical_azimuths_mdeg`。

**失败响应**（`feasible: false`）：

- `failed_segment_index`：最早失去全部可达方位的段（1 基序号）
- `failed_segment_id`：该段提交时的 `id`
- `axis`：`azimuth` / `elevation` / `both`（同一空档两轴共同超限）

请求结构非法返回 HTTP 400。

## 运行

```bash
# 构建并启动带健康检查的 API（宿主机端口可配置）
TRACKING_API_HOST_PORT=9090 docker compose up -d --build api

# 一次性核验：跑测试套件、等待健康、提交可行计划与受阻（方位/俯仰/双轴）计划
# 退出码 0 = 全部符合预期，非 0 = 失败
docker compose build verify
docker compose run --rm verify
```

健康检查为容器内 `GET /health` + 一次最小可行审计调用
（`docker/healthcheck.py`）。

## 本地开发（无需 Docker）

零第三方依赖，Python 3.11 标准库：

```bash
python -m unittest discover -s tests -v
PORT=8080 python -m app.main
SERVICE_URL=http://127.0.0.1:8080 python verify.py
```

## 算法

1. 为每段枚举缆绕范围内所有物理转角 `a + 360000·k`；
2. 前向传播：由初始物理方位，按空档半径传播可达位集；
3. 后向传播：由末段倒推，剔除无法接续到终点的"死路转角"（最近转角贪心正是
   在此误判）；
4. 两集合交集即"仍可到达的物理方位集合"；最早前向集合为空的段为失败段，
   并结合该空档俯仰是否超限给出 `azimuth` / `elevation` / `both`。
