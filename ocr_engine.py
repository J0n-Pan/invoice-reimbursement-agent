"""可插拔 OCR 引擎。

安装 PaddleOCR 后会自动尝试真实识别；未安装时返回可解释的演示模式结果。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_engine: Any = None
_engine_error: str = ""

_PROJECT_ROOT = Path(__file__).resolve().parent


def _writable_cache_root() -> Path:
    """Choose a cache owned by the current Windows user.

    The project cache can be created by the Codex sandbox or another account,
    so reusing it may cause PermissionError when the user starts the app.
    """
    candidates: list[Path] = []
    configured = os.environ.get("INVOICE_AGENT_CACHE_HOME", "").strip()
    if configured:
        candidates.append(Path(configured))
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        # Keep model files out of the project checkout.  A fresh namespace also
        # avoids caches created by the Codex sandbox or another Windows user.
        candidates.append(Path(local_app_data) / "InvoiceReimbursementAgent-v4")
    # Keep a project-local fallback for portable deployments, but never reuse
    # the old v2/v3 namespaces by default.
    candidates.append(_PROJECT_ROOT / ".invoice-agent-cache-v4")

    def existing_models_are_readable(candidate: Path) -> bool:
        """Reject a cache whose existing model metadata cannot be read.

        Checking only the cache root is not enough: Windows ACLs can differ on
        a nested model directory or on inference.yml itself.  PaddleX then
        fails later with a misleading engine-not-ready error.
        """
        models_root = candidate / "paddlex" / "official_models"
        try:
            try:
                models_root.stat()
            except FileNotFoundError:
                return True
            for model_dir in models_root.iterdir():
                try:
                    if not model_dir.is_dir():
                        continue
                except OSError:
                    return False
                for metadata_name in ("inference.yml", "config.yml"):
                    metadata = model_dir / metadata_name
                    try:
                        metadata.stat()
                    except FileNotFoundError:
                        continue
                    with metadata.open("rb") as handle:
                        handle.read(1)
        except OSError:
            return False
        return True

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            # Checking only the cache root is insufficient: PaddleX writes
            # model metadata several levels below it, and those folders may
            # have different ACLs from the root.
            writable_folders = (
                candidate,
                candidate / "paddlex",
                candidate / "paddlex" / "official_models",
                candidate / "paddle",
            )
            for folder in writable_folders:
                folder.mkdir(parents=True, exist_ok=True)
                probe = folder / ".write-check"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            if not existing_models_are_readable(candidate):
                continue
            return candidate
        except OSError:
            continue
    # A temp fallback is preferable to returning a known-unreadable project
    # cache.  It is only reached when both the user cache and project folder
    # are unavailable, and still gives PaddleX a writable location.
    return Path(tempfile.gettempdir()) / "InvoiceReimbursementAgent-v4"


_CACHE_ROOT = _writable_cache_root()
_PADDLEX_CACHE = _CACHE_ROOT / "paddlex"
_PADDLE_CACHE = _CACHE_ROOT / "paddle"
_PADDLEX_CACHE.mkdir(parents=True, exist_ok=True)
_PADDLE_CACHE.mkdir(parents=True, exist_ok=True)
os.environ["PADDLE_PDX_CACHE_HOME"] = str(_PADDLEX_CACHE)
os.environ["PADDLE_HOME"] = str(_PADDLE_CACHE)
# Keep secondary model clients inside the same writable namespace instead of
# falling back to a protected global cache such as %USERPROFILE%\.modelscope.
os.environ["MODELSCOPE_CACHE"] = str(_CACHE_ROOT / "modelscope")
os.environ["HF_HOME"] = str(_CACHE_ROOT / "huggingface")
# Once the local model cache is ready, do not make startup depend on an
# external model-host connectivity probe.  If a model is genuinely missing,
# PaddleOCR will still report the download error clearly.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def _get_engine() -> Any:
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    try:
        from paddleocr import PaddleOCR

        _engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            engine="paddle",
        )
        return _engine
    except Exception as exc:
        _engine_error = str(exc)
        return None


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "json"):
        raw = value.json
        raw = raw() if callable(raw) else raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
        if isinstance(raw, dict):
            return raw
    if hasattr(value, "to_dict"):
        raw = value.to_dict()
        return raw if isinstance(raw, dict) else {}
    return {}


def recognize(file_path: str | None) -> dict[str, Any]:
    path = Path(file_path or "")
    engine = _get_engine()
    if engine is None:
        return {
            "available": False,
            "engine": "演示模式",
            "texts": [],
            "tables": [],
            "confidence": 0,
            "error": _engine_error or "未安装 PaddleOCR",
        }

    try:
        texts: list[str] = []
        scores: list[float] = []
        tables: list[Any] = []
        raw_results: list[dict[str, Any]] = []
        for result in engine.predict(str(path)):
            data = _to_dict(result)
            raw_results.append(data)
            payload = data.get("res", data) if isinstance(data, dict) else {}
            for text in payload.get("rec_texts", []) or []:
                texts.append(str(text))
            for score in payload.get("rec_scores", []) or []:
                try:
                    scores.append(float(score))
                except (TypeError, ValueError):
                    pass
            tables.extend(payload.get("table_res_list", []) or [])
        confidence = sum(scores) / len(scores) if scores else 0
        return {
            "available": True,
            "engine": "PaddleOCR",
            "texts": texts,
            "tables": tables,
            "confidence": round(confidence, 4),
            "raw": raw_results,
        }
    except Exception as exc:
        return {
            "available": True,
            "engine": "PaddleOCR",
            "texts": [],
            "tables": [],
            "confidence": 0,
            "error": f"OCR 调用失败：{exc}",
        }
