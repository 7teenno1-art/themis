#!/usr/bin/env python3
"""Минимальный public-only fetch через Линкей с проверкой raw receipt."""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit


ALLOWED_HOSTS = frozenset({
    "pypi.org", "github.com", "raw.githubusercontent.com", "consultant.ru",
    "www.consultant.ru", "vsrf.ru", "www.vsrf.ru", "ksrf.ru", "www.ksrf.ru",
    "publication.pravo.gov.ru", "pravo.gov.ru", "www.pravo.gov.ru", "sudact.ru",
})
MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
SAFE_MANIFEST_PATH = re.compile(r"^runs/[A-Za-z0-9._-]{1,160}/manifest\.json$")
SAFE_OUTCOMES = frozenset({"success", "retryable", "terminal", "broken"})
SAFE_ATTEMPT_CODES = frozenset({
    "ok", "http_5xx", "rate_limited", "timeout", "network", "empty", "wrong_mime",
    "parse_failed", "policy_denied", "robots_denied", "access_denied", "auth_required",
    "captcha", "paywall", "paid_required", "not_found", "gone", "hard_limit",
    "adapter_crash", "adapter_protocol_error", "hash_mismatch",
})
SAFE_LYNCEUZ_CODES = SAFE_ATTEMPT_CODES | frozenset({
    "health", "route_explained", "invalid_input", "partial", "exhausted",
    "no_eligible_engine", "unavailable_no_free_search_provider", "internal_error",
    "unknown_attempt_code", "output_failure", "interrupted", "blocked",
})
SAFE_STDERR_CODES = frozenset({
    "EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT",
    "EHOSTUNREACH", "ENETUNREACH", "EPIPE", "ERR_RESPONSE_HEADER_TIMEOUT",
    "ERR_TLS_CERT_ALTNAME_INVALID", "CERT_HAS_EXPIRED",
})
SAFE_STDERR_TYPES = frozenset({
    "Error", "TypeError", "RangeError", "ReferenceError", "SyntaxError", "AggregateError",
})
NONZERO_RETRY_DELAYS = (0.25, 1.0)


class MaintenanceFetchError(RuntimeError):
    """Типизированный отказ fetch; только allowlisted технические детали."""

    def __init__(self, code: str, diagnostic: dict | None = None):
        self.code = code
        self.diagnostic = diagnostic or {}
        detail = (": " + json.dumps(self.diagnostic, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"))) if self.diagnostic else ""
        super().__init__(code + detail)


def _validate_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > 4096 or url != url.strip():
        raise MaintenanceFetchError("invalid_url")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise MaintenanceFetchError("invalid_url") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None or port not in (None, 443)):
        raise MaintenanceFetchError("invalid_url")
    if parsed.hostname.lower() == "sudact.ru":
        # Публичный корпус /law, не отдельная политика поиска /doc_ajax/.
        path = posixpath.normpath(unquote(parsed.path))
        if "%" in path or not (path.startswith("/law/") or path in {"/robots.txt", "/sitemap.xml"}):
            raise MaintenanceFetchError("unapproved_sudact_path")
    return url


def _lynceuz_entry() -> Path:
    home = Path(os.environ.get("LYNCEUZ_HOME", str(Path.home() / "Проекты" / "lynceuz")))
    entry = home / "src" / "lynceuz.mjs"
    if not entry.is_file():
        raise MaintenanceFetchError("lynceuz_missing")
    return entry


def _safe_path(data_root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise MaintenanceFetchError("invalid_artifact")
    root = data_root.resolve()
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError):
        raise MaintenanceFetchError("artifact_outside_state") from None
    if not path.is_file():
        raise MaintenanceFetchError("invalid_artifact")
    return path


def _artifact(data_root: Path, relative: object) -> bytes:
    path = _safe_path(data_root, relative)
    try:
        size = path.stat().st_size
        if size < 0 or size > MAX_BODY_BYTES:
            raise MaintenanceFetchError("artifact_too_large")
        body = path.read_bytes()
    except OSError as exc:
        raise MaintenanceFetchError("artifact_unreadable") from exc
    if len(body) != size:
        raise MaintenanceFetchError("artifact_changed")
    return body


def _read_manifest(data_root: Path, relative: object) -> dict:
    path = _safe_path(data_root, relative)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise MaintenanceFetchError("invalid_manifest")
        value = json.loads(raw)
    except MaintenanceFetchError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise MaintenanceFetchError("invalid_manifest") from exc
    if not isinstance(value, dict):
        raise MaintenanceFetchError("invalid_manifest")
    return value


def _safe_number(value: object, maximum: int) -> int | None:
    if type(value) is int and 0 <= value <= maximum:
        return value
    return None


def _safe_attempts(value: object) -> list[dict]:
    """Только безопасные технические поля; HTTP evidence и raw body не публикуются."""
    if not isinstance(value, list):
        return []
    result = []
    for source in value[:16]:
        if not isinstance(source, dict):
            continue
        attempt = {}
        outcome = source.get("outcome")
        if isinstance(outcome, str) and outcome in SAFE_OUTCOMES:
            attempt["outcome"] = outcome
        code = source.get("code")
        if isinstance(code, str) and code in SAFE_ATTEMPT_CODES:
            attempt["code"] = code
        if not attempt:
            continue
        number = _safe_number(source.get("attempt"), 1_000)
        if number is not None and number > 0:
            attempt["attempt"] = number
        for name in ("duration_ms", "delay_ms"):
            number = _safe_number(source.get(name), 120_000)
            if number is not None:
                attempt[name] = number
        capped = source.get("retry_after_capped")
        if type(capped) is bool:
            attempt["retry_after_capped"] = capped
        result.append(attempt)
    return result


def _safe_process_diagnostic(returncode: object, stderr: object) -> dict:
    diagnostic = {}
    if type(returncode) is int and -255 <= returncode <= 255:
        diagnostic["returncode"] = returncode
    if not isinstance(stderr, str):
        return diagnostic
    tokens = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]{1,63}\b", stderr[:65_536]))
    codes = sorted(tokens & SAFE_STDERR_CODES)
    types = sorted(tokens & SAFE_STDERR_TYPES)
    if codes:
        diagnostic["stderr_codes"] = codes
    if types:
        diagnostic["stderr_types"] = types
    return diagnostic


def _failure_diagnostic(stdout: object, data_root: Path, returncode: object = None,
                        stderr: object = None) -> dict:
    """Безопасная диагностика nonzero Lynceuz без stderr, message и body."""
    diagnostic = _safe_process_diagnostic(returncode, stderr)
    try:
        envelope = json.loads(stdout)
    except (TypeError, ValueError):
        return diagnostic
    if not isinstance(envelope, dict):
        return diagnostic
    code = envelope.get("code")
    if isinstance(code, str) and code in SAFE_LYNCEUZ_CODES:
        diagnostic["lynceuz_code"] = code
    relative = envelope.get("manifest_path")
    if not isinstance(relative, str) or not SAFE_MANIFEST_PATH.fullmatch(relative):
        return diagnostic
    try:
        manifest = _read_manifest(data_root, relative)
    except MaintenanceFetchError:
        return diagnostic
    diagnostic["manifest_path"] = relative
    attempts = _safe_attempts(manifest.get("attempts") if isinstance(manifest, dict) else None)
    if attempts:
        diagnostic["attempts"] = attempts
    return diagnostic


def _success_result(envelope: object, data_root: Path, url: str) -> dict:
    if not isinstance(envelope, dict) or envelope.get("status") != "ok" or envelope.get("code") != "ok":
        raise MaintenanceFetchError("lynceuz_not_success")
    body = _artifact(data_root, envelope.get("artifact_path"))
    revision = "sha256:" + hashlib.sha256(body).hexdigest()
    if envelope.get("source_hash") != revision:
        raise MaintenanceFetchError("source_hash_mismatch")
    try:
        manifest = _read_manifest(data_root, envelope.get("manifest_path"))
        if (manifest.get("requested_url") != url
                or manifest.get("source_hash") != revision
                or manifest.get("artifact_hash") != revision
                or manifest.get("artifact_path") != envelope.get("artifact_path")):
            raise MaintenanceFetchError("receipt_binding_mismatch")
        source_url = _validate_url(manifest.get("effective_url"))
    except MaintenanceFetchError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise MaintenanceFetchError("invalid_manifest") from exc
    return {
        "body": body,
        "source_revision": revision,
        "source_url": source_url,
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _all_attempts_are(manifest: dict, code: str, outcome: str) -> bool:
    attempts = manifest.get("attempts")
    return (isinstance(attempts, list) and bool(attempts)
            and all(type(item) is dict and item.get("code") == code
                    and item.get("outcome") == outcome for item in attempts))


def _retryable_nonzero(stdout: object, diagnostic: dict, data_root: Path, url: str) -> bool:
    code = diagnostic.get("lynceuz_code")
    if code not in {"exhausted", "ok"}:
        return False
    try:
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict):
            return False
        manifest = _read_manifest(data_root, envelope.get("manifest_path"))
    except (MaintenanceFetchError, TypeError, ValueError):
        return False
    if code == "exhausted":
        return _all_attempts_are(manifest, "network", "retryable")
    try:
        _success_result(envelope, data_root, url)
    except MaintenanceFetchError:
        return False
    return (manifest.get("status") == "ok" and manifest.get("verification") == "source_captured"
            and _all_attempts_are(manifest, "ok", "success"))


def fetch_public(url: str, state_dir: Path) -> dict:
    """Fetch один allowlisted public URL через Линкей. Сеть разрешает caller."""
    url = _validate_url(url)
    if not isinstance(state_dir, Path):
        state_dir = Path(state_dir)
    data_root = state_dir / ".lynceuz"
    node = shutil.which("node")
    if not node:
        raise MaintenanceFetchError("node_missing")
    argv = [node, str(_lynceuz_entry()), "url", url, "--json", "--format", "raw",
            "--data-root", str(data_root), "--cache", "off"]
    result = None
    for retry in range(len(NONZERO_RETRY_DELAYS) + 1):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise MaintenanceFetchError("lynceuz_timeout") from exc
        except OSError as exc:
            raise MaintenanceFetchError("lynceuz_failed") from exc
        if result.returncode == 0:
            break
        diagnostic = _failure_diagnostic(
            result.stdout, data_root, result.returncode, result.stderr)
        if retry >= len(NONZERO_RETRY_DELAYS) or not _retryable_nonzero(
                result.stdout, diagnostic, data_root, url):
            raise MaintenanceFetchError("lynceuz_failed", diagnostic)
        time.sleep(NONZERO_RETRY_DELAYS[retry])
    try:
        envelope = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise MaintenanceFetchError("invalid_result") from exc
    return _success_result(envelope, data_root, url)
