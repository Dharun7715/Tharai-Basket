"""Regression smoke tests for the Tharai Basket Flask application.

Run with: python -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class TharaiBasketSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.previous_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(cls.temp_dir.name, "test.db")

        from app import create_app, db

        cls.app = create_app()
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-only-secret")
        with cls.app.app_context():
            db.drop_all()
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        if cls.previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = cls.previous_database_url
        cls.temp_dir.cleanup()

    def setUp(self):
        self.client = self.app.test_client()

    def test_health_endpoint_returns_ok(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_security_headers_are_present(self):
        response = self.client.get("/health")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(response.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")

    def test_admin_quality_reports_requires_admin_login(self):
        response = self.client.get("/admin/issues")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_backup_cli_creates_sqlite_backup(self):
        result = self.app.test_cli_runner().invoke(args=["backup-db"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Database backup created:", result.output)

    def test_homepage_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Tharai Basket", response.data)

    def test_checkout_requires_customer_login(self):
        response = self.client.get("/checkout")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_order_history_requires_login(self):
        response = self.client.get("/my-orders")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_admin_dashboard_requires_login(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_delivery_slots_api_returns_json_for_date(self):
        selected = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
        response = self.client.get("/api/delivery-slots?date=" + selected.isoformat())
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["date"], selected.isoformat())
        self.assertIsInstance(payload["slots"], list)

    def test_delivery_slots_reject_date_outside_four_day_window(self):
        selected = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=4)
        response = self.client.get("/api/delivery-slots?date=" + selected.isoformat())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["slots"], [])


if __name__ == "__main__":
    unittest.main()
