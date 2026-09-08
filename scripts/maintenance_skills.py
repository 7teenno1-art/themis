#!/usr/bin/env python3
"""Проверка происхождения и свежести проектных навыков без сети.

Канон навыков хранится в ``.claude/skills``. Каталог ``.agents/skills`` —
производное зеркало; его содержимое сверяется тем же преобразованием, что и
``sync_prompts``. Этот модуль ничего не скачивает и не исполняет из SKILL.md.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import sync_prompts

MANIFEST = Path("config/skill-sources.json")
CANONICAL = Path(".claude/skills")
MIRROR = Path(".agents/skills")
ITEM_PREFIX = "skill-doc:"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _load_manifest(root: Path) -> dict:
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"manifest unavailable: {exc}") from exc
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("skills"), dict) or not data["skills"]):
        raise ValueError("manifest must contain a skills object")
    return data


def _skill_names(root: Path, manifest: dict | None = None) -> list[str]:
    names = set()
    canonical = root / CANONICAL
    if canonical.is_dir() and not canonical.is_symlink():
        names.update(path.name for path in canonical.iterdir()
                     if path.is_dir() or path.is_symlink())
    if manifest:
        names.update(name for name in manifest.get("skills", {}) if isinstance(name, str))
    return sorted(names)


def source_ids(root: Path) -> list[str]:
    """Вернуть IDs проектных копий; внешний upstream не подразумевается."""
    root = Path(root)
    try:
        manifest = _load_manifest(root)
    except ValueError:
        manifest = None
    return [ITEM_PREFIX + name for name in _skill_names(root, manifest)] or [ITEM_PREFIX + "inventory"]


def _frontmatter(raw: bytes, expected_name: str) -> None:
    text = raw.decode("utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("frontmatter missing")
    head = text[4:].split("\n---\n", 1)[0]
    values = {}
    for line in head.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("\"'")
    if values.get("name") != expected_name or not values.get("description"):
        raise ValueError("frontmatter name/description mismatch")


def _git_revision(root: Path, relative: Path, raw: bytes) -> str:
    """Доказательство локальной истории, с безопасным fallback для стенда."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%H", "--", str(relative)],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    revision = result.stdout.strip() if result else ""
    return f"git:{revision}" if revision else f"local:{_sha(raw)}"


def _item(item_id: str, status: str, checked: str | None = None,
          updated: str | None = None, detail: str = "", revision: str | None = None,
          **extra) -> dict:
    item = {
        "id": item_id, "status": status, "last_checked": checked,
        "last_updated": updated, "detail": detail, "source_revision": revision,
    }
    item.update(extra)
    return item


def _tree(path: Path) -> dict[str, bytes]:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("skill directory missing or symlink")
    files = {}
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise ValueError("skill tree contains a symlink")
        if child.is_file():
            files[child.relative_to(path).as_posix()] = child.read_bytes()
    return files


def _tree_sha(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, raw in sorted(files.items()):
        digest.update(name.encode() + b"\0" + hashlib.sha256(raw).digest())
    return "sha256:" + digest.hexdigest()


def _inspect(root: Path, name: str, checked: str) -> dict:
    item_id = ITEM_PREFIX + name
    if not SAFE_NAME.fullmatch(name):
        return _item(item_id, "needs_review", detail="unsafe skill name; provenance unknown",
                     provenance="unknown")
    try:
        manifest = _load_manifest(root)
    except ValueError as exc:
        return _item(item_id, "needs_review", detail=str(exc), provenance="unknown")

    entry = manifest.get("skills", {}).get(name)
    if not isinstance(entry, dict):
        return _item(item_id, "needs_review",
                     detail="skill is absent from provenance manifest; origin is unknown",
                     provenance="unknown")
    kind = entry.get("kind")
    if kind == "imported":
        return _item(item_id, "needs_review", detail=(
            "imported skill upstream has not been checked; approval and a pinned revision "
            "alone do not prove freshness"), provenance="imported")
    elif kind != "project-local":
        return _item(item_id, "needs_review", detail="unknown provenance kind", provenance="unknown")
    if entry.get("canonical") != (CANONICAL / name).as_posix():
        return _item(item_id, "needs_review", detail="canonical provenance declaration mismatch",
                     provenance="unknown")

    canonical = root / CANONICAL / name / "SKILL.md"
    mirror = root / MIRROR / name / "SKILL.md"
    try:
        parents = (root / ".claude", root / CANONICAL, canonical.parent,
                   root / ".agents", root / MIRROR, mirror.parent)
        if any(path.is_symlink() for path in parents) or canonical.is_symlink() or mirror.is_symlink():
            raise ValueError("skill canonical or generated mirror contains a symlink")
        raw = canonical.read_bytes()
        _frontmatter(raw, name)
        originals = _tree(canonical.parent)
        mirrored = _tree(mirror.parent)
        expected = {name: sync_prompts.to_agents(body.decode("utf-8")).encode("utf-8")
                    for name, body in originals.items()}
        if mirrored != expected:
            return _item(item_id, "blocked", detail=(
                "canonical skill and generated mirror differ; local edits preserved"),
                provenance=kind, canonical_sha256=_tree_sha(originals), mirror_sha256=_tree_sha(mirrored))
    except (OSError, UnicodeError, ValueError) as exc:
        return _item(item_id, "blocked", detail=str(exc), provenance=kind)

    return _item(item_id, "current", checked, None,
                 "project-local canonical tree and generated mirror verified; "
                 "not an upstream release or an authorship claim",
                 _tree_sha(originals), provenance=kind,
                 canonical_sha256=_tree_sha(originals), mirror_sha256=_tree_sha(mirrored),
                 git_revision=_git_revision(root, CANONICAL / name / "SKILL.md", raw))


def refresh_skills(root: Path, state_dir: Path | None = None,
                   now: datetime | None = None, online: bool = False,
                   due_ids: set[str] | None = None) -> list[dict]:
    """Проверить навыки локально; ``online`` принят для общего scheduler API.

    Даже при ``online=True`` проектный канон не подменяется внешней версией.
    Источник управления объявлен в manifest; это не утверждение об авторстве.
    """
    del state_dir, online
    root = Path(root).resolve()
    checked = (now or datetime.now()).isoformat(timespec="seconds")
    wanted = set(source_ids(root)) if due_ids is None else set(due_ids) & set(source_ids(root))
    return [_inspect(root, item_id[len(ITEM_PREFIX):], checked)
            for item_id in sorted(wanted) if item_id.startswith(ITEM_PREFIX)]


__all__ = ["source_ids", "refresh_skills"]
