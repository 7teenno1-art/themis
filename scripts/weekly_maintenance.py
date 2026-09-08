#!/usr/bin/env python3
"""Недельное обслуживание Фемиды по фактическому сбросу подписки, без USD.

launchd вызывает --tick каждый час. Само обслуживание выполняется один раз на
подтвержденный цикл; --run-now явно запускает первоначальное обслуживание.
Ни чтение статуса, ни отсутствие данных подписки не считаются обновлением.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subscription_reset import observe_codex, reset_due

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.themiz.weekly-maintenance"
LEGACY_LABEL = "legal.corpus.update"
LEGACY_SCRIPT = str(Path.home() / "Проекты" / "themis" / "scripts" / "legal-corpus-monthly.sh")
LEGACY_ARGS = ("/bin/bash", LEGACY_SCRIPT)
SCHEDULER_PATH = os.pathsep.join((str(Path(sys.executable).parent), "/usr/local/bin",
                                "/opt/homebrew/bin", str(Path.home() / ".npm-global" / "bin"),
                                "/usr/bin", "/bin", "/usr/sbin", "/sbin"))
WEEK = timedelta(days=7)
RETRY = timedelta(hours=6)
MAX_ATTEMPTS = 3
PACKAGES = ("markitdown", "pypdf", "pypdfium2", "pillow", "python-docx",
            "openai-whisper", "pymupdf")
SUCCESS = {"current", "updated"}


def stamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


def read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: нужен JSON-объект")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            os.chmod(tmp, 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def maintenance_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "maintenance.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def stale(item: dict, now: datetime) -> bool:
    checked = parse_time(item.get("last_checked"))
    return (item.get("status") not in SUCCESS or checked is None or checked > now
            or now - checked > WEEK)


def store_result(previous: dict, result: dict, now: datetime) -> dict:
    """Ошибка не освежает старую успешную проверку или активацию."""
    record = {**previous, **result, "last_attempt": stamp(now)}
    for key in ("last_checked", "last_updated"):
        record[key] = previous.get(key)
    if result.get("status") in SUCCESS:
        record["last_checked"] = stamp(now)
    if result.get("status") == "updated":
        record["last_updated"] = stamp(now)
    return record


def latest_versions(state_dir: Path, installed: dict) -> tuple[dict, dict]:
    """Линкей получает PyPI metadata; выбираем stable для текущего Python."""
    from maintenance_fetch import fetch_public
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version
    versions, proofs = {}, {}
    for name in PACKAGES:
        # MuPDF остается только установленным fallback, не новой зависимостью.
        if name == "pymupdf" and not installed.get(name):
            continue
        response = fetch_public(f"https://pypi.org/pypi/{name}/json", state_dir)
        metadata = json.loads(response["body"])
        canonical = lambda value: re.sub(r"[-_.]+", "-", value).lower()
        if (not isinstance(metadata, dict) or not isinstance(metadata.get("info"), dict)
                or not isinstance(metadata["info"].get("name"), str)
                or canonical(metadata["info"]["name"]) != canonical(name)
                or not isinstance(metadata.get("releases"), dict)):
            raise ValueError(f"{name}: PyPI metadata относится к другому пакету или поврежден")
        candidates = []
        for value, files in metadata.get("releases", {}).items():
            try:
                version = Version(value)
            except InvalidVersion:
                continue
            if version.is_prerelease or version.is_devrelease:
                continue
            if not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", value):
                continue
            if any(not f.get("yanked") and f.get("packagetype") in {"bdist_wheel", "sdist"}
                   and SpecifierSet(f.get("requires_python") or "").contains(platform.python_version())
                   for f in files if isinstance(f, dict)):
                candidates.append((version, value))
        if not candidates:
            raise ValueError(f"{name}: не найдена совместимая stable-версия")
        latest = max(candidates)[1]
        if installed.get(name) and Version(latest) < Version(installed[name]):
            raise ValueError(f"{name}: источник предлагает понижение версии")
        versions[name] = latest
        proofs[name] = response["source_revision"]
    return versions, proofs


def perform(root: Path, directory: Path, previous: dict, now: datetime,
            approved: bool) -> list[dict]:
    from extraction_runtime import inventory, upgrade
    from maintenance_sources import CORPUS_EXTRACTOR_VERSION, refresh_sources, source_ids
    result = []
    installed = inventory(root)
    if stale(previous.get("python-stack", {}), now):
        item = {"id": "python-stack", "status": "blocked", "packages": installed["packages"],
                "python": installed["python"], "detail": "Нужны утвержденные PyPI/GitHub источники"}
        if approved:
            try:
                if installed.get("error"):
                    raise ValueError(f"инвентаризация runtime: {installed['error']}")
                versions, proofs = latest_versions(directory, installed["packages"])
                item = {**upgrade(root, versions), "id": "python-stack", "source_revisions": proofs}
            except Exception as exc:
                item = {"id": "python-stack", "status": "failed", "detail": f"{type(exc).__name__}: {exc}"}
        result.append(item)
    # Adapter сам различает локальную проверку и внешний refresh; корпус/навыки
    # не подменяются установкой Python-пакетов.
    due = {name for name in source_ids(root) if stale(previous.get(name, {}), now)
           or (name.startswith("skill-doc:")
               and previous.get(name, {}).get("provenance") != "project-local")
           or (name.startswith("legal-corpus:") and name != "legal-corpus:plenums"
               and previous.get(name, {}).get("extractor_version") != CORPUS_EXTRACTOR_VERSION)}
    # Публичный корпус имеет собственную очередь. Недельный цикл версий не
    # держит его загрузку внутри maintenance_lock и не управляет его повторами.
    import background_corpus
    if background_corpus.enabled(root):
        legal_due = {name for name in due if name.startswith("legal-corpus:")}
        detail = "Отдельная фоновая очередь; свежесть и причины отказов — background_corpus.py --status"
        try:
            background_corpus.enqueue_registered(root)
        except Exception as exc:
            detail = f"Фоновая очередь недоступна ({type(exc).__name__}); синхронная загрузка не запускалась"
        result.extend({"id": name, "status": "delegated", "detail": detail,
                       "managed_by": "background_corpus"} for name in sorted(legal_due))
        due -= legal_due
    if due:
        source_results = refresh_sources(root, directory, now, online=approved, due_ids=due)
        result.extend(source_results)
        returned = {item.get("id") for item in source_results}
        result.extend({"id": name, "status": "failed", "detail": "Адаптер пропустил результат компонента"}
                      for name in sorted(due - returned))
    for name, detail in (
        ("apple-vision", "Apple Vision поставляется с macOS. Обновление ОС требует отдельного решения; версия upstream не подтверждена."),
        ("ffmpeg", "Системный FFmpeg не изменяется. Для новой сборки нужна отдельная проверка источника, лицензии и совместимости."),
    ):
        if stale(previous.get(name, {}), now):
            result.append({"id": name, "status": "blocked", "detail": detail,
                           "inventory": installed.get("native", {})})
    return result


def retire_local_upstream_checks(state: dict, results: list[dict], now: datetime) -> None:
    """Archive an obsolete upstream placeholder only after verified local provenance."""
    for item in results:
        name = item.get("id", "")
        if (not name.startswith("skill-doc:") or item.get("status") != "current"
                or item.get("provenance") != "project-local"
                or not item.get("canonical_sha256") or not item.get("mirror_sha256")):
            continue
        old_id = "skill-upstream:" + name.split(":", 1)[1]
        if old_id in state["items"]:
            state.setdefault("retired_items", {})[old_id] = {
                "previous": state["items"].pop(old_id), "retired_at": stamp(now),
                "reason": "project-local canonical; external upstream check is not applicable",
                "evidence": item,
            }


def write_report(directory: Path, state: dict) -> None:
    lines = ["# Недельное обслуживание Фемиды", "", f"Последнее обслуживание: {state.get('status', 'not_started')}",
             f"Планировщик: {state.get('scheduler_status', 'unknown')}",
             "", "| Компонент | Результат | Последняя успешная проверка | Последнее обновление |",
             "|---|---|---|---|"]
    for name, item in state.get("items", {}).items():
        lines.append(f"| {name} | {item.get('status', 'unknown')} | {item.get('last_checked') or 'нет'} | "
                     f"{item.get('last_updated') or 'нет'} |")
    for name, item in state.get("items", {}).items():
        if item.get("detail"):
            lines.extend(["", f"{name}: {item['detail']}"])
    if state.get("retired_items"):
        lines += ["", f"Архив проверок, утративших применимость: {len(state['retired_items'])}. "
                  "Причина и прежний результат сохранены в state.json; это не подтверждение upstream."]
    background = state.get("background_corpus")
    if background:
        lines += ["", "## Независимая подкачка нормативного корпуса", "",
                  "Фемида читает рабочие копии без ожидания очереди. Этот раздел не даёт юридического допуска.",
                  "Состояния заданий: " + json.dumps(background.get("queue", {}), ensure_ascii=False),
                  "Подробности: `python3 scripts/background_corpus.py --status`."]
        if background.get("error"):
            lines.append("Состояние очереди не прочитано: " + background["error"])
    lines += ["", "USD не применяется. Квота подписки не вычисляется из токенов.",
              "Отсутствие свежего наблюдения reset не означает, что лимит сбросился."]
    (directory / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def tick(root: Path, sessions: Path, now: datetime, *, force: bool = False,
         observer=observe_codex, executor=perform) -> dict:
    directory = root / ".agent" / "maintenance"
    with maintenance_lock(directory):
        state = read_json(directory / "state.json", {"schema": 1, "items": {}})
        if (state.get("schema") != 1 or not isinstance(state.get("items"), dict)
                or any(not isinstance(item, dict) for item in state["items"].values())
                or not isinstance(state.get("retired_items", {}), dict)
                or type(state.get("attempts", 0)) is not int or state.get("attempts", 0) < 0):
            raise ValueError("повреждено состояние обслуживания; оно не перезаписано")
        policy = read_json(directory / "policy.json")
        observed_start = time.monotonic()
        current = observer(sessions, now)
        # A live session can append quota events while a full scan is running.
        # Compare them with the end of observation, not the pre-scan clock.
        now += timedelta(seconds=time.monotonic() - observed_start)
        state["observation"] = current
        state["last_poll"] = stamp(now)
        baseline = state.get("baseline")
        if reset_due(baseline, current, now):
            cycle = f"{current['provider']}:{current.get('limit_id')}:{current['reset_at']}"
            if cycle != state.get("completed_cycle") and cycle != state.get("pending_cycle"):
                state.update(pending_cycle=cycle, attempts=0, retry_after=None)
        if current.get("status") == "fresh":
            state["baseline"] = current
        if force and not state.get("pending_cycle"):
            state.update(pending_cycle=f"manual:{stamp(now)}", attempts=0, retry_after=None)
        pending = state.get("pending_cycle")
        retry_after = parse_time(state.get("retry_after"))
        used = current.get("used_percent")
        allowed = (current.get("status") == "fresh" and type(used) in (int, float)
                   and 0 <= used < 100)
        if not force and (not pending or not allowed):
            state["scheduler_status"] = "waiting_reset" if allowed else "unknown_or_exhausted_quota"
        elif not force and (state.get("attempts", 0) >= MAX_ATTEMPTS or (retry_after and now < retry_after)):
            state["scheduler_status"] = "retry_paused"
        else:
            # intent durable до работы: упавший процесс не превращается в новый reset.
            state.update(status="running", scheduler_status="running", attempts=state.get("attempts", 0) + 1)
            atomic_json(directory / "state.json", state)
            execution_start = time.monotonic()
            try:
                results = executor(root, directory, state["items"], now,
                                   policy.get("official_sources_approved") is True)
                now += timedelta(seconds=time.monotonic() - execution_start)
                state["last_run_finished"] = stamp(now)
                if not isinstance(results, list):
                    raise ValueError("исполнитель не вернул результаты компонентов")
                if not results and not state["items"]:
                    raise ValueError("пустой реестр не подтверждает актуальность")
                ids = set()
                for item in results:
                    if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                            or not item["id"] or not isinstance(item.get("status"), str)
                            or item["id"] in ids):
                        raise ValueError("поврежденный или повторный результат компонента")
                    ids.add(item["id"])
                for item in results:
                    item_id = item["id"]
                    state["items"][item_id] = store_result(state["items"].get(item_id, {}), item, now)
                retire_local_upstream_checks(state, results, now)
                failed = [k for k, v in state["items"].items() if stale(v, now)]
                state["status"] = "partial" if failed else "current"
                state["scheduler_status"] = "retry_scheduled" if failed else "waiting_reset"
                state.pop("error", None)
                if not failed:
                    state.update(completed_cycle=pending, pending_cycle=None, retry_after=None)
                else:
                    state["retry_after"] = stamp(now + RETRY)
            except Exception as exc:
                state.update(status="failed", error=f"{type(exc).__name__}: {exc}",
                             scheduler_status="retry_scheduled",
                             retry_after=stamp(now + RETRY))
        import background_corpus
        if background_corpus.enabled(root):
            try:
                report = background_corpus.status(root)
                state["background_corpus"] = {"queue": report.get("queue", {}),
                                               "observed_at": stamp(now)}
            except Exception as exc:
                state["background_corpus"] = {"error": type(exc).__name__}
        atomic_json(directory / "state.json", state)
        write_report(directory, state)
        return state


def _canonical_root(root: Path) -> Path:
    root = root.resolve()
    if root != ROOT.resolve():
        raise ValueError("расписание разрешено только для канонического корня Femida")
    return root


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _legacy_identity(path: Path) -> tuple[str | None, tuple[str, ...] | None]:
    """Читает только label/argv; старый XML на машине не проходит строгий plist parser."""
    raw = path.read_bytes()
    try:
        value = plistlib.loads(raw)
        args = value.get("ProgramArguments") if isinstance(value, dict) else None
        return value.get("Label"), tuple(args) if isinstance(args, list) else None
    except Exception:
        text = raw.decode("utf-8", "replace")
        label = re.search(r"<key>Label</key>\s*<string>([^<]*)</string>", text, re.S)
        argv = re.search(r"<key>ProgramArguments</key>\s*<array>(.*?)</array>", text, re.S)
        args = tuple(re.findall(r"<string>([^<]*)</string>", argv.group(1))) if argv else None
        return (label.group(1) if label else None), args


def _legacy_conflict(agents: Path) -> str | None:
    legacy = agents / f"{LEGACY_LABEL}.plist"
    if not legacy.exists():
        return None
    try:
        identity = _legacy_identity(legacy)
    except Exception:
        identity = (None, None)
    if identity == (LEGACY_LABEL, LEGACY_ARGS):
        return ("legacy legal.corpus.update.plist содержит устаревший root "
                f"{Path(LEGACY_SCRIPT).parent.parent}; решение владельца нужно до установки; файл не изменен")
    return "legacy legal.corpus.update.plist существует; решение владельца нужно до установки; файл не изменен"


def _payload(root: Path) -> dict:
    directory = root / ".agent" / "maintenance"
    command = [sys.executable, str(root / "scripts" / "weekly_maintenance.py"), "--tick", "--root", str(root)]
    return {"Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(root),
            "StartInterval": 3600, "RunAtLoad": True,
            "EnvironmentVariables": {"PATH": SCHEDULER_PATH},
            "StandardOutPath": str(directory / "scheduler.log"),
            "StandardErrorPath": str(directory / "scheduler.err.log")}


def migrate_legacy(root: Path) -> dict:
    """Явная, обратимая миграция единственного подтвержденного legacy LaunchAgent."""
    if sys.platform != "darwin":
        raise ValueError("автомиграция расписания доступна только на macOS")
    root = _canonical_root(root)
    agents = _agents_dir()
    legacy = agents / f"{LEGACY_LABEL}.plist"
    if not legacy.exists():
        return {"status": "blocked", "reason": "legacy legal.corpus.update.plist не найден"}
    try:
        exact = _legacy_identity(legacy) == (LEGACY_LABEL, LEGACY_ARGS)
    except Exception:
        exact = False
    if not exact:
        return {"status": "blocked", "reason": "legacy payload не совпадает с подтвержденным; файл не изменен"}
    directory = root / ".agent" / "maintenance"
    backup = directory / f"{LEGACY_LABEL}.plist.backup"
    if backup.exists():
        return {"status": "blocked", "reason": "backup legacy уже существует; файл не изменен"}
    domain = f"gui/{os.getuid()}"
    directory.mkdir(parents=True, exist_ok=True)
    was_loaded = False
    try:
        active = subprocess.run(["launchctl", "print", f"{domain}/{LEGACY_LABEL}"],
                                capture_output=True, timeout=10)
        was_loaded = active.returncode == 0
        if was_loaded:
            subprocess.run(["launchctl", "bootout", f"{domain}/{LEGACY_LABEL}"], check=True,
                           capture_output=True, timeout=20)
        legacy.replace(backup)
    except Exception:
        if was_loaded and legacy.exists():
            subprocess.run(["launchctl", "bootstrap", domain, str(legacy)], check=False,
                           capture_output=True, timeout=20)
        raise
    return {"status": "migrated", "backup": backup, "legacy": legacy,
            "was_loaded": was_loaded, "domain": domain}


def _restore_legacy(migration: dict) -> None:
    backup, legacy = migration["backup"], migration["legacy"]
    if backup.exists() and not legacy.exists():
        backup.replace(legacy)
    if migration.get("was_loaded"):
        subprocess.run(["launchctl", "bootstrap", migration["domain"], str(legacy)], check=False,
                       capture_output=True, timeout=20)


def install(root: Path, *, replace_legacy: bool = False) -> dict:
    """Ставит только канонический hourly LaunchAgent; legacy никогда не трогает."""
    if sys.platform != "darwin":
        raise ValueError("автоустановка расписания доступна только на macOS")
    root = _canonical_root(root)
    agents = _agents_dir()
    conflict = _legacy_conflict(agents)
    if conflict:
        if not replace_legacy:
            return {"status": "blocked", "reason": conflict}
        migration = migrate_legacy(root)
        if migration["status"] == "blocked":
            return migration
    else:
        migration = None
    created = False
    domain = f"gui/{os.getuid()}"
    bootstrap_attempted = False
    target = agents / f"{LABEL}.plist"
    try:
        directory = root / ".agent" / "maintenance"
        directory.mkdir(parents=True, exist_ok=True)
        agents.mkdir(parents=True, exist_ok=True)
        payload = _payload(root)
        if target.exists():
            old = plistlib.loads(target.read_bytes())
            if old != payload:
                raise ValueError("задание с этой меткой уже отличается; существующее не перезаписано")
        else:
            target.write_bytes(plistlib.dumps(payload))
            created = True
        active = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True, timeout=10)
        if active.returncode:
            bootstrap_attempted = True
            subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True,
                           capture_output=True, timeout=20)
        subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], check=True,
                       capture_output=True, timeout=10)
    except Exception:
        if bootstrap_attempted:
            subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False,
                           capture_output=True, timeout=20)
        if created:
            target.unlink(missing_ok=True)
        if migration:
            _restore_legacy(migration)
        raise
    return {"status": "installed", "label": LABEL, "poll_seconds": 3600, "root": str(root),
            "legacy_migrated": bool(migration)}


def uninstall(root: Path) -> dict:
    """Удаляет только свой canonical plist, поэтому job не возродится при login."""
    if sys.platform != "darwin":
        raise ValueError("автоудаление расписания доступно только на macOS")
    root = _canonical_root(root)
    target = _agents_dir() / f"{LABEL}.plist"
    existed = target.exists()
    if existed and plistlib.loads(target.read_bytes()) != _payload(root):
        raise ValueError("задание с этой меткой уже отличается; существующее не удалено")
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False,
                   capture_output=True, timeout=20)
    target.unlink(missing_ok=True)
    return {"status": "uninstalled", "label": LABEL, "plist_removed": existed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".codex" / "sessions")
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ("status", "tick", "run-now", "install", "uninstall", "approve-sources"):
        modes.add_argument(f"--{mode}", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--replace-legacy", action="store_true",
                        help="заменить только известное старое расписание Фемиды с резервной копией")
    args = parser.parse_args()
    root = args.root.resolve()
    directory = root / ".agent" / "maintenance"
    try:
        if args.status:
            result = read_json(directory / "state.json", {"status": "not_started"})
        elif args.approve_sources:
            result = {"official_sources_approved": True, "approved_at": stamp(datetime.now(UTC)),
                      "scope": "Femida only; public PyPI/GitHub; no case data, OS updates or VPS"}
            atomic_json(directory / "policy.json", result)
        elif args.install:
            result = install(root, replace_legacy=args.replace_legacy)
        elif args.uninstall:
            result = uninstall(root)
        else:
            result = tick(root, args.sessions_root, datetime.now(UTC), force=args.run_now)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if result.get("status") in {"partial", "failed", "blocked"} else 0
    except BlockingIOError:
        print(json.dumps({"status": "already_running"}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
