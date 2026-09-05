"""Windows Web service for the invoice reimbursement Agent."""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from auth import (
    AuthenticationError,
    AuthorizationError,
    authenticate,
    change_password,
    clear_cookie_header,
    cookie_header,
    create_first_admin,
    create_user,
    current_user,
    end_session,
    init_auth_schema,
    list_users,
    record_audit,
    require_role,
    reset_password,
    setup_required,
    start_session,
    update_user,
    verify_csrf,
)
from invoice_agent import (
    create_upload,
    dashboard,
    delete_invoice,
    export_archive,
    get_invoice,
    get_invoice_original,
    list_invoices,
    process_invoice,
    review_invoice,
)


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_PORT", "8765"))
AGENT_WORKERS = max(1, min(int(os.environ.get("AGENT_WORKERS", "4")), 16))
PROCESS_POOL = ThreadPoolExecutor(max_workers=AGENT_WORKERS, thread_name_prefix="invoice-agent")
BUSINESS_ROLES = {"employee", "finance"}

init_auth_schema()


def _secure_cookies() -> bool:
    return os.environ.get("AGENT_SECURE_COOKIES", "0").lower() in {"1", "true", "yes"}


def submit_job(job_id: str) -> None:
    PROCESS_POOL.submit(process_invoice, job_id)


def resume_pending_jobs() -> None:
    for item in list_invoices(status="pending", limit=200):
        submit_job(item["id"])


def public_user(user: dict | None) -> dict | None:
    if not user:
        return None
    return {
        key: user.get(key)
        for key in ("id", "username", "display_name", "role", "role_label", "department", "active")
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "InvoiceAgent/2.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, payload: object, status: int = HTTPStatus.OK, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str, download_name: str, status: int = HTTPStatus.OK) -> None:
        """Stream a generated file and remove the temporary export afterwards."""
        try:
            size = path.stat().st_size
            encoded_name = quote(download_name, safe="")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="invoice_archive.zip"; filename*=UTF-8\'\'{encoded_name}',
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            path.unlink(missing_ok=True)

    def send_redirect(self, location: str, status: int = HTTPStatus.FOUND) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def request_user(self, roles: set[str] | None = None, *, csrf: bool = False) -> dict | None:
        user = current_user(self.headers.get("Cookie"))
        try:
            user = require_role(user, roles or {"employee", "finance", "admin"})
            if csrf:
                verify_csrf(user, self.headers.get("X-CSRF-Token"))
            return user
        except AuthenticationError as exc:
            self.send_json({"error": str(exc), "setup_required": setup_required()}, HTTPStatus.UNAUTHORIZED)
        except AuthorizationError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        return None

    def payload(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 25 * 1024 * 1024:
            raise ValueError("单个请求不能超过 25MB")
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求数据格式错误") from exc
        if not isinstance(value, dict):
            raise ValueError("请求数据必须是对象")
        return value

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/login", "/login.html"}:
            user = current_user(self.headers.get("Cookie"))
            if user:
                return self.send_redirect("/workspace.html")
            if setup_required():
                return self.send_redirect("/setup.html")
            return self.serve_static("/login.html")
        if parsed.path in {"/setup", "/setup.html"}:
            if current_user(self.headers.get("Cookie")):
                return self.send_redirect("/workspace.html")
            if not setup_required():
                return self.send_redirect("/login.html")
            return self.serve_static("/setup.html")
        if parsed.path in {"/workspace", "/workspace.html", "/index.html"}:
            if not current_user(self.headers.get("Cookie")):
                return self.send_redirect("/setup.html" if setup_required() else "/login.html")
            return self.serve_static("/index.html")
        if parsed.path == "/api/me":
            user = current_user(self.headers.get("Cookie"))
            return self.send_json(
                {
                    "authenticated": bool(user),
                    "setup_required": setup_required(),
                    "user": public_user(user),
                    "csrf_token": user.get("csrf_token", "") if user else "",
                }
            )

        if parsed.path.startswith("/api/"):
            user = self.request_user()
            if not user:
                return
            if parsed.path == "/api/users":
                if not self.request_user({"admin"}):
                    return
                return self.send_json({"items": list_users()})
            if parsed.path == "/api/dashboard":
                finance_user = self.request_user({"finance"})
                if not finance_user:
                    return
                return self.send_json(dashboard(finance_user))
            if parsed.path == "/api/invoices":
                business_user = self.request_user(BUSINESS_ROLES)
                if not business_user:
                    return
                query = parse_qs(parsed.query)
                status = query.get("status", [None])[0]
                search = query.get("search", [""])[0]
                return self.send_json({"items": list_invoices(status=status, search=search, actor=business_user)})
            if parsed.path.startswith("/api/invoices/") and parsed.path.endswith("/file"):
                business_user = self.request_user(BUSINESS_ROLES)
                if not business_user:
                    return
                job_id = parsed.path.split("/")[3]
                try:
                    target = get_invoice_original(job_id, business_user)
                    content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                    record_audit(business_user, "view_original", "invoice", job_id)
                    return self.send_bytes(target.read_bytes(), content_type)
                except PermissionError as exc:
                    return self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            if parsed.path.startswith("/api/invoices/") and parsed.path.endswith("/open-folder"):
                return self.send_json(
                    {"error": "集中部署模式不允许打开服务器文件夹，请使用“查看原件”或“下载原件”"},
                    HTTPStatus.FORBIDDEN,
                )
            if parsed.path.startswith("/api/invoices/"):
                business_user = self.request_user(BUSINESS_ROLES)
                if not business_user:
                    return
                job_id = parsed.path.rsplit("/", 1)[-1]
                try:
                    return self.send_json(get_invoice(job_id, business_user))
                except PermissionError as exc:
                    return self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
                except ValueError as exc:
                    return self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            if parsed.path == "/api/export":
                finance_user = self.request_user({"finance"})
                if not finance_user:
                    return
                try:
                    archive_path = export_archive(finance_user)
                    record_audit(finance_user, "export_invoices", "invoice")
                    download_name = f"发票归档_{datetime.now():%Y%m%d_%H%M%S}.zip"
                    return self.send_file(archive_path, "application/zip", download_name)
                except PermissionError as exc:
                    return self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
                except OSError as exc:
                    return self.send_json({"error": f"导出失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

        return self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self.payload()
            if parsed.path == "/api/login":
                user = authenticate(data.get("username", ""), data.get("password", ""))
                token, csrf_token = start_session(user["id"])
                record_audit(user, "login", "session")
                return self.send_json(
                    {"user": public_user(user), "csrf_token": csrf_token},
                    extra_headers={"Set-Cookie": cookie_header(token, secure=_secure_cookies())},
                )
            if parsed.path == "/api/setup":
                if not setup_required():
                    return self.send_json({"error": "管理员初始化已完成，请使用登录功能"}, HTTPStatus.CONFLICT)
                user = create_first_admin(
                    data.get("username", ""), data.get("password", ""), data.get("display_name", "")
                )
                token, csrf_token = start_session(user["id"])
                record_audit(user, "setup_admin", "user", user["id"])
                return self.send_json(
                    {"user": public_user(user), "csrf_token": csrf_token},
                    HTTPStatus.CREATED,
                    {"Set-Cookie": cookie_header(token, secure=_secure_cookies())},
                )

            if parsed.path == "/api/logout":
                user = self.request_user(csrf=True)
                if not user:
                    return
                record_audit(user, "logout", "session")
                end_session(self.headers.get("Cookie"))
                return self.send_json({"message": "已退出登录"}, extra_headers={"Set-Cookie": clear_cookie_header()})

            if parsed.path == "/api/account/change-password":
                user = self.request_user(csrf=True)
                if not user:
                    return
                change_password(
                    user["id"],
                    data.get("current_password", ""),
                    data.get("new_password", ""),
                )
                record_audit(user, "change_password", "user", user["id"])
                return self.send_json(
                    {"message": "密码已修改，请重新登录"},
                    extra_headers={"Set-Cookie": clear_cookie_header()},
                )

            if parsed.path == "/api/users":
                user = self.request_user({"admin"}, csrf=True)
                if not user:
                    return
                created = create_user(
                    data.get("username", ""),
                    data.get("password", ""),
                    data.get("role", "employee"),
                    data.get("display_name", ""),
                    data.get("department", ""),
                )
                record_audit(user, "create_user", "user", created.get("id"), {"role": created.get("role")})
                return self.send_json(created, HTTPStatus.CREATED)

            if parsed.path.startswith("/api/users/"):
                user = self.request_user({"admin"}, csrf=True)
                if not user:
                    return
                target_id = parsed.path.split("/")[3]
                if parsed.path.endswith("/reset-password"):
                    reset = reset_password(target_id, data.get("password", ""))
                    record_audit(user, "reset_password", "user", target_id)
                    return self.send_json(reset)
                if parsed.path.endswith("/toggle"):
                    updated = update_user(target_id, active=bool(data.get("active", False)))
                else:
                    updated = update_user(
                        target_id,
                        **{
                            key: data[key]
                            for key in ("display_name", "department", "role", "active")
                            if key in data
                        },
                    )
                record_audit(user, "update_user", "user", target_id)
                return self.send_json(updated)

            if parsed.path == "/api/upload":
                user = self.request_user({"employee"}, csrf=True)
                if not user:
                    return
                file_name = str(data.get("file_name", "发票文件"))
                content = base64.b64decode(data.get("content_base64", ""), validate=True)
                job_id = create_upload(file_name, content, owner_user_id=user["id"])
                item = get_invoice(job_id, user)
                record_audit(user, "upload_invoice", "invoice", job_id, {"file_name": item.get("file_name")})
                if item["status"] == "pending":
                    submit_job(job_id)
                return self.send_json(get_invoice(job_id, user), HTTPStatus.ACCEPTED)

            if parsed.path.startswith("/api/process/"):
                user = self.request_user(BUSINESS_ROLES, csrf=True)
                if not user:
                    return
                job_id = parsed.path.rsplit("/", 1)[-1]
                get_invoice(job_id, user)
                process_invoice(
                    job_id,
                    manual_fields=data.get("manual_fields"),
                    claim_context=data.get("claim_context"),
                    tax_verification=data.get("tax_verification"),
                )
                record_audit(user, "process_invoice", "invoice", job_id)
                return self.send_json(get_invoice(job_id, user))

            if parsed.path.startswith("/api/review/"):
                user = self.request_user({"finance"}, csrf=True)
                if not user:
                    return
                job_id = parsed.path.rsplit("/", 1)[-1]
                item = review_invoice(job_id, data.get("action", ""), data.get("reason", ""), user)
                record_audit(user, "review_invoice", "invoice", job_id, {"action": data.get("action", "")})
                return self.send_json(item)

            return self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except AuthenticationError as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
        except AuthorizationError as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except (ValueError, binascii.Error) as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}")
            return self.send_json({"error": f"处理失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/invoices/"):
            return self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        user = self.request_user(BUSINESS_ROLES, csrf=True)
        if not user:
            return
        job_id = parsed.path.rsplit("/", 1)[-1]
        try:
            result = delete_invoice(job_id, user)
            record_audit(user, "delete_invoice", "invoice", job_id)
            return self.send_json(result)
        except PermissionError as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}")
            return self.send_json({"error": f"删除失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"/", ""} else path.lstrip("/")
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            return self.send_json({"error": "非法路径"}, HTTPStatus.FORBIDDEN)
        if not target.is_file():
            return self.send_json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return self.send_bytes(
            target.read_bytes(),
            f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type,
        )


def run() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"发票报销 Agent 已启动：http://{HOST}:{PORT}")
    print(f"后台处理并发数：{AGENT_WORKERS}")
    print("首次使用请在页面初始化管理员账号")
    print("按 Ctrl+C 停止服务")
    resume_pending_jobs()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.server_close()
        PROCESS_POOL.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    run()
