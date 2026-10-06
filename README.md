# 射电地面站观测计划审计服务

`POST /api/tracking-plans/audit` —— 对连续定点观测计划做**物理方位可达性**审计：
目标 0..359999 毫度的逻辑方位对应多个物理转角（可增减整周），算法判断是否存在一组
首尾相接、全程不脱离缆绕范围且每段空档内同时满足方位/俯仰转速限制的物理方位序列，
并给出每段在可行链路上**仍可到达**的物理方位集合。

## 为什么不能逐段选最近转角

逻辑方位 `a` 对应的物理方位是网格 `a + 360000·k`（k 为任意整数，且落在缆绕范围
内）。某段上"离当前位置最近"的点可能把天线带进缆绕死角，后续空档转不出来；而另一
个稍远的点反而能接通整条计划。例如（仓库测试中的最小陷阱）：

* 缆绕范围 `[-720000, 0]`，初始物理方位 `-90000`；
* 段1 目标方位 270000，空档 400000 ms、方位限速 1 毫度/ms：候选
  `{-450000, -90000}`，贪心选 `-90000`（距离 0）；
* 段2 目标方位 10000，空档 100000 ms：候选 `{-710000, -350000}`，从 `-90000`
  出发最近也要 260000 > 100000，**贪心误判失败**；
* 全局链路 `-90000 → -450000`（360000 ≤ 400000）`→ -350000`（100000）存在，
  计划应当放行；且段1 上 `-90000` 不属于任何完整链路，"仍可到达"集合只有
  `{-450000}`。

本服务因此采用**可达集合传播**，而不是逐段独立决策。

## 算法

每个观测段的候选物理方位是等差网格 `a + 360000·k`。以**圈号 k 的连续整数区间**
表示可达集合：

1. **前向传播**：从初始物理方位出发，逐段按空档方位预算 `v_az·dt` 膨胀圈号区间，
   再与该段网格（缆绕裁剪后的圈号区间）求交；俯仰轴在同一空档独立判定
   `|Δel| ≤ v_el·dt`。
2. **后向传播**：从末段反向做同样的对称膨胀求交。
3. 两段集合的交集即位于至少一条完整首尾相接链路上的物理方位（"仍可到达"）。
   前向传播到达末段 ⇔ 存在完整链路 ⇔ 放行。

网格等距（间距恰为 360000），圈号区间膨胀后仍为精确的连续区间，故无需枚举转角，
复杂度 O(n)，n ≤ 200。全部整数运算，无浮点误差。俯仰失败不影响方位可达性的独立
计算，因此受阻时可明确区分 `azimuth` / `elevation` / `both`，并报告最早失去全部
可达方位的段（1-based 序号与段 id）。

## 接口

### `POST /api/tracking-plans/audit`

请求（角度：整数毫度；转速：整数毫度/毫秒；时间：整数毫秒）：

```json
{
  "initial":     {"time_ms": 0, "physical_azimuth_mdeg": -90000, "elevation_mdeg": 0},
  "wrap_range":  {"min_mdeg": -720000, "max_mdeg": 0},
  "speed_limits": {"azimuth_mdeg_per_ms": 1, "elevation_mdeg_per_ms": 1000},
  "segments": [
    {"id": 101, "start_ms": 400000, "end_ms": 500000,
     "azimuth_mdeg": 270000, "elevation_mdeg": 0},
    {"id": 102, "start_ms": 600000, "end_ms": 700000,
     "azimuth_mdeg": 10000,  "elevation_mdeg": 0}
  ]
}
```

约束：观测段 1..200 个、按时间排列且互不相邻重叠（后段 `start_ms` 不得早于前段
`end_ms`）、`id` 唯一；逻辑方位 ∈ [0, 359999]；物理方位可增减整周但必须处于闭合
缆绕范围；转向只允许发生在相邻空档（初始时刻→首段起点、前段终点→后段起点）内。

成功响应（HTTP 200）：

```json
{
  "feasible": true,
  "segments": [
    {"id": 101, "reachable_physical_azimuth": [-450000]},
    {"id": 102, "reachable_physical_azimuth": [-350000]}
  ]
}
```

受阻响应（HTTP 200，业务结果）：

```json
{
  "feasible": false,
  "blocked_index": 2,
  "blocked_segment_id": 102,
  "reason": "azimuth",
  "segments": [
    {"id": 101, "reachable_physical_azimuth": [-90000]},
    {"id": 102, "reachable_physical_azimuth": []}
  ]
}
```

`reason` ∈ `azimuth` | `elevation` | `both`。请求非法（类型、范围、重叠、重复 id
等）返回 HTTP 400 与错误信息。`GET /health` 返回 200 供健康检查。

## 运行

仅需 Python 3.11+ 标准库，无第三方依赖：

```bash
python3 -m app.server            # 默认 0.0.0.0:8080，可用 PORT 环境变量覆盖
python3 -m unittest discover -s tests
```

### Docker Compose

```bash
# 启动带健康检查的服务
docker compose up --build -d api

# 自定义宿主机端口
HOST_PORT=9090 docker compose up --build -d api   # 或复制 .env.example 为 .env

# 一次性校验服务：编译代码、跑单元测试、等待健康检查、
# 提交可行计划与方位/俯仰/双轴受阻计划，退出码 0/1 报告结果
docker compose run --rm verify
```

`verify` 服务在 compose 中通过 `depends_on: condition: service_healthy` 等待 `api`
健康后才启动；本地无 Docker 时也可直接运行（先启动服务）：

```bash
python3 -m app.server &
BASE_URL=http://localhost:8080 python3 scripts/verify.py
```

## 目录

```
app/audit.py      核心审计算法（圈号区间前向/后向传播）
app/server.py     标准库 HTTP 服务
scripts/verify.py 一次性校验服务（编译 + 单测 + 健康检查 + 计划提交）
tests/test_audit.py 单测，含 3000 组以上随机计划与暴力枚举 DP 的对拍
Dockerfile / docker-compose.yml / .dockerignore / .env.example
```
