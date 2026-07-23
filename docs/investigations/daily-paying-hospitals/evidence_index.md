# 证据索引

| 结论 | 证据 | 可信度 | 限制 |
|---|---|---|---|
| 支付订单可按医院关联 | `C:\workspace\hospital-backend\src\main\java\com\zly\hospital\model\PaymentOrder.java`、`PaddlePaymentOrder.java`、`SteamPaymentOrder.java` | 高 | 来自同工作区后端源码，未直接查询生产表结构 |
| 统计入口是 `/api/stats` | `app/app.py` 中 `stats_api`、`load_stats_from_prod` | 高 | 无 |
| 金额沿用最小单位除以 100 | `app/app.py` 中 `major_amount`、`load_daily_recharge` | 高 | 假设各接入币种均沿用现有金额规则 |
| 7 日清单支持汇总与滚动 | `app/templates/dashboard.html` 中 `renderPayingHospitals`、`.paying-hospital-scroll` | 高 | 生产数据量未在本机复现 |
| 桌面端无控制台错误 | `ui-verification-desktop.png` 与 `test_results.md` | 高 | 使用本地模拟统计数据 |
