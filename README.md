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
