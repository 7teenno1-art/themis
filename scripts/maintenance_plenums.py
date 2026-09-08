#!/usr/bin/env python3
"""Безопасное обновление локальных Пленумов из receipt Линкея."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import maintenance_fetch
import update_legal_corpus as corpus


ITEM_ID = "legal-corpus:plenums"
MAX_CATALOG_PAGES = 40
MAX_RUNTIME_SECONDS = 15 * 60
RESUME_MAX_AGE = timedelta(hours=24)
MAX_RESUME_META_BYTES = 4 * 1024
NEXT_PAGE_RE = re.compile(r"[?&](?:amp;)?page=(\d+)", re.I)
TERMINAL_RE = re.compile(
    r"(?:aria-disabled\s*=\s*[\"']true[\"']|class\s*=\s*[\"'][^\"']*\bdisabled\b[^\"']*[\"'])"
    r"[^>]*>[^<]*(?:следующ|next)|(?:нет\s+(?:следующ|next))", re.I)
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")


def _sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _safe_path(base: Path, path: Path) -> None:
    """Запись только ниже обычного (не symlink) root/state дерева."""
    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise ValueError("path outside managed tree") from exc
    current = base
    if current.is_symlink():
        raise ValueError("managed root is symlink")
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("managed path is symlink")


def _receipt_paths(state_dir: Path) -> set[str]:
    runs = state_dir / "fetch" / ".lynceuz" / "runs"
    if not runs.is_dir() or runs.is_symlink():
        return set()
    return {str(path.relative_to(state_dir)) for path in runs.glob("*/manifest.json")
            if path.is_file() and not path.is_symlink()}


def _resume_paths(state_dir: Path, url: str) -> tuple[Path, Path]:
    token = hashlib.sha256(url.encode("utf-8")).hexdigest()
    base = state_dir / "receipt-cache"
    return base / f"{token}.body", base / f"{token}.json"


def _resume_receipt(url: str, state_dir: Path, now: datetime | None = None) -> dict | None:
    """Возвращает лишь свежий receipt, целиком связанный с его URL и телом."""
    body_path, meta_path = _resume_paths(state_dir, url)
    try:
        _safe_path(state_dir, body_path)
        _safe_path(state_dir, meta_path)
        if (not body_path.is_file() or not meta_path.is_file() or body_path.is_symlink()
                or meta_path.is_symlink()):
            return None
        if (body_path.stat().st_size > maintenance_fetch.MAX_BODY_BYTES
                or meta_path.stat().st_size > MAX_RESUME_META_BYTES):
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return None
        body = body_path.read_bytes()
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            return None
        current = current.astimezone(UTC)
        checked = datetime.fromisoformat(str(meta["checked_at"]).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            return None
        checked = checked.astimezone(UTC)
        revision = _sha(body)
        if (not isinstance(meta, dict) or meta.get("url") != url or meta.get("source_url") != url
                or meta.get("source_revision") != revision or checked > current
                or current - checked > RESUME_MAX_AGE):
            return None
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return {"body": body, "source_url": url, "source_revision": revision,
            "checked_at": checked.isoformat().replace("+00:00", "Z")}


def _store_resume_receipt(url: str, state_dir: Path, receipt: dict) -> None:
    body = receipt["body"]
    body_path, meta_path = _resume_paths(state_dir, url)
    _safe_path(state_dir, body_path)
    _safe_path(state_dir, meta_path)
    meta = {"url": url, "source_url": url, "source_revision": _sha(body),
            "checked_at": receipt["checked_at"]}
    _atomic(body_path, body)
    _atomic(meta_path, (json.dumps(meta, sort_keys=True, separators=(",", ":")) + "\n").encode())


def _frontmatter(raw: bytes) -> tuple[dict[str, str], str]:
    text = raw.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("frontmatter missing")
    head, body = text[4:].split("\n---\n", 1)
    meta = {}
    for line in head.splitlines():
        match = re.fullmatch(r'([^:\n]+):\s*"?([^"\n]*)"?', line)
        if match:
            meta[match.group(1).strip()] = match.group(2).strip()
    return meta, body


def _valid(raw: bytes, source_url: str | None = None) -> dict[str, str]:
    meta, body = _frontmatter(raw)
    if source_url is not None and meta.get("источник") != source_url:
        raise ValueError("candidate source URL mismatch")
    if not DATE_RE.fullmatch(meta.get("дата_редакции", "")):
        raise ValueError("candidate redaction date missing")
    law = meta.get("закон", "")
    if not re.fullmatch(r"от \d{2}\.\d{2}\.\d{4} N \S+", law):
        raise ValueError("candidate law date or number missing")
    declared = meta.get("sha256", "")
    if not SHA_RE.fullmatch(declared) or hashlib.sha256(body.encode()).hexdigest() != declared:
        raise ValueError("candidate body sha256 mismatch")
    if len(body.strip()) < 200:
        raise ValueError("candidate body too short")
    return meta


def _receipt(url: str, state_dir: Path, deadline: float, receipt_paths: list[str], *, resume: bool = False) -> dict:
    if time.monotonic() >= deadline:
        raise TimeoutError("maintenance deadline exhausted")
    if resume:
        cached = _resume_receipt(url, state_dir)
        if cached is not None:
            return cached
    before = _receipt_paths(state_dir)
    try:
        receipt = maintenance_fetch.fetch_public(url, state_dir / "fetch")
    except Exception:
        receipt_paths.extend(sorted(_receipt_paths(state_dir) - before))
        raise
    receipt_paths.extend(sorted(_receipt_paths(state_dir) - before))
    body, revision = receipt.get("body"), receipt.get("source_revision")
    if (not isinstance(body, bytes) or receipt.get("source_url") != url
            or revision != _sha(body)):
        raise ValueError("fetch receipt is not bound to source")
    if resume:
        _store_resume_receipt(url, state_dir, receipt)
    return receipt


def _catalog_url(page: int) -> str:
    return corpus.SUDACT_PLENUM_CATALOG if page == 1 else f"{corpus.SUDACT_PLENUM_CATALOG}?page={page}"


def _discover(state_dir: Path, deadline: float, receipt_paths: list[str]) -> tuple[list[tuple[str, str]], dict, list[dict]]:
    """Каталог complete только при явном disabled-next/конечном маркере."""
    found, seen, pages, bodies, receipts = [], set(), [], set(), []
    for page in range(1, MAX_CATALOG_PAGES + 1):
        url = _catalog_url(page)
        receipt = _receipt(url, state_dir, deadline, receipt_paths)
        receipts.append(receipt)
        raw = receipt["body"]
        digest = _sha(raw)
        if digest in bodies:
            return found, {"status": "incomplete", "reason": "catalog_repeat", "pages": pages}, receipts
        bodies.add(digest)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("catalog is not UTF-8") from exc
        hits = corpus.PLENUM_LINK_RE.findall(text)
        added = 0
        for href, label in hits:
            if href not in seen:
                seen.add(href)
                found.append((href, label))
                added += 1
        pages.append({"page": page, "url": url, "source_revision": receipt["source_revision"],
                      "links": len(hits), "new_links": added})
        next_pages = {int(value) for value in NEXT_PAGE_RE.findall(text)}
        if page + 1 in next_pages:
            continue
        if TERMINAL_RE.search(text):
            if not found:
                return found, {"status": "incomplete", "reason": "catalog_empty", "pages": pages}, receipts
            return found, {"status": "complete", "pages": pages}, receipts
        return found, {"status": "incomplete", "reason": "catalog_end_not_proven", "pages": pages}, receipts
    return found, {"status": "incomplete", "reason": "catalog_page_limit", "pages": pages}, receipts


def _reused_catalog(catalog: tuple[list[tuple[str, str]], dict, list[dict]]) -> tuple[list[tuple[str, str]], dict, list[dict]]:
    """Принимает лишь недавний полный discovery с теми же URL и receipt SHA."""
    targets, discovery, receipts = catalog
    pages = discovery.get("pages") if isinstance(discovery, dict) else None
    if (not isinstance(targets, list) or not isinstance(pages, list) or len(pages) != len(receipts)
            or discovery.get("status") != "complete" or not pages):
        raise ValueError("reused catalog is incomplete")
    current = datetime.now(UTC)
    found, seen, bodies, actual_pages = [], set(), set(), []
    for index, receipt in enumerate(receipts, 1):
        if not isinstance(receipt, dict) or receipt.get("source_url") != _catalog_url(index):
            raise ValueError("reused catalog URL mismatch")
        body, revision = receipt.get("body"), receipt.get("source_revision")
        if not isinstance(body, bytes) or revision != _sha(body):
            raise ValueError("reused catalog receipt hash mismatch")
        try:
            checked = datetime.fromisoformat(str(receipt["checked_at"]).replace("Z", "+00:00"))
            checked = checked.astimezone(UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("reused catalog checked_at invalid") from exc
        if checked > current or current - checked > timedelta(hours=1):
            raise ValueError("reused catalog is older than one hour")
        digest = _sha(body)
        if digest in bodies:
            raise ValueError("reused catalog repeats a page")
        bodies.add(digest)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("reused catalog is not UTF-8") from exc
        hits = corpus.PLENUM_LINK_RE.findall(text)
        for href, label in hits:
            if href not in seen:
                seen.add(href)
                found.append((href, label))
        actual_pages.append({"page": index, "url": receipt["source_url"], "source_revision": revision,
                             "links": len(hits), "new_links": len(found)})
        next_pages = {int(value) for value in NEXT_PAGE_RE.findall(text)}
        if index + 1 in next_pages:
            if index == len(receipts):
                raise ValueError("reused catalog misses next page")
            continue
        if not TERMINAL_RE.search(text):
            raise ValueError("reused catalog end is not proven")
        if index != len(receipts):
            raise ValueError("reused catalog has pages after terminal")
    if found != targets:
        raise ValueError("reused catalog target mismatch")
    return targets, {"status": "complete", "pages": actual_pages}, receipts


@contextmanager
def _build_from_receipt(receipt: dict, now: datetime):
    """Переиспользует проверенный full-document parser, но не его запись на диск."""
    old_get, old_today = corpus.http_get, corpus.TODAY

    def local_get(url: str, _cache_key: str) -> bytes | None:
        return receipt["body"] if url == receipt["source_url"] else None

    corpus.http_get = local_get
    corpus.TODAY = now.astimezone(UTC).strftime("%d.%m.%Y")
    try:
        yield
    finally:
        corpus.http_get, corpus.TODAY = old_get, old_today


def _candidate(href: str, label: str, receipt: dict, now: datetime) -> tuple[str, bytes]:
    with _build_from_receipt(receipt, now):
        built = corpus.build_plenum_markdown(href, label)
    if built is None:
        parser = corpus.PlenumTextParser()
        html_text = receipt["body"].decode("utf-8", errors="ignore")
        parser.feed(html_text)
        container = "present" if corpus.PlenumTextParser.CONTENT_ID_PREFIX in html_text else "absent"
        raise ValueError(f"source body too short ({len(parser.text())} chars; content container {container})")
    name, content = built
    raw = content.encode("utf-8")
    meta = _valid(raw, receipt["source_url"])
    law = re.fullmatch(r"от (\d{2}\.\d{2}\.\d{4}) N (\S+)", meta["закон"])
    if law is None:
        raise ValueError("candidate law identity missing")
    slug_date = re.search(r"-ot-(\d{2})(\d{2})(\d{4})(?:[-_/]|$)", href)
    if slug_date and law.group(1) != ".".join(slug_date.groups()):
        raise ValueError("candidate heading date mismatches source slug")
    labeled = corpus.PLENUM_TITLE_RE.search(label)
    if labeled and (law.group(1), law.group(2)) != (labeled.group(1), labeled.group(2) or "?"):
        raise ValueError("candidate heading mismatches catalog identity")
    return name, raw


def _source_url(href: str) -> str:
    if not isinstance(href, str) or not href.startswith("/law/postanovlenie-plenuma"):
        raise ValueError("catalog link is outside approved Plenum path")
    return "https://sudact.ru" + href


def _supplemental_targets(originals: dict[str, bytes], catalog_targets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Старые акты вне текущего каталога — только по их уже подтвержденному source URL."""
    catalog_urls = {_source_url(href) for href, _label in catalog_targets}
    seen_sources, supplemental = set(), []
    for name, raw in sorted(originals.items()):
        meta, body = _frontmatter(raw)
        source = meta.get("источник", "")
        parsed = urlsplit(source)
        if (parsed.scheme != "https" or parsed.hostname != "sudact.ru" or parsed.username
                or parsed.password or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            raise ValueError("existing source is not an approved Sudact law URL")
        href = parsed.path
        if source != _source_url(href):
            raise ValueError("existing source URL is not canonical")
        expected_name = corpus.plenum_slug_to_filename(href)
        if expected_name != name:
            raise ValueError("existing source/file identity mismatch")
        if source in seen_sources:
            raise ValueError("duplicate existing source URL")
        seen_sources.add(source)
        if source not in catalog_urls:
            title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), "")
            if not title:
                raise ValueError("existing source title missing")
            supplemental.append((href, title))
    return supplemental


def _stage_dir(state_dir: Path, now: datetime) -> Path:
    base = state_dir / "candidates" / "plenums"
    token = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = base / token
    number = 1
    while path.exists():
        number += 1
        path = base / f"{token}-{number}"
    path.mkdir(parents=True)
    return path


def _activate(changed: list[dict], live_dir: Path, state_dir: Path) -> None:
    for item in changed:
        live, original = item["live"], item["original"]
        _safe_path(live_dir, live)
        if (live.read_bytes() if live.exists() else None) != original:
            raise RuntimeError(f"CAS: local corpus changed: {live.name}")
    applied = []
    try:
        for item in changed:
            live, original, candidate = item["live"], item["original"], item["candidate"]
            if (live.read_bytes() if live.exists() else None) != original:
                raise RuntimeError(f"CAS: local corpus changed: {live.name}")
            if original is not None:
                backup = state_dir / "backups" / "plenums" / _sha(original)[7:] / live.name
                _safe_path(state_dir, backup)
                if not backup.exists():
                    _atomic(backup, original)
            # Записываем намерение до replace: сбой после os.replace тоже откатывается.
            applied.append(item)
            _atomic(live, candidate)
    except Exception:
        for item in reversed(applied):
            live, original, candidate = item["live"], item["original"], item["candidate"]
            if (live.read_bytes() if live.exists() else None) == candidate:
                if original is None:
                    live.unlink(missing_ok=True)
                else:
                    _atomic(live, original)
        raise


def refresh_plenums(root: Path, state_dir: Path, now: datetime, online: bool,
                    *, deadline: float | None = None,
                    catalog: tuple[list[tuple[str, str]], dict, list[dict]] | None = None) -> dict:
    """Stage complete catalog, validate every candidate, then atomically update changed files."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        return {"id": ITEM_ID, "status": "blocked", "detail": "now must be timezone-aware"}
    root_input, state_input = Path(root), Path(state_dir)
    if root_input.is_symlink() or state_input.is_symlink():
        return {"id": ITEM_ID, "status": "blocked", "detail": "root or state path is symlink"}
    root, state_dir = root_input.resolve(), state_input.resolve()
    allowed_state = root / ".agent" / "maintenance"
    if state_dir != allowed_state and allowed_state not in state_dir.parents:
        return {"id": ITEM_ID, "status": "blocked", "detail": "state_dir outside .agent/maintenance"}
    try:
        _safe_path(root, state_dir)
    except ValueError as exc:
        return {"id": ITEM_ID, "status": "blocked", "detail": str(exc)}
    try:
        live_dir = root / "knowledge" / "plenumy"
        _safe_path(root, live_dir)
        if not live_dir.is_dir():
            return {"id": ITEM_ID, "status": "blocked", "detail": "canonical plenum corpus is absent"}
        paths = sorted(live_dir.glob("*.md"))
        if any(path.is_symlink() for path in paths):
            return {"id": ITEM_ID, "status": "blocked", "detail": "canonical corpus contains symlink"}
        originals = {path.name: path.read_bytes() for path in paths}
        for raw in originals.values():
            _valid(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        return {"id": ITEM_ID, "status": "blocked", "detail": f"invalid local corpus: {exc}"}
    if not online:
        return {"id": ITEM_ID, "status": "needs_review", "detail": "local corpus valid; upstream not checked",
                "source_revision": _sha(b"".join(sorted(originals.values())))}
    if deadline is None:
        deadline = time.monotonic() + MAX_RUNTIME_SECONDS
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        return {"id": ITEM_ID, "status": "blocked", "detail": "deadline must be finite monotonic time"}
    receipts: list[dict] = []
    receipt_paths: list[str] = []
    try:
        if catalog is None:
            targets, discovery, catalog_receipts = _discover(state_dir, deadline, receipt_paths)
        else:
            targets, discovery, catalog_receipts = _reused_catalog(catalog)
        receipts.extend(catalog_receipts)
        if discovery["status"] != "complete":
            return {"id": ITEM_ID, "status": "failed", "detail": discovery["reason"], "discovery": discovery,
                    "receipt_paths": receipt_paths}
        supplemental = _supplemental_targets(originals, targets)
        discovery = {**discovery, "supplemental_existing_not_in_catalog": len(supplemental)}
        targets = [*targets, *supplemental]
        stage_now = datetime.now(UTC)
        stage = _stage_dir(state_dir, stage_now)
        _safe_path(state_dir, stage)
        changed, staged = [], []
        names, urls = set(), set()
        local_sources = {name: _valid(raw).get("источник", "") for name, raw in originals.items()}
        for index, (href, label) in enumerate(targets):
            url = _source_url(href)
            if url in urls:
                raise ValueError("duplicate candidate source URL")
            urls.add(url)
            try:
                receipt = _receipt(url, state_dir, deadline, receipt_paths, resume=True)
                receipts.append(receipt)
                name, candidate = _candidate(href, label, receipt, stage_now)
            except (OSError, UnicodeError, ValueError, RuntimeError, maintenance_fetch.MaintenanceFetchError) as exc:
                pending = {"schema_version": 1, "status": "blocked", "failed_source_url": url,
                           "detail": str(exc), "remaining_source_urls": [
                               _source_url(rest) for rest, _label in targets[index:]],
                           "receipt_paths": receipt_paths}
                _atomic(stage / "pending.json", (json.dumps(pending, ensure_ascii=False, indent=2) + "\n").encode())
                return {"id": ITEM_ID, "status": "failed", "detail": f"refresh not activated: {exc}",
                        "stage": str(stage), "remaining": len(pending["remaining_source_urls"]),
                        "receipt_paths": receipt_paths}
            if name in names:
                raise ValueError("duplicate candidate output name")
            names.add(name)
            if name in local_sources and local_sources[name] != url:
                raise ValueError("candidate output collides with another source")
            original = originals.get(name)
            if original is not None and _valid(original).get("закон") != _valid(candidate).get("закон"):
                raise ValueError("candidate changes existing law date or number")
            live = live_dir / name
            if original != candidate:
                _safe_path(state_dir, stage / "files" / name)
                _atomic(stage / "files" / name, candidate)
                changed.append({"live": live, "original": original, "candidate": candidate})
            staged.append({"name": name, "source_url": url, "source_revision": receipt["source_revision"],
                           "content_sha256": _sha(candidate), "changed": original != candidate})
        missing = sorted(set(originals) - names)
        completed_now = datetime.now(UTC)
        manifest = {"schema_version": 1, "last_checked": completed_now.isoformat(),
                    "discovery": discovery, "candidates": staged,
                    "missing_existing": missing, "receipt_paths": receipt_paths,
                    "official_vsrf": {"verified": 0, "total": len(staged), "status": "coverage_missing"}}
        _atomic(stage / "manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
        if missing:
            return {"id": ITEM_ID, "status": "failed", "detail": "union discovery misses existing canonical files",
                    "discovery": discovery, "receipt_paths": receipt_paths, "stage": str(stage)}
        _activate(changed, live_dir, state_dir)
        material = "\n".join(sorted(f"{x['source_url']} {x['source_revision']}" for x in receipts)).encode()
        return {"id": ITEM_ID, "status": "updated" if changed else "current", "last_checked": completed_now.isoformat(),
                "last_updated": completed_now.isoformat() if changed else None,
                "changed": len(changed), "discovery": discovery, "stage": str(stage),
                "source_revision": _sha(material), "receipt_paths": receipt_paths,
                "official_vsrf": manifest["official_vsrf"]}
    except (OSError, UnicodeError, ValueError, RuntimeError, maintenance_fetch.MaintenanceFetchError) as exc:
        return {"id": ITEM_ID, "status": "failed", "detail": f"refresh not activated: {exc}",
                "receipt_paths": receipt_paths}
