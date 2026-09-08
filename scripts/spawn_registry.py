#!/usr/bin/env python3
"""Дисковый реестр поручений субагентам.

    python3 scripts/spawn_registry.py CASE --assign WHO --task TEXT
    python3 scripts/spawn_registry.py CASE --finish ID --result TEXT
    python3 scripts/spawn_registry.py CASE --show
    python3 scripts/spawn_registry.py CASE --json
    python3 scripts/spawn_registry.py --selftest

Возраст открытой записи только делит ее на «идет» и «молчит».
Закрывает поручение только явная запись --finish.
"""

import argparse
import datetime
import fcntl
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import case_paths

STATE_NAME = "spawns.json"
SILENCE_SECONDS = 20 * 60
SILENCE_MINUTES = SILENCE_SECONDS // 60
VERSION = 1
FIELDS = ("id", "assignee", "assignment", "assigned_at", "returned", "returned_at")


class RegistryError(ValueError):
    """Ошибка формата или перехода реестра."""


def state_path(case) -> Path:
    """Реестр лежит рядом с прочим машинным состоянием дела."""
    return case_paths.context(Path(case)) / STATE_NAME


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{label} обязательно")
    return " ".join(value.split())


def _number(value, label: str) -> float:
    valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not valid or not math.isfinite(value):
        raise RegistryError(f"{label} должно быть конечным числом")
    return float(value)


def _moment(value=None) -> float:
    return _number(time.time() if value is None else value, "время")


def _record(value) -> dict:
    if not isinstance(value, dict):
        raise RegistryError("запись реестра не объект")
    missing = [field for field in FIELDS if field not in value]
    if missing:
        raise RegistryError("в записи нет полей: " + ", ".join(missing))
    record = {
        "id": _text(value["id"], "id"),
        "assignee": _text(value["assignee"], "assignee"),
        "assignment": _text(value["assignment"], "assignment"),
        "assigned_at": _number(value["assigned_at"], "assigned_at"),
        "returned": value["returned"],
        "returned_at": value["returned_at"],
    }
    if record["returned"] is not None and not isinstance(record["returned"], str):
        raise RegistryError("returned должно быть строкой или null")
    if (record["returned"] is None) != (record["returned_at"] is None):
        raise RegistryError("returned и returned_at заполняются вместе")
    if record["returned_at"] is not None:
        record["returned_at"] = _number(record["returned_at"], "returned_at")
        if record["returned_at"] < record["assigned_at"]:
            raise RegistryError("возврат не может быть раньше поручения")
    return record


def _load(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RegistryError(f"реестр не читается: {exc}") from exc
    if not raw.strip():
        return []
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"реестр поврежден: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != VERSION:
        raise RegistryError("неизвестный формат реестра")
    if not isinstance(state.get("records"), list):
        raise RegistryError("records не список")
    records = [_record(value) for value in state["records"]]
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise RegistryError("в реестре повторяются id")
    return records


def records(case) -> list[dict]:
    """Последнее дисковое состояние каждого поручения."""
    case = Path(case)
    if not case.is_dir():
        raise RegistryError(f"каталог дела не найден: {case}")
    return _load(state_path(case))


def _save(path: Path, values: list[dict]) -> None:
    # ponytail: один JSON-снимок для локального диска; потолок —
    # многохостовая запись, путь апгрейда — SQLite.
    temp = path.with_suffix(".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": VERSION, "records": values}, stream,
                      ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError as exc:
        raise RegistryError(f"реестр не записан: {exc}") from exc


def _update(case, change):
    case = Path(case)
    if not case.is_dir():
        raise RegistryError(f"каталог дела не найден: {case}")
    path = state_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = _load(path)
            result = change(current)
            _save(path, current)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def assign(case, assignee: str, assignment: str, now=None, record_id=None) -> str:
    """Записать поручение до запуска субагента и вернуть ID."""
    record = {
        "id": _text(record_id, "id") if record_id is not None else uuid.uuid4().hex,
        "assignee": _text(assignee, "assignee"),
        "assignment": _text(assignment, "assignment"),
        "assigned_at": _moment(now),
        "returned": None,
        "returned_at": None,
    }

    def change(current):
        if any(item["id"] == record["id"] for item in current):
            raise RegistryError(f"id уже занят: {record['id']}")
        current.append(record)
        return record["id"]

    return _update(case, change)


def finish(case, record_id: str, result: str, now=None) -> dict:
    """Записать явно видимый возврат; пустая строка тоже терминальна."""
    record_id = _text(record_id, "id")
    if not isinstance(result, str):
        raise RegistryError("result должен быть строкой")
    returned_at = _moment(now)

    def change(current):
        found = next((record for record in current if record["id"] == record_id), None)
        if found is None:
            raise RegistryError(f"неизвестное поручение: {record_id}")
        if found["returned"] is not None:
            raise RegistryError(f"поручение уже закрыто: {record_id}")
        found.update({"returned": result, "returned_at": returned_at})
        _record(found)
        return dict(found)

    return _update(case, change)


def poll(case, now=None, threshold: float = SILENCE_SECONDS) -> dict:
    """Свежие открытые идут, старые молчат, но не закрываются."""
    now = _moment(now)
    threshold = _number(threshold, "порог")
    if threshold <= 0:
        raise RegistryError("порог должен быть больше нуля")
    all_records = records(case)
    pending, running, silent = [], [], []
    for record in all_records:
        if record["returned"] is not None:
            continue
        item = {**record, "age_seconds": max(0.0, now - record["assigned_at"])}
        pending.append(item)
        (silent if item["age_seconds"] > threshold else running).append(item)
    return {
        "assigned": len(all_records),
        "returned": sum(record["returned"] is not None for record in all_records),
        "pending": pending,
        "running": running,
        "silent": silent,
        "threshold_seconds": threshold,
        "records": all_records,
    }


def _summary(report: dict) -> str:
    minutes = report["threshold_seconds"] / 60
    limit = str(int(minutes)) if minutes.is_integer() else f"{minutes:g}"

    def count_and_names(items):
        names = ", ".join(item["assignee"] for item in items)
        return str(len(items)) + (f" ({names})" if names else "")

    line = (
        f"активные агенты: поручено {report['assigned']}, "
        f"вернулось {report['returned']}; "
        f"идут <= {limit} мин: {count_and_names(report['running'])}; "
        f"молчат > {limit} мин: {count_and_names(report['silent'])}"
    )
    if report["assigned"] == 0:
        line += "; реестр пуст (нет данных о поручениях)"
    return line


def summary(case, now=None, threshold: float = SILENCE_SECONDS) -> str:
    return _summary(poll(case, now=now, threshold=threshold))


def _date(timestamp: float) -> str:
    value = datetime.datetime.fromtimestamp(timestamp).astimezone()
    return value.strftime("%d.%m.%Y %H:%M:%S %z")


def render(case, now=None, threshold: float = SILENCE_SECONDS) -> str:
    report = poll(case, now=now, threshold=threshold)
    lines = [_summary(report)]
    for record in report["records"]:
        returned = "не вернулось"
        if record["returned"] is not None:
            returned = json.dumps(record["returned"], ensure_ascii=False)
            returned += f" ({_date(record['returned_at'])})"
        lines.append(
            f"{record['id']} · кому поручено: {record['assignee']} · "
            f"что поручено: {record['assignment']} · "
            f"когда: {_date(record['assigned_at'])} · что вернулось: {returned}"
        )
    return "\n".join(lines)


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *map(str, args)],
        text=True, capture_output=True, check=True,
    )


def selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="spawn-registry-") as raw:
        case = Path(raw) / "cases" / "client" / "matter"
        case.mkdir(parents=True)
        assert poll(case)["assigned"] == 0

        started = _run(case, "--assign", "reader-1", "--task", "прочитать файл")
        record_id = started.stdout.strip()
        first = json.loads(_run(case, "--json").stdout)
        assert first["assigned"] == 1 and first["returned"] == 0
        assert first["running"][0]["assignee"] == "reader-1"

        # Брошенный temp не портит атомарный снимок.
        state_path(case).with_suffix(".tmp").write_text("{", encoding="utf-8")
        _run(case, "--finish", record_id, "--result", "ОТКАЗ: тест")
        closed = json.loads(_run(case, "--json").stdout)
        assert closed["assigned"] == 1 and closed["returned"] == 1
        assert not closed["pending"]
        record = closed["records"][0]
        assert {"assignee", "assigned_at", "returned"} <= record.keys()
        assert record["returned"] == "ОТКАЗ: тест"
    print("selftest пройден: реестр читается после перезапуска процесса")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", nargs="?", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--assign", "--spawn", dest="assignee", metavar="WHO")
    action.add_argument("--finish", "--return", dest="record_id", metavar="ID")
    action.add_argument("--show", "--status", "--poll", action="store_true")
    parser.add_argument("--task")
    parser.add_argument("--result")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--threshold", type=float, default=SILENCE_SECONDS,
                        help="порог молчания в секундах")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        selftest()
        return 0
    if args.case is None:
        parser.error("укажите CASE")
    try:
        if args.assignee is not None:
            if args.task is None:
                parser.error("--assign требует --task")
            print(assign(args.case, args.assignee, args.task))
        elif args.record_id is not None:
            if args.result is None:
                parser.error("--finish требует --result")
            finish(args.case, args.record_id, args.result)
            print(args.record_id)
        elif args.json:
            print(json.dumps(poll(args.case, threshold=args.threshold),
                             ensure_ascii=False, indent=2))
        else:
            print(render(args.case, threshold=args.threshold))
    except RegistryError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
