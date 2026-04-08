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

- `POLL_INTERVAL_SECONDS=5`

## 价差提醒规则

现在价差提醒的规则是：**看最近 60 秒窗口内的最大绝对波动幅度。**

系统持续采样两条价差：

- `open_spread = trade bid - ostium ask`
- `close_spread = trade ask - ostium bid`

对于每条价差，在最近 **60 秒** 的窗口内计算：

- `window_max = 这 60 秒内的最大值`
- `window_min = 这 60 秒内的最小值`
- `window_abs_move = window_max - window_min`

当前提醒条件：

- `open_spread` 的 `window_abs_move > 0.6`
- 或 `close_spread` 的 `window_abs_move > 0.6`

### 触发方式

当前规则不是“一次超阈值就立刻告警”，而是：

- 某条价差第一次满足 `window_abs_move > 0.6` 时，只记一次确认，不立刻提醒
- 只有当**下一次采样**这条价差仍然满足 `window_abs_move > 0.6` 时，才会真正触发提醒
- 也就是说，在默认 `POLL_INTERVAL_SECONDS=5` 下，实际语义是：**连续两次采样都 breakout 才提醒**
- 同一个 breakout 窗口不会连续重复提醒
- 只有当窗口振幅重新回落到阈值以内后，才会重新进入可触发状态
- Ostium 从闭市切回开市后，会先进入 **60 秒 warm-up**：这段时间只收集新样本，不触发 spread alert

程序内部会分别为两条价差单独计数：

- `open_spread` 连续确认次数
- `close_spread` 连续确认次数

这样可以避免 `open_spread` 和 `close_spread` 互相串计数导致误报。

### 当前配置

- `SPREAD_CHANGE_WINDOW_SECONDS=60`
- `SPREAD_CHANGE_THRESHOLD=0.6`
- `SPREAD_BREAKOUT_CONFIRM_SAMPLES=2`

注意：

- **价差提醒不再使用 10 分钟冷却**
- 同一波动窗口只提醒一次，回落后才会重新 armed
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

另外还提供一个图表页：

- `/chart`

用途：

- 统一使用北京时间展示提醒历史
- 展示价差是变大还是变小
- 展示提醒时间、价差走势
- 按天分线对比（例如 4-1 一条线、4-2 一条线）
- 读取 `alerts_log.jsonl` 中的有效 JSON 行；损坏行会被跳过

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
SPREAD_CHANGE_THRESHOLD=0.6
SPREAD_BREAKOUT_CONFIRM_SAMPLES=2
SYMBOL=CL
TRADE_LIQUIDATION_PRICE=140
OSTIUM_LIQUIDATION_PRICE=80
LIQUIDATION_ALERT_DISTANCE=5
LIQUIDATION_ALERT_COOLDOWN_SECONDS=1800
```

## 当前状态总结

当前这套服务已经具备：

- `trade.xyz` / `ostium` 双边 `CL` 抓价
- 基于 60 秒窗口波动的价差提醒
- 连续两次采样确认后的 spread 告警
- 爆仓价接近提醒
- 按 `isMarketOpen` 控制提醒开关
- 电话告警
- 告警历史记录
- 日志安全写入保护
- `systemd` 正式托管

如果后续继续扩展，建议优先考虑：

- 在日志里记录每次触发时所使用的阈值版本
- 增加手动测试接口
- 增加更直观的状态字段（如 armed/cooling、下一次 cooldown 剩余时间）
