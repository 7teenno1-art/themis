#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preflight_search.py — проверка каналов поиска ДО запуска охоты за практикой.

Зачем. В прогоне боевого дела (раздел имущества) добор через Tavily стоил 249 950 токенов и вернул
«инструмент недоступен»: сервер числится в конфигурации без ключа, а MCP вообще
не наследуется агентом с явным списком `tools` в frontmatter. Отдельно охотник
уже в процессе обнаружил, что квота веб-поиска исчерпана. Обе проверки —
одна команда.

Использование:
    python3 scripts/preflight_search.py
    python3 scripts/preflight_search.py --json
    python3 scripts/preflight_search.py --selftest

Выход: таблица «канал → статус → что делать». Код возврата 1, если не осталось
ни одного внешнего канала — тогда охота за внешней практикой не запускается,
работаем по knowledge/practice_index.md.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import sreda  # noqa: E402,F401  переходный период имен переменных

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def resolve_case(cli_case: str) -> str:
    """Дело прогона: явный --case важнее $THEMIZ_CASE. Переменную в бою никто не
    выставлял — флага не было вовсе (02.09.2026), и мертвый канал не долетал до
    preflight: источник опрашивался повторно 128 раз за прогон 01.09.2026."""
    return cli_case or os.environ.get("THEMIZ_CASE", "")


def _sudact_allowed() -> bool:
    """Точка правды — practice_search.search_allowed(). Читаем ее, а не копию условия."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        from practice_search import search_allowed
        return search_allowed()
    except Exception:
        return os.environ.get("THEMIZ_SUDACT_SEARCH") == "1"


def probe_url(url: str, timeout: int = 8) -> bool:
    """Живой ли публикатор. ponytail: HEAD часто режут, берем первые байты GET."""
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-A", UA, "-o", "/dev/null",
             "-w", "%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 4)
        return r.stdout.strip().startswith(("2", "3"))
    except Exception:
        return False


def check_lynceuz() -> tuple[bool, str]:
    """Основной сборщик: проверяем установленный CLI, не наличие чужого ключа.

    Health подтверждает возможность url/crawl/extract, но не доступность
    конкретного сайта и не полноценный поиск по произвольному запросу.
    """
    home = Path(os.environ.get("LYNCEUZ_HOME", str(Path.home() / "Проекты" / "lynceuz")))
    entry = home / "src" / "lynceuz.mjs"
    if not entry.is_file():
        return False, "CLI не найден; указать каталог установки в LYNCEUZ_HOME"
    try:
        result = subprocess.run(["node", str(entry), "health", "--json"], cwd=home,
                                capture_output=True, text=True, timeout=20)
        report = json.loads(result.stdout)
        if result.returncode or not isinstance(report, dict) or report.get("status") != "ok":
            reason = report.get("message", report.get("code", "нет описания")) \
                if isinstance(report, dict) else "ответ не объект"
            return False, f"health не прошел (код {result.returncode}): {reason}"
        ready = [c for c in report.get("capabilities", [])
                 if isinstance(c, dict) and c.get("state") == "ready"]
        commands = {command for c in ready for command in c.get("commands", [])}
        if not {"url", "crawl", "extract"} <= commands:
            return False, "health не подтвердил url/crawl/extract"
        search = "search готов" if "search" in commands else "общий search не подтвержден"
        return True, f"url/crawl/extract готовы; {search}; сайт проверять отдельным запуском"
    except subprocess.TimeoutExpired:
        return False, "таймаут health (20 с)"
    except (OSError, ValueError, TypeError) as exc:
        return False, f"health не прочитан: {type(exc).__name__}"


def check_mcp_key(server: str) -> tuple[bool, str]:
    """Есть ли ключ у MCP-сервера в $HOME/.claude.json."""
    cfg = str(Path.home() / ".claude.json")
    if not os.path.exists(cfg):
        return False, f"{cfg} отсутствует"
    try:
        data = json.load(open(cfg, encoding="utf-8"))
    except Exception as e:
        return False, f"конфигурация нечитаема: {e}"

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "mcpServers" and isinstance(v, dict) and server in v:
                    return v[server]
                got = walk(v)
                if got is not None:
                    return got
        elif isinstance(node, list):
            for v in node:
                got = walk(v)
                if got is not None:
                    return got
        return None

    entry = walk(data)
    if entry is None:
        return False, "сервер не зарегистрирован"
    env = entry.get("env") or {}
    if any(env.values()):
        return True, "ключ задан"
    return False, "зарегистрирован, но env пуст — ключа нет"


def probe_sudact(timeout: int = 12) -> bool:
    """Отвечает ли поиск практики на РЕАЛЬНЫЙ запрос. Проба идет по тому же
    маршруту, каким ходит practice_search.py: корень сайта может отвечать 200,
    когда сам поиск лежит с HTTP 500 — так и было 21.08.2026."""
    url = ("https://sudact.ru/regular/doc_ajax/?regular-txt=%D0%B4%D0%BE%D0%BF%D1%80%D0%BE%D1%81"
           "&regular-case_doc=&regular-lawchunkinfo=&regular-date_from=&regular-date_to="
           "&regular-workflow_stage=&regular-area=&regular-court=&regular-judge=")
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-A", UA, "-o", "/dev/null",
             "-w", "%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 4)
        return r.stdout.strip().startswith("2")
    except Exception:
        return False


def check_sgai() -> tuple[bool, str]:
    exe = subprocess.run(["which", "sgai"], capture_output=True, text=True)
    if not exe.stdout.strip():
        return False, "CLI не установлен"
    # Баланс спрашиваем у `credits`, а не у `validate`. Прецедент 21.08.2026:
    # `validate` проверяет здоровье КЛЮЧА и при нулевом остатке отвечает успехом —
    # preflight печатал «OK», а два охотника подряд получали «Insufficient credits»
    # на живой охоте. Отчет, расходящийся с фактом, хуже отсутствия отчета:
    # по нему планируют работу.
    try:
        r = subprocess.run(["sgai", "credits", "--json"], capture_output=True,
                           text=True, timeout=25)
        blob = (r.stdout or "") + (r.stderr or "")
        m = re.search(r'"remaining"\s*:\s*(\d+)', blob)
        if m:
            left = int(m.group(1))
            plan = re.search(r'"plan"\s*:\s*"([^"]+)"', blob)
            suffix = f" ({plan.group(1)})" if plan else ""
            if left == 0:
                return False, f"кредиты исчерпаны{suffix}"
            return True, f"кредитов: {left}{suffix}"
        if re.search(r"no credits|insufficient", blob, re.I):
            return False, "баланс исчерпан"
        if r.returncode == 0:
            return True, "доступен, остаток не прочитан"
        return False, f"credits вернул код {r.returncode}"
    except subprocess.TimeoutExpired:
        return False, "таймаут проверки"
    except Exception as e:
        return False, str(e)[:40]


def search_channels(rows):
    """Живой поиск СудАкта считается поиском, не только публикатором."""
    return [r for r in rows if r[0].startswith(
        ("ScrapeGraphAI", "MCP", "Поиск практики sudact.ru")) and r[1] is True]


def selftest() -> int:
    """Без сети. Порог путь-резолва (b8086b2): check_mcp_key читал литеральный
    "$HOME/.claude.json" → «отсутствует» при файле в 91 КБ. Фикстура ПО ОБЕ
    стороны порога: HOME с конфигом — ключ найден, НЕ слепое «отсутствует»;
    HOME без конфига — честный отказ."""
    import tempfile
    import contextlib
    import io
    from unittest.mock import patch
    checks = []
    _home0 = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["HOME"] = tmp
            no_ok, no_note = check_mcp_key("tavily")
            checks.append(("нет конфига → честный отказ, не выдумка",
                           no_ok is False and "отсутствует" in no_note))
            with open(os.path.join(tmp, ".claude.json"), "w", encoding="utf-8") as fh:
                json.dump({"mcpServers": {"tavily": {"env": {"TAVILY_API_KEY": "x"}}}}, fh)
            ok, note = check_mcp_key("tavily")
            checks.append(("HOME развернут: конфиг найден, не слепое «отсутствует»",
                           ok is True and "отсутствует" not in note))
        finally:
            if _home0 is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = _home0

    _case0 = os.environ.pop("THEMIZ_CASE", None)
    try:
        checks.append(("--case работает без $THEMIZ_CASE",
                       resolve_case("cases/klient/delo") == "cases/klient/delo"))
        checks.append(("без --case и без переменной — дело не опознано",
                       resolve_case("") == ""))
        os.environ["THEMIZ_CASE"] = "env-delo"
        checks.append(("явный --case важнее $THEMIZ_CASE", resolve_case("flag-delo") == "flag-delo"))
        checks.append(("без --case используется $THEMIZ_CASE", resolve_case("") == "env-delo"))
    finally:
        os.environ.pop("THEMIZ_CASE", None)
        if _case0 is not None:
            os.environ["THEMIZ_CASE"] = _case0
    checks.append(("живой СудАкт достаточен для поиска без платного агрегатора",
                   bool(search_channels([("Поиск практики sudact.ru", True, "", "")]))))
    checks.append(("мертвый поиск и живой публикатор не дают поискового канала",
                   not search_channels([("Поиск практики sudact.ru", False, "", ""),
                                        ("Публикатор vsrf.ru", True, "", "")])))
    with tempfile.TemporaryDirectory() as tmp:
        entry = Path(tmp) / "src" / "lynceuz.mjs"
        entry.parent.mkdir()
        entry.write_text("// synthetic fixture\n", encoding="utf-8")
        health = {"status": "ok", "capabilities": [
            {"state": "ready", "commands": ["url", "crawl", "extract"]}]}
        response = subprocess.CompletedProcess([], 0, json.dumps(health), "")
        with patch.dict(os.environ, {"LYNCEUZ_HOME": tmp}), \
                patch.object(subprocess, "run", return_value=response) as run:
            ok, note = check_lynceuz()
            checks.append(("Линкей проверяется живым health, не кредитами резерва",
                           ok and "search не подтвержден" in note and
                           run.call_args.args[0][-2:] == ["health", "--json"]))
            response.stdout = '{"status":"ok","capabilities":[]}'
            checks.append(("пустой health Линкея не считается готовностью", not check_lynceuz()[0]))
    captured = io.StringIO()
    with patch.object(sys, "argv", ["preflight_search.py", "--json"]), \
            patch(__name__ + ".check_lynceuz", return_value=(True, "synthetic health")), \
            patch(__name__ + ".check_sgai", return_value=(False, "нет кредитов")), \
            patch(__name__ + ".check_mcp_key", return_value=(False, "нет")), \
            patch(__name__ + ".probe_url", return_value=True), \
            patch(__name__ + "._sudact_allowed", return_value=False), \
            patch(__name__ + ".resolve_case", return_value=""), \
            contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
        code = main()
    ordered = json.loads(captured.getvalue())
    checks.append(("Линкей первый, ScrapeGraphAI резерв; stdout содержит только JSON",
                   code == 0 and ordered[0]["channel"] == "Линкей (lynceuz)" and
                   "РЕЗЕРВ" in ordered[1]["action"]))
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'✓' if ok else '✗'} {n}")
    if bad:
        print(f"selftest ПРОВАЛЕН: {len(bad)} из {len(checks)}")
        return 1
    print(f"selftest пройден: {len(checks)}/{len(checks)} — путь-резолв без сети")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="проверка без сети")
    ap.add_argument("--case", default="", help="путь к делу — общий счет каналов и квот; "
                    "иначе $THEMIZ_CASE")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    rows = []
    case = resolve_case(a.case)

    lynceuz_ok, note = check_lynceuz()
    rows.append(("Линкей (lynceuz)", lynceuz_ok, note,
                 "ОСНОВНОЙ: skill lynceuz; лестница обнаружения; сохранять манифест"
                 if lynceuz_ok else "ОСНОВНОЙ недоступен: назвать причину перед переходом к резервам"))

    ok, note = check_sgai()
    rows.append(("ScrapeGraphAI (sgai)", ok, note,
                 "РЕЗЕРВ после Линкея; только --json" if ok else "РЕЗЕРВ недоступен: не поручать охотникам"))

    for srv in ("tavily", "firecrawl"):
        ok, note = check_mcp_key(srv)
        rows.append((f"MCP {srv}", ok, note,
                     "доступен как канал" if ok else "не поручать: MCP не наследуется "
                     "агентом с явным tools"))

    for name, url in (("vsrf.ru", "https://vsrf.ru/"),
                      ("legalacts.ru", "https://legalacts.ru/"),
                      ("eg-online.ru", "https://www.eg-online.ru/")):
        ok = probe_url(url)
        rows.append((f"Публикатор {name}", ok, "отвечает" if ok else "недоступен",
                     "verify_act.py сработает" if ok else "верификация через фолбэк"))

    # Решение по sudact живет в practice_search.py (SUDACT_SEARCH_ALLOWED +
    # THEMIZ_SUDACT_SEARCH). Preflight его читает, а не дублирует условие:
    # своя копия условия врала «закрыт» при работающем поиске.
    sudact_on = _sudact_allowed()
    if not sudact_on:
        rows.append(("Поиск практики sudact.ru", False,
                     "выключен явно (THEMIZ_SUDACT_SEARCH=0)",
                     "искать в knowledge/practice_index.md; акт по URL — --doc"))
    else:
        # Мертвый канал из общего файла прогона не опрашивается повторно до
        # истечения TTL записи — 01.09.2026 мертвый источник опрашивался 128 раз.
        dead = None
        chan = None
        if case:
            try:
                sys.path.insert(0, os.path.join(ROOT, "scripts"))
                import channels as chan  # noqa: F811 — присваиваем в локальную
                rec = chan.status(case, "sudact")
                if rec and not rec.get("жив", True):
                    dead = rec
            except Exception:
                dead, chan = None, None
        if dead is not None:
            rows.append(("Поиск практики sudact.ru", False,
                         f"мертв по общему состоянию прогона: "
                         f"{dead.get('причина') or 'без причины'}",
                         "не опрашивать до истечения записи (channels.py --show)"))
        else:
            # Флаг владельца говорит «искать РАЗРЕШЕНО», но не «источник ЖИВ».
            # 21.08.2026 источник весь день отдавал HTTP 500, а preflight печатал «OK»,
            # и охотники записали пустой результат как отсутствие практики. Разрешение
            # и живость — разные вопросы, спрашиваем оба.
            alive = probe_sudact()
            if chan is not None:
                try:
                    chan.mark(case, "sudact", alive,
                              "отвечает" if alive else "источник НЕ отвечает")
                except Exception:
                    pass
            rows.append(("Поиск практики sudact.ru", alive,
                         "включен владельцем, источник отвечает" if alive
                         else "включен владельцем, но источник НЕ отвечает",
                         "practice_search.py ищет" if alive
                         else "пустой результат НЕ считать отсутствием практики — повторить позже"))
    # Расход WebSearch — общий счет прогона (scripts/channels.py), не догадка
    # отдельного охотника: раньше поле было советом «спросить в первом отчете», и
    # трое охотников независимо отвечали «квоты много» на одном и том же прогоне.
    ws_used = ws_cap = None
    if case:
        try:
            sys.path.insert(0, os.path.join(ROOT, "scripts"))
            import channels as _channels
            ws_used, ws_cap = _channels.quota_status(case, "websearch")
        except Exception:
            ws_used = None
    if ws_used is None:
        rows.append(("WebSearch (квота сессии)", None,
                     "дело не опознано ($THEMIZ_CASE/--case) — общий счет недоступен",
                     "передать дело: --case ДЕЛО либо $THEMIZ_CASE"))
    else:
        ws_ok = not ws_cap or ws_used < ws_cap
        rows.append(("WebSearch (квота сессии)", ws_ok,
                     f"общий счет прогона: {ws_used}" + (f" из {ws_cap}" if ws_cap else ""),
                     "channels.py ДЕЛО --show" if ws_ok
                     else "КВОТА ИСЧЕРПАНА — не звать WebSearch"))

    if a.json:
        print(json.dumps([{"channel": c, "ok": o, "note": n, "action": act}
                          for c, o, n, act in rows], ensure_ascii=False, indent=2))
    else:
        print(f"{'КАНАЛ':<28}{'СТАТУС':<10}{'ПРИМЕЧАНИЕ':<44}ДЕЙСТВИЕ")
        print("-" * 118)
        for c, o, n, act in rows:
            mark = "?" if o is None else ("OK" if o else "НЕТ")
            print(f"{c:<28}{mark:<10}{n:<44}{act}")
        print("-" * 118)

    external = search_channels(rows)
    publishers = [r for r in rows if r[0].startswith("Публикатор") and r[1]]
    diagnostics = sys.stderr if a.json else sys.stdout
    if not external and not publishers and not lynceuz_ok:
        print("\nВНЕШНИХ КАНАЛОВ НЕТ. Охоту за внешней практикой не запускать: "
              "работать по knowledge/practice_index.md и честно зафиксировать пробел.",
              file=diagnostics)
        return 1
    if not external:
        print("\nПоиск по общему запросу не подтвержден. " +
              ("Линкей доступен: пройти лестницу обнаружения на целевом источнике. "
               if lynceuz_ok else "Линкей недоступен. ") +
              "Доступность публикатора сама по себе не подтверждает работу его поиска.",
              file=diagnostics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
