# 荣光医院运营统计看板

这是独立于游戏工程的运营看板。统计 API 部署在流式备份节点，只读访问本机 PostgreSQL 副本；ccnode 只提供 SSO、页面和 API 转发。统计快照保存在统计 API 节点的 `data/ops_dashboard.sqlite3`。

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

- 统计 SQL 只在 `160.16.91.200` 流式备份节点执行，并连接本机只读 PostgreSQL 副本。
- ccnode 通过带 Bearer 令牌的服务端 API 获取统计结果，不再直接连接生产数据库。
- 默认启用 Firebase SSO，复用游戏现有 Firebase 项目登录，只有 `OPS_DASHBOARD_ALLOWED_EMAILS` 中的账号可以访问页面和统计 API；`/healthz` 保持开放给部署健康检查使用。
- 每次数据库连接都会设置 `default_transaction_read_only=on`。
- 页面每 60 秒刷新当前页签；四个主页签首次进入时独立并发加载，每个页签显示自己的旋转状态，已完成页签可以立即查看。
- 看板页面使用自适应版面，手机宽度下卡片、图表、页签和明细弹窗自动收缩，宽表格支持横向滑动查看。
- 看板默认使用浅灰页面底、白色内容面、橙色主强调和青绿色辅助色的亮色主题，并提供亮暗主题切换；用户选择保存在浏览器 `localStorage` 的 `ops-dashboard-theme` 中，图表坐标、图例和提示框会同步更新。
- 每 10 分钟保存一次本地每日快照。
- 每日趋势图会用生产库近14日数据覆盖同日本地快照，快照日期按 `OPS_DASHBOARD_TIME_ZONE` 计算，避免早期或错日快照遮盖真实数据。
- 数据库暂不可用时，页面仍返回空统计并在备注中显示数据源错误，避免首屏变成 500。
- 顶部今日收入拆分为 Stripe 和 Steam 两张卡片，分别展示 CNY 金额、订单数和元宝。
- 图表区包含近14日元宝消耗按小时统计、滚动最近10周元宝消耗总量，以及滚动最近10周元宝购入量；消耗口径为 `t_log_yuanbao` 中 `old_value > new_value` 的减少量，购入口径为已完成支付订单的 `yuanbao_amount`，周统计按周一至周日聚合。
- 图表下方提供道具、金钱、元宝、声望、公会、注册者统计页签，每页支持 20、50、100 条分页。
- 页面刷新按钮显示当前页签状态；后台加载中的其他页签在页签按钮上独立显示旋转状态。
- 道具页签按全量日志聚合商品购买数量、消耗数量和元宝使用数量；金钱、元宝、声望、公会按当前值从高到低排序；注册者按注册时间从新到旧排序。
- 页面顶部提供站点级页签：`运营总览` 保持原有统计画面，`特需门诊` 展示常见病特需门诊只读分析，`医托拉人` 展示医托钱包改档后的普通拉人、钱包和名片反拉数据，`跳蚤市场` 展示洗手间交易池的库存、流转和参与医院统计。
- 医托拉人页签按钱包规则上线后展示普通拉人次数、成功拉走病人数、钱包打开率、生成金币总量、平均每钱包金币、道具掉落率、名片反拉点击和反拉成功；普通拉人使用目标医院日志，钱包指标只归因普通成功拉人，反拉使用名片点击日志和反拉成功日志。
- 医托拉人链路趋势图使用双轴展示，普通拉人走左轴，反拉成功走右轴；钱包生成数和普通拉人次数保持一致，不在顶部卡片和链路趋势图重复展示，只作为打开率分母和明细审计字段保留。
- 医托钱包统计只展示钱包规则上线后的普通拉人、钱包和名片反拉数据，并按好友/非好友和拉走人数段 `1-39`、`40-69`、`70+` 拆分；钱包上线前没有金币或道具掉落机会，页面只展示当前生产规则，不展示旧规则对比。当前生产规则读取 `t_broker_wallet_rule`，钱包读取 `t_broker_wallet_drop`，名片读取 `t_broker_retaliation_voucher`。
- 跳蚤市场页展示当前可售挂单和数量、近 14 日成交频度与数量、玩家买卖医院数、活跃挂单账龄、商品流转 Top、最快消耗、买卖医院 Top 和大街成功捡取；滞销口径为活跃超过 48 小时仍未成交的挂单。
- 最快消耗中，成交耗时使用挂单 `create_time` 到首笔 `PURCHASE` 交易 `create_time` 的秒差，大街捡取耗时使用入街记录 `create_time` 到成功 `STREET_PICKUP` 交易 `create_time` 的秒差；开始和完成时间显示到秒，避免同一分钟内完成时看起来时间完全相同。
- 跳蚤市场成功捡取只统计 `t_toilet_market_transaction` 中 `transaction_type='STREET_PICKUP'` 且 `street_item_id` 非空的记录，排除每日翻找但没有捡到内容的失败尝试；玩家卖家数排除 `listing_source='ADMIN'` 的系统注入挂单，系统注入挂单和数量保留在池子提示中。
- 跳蚤市场统计只读查询 `t_toilet_market_listing`、`t_toilet_market_transaction`、`t_toilet_street_item` 和 `t_hospitals`。
- 特需门诊页按北京时间和周三开诊周期展示子页签，最新周排在最前，新一周出现后自动把上一周向后顺延；接口只计算当前选中的一周，其他周在点击后独立加载并缓存。每个周期页签包含周期每日诊断、每小时总览、病历等级分布、具体病历分布、道具奖品发放、后台补偿奖品、资源奖品发放、每周库存消耗、医院行为 Top 30、对账异常和风险提示次数。
- 周期每日诊断按选中周三周期生成 7 天序列，并按 `create_time` 的北京时间日期统计实际发生日；日确诊和累计确诊使用左轴，付费确诊使用右轴；每日表同时列出票消耗、购票和元宝成本。
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

远端 `/rhdashboard/.env` 必须保留在 ccnode 服务器本地，不随代码发布。站点运行在 ccnode，统计数据通过备份节点 API 获取。至少需要配置真实值：

```env
DASHBOARD_PUBLIC_IP=127.0.0.1
DASHBOARD_PUBLIC_PORT=18091
OPS_DASHBOARD_TIME_ZONE=Asia/Tokyo
OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS=10
OPS_DASHBOARD_STATS_API_URL=http://statistics-tunnel:18092
OPS_DASHBOARD_STATS_API_TOKEN=<SHARED_RANDOM_TOKEN>
OPS_DASHBOARD_STATS_API_TIMEOUT_SECONDS=30
OPS_DASHBOARD_URL_PREFIX=/rhdashboard
OPS_DASHBOARD_AUTH_MODE=firebase
OPS_DASHBOARD_ALLOWED_EMAILS=sunshaoxuan@gmail.com
OPS_DASHBOARD_SECRET_KEY=<LONG_RANDOM_SECRET>
OPS_DASHBOARD_FIREBASE_PROJECT_ID=r-hospital-c8069
```

Firebase Authentication 的已获授权网域需要包含 `ccnode.briconbric.com`。`OPS_DASHBOARD_URL_PREFIX=/rhdashboard` 用于登录拦截、登录成功回跳和退出登录链接，不能省略。

ccnode 通过 Compose 内的 `statistics-tunnel` 服务连接统计节点。专用 SSH 私钥保存在 `/rhdashboard/ssh/statistics-api`，已校验的主机公钥保存在 `/rhdashboard/ssh/statistics-api.known_hosts`，两者都只保留在服务器本地。备份节点的对应 `authorized_keys` 条目限制来源为 ccnode，并且只能转发到 `127.0.0.1:18092`。Dashboard 容器只访问 Compose 内部地址 `http://statistics-tunnel:18092`。

隧道使用 10 秒保活探测并由 Docker 自动重启。Dashboard 对只读 GET 请求配置连接池和有限重试，用于吸收短时 SSH 链路重连。启用远端统计 API 后，ccnode 不再启动本地数据库快照采样线程，也不需要任何 `PROD_DB_*` 变量。

首次使用 `/rhdashboard/` 子路径访问时，需要在 ccnode 的 nginx 上追加 location：

```bash
cd /rhdashboard
sh scripts/configure-ccnode-nginx-rhdashboard.sh
```

该脚本会写入 ccnode 的 nginx 配置，再执行 `nginx -t` 和 reload。

### 统计 API 节点

统计服务固定部署在 `160.16.91.200:/home/ubuntu/rhospital-statistics`，容器使用 host network 访问 `127.0.0.1:5432` 的流式只读副本。节点 `.env` 至少配置：

```env
PROD_DB_URL=postgresql://127.0.0.1:5432/hospital
PROD_DB_USERNAME=<READ_ONLY_USER>
PROD_DB_PASSWORD=<READ_ONLY_PASSWORD>
OPS_DASHBOARD_SERVICE_MODE=statistics_api
OPS_DASHBOARD_STATS_API_TOKEN=<SHARED_RANDOM_TOKEN>
OPS_DASHBOARD_TIME_ZONE=Asia/Tokyo
OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS=10
```

同一个令牌写入 ccnode 和统计节点。当前节点上游网络只开放 SSH，ccnode 通过受限端口转发访问统计 API。主机防火墙继续把 `18092` 限制为 ccnode 公网 IP 和本机访问，作为额外保护：

```bash
sudo sh scripts/configure-statistics-api-firewall.sh 203.24.89.50 18092
```

本地提交完成后先发布统计节点，再发布 ccnode：

```powershell
.\scripts\deploy-statistics-node.ps1
.\scripts\deploy-ccnode.ps1
```

日常发布会复用 `/rhdashboard/ssh` 下的专用密钥。`remote-update.sh` 在启动容器前检查私钥和主机公钥，缺少任一文件都会停止发布，避免 Dashboard 在无统计链路的状态下替换线上容器。

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
