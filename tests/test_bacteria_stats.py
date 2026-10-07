import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ["OPS_DASHBOARD_DISABLE_SAMPLER"] = "1"
os.environ["OPS_DASHBOARD_AUTH_MODE"] = "none"

from app.bacteria_stats import build_bacteria_stats, load_bacteria_stats, unavailable_bacteria_stats
import app.app as app_module


class BacteriaStatsTest(unittest.TestCase):
    now = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)

    def build(self, events=(), purchases=(), players=()):
        return build_bacteria_stats(events, purchases, players, [], self.now, "Asia/Tokyo")

    def event(self, id, kind, hospital=1, at="2026-10-01T03:00:00"):
        return {"id": id, "kind": kind, "hospital_id": hospital, "create_time": at}

    def test_empty_window_has_seven_days_and_undefined_rates(self):
        data = self.build()
        self.assertEqual(len(data["dailyTrend"]), 7)
        self.assertEqual(data["windowStart"], "2026-09-26")
        self.assertEqual(data["windowEnd"], "2026-10-02")
        self.assertEqual(len(data["hourly"]), 24)
        self.assertIsNone(data["summary"]["win_rate"])
        self.assertIsNone(data["summary"]["avg_retry"])
        self.assertEqual(data["summary"]["settlements"], 0)

    def test_retries_exclude_first_success_and_trailing_losses(self):
        events = [self.event(i, k) for i, k in enumerate(
            ["LOST", "LOST", "WON", "LOST", "LOST", "WON", "WON", "LOST"], 1)]
        data = self.build(list(reversed(events)))
        s = data["summary"]
        self.assertEqual(s["retry_samples"], 2)
        self.assertEqual(s["avg_retry"], 1)
        self.assertEqual(s["max_retry"], 2)
        self.assertEqual(s["losses"], 5)
        self.assertEqual(data["hospitals"][0]["trailing_losses"], 1)
        self.assertEqual(sum(r["wins"] for r in data["retryDistribution"]), 2)

    def test_daily_active_hospitals_deduplicate_settlements_per_local_day(self):
        data = self.build([
            self.event(1, "WON", at="2026-10-01T14:59:59Z"),
            self.event(2, "LOST", at="2026-10-01T14:59:59Z"),
            self.event(3, "LOST", 2, at="2026-10-01T14:59:59Z"),
            self.event(4, "EMPTY", 3, at="2026-10-01T14:59:59Z"),
            self.event(5, "WON", at="2026-10-01T15:00:00Z"),
            self.event(6, "LOST", at="2026-10-02T08:00:00Z"),
        ], players=[{"hospital_id": 4, "active_attempt_id": "unsettled"}])
        self.assertEqual([r["active_hospitals"] for r in data["dailyTrend"]],
                         [0, 0, 0, 0, 0, 2, 1])
        self.assertEqual([r["settlements"] for r in data["dailyTrend"]][-2:], [3, 2])
        self.assertEqual(data["summary"]["active_hospitals"], 2)
        self.assertEqual([r["day"] for r in data["dailyTrend"]][-2:],
                         ["2026-10-01", "2026-10-02"])
        self.assertNotIn("frequency", data)

    def test_hospital_sequences_are_independent_and_empty_is_not_settlement(self):
        data = self.build([self.event(1, "WON"), self.event(2, "LOST", 2),
            self.event(3, "EMPTY"), self.event(4, "WON"), self.event(5, "WON", 2)])
        self.assertEqual(data["summary"]["settlements"], 4)
        self.assertEqual(data["summary"]["active_hospitals"], 2)
        self.assertEqual(data["summary"]["retry_samples"], 1)
        self.assertEqual(data["summary"]["avg_retry"], 0)
        self.assertEqual(data["summary"]["empty_requests"], 1)

    def test_timezone_boundaries_zero_days_and_future_exclusion(self):
        events = [self.event(1, "WON", at="2026-09-25T14:59:59"),
            self.event(2, "LOST", at="2026-09-25T15:00:00"),
            self.event(3, "WON", at="2026-10-01T15:00:00Z"),
            self.event(4, "WON", at="2026-10-02T09:00:01Z")]
        data = self.build(events)
        self.assertEqual(data["summary"]["settlements"], 2)
        self.assertEqual(data["dailyTrend"][0]["losses"], 1)
        self.assertEqual(data["dailyTrend"][-1]["wins"], 1)
        self.assertEqual(data["dailyTrend"][1]["settlements"], 0)
        self.assertEqual(data["hourly"][0]["wins"], 1)
        self.assertEqual(data["hospitals"][0]["active_days"], 2)
        self.assertEqual(data["summary"]["per_active_day"], 1)

    def test_actual_purchases_repurchases_and_inactive_buyers(self):
        purchases = [{"hospital_id": h, "create_time": "2026-10-01T03:00:00", "spent": n}
            for h, n in [(1, 5), (1, 5), (2, 5), (1, 0), (1, -5)]]
        data = self.build([self.event(1, "WON")], purchases)
        s = data["summary"]
        self.assertEqual(s["buyers"], 2)
        self.assertEqual(s["purchase_count"], 3)
        self.assertEqual(s["yuanbao"], 15)
        self.assertEqual(s["repeat_buyers"], 1)
        self.assertEqual(s["active_buyer_rate"], 100)
        self.assertEqual(data["dailyTrend"][-2]["buyers"], 2)
        self.assertEqual(len(data["buyerHospitals"]), 2)

    def test_state_views_do_not_count_as_play_and_snapshot_cannot_create_events(self):
        players = [{"hospital_id": 1, "highest_cleared_level": 20},
            {"hospital_id": 2, "highest_cleared_level": 6, "last_settled_attempt_id": "private"},
            {"hospital_id": 3, "highest_cleared_level": 0, "active_attempt_id": "private"}]
        data = self.build([self.event(1, "LOST", 2)], players=players)
        self.assertEqual(data["summary"]["state_hospitals"], 3)
        self.assertEqual(data["summary"]["ever_played_hospitals"], 2)
        self.assertEqual(sum(r["hospitals"] for r in data["progress"]), 2)
        self.assertEqual(data["summary"]["wins"], 0)
        self.assertNotIn("private", json.dumps(data))
        self.assertEqual(players[1]["last_settled_attempt_id"], "private")

    def test_buyers_not_lost_when_outside_usage_top_fifty(self):
        events = [self.event(i, "WON", i) for i in range(1, 61)]
        purchases = [{"hospital_id": 70, "create_time": "2026-10-01T03:00:00", "spent": 5}]
        data = self.build(events, purchases)
        self.assertEqual(len(data["hospitals"]), 50)
        self.assertEqual(data["hospitalCount"], 61)
        self.assertEqual(data["buyerHospitals"][0]["hospital_id"], 70)

    def test_sql_is_parameterized_read_only_and_uses_utc_bounds(self):
        calls = []
        def query(conn, sql, params=()):
            calls.append((sql, params))
            return []
        load_bacteria_stats(object(), query, self.now, "Asia/Tokyo")
        self.assertEqual(len(calls), 4)
        for sql, params in calls:
            self.assertEqual(sql.count("%s"), len(params))
            self.assertNotRegex(sql.lower(), r"\b(insert|update|delete|alter|drop)\b")
        self.assertEqual(calls[0][1][2], datetime(2026, 9, 25, 15))
        self.assertIn("old_value > new_value", calls[1][0])
        self.assertIn("reason in (%s, %s)", calls[1][0])
        self.assertIn("重玩奖励", calls[3][0])

    def test_unavailable_is_distinct_from_zero(self):
        result = unavailable_bacteria_stats(RuntimeError("offline"), self.now, "Asia/Tokyo")
        self.assertEqual(result["sourceError"], "offline")
        self.assertEqual(result["summary"], {})

    def test_api_forwarding_and_source_failure(self):
        client = app_module.app.test_client()
        with patch.object(app_module, "use_stats_api", return_value=True), patch.object(app_module, "fetch_stats_api", return_value={"summary": {"buyers": 2}}) as fetch:
            response = client.get("/api/bacteria-lab-stats")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["summary"]["buyers"], 2)
            fetch.assert_called_once_with("/api/bacteria-lab-stats")
        with patch.object(app_module, "use_stats_api", return_value=True), patch.object(app_module, "fetch_stats_api", side_effect=RuntimeError("offline")):
            response = client.get("/api/bacteria-lab-stats")
            self.assertEqual(response.json["sourceError"], "offline")
            self.assertEqual(response.json["summary"], {})

    def test_dashboard_wires_page_and_null_metrics(self):
        html = app_module.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('data-page-tab="bacteriaLab"', html)
        self.assertIn('id="bacteriaLabPage"', html)
        self.assertIn("fetchJson('api/bacteria-lab-stats')", html)
        self.assertIn("value == null || !available ? '-'", html)
        self.assertIn("最近7天每日活跃医院数", html)
        self.assertIn('id="bacteriaActiveDailyChart"', html)
        self.assertIn("data: rows.map(r => r.active_hospitals)", html)
        self.assertNotIn("bacteriaFrequencyChart", html)
        self.assertNotIn("data.frequency", html)

    def test_new_api_requires_dashboard_login_and_service_token(self):
        client = app_module.app.test_client()
        with patch.object(app_module, "AUTH_MODE", "firebase"), patch.object(app_module, "auth_setup_error", return_value=None):
            self.assertEqual(client.get("/api/bacteria-lab-stats").status_code, 401)
        with patch.object(app_module, "SERVICE_MODE", "statistics_api"), patch.object(app_module, "STATS_API_TOKEN", "test-only"):
            self.assertEqual(client.get("/api/bacteria-lab-stats").status_code, 401)
            self.assertEqual(client.get("/api/bacteria-lab-stats", headers={"Authorization": "Bearer incorrect"}).status_code, 401)
            with patch.object(app_module, "use_stats_api", return_value=True), patch.object(app_module, "fetch_stats_api", return_value={"summary": {}}):
                self.assertEqual(client.get("/api/bacteria-lab-stats", headers={"Authorization": "Bearer test-only"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
