# Agent Instructions

The rules for this repository live in [AGENTS.md](AGENTS.md). Read that file first, follow it exactly, and do not duplicate its rules here.

## Онбординг разговором

Свежий клон объясняет себя сам: `docs/ONBOARDING-CHAT.ru.md`, по-английски `docs/ONBOARDING-CHAT.md`, по-китайски `docs/ONBOARDING-CHAT.zh.md`. Веди установку по нему шагами и называй цену шага до того, как его исполнить.

## Граф знаний

Дом уже разобран в граф: `graphify-out/graph.json` и `graphify-out/GRAPH_REPORT.md`. Ходи по нему вместо чтения файлов подряд, это бесплатная память дома. Обновляется двумя командами подряд: `graphify update .`, затем
`python3 scripts/graph_otnositelnyy.py .` — вторая убирает из графа абсолютные
пути машины сборки, иначе ПД-сторож не пустит его в коммит.
