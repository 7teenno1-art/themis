#!/usr/bin/env python3
"""Фактическое weekly-окно подписки из безопасных полей Codex session events."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path


WEEK_MINUTES = 7 * 24 * 60
MAX_AGE = timedelta(days=7)
MAX_WEEK_AHEAD = timedelta(days=7, hours=1)
MIN_RESET_AT = 1_577_836_800  # 01.01.2020 UTC: отсечь не-epoch и мусор.
MAX_PERCENT = 100.0
SOURCE = "codex_session_rate_limits"
SCAN_SECONDS = 30.0
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_SCAN_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
SMALL_JSON_BYTES = 64 * 1024


def _unknown(status: str) -> dict:
    return {
        "provider": "codex",
        "source": SOURCE,
        "observed_at": None,
        "reset_at": None,
        "limit_id": None,
        "status": status,
        "used_percent": None,
    }


def _utc(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(UTC) if result.tzinfo else None


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _weekly(payload: object, observed_at: datetime) -> dict | None:
    """Возвращает weekly limit; плохая структурированная метрика бросает ValueError."""
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    matches = []
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError(f"{name} не объект")
        minutes = window.get("window_minutes")
        if minutes is None:
            continue
        if type(minutes) is not int or minutes < 1 or minutes > 31 * 24 * 60:
            raise ValueError(f"{name}.window_minutes")
        if minutes != WEEK_MINUTES:
            continue
        reset_at = window.get("resets_at")
        if type(reset_at) is not int or reset_at < MIN_RESET_AT:
            raise ValueError(f"{name}.resets_at")
        if reset_at > int((observed_at + MAX_WEEK_AHEAD).timestamp()):
            raise ValueError(f"{name}.resets_at вне weekly-окна")
        used = window.get("used_percent")
        if used is not None:
            if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= MAX_PERCENT:
                raise ValueError(f"{name}.used_percent")
            used = float(used)
        limit_id = limits.get("limit_id")
        if limit_id is not None and (not isinstance(limit_id, str) or not limit_id):
            raise ValueError("limit_id")
        matches.append({
            "provider": "codex", "source": SOURCE,
            "observed_at": _format_utc(observed_at), "reset_at": reset_at,
            "limit_id": limit_id, "status": "fresh", "used_percent": used,
        })
    if len(matches) > 1:
        raise ValueError("два weekly-окна")
    return matches[0] if matches else None


def observe_codex(session_root: Path, now: datetime) -> dict:
    """Последнее factual weekly-окно. Не читает и не выдает текст диалогов."""
    if now.tzinfo is None:
        raise ValueError("now должен быть timezone-aware")
    now = now.astimezone(UTC)
    started = time.monotonic()
    latest: tuple[datetime, str, int, dict] | None = None
    corrupt = False
    corrupt_at: datetime | None = None
    unplaced_quota_corrupt = False
    incomplete = False
    scanned_bytes = 0
    try:
        # Дата папки — дата создания thread, не event timestamp: long thread может быть актуален.
        paths = []
        cutoff = now.timestamp() - MAX_AGE.total_seconds()
        for path in session_root.rglob("*.jsonl"):
            if time.monotonic() - started >= SCAN_SECONDS:
                return _unknown("unknown_timeout")
            try:
                stat = path.stat()
            except OSError:
                corrupt = True
                incomplete = True
                continue
            # mtime надежно меняется при append. Дата каталогов thread не используется.
            if stat.st_mtime >= cutoff:
                paths.append((stat.st_mtime, stat.st_size, path))
    except OSError:
        return _unknown("unknown_missing")
    paths.sort(reverse=True, key=lambda item: (item[0], str(item[2])))
    for path_index, (_mtime, initial_size, path) in enumerate(paths):
        if time.monotonic() - started >= SCAN_SECONDS:
            return _unknown("unknown_timeout")
        truncated = initial_size > MAX_FILE_BYTES
        try:
            with path.open("rb") as lines:
                if truncated:
                    lines.seek(initial_size - MAX_FILE_BYTES)
                    scanned_bytes += len(lines.readline(MAX_LINE_BYTES))
                ordinal = 0
                while lines.tell() < initial_size:
                    if time.monotonic() - started >= SCAN_SECONDS:
                        return _unknown("unknown_timeout")
                    line = lines.readline(min(MAX_LINE_BYTES + 1, initial_size - lines.tell()))
                    if not line:
                        break
                    ordinal += 1
                    scanned_bytes += len(line)
                    if scanned_bytes > MAX_SCAN_BYTES:
                        return _unknown("unknown_scan_incomplete")
                    if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                        has_quota = b'"token_count"' in line or b'"rate_limits"' in line
                        while line and not line.endswith(b"\n") and lines.tell() < initial_size:
                            line = lines.readline(min(
                                MAX_LINE_BYTES + 1, initial_size - lines.tell()))
                            scanned_bytes += len(line)
                            has_quota |= b'"token_count"' in line or b'"rate_limits"' in line
                            if (scanned_bytes > MAX_SCAN_BYTES
                                    or time.monotonic() - started >= SCAN_SECONDS):
                                return _unknown("unknown_scan_incomplete")
                        if has_quota:
                            unplaced_quota_corrupt = True
                        continue
                    if not line.strip():
                        continue
                    quota_line = b'"token_count"' in line or b'"rate_limits"' in line
                    if not quota_line and len(line) > SMALL_JSON_BYTES:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        corrupt = True
                        if quota_line:
                            stamp = re.search(br'"timestamp"\s*:\s*"([^"]+)"', line)
                            bad_at = _utc(stamp.group(1).decode("ascii", "ignore")) if stamp else None
                            if bad_at and (corrupt_at is None or bad_at > corrupt_at):
                                corrupt_at = bad_at
                            elif not bad_at:
                                unplaced_quota_corrupt = True
                        continue
                    if not isinstance(entry, dict) or entry.get("type") != "event_msg":
                        continue
                    observed_at = _utc(entry.get("timestamp"))
                    payload = entry.get("payload")
                    if (isinstance(payload, dict) and payload.get("type") == "token_count"
                            and isinstance(payload.get("rate_limits"), dict) and not observed_at):
                        corrupt = True
                        unplaced_quota_corrupt = True
                        continue
                    try:
                        record = _weekly(payload, observed_at) if observed_at else None
                    except ValueError:
                        corrupt = True
                        if observed_at and (corrupt_at is None or observed_at > corrupt_at):
                            corrupt_at = observed_at
                        continue
                    if record is None:
                        continue
                    key = (observed_at, str(path), ordinal)
                    if latest is None or key >= latest[:3]:
                        latest = (observed_at, str(path), ordinal, record)
                if path.stat().st_size < initial_size:
                    incomplete = True
                if truncated:
                    incomplete = True
        except OSError:
            corrupt = True
            incomplete = True
        # mtime файла не предшествует записанному в него событию. После обработки
        # всех файлов с mtime >= factual timestamp остаток не может его опередить.
        if (latest and latest[0].timestamp() <= _mtime and path_index + 1 < len(paths)
                and paths[path_index + 1][0] < latest[0].timestamp()):
            break
    if incomplete or unplaced_quota_corrupt:
        return _unknown("unknown_scan_incomplete")
    if latest is None:
        return _unknown("unknown_corrupt" if corrupt else "unknown_missing")
    # Scan может длиться дольше секунды: event, записанный во время scan, не future.
    now += timedelta(seconds=max(0.0, time.monotonic() - started))
    observed_at, _, _, record = latest
    if corrupt_at is not None and corrupt_at >= observed_at:
        return _unknown("unknown_corrupt")
    if observed_at > now:
        return _unknown("unknown_future")
    if now - observed_at > MAX_AGE:
        return _unknown("unknown_stale")
    if record["reset_at"] <= int(now.timestamp()):
        return _unknown("unknown_expired")
    return record


def reset_due(previous_record: dict | None, current_record: dict | None, now: datetime) -> bool:
    """True лишь когда новая factual weekly-граница подтвердила прошедший reset.

    Отсутствующий previous — bootstrap: caller сохраняет baseline, reset не заявляет.
    """
    if now.tzinfo is None:
        raise ValueError("now должен быть timezone-aware")
    if not isinstance(previous_record, dict) or not isinstance(current_record, dict):
        return False
    if previous_record.get("status") != "fresh" or current_record.get("status") != "fresh":
        return False
    if (not isinstance(previous_record.get("provider"), str)
            or previous_record.get("provider") != current_record.get("provider")
            or not isinstance(previous_record.get("limit_id"), str)
            or previous_record.get("limit_id") != current_record.get("limit_id")):
        return False
    before, after = previous_record.get("reset_at"), current_record.get("reset_at")
    if type(before) is not int or type(after) is not int:
        return False
    observed = _utc(current_record.get("observed_at"))
    now = now.astimezone(UTC)
    if not observed or observed > now or now - observed > MAX_AGE:
        return False
    return (now.timestamp() >= before and after > before and after > now.timestamp()
            and observed.timestamp() >= before)


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    result = _utc(value)
    if result is None:
        raise ValueError("--now: нужен RFC3339 UTC timestamp")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Фактическое weekly-окно Codex подписки")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--sessions-root", type=Path,
                        default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--now", help="RFC3339 timestamp; только для проверок")
    args = parser.parse_args()
    try:
        record = observe_codex(args.sessions_root, _now(args.now))
    except ValueError as exc:
        print(f"subscription_reset: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
