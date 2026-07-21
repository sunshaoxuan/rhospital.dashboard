import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["OPS_DASHBOARD_DISABLE_SAMPLER"] = "1"
os.environ["OPS_DASHBOARD_AUTH_MODE"] = "none"

import app.app as app_module
from app.app import app


class AppContractTest(unittest.TestCase):
    def test_healthz_returns_ok(self):
        client = app.test_client()
        response = client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_favicon_returns_empty_no_content(self):
        client = app.test_client()
        response = client.get("/favicon.ico")

        self.assertEqual(response.status_code, 204)

    def test_expected_routes_exist(self):
        rules = {str(rule) for rule in app.url_map.iter_rules()}

        self.assertIn("/", rules)
        self.assertIn("/healthz", rules)
        self.assertIn("/favicon.ico", rules)
        self.assertIn("/auth/login", rules)
        self.assertIn("/auth/firebase-login", rules)
        self.assertIn("/auth/logout", rules)
        self.assertIn("/api/stats", rules)
        self.assertIn("/api/source-health", rules)
        self.assertIn("/api/special-clinic-stats", rules)
        self.assertIn("/api/broker-stats", rules)
        self.assertIn("/api/toilet-market-stats", rules)
        self.assertIn("/api/stat-table", rules)
        self.assertIn("/api/item-activity-details", rules)

    def test_dashboard_has_site_level_tabs(self):
        client = app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn('data-page-tab="overall"', html)
        self.assertIn('data-page-tab="specialClinic"', html)
        self.assertIn('data-page-tab="broker"', html)
        self.assertIn('data-page-tab="toiletMarket"', html)
        self.assertIn('id="specialClinicPage"', html)
        self.assertIn('id="brokerPage"', html)
        self.assertIn('id="toiletMarketPage"', html)
        self.assertIn('id="clinicWeekTabs"', html)
        self.assertIn("data-clinic-week", html)
        self.assertIn('id="clinicDailyChart"', html)
        self.assertIn('id="clinicDailyRows"', html)
        self.assertIn("周期每日诊断", html)
        self.assertIn("每日诊断统计", html)
        self.assertIn("付费确诊使用右轴", html)
        self.assertIn("yAxisID: 'paid'", html)
        self.assertIn("累计确诊", html)
        self.assertIn("最近10周元宝消耗总量", html)
        self.assertIn('id="weeklyYuanbaoSpendChart"', html)
        self.assertIn("renderWeeklyYuanbaoSpendChart", html)
        self.assertIn("最近10周元宝购入量", html)
        self.assertIn('id="weeklyYuanbaoPurchaseChart"', html)
        self.assertIn("renderWeeklyYuanbaoPurchaseChart", html)
        self.assertIn("const pageLoadState", html)
        self.assertIn("PAGE_KEYS.forEach(page => refreshPage(page))", html)
        self.assertIn("setPageLoading", html)
        self.assertIn("renderCachedPage", html)
        self.assertNotIn("let activeRefresh", html)
        self.assertNotIn("let pendingRefresh", html)

    def test_dashboard_supports_persistent_light_and_dark_themes(self):
        client = app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn("ops-dashboard-theme", html)
        self.assertIn("|| 'light'", html)
        self.assertIn('html[data-theme="dark"]', html)
        self.assertIn('id="themeToggle"', html)
        self.assertIn('id="themeIcon"', html)
        self.assertIn("toggleTheme", html)
        self.assertIn("syncThemeControl", html)
        self.assertIn("--bg: #f5f7f8", html)
        self.assertIn("--primary: #ef6a32", html)
        self.assertIn("--teal: #079c9c", html)
        self.assertIn("--chart-grid", html)
        self.assertNotIn("radial-gradient", html)

    def test_login_page_uses_light_operational_style(self):
        client = app.test_client()
        response = client.get("/auth/login")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("background: #f5f7f8", html)
        self.assertIn("background: #fff", html)
        self.assertIn("background: #ef6a32", html)
        self.assertIn("rgba(7,156,156,0.24)", html)

    def test_dashboard_has_broker_stats_page(self):
        client = app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn("医托拉人", html)
        self.assertIn("api/broker-stats", html)
        self.assertIn("renderBrokerStats", html)
        self.assertIn("普通拉人次数", html)
        self.assertIn("钱包打开率", html)
        self.assertIn("道具掉落率", html)
        self.assertIn("名片反拉", html)
        self.assertIn("上线后统计明细", html)
        self.assertIn("钱包上线后医托链路趋势", html)
        self.assertIn("反拉成功使用右轴", html)
        self.assertIn("yAxisID: 'retaliation'", html)
        self.assertIn("好友和非好友，按拉走人数分段", html)
        self.assertIn("当前钱包规则", html)
        self.assertIn("钱包玩法上线后才有金币和道具掉落机会", html)
        self.assertNotIn("钱包规则前后对比", html)
        self.assertNotIn("原金币", html)
        self.assertNotIn("新金币", html)
        self.assertNotIn("钱包生成次数", html)
        self.assertNotIn("label: '钱包生成'", html)

    def test_broker_stats_query_separates_wallet_and_retaliation_sources(self):
        source = Path("app/app.py").read_text(encoding="utf-8")

        self.assertIn("load_broker_stats_from_prod", source)
        self.assertIn("t_broker_wallet_drop", source)
        self.assertIn("t_broker_retaliation_voucher", source)
        self.assertIn("t_broker_wallet_rule", source)
        self.assertIn("cutoff_at", source)
        self.assertIn("钱包上线后", source)
        self.assertIn("【%%】派遣医托从您的医院拉走了%%位病人%%", source)
        self.assertIn("【%%】顺着医托名片找了回来，从您的医院反拉走了%%位病人%%", source)
        self.assertIn("您按名片找到了对方医托，准备反拉一次。%%", source)
        self.assertIn("wallet_count", source)
        self.assertIn("relation_type", source)
        self.assertIn("patient_band", source)
        self.assertNotIn("BROKER_RULE_BASELINE", source)
        self.assertNotIn("pre_ordinary_success_count", source)

    def test_dashboard_has_toilet_market_stats_page(self):
        client = app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn("跳蚤市场", html)
        self.assertIn("api/toilet-market-stats", html)
        self.assertIn("renderToiletMarketStats", html)
        self.assertIn("当前可售挂单", html)
        self.assertIn("近14日成交", html)
        self.assertIn("滞销挂单", html)
        self.assertIn("商品热度 Top 30", html)
        self.assertIn("最快消耗 Top 20", html)
        self.assertIn("耗时秒", html)
        self.assertIn("进入渠道时间", html)
        self.assertIn("成交耗时从挂单创建算到 PURCHASE 交易", html)
        self.assertIn("卖家成交 Top 20", html)
        self.assertIn("买家购买 Top 20", html)
        self.assertIn("当前降权组合", html)
        self.assertIn("降权物品统计 Top 50", html)
        self.assertIn("扔到大街的物品统计 Top 50", html)
        self.assertIn("被乞丐捡到的物品统计 Top 50", html)
        self.assertIn('id="toiletDownrankedItemRows"', html)
        self.assertIn('id="toiletStreetThrownItemRows"', html)
        self.assertIn('id="toiletBeggarPickedItemRows"', html)
        self.assertIn('id="toiletDailyChart"', html)
        self.assertIn('id="toiletAgingChart"', html)

    def test_toilet_market_stats_use_successful_pickups_and_player_sellers(self):
        source = Path("app/app.py").read_text(encoding="utf-8")

        self.assertIn("load_toilet_market_stats_from_prod", source)
        self.assertIn("t_toilet_market_listing", source)
        self.assertIn("t_toilet_market_transaction", source)
        self.assertIn("t_toilet_street_item", source)
        self.assertIn("transaction_type = 'PURCHASE'", source)
        self.assertIn("transaction_type = 'STREET_PICKUP'", source)
        self.assertIn("street_item_id is not null", source)
        self.assertIn("listing_source = 'PLAYER'", source)
        self.assertIn("listing_source = 'ADMIN'", source)
        self.assertIn("TOILET_MARKET_STALE_HOURS = 48", source)
        self.assertIn("fastestConsumed", source)
        self.assertIn("extract(epoch from (f.sold_at - l.create_time)) as consume_seconds", source)
        self.assertIn("extract(epoch from (t.create_time - s.create_time)) as consume_seconds", source)
        self.assertIn("as consume_seconds", source)
        self.assertIn("'YYYY-MM-DD HH24:MI:SS'", source)
        self.assertIn("pickup_quantity", source)
        self.assertIn("t_toilet_market_offer_exposure", source)
        self.assertIn("t_toilet_market_offer_exposure_archive", source)
        self.assertIn("display_count = 1", source)
        self.assertIn("display_count = 2", source)
        self.assertIn("zero_rated_at is not null", source)
        self.assertIn("street_thrown_items", source)
        self.assertIn("beggar_picked_items", source)
        self.assertIn("picked_hospital_id is not null", source)

    def test_unavailable_toilet_market_stats_keep_new_item_collections(self):
        payload = app_module.load_unavailable_toilet_market_stats(RuntimeError("offline"))

        self.assertEqual(payload["downrankedItems"], [])
        self.assertEqual(payload["streetThrownItems"], [])
        self.assertEqual(payload["beggarPickedItems"], [])

    def test_overall_stats_include_weekly_yuanbao_spending(self):
        source = Path("app/app.py").read_text(encoding="utf-8")

        self.assertIn("weeklyYuanbaoSpending", source)
        self.assertIn("load_weekly_yuanbao_spending", source)
        self.assertIn("weeklyYuanbaoPurchases", source)
        self.assertIn("load_weekly_yuanbao_purchases", source)
        self.assertIn("generate_series", source)
        self.assertIn("current_week_start - interval '9 weeks'", source)
        self.assertIn("old_value > new_value", source)
        self.assertIn("coalesce(sum(coalesce(yuanbao_amount, 0)), 0) as yuanbao_purchased", source)

    def test_special_clinic_reward_charts_separate_items_and_resources(self):
        client = app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn("道具奖品发放", html)
        self.assertIn("主账资源发放", html)
        self.assertIn('id="clinicRewardItemChart"', html)
        self.assertIn("声望奖励", html)
        self.assertIn('id="clinicResourceRows"', html)
        self.assertIn("'prestige_reward'", html)
        self.assertIn("yAxisID: 'money'", html)

    def test_special_clinic_prescription_page_item_name_is_mapped(self):
        self.assertEqual(app_module.SPECIAL_CLINIC_ITEM_NAMES[1351], "荣光病志残页")

    def test_special_clinic_week_meta_labels_latest_first(self):
        first = app_module.special_clinic_week_meta({"clinic_date": "2026-07-01"}, 0)
        second = app_module.special_clinic_week_meta({"clinic_date": "2026-06-24"}, 1)

        self.assertEqual(first["key"], "2026-07-01")
        self.assertEqual(first["label"], "本周 07-01")
        self.assertEqual(first["range_label"], "07-01 至 07-07")
        self.assertEqual(second["label"], "上周 06-24")

    def test_special_clinic_week_summary_uses_weekly_record_count(self):
        summary = {"diagnosis_count": 10, "active_hospital_count": 4}

        app_module.apply_special_clinic_week_summary(summary, {
            "clinic_date": "2026-07-01",
            "diagnosis_count_from_record": 42,
            "status": "OPEN",
            "initial_total": 6000,
            "supply_total": 6000,
            "total_diagnoses": 41,
            "consume_rate": 0.68,
            "prescription_page_budget_total": 377,
            "prescription_page_awarded_total": 367,
            "prescription_page_consume_rate": 97.35,
        })

        self.assertEqual(summary["diagnosis_count"], 42)
        self.assertEqual(summary["latest_clinic_date"], "2026-07-01")
        self.assertEqual(summary["cabinet_status"], "OPEN")
        self.assertEqual(summary["supply_total"], 6000)
        self.assertEqual(summary["prescription_page_budget_total"], 377)
        self.assertEqual(summary["prescription_page_awarded_total"], 367)
        self.assertEqual(summary["prescription_page_consume_rate"], 97.35)

    def test_special_clinic_supply_metrics_use_weekly_consumption_numerator(self):
        row = app_module.add_special_clinic_supply_metrics({
            "initial_total": 100,
            "total_diagnoses": 30,
            "remaining_total": 120,
        })

        self.assertEqual(row["supply_total"], 100)
        self.assertEqual(row["consume_rate"], 30.0)

        weekly_row = app_module.add_special_clinic_supply_metrics({
            "initial_total": 6000,
            "total_diagnoses": 4192,
            "remaining_total": 1808,
            "cabinet_remaining_total": 2339,
        })

        self.assertEqual(weekly_row["supply_total"], 6000)
        self.assertEqual(weekly_row["consume_rate"], 69.87)

        prize_row = app_module.add_special_clinic_supply_metrics({
            "initial_total": 6000,
            "total_diagnoses": 3000,
            "prescription_page_budget_total": 377,
            "prescription_page_awarded_total": 367,
        })

        self.assertEqual(prize_row["prescription_page_consume_rate"], 97.35)

    def test_special_clinic_depleted_at_select_tolerates_missing_column(self):
        sql, params = app_module.special_clinic_depleted_at_select(False)

        self.assertEqual(sql, "'' as depleted_at")
        self.assertEqual(params, ())
        self.assertNotIn("c.depleted_at", sql)

    def test_special_clinic_weekly_cabinet_query_uses_canonical_inventory_numerator(self):
        source = Path("app/app.py").read_text(encoding="utf-8")

        self.assertIn("clinic_week_start", source)
        self.assertIn("t_compensation_batch_record", source)
        self.assertIn("compensated_reward_item_count", source)
        self.assertIn("load_special_clinic_compensation_rewards", source)
        self.assertIn("load_special_clinic_daily_summary", source)
        self.assertIn('"dailySummary": load_special_clinic_daily_summary(conn, week_start)', source)
        self.assertIn("special_clinic_time_filter(\"create_time\", week_start)", source)
        self.assertIn("cumulative_diagnosis_count", source)
        self.assertIn("prescription_page_budget_total", source)
        self.assertIn("prescription_page_awarded_total", source)
        self.assertIn("canonical_cabinet", source)
        self.assertIn("cabinet_aggregate", source)
        self.assertIn("cabinet_rank = 1", source)
        self.assertIn("clinic_date = clinic_week_start", source)
        self.assertIn("coalesce(c.initial_total, 0) as initial_total", source)
        self.assertIn("coalesce(c.remaining_total, 0) as cabinet_remaining_total", source)
        self.assertIn("coalesce(sum(total_diagnoses), 0) as total_diagnoses", source)
        self.assertIn("coalesce(c.remaining_total, 0) as remaining_total", source)
        self.assertIn("coalesce(c.total_diagnoses, 0) as total_diagnoses", source)
        self.assertIn("coalesce(a.total_diagnoses, 0) as weekly_cabinet_diagnoses", source)
        self.assertIn("greatest(coalesce(a.total_diagnoses, 0) - coalesce(c.total_diagnoses, 0), 0) as non_canonical_cabinet_diagnoses", source)
        self.assertIn("coalesce(c.replenished_total, 0) as replenished_total", source)
        self.assertIn("coalesce(c.last_replenish_hour_key, '') as last_replenish_hour_key", source)
        self.assertIn("recent_2h_diagnoses", source)
        self.assertIn("estimated_replenishment_now", source)
        self.assertIn('"diagnosis_count": cabinet_row.get("diagnosis_count_from_record", summary.get("diagnosis_count", 0))', source)
        self.assertIn('"cycle_day": cabinet_row.get("cycle_day", 0)', source)
        self.assertIn("left join record_weekly", source)
        self.assertIn('"weeklyPages": weekly_pages', source)
        self.assertIn('"weekOptions": week_options', source)
        self.assertIn("load_special_clinic_weekly_pages(conn, weekly_cabinet, week_start)", source)

    def test_dashboard_uses_weekly_cabinet_copy(self):
        client = app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn("每周库存消耗", html)
        self.assertIn("refresh-btn", html)
        self.assertIn("refreshSpin", html)
        self.assertIn("updateRefreshControl", html)
        self.assertIn("aria-busy", html)
        self.assertIn("刷新中", html)
        self.assertIn("门诊票流水", html)
        self.assertIn("后台补偿奖品", html)
        self.assertIn("残页奖池消耗率", html)
        self.assertIn("compensated_reward_item_count", html)
        self.assertIn("compensation_item_count", html)
        self.assertIn("prescription_page_consume_rate", html)
        self.assertIn("周总量", html)
        self.assertIn("clinic-inventory-list", html)
        self.assertIn("renderClinicCabinetRows", html)
        self.assertIn("病历池", html)
        self.assertIn("残页奖池", html)
        self.assertIn("补仓", html)
        self.assertIn("对账", html)
        self.assertIn("主柜消耗", html)
        self.assertIn("遗留日柜", html)
        self.assertIn("non_canonical_cabinet_diagnoses", html)
        self.assertIn("诊期补仓", html)
        self.assertIn("当前触发补仓", html)
        self.assertIn("weeklyCabinet", html)

    def test_release_target_remains_ccnode(self):
        script = Path("scripts/deploy-ccnode.ps1").read_text(encoding="utf-8")
        self.assertRegex(script, r'\[string\]\$RemoteHost\s*=\s*"ccnode\.briconbric\.com"')
        self.assertNotRegex(script, r'\[string\]\$RemoteHost\s*=\s*"178\.239\.117\.99"')

    def test_ccnode_statistics_api_uses_restricted_ssh_tunnel(self):
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        remote_update = Path("scripts/remote-update.sh").read_text(encoding="utf-8")

        self.assertIn("statistics-tunnel:", compose)
        self.assertIn("StrictHostKeyChecking=yes", compose)
        self.assertIn("0.0.0.0:18092:127.0.0.1:18092", compose)
        self.assertIn("ServerAliveInterval=10", compose)
        self.assertIn("ConnectionAttempts=3", compose)
        self.assertIn("condition: service_healthy", compose)
        self.assertIn("openssh-client", dockerfile)
        self.assertIn("ssh/statistics-api.known_hosts", remote_update)

    def test_statistics_node_release_targets_streaming_backup(self):
        deploy = Path("scripts/deploy-statistics-node.ps1").read_text(encoding="utf-8")
        compose = Path("docker-compose.statistics.yml").read_text(encoding="utf-8")
        firewall = Path("scripts/configure-statistics-api-firewall.sh").read_text(encoding="utf-8")

        self.assertIn('RemoteHost = "160.16.91.200"', deploy)
        self.assertIn("network_mode: host", compose)
        self.assertIn("0.0.0.0:18092", compose)
        self.assertIn("mem_limit: 384m", compose)
        self.assertIn("203.24.89.50", firewall)
        self.assertIn("--dport ${api_port} -j DROP", firewall)

    def test_stat_table_uses_single_window_count_query(self):
        source = Path("app/app.py").read_text(encoding="utf-8")

        self.assertIn("def window_paged_query", source)
        self.assertIn("count(*) over() as total_count", source)
        self.assertIn("row.pop(\"total_count\", None)", source)

    def test_production_connection_is_reused_per_worker_thread(self):
        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, *_):
                return None

        class FakeConnection:
            def __init__(self):
                self.closed = False
                self.autocommit = False
                self.read_only = False
                self.rollback_count = 0

            def cursor(self):
                return FakeCursor()

            def rollback(self):
                self.rollback_count += 1

            def close(self):
                self.closed = True

        old_state = app_module.prod_connection_state
        app_module.prod_connection_state = type(old_state)()
        fake = FakeConnection()
        try:
            with patch.object(app_module, "require_config", return_value="test"):
                with patch.object(app_module.psycopg, "connect", return_value=fake) as connect:
                    with app_module.prod_connection():
                        pass
                    with app_module.prod_connection():
                        pass

            self.assertEqual(connect.call_count, 1)
            self.assertTrue(fake.read_only)
            self.assertEqual(fake.rollback_count, 2)
        finally:
            app_module.discard_prod_connection()
            app_module.prod_connection_state = old_state

    def test_statistics_service_requires_bearer_token(self):
        old_mode = app_module.SERVICE_MODE
        old_token = app_module.STATS_API_TOKEN
        try:
            app_module.SERVICE_MODE = "statistics_api"
            app_module.STATS_API_TOKEN = "service-token"
            client = app.test_client()

            api_response = client.get("/api/stats")
            page_response = client.get("/")
            health_response = client.get("/healthz")

            self.assertEqual(api_response.status_code, 401)
            self.assertEqual(page_response.status_code, 404)
            self.assertEqual(health_response.status_code, 200)
        finally:
            app_module.SERVICE_MODE = old_mode
            app_module.STATS_API_TOKEN = old_token

    def test_dashboard_proxies_statistics_api(self):
        old_mode = app_module.SERVICE_MODE
        old_url = app_module.STATS_API_BASE_URL
        old_token = app_module.STATS_API_TOKEN
        try:
            app_module.SERVICE_MODE = "dashboard"
            app_module.STATS_API_BASE_URL = "http://160.16.91.200:18092"
            app_module.STATS_API_TOKEN = "service-token"
            with patch.object(app_module, "fetch_stats_api", return_value={"summary": {}}) as fetch:
                response = app.test_client().get("/api/broker-stats")

            self.assertEqual(response.status_code, 200)
            fetch.assert_called_once_with("/api/broker-stats")
        finally:
            app_module.SERVICE_MODE = old_mode
            app_module.STATS_API_BASE_URL = old_url
            app_module.STATS_API_TOKEN = old_token

    def test_statistics_api_session_reuses_pool_and_retries_get_requests(self):
        old_state = app_module.stats_api_session_state
        app_module.stats_api_session_state = type(old_state)()
        client = None
        try:
            client = app_module.get_stats_api_session()
            self.assertIs(client, app_module.get_stats_api_session())

            retries = client.get_adapter("http://").max_retries
            self.assertEqual(retries.total, 4)
            self.assertEqual(retries.connect, 4)
            self.assertEqual(retries.read, 2)
            self.assertIn("GET", retries.allowed_methods)
        finally:
            if client is not None:
                client.close()
            app_module.stats_api_session_state = old_state

    def test_dashboard_proxy_does_not_start_direct_database_sampler(self):
        old_url = app_module.STATS_API_BASE_URL
        old_disabled = os.environ.pop("OPS_DASHBOARD_DISABLE_SAMPLER", None)
        try:
            app_module.STATS_API_BASE_URL = "http://statistics-tunnel:18092"
            self.assertFalse(app_module.should_start_sampler())
        finally:
            app_module.STATS_API_BASE_URL = old_url
            if old_disabled is not None:
                os.environ["OPS_DASHBOARD_DISABLE_SAMPLER"] = old_disabled

    def test_special_clinic_route_loads_only_requested_week(self):
        with patch.object(app_module, "load_special_clinic_stats_from_prod", return_value={"weeklyPages": []}) as loader:
            response = app.test_client().get("/api/special-clinic-stats?week=2026-07-08")

        self.assertEqual(response.status_code, 200)
        loader.assert_called_once_with("2026-07-08")

    def test_ccnode_uses_statistics_api_without_database_firewall_dependency(self):
        remote_update = Path("scripts/remote-update.sh").read_text(encoding="utf-8")

        self.assertNotIn("rhdashboard-db-firewall.sh", remote_update)
        self.assertIn("OPS_DASHBOARD_STATS_API_URL", Path("app/app.py").read_text(encoding="utf-8"))

    def test_documented_stats_source_is_streaming_backup_api(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("https://ccnode.briconbric.com/rhdashboard/", readme)
        self.assertIn("PROD_DB_URL=postgresql://127.0.0.1:5432/hospital", readme)
        self.assertIn("OPS_DASHBOARD_STATS_API_URL=http://statistics-tunnel:18092", readme)
        self.assertIn("OPS_DASHBOARD_ALLOWED_EMAILS=sunshaoxuan@gmail.com", readme)
        self.assertIn("OPS_DASHBOARD_FIREBASE_PROJECT_ID=r-hospital-c8069", readme)
        self.assertIn("t_compensation_batch_record", readme)
        self.assertIn("后台补偿奖品", readme)
        self.assertIn("门诊票流水仅统计 `t_special_clinic_ticket_log`", readme)
        deploy_section = re.search(r"## ccnode 简单发布流程(?P<body>.*)", readme, re.S)
        self.assertIsNotNone(deploy_section)
        self.assertNotIn("http://178.239.117.99/rhdashboard/", deploy_section.group("body"))

    def test_dashboard_auth_defaults_to_firebase_sso_allowlist(self):
        source = Path("app/app.py").read_text(encoding="utf-8")

        self.assertIn('AUTH_MODE = os.getenv("OPS_DASHBOARD_AUTH_MODE", "firebase")', source)
        self.assertIn('URL_PREFIX = os.getenv("OPS_DASHBOARD_URL_PREFIX", "")', source)
        self.assertIn("prefixed_url_for", source)
        self.assertIn('"sunshaoxuan@gmail.com"', source)
        self.assertIn("AUTH_PUBLIC_ENDPOINTS", source)
        self.assertIn('"healthz"', source)
        self.assertIn("FIREBASE_PROJECT_ID", source)
        self.assertIn("FIREBASE_CERTS_URL", source)
        self.assertIn("verify_firebase_id_token", source)
        self.assertIn("signInWithPopup", source)
        self.assertIn("/auth/firebase-login", source)

    def test_auth_redirect_respects_subpath_prefix(self):
        old_auth_mode = app_module.AUTH_MODE
        old_url_prefix = app_module.URL_PREFIX
        old_secret_key = app.secret_key
        old_secret_env = os.environ.get("OPS_DASHBOARD_SECRET_KEY")
        try:
            app_module.AUTH_MODE = "firebase"
            app_module.URL_PREFIX = "/rhdashboard"
            app.secret_key = "test-secret"
            os.environ["OPS_DASHBOARD_SECRET_KEY"] = "test-secret"
            client = app.test_client()

            response = client.get("/")

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/rhdashboard/auth/login?next=/")
        finally:
            app_module.AUTH_MODE = old_auth_mode
            app_module.URL_PREFIX = old_url_prefix
            app.secret_key = old_secret_key
            if old_secret_env is None:
                os.environ.pop("OPS_DASHBOARD_SECRET_KEY", None)
            else:
                os.environ["OPS_DASHBOARD_SECRET_KEY"] = old_secret_env

    def test_firebase_login_redirect_respects_subpath_prefix(self):
        old_auth_mode = app_module.AUTH_MODE
        old_url_prefix = app_module.URL_PREFIX
        old_secret_key = app.secret_key
        old_secret_env = os.environ.get("OPS_DASHBOARD_SECRET_KEY")
        try:
            app_module.AUTH_MODE = "firebase"
            app_module.URL_PREFIX = "/rhdashboard"
            app.secret_key = "test-secret"
            os.environ["OPS_DASHBOARD_SECRET_KEY"] = "test-secret"
            client = app.test_client()

            with patch.object(
                app_module,
                "verify_firebase_id_token",
                return_value={"email": "sunshaoxuan@gmail.com", "sub": "test-uid"},
            ):
                root_response = client.post(
                    "/auth/firebase-login",
                    json={"idToken": "token", "next": "/"},
                )
                prefixed_response = client.post(
                    "/auth/firebase-login",
                    json={"idToken": "token", "next": "/rhdashboard/"},
                )

            self.assertEqual(root_response.status_code, 200)
            self.assertEqual(root_response.get_json()["redirect"], "/rhdashboard/")
            self.assertEqual(prefixed_response.status_code, 200)
            self.assertEqual(prefixed_response.get_json()["redirect"], "/rhdashboard/")
        finally:
            app_module.AUTH_MODE = old_auth_mode
            app_module.URL_PREFIX = old_url_prefix
            app.secret_key = old_secret_key
            if old_secret_env is None:
                os.environ.pop("OPS_DASHBOARD_SECRET_KEY", None)
            else:
                os.environ["OPS_DASHBOARD_SECRET_KEY"] = old_secret_env

    def test_merge_snapshot_history_prefers_recent_prod_recharge(self):
        old_data_dir = app_module.DATA_DIR
        old_sqlite_path = app_module.SQLITE_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            app_module.DATA_DIR = Path(temp_dir)
            app_module.SQLITE_PATH = Path(temp_dir) / "ops_dashboard.sqlite3"
            try:
                app_module.ensure_snapshot_table()
                with app_module.sqlite_connection() as conn:
                    conn.execute(
                        """
                        insert into daily_snapshot (
                            day, generated_at, online_now_accounts, max_online_now_accounts,
                            active_today_accounts, registrations_today,
                            recharge_cny_today, recharge_yuanbao_today, recharge_orders_today,
                            skin_owner_accounts, skin_equipped_accounts, skin_free_accounts,
                            skin_purchase_log_accounts, skin_paid_confirmed_accounts
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        ("2026-06-22", "2026-06-22T00:00:00+09:00", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                    )
                stats = {
                    "dailyActive": [{"day": "2026-06-22", "count": 12}],
                    "dailyRegistrations": [{"day": "2026-06-22", "count": 3}],
                    "dailyRecharge": [
                        {"day": "2026-06-22", "currency": "cny", "orders": 1, "amount": 120.0, "yuanbao": 750}
                    ],
                }

                merged = app_module.merge_snapshot_history(stats)

                self.assertEqual(merged["dailyActive"], [{"day": "2026-06-22", "count": 12}])
                self.assertEqual(merged["dailyRegistrations"], [{"day": "2026-06-22", "count": 3}])
                self.assertEqual(
                    merged["dailyRecharge"],
                    [{"day": "2026-06-22", "currency": "cny", "orders": 1, "amount": 120.0, "yuanbao": 750}],
                )
            finally:
                app_module.DATA_DIR = old_data_dir
                app_module.SQLITE_PATH = old_sqlite_path


if __name__ == "__main__":
    unittest.main()
