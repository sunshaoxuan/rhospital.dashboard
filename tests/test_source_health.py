import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["OPS_DASHBOARD_DISABLE_SAMPLER"] = "1"
os.environ["OPS_DASHBOARD_AUTH_MODE"] = "none"

import app.app as m


class SourceHealthTest(unittest.TestCase):
    def healthy(self, **changes):
        source = dict(in_recovery=True, transaction_read_only=True, replay_paused=False,
            receiver_status="streaming", receive_delay_seconds=1, replay_delay_seconds=1,
            wal_lag_bytes=0)
        source.update(changes)
        return source

    def test_streaming_current_replica_is_healthy(self):
        self.assertEqual(m.source_health_status(self.healthy())["status"], "ok")

    def test_disconnected_replica_with_null_delay_is_unavailable(self):
        result = m.source_health_status(self.healthy(receiver_status=None, replay_delay_seconds=None))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("disconnected", result["error"])

    def test_primary_and_writable_connections_are_rejected(self):
        for changes in [dict(in_recovery=False), dict(transaction_read_only=False)]:
            with self.subTest(changes=changes):
                self.assertEqual(m.source_health_status(self.healthy(**changes))["status"], "unavailable")

    def test_paused_and_missing_progress_are_rejected(self):
        for changes in [dict(replay_paused=True), dict(replay_delay_seconds=None), dict(wal_lag_bytes=None)]:
            with self.subTest(changes=changes):
                self.assertEqual(m.source_health_status(self.healthy(**changes))["status"], "unavailable")

    def test_heartbeat_and_replay_lag_thresholds(self):
        self.assertEqual(m.source_health_status(self.healthy(receive_delay_seconds=120))["status"], "ok")
        for changes in [dict(receive_delay_seconds=121), dict(receive_delay_seconds=None),
                        dict(wal_lag_bytes=100, replay_delay_seconds=121)]:
            with self.subTest(changes=changes):
                self.assertEqual(m.source_health_status(self.healthy(**changes))["status"], "unavailable")

    def test_idle_primary_does_not_make_caught_up_replica_stale(self):
        self.assertEqual(m.source_health_status(self.healthy(replay_delay_seconds=86400))["status"], "ok")

    def test_statistics_queries_never_run_against_failed_source(self):
        conn = MagicMock()
        with patch.object(m, "SERVICE_MODE", "statistics_api"), patch.object(m, "get_prod_connection", return_value=conn), patch.object(m, "read_source_health", return_value={"status": "unavailable", "error": "replication disconnected"}):
            with self.assertRaisesRegex(RuntimeError, "replication disconnected"):
                with m.prod_connection():
                    self.fail("Business query reached an unhealthy source")
        conn.rollback.assert_called_once()

    def test_healthy_statistics_context_runs_read_and_rolls_back(self):
        conn = MagicMock()
        with patch.object(m, "SERVICE_MODE", "statistics_api"), patch.object(m, "get_prod_connection", return_value=conn), patch.object(m, "read_source_health", return_value={"status": "ok"}):
            with m.prod_connection() as current:
                self.assertIs(current, conn)
        conn.rollback.assert_called_once()

    def test_source_health_http_status_tracks_actual_health(self):
        conn = MagicMock()
        for status, code in [("ok", 200), ("unavailable", 503)]:
            with self.subTest(status=status), patch.object(m, "use_stats_api", return_value=False), patch.object(m, "get_prod_connection", return_value=conn), patch.object(m, "read_source_health", return_value={"status": status}):
                response = m.app.test_client().get("/api/source-health")
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json["status"], status)
        self.assertEqual(conn.rollback.call_count, 2)

    def test_stale_source_returns_explicit_bacteria_error_not_zero_summary(self):
        conn = MagicMock()
        with patch.object(m, "SERVICE_MODE", "statistics_api"), patch.object(m, "STATS_API_TOKEN", "test-only"), patch.object(m, "get_prod_connection", return_value=conn), patch.object(m, "read_source_health", return_value={"status": "unavailable", "error": "replication disconnected"}):
            response = m.app.test_client().get("/api/bacteria-lab-stats", headers={"Authorization": "Bearer test-only"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["summary"], {})
            self.assertEqual(response.json["sourceError"], "replication disconnected")

    def test_overall_source_failure_is_explicit_and_rejected_by_page_loader(self):
        data = m.load_unavailable_stats(RuntimeError("replication disconnected"))
        self.assertEqual(data["sourceError"], "replication disconnected")
        html = m.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("if (data.sourceError) throw new Error(data.sourceError);", html)


if __name__ == "__main__":
    unittest.main()
