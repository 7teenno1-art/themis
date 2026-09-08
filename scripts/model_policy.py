#!/usr/bin/env python3
"""model_policy.py — модель Codex для шага выводится из уровня дела.

Фемида работает без обязательного Claude. Политика называет только доступные модели
Codex: Luna — механика, Terra — основной исполнитель, Sol — спорный анализ, Astra —
оркестрация и приемка. Общий провайдер остается общим риском; Astra-рецензент —
независимый контур по модели и роли, не по поставщику.

    --level MICRO|L1|L2|L3 --step ШАГ   печатает модель Codex
    --brief ФАЙЛ                        сверяет таблицу ПЛАН брифа с политикой
    --selftest                          проверка без сети

Источник истины — этот исполняемый файл и Codex role TOML. Бриф сверяется с кодом,
а не с модельной памятью агента.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

LEVELS = ("MICRO", "L1", "L2", "L3")

# шаг → (модель по уровню | «-» там, где шаг на этом уровне не запускается вовсе)
POLICY = {
    # Terra составляет, Astra принимает. Общий поставщик — документированный риск.
    "draft":         {level: "gpt-5.6-terra" for level in LEVELS},
    "review":        {level: "gpt-6-astra" for level in LEVELS},
    # Охота: сбор перспектив, разнообразие углов важнее силы. На MICRO запрещена
    # триажем — тема уже покрыта practice_index.md.
    "hunt":          {"MICRO": "-", "L1": "gpt-5.6-terra", "L2": "gpt-5.6-terra", "L3": "gpt-5.6-terra"},
    "hunt-skeptic":  {"MICRO": "-", "L1": "gpt-5.6-sol", "L2": "gpt-5.6-sol", "L3": "gpt-5.6-sol"},
    # Совет спорит на Sol, принимает Astra.
    "council-role":  {"MICRO": "-", "L1": "-", "L2": "gpt-5.6-sol", "L3": "gpt-5.6-sol"},
    "council-chair": {"MICRO": "-", "L1": "-", "L2": "gpt-6-astra", "L3": "gpt-6-astra"},
    # Механика: извлечение и классификация — дешевой моделью.
    "read-text":     {level: "gpt-5.6-luna" for level in LEVELS},
    "classify":      {level: "gpt-5.6-luna" for level in LEVELS},
    # Скан-читатели: у них фолбэк на облачный vision, дешевле не ставить.
    "read-scan":     {level: "gpt-5.6-terra" for level in LEVELS},
}

# Исполнитель из брифа → шаг политики. Кого здесь нет, того политика не судит,
# но модель назвать он обязан все равно (пустая клетка = решение не принято).
AGENT_STEP = {
    # Персоны конвейера: в брифе и в чате исполнителя зовут по имени роли,
    # и политика, знающая только машинное имя, молча пропускает такую строку.
    "сперанский": "draft",
    "кони": "review",
    "спасович": "hunt",
    "плевако": "hunt",
    "карабчевский": "hunt-skeptic",
    "урусов": "council-chair",
    "покровский": "read-text",
    "гольмстен": "read-scan",
    "буринский": "read-scan",
    "мейер": "read-scan",
    "шершеневич": "hunt-skeptic",
    "рождественский": "classify",
    "грузенберг": "classify",
    "андреевский": "draft",
    "doc-drafter": "draft",
    "doc-reviewer": "review",
    "practice-hunter-classic": "hunt",
    "practice-hunter-skeptic": "hunt-skeptic",
    "practice-hunter-tactical": "hunt",
    "askacouncil": "council-role",
    "position-council": "council-role",
    "docx-reader": "read-text",
    "pdf-reader": "read-scan",
    "image-reader": "read-scan",
    "case-mapper": "read-scan",
    "case-reconciler": "hunt-skeptic",
    "archivist": "classify",
    "inbox-triage": "classify",
    "hearing-prep": "draft",
}

# Единственный runtime-профиль Codex. sync_prompts.py импортирует его, поэтому
# модель в брифе и модель role TOML не могут разойтись тихо.
CODEX_ROLE_RUNTIME = {
    "archivist": (POLICY["classify"]["L1"], "low"),
    "case-mapper": (POLICY["read-scan"]["L1"], "high"),
    "case-reconciler": (POLICY["hunt-skeptic"]["L1"], "high"),
    "doc-drafter": (POLICY["draft"]["L1"], "high"),
    "doc-reviewer": (POLICY["review"]["L1"], "xhigh"),
    "docx-reader": (POLICY["read-text"]["L1"], "low"),
    "hearing-prep": (POLICY["draft"]["L1"], "high"),
    "image-reader": (POLICY["read-scan"]["L1"], "medium"),
    "inbox-triage": (POLICY["classify"]["L1"], "low"),
    "pdf-reader": (POLICY["read-scan"]["L1"], "medium"),
    "practice-hunter-classic": (POLICY["hunt"]["L1"], "high"),
    "practice-hunter-skeptic": (POLICY["hunt-skeptic"]["L1"], "high"),
    "practice-hunter-tactical": (POLICY["hunt"]["L1"], "high"),
}


def _step_of(executor: str) -> str:
    """Шаг по исполнителю: точное имя, «Персона (агент)» и просто персона.
    Совпадение по вхождению, чтобы форма записи не решала, судить строку или нет."""
    low = executor.lower()
    for key, step in AGENT_STEP.items():
        if key == low or key in low:
            return step
    return ""


def model_for(level: str, step: str) -> str:
    """Алиас модели либо «-», если шаг на этом уровне не запускается."""
    return POLICY[step][level]


def cmd_pair(level: str, step: str) -> int:
    if level not in LEVELS:
        print(f"ERROR: уровень «{level}» неизвестен, ожидались {', '.join(LEVELS)}", file=sys.stderr)
        return 2
    if step not in POLICY:
        print(f"ERROR: шаг «{step}» неизвестен, ожидались {', '.join(sorted(POLICY))}", file=sys.stderr)
        return 2
    m = model_for(level, step)
    if m == "-":
        print(f"шаг «{step}» на уровне {level} запрещен триажем — не запускать", file=sys.stderr)
        return 1
    print(m)
    return 0


_LEVEL_RE = re.compile(r"Уровень\s*:\s*(MICRO|L1|L2|L3)", re.I)


def check_brief(path: Path) -> int:
    """Сверка плана брифа с политикой. Fail-closed: непонятно — код 1, не 0."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"ERROR: бриф не прочитан: {e}", file=sys.stderr)
        return 1
    m = _LEVEL_RE.search(text)
    if not m:
        print("ERROR: в брифе не назван уровень (строка КЛАССИФИКАЦИЯ, «Уровень: L2»). "
              "Без уровня модель шага не выводится — бриф не принят.", file=sys.stderr)
        return 1
    level = m.group(1).upper()

    rows, bad = 0, []
    canonical_header = ["Шаг", "Исполнитель", "Модель", "Прогноз"]
    header = canonical_header[:]
    executor_idx, model_idx = 1, 2
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[0].lower().startswith("шаг"):
            header = cells
            normalized = {c.strip().lower(): i for i, c in enumerate(cells)}
            if "исполнитель" not in normalized or "модель" not in normalized:
                bad.append(("шапка таблицы должна содержать «Исполнитель» и «Модель»",
                            cells))
                continue
            executor_idx = normalized["исполнитель"]
            model_idx = normalized["модель"]
            continue
        if len(cells) <= max(executor_idx, model_idx):
            continue
        rows += 1
        executor, model = cells[executor_idx], cells[model_idx].lower()
        if not model:
            bad.append((f"{executor}: колонка «Модель» пуста — решение о модели не принято",
                        cells))
            continue
        step = _step_of(executor)
        if not step:
            continue
        want = model_for(level, step)
        if want == "-":
            bad.append((f"{executor}: шаг «{step}» на уровне {level} запрещен триажем, "
                        f"а в плане стоит", cells))
        elif model != want:
            note = " (модель не соответствует назначению роли)"
            bad.append((f"{executor}: в плане «{model}», политика на {level} — «{want}»{note}",
                        cells))
    if not rows:
        print("ERROR: в брифе нет строк таблицы ПЛАН — сверять нечего, бриф не принят.",
              file=sys.stderr)
        return 1
    if bad:
        print(f"расхождений с политикой моделей ({level}): {len(bad)}", file=sys.stderr)
        print("  ожидаемая шапка таблицы: | " + " | ".join(canonical_header) + " |",
              file=sys.stderr)
        for msg, cells in bad:
            parsed = "; ".join(
                f"{header[i] if i < len(header) else f'колонка{i + 1}'}={cell or '<пусто>'}"
                for i, cell in enumerate(cells)
            )
            print("  · " + msg, file=sys.stderr)
            print("    разобранная строка: " + parsed, file=sys.stderr)
        return 1
    print(f"план брифа сходится с политикой моделей ({level}), строк: {rows}")
    return 0


def selftest() -> int:
    assert model_for("L1", "draft") == "gpt-5.6-terra"
    assert model_for("L3", "draft") == "gpt-5.6-terra"
    assert model_for("MICRO", "hunt") == "-"
    assert model_for("L3", "hunt-skeptic") == "gpt-5.6-sol", "скептик на L3 не Sol"
    assert model_for("MICRO", "hunt-skeptic") == "-", "охота-скептик на MICRO не запрещена триажем"
    assert _step_of("practice-hunter-skeptic") == "hunt-skeptic"
    assert _step_of("practice-hunter-classic") == "hunt", "рядовой охотник ушел в шаг скептика"
    for step, by_level in POLICY.items():
        assert set(by_level) == set(LEVELS), f"{step}: политика не покрывает все уровни"
        for lvl, m in by_level.items():
            assert m in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra", "-"), \
                f"{step}/{lvl}: странная модель {m}"
    for agent, step in AGENT_STEP.items():
        assert step in POLICY, f"{agent} указывает на неизвестный шаг {step}"
    assert set(CODEX_ROLE_RUNTIME) == {
        key for key in AGENT_STEP if key.isascii() and not key.startswith("askacouncil")
        and not key.startswith("position-council")
    }, "Codex roles расходятся с политикой"

    ok = """КЛАССИФИКАЦИЯ  Уровень: L1 · Трек: FAST
| Шаг | Исполнитель | Модель | Прогноз |
|---|---|---|---|
| 4 | doc-drafter | gpt-5.6-terra | 40k |
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "b.md"
        p.write_text(ok, encoding="utf-8")
        assert check_brief(p) == 0, "верный бриф отвергнут"
        p.write_text(ok.replace("gpt-5.6-terra", "gpt-5.6-sol"), encoding="utf-8")
        assert check_brief(p) == 1, "Sol на месте Terra пропущен"
        p.write_text(ok.replace("Уровень: L1 · ", ""), encoding="utf-8")
        assert check_brief(p) == 1, "бриф без уровня принят"
        p.write_text(ok.replace("| gpt-5.6-terra ", "|  "), encoding="utf-8")
        assert check_brief(p) == 1, "пустая модель принята"
        p.write_text(ok.replace("| 4 | doc-drafter | gpt-5.6-terra | 40k |",
                                "| 2 | practice-hunter-classic | gpt-5.6-terra | 60k |")
                       .replace("Уровень: L1", "Уровень: MICRO"), encoding="utf-8")
        assert check_brief(p) == 1, "охота на MICRO пропущена"
        p.write_text(ok.replace("doc-drafter", "Сперанский").replace("gpt-5.6-terra", "gpt-5.6-sol"),
                     encoding="utf-8")
        assert check_brief(p) == 1, "исполнитель-персона не узнан"
        p.write_text("""КЛАССИФИКАЦИЯ  Уровень: L1 · Трек: FAST
| Шаг | Исполнитель | Прогноз | Модель |
|---|---|---|---|
| 4 | doc-drafter | gpt-5.6-terra | gpt-5.6-sol |
""", encoding="utf-8")
        assert check_brief(p) == 1, "неверная модель в переставленной колонке Модель пропущена"
    print("selftest пройден: политика полна, бриф судится fail-closed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Модель шага по уровню дела.")
    ap.add_argument("--level", help="MICRO|L1|L2|L3")
    ap.add_argument("--step", help="|".join(sorted(POLICY)))
    ap.add_argument("--brief", help="сверить таблицу ПЛАН брифа с политикой")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.brief:
        return check_brief(Path(a.brief))
    if a.level and a.step:
        return cmd_pair(a.level.upper(), a.step)
    ap.error("нужны --level и --step, либо --brief, либо --selftest")


if __name__ == "__main__":
    sys.exit(main())
