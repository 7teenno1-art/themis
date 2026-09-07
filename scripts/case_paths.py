#!/usr/bin/env python3
"""case_paths.py — контракт раскладки дела. Этап 3 плана FINAL-PLAN-2026-08-18.

Единственный источник правды о том, где что лежит внутри дела. До этого модуля
раскладка жила строковыми литералами в 46 файлах (203 вхождения, замер 19.08.2026):
любая правка требовала найти их все, а пропущенный литерал ломался молча.

## Два слоя (решение владельца)

Дело смотрит на человека одной стороной, а на агента — другой.

    cases/<клиент>/<дело>/
      _case.md            карточка дела — человек читает
      00_intake/          исходники доверителя, неприкосновенны
      02_hearings/        события и ПОДАННЫЕ документы
      GOTOVO/             готовые документы — то, за чем человек приходит
      .agent/             рабочая кухня, в Finder не видна
        context/            карта, практика, позиция, рабочие файлы роя
        drafts/             черновики .md + _baselines/ (снимки ДО правок доверителя)
        archive/            отработанное

`GOTOVO` латиницей: кириллица в имени папки нарушает `AGENTS.md` (латиница, цифры,
дефисы) и ломает пути на чужой файловой системе. «ГОТОВО» — подпись в панели.

`.agent/` скрыта точкой: человек не должен видеть кухню. Обратная сторона — она
невидима и в Finder, и одним `git add -A` уезжает в публичный репозиторий; это
закрывается `.gitignore`, а не памятью.

## Жизненный цикл документа

Документ живет в `.agent/drafts/<имя>.md` весь цикл правок. `.docx` собирается
ОДИН раз — после вердикта Кони «ГОТОВ К ПОДАЧЕ» — и кладется в `GOTOVO/`.
Раньше `.docx` пересобирался на каждом раунде, и папка готовых наполнялась
недоделанным.
"""
import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

# ── Контракт ─────────────────────────────────────────────────────────────────
AGENT_DIR = ".agent"
INTAKE = "00_intake"
HEARINGS = "02_hearings"
READY = "GOTOVO"
READY_LABEL = "ГОТОВО"          # подпись в панели; в путях — только латиница
CONTEXT = f"{AGENT_DIR}/context"
DRAFTS = f"{AGENT_DIR}/drafts"
ARCHIVE = f"{AGENT_DIR}/archive"
BASELINES = f"{DRAFTS}/_baselines"
WORKING = "_working"
DRAFT_NAME = "{document}.md"
READY_MD_NAME = "{document}.md"
READY_DOCX_NAME = "{document}.docx"
MAX_REVIEW_ROUNDS = 2
REVIEW_STOP_ROUND = MAX_REVIEW_ROUNDS + 1
# Журнал вердиктов Кони. Имя — ОДНО на систему; лежит прямо в drafts, НЕ в _working:
# сторож (claude_guard) освобождает _working/_baselines от гейта протокола, и журнал
# внутри них — слепое пятно (D03, 01.09.2026: дописанная строка открыла сборку .docx).
VERDICTS_NAME = "verdicts.jsonl"

# Что человеку видно в корне дела. Все прочее — кухня.
HUMAN_VISIBLE = ("_case.md", INTAKE, HEARINGS, READY)

# Старое → новое. Порядок важен: длинные ключи раньше коротких, иначе
# «03_drafts/_baselines» починится как «.agent/drafts» + хвост от старого имени.
LEGACY = (
    ("01_context", CONTEXT),
    ("03_drafts", DRAFTS),
    ("04_archive", ARCHIVE),
)


def _p(case):
    return case if isinstance(case, Path) else Path(case)


def intake(case):
    return _p(case) / INTAKE


def hearings(case):
    return _p(case) / HEARINGS


def ready(case):
    """Папка готовых документов — единственное, за чем человек приходит в дело."""
    return _p(case) / READY


def _contract_block(*paths, **values):
    sources = [{"path": _p(path), "exists": _p(path).is_file()} for path in paths]
    return {"sources": sources, "exists": all(s["exists"] for s in sources), **values}


def document_contract(case):
    """Файлы результата и шесть блоков требований с наличием источников."""
    case = _p(case)
    root = Path(__file__).resolve().parent.parent
    council = context(case) / "_council/council_verdict.md"
    legacy_council = context(case) / "_practice/council_verdict.md"
    if not council.is_file() and legacy_council.is_file():
        council = legacy_council
    return {
        "draft_md": drafts(case) / DRAFT_NAME,
        "ready_md": ready(case) / READY_MD_NAME,
        "ready_docx": ready(case) / READY_DOCX_NAME,
        "review_rounds": MAX_REVIEW_ROUNDS,
        "blocks": {
            "content": _contract_block(brief(case), positions(case)),
            "sources": _contract_block(practice(case), council),
            "canon": _contract_block(working(case) / "canon_formulirovki.md"),
            "form": _contract_block(
                root / "scripts/create_docx.py",
                root / ".claude/skills/doc-drafter/SKILL.md"),
            "admission": _contract_block(root / "scripts/verdict.py"),
            "home": _contract_block(
                Path(__file__).resolve(),
                ready_dir=ready(case), ready_name=READY_DOCX_NAME),
        },
    }


def agent_root(case):
    return _p(case) / AGENT_DIR


def context(case):
    return _p(case) / AGENT_DIR / "context"


def drafts(case):
    return _p(case) / AGENT_DIR / "drafts"


def archive(case):
    return _p(case) / AGENT_DIR / "archive"


def baselines(case):
    """Неизменяемые снимки выданного — база сравнения для разбора правок доверителя."""
    return drafts(case) / "_baselines"


def working(case):
    return context(case) / WORKING


def knowledge_map(case):
    return context(case) / "knowledge-map.md"


def practice(case):
    return context(case) / "practice.md"


def positions(case):
    return context(case) / "positions.md"


def brief(case):
    return working(case) / "brief.md"


def review_log(case):
    return drafts(case) / WORKING / "review_log.md"


def verdicts(case):
    """Журнал вердиктов — прямо в drafts, ВНЕ освобожденного сторожем _working (D03)."""
    return drafts(case) / VERDICTS_NAME


# ── Межсессионный лок дела (REQ-10) ──────────────────────────────────────────
CASE_LOCK_NAME = ".lock"
CASE_LOCK_STALE_SECONDS = 6 * 60 * 60
# ponytail: повторно выданный PID может держать дело только до TTL; если это
# проявится, добавить время старта процесса.


def case_lock_path(case):
    return context(case) / CASE_LOCK_NAME


def _case_lock_process_alive(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OverflowError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _case_lock_result(state, owner=None, reason="", ok=True):
    owner = dict(owner) if isinstance(owner, dict) else {}
    return {
        "ok": ok,
        "locked": state in {"live", "owned", "acquired", "locked", "invalid", "busy"},
        "state": state,
        "session": owner.get("session", ""),
        "pid": owner.get("pid"),
        "timestamp": owner.get("timestamp"),
        "reason": reason,
    }


def _case_lock_args(case, now, max_age):
    case = _p(case)
    path = case_lock_path(case)
    if not case.is_dir():
        raise ValueError(f"каталог дела не найден: {case}")
    if not path.parent.is_dir():
        raise ValueError(f"каталог состояния дела не найден: {path.parent}")
    if isinstance(now, bool) or isinstance(max_age, bool):
        raise ValueError("now и max_age должны быть числами")
    now = time.time() if now is None else float(now)
    max_age = float(max_age)
    if not math.isfinite(now) or not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("now и max_age должны быть конечными, max_age > 0")
    return path, now, max_age


@contextlib.contextmanager
def _case_lock_mutex(path):
    """Один mutex без второго файла: flock на существующем context-каталоге."""
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _case_lock_invalid(path, mtime, now, max_age, reason):
    if now - mtime >= max_age:
        path.unlink(missing_ok=True)
        return _case_lock_result(
            "stale_removed", reason=f"снят протухший лок: {reason}")
    return _case_lock_result("invalid", reason=reason, ok=False)


def _case_lock_inspect(path, now, max_age):
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return _case_lock_result("free")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return _case_lock_invalid(
            path, mtime, now, max_age,
            f"лок поврежден: {type(exc).__name__}")

    pid = record.get("pid") if isinstance(record, dict) else None
    timestamp = record.get("timestamp") if isinstance(record, dict) else None
    session = record.get("session") if isinstance(record, dict) else None
    try:
        timestamp = float(timestamp) if type(timestamp) in (int, float) else math.nan
    except OverflowError:
        timestamp = math.nan
    valid = (
        isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
        and math.isfinite(timestamp)
        and isinstance(session, str) and bool(session.strip())
    )
    if not valid:
        return _case_lock_invalid(
            path, mtime, now, max_age,
            "лок поврежден: обязательные поля неверны")

    owner = {"pid": pid, "timestamp": timestamp, "session": session.strip()}
    stale = []
    if not _case_lock_process_alive(pid):
        stale.append(f"процесс {pid} не жив")
    if now - owner["timestamp"] >= max_age:
        stale.append(f"отметка старше порога {int(max_age)} с")
    if stale:
        path.unlink()
        return _case_lock_result(
            "stale_removed", owner,
            reason="снят протухший лок: " + "; ".join(stale))
    return _case_lock_result("live", owner)


def _case_lock_write_new(path, owner):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(owner, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def case_lock_status(case, now=None, max_age=CASE_LOCK_STALE_SECONDS):
    path, now, max_age = _case_lock_args(case, now, max_age)
    with _case_lock_mutex(path):
        return _case_lock_inspect(path, now, max_age)


def case_lock_acquire(case, session, pid=None, now=None,
                      max_age=CASE_LOCK_STALE_SECONDS):
    session = session.strip() if isinstance(session, str) else ""
    pid = os.getpid() if pid is None else pid
    if not session:
        raise ValueError("имя сессии обязательно")
    if not _case_lock_process_alive(pid):
        raise ValueError(f"процесс владельца не жив: {pid}")
    path, now, max_age = _case_lock_args(case, now, max_age)
    owner = {"pid": pid, "timestamp": now, "session": session}
    with _case_lock_mutex(path):
        current = _case_lock_inspect(path, now, max_age)
        removed_reason = current["reason"] if current["state"] == "stale_removed" else ""
        if current["locked"]:
            if not current["ok"]:
                return current
            if current["session"] != session:
                reason = f"дело занято: сессия {current['session']} (pid {current['pid']})"
                return _case_lock_result(
                    "locked", current, reason, ok=False)
            owner["timestamp"] = math.nextafter(
                max(now, current["timestamp"]), math.inf)
            path.write_text(
                json.dumps(owner, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            return _case_lock_result("owned", owner)
        try:
            _case_lock_write_new(path, owner)
        except FileExistsError:
            return _case_lock_result(
                "busy", reason="лок меняется другой сессией", ok=False)
        return _case_lock_result("acquired", owner, removed_reason)


def case_lock_release(case, session, pid=None, now=None,
                      max_age=CASE_LOCK_STALE_SECONDS, timestamp=None):
    session = session.strip() if isinstance(session, str) else ""
    if not session:
        raise ValueError("имя сессии обязательно")
    if pid is not None and (
            isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise ValueError("pid должен быть положительным целым")
    if timestamp is not None and (
            type(timestamp) not in (int, float) or not math.isfinite(timestamp)):
        raise ValueError("timestamp должен быть конечным числом")
    path, now, max_age = _case_lock_args(case, now, max_age)
    with _case_lock_mutex(path):
        current = _case_lock_inspect(path, now, max_age)
        if not current["locked"]:
            return current
        if not current["ok"]:
            return current
        if (current["session"] != session
                or (pid is not None and current["pid"] != pid)
                or (timestamp is not None and current["timestamp"] != timestamp)):
            reason = f"чужой лок: сессия {current['session']} (pid {current['pid']})"
            return _case_lock_result(
                "locked", current, reason, ok=False)
        path.unlink()
        return _case_lock_result("released", current)


# ── Машинное состояние прогона (M01) ──────────────────────────────────────────
# ЕДИНСТВЕННЫЙ адрес файла прогона. Сторож (claude_guard) читает его, проводник
# (themiz-pipeline) и владелец пишут через CLI ниже — второго адреса не заводить.
# Лежит в .agent/context/: сторож освобождает эту папку от гейта протокола (кроме
# практики/позиции), человеку кухня не видна. Ключи: guide (проводник запущен),
# preflight_code (последний код preflight_search), preflight_override (решение
# владельца работать при упавших каналах).
RUN_STATE_NAME = "run.json"


def run_state(case):
    return context(case) / RUN_STATE_NAME


def run_read(case):
    """Состояние прогона как dict; отсутствует/битый — пустой dict (fail-open чтения)."""
    try:
        data = json.loads(run_state(case).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def run_write(case, **updates):
    """Аддитивно слить updates в файл прогона. Возвращает новое состояние."""
    st = run_read(case)
    st.update(updates)
    p = run_state(case)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return st


def verdicts_legacy(case):
    """Старый адрес журнала (внутри _working). Только чтение, как запасной, с
    предупреждением: живые дела не переписываем — формат меняем аддитивно."""
    return drafts(case) / WORKING / VERDICTS_NAME


def modernize(text):
    """Старые пути в тексте → новые. Для промптов и разовой правки кода."""
    for old, new in LEGACY:
        text = text.replace(old, new)
    return text


# ── Переезд ──────────────────────────────────────────────────────────────────

def nfc(name):
    """Имя в NFC.

    macOS хранит имена файлов в NFD (разложенной) форме, Linux и git — в NFC.
    Непереведенное имя на сервере превращается в другой путь, и дело просто
    не находится. На диске уже есть дело с турецкой «ı» — на нем это видно.
    """
    return unicodedata.normalize("NFC", name)


def collisions(names):
    """Имена, схлопывающиеся в одно после нормализации. Переезжать с ними нельзя:
    второе дело затрет первое молча."""
    seen, bad = {}, {}
    for n in names:
        key = nfc(n)
        if key in seen and seen[key] != n:
            bad.setdefault(key, {seen[key]}).add(n)
        seen.setdefault(key, n)
    return {k: sorted(v) for k, v in bad.items()}


def migration_moves(case):
    """Что и куда переезжает в одном деле: [(источник, назначение)].

    Пусто, если дело уже в новой раскладке. `00_intake` и `02_hearings` не
    фигурируют вовсе — они неприкосновенны.
    """
    case = _p(case)
    moves = []
    for old, new in LEGACY:
        src = case / old
        if src.is_dir():
            moves.append((src, case / new))
    return moves


def is_migrated(case):
    case = _p(case)
    return agent_root(case).is_dir() and not any(
        (case / old).is_dir() for old, _ in LEGACY)


CONTRACT_DOCS = (
    "AGENTS.md",
    ".claude/CLAUDE.md",
    ".claude/commands/draft.md",
    ".claude/commands/finalize.md",
    ".claude/commands/new-case.md",
    ".claude/agents/doc-drafter.md",
    ".claude/agents/doc-reviewer.md",
    ".claude/skills/doc-drafter/SKILL.md",
    ".claude/skills/themiz-setup/SKILL.md",
)
REVIEW_CONTRACT_DOCS = (
    ".claude/commands/draft.md",
    ".claude/agents/doc-reviewer.md",
    ".claude/skills/doc-drafter/SKILL.md",
)
ROUND_WORDS = {
    "один": 1, "одного": 1, "два": 2, "двух": 2, "три": 3, "трех": 3,
    "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5,
}
ROUND_VALUE_RE = r"\d+|один|одного|два|двух|три|трех|четыре|четырех|пять|пяти"
ROUND_LIMIT_RE = re.compile(
    rf"(?:(?:до|после|не более|лимит\s*[-—:]?)\s*"
    rf"(?P<leading>{ROUND_VALUE_RE})\s+(?:раунд\w*|круг\w*)|"
    rf"(?P<trailing>{ROUND_VALUE_RE})\s+(?:раунд\w*|круг\w*)\s+"
    rf"(?:без одобрения|максимум))",
    re.IGNORECASE,
)
DIRECT_DRAFT_DOCX_RE = re.compile(
    r"\.agent/drafts/(?!_baselines/|_working/)[^`\s\"')]+\.docx",
    re.IGNORECASE,
)
DOCX_IN_DRAFTS_RE = re.compile(
    r"`?\.docx`?\s+(?:лежит|живет|хранится|сохраняется|в|→|->)"
    r"[^\n]{0,40}`?\.agent/drafts/",
    re.IGNORECASE,
)
VERSIONED_DRAFT_RE = re.compile(
    r"\.agent/drafts/[^`\s\"')]*_v\d+\.(?:md|docx)", re.IGNORECASE)
WRONG_HOME_RE = re.compile(
    r"(?:готов\w*|итог\w*|на выходе|свежайш\w*|за результат\w*)"
    r"[^\n]{0,120}\.agent/drafts|"
    r"\.agent/drafts[^\n]{0,120}(?:готов\w*|итог\w*|на выходе|за результат\w*)",
    re.IGNORECASE,
)
READY_CLAIM_RE = re.compile(
    r"(?:готов(?:ый|ого|ые|ых)\s+(?:`?\.docx`?|документ\w*|файл\w*)|"
    r"дом\s+готов\w*|на выходе[^\n]{0,40}\.docx|свежайш\w*[^\n]{0,30}\.docx|"
    r"за результат\w*)",
    re.IGNORECASE,
)
HOME_AFTER_CLAIM_RE = re.compile(
    r"(?:\b(?:в|из)\s+(?:(?:папк|каталог|директори)\w*\s+)?|→\s*|:\s*)"
    r"`?[^`\s,;)]+/",
    re.IGNORECASE,
)


def _instruction_files(root):
    """Живые инструкции; исторические разборы и очередь намеренно не входят."""
    root = Path(root)
    files = {root / "AGENTS.md", root / "CLAUDE.md", root / ".claude/CLAUDE.md"}
    # `.agents/` и `.codex/` — механическое производное `sync_prompts.py`;
    # здесь проверяется канон, а побайтовый дрейф производного держит его --check.
    for pattern in (".claude/commands/*.md", ".claude/agents/*.md",
                    ".claude/skills/*/*.md"):
        files.update(root.glob(pattern))
    return sorted(p for p in files if p.is_file())


def _contract_text_errors(path, text):
    errors = []
    for number, line in enumerate(text.splitlines(), 1):
        if (DIRECT_DRAFT_DOCX_RE.search(line) or DOCX_IN_DRAFTS_RE.search(line)
                or VERSIONED_DRAFT_RE.search(line)):
            errors.append(f"{path}:{number}: готовый файл назван в служебной папке")
        elif WRONG_HOME_RE.search(line) and READY not in line and "case_paths.py" not in line:
            errors.append(f"{path}:{number}: назван другой дом готового документа")
        else:
            claim = READY_CLAIM_RE.search(line)
            tail = line[claim.end():claim.end() + 120] if claim else ""
            if HOME_AFTER_CLAIM_RE.search(tail) and READY not in tail:
                errors.append(
                    f"{path}:{number}: дом готового документа объявлен вне case_paths.py")
    return errors


def instruction_contract_errors(root):
    """Расхождения живых инструкций с машинным контрактом документа."""
    root = Path(root)
    errors = []
    for rel in CONTRACT_DOCS:
        path = root / rel
        if not path.is_file():
            errors.append(f"{rel}: файл контракта не найден")
            continue
        text = path.read_text(encoding="utf-8")
        if "case_paths.py" not in text:
            errors.append(f"{rel}: нет ссылки на scripts/case_paths.py")
    for path in _instruction_files(root):
        errors.extend(_contract_text_errors(path.relative_to(root),
                                            path.read_text(encoding="utf-8")))
    for rel in REVIEW_CONTRACT_DOCS:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in ROUND_LIMIT_RE.finditer(text):
            raw = (match.group("leading") or match.group("trailing")).lower()
            value = int(raw) if raw.isdigit() else ROUND_WORDS[raw]
            line = text.count("\n", 0, match.start()) + 1
            errors.append(
                f"{rel}:{line}: число раундов {value} повторено вместо ссылки на case_paths.py")
    return errors


def selftest():
    import tempfile
    assert CONTEXT == ".agent/context" and DRAFTS == ".agent/drafts"
    assert READY == "GOTOVO" and READY.isascii(), "имя папки готовых обязано быть латиницей"
    assert AGENT_DIR.startswith("."), "кухня обязана быть скрытой"
    assert all(p.isascii() for p in (AGENT_DIR, INTAKE, HEARINGS, READY, CONTEXT, DRAFTS))
    contract = document_contract("cases/x/y")
    assert {key: contract[key] for key in (
        "draft_md", "ready_md", "ready_docx", "review_rounds")
    } == {
        "draft_md": Path("cases/x/y/.agent/drafts/{document}.md"),
        "ready_md": Path("cases/x/y/GOTOVO/{document}.md"),
        "ready_docx": Path("cases/x/y/GOTOVO/{document}.docx"),
        "review_rounds": MAX_REVIEW_ROUNDS,
    }
    blocks = contract["blocks"]
    assert tuple(blocks) == (
        "content", "sources", "canon", "form", "admission", "home"
    ), "контракт обязан отдать ровно шесть блоков"
    assert all(block["sources"] for block in blocks.values()), "блок без пути источника"
    assert all("path" in source and "exists" in source
               for block in blocks.values() for source in block["sources"]), \
        "источник без пути или признака наличия"
    assert blocks["form"]["exists"] and blocks["admission"]["exists"] \
        and blocks["home"]["exists"], "системный источник контракта не найден"
    assert not blocks["canon"]["exists"], "несуществующий источник объявлен существующим"
    assert blocks["canon"]["sources"][0] == {
        "path": Path("cases/x/y/.agent/context/_working/canon_formulirovki.md"),
        "exists": False,
    }, "отсутствующий источник пропущен или не помечен"
    assert blocks["home"]["ready_dir"] == Path("cases/x/y/GOTOVO")
    assert blocks["home"]["ready_name"] == READY_DOCX_NAME

    root = Path(__file__).resolve().parent.parent
    verdict_config_path = root / "config/verdict.json"
    if verdict_config_path.is_file():
        verdict_config = json.loads(verdict_config_path.read_text(encoding="utf-8"))
        assert "round_limit" not in verdict_config, \
            "базовый лимит повторен в config/verdict.json вместо case_paths.py"
    errors = instruction_contract_errors(root)
    assert not errors, "один дом документа нарушен:\n" + "\n".join(errors)
    hostile = _contract_text_errors(
        "hostile.md", "Готовый .docx живет в .agent/drafts/chuzhoy.docx")
    assert hostile, "враждебная инструкция с другим домом не поймана"
    hostile = _contract_text_errors("hostile.md", "Готовый .docx живет в CHUZHOY-DOM/")
    assert hostile, "произвольный чужой дом готового документа не пойман"
    hostile = _contract_text_errors("hostile.md", "Свежайший .docx брать из tmp/out")
    assert hostile, "чужой дом после предлога «из» не пойман"
    hostile = _contract_text_errors(
        "hostile.md", "Готовый документ в tmp/out, см. scripts/case_paths.py")
    assert hostile, "ссылка на case_paths.py замаскировала чужой дом"
    round_match = ROUND_LIMIT_RE.search("До 5 раундов")
    assert round_match
    raw_rounds = (round_match.group("leading") or round_match.group("trailing")).lower()
    hostile_rounds = int(raw_rounds) if raw_rounds.isdigit() else ROUND_WORDS[raw_rounds]
    assert hostile_rounds != MAX_REVIEW_ROUNDS, \
        "чужой лимит рецензии не распознан"
    for rules in root.rglob("AGENTS.md"):
        if ".git" not in rules.parts:
            assert len(rules.read_text(encoding="utf-8").splitlines()) <= 200, \
                f"{rules.relative_to(root)}: превышен лимит 200 строк"
    for rules in root.rglob("CLAUDE.md"):
        if ".git" not in rules.parts:
            assert len(rules.read_text(encoding="utf-8").splitlines()) <= 200, \
                f"{rules.relative_to(root)}: превышен лимит 200 строк"

    # Порядок замен: длинный ключ раньше короткого
    assert modernize("cases/x/y/03_drafts/_baselines/a.docx") == \
        "cases/x/y/.agent/drafts/_baselines/a.docx"
    assert modernize("01_context/_working/brief.md") == ".agent/context/_working/brief.md"
    assert modernize("00_intake/скан.pdf") == "00_intake/скан.pdf", "интейк тронут"
    assert modernize("02_hearings/x") == "02_hearings/x", "заседания тронуты"

    with tempfile.TemporaryDirectory(prefix="casepaths-selftest-") as tmp:
        untouched = Path(tmp) / "contract-only"
        document_contract(untouched)
        assert not untouched.exists(), "чтение контракта создало каталог дела"

        mixed = Path(tmp) / "mixed-content"
        mixed_brief = mixed / ".agent/context/_working/brief.md"
        mixed_brief.parent.mkdir(parents=True)
        mixed_brief.write_text("source\n", encoding="utf-8")
        mixed_content = document_contract(mixed)["blocks"]["content"]
        assert [s["exists"] for s in mixed_content["sources"]] == [True, False] \
            and not mixed_content["exists"], "частично полный блок объявлен полным"

        legacy = Path(tmp) / "legacy-council"
        (legacy / ".agent/context/_practice").mkdir(parents=True)
        (legacy / ".agent/context/practice.md").write_text("source\n", encoding="utf-8")
        old_verdict = legacy / ".agent/context/_practice/council_verdict.md"
        old_verdict.write_text("source\n", encoding="utf-8")
        sources_block = document_contract(legacy)["blocks"]["sources"]
        assert sources_block["exists"] and sources_block["sources"][1]["path"] == old_verdict, \
            "существующий старый путь вердикта совета объявлен отсутствующим"

        case = Path(tmp) / "delo-2026"
        for d in ("01_context/_working", "03_drafts/_baselines", "04_archive",
                  "00_intake", "02_hearings"):
            (case / d).mkdir(parents=True)
        moves = migration_moves(case)
        assert len(moves) == 3, f"переезжать должны ровно три каталога, а не {len(moves)}"
        assert not any("00_intake" in str(s) or "02_hearings" in str(s) for s, _ in moves), \
            "неприкосновенный каталог попал в переезд"
        assert not is_migrated(case), "непереехавшее дело объявлено переехавшим"
        for src, dst in moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        assert is_migrated(case), "переехавшее дело не опознано"
        assert migration_moves(case) == [], "повторный переезд не идемпотентен"
        assert baselines(case) == case / ".agent/drafts/_baselines"
        assert ready(case).name == "GOTOVO"
        # Журнал вердиктов — вне _working (D03): новый адрес прямо в drafts,
        # старый (в _working) отдельным helper-ом для чтения-запаса.
        assert verdicts(case) == case / ".agent/drafts/verdicts.jsonl"
        assert verdicts_legacy(case) == case / ".agent/drafts/_working/verdicts.jsonl"
        assert WORKING not in verdicts(case).parts, "журнал остался в слепом пятне сторожа"

        # Файл прогона (M01): один адрес в .agent/context, аддитивная запись
        assert run_state(case) == case / ".agent/context/run.json"
        assert run_read(case) == {}, "пустое состояние не пусто"
        run_write(case, guide="themiz-pipeline")
        run_write(case, preflight_code=0)
        st = run_read(case)
        assert st == {"guide": "themiz-pipeline", "preflight_code": 0}, \
            f"аддитивная запись прогона сломана: {st}"

        # REQ-10: лок меняет только .lock; живой владелец назван, старый/мертвый снят.
        lock_case = Path(tmp) / "lock-client" / "lock-case"
        context(lock_case).mkdir(parents=True)
        before_lock = {p.relative_to(lock_case) for p in lock_case.rglob("*")}
        now = time.time()
        acquired = case_lock_acquire(lock_case, "session-one", os.getpid(), now)
        assert acquired["state"] == "acquired" and acquired["ok"], acquired
        assert set(json.loads(case_lock_path(lock_case).read_text())) == {
            "pid", "timestamp", "session"}
        assert case_lock_status(lock_case, now + 1)["state"] == "live"
        foreign = case_lock_acquire(lock_case, "session-two", os.getpid(), now + 2)
        assert not foreign["ok"] and "session-one" in foreign["reason"], foreign
        owned = case_lock_acquire(lock_case, "session-one", os.getpid(), now + 3)
        assert owned["state"] == "owned", owned
        assert not case_lock_release(
            lock_case, "session-one", os.getpid(),
            timestamp=acquired["timestamp"])["ok"], "старый захват снял свежий лок"
        assert case_lock_release(
            lock_case, "session-one", os.getpid(),
            timestamp=owned["timestamp"])["state"] == "released"
        assert not case_lock_path(lock_case).exists(), "снятый лок остался на диске"

        case_lock_path(lock_case).write_text(json.dumps({
            "pid": 999_999_999, "timestamp": now, "session": "dead-session",
        }), encoding="utf-8")
        dead = case_lock_status(lock_case, now + 4)
        assert dead["state"] == "stale_removed" and "не жив" in dead["reason"], dead

        case_lock_acquire(
            lock_case, "old-session", os.getpid(),
            now - CASE_LOCK_STALE_SECONDS - 1)
        old = case_lock_status(lock_case, now)
        assert old["state"] == "stale_removed" and "старше порога" in old["reason"], old
        assert not case_lock_path(lock_case).exists()
        assert {p.relative_to(lock_case) for p in lock_case.rglob("*")} == before_lock, \
            "дело изменено помимо .lock"

    # Unicode: NFD и NFC одного имени обязаны считаться одним делом
    nfd_name = unicodedata.normalize("NFD", "кузнецова-йогурт")
    nfc_name = unicodedata.normalize("NFC", "кузнецова-йогурт")
    assert nfd_name != nfc_name, "тестовое имя не различается по формам — проверка пустая"
    assert nfc(nfd_name) == nfc(nfc_name), "нормализация не сводит формы"
    assert collisions([nfd_name, nfc_name]), "коллизия NFD/NFC не поймана"
    assert not collisions(["ivanov-ivan", "petrov-petr"]), "ложная коллизия на разных именах"
    # Турецкая «ı» без точки — не латинская «i»: разные буквы, коллизией не считаются.
    # Такое имя на диске есть; фикстура вымышленная — сторож ПД поймал реальное 19.08.2026.
    assert not collisions(["demidov-ab", "demıdov-ab"]), "разные буквы объявлены коллизией"
    print("selftest: один дом готового документа, один лимит рецензии, лок дела, "
          "инструкции ≤200 строк; "
          "контракт латиницей, скрытая кухня, порядок замен, неприкосновенные каталоги, "
          "идемпотентность переезда, NFD/NFC — ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Контракт раскладки дела.")
    ap.add_argument("--show", metavar="CASE", help="показать пути конкретного дела")
    ap.add_argument("--document-contract", nargs="?", const="cases/{client}/{case}",
                    metavar="CASE", help="показать дом, имена файлов и лимит рецензии")
    ap.add_argument("--run-get", nargs="+", metavar=("CASE", "KEY"),
                    help="прочитать файл прогона (CASE [KEY])")
    ap.add_argument("--run-set", nargs=3, metavar=("CASE", "KEY", "VALUE"),
                    help="записать ключ в файл прогона (VALUE как JSON, иначе строка)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.document_contract is not None:
        for key, value in document_contract(a.document_contract).items():
            if key == "blocks":
                value = json.dumps(value, ensure_ascii=False, default=str,
                                   separators=(",", ":"))
            print(f"{key}={value}")
        return 0
    if a.run_set:
        case, key, value = a.run_set
        try:
            value = json.loads(value)      # число/булево/строка-в-кавычках
        except ValueError:
            pass                            # голая строка
        run_write(case, **{key: value})
        print(f"прогон {case}: {key} = {value!r}")
        return 0
    if a.run_get:
        case = a.run_get[0]
        st = run_read(case)
        if len(a.run_get) > 1:
            print(json.dumps(st.get(a.run_get[1]), ensure_ascii=False))
        else:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    if a.show:
        c = Path(a.show)
        for label, p in (("интейк", intake(c)), ("заседания", hearings(c)),
                         ("ГОТОВО", ready(c)), ("контекст", context(c)),
                         ("черновики", drafts(c)), ("снимки", baselines(c)),
                         ("архив", archive(c))):
            print(f"  {label:12} {p}")
        print(f"  переехало:   {'да' if is_migrated(c) else 'нет'}")
        return 0
    print(f"раскладка дела: {' · '.join(HUMAN_VISIBLE)} + {AGENT_DIR}/"
          f"{{context,drafts,archive}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
