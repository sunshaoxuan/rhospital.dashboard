import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ["OPS_DASHBOARD_DISABLE_SAMPLER"] = "1"

import app.app as app_module
from app.app import app


class AppContractTest(unittest.TestCase):
    def test_healthz_returns_ok(self):
        client = app.test_client()
        response = client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_expected_routes_exist(self):
        rules = {str(rule) for rule in app.url_map.iter_rules()}

        self.assertIn("/", rules)
        self.assertIn("/healthz", rules)
        self.assertIn("/api/stats", rules)
        self.assertIn("/api/special-clinic-stats", rules)
        self.assertIn("/api/stat-table", rules)
        self.assertIn("/api/item-activity-details", rules)

    def test_dashboard_has_site_level_tabs(self):
        client = app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn('data-page-tab="overall"', html)
        self.assertIn('data-page-tab="specialClinic"', html)
        self.assertIn('id="specialClinicPage"', html)

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

    def test_special_clinic_depleted_at_select_tolerates_missing_column(self):
        sql, params = app_module.special_clinic_depleted_at_select(False)

        self.assertEqual(sql, "'' as depleted_at")
        self.assertEqual(params, ())
        self.assertNotIn("c.depleted_at", sql)

    def test_special_clinic_weekly_cabinet_query_groups_numerator(self):
        source = Path("app/app.py").read_text(encoding="utf-8")

        self.assertIn("clinic_week_start", source)
        self.assertIn("canonical_cabinet", source)
        self.assertIn("cabinet_aggregate", source)
        self.assertIn("cabinet_rank = 1", source)
        self.assertIn("clinic_date = clinic_week_start", source)
        self.assertIn("coalesce(c.initial_total, 0) as initial_total", source)
        self.assertIn("coalesce(c.remaining_total, 0) as cabinet_remaining_total", source)
        self.assertIn("coalesce(sum(total_diagnoses), 0) as total_diagnoses", source)
        self.assertIn("greatest(coalesce(c.initial_total, 0) - coalesce(a.total_diagnoses, 0), 0) as remaining_total", source)
        self.assertIn("coalesce(c.replenished_total, 0) as replenished_total", source)
        self.assertIn("coalesce(c.last_replenish_hour_key, '') as last_replenish_hour_key", source)
        self.assertIn("recent_2h_diagnoses", source)
        self.assertIn("estimated_replenishment_now", source)
        self.assertIn('"diagnosis_count": latest_week.get("diagnosis_count_from_record", 0)', source)
        self.assertIn('"cycle_day": latest_week.get("cycle_day", 0)', source)
        self.assertIn("left join record_weekly", source)

    def test_dashboard_uses_weekly_cabinet_copy(self):
        client = app.test_client()
        response = client.get("/")
        html = response.get_data(as_text=True)

        self.assertIn("每周库存消耗", html)
        self.assertIn("周总量", html)
        self.assertIn("统计剩余", html)
        self.assertIn("柜体剩余", html)
        self.assertIn("诊期补仓", html)
        self.assertIn("当前触发补仓", html)
        self.assertIn("weeklyCabinet", html)

    def test_release_target_remains_ccnode(self):
        script = Path("scripts/deploy-ccnode.ps1").read_text(encoding="utf-8")
        self.assertRegex(script, r'\[string\]\$RemoteHost\s*=\s*"ccnode\.briconbric\.com"')
        self.assertNotRegex(script, r'\[string\]\$RemoteHost\s*=\s*"178\.239\.117\.99"')

    def test_documented_stats_source_is_orangevps_only(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("https://ccnode.briconbric.com/rhdashboard/", readme)
        self.assertIn("PROD_DB_URL=postgresql://178.239.117.99:35432/hospital", readme)
        deploy_section = re.search(r"## ccnode 简单发布流程(?P<body>.*)", readme, re.S)
        self.assertIsNotNone(deploy_section)
        self.assertNotIn("http://178.239.117.99/rhdashboard/", deploy_section.group("body"))

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
