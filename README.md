# 荣光医院本地运营统计看板

这是独立于游戏工程的本机看板。它只读连接数据库，统计数据快照保存在本地 `data/ops_dashboard.sqlite3`。

## Windows 安装或升级

```powershell
git clone https://github.com/sunshaoxuan/rhospital.dashboard.git
cd rhospital.dashboard
.\install-or-upgrade.ps1
```

以后升级：

```powershell
cd rhospital.dashboard
.\install-or-upgrade.ps1
```

## Linux 安装或升级

```bash
git clone https://github.com/sunshaoxuan/rhospital.dashboard.git
cd rhospital.dashboard
chmod +x install-or-upgrade.sh
./install-or-upgrade.sh
```

以后升级重复运行 `./install-or-upgrade.sh`。

访问地址：

```text
http://<SERVER_IP>:18091/
```

安装脚本会自动检测本机真实 IPv4，并写入 `.env` 的 `DASHBOARD_PUBLIC_IP`。脚本会拒绝 `localhost`、`127.0.0.1`、`0.0.0.0`。默认对外端口是 `18091`，用于避免和本机其他服务冲突。

## 自动恢复

`docker-compose.yml` 使用 `restart: unless-stopped`。只要 Docker Desktop 随系统启动，容器会自动恢复。

停止：

```powershell
docker compose down
```

## 数据边界

- 生产数据库只执行 `SELECT`。
- 每次数据库连接都会设置 `default_transaction_read_only=on`。
- 页面每 60 秒轮询一次，数据未变化时不重绘。
- 看板页面使用自适应版面，手机宽度下卡片、图表、页签和明细弹窗自动收缩，宽表格支持横向滑动查看。
- 每 10 分钟保存一次本地每日快照。
- 每日趋势图会用生产库近14日数据覆盖同日本地快照，快照日期按 `OPS_DASHBOARD_TIME_ZONE` 计算，避免早期或错日快照遮盖真实数据。
- 数据库暂不可用时，页面仍返回空统计并在备注中显示数据源错误，避免首屏变成 500。
- 顶部今日收入拆分为 Stripe 和 Steam 两张卡片，分别展示 CNY 金额、订单数和元宝。
- 图表区包含近14日元宝消耗按小时统计，口径为 `t_log_yuanbao` 中 `old_value > new_value` 的减少量。
- 图表下方提供道具、金钱、元宝、声望、公会、注册者统计页签，每页支持 20、50、100 条分页。
- 页面刷新按钮在请求进行中显示旋转状态和“刷新中”，并临时禁用重复点击。
- 道具页签按全量日志聚合商品购买数量、消耗数量和元宝使用数量；金钱、元宝、声望、公会按当前值从高到低排序；注册者按注册时间从新到旧排序。
- 页面顶部提供站点级页签：`运营总览` 保持原有统计画面，`特需门诊` 展示常见病特需门诊只读分析，`医托拉人` 展示医托钱包改档后的普通拉人、钱包和名片反拉数据。
- 医托拉人页签按钱包规则上线后展示普通拉人次数、成功拉走病人数、钱包打开率、生成金币总量、平均每钱包金币、道具掉落率、名片反拉点击和反拉成功；普通拉人使用目标医院日志，钱包指标只归因普通成功拉人，反拉使用名片点击日志和反拉成功日志。
- 医托拉人链路趋势图使用双轴展示，普通拉人走左轴，反拉成功走右轴；钱包生成数和普通拉人次数保持一致，不在顶部卡片和链路趋势图重复展示，只作为打开率分母和明细审计字段保留。
- 医托钱包统计只展示钱包规则上线后的普通拉人、钱包和名片反拉数据，并按好友/非好友和拉走人数段 `1-39`、`40-69`、`70+` 拆分；钱包上线前没有金币或道具掉落机会，页面只展示当前生产规则，不展示旧规则对比。当前生产规则读取 `t_broker_wallet_rule`，钱包读取 `t_broker_wallet_drop`，名片读取 `t_broker_retaliation_voucher`。
- 特需门诊页按北京时间和周三开诊周期展示子页签，最新周排在最前，新一周出现后自动把上一周向后顺延；每个周期页签包含每小时总览、病历等级分布、具体病历分布、道具奖品发放、后台补偿奖品、资源奖品发放、每周库存消耗、医院行为 Top 30、对账异常和风险提示次数。
- 特需门诊道具奖品来自 `reward_items` 道具 JSON 和后台补偿物品批次，并单独展示 Top 图和明细表；名称映射包含病志残页 `1351` 和特需门诊票 `1792`；主账资源来自确诊记录字段，声望读取 `prestige_reward`，资源图中金钱使用独立右轴，避免百万级金钱压住其他资源。
- 后台补偿批次统计只归入奖品口径；例如 `item_id=1792` 的特需门诊票补偿会显示在道具奖品和后台补偿奖品表，并保留批次原因。门诊票流水仅统计 `t_special_clinic_ticket_log`。
- 顶部百分比分为病历池消耗率和残页奖池消耗率；前者使用周三主柜体 `total_diagnoses / initial_total`，后者使用 `prescription_page_awarded_total / prescription_page_budget_total`。
- 特需门诊库存消耗率按周统计，周总量取周三周期主柜体行 `initial_total`，分子取同一主柜体行 `total_diagnoses`，主指标剩余取该主柜体行 `remaining_total`；患者记录按周期汇总保留为 `diagnosis_count_from_record`，周内其他柜体消耗保留为 `weekly_cabinet_diagnoses` 和 `non_canonical_cabinet_diagnoses`，用于发现旧部署留下的日粒度记录差异。
- 特需门诊每周库存消耗展示按业务域分组为病历池、残页奖池、补仓和对账，保留同一周库存快照里的完整字段，减少横向宽表造成的阅读混乱。
- 特需门诊顶部成功诊断卡片使用每周库存 SQL 同一快照里的 `diagnosis_count_from_record`，避免生产写入活跃时多条独立查询造成同屏数字短暂不一致。
- 特需门诊补仓统计镜像产品代码 `maybeReplenishCabinet`：周总量已包含 `replenished_total`，诊期补仓字段来自周三周期柜体行的累计落库值；当前生产库没有补仓流水表，页面不伪造每日历史增量，只展示累计补仓、剩余补仓上限、近 2 小时确诊、预测剩余和按当前公式估算的触发补仓量。
- 特需门诊统计只读查询 `t_special_clinic_patient_record`、`t_special_clinic_ticket_log`、`t_special_clinic_cabinet`、`t_special_clinic_player_state`、`t_backpack`、`t_compensation_batch_record`、`t_log_yuanbao` 和 `t_log_right_bottom`。
- `.env` 和本地 SQLite 文件不应提交到任何仓库。

## 端口

默认对外端口是 `18091`，容器内部端口是 `8091`。如需更改对外端口，修改 `.env` 中的 `DASHBOARD_PUBLIC_PORT`。

## ccnode 简单发布流程

目标访问地址：

```text
https://ccnode.briconbric.com/rhdashboard/
```

远端固定目录：

```text
/rhdashboard
```

### 首次远端准备

远端 `/rhdashboard/.env` 必须保留在 ccnode 服务器本地，不随代码发布。站点运行在 ccnode，统计数据源指向当前游戏服务器 `178.239.117.99`。至少需要配置真实值：

```env
DASHBOARD_PUBLIC_IP=127.0.0.1
DASHBOARD_PUBLIC_PORT=18091
PROD_DB_URL=postgresql://178.239.117.99:35432/hospital
PROD_DB_USERNAME=<READ_ONLY_USER>
PROD_DB_PASSWORD=<READ_ONLY_PASSWORD>
OPS_DASHBOARD_TIME_ZONE=Asia/Tokyo
OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS=10
```

首次使用 `/rhdashboard/` 子路径访问时，需要在 ccnode 的 nginx 上追加 location：

```bash
cd /rhdashboard
sh scripts/configure-ccnode-nginx-rhdashboard.sh
```

该脚本会写入 ccnode 的 nginx 配置，再执行 `nginx -t` 和 reload。

### 日常发布

在 Windows 本地仓库提交代码后运行：

```powershell
.\scripts\deploy-ccnode.ps1
```

发布脚本执行顺序：

1. 编译 Python 文件。
2. 运行单元测试。
3. 构建本地 Docker 镜像。
4. 将镜像保存为 `.release/*.tar`。
5. 推送当前 git 分支到 `origin`。
6. 上传镜像包、`docker-compose.yml` 和远端更新脚本到 `/rhdashboard/releases/<tag>/`。
7. 远端 `docker load` 镜像并用 `docker compose up -d --no-build` 更新容器。
8. 检查 `http://127.0.0.1:18091/healthz`。
9. 清理不再使用的旧镜像。

如只想演练上传和远端更新，不推送 git：

```powershell
.\scripts\deploy-ccnode.ps1 -SkipGitPush
```

### 回滚

远端镜像以提交号和 UTC 时间组成标签。需要回滚时，在 ccnode 上指定旧标签启动：

```bash
cd /rhdashboard
DASHBOARD_IMAGE=hospital-ops-dashboard:<OLD_TAG> docker compose up -d --no-build
```

如 nginx 配置需要回滚，使用脚本输出的备份文件恢复 ccnode 配置，随后执行：

```bash
nginx -t && systemctl reload nginx
```
