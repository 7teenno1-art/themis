#!/usr/bin/env python3
"""Project-local knowledge maintenance adapters; no implicit network."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import maintenance_fetch
import maintenance_skills
import maintenance_plenums
import update_legal_corpus as corpus_updater

FIELDS = ("id", "status", "last_checked", "last_updated", "detail", "source_revision")
MAX_RUNTIME_SECONDS = 15 * 60
SOURCE_TIMEOUT_SECONDS = 120  # enforced by maintenance_fetch/Lynceuz
CORPUS_EXTRACTOR_VERSION = 2
CORPUS_RECEIPT_TTL = timedelta(days=7)
BLOCKED_FETCH_CODES = frozenset({
    "access_denied", "auth_required", "captcha", "paid_required", "paywall",
    "policy_denied", "robots_denied", "unapproved_sudact_path",
})
DOCS = {
    "technical-doc:markitdown-readme":
        "https://raw.githubusercontent.com/microsoft/markitdown/main/README.md",
    "technical-doc:pypdf-readme":
        "https://raw.githubusercontent.com/py-pdf/pypdf/main/README.md",
}


def _item(item_id, status, checked=None, updated=None, detail="", revision=None):
    return dict(zip(FIELDS, (item_id, status, checked, updated, detail, revision)))


def _sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def source_ids(root: Path) -> list[str]:
    """Inventory only: does not synchronize, write, or use the network."""
    root = Path(root)
    ids = maintenance_skills.source_ids(root)
    corpus = root / "knowledge" / "kodeksy"
    ids += [f"legal-corpus:{p.stem}" for p in sorted(corpus.glob("*.md"))]
    return ids + ["legal-corpus:plenums", *DOCS]


def _parse_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _due(state_dir: Path, now: datetime, ids: set[str]) -> set[str]:
    try:
        old = json.loads((state_dir / "sources.json").read_text(encoding="utf-8"))["sources"]
    except (OSError, ValueError, KeyError, TypeError):
        return ids
    by_id = {item.get("id"): item for item in old if isinstance(item, dict)}
    due = set()
    for item_id in ids:
        item, checked = by_id.get(item_id), None
        if item:
            checked = _parse_time(item.get("last_checked"))
        if (not item or item.get("status") not in {"current", "updated"} or not checked
                or now.replace(tzinfo=None) - checked > timedelta(days=7)):
            due.add(item_id)
    return due


def _frontmatter(raw: bytes) -> tuple[dict[str, str], str, bool]:
    text = raw.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("frontmatter missing")
    head, body = text[4:].split("\n---\n", 1)
    meta = {}
    for line in head.splitlines():
        match = re.match(r'^([^:\n]+):\s*"?([^"\n]*)"?$', line)
        if match:
            meta[match.group(1).strip()] = match.group(2).strip()
    return meta, body, bool(re.search(r"^пропущенные_статьи:\s*$", head, re.M))


def _valid_corpus(path: Path, allow_partial: bool = False) -> tuple[dict[str, str], bytes, bool]:
    raw = path.read_bytes()
    meta, body, partial = _frontmatter(raw)
    declared = meta.get("sha256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", declared):
        raise ValueError("body sha256 missing")
    if hashlib.sha256(body.encode()).hexdigest() != declared:
        raise ValueError("body sha256 mismatch; possible local change")
    if partial and not allow_partial:
        raise ValueError("partial export")
    date = meta.get("дата_редакции", "")
    if not date or "?" in date:
        raise ValueError("source revision date is not verified")
    return meta, raw, partial


@contextmanager
def _updater_stage(state_dir: Path, now: datetime, receipts: list[dict], deadline: float,
                   slug: str, cache_control: dict):
    candidates = state_dir / "candidates"
    candidates.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="corpus-", dir=candidates) as name:
        stage = Path(name)
        names = ("ROOT", "KODEKSY_DIR", "PLENUMY_DIR", "ARCHIVE_DIR", "LOG_FILE",
                 "CACHE_DIR", "TODAY", "FAILURES", "http_get")
        saved = {key: getattr(corpus_updater, key) for key in names}

        def http_get(url: str, _cache_key: str) -> bytes | None:
            try:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                article = re.search(r"/cons_doc_LAW_\d+/[a-f0-9]+/$", url)
                edition = cache_control.get("edition")
                cache = state_dir / "corpus-cache" / slug / str(edition) / hashlib.sha256(
                    url.encode()).hexdigest()
                if article and edition and cache.with_suffix(".json").is_file():
                    try:
                        proof = json.loads(cache.with_suffix(".json").read_text(encoding="utf-8"))
                        body = cache.with_suffix(".body").read_bytes()
                        checked = datetime.fromisoformat(str(proof["checked_at"]).replace("Z", "+00:00"))
                        current = now if now.tzinfo else now.replace(tzinfo=checked.tzinfo)
                        if (proof.get("source_url") == url and proof.get("edition") == edition
                                and proof.get("source_revision") == _sha(body)
                                and checked <= current and current - checked <= CORPUS_RECEIPT_TTL):
                            receipts.append({**proof, "body": body})
                            return body
                    except (OSError, ValueError, TypeError):
                        pass
                receipt = maintenance_fetch.fetch_public(url, state_dir / "fetch")
                receipts.append(receipt)
                if article and edition:
                    proof = {"source_url": url, "source_revision": receipt["source_revision"],
                             "checked_at": receipt["checked_at"], "edition": edition}
                    try:
                        _atomic(cache.with_suffix(".body"), receipt["body"])
                        _atomic(cache.with_suffix(".json"),
                                (json.dumps(proof, ensure_ascii=False) + "\n").encode())
                    except OSError:
                        pass
                return receipt["body"]
            except maintenance_fetch.MaintenanceFetchError as exc:
                corpus_updater.FAILURES.append(f"fetch:{exc.code}")
                return None
            except Exception as exc:
                corpus_updater.FAILURES.append(f"internal:{type(exc).__name__}")
                return None

        try:
            corpus_updater.ROOT = str(stage)
            corpus_updater.KODEKSY_DIR = str(stage / "kodeksy")
            corpus_updater.PLENUMY_DIR = str(stage / "plenumy")
            corpus_updater.ARCHIVE_DIR = str(stage / "archive")
            corpus_updater.LOG_FILE = str(stage / "corpus.log")
            corpus_updater.CACHE_DIR = str(stage / "cache")
            corpus_updater.TODAY = now.strftime("%d.%m.%Y")
            corpus_updater.FAILURES = []
            corpus_updater.http_get = http_get
            yield stage
        finally:
            for key, value in saved.items():
                setattr(corpus_updater, key, value)


def _receipt_proof(receipts: list[dict]) -> tuple[str, str]:
    material = "\n".join(sorted(f"{x['source_url']} {x['source_revision']}" for x in receipts))
    # A corpus is no fresher than its oldest verified source part.
    return min(str(x["checked_at"]) for x in receipts), _sha(material.encode())


def _corpus_plan_path(state_dir: Path, slug: str) -> Path:
    return state_dir / "corpus-cache" / slug / "plan.json"


def _save_corpus_plan(state_dir: Path, slug: str, edition: str,
                      links: list[tuple[int, str, str, str]], receipts: list[dict]) -> None:
    toc_urls = {f"https://www.consultant.ru/document/cons_doc_LAW_{doc_id}/" for doc_id, *_ in links}
    proof = {item.get("source_url"): item for item in receipts if isinstance(item, dict)}
    if not toc_urls or not all(url in proof for url in toc_urls):
        return
    articles = [f"https://www.consultant.ru/document/cons_doc_LAW_{doc_id}/{link_hash}/"
                for doc_id, _number, _title, link_hash in links]
    if len(articles) != len(set(articles)):
        raise ValueError("duplicate article URLs in source plan")
    record = {"schema": 1, "edition": edition, "articles": articles,
              "toc_receipts": [{"source_url": url, "source_revision": proof[url].get("source_revision"),
                                "checked_at": proof[url].get("checked_at")} for url in sorted(toc_urls)]}
    _atomic(_corpus_plan_path(state_dir, slug),
            (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())


def _corpus_progress(state_dir: Path, slug: str, edition: str | None, now: datetime) -> dict:
    unknown = {"done": None, "total": None, "pending_count": None,
               "unit": "source_pages_received"}
    if not edition:
        return unknown
    path = _corpus_plan_path(state_dir, slug)
    try:
        if path.is_symlink() or path.stat().st_size > 512 * 1024:
            return unknown
        plan = json.loads(path.read_text(encoding="utf-8"))
        articles = plan.get("articles") if isinstance(plan, dict) else None
        if (plan.get("schema") != 1 or plan.get("edition") != edition or not isinstance(articles, list)
                or not articles or len(articles) != len(set(articles))
                or any(not isinstance(url, str) for url in articles)):
            return unknown
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return unknown
    received = 0
    current = now if now.tzinfo else now.replace(tzinfo=UTC)
    for url in articles:
        key = hashlib.sha256(url.encode()).hexdigest()
        base = state_dir / "corpus-cache" / slug / edition / key
        try:
            meta_path, body_path = base.with_suffix(".json"), base.with_suffix(".body")
            if meta_path.is_symlink() or body_path.is_symlink():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body = body_path.read_bytes()
            checked = datetime.fromisoformat(str(meta["checked_at"]).replace("Z", "+00:00"))
            if (checked.tzinfo is None or checked.utcoffset() is None or checked > current
                    or current - checked > CORPUS_RECEIPT_TTL or meta.get("source_url") != url
                    or meta.get("edition") != edition or meta.get("source_revision") != _sha(body)):
                continue
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        received += 1
    return {"done": received, "total": len(articles), "pending_count": len(articles) - received,
            "unit": "source_pages_received"}


def _corpus_sources(entry: dict) -> str:
    return ", ".join(
        f"https://www.consultant.ru/document/cons_doc_LAW_{doc_id}/"
        for doc_id in entry["doc_ids"])


def _blocked_fetch_failure(failures: list[object]) -> str | None:
    for item in failures:
        if isinstance(item, str) and item.startswith("fetch:"):
            code = item.removeprefix("fetch:")
            if code in BLOCKED_FETCH_CODES:
                return code
    return None


def _activate(live: Path, candidate: Path, original: bytes | None, state_dir: Path) -> None:
    if original is None:
        if live.exists():
            raise RuntimeError("live corpus appeared during refresh")
        _atomic(live, candidate.read_bytes())
        return
    if not live.exists() or live.read_bytes() != original:
        raise RuntimeError("live corpus changed during refresh")
    backup = state_dir / "backups" / live.stem / f"{_sha(original)[7:]}.md"
    if not backup.exists():
        _atomic(backup, original)
    _atomic(live, candidate.read_bytes())


def _extraction_proof(state_dir: Path, slug: str, raw: bytes) -> bool:
    try:
        proof = json.loads((state_dir / "corpus-proofs" / slug / f"{_sha(raw)[7:]}.json").read_text())
        return (proof.get("extractor_version") == CORPUS_EXTRACTOR_VERSION
                and proof.get("corpus_sha256") == _sha(raw)
                and proof.get("coverage") == "complete"
                and proof.get("receipt_mode") == "verified-network-all"
                and bool(proof.get("source_revision")))
    except (OSError, ValueError, AttributeError):
        return False


def _corpus(path: Path, state_dir: Path, now: datetime, online: bool,
            deadline: float, *, force_rebuild: bool = False, initialize_missing: bool = False,
            original_snapshot: bytes | None = None, snapshot_exists: bool | None = None) -> dict:
    item_id = f"legal-corpus:{path.stem}"
    try:
        if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise ValueError("corpus path contains a symlink")
        meta, original, partial = _valid_corpus(path, allow_partial=True)
    except FileNotFoundError:
        if not initialize_missing:
            return _item(item_id, "blocked", detail="local corpus is absent")
        meta, original, partial = {}, None, False
    except (OSError, UnicodeError, ValueError) as exc:
        return _item(item_id, "blocked", detail=f"invalid local corpus: {exc}")
    if snapshot_exists is not None:
        if snapshot_exists != (original is not None) or original != original_snapshot:
            return _item(item_id, "blocked", detail="local corpus changed since job snapshot")
    local_revision = _sha(original) if original is not None else None
    if not online:
        return _item(item_id, "needs_review", detail=
                     ("local sha valid; partial export requires verified rebuild"
                      if partial else "local sha valid; no declared missing articles; "
                      "upstream freshness and full extraction not checked"),
                     revision=local_revision)
    entry = next((x for x in corpus_updater.KODEKSY if x["slug"] == path.stem), None)
    if not entry:
        return _item(item_id, "blocked", detail="absent from updater registry",
                     revision=local_revision)
    receipts: list[dict] = []
    cache_control = {"edition": None}
    edition = None
    try:
        def failed_from_fetch() -> dict | None:
            code = _blocked_fetch_failure(corpus_updater.FAILURES)
            if code:
                return {**_item(item_id, "blocked", detail=f"source access blocked: {code}",
                                revision=local_revision), "failure_code": code}
            return None

        def deferred_from_deadline() -> dict | None:
            if "internal:TimeoutError" not in corpus_updater.FAILURES:
                return None
            return {**_item(item_id, "deferred", detail="deadline exhausted before full corpus validation",
                            revision=local_revision),
                    "progress": _corpus_progress(state_dir, path.stem, edition, datetime.now(UTC))}

        with _updater_stage(state_dir, now, receipts, deadline, path.stem, cache_control) as stage:
            candidate = stage / "kodeksy" / path.name
            candidate.parent.mkdir(parents=True)
            if original is not None:
                candidate.write_bytes(original)
            result = corpus_updater.cmd_check_one(entry)
            if corpus_updater.FAILURES or "КАНАЛ НЕДОСТУПЕН" in result or "?" in result:
                blocked = failed_from_fetch()
                if blocked:
                    return blocked
                deferred = deferred_from_deadline()
                if deferred:
                    return deferred
                raise RuntimeError("source check failed")
            unchanged = "без изменений" in result and not partial
            if unchanged and not force_rebuild:
                checked, revision = _receipt_proof(receipts)
                if not _extraction_proof(state_dir, path.stem, original):
                    return _item(item_id, "needs_review", detail=
                                 "source edition date checked; legacy extraction has no current "
                                 "parser/body-SHA/full-coverage proof; one verified rebuild required",
                                 revision=revision)
                if path.read_bytes() != original:
                    raise RuntimeError("live corpus changed during source check")
                exported = datetime.strptime(meta["дата_выгрузки"], "%d.%m.%Y").date().isoformat()
                return {**_item(item_id, "current", checked, exported,
                                "source revision date matches verified extraction; "
                                f"source: {_corpus_sources(entry)}", revision),
                        "extractor_version": CORPUS_EXTRACTOR_VERSION}
            if "ИЗМЕНИЛОСЬ" not in result and not (
                    partial and "НЕПОЛНАЯ ВЫГРУЗКА" in result) and not unchanged and original is not None:
                raise RuntimeError("unexpected source check result")
            expected = []
            expected_links = []
            edition_dates = []
            for doc_id in entry["doc_ids"]:
                source_meta, entries = corpus_updater.fetch_toc(doc_id)
                if not source_meta:
                    raise RuntimeError("source ToC unavailable for coverage validation")
                source_date = source_meta.get("redaction_date")
                if not source_date or "?" in source_date:
                    raise RuntimeError("source edition is not verifiable")
                if entry.get("chapter_scope"):
                    entries = corpus_updater.scope_to_chapter(entries, entry["chapter_scope"])
                edition_dates.append([doc_id, source_date])
                expected += [corpus_updater.article_label(number, title)
                             for kind, number, title, _hash in entries if kind == "article"]
                expected_links += [(doc_id, number, title, link_hash)
                                   for kind, number, title, link_hash in entries if kind == "article"]
            if not expected:
                raise RuntimeError("source ToC has no verifiable articles")
            edition = _sha(json.dumps(
                edition_dates, ensure_ascii=False, separators=(",", ":")).encode())[7:]
            cache_control["edition"] = edition
            _save_corpus_plan(state_dir, path.stem, edition, expected_links, receipts)
            corpus_updater.FAILURES = []
            corpus_updater.cmd_build_one(entry, "update")
            blocked = failed_from_fetch()
            if blocked:
                return blocked
            deferred = deferred_from_deadline()
            if deferred:
                return deferred
            candidate_meta, candidate_raw, _ = _valid_corpus(candidate)
            candidate_text = candidate_raw.decode("utf-8")
            actual = re.findall(r"^### (Статья .+)$", candidate_text, re.M)
            if (corpus_updater.FAILURES or sorted(actual) != sorted(expected)
                    or candidate_raw == original and not force_rebuild):
                raise RuntimeError("candidate failed full coverage validation")
            if len(expected) != len(set(expected)):
                raise RuntimeError("duplicate article identities in source coverage")
            final_links = []
            for doc_id, edition_date in edition_dates:
                final_meta, final_entries = corpus_updater.fetch_toc(doc_id)
                if not final_meta or final_meta.get("redaction_date") != edition_date:
                    raise RuntimeError("source edition changed during rebuild")
                if entry.get("chapter_scope"):
                    final_entries = corpus_updater.scope_to_chapter(final_entries, entry["chapter_scope"])
                final_links += [(doc_id, number, title, link_hash)
                                for kind, number, title, link_hash in final_entries if kind == "article"]
            if final_links != expected_links:
                raise RuntimeError("source ToC changed during rebuild")
            checked, revision = _receipt_proof(receipts)
            proof = {"extractor_version": CORPUS_EXTRACTOR_VERSION,
                     "corpus_sha256": _sha(candidate_raw), "coverage": "complete",
                     "receipt_mode": "verified-network-all",
                     "article_count": len(actual), "source_revision": revision,
                     "last_checked": checked, "editions": edition_dates}
            proof_path = state_dir / "corpus-proofs" / path.stem / f"{_sha(candidate_raw)[7:]}.json"
            old_proof = proof_path.read_bytes() if proof_path.exists() else None
            _atomic(proof_path, (json.dumps(proof, ensure_ascii=False, indent=2) + "\n").encode())
            try:
                _activate(path, candidate, original, state_dir)
            except Exception:
                if old_proof is None:
                    proof_path.unlink(missing_ok=True)
                else:
                    _atomic(proof_path, old_proof)
                raise
            return {**_item(item_id, "updated", checked, now.date().isoformat(),
                            f"verified candidate activated; source date "
                            f"{candidate_meta['дата_редакции']}; source: "
                            f"{_corpus_sources(entry)}", revision),
                    "extractor_version": CORPUS_EXTRACTOR_VERSION,
                    "candidate_sha256": _sha(candidate_raw)}
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return _item(item_id, "failed", detail=f"refresh not activated: {exc}",
                     revision=local_revision)


def _doc(item_id: str, url: str, state_dir: Path, online: bool) -> dict:
    if not online:
        return _item(item_id, "needs_review", detail="upstream not checked")
    target = state_dir / "docs" / f"{item_id.split(':', 1)[1]}.md"
    sidecar = target.with_suffix(".json")
    try:
        initial_body = target.read_bytes() if target.exists() else None
        initial_sidecar = sidecar.read_bytes() if sidecar.exists() else None
        previous = json.loads(initial_sidecar) if initial_sidecar is not None else None
        if initial_body is not None and (not previous or _sha(initial_body) != previous.get("content_sha256")):
            raise RuntimeError("managed snapshot has local changes")
        receipt = maintenance_fetch.fetch_public(url, state_dir / "fetch")
        body = receipt["body"]
        if not body.decode("utf-8").strip():
            raise ValueError("empty or non-text README")
        current_body = target.read_bytes() if target.exists() else None
        current_sidecar = sidecar.read_bytes() if sidecar.exists() else None
        if current_body != initial_body or current_sidecar != initial_sidecar:
            raise RuntimeError("managed snapshot changed during fetch")
        changed = initial_body != body
        updated = receipt["checked_at"] if changed else previous.get("last_updated")
        record = {"source_url": receipt["source_url"], "source_revision": receipt["source_revision"],
                  "content_sha256": _sha(body), "last_checked": receipt["checked_at"],
                  "last_updated": updated, "handling": "opaque public data; not instructions"}
        metadata = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
        if changed:
            try:
                _atomic(target, body)
                _atomic(sidecar, metadata)
            except OSError:
                if initial_body is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic(target, initial_body)
                raise
        else:
            _atomic(sidecar, metadata)
        return _item(item_id, "updated" if changed else "current", receipt["checked_at"], updated,
                     "opaque public snapshot; never executed as instructions",
                     receipt["source_revision"])
    except (OSError, UnicodeError, ValueError, RuntimeError,
            maintenance_fetch.MaintenanceFetchError) as exc:
        return _item(item_id, "failed", detail=f"snapshot not changed: {exc}")


def refresh_sources(root: Path, state_dir: Path, now: datetime, online: bool,
                    due_ids: set[str] | None = None) -> list[dict]:
    """Refresh due project sources; explicit due_ids filters before side effects."""
    root, state_dir = Path(root).resolve(), Path(state_dir).resolve()
    allowed = (root / ".agent" / "maintenance").resolve()
    if state_dir != allowed and allowed not in state_dir.parents:
        raise ValueError("state_dir must stay under root/.agent/maintenance")
    known = set(source_ids(root))
    wanted = (known & set(due_ids)) if due_ids is not None else _due(state_dir, now, known)
    checked, started = now.isoformat(timespec="seconds"), time.monotonic()
    items = maintenance_skills.refresh_skills(root, state_dir, now, online, wanted)
    for item_id in sorted(wanted - {x["id"] for x in items}):
        if time.monotonic() - started >= MAX_RUNTIME_SECONDS:
            items.append(_item(item_id, "deferred", detail="15 minute maintenance budget exhausted"))
        elif item_id.startswith("legal-corpus:") and item_id != "legal-corpus:plenums":
            items.append(_corpus(root / "knowledge" / "kodeksy" / f"{item_id[13:]}.md",
                                 state_dir, now, online, started + MAX_RUNTIME_SECONDS))
        elif item_id == "legal-corpus:plenums":
            items.append(maintenance_plenums.refresh_plenums(
                root, state_dir, now, online, deadline=started + MAX_RUNTIME_SECONDS))
        elif item_id in DOCS:
            items.append(_doc(item_id, DOCS[item_id], state_dir, online))
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        previous_items = json.loads(
            (state_dir / "sources.json").read_text(encoding="utf-8")).get("sources", [])
    except (OSError, ValueError, TypeError, AttributeError):
        previous_items = []
    merged = {x.get("id"): x for x in previous_items
              if isinstance(x, dict) and x.get("id") in known}
    merged.update({x["id"]: x for x in items})
    card = json.dumps({"schema_version": 1, "checked_at": checked,
                       "sources": [merged[x] for x in sorted(merged)]},
                      ensure_ascii=False, indent=2).encode() + b"\n"
    _atomic(state_dir / "sources.json", card)
    return items
