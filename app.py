"""Windows 本地 Web 界面与 API。启动：python app.py"""

from __future__ import annotations

import base64
import binascii
import os
import json
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from invoice_agent import (
    create_demo_jobs,
    create_upload,
    dashboard,
    export_csv,
    get_invoice,
    list_invoices,
    process_invoice,
    review_invoice,
)


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
HOST = "127.0.0.1"
PORT = 8765
AGENT_WORKERS = max(1, min(int(os.environ.get("AGENT_WORKERS", "4")), 16))
PROCESS_POOL = ThreadPoolExecutor(max_workers=AGENT_WORKERS, thread_name_prefix="invoice-agent")


def submit_job(job_id: str) -> None:
    PROCESS_POOL.submit(process_invoice, job_id)


def resume_pending_jobs() -> None:
    for item in list_invoices(status="pending", limit=200):
        submit_job(item["id"])


class Handler(BaseHTTPRequestHandler):
    server_version = "InvoiceAgent/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/dashboard":
            return self.send_json(dashboard())
        if parsed.path == "/api/invoices":
            status = parse_qs(parsed.query).get("status", [None])[0]
            return self.send_json({"items": list_invoices(status=status)})
        if parsed.path.startswith("/api/invoices/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            try:
                return self.send_json(get_invoice(job_id))
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        if parsed.path == "/api/export":
            return self.send_bytes(export_csv().encode("utf-8-sig"), "text/csv; charset=utf-8")
        return self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if length > 25 * 1024 * 1024:
            return self.send_json({"error": "单个文件不能超过 25MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except json.JSONDecodeError:
            return self.send_json({"error": "请求数据格式错误"}, HTTPStatus.BAD_REQUEST)

        try:
            if parsed.path == "/api/demo":
                items = create_demo_jobs()
                return self.send_json({"items": items})
            if parsed.path == "/api/upload":
                file_name = str(payload.get("file_name", "发票文件"))
                content = base64.b64decode(payload.get("content_base64", ""), validate=True)
                job_id = create_upload(file_name, content)
                item = get_invoice(job_id)
                if item["status"] == "pending":
                    submit_job(job_id)
                return self.send_json(get_invoice(job_id), HTTPStatus.ACCEPTED)
            if parsed.path.startswith("/api/process/"):
                job_id = parsed.path.rsplit("/", 1)[-1]
                return self.send_json(process_invoice(job_id))
            if parsed.path.startswith("/api/review/"):
                job_id = parsed.path.rsplit("/", 1)[-1]
                return self.send_json(review_invoice(job_id, payload.get("action", ""), payload.get("reason", "")))
            return self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except (ValueError, binascii.Error) as exc:
            return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return self.send_json({"error": f"处理失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"/", ""} else path.lstrip("/")
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            return self.send_json({"error": "非法路径"}, HTTPStatus.FORBIDDEN)
        if not target.is_file():
            return self.send_json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return self.send_bytes(target.read_bytes(), f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)

def run() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"发票报销 Agent 已启动：http://{HOST}:{PORT}")
    print(f"后台处理并发数：{AGENT_WORKERS}")
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
