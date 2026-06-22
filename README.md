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
- 每 10 分钟保存一次本地每日快照。
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

远端 `/rhdashboard/.env` 必须保留在服务器本地，不随代码发布。至少需要配置真实值：

```env
DASHBOARD_PUBLIC_IP=127.0.0.1
DASHBOARD_PUBLIC_PORT=18091
PROD_DB_URL=postgresql://<PROD_DB_HOST>:<PROD_DB_PORT>/<PROD_DB_NAME>
PROD_DB_USERNAME=<READ_ONLY_USER>
PROD_DB_PASSWORD=<READ_ONLY_PASSWORD>
OPS_DASHBOARD_TIME_ZONE=Asia/Tokyo
OPS_DASHBOARD_QUERY_TIMEOUT_SECONDS=10
```

首次使用 `/rhdashboard/` 子路径访问时，需要在 ccnode 上追加 Nginx location：

```bash
cd /rhdashboard
sh scripts/configure-ccnode-nginx-rhdashboard.sh
```

该脚本会先备份 `/etc/nginx/conf.d/xray.conf`，再执行 `nginx -t` 和 `systemctl reload nginx`。

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

如 Nginx 配置需要回滚，使用脚本输出的备份文件覆盖 `/etc/nginx/conf.d/xray.conf`，随后执行：

```bash
nginx -t && systemctl reload nginx
```
