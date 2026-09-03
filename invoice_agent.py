"""企业内部发票报销 Agent 的核心流程。

默认使用本地 SQLite 保存任务，OCR 通过 ocr_engine.py 可插拔。
没有安装 PaddleOCR 时，系统仍可以在演示模式下运行，并会把真实上传任务安全地转入人工复核。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ocr_engine import recognize


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "invoice_agent.db"

STATUS_LABELS = {
    "registered": "已登记",
    "noncompliant": "不合规",
    "review": "待复核",
    "processing": "处理中",
    "pending": "待处理",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                file_path TEXT,
                file_hash TEXT,
                status TEXT NOT NULL,
                invoice_no TEXT,
                invoice_date TEXT,
                seller TEXT,
                buyer TEXT,
                amount REAL,
                tax REAL,
                total REAL,
                confidence REAL DEFAULT 0,
                reason TEXT DEFAULT '',
                source TEXT DEFAULT 'upload',
                ocr_json TEXT DEFAULT '{}',
                fields_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
            CREATE INDEX IF NOT EXISTS idx_invoices_created_at ON invoices(created_at);
            CREATE INDEX IF NOT EXISTS idx_invoices_invoice_no ON invoices(invoice_no);
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        if count == 0:
            _seed_demo_data(conn)


def _seed_demo_data(conn: sqlite3.Connection) -> None:
    samples = [
        {
            "file_name": "差旅费_北京_001.jpg",
            "status": "registered",
            "invoice_no": "044001930111",
            "invoice_date": "2026-09-03",
            "seller": "北京示例服务有限公司",
            "buyer": "示例科技有限公司",
            "amount": 1000,
            "tax": 60,
            "total": 1060,
            "confidence": 0.99,
            "reason": "符合当前报销规则",
        },
        {
            "file_name": "办公用品_002.jpg",
            "status": "registered",
            "invoice_no": "044001930112",
            "invoice_date": "2026-09-03",
            "seller": "上海示例办公用品有限公司",
            "buyer": "示例科技有限公司",
            "amount": 520,
            "tax": 31.2,
            "total": 551.2,
            "confidence": 0.98,
            "reason": "符合当前报销规则",
        },
        {
            "file_name": "交通费_003.jpg",
            "status": "registered",
            "invoice_no": "044001930113",
            "invoice_date": "2026-09-02",
            "seller": "深圳示例交通服务有限公司",
            "buyer": "示例科技有限公司",
            "amount": 180,
            "tax": 10.8,
            "total": 190.8,
            "confidence": 0.97,
            "reason": "符合当前报销规则",
        },
        {
            "file_name": "金额异常_004.jpg",
            "status": "noncompliant",
            "invoice_no": "044001930114",
            "invoice_date": "2026-09-02",
            "seller": "广州示例供应商有限公司",
            "buyer": "示例科技有限公司",
            "amount": 1000,
            "tax": 60,
            "total": 1200,
            "confidence": 0.96,
            "reason": "金额、税额与价税合计不一致",
        },
        {
            "file_name": "信息缺失_005.jpg",
            "status": "review",
            "invoice_no": "044001930115",
            "invoice_date": "2026-09-01",
            "seller": "杭州示例供应商有限公司",
            "buyer": "",
            "amount": 300,
            "tax": 18,
            "total": 318,
            "confidence": 0.71,
            "reason": "缺少购买方信息，需人工复核",
        },
    ]
    for sample in samples:
        _insert_invoice(conn, sample, source="demo")


def _insert_invoice(conn: sqlite3.Connection, data: dict[str, Any], source: str = "upload") -> str:
    job_id = data.get("id") or f"INV-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
    stamp = data.get("created_at") or now_iso()
    fields = {
        key: data.get(key)
        for key in ("invoice_no", "invoice_date", "seller", "buyer", "amount", "tax", "total")
    }
    conn.execute(
        """
        INSERT INTO invoices (
            id, file_name, file_path, file_hash, status, invoice_no, invoice_date,
            seller, buyer, amount, tax, total, confidence, reason, source,
            ocr_json, fields_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            data.get("file_name", "未命名文件"),
            data.get("file_path", ""),
            data.get("file_hash", ""),
            data.get("status", "pending"),
            fields["invoice_no"],
            fields["invoice_date"],
            fields["seller"],
            fields["buyer"],
            fields["amount"],
            fields["tax"],
            fields["total"],
            float(data.get("confidence", 0) or 0),
            data.get("reason", ""),
            source,
            json.dumps(data.get("ocr", {}), ensure_ascii=False, default=str),
            json.dumps(fields, ensure_ascii=False, default=str),
            stamp,
            stamp,
        ),
    )
    return job_id


def create_upload(file_name: str, content: bytes) -> str:
    safe_name = re.sub(r"[^0-9A-Za-z一-龥._-]", "_", file_name or "发票文件")[:120]
    digest = hashlib.sha256(content).hexdigest()
    with connect() as conn:
        duplicated = conn.execute(
            "SELECT id FROM invoices WHERE file_hash = ? AND file_hash != '' LIMIT 1", (digest,)
        ).fetchone()
        if duplicated:
            return str(duplicated[0])
        target = UPLOAD_DIR / f"{uuid.uuid4().hex[:10]}_{safe_name}"
        target.write_bytes(content)
        return _insert_invoice(
            conn,
            {"file_name": safe_name, "file_path": str(target), "file_hash": digest, "status": "pending"},
        )


def process_invoice(job_id: str, manual_fields: dict[str, Any] | None = None) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise ValueError("找不到发票任务")
        conn.execute("UPDATE invoices SET status = 'processing', updated_at = ? WHERE id = ?", (now_iso(), job_id))

    try:
        if manual_fields is not None:
            ocr_result = {"available": True, "engine": "演示数据", "texts": [], "tables": []}
            fields = normalize_fields(manual_fields)
        else:
            ocr_result = recognize(row["file_path"])
            fields = normalize_fields(extract_fields(ocr_result))

        decision = evaluate_compliance(fields, job_id, ocr_result)
        status = {"PASS": "registered", "REJECT": "noncompliant", "REVIEW": "review"}[decision["status"]]
        with connect() as conn:
            conn.execute(
                """
                UPDATE invoices SET status=?, invoice_no=?, invoice_date=?, seller=?, buyer=?,
                amount=?, tax=?, total=?, confidence=?, reason=?, ocr_json=?, fields_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    fields.get("invoice_no", ""),
                    fields.get("invoice_date", ""),
                    fields.get("seller", ""),
                    fields.get("buyer", ""),
                    fields.get("amount"),
                    fields.get("tax"),
                    fields.get("total"),
                    fields.get("confidence", 0),
                    decision["reason"],
                    json.dumps(ocr_result, ensure_ascii=False, default=str),
                    json.dumps(fields, ensure_ascii=False, default=str),
                    now_iso(),
                    job_id,
                ),
            )
        return get_invoice(job_id)
    except Exception as exc:
        with connect() as conn:
            conn.execute(
                "UPDATE invoices SET status='review', reason=?, updated_at=? WHERE id=?",
                (f"处理异常：{exc}", now_iso(), job_id),
            )
        return get_invoice(job_id)


def normalize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    result = dict(fields or {})
    for key in ("invoice_no", "invoice_date", "seller", "buyer"):
        result[key] = str(result.get(key) or "").strip()
    for key in ("amount", "tax", "total"):
        result[key] = parse_money(result.get(key))
    result["confidence"] = float(result.get("confidence", 0) or 0)
    return result


def parse_money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.-]", "", str(value))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def extract_fields(ocr_result: dict[str, Any]) -> dict[str, Any]:
    lines = [str(item).strip() for item in ocr_result.get("texts", []) if str(item).strip()]
    text = " ".join(lines)

    def find(patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(" ：:;")
        return ""

    def find_name(heading: str) -> str:
        """按发票版式，在购买方/销售方标题附近提取名称。"""
        for index, line in enumerate(lines):
            if heading not in line:
                continue
            for candidate in lines[index : index + 4]:
                match = re.search(r"名称[：: ]*(.+)", candidate)
                if match:
                    return match.group(1).strip(" ：:;")
        return ""

    def money_values(value: str) -> list[str]:
        return re.findall(r"[-−]?[¥￥]?\s*[0-9][0-9,]*\.[0-9]{1,2}", value)

    # 电子发票常把“金额/税额/价税合计”的标题和值分开放置，不能只依赖
    # “金额：xxx”这种单行格式。这里同时支持单行标签和合计区域的列顺序。
    summary_values: list[str] = []
    for index, line in enumerate(lines):
        if line == "合计" or line.startswith("合计"):
            for candidate in lines[index + 1 : index + 5]:
                summary_values.extend(money_values(candidate))
            break

    amount = find([r"金额[：: ]*([-−]?[¥￥]?\s*[0-9,]+(?:\.[0-9]{1,2})?)"])
    tax = find([r"税额[：: ]*([-−]?[¥￥]?\s*[0-9,]+(?:\.[0-9]{1,2})?)"])
    if not amount and len(summary_values) >= 1:
        amount = summary_values[0]
    if not tax and len(summary_values) >= 2:
        tax = summary_values[1]

    total = find([
        r"小写[^0-9¥￥]{0,10}[¥￥]?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"价税合计[^0-9¥￥]{0,10}[¥￥]?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
    ])
    if not total:
        for index, line in enumerate(lines):
            if "小写" in line or "价税合计" in line:
                values = money_values(line)
                if not values and index + 1 < len(lines):
                    values = money_values(lines[index + 1])
                if values:
                    total = values[-1]
                    break
    if not total and len(summary_values) >= 3:
        total = summary_values[2]

    return {
        "invoice_no": find([r"发票号码[：: ]*([0-9A-Za-z-]+)", r"号码[：: ]*([0-9A-Za-z-]{8,})"]),
        "invoice_date": find([r"开票日期[：: ]*([0-9]{4}[-/.年][0-9]{1,2}[-/.月][0-9]{1,2}日?)"]),
        "seller": find_name("销售方") or find([r"销售方[：: ]*(.{2,30})", r"名称[：: ]*(.{2,30})"]),
        "buyer": find_name("购买方") or find([r"购买方[：: ]*(.{2,30})"]),
        "amount": amount,
        "tax": tax,
        "total": total,
        "confidence": float(ocr_result.get("confidence", 0) or 0),
    }


def evaluate_compliance(fields: dict[str, Any], job_id: str, ocr_result: dict[str, Any]) -> dict[str, str]:
    hard_fails: list[str] = []
    reviews: list[str] = []
    required = {"invoice_no": "发票号码", "invoice_date": "开票日期", "seller": "销售方", "buyer": "购买方", "total": "价税合计"}
    missing = [label for key, label in required.items() if not fields.get(key)]
    if missing:
        reviews.append("缺少：" + "、".join(missing))

    amount, tax, total = fields.get("amount"), fields.get("tax"), fields.get("total")
    if total is not None and total <= 0:
        hard_fails.append("价税合计必须大于 0")
    if amount is not None and tax is not None and total is not None and abs(amount + tax - total) > 0.02:
        hard_fails.append("金额、税额与价税合计不一致")

    invoice_no = fields.get("invoice_no")
    if invoice_no:
        with connect() as conn:
            duplicate = conn.execute(
                "SELECT id FROM invoices WHERE invoice_no=? AND id!=? AND status!='noncompliant' LIMIT 1",
                (invoice_no, job_id),
            ).fetchone()
        if duplicate:
            hard_fails.append("发票号码重复")

    if not ocr_result.get("available", False):
        ocr_error = str(ocr_result.get("error", "")).strip()
        reviews.append(
            f"OCR 引擎未就绪：{ocr_error}"
            if ocr_error
            else "未安装 OCR 引擎，无法完成真实识别"
        )
    elif float(fields.get("confidence", 0) or 0) < 0.80:
        reviews.append("识别置信度低于 0.80")

    if hard_fails:
        return {"status": "REJECT", "reason": "；".join(hard_fails)}
    if reviews:
        return {"status": "REVIEW", "reason": "；".join(reviews)}
    return {"status": "PASS", "reason": "符合当前报销规则"}


def get_invoice(job_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ValueError("找不到发票任务")
    item = dict(row)
    item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    item["fields"] = json.loads(item.pop("fields_json") or "{}")
    item["ocr"] = json.loads(item.pop("ocr_json") or "{}")
    return item


def list_invoices(status: str | None = None, limit: int = 80) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with connect() as conn:
        if status and status in STATUS_LABELS:
            rows = conn.execute("SELECT * FROM invoices WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM invoices ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [_public_row(row) for row in rows]


def _public_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("ocr_json", None)
    item.pop("fields_json", None)
    item.pop("file_path", None)
    item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    return item


def dashboard() -> dict[str, Any]:
    with connect() as conn:
        counts = {key: conn.execute("SELECT COUNT(*) FROM invoices WHERE status=?", (key,)).fetchone()[0] for key in STATUS_LABELS}
        processed_today = conn.execute(
            "SELECT COUNT(*) FROM invoices WHERE date(created_at)=date('now','localtime') AND status IN ('registered','noncompliant','review')"
        ).fetchone()[0]
    return {
        "target": 10000,
        "processed_today": processed_today,
        "progress": round(min(100, processed_today / 10000 * 100), 1),
        "registered": counts["registered"],
        "noncompliant": counts["noncompliant"],
        "review": counts["review"],
        "processing": counts["processing"],
        "pending": counts["pending"],
        "ocr_mode": "真实 OCR" if _ocr_available() else "演示模式",
    }


def _ocr_available() -> bool:
    try:
        import paddleocr  # noqa: F401
        return True
    except Exception:
        return False


def create_demo_jobs() -> list[dict[str, Any]]:
    cases = [
        {
            "file_name": "演示_合规发票.jpg",
            "invoice_no": f"DEMO{uuid.uuid4().hex[:10].upper()}",
            "invoice_date": "2026-09-03",
            "seller": "示例服务有限公司",
            "buyer": "示例科技有限公司",
            "amount": 800,
            "tax": 48,
            "total": 848,
            "confidence": 0.98,
        },
        {
            "file_name": "演示_金额异常.jpg",
            "invoice_no": f"DEMO{uuid.uuid4().hex[:10].upper()}",
            "invoice_date": "2026-09-03",
            "seller": "示例供应商有限公司",
            "buyer": "示例科技有限公司",
            "amount": 800,
            "tax": 48,
            "total": 900,
            "confidence": 0.98,
        },
        {
            "file_name": "演示_信息缺失.jpg",
            "invoice_no": f"DEMO{uuid.uuid4().hex[:10].upper()}",
            "invoice_date": "2026-09-03",
            "seller": "示例供应商有限公司",
            "buyer": "",
            "amount": 300,
            "tax": 18,
            "total": 318,
            "confidence": 0.74,
        },
    ]
    created: list[dict[str, Any]] = []
    for case in cases:
        with connect() as conn:
            job_id = _insert_invoice(conn, {**case, "status": "pending"}, source="demo")
        created.append(process_invoice(job_id, manual_fields=case))
    return created


def review_invoice(job_id: str, action: str, reason: str = "") -> dict[str, Any]:
    if action not in {"pass", "reject"}:
        raise ValueError("不支持的复核动作")
    status = "registered" if action == "pass" else "noncompliant"
    final_reason = reason or ("人工复核通过" if action == "pass" else "人工复核确认不合规")
    with connect() as conn:
        conn.execute("UPDATE invoices SET status=?, reason=?, updated_at=? WHERE id=?", (status, final_reason, now_iso(), job_id))
    return get_invoice(job_id)


def export_csv() -> str:
    rows = list_invoices(limit=200)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["id", "file_name", "status_label", "invoice_no", "invoice_date", "seller", "buyer", "amount", "tax", "total", "confidence", "reason", "updated_at"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


init_db()
