#!/usr/bin/env python3
"""Private runtime for the local extraction and transcription tools.

This module is deliberately stdlib-only.  It never installs anything on import;
the upgrade command is the only path that creates a release or invokes pip.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import venv

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux have fcntl
    fcntl = None


CURRENT = "current"
PREVIOUS = "previous"
RELEASES = "releases"
DIRECT_PACKAGES = {
    "markitdown": "markitdown",
    "pypdf": "pypdf",
    "pypdfium2": "pypdfium2",
    "pillow": "Pillow",
    "python-docx": "python-docx",
    "openai-whisper": "openai-whisper",
}
OPTIONAL_PACKAGES = {"pymupdf": "PyMuPDF"}
PACKAGE_NAMES = {**DIRECT_PACKAGES, **OPTIONAL_PACKAGES}
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+!-]*$")


def project_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Return the repository root for a script in ``scripts/``."""
    path = Path(start or __file__).resolve()
    return path.parent.parent if path.parent.name == "scripts" else path


def _runtime_dir(root: Path) -> Path:
    return root / ".agent" / "extraction-runtime"


def _python_in(release: Path) -> Path:
    for name in ("python", "python3"):
        candidate = release / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return release / "bin" / "python"


def _current_release(root: Path) -> Path | None:
    pointer = _runtime_dir(root) / CURRENT
    if not pointer.is_symlink():
        if pointer.exists():
            raise RuntimeError(f"managed runtime pointer не symlink: {pointer}")
        return None
    try:
        release = pointer.resolve(strict=True)
    except OSError:
        raise RuntimeError(f"managed runtime pointer поврежден: {pointer}")
    releases = (_runtime_dir(root) / RELEASES).resolve()
    try:
        release.relative_to(releases)
    except ValueError:
        raise RuntimeError(f"managed runtime pointer выходит из releases: {pointer}")
    return release


def _runtime_python(root: Path) -> Path | None:
    override = os.environ.get("THEMIZ_EXTRACTION_PYTHON", "").strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError("THEMIZ_EXTRACTION_PYTHON указывает не на исполняемый Python")
        return candidate
    release = _current_release(root)
    if release is None:
        return None
    candidate = _python_in(release)
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError(f"managed runtime поврежден: нет {candidate}")
    return candidate


def _venv_root(python: Path) -> Path:
    # Do not resolve the executable itself: venv/bin/python is commonly a
    # symlink to the framework interpreter.  sys.prefix is the source of truth.
    return python.parent.parent


def _prepend_runtime_bin(python: Path) -> None:
    bindir = str(python.parent)
    path = os.environ.get("PATH", "").split(os.pathsep) if os.environ.get("PATH") else []
    if bindir not in path:
        os.environ["PATH"] = os.pathsep.join([bindir, *path])


def ensure_runtime(root: str | os.PathLike[str] | None = None,
                   entrypoint: str | os.PathLike[str] | None = None) -> Path | None:
    """Re-exec the current entrypoint under the managed extraction Python.

    With no activated release this is a no-op, preserving the existing install
    until the first approved runtime upgrade.  An explicit bad override fails
    closed instead of silently falling back to global Python.
    """
    script = Path(entrypoint or sys.argv[0] or __file__).resolve()
    root_path = Path(root).resolve() if root else project_root(script)
    target = _runtime_python(root_path)
    if target is None:
        return None
    _prepend_runtime_bin(target)
    expected_prefix = _venv_root(target).resolve()
    if Path(sys.prefix).resolve() == expected_prefix:
        return target
    if not (_venv_root(target) / "pyvenv.cfg").is_file():
        raise RuntimeError(f"managed runtime не является venv: {_venv_root(target)}")
    os.execv(str(target), [str(target), str(script), *sys.argv[1:]])
    raise AssertionError("execv unexpectedly returned")


def _probe_python(python: Path) -> dict:
    names = list(PACKAGE_NAMES.values())
    code = (
        "import importlib.metadata as m, json, sys\n"
        f"names={names!r}\n"
        "from importlib.metadata import PackageNotFoundError\n"
        "found = {}\n"
        "for n in names:\n"
        "    try: found[n.lower()] = m.version(n)\n"
        "    except PackageNotFoundError: found[n.lower()] = None\n"
        "print(json.dumps({'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
        "'packages': {n: found.get(n.lower()) for n in names}}, ensure_ascii=False))"
    )
    try:
        result = subprocess.run([str(python), "-c", code], capture_output=True,
                                text=True, timeout=20, check=False)
        if result.returncode:
            return {"error": (result.stderr or result.stdout).strip()[-500:]}
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"error": str(exc)}


def _host_status(root: Path) -> dict:
    vision = root / "bin" / "vision-doc"
    ffmpeg = shutil.which("ffmpeg")
    ffmpeg_version = None
    if ffmpeg:
        try:
            result = subprocess.run([ffmpeg, "-version"], capture_output=True,
                                    text=True, timeout=5, check=False)
            ffmpeg_version = (result.stdout or result.stderr).splitlines()[0] if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "vision_doc": {"path": str(vision), "executable": vision.is_file() and os.access(vision, os.X_OK)},
        "ffmpeg": {"path": ffmpeg, "version": ffmpeg_version},
        "managed_by": "host (not auto-upgraded)",
    }


def _result(status: str, detail: str, current: dict | None = None, **extra) -> dict:
    current = current or {}
    result = {
        "status": status,
        "detail": detail,
        "packages": current.get("packages", {}),
        "python": current.get("python", str(Path(sys.executable))),
        "native": current.get("native", current.get("host", {})),
    }
    result.update(extra)
    return result


def inventory(root: str | os.PathLike[str]) -> dict:
    """Describe the active private runtime without installing or importing it."""
    root_path = Path(root).resolve()
    runtime_dir = _runtime_dir(root_path)
    pointer = runtime_dir / CURRENT
    release = _current_release(root_path)
    # Status is about the activated pointer, not a caller's temporary override.
    python = _python_in(release) if release else Path(sys.executable)
    probe = _probe_python(python) if python else _probe_python(Path(sys.executable))
    manifest = {}
    if release:
        try:
            manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    system_site = False
    if release:
        try:
            cfg = (release / "pyvenv.cfg").read_text(encoding="utf-8").lower()
            system_site = "include-system-site-packages = true" in cfg
        except OSError:
            pass
    packages = {}
    for canonical, dist in PACKAGE_NAMES.items():
        packages[canonical] = (probe.get("packages") or {}).get(dist)
    missing = [name for name in DIRECT_PACKAGES if packages.get(name) is None]
    native = _host_status(root_path)
    return {
        "root": str(root_path),
        "pointer": str(pointer),
        "active": release is not None,
        "release": str(release) if release else None,
        "python": str(python) if python else str(Path(sys.executable)),
        "prefix": probe.get("prefix"),
        "base_prefix": probe.get("base_prefix"),
        "packages": packages,
        "system_site_packages": system_site,
        "manifest": manifest,
        "native": native,
        "host": native,
        "error": probe.get("error") or ("missing required packages: " + ", ".join(missing) if missing else None),
    }


def _normalise_versions(versions: dict[str, str], current: dict) -> dict[str, str]:
    if not isinstance(versions, dict):
        raise ValueError("versions должен быть JSON-объектом")
    normal = {str(k).lower().replace("_", "-"): str(v) for k, v in versions.items()}
    unknown = sorted(set(normal) - set(PACKAGE_NAMES))
    missing = sorted(set(DIRECT_PACKAGES) - set(normal))
    if unknown or missing:
        raise ValueError(f"allowlist: неизвестные={unknown}, отсутствуют={missing}")
    if "pymupdf" in normal and not current.get("packages", {}).get("pymupdf"):
        raise ValueError("pymupdf разрешен только как уже установленный fallback")
    for name, version in normal.items():
        if not VERSION_RE.fullmatch(version):
            raise ValueError(f"небезопасная exact-версия {name}={version!r}")
    return normal


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=False, **kwargs)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _atomic_pointer(path: Path, target: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    os.symlink(os.path.relpath(target, path.parent), temporary)
    os.replace(temporary, path)


@contextmanager
def _upgrade_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _synthetic_docx(path: Path) -> None:
    import zipfile
    files = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Femida extraction smoke</w:t></w:r></w:p></w:body></w:document>',
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)


def _synthetic_pdf(path: Path) -> None:
    body = "BT /F1 14 Tf 72 720 Td (Femida extraction smoke verifies text PDF routing) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(body)} >>\nstream\n{body}\nendstream".encode(),
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)


def _smoke(root: Path, python: Path) -> dict:
    """Exercise the real router on private synthetic text/PDF/office/image files."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="themiz-extraction-smoke-") as td:
        work = Path(td)
        html = work / "sample.html"
        html.write_text("<p>Femida extraction smoke</p>", encoding="utf-8")
        pdf = work / "sample.pdf"
        _synthetic_pdf(pdf)
        scan_pdf = work / "scan.pdf"
        docx = work / "sample.docx"
        _synthetic_docx(docx)
        image = work / "sample.png"
        script = root / "scripts" / "markdown_extract.py"
        cache_home = work / "home"
        cache_home.mkdir()
        env = {**os.environ, "HOME": str(cache_home),
               "THEMIZ_EXTRACTION_PYTHON": str(python),
               "PATH": str(python.parent) + os.pathsep + os.environ.get("PATH", "")}
        env["THEMIZ_STT_CMD"] = str(python.parent / "whisper")
        image_build = _run([str(python), "-c",
                            "from PIL import Image, ImageDraw, ImageFont; "
                            "i=Image.new('RGB',(1800,300),'white'); "
                            "f=ImageFont.load_default(size=40); "
                            "ImageDraw.Draw(i).text((40,110),'FEMIDA EXTRACTION SMOKE',font=f,fill='black'); "
                            "i.save(__import__('sys').argv[1])", str(image)],
                           cwd=str(root), capture_output=True, text=True, timeout=30, env=env)
        if image_build.returncode:
            return {"ok": False, "all_green": False,
                    "error": "candidate Pillow не создал image fixture"}
        scan_build = _run([str(python), "-c",
                           "from reportlab.pdfgen.canvas import Canvas; "
                           "from reportlab.lib.utils import ImageReader; "
                           "import sys; c=Canvas(sys.argv[2],pagesize=(900,180)); "
                           "c.drawImage(ImageReader(sys.argv[1]),0,0,width=900,height=180); c.save()",
                           str(image), str(scan_pdf)], cwd=str(root), capture_output=True,
                          text=True, timeout=30, env=env)
        if scan_build.returncode:
            return {"ok": False, "all_green": False,
                    "error": "candidate reportlab не создал scan PDF fixture"}
        checks = {}
        for label, source in (("text", html), ("pdf", pdf), ("scan_pdf", scan_pdf),
                              ("office", docx), ("image", image)):
            command = [str(python), str(script), str(source), "--inline"]
            if label in ("image", "scan_pdf"):
                command.extend(["--render-dir", str(work / "ocr")])
            result = _run(command,
                          cwd=str(root), capture_output=True, text=True, timeout=120,
                          env=env)
            output = result.stdout + result.stderr
            if label in ("text", "office"):
                content_ok = "Femida" in output
            elif label == "pdf":
                content_ok = "Femida extraction smoke verifies text PDF routing" in output
            else:
                ocr_text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                                      for p in (work / "ocr").glob("*.txt")) if (work / "ocr").is_dir() else ""
                content_ok = "FEMIDA" in (output + ocr_text).upper() and "SMOKE" in (output + ocr_text).upper()
            checks[label] = {
                "ok": result.returncode == 0 and content_ok,
                "returncode": result.returncode,
            }
        core_ok = all(item["ok"] for item in checks.values())
        audio = {"status": "blocked", "reason": "локальная модель Whisper не подтверждена"}
        model_dirs = [str(Path(d).expanduser()) for d in
                      (os.environ.get("WHISPER_MODEL_DIR", ""),
                       str(Path.home() / ".cache" / "whisper"),
                       str(Path.home() / ".cache" / "whisper.cpp")) if d]
        small_models = [Path(d) / "small.pt" for d in model_dirs if d]
        has_model = any(p.is_file() for p in small_models)
        say = shutil.which("say")
        if shutil.which("whisper", path=env["PATH"]) and has_model and say:
            env["WHISPER_MODEL_DIR"] = str(next(p.parent for p in small_models if p.is_file()))
            wav = work / "sample.wav"
            voices = _run([say, "-v", "?"], capture_output=True, text=True, timeout=15)
            voice = next((parts[0] for parts in ((line.split()) for line in
                          (voices.stdout + voices.stderr).splitlines())
                          if parts and (parts[0].lower() in {"milena", "yuri"}
                                        or any(token.lower().startswith("ru_") for token in parts))), None)
            if not voice:
                audio = {"status": "blocked", "reason": "русский голос macOS не найден"}
                return {"ok": core_ok, "all_green": False, "checks": checks, "audio": audio}
            spoken = _run([say, "-v", voice, "-o", str(wav), "--data-format=LEI16@16000",
                           "Проверка распознавания. Номер дела сорок два."],
                          capture_output=True, text=True, timeout=30)
            if spoken.returncode:
                return {"ok": False, "all_green": False, "checks": checks,
                        "audio": {"status": "failed", "reason": "say не создал synthetic audio"}}
            result = _run([str(python), str(root / "scripts" / "voice_local.py"),
                           "--transcribe", str(wav), "--language", "ru", "--json"], cwd=str(root),
                          capture_output=True, text=True, timeout=1800, env=env)
            try:
                transcript = json.loads(result.stdout).get("text", "") if result.stdout else ""
            except ValueError:
                transcript = ""
            transcript_lower = transcript.lower()
            audio = {"status": "passed" if result.returncode == 0 and "провер" in transcript_lower
                     and (("сорок" in transcript_lower and "два" in transcript_lower)
                          or "42" in transcript_lower) else "failed",
                     "returncode": result.returncode,
                     "text": transcript[:200],
                     "detail": (result.stderr or result.stdout).strip()[-300:]}
        smoke_ok = core_ok and audio["status"] != "failed"
        return {"ok": smoke_ok, "all_green": core_ok and audio["status"] == "passed",
                "checks": checks, "audio": audio}


def _upgrade_locked(root: str | os.PathLike[str], versions: dict[str, str], *, runner=None,
                    smoke=None, now: str | None = None) -> dict:
    """Build, smoke-test and atomically activate an exact private release."""
    root_path = Path(root).resolve()
    try:
        current = inventory(root_path)
    except (OSError, RuntimeError) as exc:
        return _result("failed", str(exc), activated=False)
    try:
        pins = _normalise_versions(versions, current)
    except (TypeError, ValueError) as exc:
        return _result("blocked", str(exc), current, activated=False)
    if current.get("active") and all(current["packages"].get(name) == version
                                     for name, version in pins.items()):
        return _result("current", "exact pins уже активны", current, activated=False)
    runner = runner or _run
    smoke = smoke or _smoke
    runtime_dir = _runtime_dir(root_path)
    releases = runtime_dir / RELEASES
    releases.mkdir(parents=True, exist_ok=True)
    release_id = (now or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")) + f"-{os.getpid()}"
    release = releases / release_id
    while release.exists():
        release_id += "-1"
        release = releases / release_id
    builder = venv.EnvBuilder(with_pip=True, system_site_packages=True, clear=False)
    try:
        builder.create(release)
    except (OSError, RuntimeError) as exc:
        shutil.rmtree(release, ignore_errors=True)
        return _result("failed", f"venv не создан: {exc}", current, activated=False)
    python = _python_in(release)
    command = [str(python), "-m", "pip", "install", "--isolated", "--index-url",
               "https://pypi.org/simple", "--upgrade"]
    command.extend(f"{PACKAGE_NAMES[name]}=={pins[name]}" for name in pins)
    try:
        installed = runner(command, cwd=str(root_path), capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(release, ignore_errors=True)
        return _result("failed", f"pip не запущен: {exc}", current, activated=False)
    if installed.returncode:
        shutil.rmtree(release, ignore_errors=True)
        return _result("failed", "pip завершился с ошибкой", current, activated=False,
                       returncode=installed.returncode)
    # system-site-packages can satisfy an unchanged whisper distribution while
    # leaving its console script outside the candidate.  Install only this
    # exact direct wheel without dependencies; dependencies are inherited and
    # the normal resolver pass above remains responsible for the allowlist.
    private_whisper = release / "bin" / "whisper"
    if not private_whisper.is_file() and "openai-whisper" in pins:
        whisper_install = runner(
            [str(python), "-m", "pip", "install", "--isolated", "--index-url",
             "https://pypi.org/simple", "--ignore-installed", "--no-deps",
             f"openai-whisper=={pins['openai-whisper']}"],
            cwd=str(root_path), capture_output=True, text=True, timeout=900)
        if whisper_install.returncode or not private_whisper.is_file():
            shutil.rmtree(release, ignore_errors=True)
            return _result("failed", "candidate whisper entrypoint не создан", current,
                           activated=False, returncode=whisper_install.returncode)
    try:
        result = smoke(root_path, python)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        shutil.rmtree(release, ignore_errors=True)
        return _result("failed", f"candidate smoke завершился ошибкой: {exc}", current, activated=False)
    if (result.get("audio", {}).get("status") == "blocked"
            and pins.get("openai-whisper") != current.get("packages", {}).get("openai-whisper")):
        shutil.rmtree(release, ignore_errors=True)
        return _result("blocked", "Whisper pin изменен, но exact small.pt недоступен", current,
                       activated=False, smoke=result)
    if not result.get("ok"):
        shutil.rmtree(release, ignore_errors=True)
        return _result("failed", "candidate smoke не пройден", current, activated=False,
                       smoke=result)
    pointer = runtime_dir / CURRENT
    old = _current_release(root_path)
    try:
        _atomic_pointer(pointer, release)
        verified = inventory(root_path)
        expected = {name: pins[name] for name in pins}
        if (verified.get("prefix") != str(release)
                or any(verified["packages"].get(name) != version for name, version in expected.items())):
            raise RuntimeError("post-activation verification не пройдена")
        if old:
            _atomic_pointer(runtime_dir / PREVIOUS, old)
        manifest = {"release": release_id, "versions": pins, "system_site_packages": True,
                    "last_updated": dt.datetime.now(dt.timezone.utc).isoformat()}
        _write_json(release / "manifest.json", manifest)
    except OSError as exc:
        if old:
            _atomic_pointer(pointer, old)
        else:
            pointer.unlink(missing_ok=True)
        shutil.rmtree(release, ignore_errors=True)
        return _result("rolled_back", f"activation transaction: {exc}", current,
                       activated=False, smoke=result)
    except RuntimeError as exc:
        if old:
            _atomic_pointer(pointer, old)
        else:
            pointer.unlink(missing_ok=True)
        shutil.rmtree(release, ignore_errors=True)
        return _result("rolled_back", f"activation transaction: {exc}", current,
                       activated=False, smoke=result)
    return _result("updated", "candidate активирован", verified, activated=True,
                    release=release_id, smoke=result,
                    all_green=bool(result.get("all_green")), runtime=str(release))


def upgrade(root: str | os.PathLike[str], versions: dict[str, str], *, runner=None,
            smoke=None, now: str | None = None) -> dict:
    root_path = Path(root).resolve()
    lock = _runtime_dir(root_path) / "upgrade.lock"
    with _upgrade_lock(lock):
        return _upgrade_locked(root_path, versions, runner=runner, smoke=smoke, now=now)


def _main() -> int:
    parser = argparse.ArgumentParser(description="private extraction runtime")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true")
    group.add_argument("--upgrade", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--versions", type=Path)
    args = parser.parse_args()
    root = project_root(__file__)
    if args.status:
        try:
            result = inventory(root)
        except (OSError, RuntimeError) as exc:
            result = _result("failed", str(exc))
    else:
        if not args.versions:
            parser.error("--upgrade требует --versions FILE")
        try:
            payload = json.loads(args.versions.read_text(encoding="utf-8"))
            payload = payload.get("versions", payload) if isinstance(payload, dict) else payload
            result = upgrade(root, payload)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            try:
                current = inventory(root)
            except (OSError, RuntimeError):
                current = {}
            result = _result("failed", str(exc), current)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text if args.json or args.status else text)
    return 0 if args.status or result.get("status") in {"updated", "current"} else 1


if __name__ == "__main__":
    raise SystemExit(_main())
