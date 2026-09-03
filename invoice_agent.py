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
UPLOAD_DIR = ROOT / "upload_invoice"
LEGACY_UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "invoice_agent.db"
POLICY_PATH = ROOT / "policy.json"

STATUS_LABELS = {
    "registered": "已登记",
    "noncompliant": "不合规",
    "review": "待复核",
    "processing": "处理中",
    "pending": "待处理",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def load_policy() -> dict[str, Any]:
    """读取可热修改的合规规则；配置损坏时回退到保守默认值。"""
    defaults: dict[str, Any] = {
        "required_fields": ["invoice_no", "invoice_date", "seller", "buyer", "total"],
        "amount_tolerance": 0.02,
        "low_confidence_threshold": 0.85,
        "auto_register_invoice_limit": 3000,
        "manual_review_invoice_limit": 10000,
        "requires_tax_verification": True,
        "requires_business_context": True,
        "requires_payment_proof": True,
        "review_when_missing_field": True,
        "business_entertainment_tax_deduction": {
            "actual_expense_rate": 0.6,
            "annual_sales_rate": 0.005,
        },
    }
    try:
        loaded = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            defaults.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


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
    migrate_legacy_uploads()


def migrate_legacy_uploads() -> None:
    """Move existing uploaded originals to the new unified archive folder."""
    if not LEGACY_UPLOAD_DIR.is_dir() or LEGACY_UPLOAD_DIR.resolve() == UPLOAD_DIR.resolve():
        return

    legacy_root = LEGACY_UPLOAD_DIR.resolve()
    moved: dict[str, Path] = {}
    with connect() as conn:
        rows = conn.execute("SELECT id, file_path FROM invoices WHERE file_path IS NOT NULL AND file_path != ''").fetchall()
        for row in rows:
            source = Path(str(row["file_path"])).resolve()
            if source.parent != legacy_root or not source.is_file():
                continue
            destination = UPLOAD_DIR / source.name
            if destination.exists():
                destination = UPLOAD_DIR / f"{source.stem}_{uuid.uuid4().hex[:8]}{source.suffix}"
            source.replace(destination)
            moved[str(row["id"])] = destination

        if moved:
            conn.executemany(
                "UPDATE invoices SET file_path=?, updated_at=? WHERE id=?",
                [(str(path), now_iso(), job_id) for job_id, path in moved.items()],
            )

    # Also move any orphaned files so every uploaded original is collected in
    # the same folder, even if an earlier database write was interrupted.
    for source in LEGACY_UPLOAD_DIR.iterdir():
        if not source.is_file():
            continue
        destination = UPLOAD_DIR / source.name
        if destination.exists():
            destination = UPLOAD_DIR / f"legacy_{uuid.uuid4().hex[:8]}_{source.name}"
        source.replace(destination)


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


def _safe_file_part(value: Any, fallback: str) -> str:
    """Turn an OCR field into a Windows-safe, readable filename component."""
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return (text[:80] or fallback)


def rename_invoice_original(file_path: str, fields: dict[str, Any], job_id: str) -> tuple[str, str]:
    """Rename a newly processed original using buyer, invoice number and date."""
    raw_path = str(file_path or "").strip()
    if not raw_path:
        return "", ""

    target = Path(raw_path).resolve()
    archive_root = UPLOAD_DIR.resolve()
    if archive_root not in target.parents or not target.is_file():
        return target.name, str(target)

    buyer = _safe_file_part(fields.get("buyer"), "购买方待识别")
    invoice_no = _safe_file_part(fields.get("invoice_no"), job_id)
    date_part = _safe_file_part(fields.get("invoice_date"), "")
    name_parts = [buyer, invoice_no]
    if date_part:
        name_parts.append(date_part)
    stem = "_".join(name_parts)[:150].rstrip(" ._")
    destination = target.with_name(f"{stem}{target.suffix.lower()}")
    counter = 2
    while destination.exists() and destination.resolve() != target:
        destination = target.with_name(f"{stem}_{counter}{target.suffix.lower()}")
        counter += 1

    if destination != target:
        try:
            target.rename(destination)
        except OSError:
            # OCR and compliance results should remain usable even if a file is locked.
            return target.name, str(target)
    return destination.name, str(destination)


def process_invoice(
    job_id: str,
    manual_fields: dict[str, Any] | None = None,
    claim_context: dict[str, Any] | None = None,
    tax_verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise ValueError("找不到发票任务")
        if row["status"] == "processing":
            raise ValueError("该任务正在处理中，请稍后再试")
        claimed = conn.execute(
            "UPDATE invoices SET status = 'processing', updated_at = ? WHERE id = ? AND status = ?",
            (now_iso(), job_id, row["status"]),
        )
        if claimed.rowcount != 1:
            raise ValueError("该任务已被删除或正在处理中")

    try:
        if manual_fields is not None:
            ocr_result = {"available": True, "engine": "演示数据", "texts": [], "tables": []}
            fields = normalize_fields(manual_fields)
        else:
            ocr_result = recognize(row["file_path"])
            fields = normalize_fields(extract_fields(ocr_result))

        # 演示数据明确模拟了税务查验、业务关联、审批和付款凭证均已齐备。
        # 真实上传若未传入这些外部证据，会按 Skill 规则进入人工复核。
        if manual_fields is not None:
            claim_context = claim_context or {
                "business_related": True,
                "business_purpose": "演示业务",
                "approval": True,
                "payment_proof": True,
            }
            tax_verification = tax_verification or {"status": "valid", "source": "演示数据"}

        decision = evaluate_compliance(
            fields,
            job_id,
            ocr_result,
            claim_context=claim_context,
            tax_verification=tax_verification,
        )
        fields["suggested_reimbursable_amount"] = decision.get("suggested_reimbursable_amount")
        fields["reimbursable_amount"] = decision.get("reimbursable_amount")
        fields["tax_deductible_amount"] = decision.get("tax_deductible_amount")
        status = {"PASS": "registered", "REJECT": "noncompliant", "REVIEW": "review"}[decision["status"]]
        stored_file_name, stored_file_path = rename_invoice_original(row["file_path"], fields, job_id)
        stored_file_name = stored_file_name or row["file_name"]
        stored_file_path = stored_file_path or row["file_path"]
        with connect() as conn:
            conn.execute(
                """
                UPDATE invoices SET file_name=?, file_path=?, status=?, invoice_no=?, invoice_date=?, seller=?, buyer=?,
                amount=?, tax=?, total=?, confidence=?, reason=?, ocr_json=?, fields_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    stored_file_name,
                    stored_file_path,
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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是", "有", "通过", "有效"}


def _verification_status(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("status", "")
    return str(value or "").strip().lower()


def calculate_amounts(
    fields: dict[str, Any],
    policy: dict[str, Any],
    claim_context: dict[str, Any] | None,
) -> dict[str, float | None]:
    """计算建议报销额；没有业务额度数据时，不擅自替用户截断金额。"""
    total = parse_money(fields.get("total"))
    if total is None or total <= 0:
        return {
            "suggested_reimbursable_amount": None,
            "reimbursable_amount": None,
            "tax_deductible_amount": None,
        }

    context = claim_context or {}
    candidates = [total]
    for key in ("category_limit", "category_remaining", "budget_remaining"):
        value = parse_money(context.get(key))
        if value is not None:
            candidates.append(max(0.0, value))
    suggested = round(min(candidates), 2)
    auto_limit = parse_money(policy.get("auto_register_invoice_limit")) or 0.0
    result: dict[str, float | None] = {
        "suggested_reimbursable_amount": suggested,
        "reimbursable_amount": suggested if total <= auto_limit else None,
        "tax_deductible_amount": None,
    }

    category = str(context.get("expense_category", "")).strip().lower()
    is_entertainment = any(word in category for word in ("业务招待", "招待费", "business entertainment", "entertainment"))
    annual_sales = parse_money(context.get("annual_sales"))
    entertainment_rule = policy.get("business_entertainment_tax_deduction", {})
    if is_entertainment and annual_sales is not None and isinstance(entertainment_rule, dict):
        actual_rate = float(entertainment_rule.get("actual_expense_rate", 0.6))
        sales_rate = float(entertainment_rule.get("annual_sales_rate", 0.005))
        result["tax_deductible_amount"] = round(min(total * actual_rate, annual_sales * sales_rate), 2)
    return result


def evaluate_compliance(
    fields: dict[str, Any],
    job_id: str,
    ocr_result: dict[str, Any],
    claim_context: dict[str, Any] | None = None,
    tax_verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = load_policy()
    hard_fails: list[str] = []
    reviews: list[str] = []
    required_labels = {
        "invoice_no": "发票号码",
        "invoice_date": "开票日期",
        "seller": "销售方",
        "buyer": "购买方",
        "total": "价税合计",
    }
    required_keys = policy.get("required_fields") or list(required_labels)
    missing = [required_labels.get(key, key) for key in required_keys if not fields.get(key)]
    if missing:
        reviews.append("缺少：" + "、".join(missing))

    amount, tax, total = fields.get("amount"), fields.get("tax"), fields.get("total")
    tolerance = float(policy.get("amount_tolerance", 0.02))
    context = claim_context or {}
    invoice_type = f"{fields.get('invoice_type', '')}{context.get('invoice_type', '')}"
    is_red_invoice = (
        _truthy(fields.get("red_invoice"))
        or _truthy(context.get("red_invoice"))
        or "红字" in invoice_type
        or "负数" in invoice_type
    )
    if is_red_invoice:
        reviews.append(str(policy.get("red_invoice_action", "红字/负数发票需人工复核")))
    if total is not None and total <= 0 and not is_red_invoice:
        hard_fails.append("价税合计必须大于 0")
    if amount is not None and tax is not None and total is not None and abs(amount + tax - total) > tolerance:
        hard_fails.append("金额、税额与价税合计不一致")

    duplicate = find_duplicate_invoice(fields, job_id)
    if duplicate:
        reviews.append(f"疑似重复发票，与任务 {duplicate['id']} 重复，不允许自动登记")

    if not ocr_result.get("available", False):
        ocr_error = str(ocr_result.get("error", "")).strip()
        reviews.append(
            f"OCR 引擎未就绪：{ocr_error}"
            if ocr_error
            else "未安装 OCR 引擎，无法完成真实识别"
        )
    elif float(fields.get("confidence", 0) or 0) < float(policy.get("low_confidence_threshold", 0.85)):
        reviews.append(f"识别置信度低于 {float(policy.get('low_confidence_threshold', 0.85)):.2f}")

    if total is not None:
        auto_limit = float(policy.get("auto_register_invoice_limit", 3000))
        manual_limit = float(policy.get("manual_review_invoice_limit", 10000))
        if total > manual_limit:
            reviews.append(f"价税合计超过 {manual_limit:.2f} 元，需财务负责人审批")
        elif total > auto_limit:
            reviews.append(f"价税合计超过自动登记上限 {auto_limit:.2f} 元，需财务复核")

    if _truthy(policy.get("requires_tax_verification", True)):
        status = _verification_status(tax_verification)
        if not status:
            reviews.append("未完成税务发票查验，需人工复核")
        elif status in {"invalid", "void", "fake", "not_found", "不通过", "作废", "无效", "假票"}:
            hard_fails.append("税务发票查验未通过")
        elif status not in {"valid", "verified", "success", "通过", "有效"}:
            reviews.append("税务发票查验结果不明确，需人工复核")

    if _truthy(policy.get("requires_business_context", True)):
        if not claim_context:
            reviews.append("缺少业务用途、审批和付款凭证，需人工复核")
        else:
            business_related = context.get("business_related")
            if business_related is False or str(business_related).strip().lower() in {"false", "否", "无关"}:
                hard_fails.append("支出与企业经营活动无关")
            elif not context.get("business_purpose") and business_related is not True:
                reviews.append("缺少业务用途说明")
            if not _truthy(context.get("approval") or context.get("approved")):
                reviews.append("缺少审批记录")
            if _truthy(policy.get("requires_payment_proof", True)) and not _truthy(
                context.get("payment_proof") or context.get("paid")
            ):
                reviews.append("缺少付款凭证")

    amounts = calculate_amounts(fields, policy, claim_context)
    if hard_fails:
        return {"status": "REJECT", "reason": "；".join(hard_fails), **amounts, "reimbursable_amount": None}
    if reviews:
        return {"status": "REVIEW", "reason": "；".join(reviews), **amounts, "reimbursable_amount": None}
    return {"status": "PASS", "reason": "符合当前报销规则", **amounts}


def get_invoice(job_id: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ValueError("找不到发票任务")
    item = dict(row)
    item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    original_path = str(item.get("file_path") or "").strip()
    if original_path:
        candidate = Path(original_path)
        item["original_name"] = candidate.name
        item["original_available"] = candidate.is_file()
        item["original_url"] = f"/api/invoices/{job_id}/file" if candidate.is_file() else ""
    else:
        item["original_name"] = ""
        item["original_available"] = False
        item["original_url"] = ""
    item["archive_folder"] = str(UPLOAD_DIR)
    item["fields"] = json.loads(item.pop("fields_json") or "{}")
    item["ocr"] = json.loads(item.pop("ocr_json") or "{}")
    return item


def _duplicate_value(value: Any) -> str:
    """Normalize OCR values so spacing and punctuation do not hide duplicates."""
    return re.sub(r"[^0-9A-Za-z一-龥]", "", str(value or "")).lower()


def _duplicate_date(value: Any) -> str:
    """Normalize both 2026-06-17 and 2026年06月17日 to the same key."""
    return "".join(re.findall(r"\d", str(value or "")))


def _invoice_signature(item: dict[str, Any] | sqlite3.Row) -> tuple[str, str, str, str] | None:
    """Build a conservative business key for visually different copies."""
    if isinstance(item, sqlite3.Row):
        invoice_no = item["invoice_no"]
        invoice_date = item["invoice_date"]
        seller = item["seller"]
        total_value = item["total"]
    else:
        invoice_no = item.get("invoice_no")
        invoice_date = item.get("invoice_date")
        seller = item.get("seller")
        total_value = item.get("total")
    total = parse_money(total_value)
    if not invoice_no or not invoice_date or not seller or total is None:
        return None
    return (
        _duplicate_value(invoice_no),
        _duplicate_date(invoice_date),
        _duplicate_value(seller),
        f"{total:.2f}",
    )


def find_duplicate_invoice(fields: dict[str, Any], job_id: str) -> sqlite3.Row | None:
    """Find an existing active invoice with the same business identity.

    Exact byte-for-byte copies are blocked earlier by file_hash. This second
    check catches renamed, re-encoded, cropped, or rescanned copies by using
    invoice number, issue date, seller, and total amount. Missing key fields
    are treated as uncertain and sent through the normal manual-review rules.
    """
    signature = _invoice_signature(fields)
    if signature is None:
        return None
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, invoice_no, invoice_date, seller, total, status
            FROM invoices
            WHERE id != ? AND status IN ('pending', 'processing', 'registered', 'noncompliant', 'review')
            """,
            (job_id,),
        ).fetchall()
    for row in rows:
        if _invoice_signature(row) == signature:
            return row
    return None


def delete_invoice(job_id: str) -> dict[str, Any]:
    """Delete a non-processing task and its archived original safely."""
    with connect() as conn:
        row = conn.execute("SELECT id, status, file_path FROM invoices WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("找不到发票任务")
        if row["status"] == "processing":
            raise ValueError("该任务正在处理中，请等待处理完成后再删除")

        raw_path = str(row["file_path"] or "").strip()
        if raw_path:
            target = Path(raw_path).resolve()
            archive_root = UPLOAD_DIR.resolve()
            if archive_root not in target.parents:
                raise ValueError("发票原件不在统一归档目录内，无法安全删除")
            if target.exists():
                try:
                    target.unlink()
                except OSError as exc:
                    raise ValueError(f"无法删除发票原件：{exc}") from exc

        deleted = conn.execute("DELETE FROM invoices WHERE id=?", (job_id,)).rowcount
        if deleted != 1:
            raise ValueError("发票任务删除失败")
    return {"id": job_id, "message": "发票任务及归档原件已删除"}


def get_invoice_original(job_id: str) -> Path:
    """返回统一归档目录内的发票原件，避免接口读取任意本地路径。"""
    with connect() as conn:
        row = conn.execute("SELECT file_path FROM invoices WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ValueError("找不到发票任务")
    raw_path = str(row["file_path"] or "").strip()
    if not raw_path:
        raise ValueError("该任务没有上传的发票原件")
    target = Path(raw_path).resolve()
    archive_root = UPLOAD_DIR.resolve()
    if archive_root not in target.parents:
        raise ValueError("发票原件不在统一归档目录内")
    if not target.is_file():
        raise ValueError("发票原件文件不存在")
    return target


def list_invoices(status: str | None = None, search: str | None = None, limit: int = 80) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    clauses: list[str] = []
    params: list[Any] = []
    if status and status in STATUS_LABELS:
        clauses.append("status=?")
        params.append(status)
    search_value = str(search or "").strip()
    if search_value:
        pattern = f"%{search_value}%"
        clauses.append(
            "(id LIKE ? OR file_name LIKE ? OR invoice_no LIKE ? OR invoice_date LIKE ? OR seller LIKE ? OR buyer LIKE ?)"
        )
        params.extend([pattern] * 6)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM invoices{where} ORDER BY updated_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
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
