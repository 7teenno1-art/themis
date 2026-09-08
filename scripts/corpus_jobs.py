#!/usr/bin/env python3
"""Один возобновляемый фоновый job нормативного корпуса за вызов."""
from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import maintenance_fetch
import maintenance_plenums
import update_legal_corpus as corpus


CACHE_TTL = timedelta(days=7)
MAX_BODY_BYTES = 10 * 1024 * 1024
BLOCKED_FETCH_CODES = frozenset({
    "access_denied", "auth_required", "captcha", "paid_required", "paywall",
    "policy_denied", "robots_denied", "unapproved_sudact_path",
})


class Deadline(Exception):
    pass


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _json(path: Path, value: dict) -> None:
    _atomic(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())


def _under(base: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise ValueError("path outside managed tree") from exc
    current = base
    if current.is_symlink():
        raise ValueError("managed root is symlink")
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("managed path is symlink")
    return path


def _progress(done: int, total: int) -> dict:
    return {"done": done, "total": total, "pending_count": max(0, total - done)}


def _job_dir(root: Path, state_dir: Path, resource_id: str) -> Path:
    wanted = root / ".agent" / "maintenance" / "background" / "jobs"
    token = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()
    expected = wanted / token
    if state_dir != expected:
        raise ValueError("state_dir is not resource job directory")
    _under(root, state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _read_checkpoint(job: Path, resource: dict) -> dict:
    try:
        value = json.loads((job / "checkpoint.json").read_text(encoding="utf-8"))
        if value.get("resource") == resource and isinstance(value.get("articles"), dict):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return {"schema_version": 1, "resource": resource, "articles": {}, "receipts": {}}


def _write_checkpoint(job: Path, checkpoint: dict) -> None:
    _json(job / "checkpoint.json", checkpoint)


def _cache_paths(job: Path, url: str) -> tuple[Path, Path]:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return job / "receipts" / f"{key}.body", job / "receipts" / f"{key}.json"


def _cached(job: Path, url: str, now: datetime) -> dict | None:
    body_path, meta_path = _cache_paths(job, url)
    try:
        if (not body_path.is_file() or not meta_path.is_file() or body_path.is_symlink()
                or meta_path.is_symlink() or body_path.stat().st_size > MAX_BODY_BYTES
                or meta_path.stat().st_size > 4096):
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return None
        parsed = datetime.fromisoformat(str(meta["checked_at"]).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        checked = parsed.astimezone(UTC)
        body = body_path.read_bytes()
        if (not meta.get("verified") or checked > now or now - checked > CACHE_TTL or meta.get("url") != url
                or meta.get("source_url") != url or meta.get("source_revision") != _sha(body)):
            return None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return {"body": body, "source_url": url, "source_revision": _sha(body),
            "checked_at": checked.isoformat().replace("+00:00", "Z")}


def _child_fetch(conn, url: str, fetch_dir: str) -> None:
    try:
        conn.send((True, maintenance_fetch.fetch_public(url, Path(fetch_dir))))
    except maintenance_fetch.MaintenanceFetchError as exc:
        # Only typed, public transport evidence crosses the process boundary.
        conn.send((False, "fetch", exc.code, exc.diagnostic))
    except Exception as exc:  # child cannot safely re-raise through process boundary
        conn.send((False, "internal", type(exc).__name__))
    finally:
        conn.close()


def _fetch_live(url: str, fetch_dir: Path, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Deadline
    parent, child = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(target=_child_fetch, args=(child, url, str(fetch_dir)))
    process.start()
    child.close()
    try:
        if not parent.poll(remaining):
            raise Deadline
        message = parent.recv()
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        parent.close()
    if message[0]:
        return message[1]
    if message[1] == "fetch":
        raise maintenance_fetch.MaintenanceFetchError(message[2], message[3])
    raise RuntimeError(f"fetch child failed: {message[2]}")


def _receipt(job: Path, url: str, now: datetime, deadline: float) -> dict:
    cached = _cached(job, url, now)
    if cached is not None:
        return cached
    receipt = _fetch_live(url, job / "fetch", deadline)
    body = receipt.get("body") if isinstance(receipt, dict) else None
    if (not isinstance(body, bytes) or receipt.get("source_url") != url
            or receipt.get("source_revision") != _sha(body) or len(body) > MAX_BODY_BYTES):
        raise ValueError("fetch receipt is not bound to source")
    try:
        parsed = datetime.fromisoformat(str(receipt["checked_at"]).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("fetch receipt timestamp is naive")
        checked = parsed.astimezone(UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("fetch receipt timestamp invalid") from exc
    if checked > datetime.now(UTC):
        raise ValueError("fetch receipt timestamp is in the future")
    body_path, meta_path = _cache_paths(job, url)
    _atomic(body_path, body)
    _json(meta_path, {"url": url, "source_url": url, "source_revision": _sha(body),
                      "checked_at": checked.isoformat().replace("+00:00", "Z"), "verified": False})
    return {"body": body, "source_url": url, "source_revision": _sha(body),
            "checked_at": checked.isoformat().replace("+00:00", "Z")}


def _verify_receipt(job: Path, receipt: dict) -> None:
    body_path, meta_path = _cache_paths(job, receipt["source_url"])
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (not isinstance(meta, dict) or not body_path.is_file()
                or meta.get("source_revision") != _sha(body_path.read_bytes())):
            raise ValueError("receipt cache changed")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt cache invalid") from exc
    meta["verified"] = True
    _json(meta_path, meta)


def _snapshot(root: Path, output: Path, job: Path, state: dict) -> bytes | None:
    saved = state.get("original")
    original_path = job / "original.body"
    if isinstance(saved, dict):
        if not saved.get("exists"):
            if output.exists():
                body = output.read_bytes()
                if state.get("activated_sha256") == _sha(body):
                    state["original"] = {"exists": True, "sha256": _sha(body)}
                    _atomic(original_path, body)
                    _write_checkpoint(job, state)
                    return body
                raise RuntimeError("CAS: local corpus appeared")
            return None
        body = original_path.read_bytes()
        if saved.get("sha256") != _sha(body):
            raise ValueError("saved original changed")
        if output.exists() and output.read_bytes() != body:
            current = output.read_bytes()
            if state.get("activated_sha256") == _sha(current):
                state["original"] = {"exists": True, "sha256": _sha(current)}
                _atomic(original_path, current)
                _write_checkpoint(job, state)
                return current
            raise RuntimeError("CAS: local corpus changed")
        return body
    _under(root, output)
    if output.exists() and output.is_symlink():
        raise ValueError("output is symlink")
    body = output.read_bytes() if output.exists() else None
    state["original"] = {"exists": body is not None, "sha256": _sha(body) if body is not None else None}
    if body is not None:
        _atomic(original_path, body)
    _write_checkpoint(job, state)
    return body


def _activate(root: Path, output: Path, candidate: bytes, state: Path, original: bytes | None) -> str:
    _under(root, output)
    if output.exists() and output.is_symlink():
        raise ValueError("output is symlink")
    if original is None and output.exists():
        raise RuntimeError("CAS: local corpus appeared")
    if original is not None and (not output.exists() or output.read_bytes() != original):
        raise RuntimeError("CAS: local corpus changed")
    if original == candidate:
        return "current"
    if original is not None:
        backup = state / "backups" / output.stem / f"{_sha(original)[7:]}.md"
        _under(root, backup)
        if not backup.exists():
            _atomic(backup, original)
    if original is not None and output.read_bytes() != original:
        raise RuntimeError("CAS: local corpus changed")
    _atomic(output, candidate)
    return "updated"


def _resource(resource: object) -> dict:
    if not isinstance(resource, dict):
        raise ValueError("resource must be object")
    required = ("id", "kind", "parser", "url", "output")
    if any(not isinstance(resource.get(key), str) or not resource[key] for key in required):
        raise ValueError("resource fields missing")
    expected_kind = {"consultant-code": {"code"}, "consultant-act": {"federal-law", "bylaw"},
                     "sudact-plenum": {"plenum"}, "sudact-catalog": {"catalog"},
                     "vsrf-review": {"review"}}
    if resource["parser"] not in expected_kind:
        raise ValueError("unsupported parser")
    if resource["kind"] not in expected_kind[resource["parser"]]:
        raise ValueError("resource kind does not match parser")
    return resource


def _output(root: Path, resource: dict) -> Path:
    value = Path(resource["output"])
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("output is not relative")
    return _under(root, root / value)


def _run_plenum(root: Path, resource: dict, job: Path, now: datetime, deadline: float, state: dict) -> dict:
    output = _output(root, resource)
    original = _snapshot(root, output, job, state)
    receipt = _receipt(job, resource["url"], now, deadline)
    href = resource["url"].removeprefix("https://sudact.ru")
    name, candidate = maintenance_plenums._candidate(href, resource.get("label", ""), receipt, now)
    if output != root / "knowledge" / "plenumy" / name:
        raise ValueError("Plenum output does not match source identity")
    maintenance_plenums._valid(candidate, resource["url"])
    _verify_receipt(job, receipt)
    status = _activate(root, output, candidate, job, original)
    state["activated_sha256"] = _sha(candidate)
    state["receipts"][resource["url"]] = {key: receipt[key] for key in receipt if key != "body"}
    _write_checkpoint(job, state)
    return {"id": resource["id"], "status": status, "detail": "verified Plenum candidate",
            "last_checked": receipt["checked_at"],
            "last_updated": datetime.now(UTC).isoformat().replace("+00:00", "Z") if status == "updated" else None,
            "progress": _progress(1, 1)}


def _run_catalog(resource: dict, job: Path, now: datetime, deadline: float, state: dict) -> dict:
    if resource["url"] != corpus.SUDACT_PLENUM_CATALOG:
        raise ValueError("catalog URL is not registered")
    original = maintenance_plenums._receipt

    def receipt(url: str, _state: Path, _deadline: float, _paths: list[str]) -> dict:
        value = _receipt(job, url, now, deadline)
        try:
            if not corpus.PLENUM_LINK_RE.findall(value["body"].decode("utf-8")):
                raise ValueError("catalog page has no Plenum links")
            _verify_receipt(job, value)
        except UnicodeDecodeError as exc:
            raise ValueError("catalog is not UTF-8") from exc
        return value

    maintenance_plenums._receipt = receipt
    try:
        targets, discovery, receipts = maintenance_plenums._discover(job, deadline, [])
    finally:
        maintenance_plenums._receipt = original
    if discovery.get("status") != "complete":
        raise RuntimeError(f"catalog discovery {discovery.get('reason', 'incomplete')}")
    discovered = []
    for href, label in targets:
        url = "https://sudact.ru" + href
        discovered.append({"id": "legal-corpus:plenum:" + Path(corpus.plenum_slug_to_filename(href)).stem,
                           "kind": "plenum", "parser": "sudact-plenum", "url": url,
                           "output": "knowledge/plenumy/" + corpus.plenum_slug_to_filename(href),
                           "label": label})
    state["receipts"].update({item["source_url"]: {key: item[key] for key in item if key != "body"}
                              for item in receipts})
    state["discovered"] = discovered
    _write_checkpoint(job, state)
    checked = min((item["checked_at"] for item in receipts), default=None)
    return {"id": resource["id"], "status": "current", "detail": "catalog page discovered",
            "last_checked": checked, "last_updated": None,
            "progress": _progress(len(discovered), len(discovered)), "discovered_resources": discovered}


def _run_generic(root: Path, resource: dict, job: Path, now: datetime, deadline: float, state: dict) -> dict:
    output = _output(root, resource)
    original = _snapshot(root, output, job, state)
    receipt = _receipt(job, resource["url"], now, deadline)
    try:
        import corpus_registry
        builder = (corpus_registry.build_review_candidate
                   if resource["parser"] == "vsrf-review"
                   else corpus_registry.build_generic_candidate)
        relative_output, candidate = builder(resource, receipt, now)
    except Exception as exc:
        raise RuntimeError(f"generic candidate failed: {type(exc).__name__}") from exc
    if relative_output != resource["output"] or not isinstance(candidate, bytes):
        raise ValueError("generic candidate missing")
    _verify_receipt(job, receipt)
    status = _activate(root, output, candidate, job, original)
    state["activated_sha256"] = _sha(candidate)
    state["receipts"][resource["url"]] = {key: receipt[key] for key in receipt if key != "body"}
    _write_checkpoint(job, state)
    return {"id": resource["id"], "status": status, "detail": "verified generic candidate",
            "last_checked": receipt["checked_at"],
            "last_updated": datetime.now(UTC).isoformat().replace("+00:00", "Z") if status == "updated" else None,
            "progress": _progress(1, 1)}


def _run_code(root: Path, resource: dict, job: Path, now: datetime, deadline: float, state: dict) -> dict:
    supplied = resource.get("entry")
    slug = supplied.get("slug") if isinstance(supplied, dict) else None
    entry = next((item for item in corpus.KODEKSY if item["slug"] == slug), None)
    if entry is None or resource["id"] != f"legal-corpus:code:{slug}":
        raise ValueError("consultant-code entry is not in registry")
    output = _output(root, resource)
    if output != root / "knowledge" / "kodeksy" / f"{slug}.md":
        raise ValueError("code output does not match registry")
    original = _snapshot(root, output, job, state)
    from maintenance_sources import _corpus
    result = _corpus(output, job, now, True, deadline, force_rebuild=True,
                     initialize_missing=True, original_snapshot=original,
                     snapshot_exists=state["original"]["exists"])
    state["delegate"] = {key: result.get(key) for key in ("status", "detail", "source_revision")}
    if result.get("status") == "updated":
        candidate_sha = result.get("candidate_sha256")
        if not isinstance(candidate_sha, str) or not candidate_sha.startswith("sha256:"):
            raise RuntimeError("updated code has no validated candidate SHA")
        state["activated_sha256"] = candidate_sha
    _write_checkpoint(job, state)
    status = result.get("status", "failed")
    progress = (result.get("progress") if isinstance(result.get("progress"), dict)
                else {"done": None, "total": None, "pending_count": None,
                      "unit": "source_pages_received"})
    return {"id": resource["id"], "status": status, "detail": result.get("detail", ""),
            "last_checked": result.get("last_checked"),
            "last_updated": datetime.now(UTC).isoformat().replace("+00:00", "Z") if status == "updated" else None,
            "failure_code": result.get("failure_code"), "progress": progress}


def _blocked_fetch(exc: maintenance_fetch.MaintenanceFetchError) -> str | None:
    if exc.code in BLOCKED_FETCH_CODES:
        return exc.code
    attempts = exc.diagnostic.get("attempts") if isinstance(exc.diagnostic, dict) else None
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get("code") in BLOCKED_FETCH_CODES:
                return attempt["code"]
    return None


def run_job(root: Path, resource: dict, state_dir: Path, deadline: float) -> dict:
    """Строит ровно один ресурс; deadline — абсолютное monotonic-время."""
    try:
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)):
            raise ValueError("deadline invalid")
        root, resource = Path(root).resolve(), _resource(resource)
        if resource["parser"] == "vsrf-review":
            return {"id": resource["id"], "status": "blocked",
                    "detail": "review_parser_not_verified", "failure_code": "review_parser_not_verified",
                    "progress": _progress(0, 0)}
        now = datetime.now(UTC)
        job = _job_dir(root, Path(state_dir).resolve(), resource["id"])
        state = _read_checkpoint(job, resource)
        if time.monotonic() >= deadline:
            return {"id": resource["id"], "status": "deferred", "detail": "deadline exhausted",
                    "progress": _progress(0, 0)}
        if resource["parser"] == "sudact-plenum":
            return _run_plenum(root, resource, job, now, deadline, state)
        if resource["parser"] == "sudact-catalog":
            return _run_catalog(resource, job, now, deadline, state)
        if resource["parser"] == "consultant-act":
            return _run_generic(root, resource, job, now, deadline, state)
        return _run_code(root, resource, job, now, deadline, state)
    except Deadline:
        return {"id": resource.get("id", "unknown") if isinstance(resource, dict) else "unknown",
                "status": "deferred", "detail": "deadline exhausted", "progress": _progress(0, 0)}
    except maintenance_fetch.MaintenanceFetchError as exc:
        code = _blocked_fetch(exc)
        return {"id": resource.get("id", "unknown") if isinstance(resource, dict) else "unknown",
                "status": "blocked" if code else "failed",
                "detail": f"source access blocked: {code}" if code else f"fetch failed: {exc.code}",
                "failure_code": code or exc.code, "progress": _progress(0, 0)}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"id": resource.get("id", "unknown") if isinstance(resource, dict) else "unknown",
                "status": "failed", "detail": str(exc), "progress": _progress(0, 0)}
