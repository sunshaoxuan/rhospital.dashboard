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
        self.assertIn("/api/stat-table", rules)
        self.assertIn("/api/item-activity-details", rules)

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
