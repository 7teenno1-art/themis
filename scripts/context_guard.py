#!/usr/bin/env python3
"""context_guard.py — машинная часть правил экономии контекста. Зовется из claude_guard.

ЗАЧЕМ. Правило «не читать заранее, не читать дважды, разгружаться между фазами» три
редакции стояло в .claude/CLAUDE.md текстом — и три редакции не исполнялось: замер
02.09.2026 дал 200,55 $ из 360,17 $ (55,7 %) на ПОВТОРНУЮ доставку уже прочитанного,
контекст оркестратора вырос с 96 480 до 503 962 токенов без единого сброса.
Правило, которое не держит машина, — пожелание.

ТРИ ВОРОТ (PreToolUse, блок = exit 2 из claude_guard):
1. ПОТОЛОК КОНТЕКСТА. Прирост контекста над стартовым весом окружения (usage первого
   запроса сессии) выше потолка → работать этой сессией нельзя: каждый следующий
   вызов оплачивает весь накопленный вес заново. Стартовый вес — преамбула, описания
   инструментов, перечень навыков — агент не выбирал и потолком не меряется.
   Пропускаются только действия РАЗГРУЗКИ: запись/чтение handoff.md, запуск приборов
   замера, спавн субагента (у него контекст свой). Выключатель на крайний случай:
   THEMIZ_CTX_LIMIT=0. Обходы THEMIZ_CTX_OK=1 считаются в сайдкаре сессии; сверх
   порога BYPASS_WARN блок несет отдельный сигнал — обход перестал быть разовым.
2. ПОВТОРНОЕ ЧТЕНИЕ. Тот же файл тем же срезом, файл на диске не менялся → блок:
   он уже в контексте, второй раз платится зря. Другой срез (offset/limit) и файл,
   изменившийся после первого чтения, проходят — это не повтор. Чтение оболочкой
   (cat/head/tail/…) видно тому же детектору: имя файла берется разбором команды.
3. ВЕС ИНСТРУКЦИЙ. Запись в CLAUDE.md / AGENTS.md меряется В БАЙТАХ, а не в строках:
   199 строк при 45 181 байте — это 95 792 токена стартового контекста на КАЖДОМ
   запросе сессии. Лимит строк остается, байтовый добавляется.

Состояние чтений — сайдкар в каталоге временных файлов, по одному на сессию;
переживать сессию ему незачем.

Проверка: python3 scripts/context_guard.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
import sreda  # noqa: E402,F401  переходный период имен переменных

# Потолок контекста ОДНОГО запроса. 200 000 — порог, после которого фазу пора
# закрывать: замер 02.09.2026 показал 104 запроса из 189 с контекстом свыше 300 000.
CTX_LIMIT = int(os.environ.get("THEMIZ_CTX_LIMIT", "200000"))

# Потолок веса файла инструкций. 16 КБ ≈ 4 000 токенов на каждый запрос сессии.
INSTRUCTION_MAX_BYTES = 16 * 1024
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")

# Что разрешено, когда потолок пробит. Пропускается то, что контекст НЕ растит:
# запись (ею и делается выгрузка), спавн субагента (контекст у него свой), приборы
# замера и состояния, самопроверки. Блокируется то, что тащит новый вес: чтение,
# произвольный Bash, сеть.
UNLOAD_PATH = re.compile(r"handoff\.md$", re.I)
UNLOAD_CMD = re.compile(r"context_ledger|token_ledger|themiz_status|retro\.py|--selftest")
UNLOAD_TOOLS = ("Agent", "Write", "Edit", "NotebookEdit")

# Осознанный обход ОДНОГО вызова: префикс прямо в команде. Слово-пропуск в середине
# строки (первым таким был `git `) молча превращается в лазейку, через которую
# проходит вся работа; здесь обход виден и в команде, и в журнале сессии.
BREAK_GLASS = "THEMIZ_CTX_OK=1"

# Порог обходов за фазу. Обход остается разовым осознанным выходом и не отменяется,
# но сверх порога ворота добавляют в блок отдельный сигнал: 4 914 обходов за сессию
# 06.09.2026 прошли без единого сигнала — обход стал стилем работы вместо выхода.
BYPASS_WARN = 3

# Хвост транскрипта, которого хватает, чтобы найти последнюю реплику с usage.
TAIL_BYTES = 512 * 1024

# Голова транскрипта, где лежит usage ПЕРВОГО запроса — стартовый вес окружения
# (системная преамбула, описания инструментов, перечень навыков). Этот вес агент
# не выбирал и уменьшить не может, потолком меряется прирост НАД ним.
HEAD_BYTES = 256 * 1024

# Команды оболочки, читающие файл целиком или срезом. Детектор повторного чтения
# обязан видеть их наравне с инструментом Read: работа сессии 06.09.2026 шла
# через оболочку, где старый детектор был слеп.
SHELL_READERS = ("cat", "head", "tail", "less", "more", "bat")


def ctx_current(transcript: str) -> int:
    """Контекст последнего запроса сессии: input + cache-write + cache-read.

    0 — прочитать нечем (нет файла, нет usage): молчаливый ноль здесь безопаснее
    блока, потому что сторож не должен запирать сессию из-за отсутствия журнала.
    """
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        msg = entry.get("message") if isinstance(entry, dict) else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if isinstance(u, dict):
            return (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0))
    return 0


def ctx_start(transcript: str) -> int | None:
    """Стартовый вес окружения: usage ПЕРВОГО запроса сессии.

    None — голова журнала не читается или usage в ней нет: тогда считать
    прирост не от чего, и ворота вправе вернуться к абсолютному весу.
    """
    try:
        with open(transcript, "rb") as fh:
            head = fh.read(HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    for raw in head.splitlines():
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        msg = entry.get("message") if isinstance(entry, dict) else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if isinstance(u, dict):
            return (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0))
    return None


def _state_path(session_id: str) -> str:
    d = os.path.join(tempfile.gettempdir(), "themiz-context")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", session_id or "no-session")
    return os.path.join(d, f"{safe}.json")


def _state(session_id: str) -> dict:
    try:
        with open(_state_path(session_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(session_id: str, data: dict) -> None:
    try:
        with open(_state_path(session_id), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass  # сайдкар — удобство, а не источник правды


def read_key(ti: dict, base: str) -> tuple[str, str]:
    """Ключ чтения: (путь+срез, mtime файла). Разные срезы — разные чтения."""
    path = ti.get("file_path") if isinstance(ti.get("file_path"), str) else ""
    abspath = os.path.abspath(os.path.join(base, os.path.expanduser(path)))
    key = f"{abspath}|{ti.get('offset')}|{ti.get('limit')}"
    try:
        stamp = str(os.path.getmtime(abspath))
    except OSError:
        stamp = ""
    return key, stamp


def shell_reads(command: str, base: str) -> list[str]:
    """Файлы, которые читает команда оболочки: разбор токенов, не догадка.

    Кандидат — нефлаговый аргумент команды-читалки (cat/head/tail/…), который
    существует файлом на диске. ponytail: пайпы без пробелов (`cat a|grep b`)
    и `sed -n '1,5p'` не разбираются — апгрейд: полный разбор по грамматике sh.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return []
    reads = []
    separators = ("|", "||", "&&", ";")
    expect_cmd = True
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in separators:
            expect_cmd = True
        elif expect_cmd:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok) or tok in ("sudo", "env", "command"):
                pass  # присваивание окружения или префикс запуска — команда дальше
            else:
                expect_cmd = False
                if os.path.basename(tok) in SHELL_READERS:
                    j = i + 1
                    while j < len(tokens) and tokens[j] not in separators:
                        arg = tokens[j]
                        if not arg.startswith("-"):
                            ap = os.path.abspath(os.path.join(base, os.path.expanduser(arg)))
                            if os.path.isfile(ap):
                                reads.append(ap)
                        j += 1
                    i = j
                    continue
        i += 1
    return reads


def _bypass_signal(state: dict) -> str:
    """Отдельный сигнал в текст блока, когда обходы перестали быть разовыми."""
    n = int(state.get("bypass") or 0)
    if n > BYPASS_WARN:
        return (f" ОТДЕЛЬНЫЙ СИГНАЛ: обходов {BREAK_GLASS} за сессию уже {n} при пороге "
                f"{BYPASS_WARN} — обход стал стилем работы вместо разового выхода. "
                "Остановиться и разгрузить контекст (handoff.md), а не обходить ворота.")
    return ""


def instruction_size(tool: str, ti: dict, base: str) -> tuple[str, int] | None:
    """(файл, его размер после записи) для CLAUDE.md/AGENTS.md, иначе None."""
    path = ti.get("file_path") if isinstance(ti.get("file_path"), str) else ""
    if not path or os.path.basename(path) not in INSTRUCTION_FILES:
        return None
    abspath = os.path.abspath(os.path.join(base, os.path.expanduser(path)))
    if tool == "Write":
        content = ti.get("content")
        return abspath, len((content if isinstance(content, str) else "").encode("utf-8"))
    if tool in ("Edit", "NotebookEdit"):
        try:
            size = os.path.getsize(abspath)
        except OSError:
            return None
        old = ti.get("old_string") or ti.get("new_source") or ""
        new = ti.get("new_string") or ""
        old_b = len(str(old).encode("utf-8"))
        new_b = len(str(new).encode("utf-8"))
        return abspath, size - old_b + new_b
    return None


def check(payload: dict) -> str | None:
    """Причина блока либо None. Побочный эффект — запись сайдкара чтений."""
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    base = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    session_id = payload.get("session_id") or ""

    # 3. Вес файла инструкций — в байтах.
    size_info = instruction_size(tool, ti, base)
    if size_info and size_info[1] > INSTRUCTION_MAX_BYTES:
        name, size = size_info
        return (f"БЛОК (вес инструкций): {os.path.basename(name)} после записи — "
                f"{size:,} байт при потолке {INSTRUCTION_MAX_BYTES:,}. ".replace(",", " ")
                + "Инструкция едет в КАЖДОМ запросе сессии: 45 181 байт = ~95 792 токена "
                  "стартового контекста на каждый вызов. Правило, выразимое хуком, "
                  "переносить в хук; невыразимое — в knowledge/claude-md-razbor.md "
                  "с причиной. Лимит строк при этом остается.")

    # 1. Потолок контекста сессии — на ПРИРОСТ над стартовым весом окружения.
    limit = CTX_LIMIT
    transcript = payload.get("transcript_path")
    if limit > 0 and isinstance(transcript, str) and transcript:
        ctx = ctx_current(transcript)
        start = ctx_start(transcript)
        # Потолку подлежит прирост: преамбула, описания инструментов и перечень
        # навыков — не выбор агента, иначе ворота срабатывают до первого полезного
        # действия и учат обходу. Старт не читается (битая голова журнала) —
        # возврат к абсолютному весу: строгие ворота лучше выключенных.
        growth = ctx - start if start is not None else ctx
        if ctx > 0 and growth > limit:
            # Поля проверяем ПООТДЕЛЬНОСТИ: склейка в одну строку ломает якорь конца
            # пути — «handoff.md» перестает быть концом строки (проба selftest).
            fields = [str(ti.get(k) or "").strip() for k in ("file_path", "command", "path")]
            razgruzka = (tool in UNLOAD_TOOLS
                         or any(BREAK_GLASS in f or UNLOAD_PATH.search(f)
                                or UNLOAD_CMD.search(f) for f in fields))
            state = _state(session_id)
            if any(BREAK_GLASS in f for f in fields):
                state["bypass"] = int(state.get("bypass") or 0) + 1
                _save(session_id, state)
            if not razgruzka:
                return (f"БЛОК (потолок контекста): прирост {growth:,} токенов над "
                        f"стартовым весом при потолке {limit:,}. ".replace(",", " ")
                        + "Контекст живой сессии не уменьшается сам, и каждый следующий "
                          "вызов оплачивает весь этот вес заново (замер 02.09.2026: 55,7 % "
                          "счета — повторная доставка прочитанного). Разгрузка: записать "
                          ".agent/context/handoff.md (что сделано · что дальше · состояние · "
                          "ССЫЛКИ на файлы, не содержимое) и продолжить чистой сессией либо "
                          "субагентом — спавн Agent разрешен, у него контекст свой. "
                          "Замер: python3 scripts/context_ledger.py. Запись, спавн агента "
                          "и приборы замера проходят; чтение и произвольный Bash — нет. "
                          f"Разовый осознанный обход — префикс {BREAK_GLASS} в команде; "
                          "снять потолок совсем — THEMIZ_CTX_LIMIT=0."
                        + _bypass_signal(state))

    # 2. Повторное чтение того же среза неизмененного файла — инструментом Read
    #    или оболочкой: `cat файл` платит тем же контекстом, что и Read.
    targets = []
    if tool == "Read" and ti.get("file_path"):
        targets.append(read_key(ti, base))
    elif tool == "Bash" and isinstance(ti.get("command"), str):
        for ap in shell_reads(ti["command"], base):
            try:
                stamp = str(os.path.getmtime(ap))
            except OSError:
                stamp = ""
            targets.append((f"{ap}|bash", stamp))
    if targets:
        state = _state(session_id)
        seen = state.setdefault("reads", {})
        repeat = None
        for key, stamp in targets:
            if repeat is None and seen.get(key) == stamp and stamp:
                repeat = key
            seen[key] = stamp
        _save(session_id, state)
        if repeat is not None:
            return (f"БЛОК (повторное чтение): {os.path.basename(repeat.split('|')[0])} этим же "
                    "срезом уже прочитан в этой сессии, и файл с тех пор не менялся — он "
                    "лежит в контексте, второе чтение платится зря. Нужен другой участок — "
                    "звать со срезом (offset/limit) или грепом; нужен свежий файл — "
                    "он пройдет сам, как только изменится на диске.")
    return None


def selftest() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        transcript = os.path.join(tmp, "s.jsonl")
        with open(transcript, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 90,
                "cache_read_input_tokens": 200}}}) + "\n")  # стартовый вес 300
            fh.write(json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 90,
                "cache_read_input_tokens": 400}}}) + "\n")  # последний запрос 500
        target = os.path.join(tmp, "f.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        claude_md = os.path.join(tmp, "CLAUDE.md")
        with open(claude_md, "w", encoding="utf-8") as fh:
            fh.write("y" * 100)

        def pl(**kw):
            base = {"session_id": "t1", "cwd": tmp, "transcript_path": transcript}
            base.update(kw)
            return base

        global CTX_LIMIT
        first = check(pl(tool_name="Read", tool_input={"file_path": target}, transcript_path=""))
        second = check(pl(tool_name="Read", tool_input={"file_path": target}, transcript_path=""))
        sliced = check(pl(tool_name="Read", tool_input={"file_path": target, "offset": 50},
                          transcript_path=""))
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("changed")
        os.utime(target, (0, 0))
        after_change = check(pl(tool_name="Read", tool_input={"file_path": target},
                                transcript_path=""))

        saved, CTX_LIMIT = CTX_LIMIT, 100
        over = check(pl(tool_name="Bash", tool_input={"command": "cat cases/x"}))
        over_read = check(pl(tool_name="Read", tool_input={"file_path": target}))
        unload = check(pl(tool_name="Write", tool_input={
            "file_path": ".agent/context/handoff.md", "content": "z"}))
        agent = check(pl(tool_name="Agent", tool_input={"description": "охота"}))
        measure = check(pl(tool_name="Bash", tool_input={
            "command": "python3 scripts/context_ledger.py"}))
        glass = check(pl(tool_name="Bash", tool_input={
            "command": f"{BREAK_GLASS} cat cases/x"}))
        sneaky = check(pl(tool_name="Bash", tool_input={"command": "git log && cat cases/x"}))
        CTX_LIMIT = 0
        off = check(pl(tool_name="Bash", tool_input={"command": "cat cases/x"}))
        CTX_LIMIT = saved

        # T208: прирост при большом стартовом весе, счетчик обходов, чтение оболочкой.
        big = os.path.join(tmp, "big.jsonl")  # старт 900 000, последний 950 000
        with open(big, "w", encoding="utf-8") as fh:
            for total in (900_000, 950_000):
                fh.write(json.dumps({"type": "assistant", "message": {"usage": {
                    "input_tokens": total}}}) + "\n")
        fresh = check(pl(tool_name="Bash", tool_input={"command": "ls"},
                         transcript_path=big, session_id="t4"))

        # Сайдкар переживает прогон: счетчик обходов с постоянным именем сессии
        # копится между запусками (4, 8, 12…). Чистим перед случаем.
        try:
            os.remove(_state_path("t2"))
        except OSError:
            pass
        saved, CTX_LIMIT = CTX_LIMIT, 100
        bypassed = None
        for _ in range(BYPASS_WARN + 1):
            bypassed = check(pl(tool_name="Bash", tool_input={
                "command": f"{BREAK_GLASS} cat cases/x"}, session_id="t2"))
        bypass_n = int(_state("t2").get("bypass") or 0)
        signaled = check(pl(tool_name="Bash", tool_input={"command": "cat cases/x"},
                            session_id="t2"))
        CTX_LIMIT = saved

        shell1 = check(pl(tool_name="Bash", tool_input={"command": f"cat {target}"},
                          transcript_path="", session_id="t3"))
        shell2 = check(pl(tool_name="Bash", tool_input={"command": f"tail -n 5 {target}"},
                          transcript_path="", session_id="t3"))

        big = check(pl(tool_name="Write", tool_input={
            "file_path": claude_md, "content": "y" * (INSTRUCTION_MAX_BYTES + 1)}))
        small = check(pl(tool_name="Write", tool_input={"file_path": claude_md, "content": "y"}))
        grow = check(pl(tool_name="Edit", tool_input={
            "file_path": claude_md, "old_string": "y",
            "new_string": "y" * (INSTRUCTION_MAX_BYTES + 10)}))
        other = check(pl(tool_name="Write", tool_input={
            "file_path": os.path.join(tmp, "note.md"), "content": "y" * 999_999}))

        checks = [
            ("первое чтение проходит", first is None),
            ("повторное чтение того же среза блокируется", second is not None),
            ("другой срез — не повтор", sliced is None),
            ("измененный файл читается заново", after_change is None),
            ("контекст выше потолка блокирует произвольный Bash", over is not None),
            ("контекст выше потолка блокирует чтение — оно и растит вес",
             over_read is not None),
            ("запись выгрузки проходит при пробитом потолке", unload is None),
            ("спавн субагента проходит: его контекст свой", agent is None),
            ("прибор замера проходит", measure is None),
            ("осознанный обход префиксом проходит", glass is None),
            ("слово-пропуск в середине команды лазейкой не работает", sneaky is not None),
            ("THEMIZ_CTX_LIMIT=0 снимает потолок", off is None),
            ("CLAUDE.md сверх байтового потолка блокируется", big is not None),
            ("CLAUDE.md в пределах потолка проходит", small is None),
            ("рост через Edit считается по байтам", grow is not None),
            ("посторонний файл байтовым потолком не меряется", other is None),
            ("битый payload не роняет сторожа", check(None) is None),
            ("нет транскрипта — нет потолка, а не блок",
             ctx_current(os.path.join(tmp, "нет.jsonl")) == 0),
            ("контекст читается с конца журнала", ctx_current(transcript) == 500),
            ("стартовый вес читается с головы журнала", ctx_start(transcript) == 300),
            ("старт не читается — None, а не угадывание",
             ctx_start(os.path.join(tmp, "нет.jsonl")) is None),
            ("прирост меньше потолка при большом стартовом весе проходит", fresh is None),
            ("обход остается разовым выходом и проходит", bypassed is None),
            ("обходы считаются в сайдкаре сессии", bypass_n == BYPASS_WARN + 1),
            ("порог обходов дает отдельный сигнал в тексте блока",
             signaled is not None and "СИГНАЛ" in signaled),
            ("первое чтение файла через оболочку проходит", shell1 is None),
            ("повторное чтение того же файла через оболочку блокируется",
             shell2 is not None),
        ]
        bad = [n for n, ok in checks if not ok]
        for n, ok in checks:
            print(f"  {'✓' if ok else '✗'} {n}")
        if bad:
            print(f"selftest ПРОВАЛЕН: {len(bad)} из {len(checks)}")
            return 1
        print(f"selftest пройден: {len(checks)}/{len(checks)}")
        return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    reason = check(data)
    if reason:
        print(reason, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(0)
