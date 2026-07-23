# 执行记录

以下命令均在 `C:\workspace\rhospital.dashboard` 执行。

1. `git status --short --branch`
2. `rg` 检索运营总览、支付表、医院字段和现有测试。
3. 读取同工作区后端的三类支付订单实体，确认 `hospital_id`。
4. `python -m unittest discover -s tests -v`
5. 启动本地 Flask 页面，使用浏览器检查桌面端和移动端布局、滚动区域、控制台与截图。
6. `git diff --check`
