#!/usr/bin/env python3
"""Durable, low-priority queue for public legal-corpus resources."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.themiz.corpus-background"
INTERVAL = 300
JOB_SECONDS = 120.0
TICK_SECONDS = 180.0
LEASE_SECONDS = 240.0
REPORT_GRACE_SECONDS = 2.0
MIN_FREE_BYTES = 1024 ** 3
SCHEDULER_PATH = os.pathsep.join((str(Path(sys.executable).parent), "/usr/local/bin",
                                  "/opt/homebrew/bin", str(Path.home() / ".npm-global" / "bin"),
                                  "/usr/bin", "/bin", "/usr/sbin", "/sbin"))
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_time(value: object, field: str, now: float) -> float:
    if not isinstance(value, str):
        raise ValueError(f"adapter не вернул {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"adapter вернул неверный {field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"adapter вернул naive {field}")
    timestamp = parsed.astimezone(UTC).timestamp()
    if timestamp > now + 1:
        raise ValueError(f"adapter вернул будущий {field}")
    return timestamp


def _resource_hash(resource: dict) -> str:
    return hashlib.sha256(_json(resource).encode()).hexdigest()


def _directory(root: Path) -> Path:
    return root / ".agent" / "maintenance" / "background"


def _safe_root(root: Path) -> Path:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("root не является каталогом")
    return root


def _inside(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("output должен быть непустым относительным путем")
    candidate = root / relative
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("output выходит за root")
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ValueError("output проходит через symlink")
        current = current.parent
    return candidate


def _validate_resource(root: Path, resource: object) -> dict:
    if not isinstance(resource, dict):
        raise ValueError("resource должен быть JSON-объектом")
    required = {"id", "kind", "parser", "url", "output"}
    optional = {"entry", "label", "title", "date", "number"}
    if not required.issubset(resource) or set(resource) - required - optional:
        raise ValueError("resource имеет неизвестные или отсутствующие поля")
    if not isinstance(resource["id"], str) or not ID_RE.fullmatch(resource["id"]):
        raise ValueError("недопустимый id ресурса")
    for key in ("kind", "parser"):
        if not isinstance(resource[key], str) or not resource[key].strip():
            raise ValueError(f"{key} должен быть непустой строкой")
    url = urlsplit(resource["url"] if isinstance(resource["url"], str) else "")
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("разрешен только публичный HTTPS URL без credentials")
    _inside(root, resource["output"])
    if "entry" in resource and not isinstance(resource["entry"], (str, int, dict)):
        raise ValueError("entry имеет недопустимый тип")
    if "label" in resource and not isinstance(resource["label"], str):
        raise ValueError("label должен быть строкой")
    # JSON round-trip drops subclasses and rejects non-serializable values.
    clean = json.loads(_json(resource))
    return clean


def _registry(root: Path) -> dict[str, dict]:
    from corpus_registry import registry

    values = registry(root)
    if not isinstance(values, list):
        raise ValueError("corpus registry должен вернуть список")
    result: dict[str, dict] = {}
    for raw in values:
        item = _validate_resource(root, raw)
        if item["id"] in result:
            raise ValueError(f"повтор id в registry: {item['id']}")
        result[item["id"]] = item
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  resource_id TEXT PRIMARY KEY,
  resource_hash TEXT NOT NULL,
  resource_json TEXT NOT NULL,
  state TEXT NOT NULL,
  priority INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at REAL NOT NULL,
  lease_until REAL,
  progress TEXT,
  detail TEXT,
  freshness TEXT NOT NULL DEFAULT 'unknown',
  availability TEXT NOT NULL DEFAULT 'unknown',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  last_checked REAL,
  last_updated REAL,
  lease_token TEXT,
  pending_resource_hash TEXT,
  pending_resource_json TEXT
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(state, available_at, priority, created_at);
"""


def _open_write(root: Path) -> sqlite3.Connection:
    directory = _directory(root)
    current = root
    for part in directory.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("background state path не может быть symlink")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("background state directory не может быть symlink")
    db = directory / "queue.sqlite3"
    conn = sqlite3.connect(db, timeout=0.05, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=50")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    additions = {
        "last_checked": "REAL", "last_updated": "REAL", "lease_token": "TEXT",
        "pending_resource_hash": "TEXT", "pending_resource_json": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
    return conn


def _row(row: sqlite3.Row, root: Path | None = None) -> dict:
    result = dict(row)
    result["id"] = result.pop("resource_id")
    raw_resource = result.pop("resource_json", None)
    result.pop("pending_resource_json", None)
    result.pop("lease_token", None)
    if root is not None:
        try:
            item = json.loads(raw_resource)
            target = _inside(root, item["output"])
            result["local_file_present"] = target.is_file() and not target.is_symlink()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, OSError):
            result["local_file_present"] = False
    if result.get("progress"):
        try:
            result["progress"] = json.loads(result["progress"])
        except json.JSONDecodeError:
            result["progress"] = {"raw": result["progress"]}
    checked = result.get("last_checked")
    if (result.get("freshness") == "current" and isinstance(checked, (int, float))
            and (checked > time.time() or time.time() - checked > 7 * 86400)):
        result["freshness"] = "stale"
    for key in ("last_checked", "last_updated"):
        if isinstance(result.get(key), (int, float)):
            result[key] = datetime.fromtimestamp(result[key], UTC).isoformat().replace("+00:00", "Z")
    return result


def _registry_validate(root: Path, resource: object) -> dict:
    """Registry owns parser/host policy; this module owns JSON/path safety."""
    import corpus_registry

    validator = getattr(corpus_registry, "validate_resource", None)
    if not callable(validator):
        raise RuntimeError("corpus_registry.validate_resource недоступен")
    checked = validator(resource)
    return _validate_resource(root, resource if checked is None else checked)


def _enqueue_validated(root: Path, item: dict, priority: int = 100, *, force: bool = False) -> dict:
    if type(priority) is not int or not -1000 <= priority <= 1000:
        raise ValueError("priority вне диапазона -1000..1000")
    now = time.time()
    digest = _resource_hash(item)
    with _open_write(root) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM jobs WHERE resource_id=?", (item["id"],)).fetchone()
        same = bool(old and old["resource_hash"] == digest)
        recently_checked = bool(old and old["last_checked"] is not None
                                and 0 <= now - old["last_checked"] <= 7 * 86400)
        deduplicated = bool(old and same and not force and (
            old["state"] in {"queued", "running", "deferred", "blocked"}
            or (old["state"] in {"current", "updated"} and recently_checked)))
        pending = bool(old and old["state"] == "running" and (not same or force))
        if pending:
            conn.execute("""UPDATE jobs SET pending_resource_hash=?,pending_resource_json=?,
                            priority=MIN(priority,?),updated_at=? WHERE resource_id=?""",
                         (digest, _json(item), priority, now, item["id"]))
        elif deduplicated:
            if priority < old["priority"]:
                conn.execute("UPDATE jobs SET priority=?, updated_at=? WHERE resource_id=?",
                             (priority, now, item["id"]))
        elif old is None:
            conn.execute("""
              INSERT INTO jobs(resource_id,resource_hash,resource_json,state,priority,attempts,
                               available_at,lease_until,progress,detail,freshness,availability,
                               created_at,updated_at,started_at,finished_at,last_checked,last_updated,
                               lease_token,pending_resource_hash,pending_resource_json)
              VALUES(?,?,?,'queued',?,0,?,NULL,NULL,NULL,'unknown','unknown',?,?,NULL,NULL,NULL,NULL,
                     NULL,NULL,NULL)
            """, (item["id"], digest, _json(item), priority, now, now, now))
        else:
            next_freshness = old["freshness"] if same else "unknown"
            if same and not recently_checked and next_freshness == "current":
                next_freshness = "stale"
            next_availability = old["availability"] if same else "unknown"
            conn.execute("""UPDATE jobs SET resource_hash=?,resource_json=?,state='queued',priority=?,
                            attempts=?,available_at=?,lease_until=NULL,lease_token=NULL,progress=NULL,
                            detail=NULL,freshness=?,availability=?,updated_at=?,
                            started_at=NULL,finished_at=NULL,
                            last_checked=CASE WHEN resource_hash=? THEN last_checked ELSE NULL END,
                            last_updated=CASE WHEN resource_hash=? THEN last_updated ELSE NULL END,
                            pending_resource_hash=NULL,pending_resource_json=NULL
                            WHERE resource_id=?""",
                         (digest, _json(item), priority, 0 if (force or not same) else old["attempts"],
                          now, next_freshness, next_availability, now, digest, digest, item["id"]))
        row = conn.execute("SELECT * FROM jobs WHERE resource_id=?", (item["id"],)).fetchone()
        conn.commit()
    result = _row(row)
    result["deduplicated"] = deduplicated
    result["pending_update"] = pending
    return result


def enqueue(root: Path, resource: dict, priority: int = 100) -> dict:
    root = _safe_root(root)
    return _enqueue_validated(root, _registry_validate(root, resource), priority)


def enqueue_registered(root: Path, ids: set[str] | list[str] | None = None, *, force: bool = False) -> list[dict]:
    root = _safe_root(root)
    items = _registry(root)
    selected = set(items) if ids is None else set(ids)
    unknown = selected - set(items)
    if unknown:
        raise ValueError("неизвестные resource id: " + ", ".join(sorted(unknown)))
    return [_enqueue_validated(root, items[name], force=force) for name in sorted(selected)]


def enqueue_all(root: Path, *, force: bool = False) -> list[dict]:
    """Публичный ручной запуск всей зарегистрированной очереди."""
    return enqueue_registered(root, force=force)


def _queued_resource(root: Path, resource_id: str) -> dict | None:
    root = _safe_root(root)
    db = _inside(root, ".agent/maintenance/background/queue.sqlite3")
    if not db.exists() or db.is_symlink():
        return None
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=0.05)
    try:
        row = conn.execute("SELECT resource_json FROM jobs WHERE resource_id=?", (resource_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def enqueue_id(root: Path, resource_id: str, *, force: bool = False) -> dict:
    root = _safe_root(root)
    item = _registry(root).get(resource_id) or _queued_resource(root, resource_id)
    if item is None:
        raise ValueError(f"неизвестный resource id: {resource_id}")
    checked = _registry_validate(root, item)
    return _enqueue_validated(root, checked, force=force)


def status(root: Path, resource_id: str | None = None) -> dict:
    root = _safe_root(root)
    db = _inside(root, ".agent/maintenance/background/queue.sqlite3")
    if not db.exists():
        return {"ok": True, "queue": {}, "jobs": []}
    if db.is_symlink():
        raise ValueError("queue database не может быть symlink")
    uri = f"file:{db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=0.05)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=50")
    try:
        if resource_id is None:
            rows = conn.execute("SELECT * FROM jobs ORDER BY priority,created_at").fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs WHERE resource_id=?", (resource_id,)).fetchall()
    finally:
        conn.close()
    jobs = [_row(item, root) for item in rows]
    counts: dict[str, int] = {}
    for item in jobs:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {"ok": True, "queue": counts, "jobs": jobs}


def schedule(root: Path) -> dict:
    """Стабильная сводка расписания для локального cockpit API."""
    root = _safe_root(root)
    policy_enabled = enabled(root)
    target = _agents_dir() / f"{LABEL}.plist"
    plist_matches = False
    if target.exists() and not target.is_symlink():
        try:
            plist_matches = plistlib.loads(target.read_bytes()) == _payload(root)
        except (OSError, ValueError, plistlib.InvalidFileException):
            plist_matches = False
    loaded = False
    if plist_matches:
        try:
            loaded = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
        except OSError:
            loaded = False
    overview = status(root)
    due = [item["available_at"] for item in overview["jobs"]
           if item["state"] in {"queued", "deferred", "failed"}
           and isinstance(item.get("available_at"), (int, float))]
    return {"interval_s": INTERVAL, "job_budget_s": JOB_SECONDS, "tick_budget_s": TICK_SECONDS,
            "next_tick_at": None,
            "queued_due_at": (datetime.fromtimestamp(min(due), UTC).isoformat().replace("+00:00", "Z")
                              if due else None),
            "enabled": policy_enabled, "loaded": loaded, "plist_matches": plist_matches,
            "active": policy_enabled and loaded,
            "scheduler_status": ("active" if policy_enabled and loaded else
                                 "disabled" if not policy_enabled else "not_loaded"),
            "queue": overview["queue"]}


@contextmanager
def _worker_lock(root: Path):
    directory = _directory(root)
    _inside(root, ".agent/maintenance/background/worker.lock")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _claim(root: Path, now: float) -> tuple[dict, dict] | None:
    with _open_write(root) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""UPDATE jobs SET state='queued',lease_until=NULL,updated_at=?,
                        lease_token=NULL,detail='recovered stale worker lease'
                        WHERE state='running' AND lease_until<?""", (now, now))
        row = conn.execute("""SELECT * FROM jobs
                              WHERE state IN ('queued','deferred','failed') AND available_at<=?
                              ORDER BY priority,available_at,created_at LIMIT 1""", (now,)).fetchone()
        if row is None:
            conn.commit()
            return None
        token = secrets.token_hex(16)
        changed = conn.execute("""UPDATE jobs SET state='running',lease_until=?,lease_token=?,started_at=?,
                                  updated_at=?,attempts=attempts+1
                                  WHERE resource_id=? AND state=? AND updated_at=?""",
                               (now + LEASE_SECONDS, token, now, now, row["resource_id"],
                                row["state"], row["updated_at"])).rowcount
        if changed != 1:
            conn.rollback()
            return None
        conn.commit()
    return json.loads(row["resource_json"]), {"attempts": row["attempts"] + 1,
                                               "lease_token": token}


def _child(root: str, resource: dict, state_dir: str, deadline: float, pipe) -> None:
    try:
        if hasattr(os, "setsid"):
            os.setsid()
        pipe.send({"ready": True})
        from corpus_jobs import run_job
        result = run_job(Path(root), resource, Path(state_dir), deadline)
        pipe.send({"result": result})
    except BaseException as exc:
        pipe.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        pipe.close()


def _cleanup_group(process, isolated: bool) -> None:
    """Stop leader and every fetch/browser descendant in its private process group."""
    if not isolated or not process.pid or not hasattr(os, "killpg"):
        if process.is_alive():
            process.terminate()
            process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        return
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    process.join(0.5)
    # Leader may already be reaped while a descendant ignores SIGTERM.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.join(1)


def _has_private_group(process) -> bool:
    try:
        return bool(process.pid and os.getpgid(process.pid) == process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _run_isolated(root: Path, resource: dict, state_dir: Path, seconds: float) -> dict:
    import multiprocessing

    end = time.monotonic() + seconds
    grace = min(REPORT_GRACE_SECONDS, max(0.01, seconds * 0.1), seconds / 2)
    adapter_deadline = end - grace
    parent, child = multiprocessing.Pipe(False)
    process = multiprocessing.get_context("spawn").Process(
        target=_child, args=(str(root), resource, str(state_dir), adapter_deadline, child))
    started = isolated = False
    try:
        process.start()
        started = True
        child.close()
        wait_ready = max(0.0, min(5.0, end - time.monotonic()))
        if not parent.poll(wait_ready):
            return {"status": "deferred", "detail": "worker не запустился в срок"}
        message = parent.recv()
        if not isinstance(message, dict) or message.get("ready") is not True:
            return {"status": "failed", "detail": "worker завершился до ready"}
        isolated = True
        if parent.poll(max(0.0, end - time.monotonic())):
            message = parent.recv()
            if "result" in message:
                return message["result"]
            return {"status": "failed", "detail": message.get("error", "worker завершился без результата")}
        return {"status": "deferred", "detail": f"job превысил {seconds:.1f} с"}
    except (EOFError, OSError) as exc:
        return {"status": "failed", "detail": f"worker pipe: {type(exc).__name__}"}
    finally:
        child.close()
        if started:
            # A completed adapter can still leave Lynceuz descendants alive.
            _cleanup_group(process, isolated or _has_private_group(process))
        parent.close()


def _finish(root: Path, resource_id: str, result: object, attempts: int, lease_token: str) -> dict:
    now = time.time()
    if not isinstance(result, dict):
        result = {"status": "failed", "detail": "adapter вернул не JSON-объект"}
    if result.get("id", resource_id) != resource_id:
        result = {"status": "failed", "detail": "adapter вернул чужой resource id"}
    outcome = result.get("status")
    if outcome not in {"current", "updated", "deferred", "failed", "blocked"}:
        result = {"status": "failed", "detail": "adapter вернул неизвестный status"}
        outcome = "failed"
    checked_at = updated_at = None
    if outcome in {"current", "updated"}:
        try:
            checked_at = _source_time(result.get("last_checked"), "last_checked", now)
            if outcome == "updated":
                updated_at = _source_time(result.get("last_updated"), "last_updated", now)
                if updated_at < checked_at:
                    raise ValueError("last_updated раньше last_checked")
        except ValueError as exc:
            result = {"status": "failed", "detail": str(exc)}
            outcome = "failed"
    if outcome == "failed":
        state = "deferred"
        available_at = now + min(86400.0, 60.0 * (2 ** min(attempts - 1, 10)))
    elif outcome == "deferred":
        state = "deferred"
        available_at = now + min(3600.0, max(60.0, float(result.get("retry_after_s", 300))))
    else:
        state, available_at = outcome, now
    freshness = str(result.get("freshness") or ("current" if outcome in {"current", "updated"} else "unknown"))
    availability = str(result.get("availability") or ("available" if outcome in {"current", "updated"} else "unknown"))
    progress = result.get("progress")
    if progress is not None:
        keys = ("done", "total", "pending_count")
        allowed = set(keys) | {"unit"}
        values = [progress.get(key) for key in keys] if isinstance(progress, dict) else []
        all_unknown = len(values) == 3 and all(value is None for value in values)
        all_known = (len(values) == 3
                     and all(type(value) is int and value >= 0 for value in values)
                     and values[0] + values[2] == values[1])
        unit_ok = (isinstance(progress, dict) and
                   ("unit" not in progress or progress["unit"] == "source_pages_received"))
        if (not isinstance(progress, dict) or not set(progress).issubset(allowed)
                or not set(keys).issubset(progress) or not unit_ok
                or not (all_unknown or all_known)):
            result = {"status": "failed", "detail": "adapter вернул недопустимый progress"}
            outcome, state, progress = "failed", "deferred", None
            available_at = now + min(86400.0, 60.0 * (2 ** min(attempts - 1, 10)))
            freshness, availability = "unknown", "unknown"
            checked_at = updated_at = None
    last_checked = checked_at
    last_updated = updated_at
    with _open_write(root) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        owned = conn.execute("""SELECT pending_resource_hash,pending_resource_json,
                              freshness,availability FROM jobs
                              WHERE resource_id=? AND state='running' AND lease_token=?""",
                           (resource_id, lease_token)).fetchone()
        if owned is None:
            changed, queue_state = 0, state
        elif owned["pending_resource_json"]:
            changed = conn.execute("""UPDATE jobs SET resource_hash=?,resource_json=?,state='queued',
                                      attempts=0,available_at=?,lease_until=NULL,lease_token=NULL,
                                      progress=NULL,detail=NULL,freshness='unknown',availability='unknown',
                                      updated_at=?,started_at=NULL,finished_at=NULL,last_checked=NULL,
                                      last_updated=NULL,pending_resource_hash=NULL,pending_resource_json=NULL
                                      WHERE resource_id=? AND state='running' AND lease_token=?""",
                                   (owned["pending_resource_hash"], owned["pending_resource_json"], now,
                                    now, resource_id, lease_token)).rowcount
            queue_state = "queued"
        else:
            if outcome not in {"current", "updated"}:
                if "freshness" not in result:
                    freshness = owned["freshness"]
                if "availability" not in result:
                    availability = owned["availability"]
            changed = conn.execute("""UPDATE jobs SET state=?,available_at=?,lease_until=NULL,
                                      lease_token=NULL,progress=?,detail=?,freshness=?,availability=?,
                                      updated_at=?,finished_at=?,last_checked=COALESCE(?,last_checked),
                                      last_updated=COALESCE(?,last_updated)
                                      WHERE resource_id=? AND state='running' AND lease_token=?""",
                                   (state, available_at, _json(progress) if progress is not None else None,
                                    str(result.get("detail", ""))[:4000], freshness, availability,
                                    now, now, last_checked, last_updated, resource_id, lease_token)).rowcount
            queue_state = state
        conn.commit()
    if changed != 1:
        return {"id": resource_id, "status": "failed", "detail": "lease потеряна до записи результата"}
    return {**result, "id": resource_id, "queue_state": queue_state,
            "freshness": freshness, "availability": availability}


def _approved(root: Path) -> bool:
    try:
        path = _inside(root, ".agent/maintenance/policy.json")
    except ValueError:
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("official_sources_approved") is True


def enabled(root: Path) -> bool:
    """Read dedicated opt-in without parsing registry or touching network."""
    try:
        root = _safe_root(root)
        path = _inside(root, ".agent/maintenance/background/policy.json")
        if path.is_symlink():
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("enabled") is True


def tick(root: Path, *, max_seconds: float = TICK_SECONDS, job_seconds: float = JOB_SECONDS,
         runner=None) -> dict:
    root = _safe_root(root)
    started = time.monotonic()
    results: list[dict] = []
    if max_seconds <= 0 or job_seconds <= 0:
        raise ValueError("таймаут должен быть положительным")
    with _worker_lock(root) as acquired:
        if not acquired:
            return {"processed": 0, "results": [], "elapsed_s": time.monotonic() - started,
                    "stopped_reason": "worker_busy"}
        if not enabled(root):
            return {"processed": 0, "results": [], "elapsed_s": time.monotonic() - started,
                    "stopped_reason": "background_disabled"}
        if not _approved(root):
            return {"processed": 0, "results": [], "elapsed_s": time.monotonic() - started,
                    "stopped_reason": "official_sources_not_approved"}
        while True:
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                reason = "tick_deadline"
                break
            claimed = _claim(root, time.time())
            if claimed is None:
                reason = "queue_empty_or_backoff"
                break
            resource, meta = claimed
            try:
                free_bytes = shutil.disk_usage(root).free
                disk_detail = f"нужно минимум {MIN_FREE_BYTES} свободных байт; доступно {free_bytes}"
            except OSError as exc:
                free_bytes = 0
                disk_detail = f"не удалось проверить свободное место: {type(exc).__name__}"
            if free_bytes < MIN_FREE_BYTES:
                results.append(_finish(root, resource["id"],
                                       {"status": "deferred", "detail": disk_detail,
                                        "retry_after_s": 3600},
                                       meta["attempts"], meta["lease_token"]))
                reason = "low_disk_space"
                break
            state_dir = _directory(root) / "jobs" / hashlib.sha256(resource["id"].encode()).hexdigest()
            state_dir.mkdir(parents=True, exist_ok=True)
            seconds = min(job_seconds, remaining)
            try:
                if runner is None:
                    outcome = _run_isolated(root, resource, state_dir, seconds)
                else:
                    outcome = runner(root, resource, state_dir, time.monotonic() + seconds)
            except Exception as exc:
                outcome = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
            discovered = outcome.get("discovered_resources", []) if isinstance(outcome, dict) else []
            try:
                if not isinstance(discovered, list):
                    raise ValueError("discovered_resources должен быть списком")
                validated = [_registry_validate(root, item) for item in discovered]
            except Exception as exc:
                outcome = {"status": "failed", "detail": f"discovered resource rejected: {exc}"}
                validated = []
            finished = _finish(root, resource["id"], outcome, meta["attempts"],
                               meta["lease_token"])
            results.append(finished)
            if finished.get("status") in {"current", "updated"}:
                for item in validated:
                    _enqueue_validated(root, item)
    return {"processed": len(results), "results": results,
            "elapsed_s": time.monotonic() - started, "stopped_reason": reason}


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _payload(root: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/nice", "-n", "10", sys.executable,
                             str(ROOT / "scripts" / "background_corpus.py"),
                             "--tick", "--root", str(root)],
        "StartInterval": INTERVAL,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "EnvironmentVariables": {"PATH": SCHEDULER_PATH},
        "WorkingDirectory": str(root),
        "StandardOutPath": str(_directory(root) / "launchd.log"),
        "StandardErrorPath": str(_directory(root) / "launchd.err.log"),
    }


def _set_enabled(root: Path, value: bool) -> None:
    path = _inside(root, ".agent/maintenance/background/policy.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("background policy не может быть symlink")
    try:
        policy = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError as exc:
        raise ValueError("background policy поврежден") from exc
    if not isinstance(policy, dict):
        raise ValueError("background policy должен быть JSON-объектом")
    policy["enabled"] = value
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(policy, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def install(root: Path) -> dict:
    root = _safe_root(root)
    if root != ROOT.resolve():
        raise ValueError("install разрешен только для canonical self root")
    agents = _agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    target = agents / f"{LABEL}.plist"
    payload = _payload(root)
    if target.exists():
        if target.is_symlink() or plistlib.loads(target.read_bytes()) != payload:
            raise FileExistsError("чужой plist не перезаписан")
        probe = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode != 0:
            try:
                subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            except Exception:
                _set_enabled(root, False)
                raise
        _set_enabled(root, True)
        return {"status": "current" if probe.returncode == 0 else "installed",
                "label": LABEL, "plist": str(target)}
    with target.open("xb") as handle:
        handle.write(plistlib.dumps(payload))
    try:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _set_enabled(root, True)
    except Exception:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target.unlink(missing_ok=True)
        raise
    return {"status": "installed", "label": LABEL, "plist": str(target)}


def uninstall(root: Path) -> dict:
    root = _safe_root(root)
    if root != ROOT.resolve():
        raise ValueError("uninstall разрешен только для canonical self root")
    target = _agents_dir() / f"{LABEL}.plist"
    if not target.exists():
        _set_enabled(root, False)
        return {"status": "absent", "label": LABEL}
    if target.is_symlink() or plistlib.loads(target.read_bytes()) != _payload(root):
        raise FileExistsError("чужой plist не удален")
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target.unlink()
    _set_enabled(root, False)
    return {"status": "uninstalled", "label": LABEL}


def refresh_graph_after_tick(root: Path, result: dict) -> dict:
    """Только фоновый CLI: независимая недельная сборка после загрузчика."""
    if result.get("stopped_reason") in {
        "worker_busy", "background_disabled", "official_sources_not_approved", "low_disk_space",
    }:
        return {"status": "skipped", "reason": result["stopped_reason"]}
    graph_script = ROOT / "scripts" / "legal_graph.py"
    if not graph_script.is_file():
        return {"status": "unavailable", "reason": "legal_graph_not_installed",
                "last_good_preserved": True}
    try:
        completed = subprocess.run(
            [sys.executable, str(graph_script),
             "--refresh", "--root", str(root), "--json"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        outcome = json.loads(completed.stdout)
        if not isinstance(outcome, dict):
            raise ValueError("graph worker did not return an object")
        if completed.returncode != 0:
            outcome["returncode"] = completed.returncode
        return outcome
    except subprocess.TimeoutExpired:
        return {"status": "deferred", "reason": "graph_deadline_60s",
                "last_good_preserved": True}
    except (OSError, ValueError) as exc:
        return {"status": "failed", "reason": type(exc).__name__, "last_good_preserved": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", nargs="?", const="", metavar="ID")
    action.add_argument("--tick", action="store_true")
    action.add_argument("--enqueue-all", action="store_true")
    action.add_argument("--enqueue", metavar="ID")
    action.add_argument("--schedule", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="повторно поставить зарегистрированный ресурс")
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.status is not None:
            result = status(args.root, args.status or None)
        elif args.schedule:
            result = schedule(args.root)
        elif args.tick:
            registry_error = None
            if enabled(args.root) and _approved(args.root):
                try:
                    enqueue_registered(args.root)
                except Exception as exc:
                    registry_error = type(exc).__name__
            result = tick(args.root)
            if registry_error:
                result["registry_error"] = registry_error
            result["legal_graph"] = refresh_graph_after_tick(args.root, result)
        elif args.enqueue_all:
            result = enqueue_all(args.root, force=args.force)
        elif args.enqueue:
            result = enqueue_id(args.root, args.enqueue, force=args.force)
        elif args.install:
            result = install(args.root)
        else:
            result = uninstall(args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "detail": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
