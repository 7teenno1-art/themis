#!/usr/bin/env python3
"""Строгий реестр публичных источников нормативного корпуса."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from copy import deepcopy
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from update_legal_corpus import KODEKSY, SUDACT_PLENUM_CATALOG


CONFIG = Path("config/corpus-needs.json")
MAX_CONFIG_BYTES = 256 * 1024
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
ID_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,159}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
CONSULTANT_RE = re.compile(
    r"^https://www\.consultant\.ru/document/cons_doc_LAW_(\d+)/$")
PLENUM_RE = re.compile(
    r"^https://sudact\.ru/law/postanovlenie-plenuma-[a-z0-9_-]+/$")
VSRF_RE = re.compile(r"^https://(?:www\.)?vsrf\.ru/[^?#]+$")
ALLOWED_PARSERS = frozenset({
    "consultant-code", "consultant-act", "sudact-plenum", "sudact-catalog", "vsrf-review",
})
PARSER_KINDS = {
    "consultant-code": frozenset({"code"}),
    "consultant-act": frozenset({"federal-law", "bylaw"}),
    "sudact-plenum": frozenset({"plenum"}),
    "sudact-catalog": frozenset({"catalog"}),
    "vsrf-review": frozenset({"review"}),
}
OUTPUT_PREFIX = {
    "consultant-code": "knowledge/kodeksy/",
    "consultant-act": "knowledge/akty/",
    "sudact-plenum": "knowledge/plenumy/",
    "sudact-catalog": ".agent/maintenance/corpus/",
    "vsrf-review": "knowledge/obzory/",
}
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "div", "dl", "dt", "dd",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "ul",
})
DENIED_CLASS_PARTS = ("blocked-document", "snippet", "teaser", "pagination")


def _plain_string(value: object, field: str, maximum: int = 1000) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or "\x00" in value):
        raise ValueError(f"invalid {field}")
    return value


def _validate_output(value: object, parser: str) -> str:
    output = _plain_string(value, "output", 240)
    path = PurePosixPath(output)
    if (path.is_absolute() or path.as_posix() != output or "\\" in output or ".." in path.parts
            or "." in path.parts or output.startswith("/") or output.endswith("/")):
        raise ValueError("invalid output")
    if not output.startswith(OUTPUT_PREFIX[parser]):
        raise ValueError("output outside parser tree")
    suffix = ".json" if parser == "sudact-catalog" else ".md"
    if not output.endswith(suffix):
        raise ValueError("invalid output suffix")
    return output


def _validate_url(value: object, parser: str) -> tuple[str, str | None]:
    url = _plain_string(value, "url", 2048)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid url") from exc
    if (parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or parsed.query or parsed.fragment):
        raise ValueError("invalid url")
    if parser in {"consultant-code", "consultant-act"}:
        match = CONSULTANT_RE.fullmatch(url)
        if not match:
            raise ValueError("invalid Consultant URL")
        return url, match.group(1)
    if parser == "sudact-plenum":
        if not PLENUM_RE.fullmatch(url):
            raise ValueError("invalid Sudact Plenum URL")
        return url, None
    if parser == "vsrf-review":
        if not VSRF_RE.fullmatch(url):
            raise ValueError("invalid VS RF review URL")
        return url, None
    if parser == "sudact-catalog":
        if url != SUDACT_PLENUM_CATALOG:
            raise ValueError("invalid Sudact catalog URL")
        return url, None
    raise ValueError("unknown parser")


def _validate_entry(value: object, doc_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("consultant-code entry required")
    allowed = {"slug", "title", "doc_ids", "part_labels", "chapter_scope"}
    if set(value) - allowed:
        raise ValueError("unknown code entry field")
    slug = _plain_string(value.get("slug"), "entry.slug", 80)
    if not SLUG_RE.fullmatch(slug):
        raise ValueError("invalid entry.slug")
    title = _plain_string(value.get("title"), "entry.title", 500)
    doc_ids = value.get("doc_ids")
    if (not isinstance(doc_ids, list) or not doc_ids
            or any(type(item) is not int or item <= 0 for item in doc_ids)
            or len(set(doc_ids)) != len(doc_ids) or str(doc_ids[0]) != doc_id):
        raise ValueError("invalid entry.doc_ids")
    result = {"slug": slug, "title": title, "doc_ids": list(doc_ids)}
    if "part_labels" in value:
        labels = value["part_labels"]
        if (not isinstance(labels, list) or len(labels) != len(doc_ids)
                or any(not isinstance(item, str) or not item.strip() for item in labels)):
            raise ValueError("invalid entry.part_labels")
        result["part_labels"] = list(labels)
    if "chapter_scope" in value:
        scope = _plain_string(value["chapter_scope"], "entry.chapter_scope", 20)
        if not re.fullmatch(r"\d+(?:\.\d+)*", scope):
            raise ValueError("invalid entry.chapter_scope")
        result["chapter_scope"] = scope
    return result


def validate_resource(resource: object) -> dict:
    """Возвращает нормализованную копию ресурса либо отклоняет его."""
    if not isinstance(resource, dict):
        raise ValueError("resource must be an object")
    allowed = {
        "id", "kind", "parser", "url", "output", "entry", "label",
        "title", "date", "number",
    }
    if set(resource) - allowed:
        raise ValueError("unknown resource field")
    parser = _plain_string(resource.get("parser"), "parser", 40)
    if parser not in ALLOWED_PARSERS:
        raise ValueError("unknown parser")
    kind = _plain_string(resource.get("kind"), "kind", 40)
    if kind not in PARSER_KINDS[parser]:
        raise ValueError("parser/kind mismatch")
    resource_id = _plain_string(resource.get("id"), "id", 160)
    if not ID_RE.fullmatch(resource_id):
        raise ValueError("invalid id")
    url, doc_id = _validate_url(resource.get("url"), parser)
    output = _validate_output(resource.get("output"), parser)
    result = {
        "id": resource_id, "kind": kind, "parser": parser, "url": url,
        "output": output,
    }
    if parser == "consultant-code":
        result["entry"] = _validate_entry(resource.get("entry"), doc_id or "")
        if output != f"knowledge/kodeksy/{result['entry']['slug']}.md":
            raise ValueError("code output/entry mismatch")
    elif "entry" in resource:
        raise ValueError("entry allowed only for consultant-code")
    if parser in {"consultant-act", "vsrf-review"}:
        for field in ("title", "date", "number"):
            result[field] = _plain_string(resource.get(field), field, 1000)
        if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", result["date"]):
            raise ValueError("invalid date")
        try:
            datetime.strptime(result["date"], "%d.%m.%Y")
        except ValueError as exc:
            raise ValueError("invalid date") from exc
    elif any(field in resource for field in ("title", "date", "number")):
        raise ValueError("act identity allowed only for consultant-act")
    if parser == "sudact-plenum":
        result["label"] = _plain_string(resource.get("label"), "label", 1000)
    elif "label" in resource:
        raise ValueError("label allowed only for sudact-plenum")
    return deepcopy(result)


def _ensure_not_symlink(root: Path, relative: str) -> None:
    if root.is_symlink():
        raise ValueError("registry root is symlink")
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("managed path is symlink")


def _read_json(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("config is symlink")
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("config too large")
        raw = path.read_bytes()
        value = json.loads(raw)
    except FileNotFoundError:
        return {"schema": 1, "needs": [], "reviews": []}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid corpus needs config") from exc
    if (not isinstance(value, dict) or value.get("schema") != 1
            or set(value) - {"schema", "needs", "reviews"}
            or not isinstance(value.get("needs", []), list)
            or not isinstance(value.get("reviews", []), list)):
        raise ValueError("invalid corpus needs config")
    value.setdefault("needs", [])
    value.setdefault("reviews", [])
    return value


def _configured_resources(root: Path) -> list[dict]:
    path = root / CONFIG
    _ensure_not_symlink(root, CONFIG.as_posix())
    value = _read_json(path)
    resources = []
    for need in value["needs"]:
        if not isinstance(need, dict) or set(need) != {
                "source", "kind", "title", "date", "number", "slug"}:
            raise ValueError("invalid corpus need")
        slug = _plain_string(need["slug"], "slug", 80)
        if not SLUG_RE.fullmatch(slug):
            raise ValueError("invalid slug")
        resources.append(validate_resource({
            "id": f"legal-corpus:act:{slug}",
            "kind": need["kind"],
            "parser": "consultant-act",
            "url": need["source"],
            "output": f"knowledge/akty/{slug}.md",
            "title": need["title"],
            "date": need["date"],
            "number": need["number"],
        }))
    for review in value["reviews"]:
        if not isinstance(review, dict) or set(review) != {
                "source", "title", "date", "number", "slug"}:
            raise ValueError("invalid corpus review")
        slug = _plain_string(review["slug"], "slug", 80)
        if not SLUG_RE.fullmatch(slug):
            raise ValueError("invalid review slug")
        resources.append(validate_resource({
            "id": f"legal-corpus:review:{slug}", "kind": "review",
            "parser": "vsrf-review", "url": review["source"],
            "output": f"knowledge/obzory/{slug}.md", "title": review["title"],
            "date": review["date"], "number": review["number"],
        }))
    return resources


def _plenum_resources(root: Path) -> list[dict]:
    base = root / "knowledge" / "plenumy"
    if not base.exists():
        return []
    _ensure_not_symlink(root, "knowledge/plenumy")
    if not base.is_dir():
        raise ValueError("Plenum corpus is not a directory")
    result = []
    for path in sorted(base.glob("*.md")):
        if path.is_symlink():
            raise ValueError("Plenum source is symlink")
        try:
            if path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise ValueError("Plenum source too large")
            prefix = path.read_text(encoding="utf-8")[:64 * 1024]
        except OSError as exc:
            raise ValueError("cannot read Plenum source") from exc
        front = re.match(r"^---\s*\n(.*?)\n---\s*\n", prefix, re.S)
        if not front:
            continue
        source_match = re.search(
            r'(?m)^источник:\s*(?:\"([^\"]+)\"|\'([^\']+)\'|(\S+))\s*$',
            front.group(1),
        )
        heading = re.search(r"(?m)^#\s+(.+?)\s*$", prefix[front.end():])
        if not source_match or not heading:
            continue
        source = next(item for item in source_match.groups() if item is not None)
        stem = path.stem
        result.append(validate_resource({
            "id": f"legal-corpus:plenum:{stem}",
            "kind": "plenum",
            "parser": "sudact-plenum",
            "url": source,
            "output": f"knowledge/plenumy/{path.name}",
            "label": heading.group(1),
        }))
    return result


def registry(root: Path | str) -> list[dict]:
    """Собирает стабильный реестр кодексов, Пленумов, каталога и approved needs."""
    root = Path(root)
    if not root.is_absolute():
        root = root.resolve()
    resources = []
    for entry in KODEKSY:
        first = entry["doc_ids"][0]
        resources.append(validate_resource({
            "id": f"legal-corpus:code:{entry['slug']}",
            "kind": "code",
            "parser": "consultant-code",
            "url": f"https://www.consultant.ru/document/cons_doc_LAW_{first}/",
            "output": f"knowledge/kodeksy/{entry['slug']}.md",
            "entry": entry,
        }))
    resources.extend(_plenum_resources(root))
    resources.append(validate_resource({
        "id": "legal-corpus:plenum-catalog",
        "kind": "catalog",
        "parser": "sudact-catalog",
        "url": SUDACT_PLENUM_CATALOG,
        "output": ".agent/maintenance/corpus/sudact-plenum-catalog.json",
    }))
    resources.extend(_configured_resources(root))
    ids, outputs = set(), set()
    for resource in resources:
        if resource["id"] in ids or resource["output"] in outputs:
            raise ValueError("duplicate resource id or output")
        ids.add(resource["id"])
        outputs.add(resource["output"])
        _ensure_not_symlink(root, resource["output"])
    return resources


class _ConsultantDocumentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonical: list[str] = []
        self.h1_depth = 0
        self.h1_parts: list[str] = []
        self.container_stack: list[str] = []
        self.container_count = 0
        self.body_parts: list[str] = []
        self.denied = False
        self.malformed = False
        self.suppressed_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {str(key).lower(): value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonical.append(values.get("href", ""))
        if tag == "h1":
            self.h1_depth += 1
        denied_class = any(
            part in token.lower() for token in classes for part in DENIED_CLASS_PARTS)
        if self.container_stack:
            if "document__text" in classes:
                self.container_count += 1
            if denied_class:
                self.denied = True
            if tag in BLOCK_TAGS:
                self.body_parts.append("\n")
            if tag not in VOID_TAGS:
                self.container_stack.append(tag)
                if self.suppressed_depth or tag in {"script", "style"}:
                    self.suppressed_depth += 1
        elif "document__text" in classes:
            self.container_count += 1
            self.denied = self.denied or denied_class
            if tag in VOID_TAGS:
                self.malformed = True
            else:
                self.container_stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.h1_depth:
            if tag == "h1":
                self.h1_depth -= 1
        if self.container_stack:
            if tag in VOID_TAGS or self.container_stack[-1] != tag:
                self.malformed = True
                return
            if tag in BLOCK_TAGS:
                self.body_parts.append("\n")
            self.container_stack.pop()
            if self.suppressed_depth:
                self.suppressed_depth -= 1

    def handle_data(self, data):
        if self.h1_depth:
            self.h1_parts.append(data)
        if self.container_stack and not self.suppressed_depth:
            self.body_parts.append(data)


def _normal_text(parts: list[str]) -> str:
    text = html.unescape("".join(parts)).replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
    result: list[str] = []
    for line in lines:
        if line:
            result.append(line)
        elif result and result[-1] != "":
            result.append("")
    return "\n".join(result).strip()


def _identity(value: str) -> str:
    value = re.sub(r"\s*\(ред\.\s+от\s+\d{2}\.\d{2}\.\d{4}[^)]*\)", "", value,
                   flags=re.I)
    value = value.translate(str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"'}))
    return re.sub(r"\s+", " ", value).strip()


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_generic_candidate(resource: object, receipt: object,
                            now: datetime) -> tuple[str, bytes]:
    """Строит полный verified Consultant act, не принимая snippets/чужой акт."""
    resource = validate_resource(resource)
    if resource["parser"] != "consultant-act":
        raise ValueError("generic candidate requires consultant-act")
    if not isinstance(receipt, dict):
        raise ValueError("invalid receipt")
    body = receipt.get("body")
    if not isinstance(body, bytes) or not body or len(body) > MAX_DOCUMENT_BYTES:
        raise ValueError("invalid receipt body")
    revision = "sha256:" + hashlib.sha256(body).hexdigest()
    if (receipt.get("source_url") != resource["url"]
            or receipt.get("source_revision") != revision):
        raise ValueError("receipt is not bound to resource")
    if not isinstance(now, datetime):
        raise ValueError("invalid timestamp")
    try:
        page = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Consultant page is not UTF-8") from exc
    parser = _ConsultantDocumentParser()
    parser.feed(page)
    parser.close()
    expected_doc_id = CONSULTANT_RE.fullmatch(resource["url"]).group(1)
    canonical_ids = [
        match.group(1) for value in parser.canonical
        if (match := CONSULTANT_RE.fullmatch(value))
    ]
    if len(parser.canonical) != 1 or canonical_ids != [expected_doc_id]:
        raise ValueError("canonical document mismatch")
    heading = _normal_text(parser.h1_parts)
    if (_identity(resource["title"]) not in _identity(heading)
            or resource["date"] not in heading
            or not re.search(rf"\bN\s*{re.escape(resource['number'])}\b", heading)):
        raise ValueError("act identity mismatch")
    if (parser.container_count != 1 or parser.denied or parser.malformed
            or parser.container_stack):
        raise ValueError("full document__text is absent")
    text = _normal_text(parser.body_parts)
    if not re.search(r"[0-9A-Za-zА-Яа-я]", text):
        raise ValueError("full document__text is empty")
    lines = [
        "---",
        f"источник: {_yaml(resource['url'])}",
        f"название: {_yaml(resource['title'])}",
        f"дата_акта: {_yaml(resource['date'])}",
        f"номер_акта: {_yaml(resource['number'])}",
        f"canonical_doc_id: {_yaml(expected_doc_id)}",
        f"дата_выгрузки: {_yaml(now.strftime('%d.%m.%Y'))}",
        f"sha256: {_yaml(revision)}",
        "---",
        "",
        f"# {resource['title']}",
        "",
        text,
        "",
    ]
    return resource["output"], "\n".join(lines).encode("utf-8")


def build_review_candidate(resource: object, receipt: object,
                           now: datetime) -> tuple[str, bytes]:
    """Review parser disabled: `review_parser_not_verified` до отдельной приемки."""
    raise ValueError("review_parser_not_verified")


def _build_review_candidate_unverified(resource: object, receipt: object,
                                       now: datetime) -> tuple[str, bytes]:
    """Непринятый parser обзоров: сохранен только для отдельной приемки."""
    resource = validate_resource(resource)
    if resource["parser"] != "vsrf-review":
        raise ValueError("review candidate requires vsrf-review")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("body"), bytes):
        raise ValueError("invalid receipt")
    body = receipt["body"]
    revision = "sha256:" + hashlib.sha256(body).hexdigest()
    if receipt.get("source_url") != resource["url"] or receipt.get("source_revision") != revision:
        raise ValueError("receipt is not bound to resource")
    if not isinstance(now, datetime):
        raise ValueError("invalid timestamp")
    if body.startswith(b"%PDF-"):
        try:
            from pypdf import PdfReader
            import io
            pages = PdfReader(io.BytesIO(body)).pages
            text = _normal_text([page.extract_text() or "" for page in pages])
        except Exception as exc:
            raise ValueError("review PDF extraction failed") from exc
        heading = text[:2000]
        parser = None
    else:
        parser = _ConsultantDocumentParser()
        try:
            page = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("VS RF page is not UTF-8") from exc
        parser.feed(page)
        parser.close()
        heading = _normal_text(parser.h1_parts)
        text = _normal_text(parser.body_parts)
        if not heading:
            heading = resource["title"]
    if (_identity(heading) != _identity(resource["title"])
            or resource["date"] not in heading
            or resource["number"] not in heading):
        raise ValueError("review identity mismatch")
    if (parser is not None and (parser.denied or parser.malformed)) or not re.search(r"[0-9A-Za-zА-Яа-я]", text):
        raise ValueError("full review text is absent")
    if len(text) < 200:
        raise ValueError("review text is incomplete")
    checked = receipt.get("checked_at")
    if not isinstance(checked, str):
        raise ValueError("receipt checked_at missing")
    lines = ["---", f"источник: {_yaml(resource['url'])}",
             f"название: {_yaml(resource['title'])}", f"дата_акта: {_yaml(resource['date'])}",
             f"номер_акта: {_yaml(resource['number'])}", f"дата_выгрузки: {_yaml(now.strftime('%d.%m.%Y'))}",
             f"дата_проверки: {_yaml(checked)}", f"sha256: {_yaml(revision[7:])}", "---", "",
             f"# {resource['title']}", "", text, ""]
    return resource["output"], "\n".join(lines).encode("utf-8")


def _write_config(root: Path, value: dict) -> None:
    relative = CONFIG.as_posix()
    _ensure_not_symlink(root, relative)
    path = root / CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def add_need(root: Path | str, *, source: str, kind: str, title: str, date: str,
             number: str, slug: str) -> dict:
    root = Path(root).resolve()
    candidate = validate_resource({
        "id": f"legal-corpus:act:{slug}",
        "kind": kind,
        "parser": "consultant-act",
        "url": source,
        "output": f"knowledge/akty/{slug}.md",
        "title": title,
        "date": date,
        "number": number,
    })
    path = root / CONFIG
    value = _read_json(path)
    need = {
        "source": candidate["url"], "kind": candidate["kind"],
        "title": candidate["title"], "date": candidate["date"],
        "number": candidate["number"], "slug": slug,
    }
    if any(item.get("slug") == slug or item.get("source") == source
           for item in value["needs"] if isinstance(item, dict)):
        raise ValueError("need already registered")
    value["needs"].append(need)
    _write_config(root, value)
    return candidate


def add_review(root: Path | str, *, source: str, title: str, date: str,
               number: str, slug: str) -> dict:
    root = Path(root).resolve()
    candidate = validate_resource({
        "id": f"legal-corpus:review:{slug}", "kind": "review", "parser": "vsrf-review",
        "url": source, "output": f"knowledge/obzory/{slug}.md", "title": title,
        "date": date, "number": number,
    })
    path = root / CONFIG
    value = _read_json(path)
    item = {"source": candidate["url"], "title": candidate["title"],
            "date": candidate["date"], "number": candidate["number"], "slug": slug}
    if any(isinstance(row, dict) and (row.get("slug") == slug or row.get("source") == source)
           for row in value["reviews"]):
        raise ValueError("review already registered")
    value["reviews"].append(item)
    _write_config(root, value)
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--add", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--kind", choices=("federal-law", "bylaw"))
    parser.add_argument("--title")
    parser.add_argument("--date")
    parser.add_argument("--number")
    parser.add_argument("--slug")
    args = parser.parse_args(argv)
    if not args.add:
        parser.error("поддерживается только явный --add")
    fields = (args.source, args.kind, args.title, args.date, args.number, args.slug)
    if any(value is None for value in fields):
        parser.error("--add требует source/kind/title/date/number/slug")
    try:
        resource = add_need(
            args.root, source=args.source, kind=args.kind, title=args.title,
            date=args.date, number=args.number, slug=args.slug,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(resource, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
