# Price Alerts

`price-alerts` 是一个运行在服务器上的价格监控服务，当前用于监控 `trade.xyz` 与 `ostium` 的 `CL` 价格，并通过 `fwalert` 电话告警。

## 当前功能

目前服务支持两类提醒：

1. **价差提醒**
2. **爆仓价接近提醒**

服务常驻运行，当前由 `systemd` 托管。

## 监控标的

- `trade.xyz`：`CL`
- `ostium`：`CL/USD`

## 抓取频率

默认每 **5 秒** 抓取一次两边价格，然后执行一次规则判断。

配置项：

- `POLL_INTERVAL_SECONDS=3`

## 价差提醒规则

现在价差提醒**只参考 `open_spread`**：

- `open_spread = trade bid - ostium ask`

系统持续采样 `open_spread`，并维护最近 **60 秒** 的窗口。

### 核心思路

不再用“窗口内最大值 - 最小值”的振幅做提醒。

现在改为比较：

- `current_open_spread - oldest_open_spread_in_window`

也就是：

- 当前 `open_spread`
- 对比 60 秒窗口里最早那个样本的 `open_spread`

得到：

- `delta_60s = current_open_spread - oldest_open_spread_in_window`

### 触发条件

当 `delta_60s` 满足以下条件之一，并且达到连续确认次数后，就会触发提醒：

1. **价差放大**
   - `delta_60s >= 0.48`
   - 事件名：`open_spread_expand_60s`

2. **价差缩小**
   - `delta_60s <= -0.48`
   - 事件名：`open_spread_contract_60s`

### 触发方式

当前规则不是“一次超阈值就立刻告警”，而是：

- 条件第一次满足时，只记一次确认，不立刻提醒
- 只有当后续采样里，这个方向持续满足条件，并达到确认次数后，才会真正触发提醒
- 在默认 `POLL_INTERVAL_SECONDS=5` 下，实际语义是：**连续三次采样都保持同一方向的 60 秒净变化超过阈值才提醒**

### 防重复提醒

同一方向不会连续重复提醒：

- 已经触发过一次“价差放大”后，只要 `delta_60s` 还维持在放大区，就不会重复报“放大”
- 已经触发过一次“价差缩小”后，只要 `delta_60s` 还维持在缩小区，就不会重复报“缩小”

只有当 `delta_60s` 回到中性区间后，才会重新进入可触发状态。

当前中性区定义为：

- `-0.2 < delta_60s < 0.2`

### Warm-up 规则

Ostium 从闭市切回开市后，会先进入 **60 秒 warm-up**：

- 这段时间只收集开市后的新样本
- 不触发 spread alert
- 同时清空旧的方向状态和确认计数

### 当前配置

- `SPREAD_CHANGE_WINDOW_SECONDS=60`
- `SPREAD_CHANGE_THRESHOLD=0.48`
- `SPREAD_BREAKOUT_CONFIRM_SAMPLES=3`
- `SPREAD_REARM_DELTA=0.2`（代码常量）

注意：

- **价差提醒只看 `open_spread`**
- **价差提醒不再使用 10 分钟冷却**
- 同一方向只提醒一次，回到中性区后才会重新 armed
- **爆仓价提醒的冷却仍然保留**

## 爆仓价接近提醒

当前还支持监控两边的爆仓价接近情况。

### 判断方式

使用 **mid 价**：

- `trade_mid = (trade_bid + trade_ask) / 2`
- `ostium_mid = (ostium_bid + ostium_ask) / 2`

然后按绝对值距离判断：

- `abs(trade_mid - TRADE_LIQUIDATION_PRICE) <= LIQUIDATION_ALERT_DISTANCE`
- `abs(ostium_mid - OSTIUM_LIQUIDATION_PRICE) <= LIQUIDATION_ALERT_DISTANCE`

### 当前配置

- `TRADE_LIQUIDATION_PRICE=140`
- `OSTIUM_LIQUIDATION_PRICE=80`
- `LIQUIDATION_ALERT_DISTANCE=5`
- `LIQUIDATION_ALERT_COOLDOWN_SECONDS=1800`

也就是说：

- 当 `trade.xyz` 的 `CL mid` 距离 `140` 只剩 **5** 以内时，会触发电话提醒
- 当 `ostium` 的 `CL mid` 距离 `80` 只剩 **5** 以内时，会触发电话提醒
- 同一平台的爆仓提醒默认 **30 分钟冷却一次**

## 提醒开关规则

现在不再按北京时间静默。

当前是否允许提醒，直接取决于 Ostium 返回的市场状态字段：

- `isMarketOpen=true`：允许提醒，也允许进行价差计算
- `isMarketOpen=false`：不提醒；同时不计算价差、不做 spread alert 判定、也不写入价差历史

也就是说，Ostium 闭市时服务仍会抓两边原始价格并更新健康状态，但不会把闭市样本纳入价差逻辑。
当 Ostium 从闭市重新开市时，系统会清空旧的价差窗口，并进入一个与 `SPREAD_CHANGE_WINDOW_SECONDS` 等长的 warm-up 阶段；warm-up 期间只收集开市后的新样本，不触发 spread alert。

## 电话告警

电话告警通过 `fwalert` 完成。

现在两类告警已拆分通道：

- **价差告警**：走原来的 `SPREAD_FWALERT_URL`
- **爆仓告警**：走独立的 `LIQUIDATION_FWALERT_URL`

代码中不会保存你的私密 webhook，而是通过本地环境变量加载：

- `SPREAD_FWALERT_URL`：价差告警通道
- `LIQUIDATION_FWALERT_URL`：爆仓告警通道

为了兼容旧配置，如果你本地还保留了 `FWALERT_URL`，代码也会把它作为默认回退值；但现在推荐明确拆成两个独立变量。

仓库中只保留：

- `.env.example`

本机实际使用：

- `.env`

## 日志与记录

### 1. 健康检查

可通过以下接口查看服务状态：

- `/`
- `/health`

默认端口：

- `8790`

### 2. 告警历史

接口：

- `/alerts`

作用：

- 返回最近的告警记录
- 包含时间、事件类型、HTTP 返回状态、价差快照等信息（被市场关闭拦截的事件不落盘）
- spread 事件会附带：
  - `oldest_open_spread`
  - `current_open_spread`
  - `delta_60s`
  - `window_samples`

另外还提供一个图表页：

- `/chart`

用途：

- 统一使用北京时间展示提醒历史
- 展示提醒时间、价差走势
- 按天分线对比（例如 4-1 一条线、4-2 一条线）
- 读取 `alerts_log.jsonl` 中的有效 JSON 行；损坏行会被跳过
- 即使告警记录里附带了最近窗口的原始 `window_samples`，图表也不会把这些原始样本展开展示

### 3. 本地持久化文件

告警记录会持久化写入：

- `alerts_log.jsonl`

当前写入方式已经做了保护：

- 先写临时文件
- 再 `flush + fsync`
- 最后用原子替换覆盖正式日志

这样即使遇到磁盘异常，也更不容易把原有 JSONL 写成半截脏数据。

每条记录会尽量包含：

- 触发时间
- 北京时间
- 事件类型
- 是否真正发起了告警请求
- `fwalert` 返回状态
- 当时的价格快照
- `open_spread`
- `close_spread`
- `trade_mid`
- `ostium_mid`
- `oldest_open_spread`
- `current_open_spread`
- `delta_60s`
- 爆仓价距离

## 运行方式

### 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8790
```

### 当前正式运行方式

当前服务已经由 `systemd` 正式托管。

常用命令：

```bash
systemctl status price-alerts
sudo systemctl restart price-alerts
journalctl -u price-alerts -n 100 --no-pager
```

## 主要配置项

`.env` 中当前会用到这些参数：

```env
SPREAD_FWALERT_URL=
LIQUIDATION_FWALERT_URL=
POLL_INTERVAL_SECONDS=5
SPREAD_CHANGE_WINDOW_SECONDS=60
SPREAD_CHANGE_THRESHOLD=0.48
SPREAD_BREAKOUT_CONFIRM_SAMPLES=3
SYMBOL=CL
TRADE_LIQUIDATION_PRICE=140
OSTIUM_LIQUIDATION_PRICE=80
LIQUIDATION_ALERT_DISTANCE=5
LIQUIDATION_ALERT_COOLDOWN_SECONDS=1800
```

## 当前状态总结

当前这套服务已经具备：

- `trade.xyz` / `ostium` 双边 `CL` 抓价
- 只基于 `open_spread` 的 60 秒净变化方向提醒
- 连续三次采样确认后的 spread 告警
- 价差放大 / 缩小双向事件
- 爆仓价接近提醒
- 按 `isMarketOpen` 控制提醒开关
- 电话告警
- 告警历史记录
- 日志安全写入保护
- `systemd` 正式托管
