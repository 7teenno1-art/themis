#!/usr/bin/env python3
"""Локальный Graphify-граф нормативного корпуса. Сеть и LLM не используются."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

WEEK = timedelta(days=7)
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
ALLOWED_KINDS = frozenset({"code", "plenum", "review", "federal-law", "bylaw"})
STATE_REL = Path(".agent/maintenance/legal-graph")
CURRENT = "current.json"
ARTICLE_RE = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:статья|ст\.)\s*(\d+(?:\.\d+)*(?:-\d+)?)\.?\s*(.*)$")
POINT_RE = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:(?:пункт|п\.)\s*)?(\d+(?:\.\d+)*)(?:[.)]\s*(.*)|\s*$)")
HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
ARTICLE_REF_RE = re.compile(
    r"(?iu)\b(?:статья|статьи|статье|статью|статьей|ст\.)\s*(\d+(?:\.\d+)*)")
ARTICLE_LIST_REF_RE = re.compile(
    r"(?iu)\b(?:статей|стать(?:и|ями|ях))\s*(\d+(?:\.\d+)*)(?:\s*(?:,|и|а также)\s*(\d+(?:\.\d+)*))+")
POINT_REF_RE = re.compile(
    r"(?iu)\b(?:пункт|пункта|пункте|пункту|пунктом|п\.)\s*(\d+(?:\.\d+)*)")
ACT_REF_RE = re.compile(
    r"(?iu)\b(?:гпк|апк|гк|кас|упк|коап|тк|жк|зк|ук|нк)\s*рф\b")
WORD_RE = re.compile(r"[0-9a-zа-я]+", re.I)

# Устойчивые сокращения корпуса. Они намеренно ограничены актами, которые
# действительно присутствуют в реестре, чтобы похожее слово не стало ссылкой.
CODE_ALIASES = {
    "гпк рф": "gpk-rf", "апк рф": "apk-rf", "гк рф": "gk-rf",
    "кас рф": "kas-rf", "упк рф": "upk-rf", "коап рф": "koap-rf",
    "тк рф": "tk-rf", "жк рф": "zhk-rf", "зк рф": "zk-rf",
    "ук рф": "uk-rf", "нк рф": "nk-rf-gosposhlina",
}


class SourceChanged(RuntimeError):
    """Источник изменился между чтением и публикацией snapshot."""


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _state(root: Path | str) -> tuple[Path, Path]:
    root = Path(root).resolve()
    return root, root / STATE_REL


def _current(state: Path) -> tuple[Path | None, dict | None]:
    try:
        if state.is_symlink():
            return None, None
        pointer_path = state / CURRENT
        snapshots = state / "snapshots"
        if pointer_path.is_symlink() or snapshots.is_symlink():
            return None, None
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        name = pointer["snapshot"]
        if (not isinstance(name, str) or PurePosixPath(name).parts != (name,)
                or not re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z", name)):
            return None, None
        snapshot = snapshots / name
        if snapshot.is_symlink() or not snapshot.is_dir():
            return None, None
        snapshot.resolve().relative_to(snapshots.resolve())
        report_path = snapshot / "report.json"
        if report_path.is_symlink():
            return None, None
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["checked_at"] = pointer.get("checked_at") or report.get("built_at")
        report["source_fingerprint"] = (pointer.get("source_fingerprint")
                                        or report.get("source_fingerprint"))
        return snapshot, report
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def status(root: Path | str) -> dict:
    """Статус текущего snapshot без скана нормативного корпуса."""
    _, state = _state(root)
    snapshot, report = _current(state)
    if not snapshot or not report:
        return {"status": "missing", "available": False, "due": True,
                "needs_review": True, "snapshot": None}
    checked = _parse_time(report.get("checked_at"))
    age = _now() - checked if checked else None
    due = age is None or age > WEEK or age < timedelta(0)
    return {
        "status": "due" if due else "current", "available": True, "due": due,
        "needs_review": due or bool(report.get("needs_review")),
        "snapshot": snapshot.name, "built_at": report.get("built_at"),
        "checked_at": report.get("checked_at"),
        "source_fingerprint": report.get("source_fingerprint"),
        "documents": report.get("documents", 0), "nodes": report.get("nodes", 0),
        "edges": report.get("edges", 0), "warnings": report.get("warnings", []),
        "input_tokens": 0, "output_tokens": 0,
    }


def graph_data(root: Path | str) -> dict:
    """Возвращает опубликованный нормативный граф для локального UI.

    Читается только последний атомарно опубликованный snapshot. Поэтому UI
    продолжает работать во время сборки нового графа. ``stale`` пересчитывается
    по текущим файлам и не смешивается с датой построения графа.
    """
    root, state = _state(root)
    snapshot, report = _current(state)
    if not snapshot or not report:
        return {"status": "missing", "nodes": [], "edges": [], "report": {},
                "warnings": [], "stale": True}
    try:
        index = json.loads((snapshot / "search-index.json").read_text(encoding="utf-8"))
        graph = json.loads((snapshot / "graphify-out/graph.json").read_text(encoding="utf-8"))
        manifest = json.loads((snapshot / "source-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "failed", "nodes": [], "edges": [], "report": report,
                "warnings": report.get("warnings", []), "stale": True}
    stale_resources: set[str] = set()
    for item in manifest.get("documents", []):
        try:
            path = _source_path(root, item.get("relative_path"))
            stat = path.stat()
            if _sha(path.read_bytes()) != item.get("raw_sha256"):
                stale_resources.add(item.get("resource_id"))
        except (OSError, ValueError, TypeError):
            stale_resources.add(item.get("resource_id"))
    nodes = []
    built = _parse_time(report.get("built_at"))
    graph_age = _now() - built if built else None
    graph_due = graph_age is None or graph_age > WEEK or graph_age < timedelta(0)
    for node in index.get("nodes", []):
        item = dict(node)
        item["stale"] = item.get("resource_id") in stale_resources
        checked = _parse_time(item.get("last_checked"))
        checked_age = _now() - checked if checked else None
        item["source_due"] = (checked_age is None or checked_age > WEEK
                              or checked_age < timedelta(0))
        item["needs_review"] = (bool(item.get("needs_review")) or item["stale"]
                                or item["source_due"] or graph_due)
        nodes.append(item)
    def graph_id(value: object) -> str:
        return re.sub(r"[^0-9A-Za-zА-Яа-я]+", "_", str(value)).strip("_").casefold()

    id_map = {graph_id(node.get("id")): node.get("id") for node in nodes}
    edges = []
    for edge in graph.get("links", graph.get("edges", [])):
        item = dict(edge)
        item["source"] = id_map.get(graph_id(item.get("source")), item.get("source"))
        item["target"] = id_map.get(graph_id(item.get("target")), item.get("target"))
        edges.append(item)
    needs_review = bool(stale_resources) or graph_due or any(
        node["needs_review"] for node in nodes)
    return {"status": "needs_review" if needs_review else "ok", "snapshot": snapshot.name,
            "nodes": nodes, "edges": edges, "report": report,
            "warnings": report.get("warnings", []), "stale": bool(stale_resources),
            "needs_review": needs_review, "graph_due": graph_due,
            "stale_resources": sorted(stale_resources)}


def _frontmatter(raw: bytes) -> tuple[dict[str, str], str, str]:
    text = raw.decode("utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise ValueError("frontmatter отсутствует")
    meta = {}
    for line in match.group(1).splitlines():
        item = re.match(r"^([\wа-яА-Я_-]+):\s*(.*?)\s*$", line)
        if not item:
            continue
        value = item.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[item.group(1)] = value
    return meta, text[match.end():], text


def _source_path(root: Path, output: object) -> Path:
    if not isinstance(output, str):
        raise ValueError("output отсутствует")
    rel = PurePosixPath(output)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "knowledge":
        raise ValueError("output вне knowledge/")
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("source path is symlink")
    try:
        current.resolve().relative_to((root / "knowledge").resolve())
    except ValueError as exc:
        raise ValueError("output вне knowledge/") from exc
    return current


def _read_source(root: Path, resource: dict) -> dict:
    path = _source_path(root, resource.get("output"))
    stat = path.stat()
    if not path.is_file() or stat.st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("источник отсутствует либо больше 10 МиБ")
    raw = path.read_bytes()
    meta, body, full_text = _frontmatter(raw)
    declared = meta.get("sha256", "")
    if declared.startswith("sha256:"):
        declared = declared[7:]
    body_sha = _sha(body.encode("utf-8"))
    if not re.fullmatch(r"[0-9a-f]{64}", declared) or declared != body_sha:
        raise ValueError("body sha256 отсутствует или не совпадает")
    freshness = meta.get("статус_свежести") or meta.get("source_freshness")
    last_checked = meta.get("last_checked")
    checked = _parse_time(last_checked)
    checked_age = _now() - checked if checked else None
    checked_stale = checked_age is None or checked_age > WEEK or checked_age < timedelta(0)
    return {
        "resource": resource, "path": path, "relative_path": path.relative_to(root).as_posix(),
        "raw_sha256": _sha(raw), "body_sha256": body_sha, "size": len(raw),
        "mtime_ns": stat.st_mtime_ns, "body": body, "full_text": full_text,
        "body_offset": len(full_text) - len(body),
        "source_url": resource.get("url") or meta.get("источник") or None,
        "revision": meta.get("дата_редакции") or meta.get("дата_акта") or None,
        "last_checked": last_checked,
        "needs_review": freshness != "current" or checked_stale,
    }


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _source_line(source: dict, body_pos: int) -> int:
    offsets = source.get("_newline_offsets")
    if offsets is None:
        offsets = [match.start() for match in re.finditer("\n", source["full_text"])]
        source["_newline_offsets"] = offsets
    return bisect_right(offsets, source["body_offset"] + body_pos) + 1


def _part_metadata(source: dict, pos: int) -> tuple[object, object]:
    # cite.part_meta повторно сканирует весь документ. На большом ГК это
    # превращает O(nodes) в O(nodes * document). Индекс строится один раз.
    index = source.get("_part_meta_index")
    if index is None:
        text = source.get("full_text", "")
        heads = [(m.start(), m.group(1)) for m in
                 re.finditer(r"^# (Часть [а-яе]+)\s*$", text, re.M)]
        def list_field(field: str) -> list[str]:
            match = re.search(rf"^{field}:\n((?:  - .*\n)+)", text, re.M)
            return [line[4:].strip().strip('"') for line in match.group(1).splitlines()] if match else []
        index = (heads, list_field("источник"), list_field("даты_частей"))
        source["_part_meta_index"] = index
    heads, sources, revisions = index
    part = bisect_right([item[0] for item in heads], source.get("body_offset", 0) + pos) - 1
    if part >= 0:
        return ((sources[part] if part < len(sources) else None) or source["source_url"],
                (revisions[part] if part < len(revisions) else None) or source["revision"])
    return source["source_url"], source["revision"]


def _node(node_id: str, label: str, source: dict, kind: str, text: str,
          pos: int) -> dict:
    source_url, revision = _part_metadata(source, pos)
    slug = _code_slug(source["resource"])
    aliases = sorted(alias for alias, target in CODE_ALIASES.items() if target == slug)
    return {
        "id": node_id, "label": label.strip() or node_id, "file_type": "document",
        "source_file": source["relative_path"], "kind": kind, "text": text.strip(),
        "source_location": f"L{_source_line(source, pos)}", "aliases": aliases,
        "resource_id": source["resource"]["id"], "source_url": source_url,
        "source_sha256": source["raw_sha256"], "body_sha256": source["body_sha256"],
        "revision": revision, "last_checked": source["last_checked"],
        "needs_review": source["needs_review"],
    }


def _edge(source: str, target: str, relation: str, path: str, *,
          excerpt: str | None = None, source_url: str | None = None,
          revision: str | None = None, line: int = 1) -> dict:
    edge = {"source": source, "target": target, "relation": relation,
            "confidence": "EXTRACTED", "confidence_score": 1.0,
            "source_file": path, "source_location": f"L{line}"}
    if relation == "references":
        # Доказательство хранится рядом с ребром, а не вычисляется UI на лету.
        edge.update({"evidence": {
            "excerpt": (excerpt or "").strip(), "source_url": source_url,
            "revision": revision, "source_node": source,
        }})
    return edge


def _graph_id(value: object) -> str:
    # Graphify валидирует наличие/ссылочную целостность ID, но не запрещает ':'.
    # Colon-ID уже является публичным контрактом локального UI и не нормализуется.
    return f"doc:{value}"


def _fragment(matches: list[re.Match[str]], index: int, body: str) -> str:
    """Полный фрагмент пункта до следующего пункта/статьи/конца акта."""
    start = matches[index].start()
    end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
    return body[start:end].strip()


def _extract(source: dict) -> tuple[list[dict], list[dict]]:
    resource, body = source["resource"], source["body"]
    doc_id = _graph_id(resource["id"])
    label = resource.get("label") or resource.get("title") or resource["id"]
    nodes = [_node(doc_id, label, source, "document", body, 0)]
    node_ids = {doc_id}
    edges, articles = [], []
    matches = list(ARTICLE_RE.finditer(body))
    for index, match in enumerate(matches):
        number = match.group(1)
        text = body[match.start():matches[index + 1].start() if index + 1 < len(matches) else len(body)]
        article_id = f"{doc_id}:article:{number}"
        title = f"Статья {number}" + (f". {match.group(2).strip()}" if match.group(2).strip() else "")
        nodes.append(_node(article_id, title, source, "article", text, match.start()))
        edges.append(_edge(doc_id, article_id, "contains", source["relative_path"],
                           line=_source_line(source, match.start())))
        articles.append((number, article_id, text))
        point_matches = list(POINT_RE.finditer(text[match.end() - match.start():]))
        for point_index, point in enumerate(point_matches):
            point_no = point.group(1)
            point_pos = match.end() + point.start()
            point_id = f"{article_id}:point:{point_no}"
            if point_id in node_ids:
                continue
            nodes.append(_node(point_id, f"Пункт {point_no}. {(point.group(2) or '').strip()}".rstrip(". "),
                               source, "point", _fragment(point_matches, point_index,
                                                            text[match.end() - match.start():]),
                               point_pos))
            edges.append(_edge(article_id, point_id, "contains", source["relative_path"],
                               line=_source_line(source, point_pos)))
            node_ids.add(point_id)
    article_starts = {match.start() for match in matches}
    for ordinal, heading in enumerate(HEADING_RE.finditer(body), 1):
        if heading.start() in article_starts:
            continue
        heading_id = f"{doc_id}:heading:{ordinal}"
        nodes.append(_node(heading_id, heading.group(2), source, "heading",
                           heading.group(2), heading.start()))
        edges.append(_edge(doc_id, heading_id, "contains", source["relative_path"],
                           line=_source_line(source, heading.start())))
    # Пленумы и обзоры часто состоят из пунктов без единой статьи.
    if resource.get("kind") in {"plenum", "review"}:
        point_matches = list(POINT_RE.finditer(body))
        for point_index, point in enumerate(point_matches):
            point_no = point.group(1)
            point_id = f"{doc_id}:point:{point_no}"
            if point_id in node_ids:
                continue
            fragment = _fragment(point_matches, point_index, body)
            nodes.append(_node(point_id, f"Пункт {point_no}", source, "point",
                               fragment, point.start()))
            edges.append(_edge(doc_id, point_id, "contains", source["relative_path"],
                               line=_source_line(source, point.start())))
            node_ids.add(point_id)
    unique = {(edge["source"], edge["target"], edge["relation"]): edge for edge in edges}
    return nodes, [unique[key] for key in sorted(unique)]


def _code_slug(resource: dict) -> str | None:
    entry = resource.get("entry")
    if isinstance(entry, dict) and isinstance(entry.get("slug"), str):
        return entry["slug"]
    output = resource.get("output")
    if isinstance(output, str) and output.startswith("knowledge/kodeksy/"):
        return Path(output).stem
    match = re.search(r":code:([^:]+)$", str(resource.get("id", "")))
    return match.group(1) if match else None


def _evidence_excerpt(text: str, start: int, end: int) -> str:
    """Достаточный, но компактный фрагмент с самой ссылкой."""
    left = max(text.rfind("\n", 0, start) + 1, text.rfind(".", 0, start) + 1)
    right_candidates = [(item, item == text.find(".", end))
                        for item in (text.find("\n", end), text.find(".", end)) if item >= 0]
    if right_candidates:
        right, period = min(right_candidates, key=lambda item: item[0])
        right += int(period)
    else:
        right = len(text)
    return text[left:right].strip()


def _node_line(node: dict, pos: int) -> int:
    try:
        start = int(str(node.get("source_location", "L1")).removeprefix("L"))
    except ValueError:
        start = 1
    return start + str(node.get("text", "")).count("\n", 0, pos)


def _cross_document_edges(sources: list[dict], nodes: list[dict]) -> list[dict]:
    """Извлекает только явные ссылки и связывает их после загрузки всех актов."""
    by_article: dict[tuple[str, str], str] = {}
    by_point: dict[tuple[str, str], str] = {}
    for node in nodes:
        match = re.match(r"(?iu)^статья\s+(\d+(?:\.\d+)*)", node.get("label", ""))
        if node["kind"] == "article" and match:
            by_article[(node["resource_id"], match.group(1))] = node["id"]
        elif node["kind"] == "point":
            number = node["id"].rsplit(":point:", 1)[-1]
            by_point[(node["resource_id"], number)] = node["id"]
    slug_to_resource = {_code_slug(source["resource"]): source["resource"]["id"]
                        for source in sources if _code_slug(source["resource"])}
    plenum_resources = {source["resource"]["id"] for source in sources
                        if source["resource"].get("kind") in {"plenum", "review"}}
    result = []
    for node in nodes:
        # Документный узел содержит весь акт и его заголовки; ссылки из него
        # были бы дубликатами и превращали объявления статей в self-edge.
        if node["kind"] not in {"article", "point", "heading"}:
            continue
        text = node.get("text", "")
        # Форма «ст. 131 ГПК РФ» / «пункт 5 ГПК РФ».
        combined = re.compile(
            r"(?iu)\b(?:статья|статьи|статье|статью|статьей|статей|ст\.)\s*"
            r"(\d+(?:\.\d+)*)\s+(гпк|апк|гк|кас|упк|коап|тк|жк|зк|ук|нк)\s*рф\b")
        for match in combined.finditer(text):
            slug = CODE_ALIASES[f"{match.group(2).casefold()} рф"]
            target_resource = slug_to_resource.get(slug)
            target = by_article.get((target_resource, match.group(1))) if target_resource else None
            if target and target != node["id"]:
                result.append(_edge(node["id"], target, "references", node["source_file"],
                                    excerpt=_evidence_excerpt(text, match.start(), match.end()),
                                    source_url=node.get("source_url"), revision=node.get("revision"),
                                    line=_node_line(node, match.start())))
        # «статьи 1, 2 ГПК РФ»: сохраняем каждую явно названную статью.
        for match in ARTICLE_LIST_REF_RE.finditer(text):
            tail = text[match.end():match.end() + 40]
            code = re.match(
                r"(?iu)\s+(гпк|апк|гк|кас|упк|коап|тк|жк|зк|ук|нк)\s*рф\b", tail)
            if not code:
                continue
            target_resource = slug_to_resource.get(
                CODE_ALIASES.get(f"{code.group(1).casefold()} рф"))
            numbers = re.findall(r"\d+(?:\.\d+)*", match.group(0))
            for number in numbers:
                target = by_article.get((target_resource, number)) if target_resource else None
                if target and target != node["id"]:
                    result.append(_edge(node["id"], target, "references", node["source_file"],
                                        excerpt=_evidence_excerpt(text, match.start(), match.end() + code.end()),
                                        source_url=node.get("source_url"), revision=node.get("revision"),
                                        line=_node_line(node, match.start())))
        for match in ARTICLE_REF_RE.finditer(text):
            # Без квалификатора нельзя приписывать ссылку текущему акту.
            tail = text[match.end():match.end() + 80]
            if re.match(r"(?iu)\s+настояще(?:го|й)\s+(?:кодекса|закона)", tail):
                target_resource = node["resource_id"]
            else:
                continue
            target = by_article.get((target_resource, match.group(1))) if target_resource else None
            if target and target != node["id"]:
                result.append(_edge(node["id"], target, "references", node["source_file"],
                                    excerpt=_evidence_excerpt(text, match.start(), match.end()),
                                    source_url=node.get("source_url"), revision=node.get("revision"),
                                    line=_node_line(node, match.start())))
        for match in POINT_REF_RE.finditer(text):
            tail = text[match.end():match.end() + 60]
            if (node["resource_id"] not in plenum_resources
                    or not re.match(r"(?iu)\s+настоящего\s+(?:постановления|обзора)", tail)):
                continue
            target = by_point.get((node["resource_id"], match.group(1)))
            if target and target != node["id"]:
                result.append(_edge(node["id"], target, "references", node["source_file"],
                                    excerpt=_evidence_excerpt(text, match.start(), match.end()),
                                    source_url=node.get("source_url"), revision=node.get("revision"),
                                    line=_node_line(node, match.start())))
    return result


def _snapshot_data(sources: list[dict]) -> tuple[dict, dict]:
    nodes, edges = [], []
    for source in sources:
        new_nodes, new_edges = _extract(source)
        nodes.extend(new_nodes)
        edges.extend(new_edges)
    # NetworkX оставляет последнюю запись при повторном ID; индекс обязан
    # повторять ровно ту же семантику, иначе UI покажет узел, которого нет в графе.
    nodes = list({node["id"]: node for node in nodes}.values())
    edges.extend(_cross_document_edges(sources, nodes))
    unique = {(edge["source"], edge["target"], edge["relation"]): edge for edge in edges}
    edges = [unique[key] for key in sorted(unique)]
    extraction = {"nodes": nodes, "edges": edges, "hyperedges": [],
                  "input_tokens": 0, "output_tokens": 0}
    # Graphify может канонизировать ID узлов при экспорте. Поиск и UI должны
    # получать стабильные идентификаторы, поэтому сохраняем независимую копию.
    search_index = {"schema": 1, "nodes": [dict(node) for node in nodes]}
    return extraction, search_index


def _write_search_db(path: Path, nodes: list[dict], edges: list[dict]) -> None:
    """Статический FTS5-индекс; публикуется только вместе со всем snapshot."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        with db:
            db.executescript("""
            PRAGMA synchronous=FULL;
            CREATE TABLE nodes (
                id TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                aliases TEXT NOT NULL,
                text TEXT NOT NULL,
                metadata TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                label, aliases, text,
                content='nodes', content_rowid='rowid', tokenize='unicode61'
            );
            CREATE TABLE edges (
                source TEXT NOT NULL, target TEXT NOT NULL, relation TEXT NOT NULL
            );
            CREATE INDEX edges_source ON edges(source);
            CREATE INDEX edges_target ON edges(target);
            """)
            for node in nodes:
                metadata = {key: value for key, value in node.items()
                            if key not in {"id", "label", "aliases", "text"}}
                cursor = db.execute(
                    "INSERT INTO nodes(id,label,aliases,text,metadata) VALUES(?,?,?,?,?)",
                    (node["id"], node.get("label", ""), " ".join(node.get("aliases", [])),
                     node.get("text", ""), json.dumps(metadata, ensure_ascii=False,
                                                        separators=(",", ":"))))
                db.execute("INSERT INTO nodes_fts(rowid,label,aliases,text) VALUES(?,?,?,?)",
                           (cursor.lastrowid, node.get("label", ""),
                            " ".join(node.get("aliases", [])), node.get("text", "")))
            db.executemany("INSERT INTO edges(source,target,relation) VALUES(?,?,?)",
                           ((edge["source"], edge["target"], edge.get("relation", ""))
                            for edge in edges))
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sqlite_candidates(snapshot: Path, tokens: set[str], limit: int) -> tuple[list, dict] | None:
    path = snapshot / "search.sqlite3"
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("search.sqlite3 не может быть symlink")
    path.resolve().relative_to(snapshot.resolve())
    terms = sorted(token for token in tokens if token not in {"ст", "п"})
    if not terms:
        return [], {}
    query = " AND ".join(f'"{token}"' for token in terms)
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute("""
            SELECT n.id,n.label,n.aliases,n.text,n.metadata
            FROM nodes_fts AS f JOIN nodes AS n ON n.rowid=f.rowid
            WHERE nodes_fts MATCH ?
            ORDER BY bm25(nodes_fts)
            LIMIT ?
        """, (query, min(1000, max(100, limit * 20)))).fetchall()
        nodes = []
        for node_id, label, aliases, text, metadata in rows:
            node = json.loads(metadata)
            node.update({"id": node_id, "label": label,
                         "aliases": aliases.split(), "text": text})
            nodes.append(node)
        ids = [node["id"] for node in nodes]
        neighbors: dict[str, set[str]] = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            sql = (f"SELECT source,target FROM edges WHERE source IN ({marks}) "
                   f"OR target IN ({marks})")
            for source, target in db.execute(sql, (*ids, *ids)):
                neighbors.setdefault(source, set()).add(target)
                neighbors.setdefault(target, set()).add(source)
    return nodes, neighbors


def _source_unchanged(source: dict) -> bool:
    try:
        path, before = source["path"], (source["size"], source["mtime_ns"], source["raw_sha256"])
        stat = path.stat()
        return not path.is_symlink() and (stat.st_size, stat.st_mtime_ns, _sha(path.read_bytes())) == before
    except OSError:
        return False


def _fingerprint(sources: list[dict], warnings: list[dict]) -> str:
    value = {
        "documents": sorted((source["resource"]["id"], source["raw_sha256"])
                            for source in sources),
        "warnings": sorted((str(item.get("resource_id")), str(item.get("problem")))
                           for item in warnings),
    }
    return _sha(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _build_snapshot(state: Path, sources: list[dict], warnings: list[dict], now: datetime) -> tuple[str, dict]:
    try:
        from graphify.build import build_from_json
        from graphify.cluster import cluster, score_all
        from graphify.export import to_json
    except ImportError as exc:
        raise RuntimeError("Graphify недоступен; установка автоматически не выполняется") from exc
    extraction, search_index = _snapshot_data(sources)
    # Библиотечная диагностика не должна ломать JSON-only stdout CLI.
    with contextlib.redirect_stdout(sys.stderr):
        graph = build_from_json(extraction, directed=True)
        communities = cluster(graph)
        cohesion = score_all(graph, communities)
    fingerprint = _fingerprint(sources, warnings)
    build_id = now.strftime("%Y%m%dT%H%M%S.%fZ")
    stage = Path(tempfile.mkdtemp(dir=state, prefix=".build-"))
    try:
        graph_dir = stage / "graphify-out"
        graph_dir.mkdir()
        graph_path = graph_dir / "graph.json"
        with contextlib.redirect_stdout(sys.stderr):
            exported = to_json(graph, communities, str(graph_path), force=True,
                               built_at_commit=None)
        if not exported:
            raise RuntimeError("Graphify отказал в экспорте")
        manifest = {
            "schema": 1, "built_at": _stamp(now), "source_fingerprint": fingerprint,
            "documents": [{key: source[key] for key in (
                "relative_path", "raw_sha256", "body_sha256", "size", "mtime_ns",
                "source_url", "revision", "last_checked", "needs_review")}
                | {"resource_id": source["resource"]["id"],
                   "kind": source["resource"]["kind"]} for source in sources],
            "warnings": warnings,
        }
        report = {
            "schema": 1, "scope": "legal-corpus", "built_at": _stamp(now),
            "source_fingerprint": fingerprint,
            "documents": len(sources), "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(), "communities": len(communities),
            "raw_cohesion": {str(key): value for key, value in cohesion.items()},
            "needs_review": any(source["needs_review"] for source in sources) or bool(warnings),
            "warnings": warnings, "input_tokens": 0, "output_tokens": 0,
            "html_generated": False,
        }
        _write_search_db(stage / "search.sqlite3", search_index["nodes"],
                         extraction["edges"])
        _atomic_json(stage / "search-index.json", search_index)
        _atomic_json(stage / "source-manifest.json", manifest)
        _atomic_json(stage / "report.json", report)
        for source in sources:
            if not _source_unchanged(source):
                raise SourceChanged(
                    f"источник изменился во время сборки: {source['relative_path']}")
        target = state / "snapshots" / build_id
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, target)
        _atomic_json(state / CURRENT, {"schema": 1, "snapshot": build_id,
                                      "checked_at": _stamp(now),
                                      "source_fingerprint": fingerprint})
        return build_id, report
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def refresh(root: Path | str, force: bool = False) -> dict:
    """Собрать новый snapshot; при любой ошибке оставить текущий LKG."""
    root, state = _state(root)
    current_path = root
    for part in STATE_REL.parts:
        current_path /= part
        if current_path.is_symlink():
            return {"status": "failed", "updated": False,
                    "error": "каталог legal-graph не может быть symlink"}
    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / ".lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "updated": False, "lkg": status(root)}
        current = status(root)
        if current["available"] and not current["due"] and not force:
            return {**current, "status": "current", "updated": False, "skipped": True}
        try:
            import corpus_registry
            resources = corpus_registry.registry(root)
        except Exception as exc:
            return {"status": "failed", "updated": False,
                    "error": f"registry: {type(exc).__name__}: {exc}", "lkg": current}
        sources, warnings = [], []
        for resource in resources:
            if resource.get("kind") == "catalog":
                continue
            if resource.get("kind") not in ALLOWED_KINDS:
                warnings.append({"resource_id": resource.get("id"), "problem": "kind не разрешен"})
                continue
            try:
                sources.append(_read_source(root, resource))
            except (OSError, UnicodeError, ValueError) as exc:
                warnings.append({"resource_id": resource.get("id"),
                                 "output": resource.get("output"), "problem": str(exc)})
        if not sources:
            return {"status": "failed", "updated": False,
                    "error": "нет ни одного целого зарегистрированного документа",
                    "warnings": warnings, "lkg": current}
        fingerprint = _fingerprint(sources, warnings)
        search_db = state / "snapshots" / (current.get("snapshot") or "") / "search.sqlite3"
        if (current["available"] and not force
                and current.get("source_fingerprint") == fingerprint
                and search_db.is_file() and not search_db.is_symlink()):
            checked_at = _stamp(_now())
            _atomic_json(state / CURRENT, {
                "schema": 1, "snapshot": current["snapshot"],
                "checked_at": checked_at, "source_fingerprint": fingerprint,
            })
            return {**status(root), "status": "current", "updated": False,
                    "skipped": True, "unchanged": True}
        try:
            build_id, report = _build_snapshot(state, sources, warnings, _now())
        except SourceChanged as exc:
            return {"status": "deferred", "updated": False, "error": str(exc),
                    "warnings": warnings, "lkg": current}
        except Exception as exc:
            return {"status": "unavailable" if "Graphify недоступен" in str(exc) else "failed",
                    "updated": False, "error": f"{type(exc).__name__}: {exc}",
                    "warnings": warnings, "lkg": current}
        return {"status": "updated", "updated": True, "snapshot": build_id,
                **report}


def _normalize(text: object) -> str:
    return " ".join(WORD_RE.findall(str(text).casefold().replace("ё", "е")))


def search(root: Path | str, query: str, limit: int = 10) -> dict:
    """Бесплатный Unicode full-text поиск с соседями графа и проверкой источников hits."""
    if not isinstance(query, str) or not query.strip() or not 1 <= limit <= 100:
        return {"status": "invalid", "query": query, "hits": [], "needs_review": True}
    root, state = _state(root)
    snapshot, report = _current(state)
    if not snapshot or not report:
        return {"status": "missing", "query": query, "hits": [], "needs_review": True}
    q = _normalize(query)
    tokens = set(q.split())
    article_query = re.search(r"(?iu)\b(?:статья|ст\.)\s*(\d+(?:\.\d+)*)", query)
    try:
        manifest = json.loads((snapshot / "source-manifest.json").read_text(encoding="utf-8"))
        sqlite_result = _sqlite_candidates(snapshot, tokens, limit)
        if sqlite_result is None:
            index = json.loads((snapshot / "search-index.json").read_text(encoding="utf-8"))
            graph = json.loads((snapshot / "graphify-out/graph.json").read_text(encoding="utf-8"))
            candidate_nodes = index.get("nodes", [])
            neighbors: dict[str, set[str]] = {}
            for edge in graph.get("links", graph.get("edges", [])):
                neighbors.setdefault(edge.get("source"), set()).add(edge.get("target"))
                neighbors.setdefault(edge.get("target"), set()).add(edge.get("source"))
        else:
            candidate_nodes, neighbors = sqlite_result
    except (OSError, ValueError, sqlite3.Error):
        return {"status": "failed", "query": query, "hits": [], "needs_review": True}
    scored = []
    for node in candidate_nodes:
        label, text = _normalize(node.get("label")), _normalize(node.get("text"))
        aliases = _normalize(" ".join(node.get("aliases", [])))
        haystack = f"{label} {text} {aliases}"
        matched = sum(token in haystack for token in tokens)
        if matched != len(tokens):
            continue
        score = (matched * 10 + (20 if q in label else 10 if q in text else 0)
                 + (5 if node.get("kind") in {"article", "point"} else 0))
        if (article_query and node.get("kind") == "article"
                and re.match(rf"(?iu)^статья\s+{re.escape(article_query.group(1))}\b", label)):
            score += 50
        scored.append((score, node.get("id", ""), node))
    scored.sort(key=lambda item: (-item[0], item[1]))
    documents = {item.get("resource_id"): item for item in manifest.get("documents", [])}
    verified = {}
    hits, out_of_sync = [], False
    for score, _, node in scored[:limit]:
        resource_id = node.get("resource_id")
        if resource_id not in verified:
            item = documents.get(resource_id, {})
            try:
                path = _source_path(root, item.get("relative_path"))
                verified[resource_id] = _sha(path.read_bytes()) == item.get("raw_sha256")
            except (OSError, ValueError):
                verified[resource_id] = False
        stale = not verified[resource_id]
        out_of_sync |= stale
        checked = _parse_time(node.get("last_checked"))
        checked_age = _now() - checked if checked else None
        source_due = checked_age is None or checked_age > WEEK or checked_age < timedelta(0)
        hits.append({
            "id": node.get("id"), "label": node.get("label"), "kind": node.get("kind"),
            "text": node.get("text"), "score": score,
            "resource_id": resource_id, "source_file": node.get("source_file"),
            "source_url": node.get("source_url"), "revision": node.get("revision"),
            "last_checked": node.get("last_checked"),
            "needs_review": bool(node.get("needs_review")) or stale or source_due,
            "source_due": source_due,
            "source_out_of_sync": stale,
            "neighbors": sorted(neighbors.get(node.get("id"), set())),
        })
    built = _parse_time(report.get("built_at"))
    graph_age = _now() - built if built else None
    graph_due = graph_age is None or graph_age > WEEK or graph_age < timedelta(0)
    needs_review = graph_due or out_of_sync or any(hit["needs_review"] for hit in hits)
    return {
        "status": "needs_review" if needs_review else "ok", "query": query,
        "snapshot": snapshot.name, "hits": hits, "source_out_of_sync": out_of_sync,
        "needs_review": needs_review, "graph_due": graph_due,
        "graph_age_seconds": max(0, graph_age.total_seconds()) if graph_age else None,
        "input_tokens": 0, "output_tokens": 0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Локальный граф нормативного корпуса")
    action = ap.add_mutually_exclusive_group(required=True)
    action.add_argument("--refresh", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--query")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.force and not args.refresh:
        ap.error("--force применяется только с --refresh")
    result = (refresh(args.root, args.force) if args.refresh else status(args.root)
              if args.status else search(args.root, args.query, args.limit))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.query:
            for hit in result.get("hits", []):
                mark = " [needs_review]" if hit["needs_review"] else ""
                print(f"{hit['label']}{mark}\n  {hit['source_file']}\n  {hit['text'][:500]}")
        else:
            print(result.get("status"), result.get("error", ""))
    return 0 if result.get("status") in {"ok", "current", "updated", "needs_review"} else 2


if __name__ == "__main__":
    sys.exit(main())
