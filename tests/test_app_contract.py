import os
import unittest

os.environ["OPS_DASHBOARD_DISABLE_SAMPLER"] = "1"

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
        self.assertIn("/api/item-activity-details", rules)


if __name__ == "__main__":
    unittest.main()
