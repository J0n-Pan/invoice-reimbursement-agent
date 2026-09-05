import base64
import importlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import auth
import invoice_agent


class HttpApiPermissionTests(unittest.TestCase):
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

        self.app = importlib.import_module("app")
        self.server = self.app.ThreadingHTTPServer(("127.0.0.1", 0), self.app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for name, value in self.originals.items():
            setattr(invoice_agent, name, value)
        self.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None, cookie=None, csrf=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if cookie:
            request.add_header("Cookie", cookie)
        if csrf:
            request.add_header("X-CSRF-Token", csrf)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8")), response.headers
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8")), error.headers

    def request_bytes(self, path, method="GET", payload=None, cookie=None, csrf=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if cookie:
            request.add_header("Cookie", cookie)
        if csrf:
            request.add_header("X-CSRF-Token", csrf)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    @staticmethod
    def session_cookie(headers):
        return headers.get("Set-Cookie", "").split(";", 1)[0]

    def test_setup_login_and_server_side_role_checks(self):
        status, setup, headers = self.request(
            "/api/setup",
            method="POST",
            payload={"username": "admin", "display_name": "系统管理员", "password": "admin-pass-123"},
        )
        self.assertEqual(status, 201)
        admin_cookie = self.session_cookie(headers)
        csrf = setup["csrf_token"]

        status, me, _ = self.request("/api/me", cookie=admin_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(me["authenticated"])
        self.assertEqual(me["csrf_token"], csrf)

        status, _, _ = self.request("/api/dashboard", cookie=admin_cookie)
        self.assertEqual(status, 403)
        status, users, _ = self.request("/api/users", cookie=admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(users["items"]), 1)

        status, _, _ = self.request(
            "/api/users",
            method="POST",
            payload={
                "username": "finance",
                "display_name": "财务人员",
                "department": "财务部",
                "role": "finance",
                "password": "finance-pass-123",
            },
            cookie=admin_cookie,
            csrf=csrf,
        )
        self.assertEqual(status, 201)

        status, login, headers = self.request(
            "/api/login",
            method="POST",
            payload={"username": "finance", "password": "finance-pass-123"},
        )
        self.assertEqual(status, 200)
        finance_cookie = self.session_cookie(headers)
        status, dashboard, _ = self.request("/api/dashboard", cookie=finance_cookie)
        self.assertEqual(status, 200)
        self.assertIn("registered", dashboard)
        self.assertEqual(login["user"]["role"], "finance")

        status, archive, headers = self.request_bytes("/api/export", cookie=finance_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/zip")
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            self.assertIn("发票清单.csv", bundle.namelist())
            self.assertIn("导出异常清单.txt", bundle.namelist())

        status, _, _ = self.request("/api/users", cookie=finance_cookie)
        self.assertEqual(status, 403)
        status, _, _ = self.request("/api/invoices", cookie=admin_cookie)
        self.assertEqual(status, 403)

    def test_role_pages_and_upload_permissions(self):
        status, setup, headers = self.request(
            "/api/setup",
            method="POST",
            payload={"username": "admin", "display_name": "系统管理员", "password": "admin-pass-123"},
        )
        self.assertEqual(status, 201)
        admin_cookie = self.session_cookie(headers)
        admin_csrf = setup["csrf_token"]

        status, _, _ = self.request(
            "/api/users",
            method="POST",
            payload={
                "username": "employee",
                "display_name": "普通员工",
                "role": "employee",
                "password": "employee-pass-123",
            },
            cookie=admin_cookie,
            csrf=admin_csrf,
        )
        self.assertEqual(status, 201)
        status, login, headers = self.request(
            "/api/login",
            method="POST",
            payload={"username": "employee", "password": "employee-pass-123"},
        )
        self.assertEqual(status, 200)
        employee_cookie = self.session_cookie(headers)
        employee_csrf = login["csrf_token"]

        status, _, _ = self.request("/api/dashboard", cookie=employee_cookie)
        self.assertEqual(status, 403)
        status, _, _ = self.request("/api/upload", method="POST", payload={"file_name": "x.png", "content_base64": base64.b64encode(b"invoice").decode()}, cookie=employee_cookie, csrf=employee_csrf)
        self.assertEqual(status, 202)

        status, finance, headers = self.request(
            "/api/login",
            method="POST",
            payload={"username": "admin", "password": "admin-pass-123"},
        )
        self.assertEqual(status, 200)
        admin_cookie = self.session_cookie(headers)
        status, _, _ = self.request("/api/demo", method="POST", payload={}, cookie=admin_cookie, csrf=finance["csrf_token"])
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
