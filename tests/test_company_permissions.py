import tempfile
import unittest
import zipfile
from pathlib import Path

import auth
import invoice_agent


class CompanyPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.originals = {
            "DATA_DIR": invoice_agent.DATA_DIR,
            "UPLOAD_DIR": invoice_agent.UPLOAD_DIR,
            "LEGACY_UPLOAD_DIR": invoice_agent.LEGACY_UPLOAD_DIR,
            "DB_PATH": invoice_agent.DB_PATH,
        }
        invoice_agent.DATA_DIR = root / "data"
        invoice_agent.UPLOAD_DIR = root / "upload_invoice"
        invoice_agent.LEGACY_UPLOAD_DIR = invoice_agent.DATA_DIR / "uploads"
        invoice_agent.DB_PATH = invoice_agent.DATA_DIR / "invoice_agent.db"
        invoice_agent.init_db()
        auth.init_auth_schema()

        self.admin = auth.create_first_admin("admin", "admin-pass-123", "系统管理员")
        self.employee = auth.create_user("employee", "employee-pass-123", "employee", "普通员工")
        self.other_employee = auth.create_user("employee2", "employee2-pass-123", "employee", "另一名员工")
        self.finance = auth.create_user("finance", "finance-pass-123", "finance", "财务人员")

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(invoice_agent, name, value)
        self.temp_dir.cleanup()

    def test_login_session_and_csrf(self):
        user = auth.authenticate("employee", "employee-pass-123")
        token, csrf = auth.start_session(user["id"])
        current = auth.current_user(f"{auth.SESSION_COOKIE}={token}")
        self.assertEqual(current["id"], user["id"])
        self.assertEqual(current["csrf_token"], csrf)
        auth.verify_csrf(current, csrf)
        with self.assertRaises(auth.AuthorizationError):
            auth.verify_csrf(current, "wrong-token")

    def test_change_password_revokes_existing_sessions(self):
        user = auth.authenticate("employee", "employee-pass-123")
        token, _ = auth.start_session(user["id"])

        with self.assertRaises(ValueError):
            auth.change_password(user["id"], "wrong-current-pass", "employee-new-pass-123")
        self.assertIsNotNone(auth.current_user(f"{auth.SESSION_COOKIE}={token}"))

        auth.change_password(user["id"], "employee-pass-123", "employee-new-pass-123")

        self.assertIsNone(auth.current_user(f"{auth.SESSION_COOKIE}={token}"))
        with self.assertRaises(auth.AuthenticationError):
            auth.authenticate("employee", "employee-pass-123")
        self.assertEqual(auth.authenticate("employee", "employee-new-pass-123")["id"], user["id"])

    def test_invoice_scope_and_duplicate_protection(self):
        job_id = invoice_agent.create_upload("原文件名.png", b"same-invoice", self.employee["id"])
        self.assertEqual(
            invoice_agent.create_upload("换了名字.png", b"same-invoice", self.employee["id"]),
            job_id,
        )
        with self.assertRaises(ValueError):
            invoice_agent.create_upload("另一账号.png", b"same-invoice", self.other_employee["id"])

        self.assertEqual(invoice_agent.get_invoice(job_id, self.employee)["id"], job_id)
        self.assertEqual(len(invoice_agent.list_invoices(actor=self.employee)), 1)
        self.assertGreaterEqual(len(invoice_agent.list_invoices(actor=self.finance)), 1)
        with self.assertRaises(PermissionError):
            invoice_agent.get_invoice(job_id, self.other_employee)
        with self.assertRaises(PermissionError):
            invoice_agent.get_invoice(job_id, self.admin)

    def test_upload_directory_and_archive_export(self):
        job_id = invoice_agent.create_upload("原始发票.png", b"archive-invoice", self.employee["id"])
        with invoice_agent.connect() as conn:
            stored_path = Path(conn.execute("SELECT file_path FROM invoices WHERE id=?", (job_id,)).fetchone()[0])

        relative_parts = stored_path.relative_to(invoice_agent.UPLOAD_DIR).parts
        self.assertRegex(relative_parts[0], r"^\d{4}$")
        self.assertRegex(relative_parts[1], r"^\d{2}$")
        self.assertRegex(relative_parts[2], r"^\d{2}$")
        self.assertEqual(relative_parts[3], job_id)
        self.assertEqual(stored_path.read_bytes(), b"archive-invoice")

        archive_path = invoice_agent.export_archive(self.finance)
        try:
            with zipfile.ZipFile(archive_path) as bundle:
                names = bundle.namelist()
                self.assertIn("发票清单.csv", names)
                self.assertIn("导出异常清单.txt", names)
                original_name = f"发票原件/{stored_path.relative_to(invoice_agent.UPLOAD_DIR).as_posix()}"
                self.assertIn(original_name, names)
                self.assertIn(job_id, bundle.read("发票清单.csv").decode("utf-8-sig"))
                self.assertEqual(bundle.read(original_name), b"archive-invoice")
        finally:
            archive_path.unlink(missing_ok=True)

    def test_pass_requires_finance_confirmation(self):
        job_id = invoice_agent.create_upload("待确认.jpg", b"finance-confirmation", self.employee["id"])
        processed = invoice_agent.process_invoice(
            job_id,
            manual_fields={
                "invoice_no": "TEST-20260905-001",
                "invoice_date": "2026-09-05",
                "seller": "测试销售方有限公司",
                "buyer": "测试购买方有限公司",
                "amount": 100,
                "tax": 13,
                "total": 113,
                "confidence": 0.99,
            },
        )
        self.assertEqual(processed["status"], "finance_pending")
        registered = invoice_agent.review_invoice(job_id, "pass", actor=self.finance)
        self.assertEqual(registered["status"], "registered")
        with self.assertRaises(PermissionError):
            invoice_agent.review_invoice(job_id, "pass", actor=self.admin)

    def test_admin_cannot_manage_finance_records(self):
        with self.assertRaises(PermissionError):
            invoice_agent.list_invoices(actor=self.admin)
        with self.assertRaises(PermissionError):
            invoice_agent.dashboard(actor=self.admin)


if __name__ == "__main__":
    unittest.main()
