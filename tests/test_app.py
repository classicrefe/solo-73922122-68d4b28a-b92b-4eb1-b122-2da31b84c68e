import json
from pathlib import Path
import tempfile
import unittest

import storage
from app import create_app


class AppWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "accounting.db"
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        db = self.app.config["ACCOUNTING_DATABASE"]
        db.close()
        self.temp.cleanup()

    def test_health_and_empty_page(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("尚未开始核算", page.get_data(as_text=True))

    def test_json_buildings_and_report(self):
        response = self.client.post(
            "/api/buildings",
            json={"variances": ["1", "4"]},
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/api/reports",
            json={
                "report_id": "R1",
                "mean": "10",
                "building_ids": [1],
            },
        )
        self.assertEqual(response.status_code, 200)
        computation = response.get_json()["computation"]
        self.assertEqual(computation["status"], "ok")
        self.assertEqual(computation["estimate"], "10")
        self.assertEqual(computation["variance"], "1")

        response = self.client.post(
            "/api/reports",
            json={"id": "R2", "mean": "20", "buildings": [2]},
        )
        body = response.get_json()
        self.assertEqual(body["computation"]["estimate"], "12")
        self.assertEqual(body["computation"]["variance"], "4/5")

    def test_form_workflow_and_conflict_rendering(self):
        response = self.client.post(
            "/buildings",
            data={"variances": "1\n1\n"},
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/reports",
            data={"report_id": "R1", "mean": "1/2", "buildings": "1,2"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("combined-value", response.get_data(as_text=True))
        self.assertIn("1/2", response.get_data(as_text=True))

        # Same report number is a replacement, not a thirteenth new report.
        response = self.client.post(
            "/reports",
            data={"report_id": "R1", "mean": "3/4", "buildings": "1,2"},
        )
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("已合规重报", page)
        state = self.client.get("/api/state").get_json()
        self.assertEqual(len(state["reports"]), 1)
        self.assertEqual(state["reports"][0]["mean_text"], "3/4")

        response = self.client.post(
            "/reports",
            data={"report_id": "R2", "mean": "0.9", "building_ids": ["1", "2"]},
        )
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("conflict-panel", page)
        self.assertIn("conflict-id-list", page)
        self.assertIn("R1", page)
        self.assertIn("R2", page)
        self.assertNotIn('id="combined-value"', page)

        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["computation"]["status"], "conflict")
        self.assertEqual(state["computation"]["conflict_ids"], ["R1", "R2"])

    def test_invalid_rereport_preserves_previous_draft_and_status(self):
        self.client.post("/api/buildings", json={"variances": ["1", "1"]})
        self.client.post(
            "/api/reports",
            json={"report_id": "R1", "mean": "1/2", "building_ids": [1, 2]},
        )
        self.client.post(
            "/api/reports",
            json={"report_id": "R2", "mean": "0.9", "building_ids": [1, 2]},
        )
        before = self.client.get("/api/state").get_json()

        response = self.client.post(
            "/api/reports",
            json={"report_id": "R2", "mean": "not-exact", "building_ids": [1, 2]},
        )
        self.assertEqual(response.status_code, 400)

        after = self.client.get("/api/state").get_json()
        self.assertEqual(
            after["reports"][1]["mean_text"], before["reports"][1]["mean_text"]
        )
        self.assertEqual(after["computation"]["status"], "conflict")
        self.assertEqual(after["computation"]["conflict_ids"], ["R1", "R2"])

    def test_report_limit_and_rereport_allowed(self):
        self.client.post("/api/buildings", json={"variances": ["1"]})
        for i in range(1, storage.MAX_REPORTS + 1):
            response = self.client.post(
                "/api/reports",
                json={"report_id": f"R{i}", "mean": "1", "building_ids": [1]},
            )
            self.assertEqual(response.status_code, 200, i)

        response = self.client.post(
            "/api/reports",
            json={"report_id": "R13", "mean": "1", "building_ids": [1]},
        )
        self.assertEqual(response.status_code, 409)

        response = self.client.post(
            "/api/reports/R1/replace",
            json={"mean": "2", "building_ids": [1]},
        )
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(len(state["reports"]), 12)
        r1 = next(r for r in state["reports"] if r["report_id"] == "R1")
        self.assertEqual(r1["mean_text"], "2")

    def test_building_limit(self):
        response = self.client.post(
            "/api/buildings",
            json={"variances": [str(i) for i in range(1, 22)]},
        )
        self.assertEqual(response.status_code, 400)

    def test_persistence_and_atomic_write_failure(self):
        self.client.post("/api/buildings", json={"variances": ["1", "1"]})
        self.client.post(
            "/api/reports",
            json={"report_id": "R1", "mean": "1/2", "building_ids": [1, 2]},
        )

        # A new app/connection against the same SQLite file sees committed data.
        reopened = create_app(self.db_path)
        try:
            state = reopened.test_client().get("/api/state").get_json()
            self.assertEqual(len(state["buildings"]), 2)
            self.assertEqual(state["computation"]["estimate"], "1/2")
        finally:
            reopened.config["ACCOUNTING_DATABASE"].close()

        original_persist = storage.persist_computation

        def fail_after_report_write(_conn):
            raise RuntimeError("injected computation write failure")

        storage.persist_computation = fail_after_report_write
        try:
            response = self.client.post(
                "/api/reports",
                json={"report_id": "R2", "mean": "3/4", "building_ids": [1]},
            )
            self.assertEqual(response.status_code, 500)
        finally:
            storage.persist_computation = original_persist

        state = self.client.get("/api/state").get_json()
        self.assertEqual([r["report_id"] for r in state["reports"]], ["R1"])
        self.assertEqual(state["computation"]["estimate"], "1/2")

    def test_exact_json_decimal_tokens(self):
        self.client.post("/api/buildings", data=json.dumps({"variances": [1]}),
                         content_type="application/json")
        response = self.client.post(
            "/api/reports",
            data='{"report_id":"A","mean":0.1,"building_ids":[1]}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/api/reports",
            data='{"report_id":"B","mean":0.10000000000000001,"building_ids":[1]}',
            content_type="application/json",
        )
        body = response.get_json()
        self.assertEqual(body["computation"]["status"], "conflict")
        self.assertEqual(body["computation"]["conflict_ids"], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
