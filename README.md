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

### 开仓提醒 `open_cross_up`

计算公式：

- `open_spread = trade bid - ostium ask`

当前使用 **双阈值 + 冷却时间** 防止阈值附近反复抖动导致连续来电：

- 触发阈值：`open_spread > 3.5`
- 重新武装阈值：`open_spread < 3.2`
- 冷却时间：`600` 秒

含义：

- 只有在 `open_spread` 先回到 **3.2 以下** 后，系统才会重新进入“可提醒”状态
- 重新进入可提醒状态后，如果 `open_spread` 再次上穿 **3.5**，才会触发新的开仓提醒
- 同类提醒还会受到 `SPREAD_ALERT_COOLDOWN_SECONDS` 限制，默认 **10 分钟内最多一次**

### 平仓提醒 `close_cross_down`

计算公式：

- `close_spread = trade ask - ostium bid`

当前规则：

- 触发阈值：`close_spread < 3.2`
- 重新武装阈值：`close_spread > 3.5`
- 冷却时间：`600` 秒

含义：

- 只有在 `close_spread` 先回到 **3.5 以上** 后，系统才会重新进入“可提醒”状态
- 重新进入可提醒状态后，如果 `close_spread` 再次跌破 **3.2**，才会触发新的平仓提醒
- 同类提醒也受 `SPREAD_ALERT_COOLDOWN_SECONDS` 控制，默认 **10 分钟内最多一次**

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

- `TRADE_LIQUIDATION_PRICE=116`
- `OSTIUM_LIQUIDATION_PRICE=78`
- `LIQUIDATION_ALERT_DISTANCE=5`
- `LIQUIDATION_ALERT_COOLDOWN_SECONDS=1800`

也就是说：

- 当 `trade.xyz` 的 `CL mid` 距离 `116` 只剩 **5** 以内时，会触发电话提醒
- 当 `ostium` 的 `CL mid` 距离 `78` 只剩 **5** 以内时，会触发电话提醒
- 同一平台的爆仓提醒默认 **30 分钟冷却一次**

## 静默规则（按北京时间）

当前静默规则如下：

1. **每天 04:59 ~ 06:10 不通知**
2. **周六 07:59 开始，到周一 06:00 之前不通知**

注意：

- 因为保留了“每天 04:59 ~ 06:10 静默”，所以周一真正恢复提醒的时间实际上是 **06:11** 之后

## 电话告警

电话告警通过 `fwalert` 完成。

代码中不会保存你的私密 webhook，而是通过本地环境变量加载：

- `FWALERT_URL`

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
- 包含时间、事件类型、是否静默、HTTP 返回状态、价差快照等信息

另外还提供一个图表页：

- `/chart`

用途：

- 统一使用北京时间展示提醒历史
- 展示价差是变大还是变小
- 展示提醒时间、价差走势
- 按天分线对比（例如 4-1 一条线、4-2 一条线）

### 3. 本地持久化文件

告警记录会持久化写入：

- `alerts_log.jsonl`

每条记录会尽量包含：

- 触发时间
- 北京时间
- 事件类型
- 是否被静默规则拦截
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
FWALERT_URL=
POLL_INTERVAL_SECONDS=5
THRESHOLD=3
OPEN_ALERT_HIGH_THRESHOLD=3.5
OPEN_ALERT_LOW_RESET=3.2
CLOSE_ALERT_LOW_THRESHOLD=3.2
CLOSE_ALERT_HIGH_RESET=3.5
SPREAD_ALERT_COOLDOWN_SECONDS=600
SYMBOL=CL
TRADE_LIQUIDATION_PRICE=116
OSTIUM_LIQUIDATION_PRICE=78
LIQUIDATION_ALERT_DISTANCE=5
LIQUIDATION_ALERT_COOLDOWN_SECONDS=1800
```

## 当前状态总结

当前这套服务已经具备：

- `trade.xyz` / `ostium` 双边 `CL` 抓价
- 价差提醒
- 爆仓价接近提醒
- 北京时间静默规则
- 电话告警
- 告警历史记录
- `systemd` 正式托管

如果后续继续扩展，建议优先考虑：

- 在日志里记录每次触发时所使用的阈值版本
- 增加手动测试接口
- 增加更直观的状态字段（如 armed/cooling、下一次 cooldown 剩余时间）
