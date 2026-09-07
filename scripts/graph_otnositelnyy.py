#!/usr/bin/env python3
"""Граф знаний, который едет вместе с домом: относительные пути и только
отслеживаемые файлы.

graphify пишет в graph.json абсолютные пути машины, на которой собирался. Для
своего дома это безразлично, для публичного репозитория это утечка раскладки
владельца: замер 07.09.2026 дал 2073 находки ПД-сторожа на одном graph.json.
Прибор переписывает корень дома на относительный путь и ничего больше не
трогает. Запускать после каждого `graphify update`, иначе следующая сборка
вернет абсолютные пути обратно.

    python3 scripts/graph_otnositelnyy.py [--selftest]
"""
import json
import os
import sys

KORNI = ("graphify-out/graph.json", "graphify-out/GRAPH_REPORT.md")


def otnositelno(text: str, koren: str) -> str:
    """Убрать корень дома из текста. Корень со слэшем на конце и без него."""
    return text.replace(koren + os.sep, "").replace(koren, ".")


def _pochistit(znachenie, koren: str):
    """Рекурсивно по разобранному JSON. Через текст нельзя: graph.json хранит
    кириллицу экранированной (\u041f...), и подстрока корня в нем не находится
    вовсе — замер 07.09.2026, 2073 абсолютных пути прошли мимо текстовой замены."""
    if isinstance(znachenie, str):
        return otnositelno(znachenie, koren)
    if isinstance(znachenie, list):
        return [_pochistit(x, koren) for x in znachenie]
    if isinstance(znachenie, dict):
        return {k: _pochistit(v, koren) for k, v in znachenie.items()}
    return znachenie


def _otslezhivaemye(dom: str) -> set:
    """Файлы, которые поедут с домом. Узел про файл вне git у читателя ведет в
    пустоту: замер 07.09.2026 - 24 таких узла на рабочие черновики и бэкапы."""
    import subprocess
    r = subprocess.run(["git", "-C", dom, "ls-files"], capture_output=True, text=True)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def _proseyat(graf: dict, dom: str) -> int:
    """Выбросить узлы про файлы вне дерева и повисшие связи. Возвращает число узлов."""
    est = _otslezhivaemye(dom)
    if not est:
        return 0
    def edet(n):
        put = n.get("source_file") or ""
        # Поблажки своему выводу нет: graphify-out/converted/ тоже не едет с домом,
        # и узел про конвертацию ведет читателя в пустоту так же, как любой другой.
        return (not put) or put in est
    bylo = len(graf.get("nodes", []))
    graf["nodes"] = [n for n in graf.get("nodes", []) if edet(n)]
    zhivye = {n.get("id") for n in graf["nodes"]}
    graf["links"] = [l for l in graf.get("links", [])
                     if l.get("source") in zhivye and l.get("target") in zhivye]
    if isinstance(graf.get("hyperedges"), list):
        graf["hyperedges"] = [h for h in graf["hyperedges"]
                              if all(x in zhivye for x in h.get("nodes", []))]
    return bylo - len(graf["nodes"])


def pochinit(dom: str) -> tuple[int, int]:
    """(файлов тронуто, длина корня). Идемпотентно: второй прогон ничего не меняет."""
    koren = os.path.abspath(dom)
    tronuto = 0
    for rel in KORNI:
        put = os.path.join(dom, rel)
        if not os.path.isfile(put):
            continue
        with open(put, encoding="utf-8") as f:
            bylo = f.read()
        if put.endswith(".json"):
            graf = _pochistit(json.loads(bylo), koren)
            if isinstance(graf, dict) and "nodes" in graf:
                vybrosheno = _proseyat(graf, dom)
                if vybrosheno:
                    print(f"  выброшено узлов про файлы вне дерева: {vybrosheno}")
            stalo = json.dumps(graf, ensure_ascii=False)
        else:
            stalo = otnositelno(bylo, koren)
        if stalo != bylo:
            with open(put, "w", encoding="utf-8") as f:
                f.write(stalo)
            tronuto += 1
    return tronuto, len(koren)


def selftest() -> int:
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "graphify-out"))
        koren = os.path.abspath(tmp)
        put = os.path.join(tmp, "graphify-out", "graph.json")
        with open(put, "w", encoding="utf-8") as f:
            json.dump({"nodes": [{"source_file": f"{koren}/scripts/x.py"}]}, f)
        pochinit(tmp)
        with open(put, encoding="utf-8") as f:
            stalo = f.read()
        if koren in stalo:
            fails.append("абсолютный путь уцелел после починки")
        if "scripts/x.py" not in stalo:
            fails.append("относительный путь потерян")
        # идемпотентность: второй прогон ничего не меняет
        pochinit(tmp)
        with open(put, encoding="utf-8") as f:
            povtorno = f.read()
        if povtorno != stalo:
            fails.append("второй прогон меняет файл — прибор не идемпотентен")
    for f in fails:
        print(f"  ✗ {f}")
    print(f"selftest {'пройден' if not fails else 'ПРОВАЛЕН'}: {3 - len(fails)}/3")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    dom = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    n, dlina = pochinit(dom)
    print(f"граф переписан на относительные пути: файлов {n}, корень {dlina} знаков")
