# 菌落实验室发布验收

## 已发布状态

- 用户在本任务中明确要求完成后直接发布ccnode运行环境。
- 实现提交：dfee4856ef96，已推送origin/main。
- 两节点镜像标签：hospital-ops-dashboard:dfee4856ef96-20261002091256。
- 统计节点：160.16.91.200，rhospital-statistics-api已更新，健康状态healthy。
- ccnode：hospital-ops-dashboard运行中，rhospital-statistics-tunnel运行中且healthy。
- 顺序：先执行deploy-statistics-node.ps1并核验实际接口，再执行deploy-ccnode.ps1。
- 两个现有发布脚本均成功完成编译、69项单元测试、镜像构建、上传、替换和健康检查。统计节点首次启动连接探测出现短暂拒绝，后续探测成功。

## 实际运行结果

2026-10-02 18:20:10 JST，实际HTTPS页面通过ccnode转发至只读统计节点：

| 指标 | 实测 |
| --- | --- |
| 活跃医院 | 40 |
| 已记录结算 | 373 |
| 成功 / 失败 | 249 / 124 |
| 结算成功率 | 66.76% |
| 购买医院 / 次数 | 1 / 2 |
| 元宝消耗 / 期内复购医院 | 10 / 1 |
| 连续失败有效样本 / 均值 / 最大值 | 210 / 0.30 / 6 |

以上为查询时点快照，后续自动刷新会变化。

统计节点新接口HTTP 200，sourceError为空；未携带服务令牌时HTTP 401。ccnode实际转发新接口HTTP 200。副本健康接口返回in_recovery=true，两个实测时点延迟分别0.857秒和0.413秒。

## 浏览器验收

- 浏览器访问真实 https://ccnode.briconbric.com/rhdashboard/，未拦截或替换任何线上接口响应。
- 使用服务端产生的授权测试会话完成运行验收。会话仅在内存中传递，浏览器验收后销毁；未修改认证配置、账号白名单或Firebase设置。本次未重新执行Firebase交互式登录流程。
- 未登录浏览器实际跳转 /rhdashboard/auth/login?next=/；未登录访问新API为401。
- 初次加载的六个统计接口及既有道具明细接口全部HTTP 200。
- 新页刷新、返回缓存、顶部更新时间、主题切换通过；当前数据源无错误。
- 1440px、390px、320px均无页面横向溢出及指标卡溢出。
- 五个图表均有真实非透明绘制像素，桌面分别26978、18162、24419、37043、59898像素。
- JavaScript运行错误0，Console错误0，失败网络请求0。
- 数据源失败、空数据和医院名称注入转义在本地隔离场景验证通过。线上未注入故障或修改业务数据。

截图只保留聚合指标与图表的首屏，未记录院长或医院明细：

- [桌面1440px](bacteria-lab-1440.png)
- [手机390px](bacteria-lab-390.png)
- [手机320px](bacteria-lab-320.png)

## 源码一致性

ccnode实际容器与本地已提交源文件SHA256一致：

| 文件 | SHA256 |
| --- | --- |
| app/bacteria_stats.py | c7c2ef3a1b96274f3a222fab3cb6636fcd1290ea282a83056f68d721656a2b35 |
| app/templates/dashboard.html | 3c3d78d84d04c9e4d100787f62ade5f06478a1ad02263f9a4602ffa401f2d753 |

## 回滚资源与边界

ccnode发布前镜像另存为rhospital-dashboard-rollback:bacteria-ccnode-20261002，镜像ID为sha256:511949946ca56cf540ad1ebc0c36c761b05d92dd0843516c0dc2c862bfe7e554。现有发布脚本清理常规旧标签，这个独立标签保留。必要时可在ccnode先切回该镜像，再把同一共享看板镜像导出至统计节点恢复旧接口，环境变量、令牌和只读副本配置保持原样。

没有数据库迁移、游戏修改或业务写入。单关首次通关重试、购买意向和真实新增机会数仍受现有日志能力限制，详见需求文档。
