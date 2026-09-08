#!/usr/bin/env python3
"""Мост «черновик .md → .docx» через DocBuilder.

Зачем: DocBuilder — конструктор с методами (add_title/add_section/add_body), а не
конвертер. Когда владельцу нужен весь пакет в Word немедленно и он проверяет тексты
сам, механическая раскладка markdown по методам конструктора занимает секунды против
часа работы роя составителей.

Ограничение осознанное: это КОНВЕРТАЦИЯ, а не составление. Вердикт Кони документу не
выдается, гейты содержания не проходятся — сборка помечается как черновая.

Запуск:
    python3 scripts/md_to_docx.py ЧЕРНОВИК.md ВЫХОД.docx
    python3 scripts/md_to_docx.py --batch КАТАЛОГ_ЧЕРНОВИКОВ КАТАЛОГ_ВЫХОДА
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# create_docx на импорте выполняет демо-сохранение в /tmp — под песочницей это
# PermissionError. Берем только класс, минуя нижний блок модуля.
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "_docbuilder_src", str(Path(__file__).resolve().parent / "create_docx.py"))
_src = _spec.loader.get_source("_docbuilder_src")
_cut = _src.find('if __name__ ==')
if _cut == -1:  # у модуля нет главного блока — берем как есть до демо-сохранения
    _cut = _src.find('b.save("/tmp/test_doc.docx")')
_mod = importlib.util.module_from_spec(_spec)
exec(compile(_src[:_cut] if _cut > 0 else _src, "create_docx.py", "exec"), _mod.__dict__)
DocBuilder = _mod.DocBuilder

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_NUMBERED = re.compile(r"^(\d+)[.)]\s+(.*)$")
_BULLET = re.compile(r"^[-*·]\s+(.*)$")
_TABLE_PIPE = re.compile(r"(?<!\\)\|")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")


def _clean(text):
    """Разметку markdown снимаем: .docx несет ее собственными средствами."""
    text = _LINK.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    return text.replace("`", "").strip()


def _is_signature_zone(line):
    low = line.lower()
    return low.startswith("подпись") or "/ __" in line or line.startswith("«___»")


def convert(md_path, docx_path):
    lines = Path(md_path).read_text(encoding="utf-8").splitlines()
    b = DocBuilder()
    title_done = False
    table_lines = []

    def flush_table():
        if not table_lines:
            return
        rows = [[_clean(cell).replace(r"\|", "|")
                 for cell in _TABLE_PIPE.split(line[1:-1])]
                for line in table_lines]
        # ponytail: потолок - 2 колонки по REQ-15; снять проверку для широких таблиц.
        if (len(rows) >= 3 and all(len(row) == 2 for row in rows)
                and all(_TABLE_SEPARATOR.fullmatch(cell) for cell in rows[1])):
            b.add_table(rows[0], rows[2:])
        else:
            for row in rows:
                if all(_TABLE_SEPARATOR.fullmatch(cell) for cell in row):
                    continue
                b.add_body([(" — ".join(cell for cell in row if cell), False)])
        table_lines.clear()

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines.append(stripped)
            continue
        flush_table()

        if not stripped:
            continue

        if line.startswith("---") or line.startswith("***") or line.lstrip().startswith("<!--"):
            continue

        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            text = _clean(line.lstrip("#").strip())
            if not text:
                continue
            if level == 1 and not title_done:
                b.add_title(text)
                title_done = True
            elif level <= 2:
                b.add_section(text)
            else:
                b.add_subsection(text)
            continue

        m = _NUMBERED.match(line.strip())
        if m:
            b.add_numbered_body([(_clean(m.group(2)), False)])
            continue

        m = _BULLET.match(line.strip())
        if m:
            b.add_bullet(_clean(m.group(1)))
            continue

        text = _clean(line)
        if not text:
            continue
        if _is_signature_zone(text):
            b.add_body([(text, False)])
            b.add_empty()
            continue
        b.add_body([(text, False)])

    flush_table()
    b.add_page_numbers()
    Path(docx_path).parent.mkdir(parents=True, exist_ok=True)
    b.save(str(docx_path))
    # DocBuilder.save() при запрете вердикта печатает отказ и НЕ бросает исключение.
    # Верим диску, а не отчету: тихий успех — та самая болезнь, ради которой скрипт писан.
    if not Path(docx_path).exists():
        raise RuntimeError("DocBuilder отказал в сборке (нет вердикта Кони) — файл не создан")
    return docx_path


def convert_plain(md_path, docx_path):
    """Рабочая копия для чтения человеком, БЕЗ гейта вердикта.

    Форматирование по обычаю судебного документа: шапка блоком в правой верхней
    части листа, название по центру прописными, текст по ширине с красной строкой,
    просительная часть и приложения выделены, подпись строкой «дата — подпись».
    """
    from docx import Document
    from docx.shared import Pt, Mm, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "PT Serif"
    st.font.size = Pt(14)
    pf = st.paragraph_format
    pf.line_spacing = 1.5
    pf.space_after = Pt(0)
    for s in doc.sections:
        s.top_margin, s.bottom_margin = Mm(20), Mm(30)
        s.left_margin, s.right_margin = Mm(30), Mm(15)

    lines = [l.rstrip() for l in Path(md_path).read_text(encoding="utf-8").splitlines()]
    lines = [l for l in lines if not l.lstrip().startswith("<!--")]

    TITLE = re.compile(r"^(ИСКОВОЕ ЗАЯВЛЕНИЕ|ОБРАЩЕНИЕ|ЗАЯВЛЕНИЕ|ЖАЛОБА|ХОДАТАЙСТВО|ВОЗРАЖЕНИЯ)\b")
    title_at = next((i for i, l in enumerate(lines) if TITLE.match(l.strip())), None)

    def para(text, align=WD_ALIGN_PARAGRAPH.JUSTIFY, indent=True, bold=False,
             before=0, after=0, left=None):
        p = doc.add_paragraph()
        r = p.add_run(text)
        r.bold = bold
        p.alignment = align
        p.paragraph_format.first_line_indent = Cm(1.25) if indent else Cm(0)
        p.paragraph_format.space_before = Pt(before)
        p.paragraph_format.space_after = Pt(after)
        if left is not None:
            p.paragraph_format.left_indent = Cm(left)
        return p

    # ── шапка: блок в правой части листа, отступ слева ≈ половина ширины полосы
    if title_at:
        # строки шапки склеиваем в абзацы по пустой строке: в Word жесткие переносы
        # исходника рвут блок «Истец: …» на обрывки, чего в документе быть не должно.
        buf = []
        def flush_head():
            if buf:
                para(" ".join(buf), align=WD_ALIGN_PARAGRAPH.LEFT, indent=False,
                     left=8.5, after=6)
                buf.clear()
        for raw in lines[:title_at]:
            line = raw.strip()
            if not line:
                flush_head()
                continue
            buf.append(_clean(line))
        flush_head()
        para("", indent=False, after=12)

    body = lines[title_at:] if title_at is not None else lines
    seen_title = False
    sign_buf = []
    tbl_buf = []

    def flush_table():
        """Таблицы markdown переносим настоящей таблицей Word, а не строкой текста."""
        if not tbl_buf:
            return
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in tbl_buf]
        rows = [r for r in rows if not set("".join(r)) <= set("-: ")]
        if not rows:
            tbl_buf.clear()
            return
        width = max(len(r) for r in rows)
        table = doc.add_table(rows=len(rows), cols=width)
        table.style = "Table Grid"
        for i, row in enumerate(rows):
            for j in range(width):
                cell = table.cell(i, j)
                cell.text = _clean(row[j]) if j < len(row) else ""
                for pp in cell.paragraphs:
                    for r in pp.runs:
                        r.font.name = "PT Serif"
                        r.font.size = Pt(12)
                        r.bold = (i == 0)
        doc.add_paragraph()
        tbl_buf.clear()
    for raw in body:
        line = raw.strip()
        if not line or line.startswith("---"):
            continue
        if line.lstrip().startswith("|"):
            tbl_buf.append(line)
            continue
        flush_table()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        text = _clean(line)
        if not text:
            continue

        if not seen_title and TITLE.match(text):
            para(text.upper(), align=WD_ALIGN_PARAGRAPH.CENTER, indent=False,
                 bold=True, after=6)
            seen_title = True
            continue
        # подзаголовок «о чем документ» — сразу под названием, по центру
        if seen_title and text[:1].islower() and doc.paragraphs[-1].runs and doc.paragraphs[-1].runs[0].bold:
            para(text, align=WD_ALIGN_PARAGRAPH.CENTER, indent=False, after=12)
            continue
        if text.upper() in ("ПРОШУ:", "ПРОШУ", "ТРЕБОВАНИЯ:"):
            para("ПРОШУ:", align=WD_ALIGN_PARAGRAPH.CENTER, indent=False, bold=True,
                 before=12, after=6)
            continue
        if text.rstrip(":").upper() in ("ПРИЛОЖЕНИЯ", "ПРИЛОЖЕНИЕ"):
            para("Приложения:", align=WD_ALIGN_PARAGRAPH.LEFT, indent=False, bold=True,
                 before=12, after=6)
            continue
        if re.match(r"^[IVX]+\.\s", text) or (len(text) < 90 and text.endswith(tuple("абвгдежзийклмнопрстуфхцчшщыэюя")) and re.match(r"^[IVX]+\.", text)):
            para(text, align=WD_ALIGN_PARAGRAPH.LEFT, indent=False, bold=True,
                 before=12, after=6)
            continue
        m = _NUMBERED.match(text)
        if m:
            para(text, align=WD_ALIGN_PARAGRAPH.JUSTIFY, indent=False, left=0.75)
            continue
        m = _BULLET.match(text)
        if m:
            para("— " + _clean(m.group(1)), align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                 indent=False, left=0.75)
            continue
        if text.startswith("«____»") or text.startswith("____"):
            sign_buf.append(text)
            continue
        para(text)

    # Блок подписи НЕ дописывается: реквизит доверителя ставится только если он
    # есть в исходнике. Автоподпись на произвольном файле — фабрикация (найдено
    # разбором 06.09.2026: подпись доверителя попала в аналитическую записку).
    flush_table()
    if sign_buf:
        from docx.enum.table import WD_TABLE_ALIGNMENT
        para("", indent=False, before=18)
        tbl = doc.add_table(rows=1, cols=2)
        tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
        left_cell, right_cell = tbl.rows[0].cells
        date_line = next((s for s in sign_buf if s.startswith("«")), "")
        sign_line = next((s for s in sign_buf if "/" in s), "")
        left_cell.paragraphs[0].add_run(date_line)
        rp = right_cell.paragraphs[0]
        rp.add_run(sign_line)
        rp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for cell in (left_cell, right_cell):
            for pp in cell.paragraphs:
                for r in pp.runs:
                    r.font.name = "PT Serif"
                    r.font.size = Pt(14)
    Path(docx_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(docx_path))
    if not Path(docx_path).exists():
        raise RuntimeError("файл не создан")
    return docx_path


def _batch(src_dir, out_dir, plain=False):
    src, out = Path(src_dir), Path(out_dir)
    made, failed = [], []
    for md in sorted(src.glob("*.md")):
        if md.name.startswith("_"):
            continue
        target = out / (md.stem + ".docx")
        try:
            (convert_plain if plain else convert)(md, target)
            made.append(target.name)
        except Exception as exc:                     # noqa: BLE001 — отчет владельцу важнее трассы
            failed.append(f"{md.name}: {type(exc).__name__}: {exc}")
    for name in made:
        print("собран:", name)
    for line in failed:
        print("ОТКАЗ:", line, file=sys.stderr)
    print(f"итого: {len(made)} собрано, {len(failed)} отказов")
    return 1 if failed else 0


def _selftest():
    """Минимальный чек: текст и таблица доходят до .docx."""
    import tempfile
    from docx import Document
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "t.md"
        md.write_text(
            "# Заголовок\n\n## Раздел\n\n1. Первый пункт\n\nОбычный абзац.\n\n"
            "| Поле | Значение |\n| --- | --- |\n| Номер | 42 |\n",
            encoding="utf-8",
        )
        out = Path(tmp) / "t.docx"
        convert(md, out)
        assert out.exists() and out.stat().st_size > 5000, "docx пуст или не собран"
        doc = Document(out)
        assert len(doc.tables) == 1, "таблица markdown не перенесена в Word"
        assert [[cell.text for cell in row.cells] for row in doc.tables[0].rows] == [
            ["Поле", "Значение"], ["Номер", "42"]]
    print("selftest пройден")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(2)
    if args[0] == "--selftest":
        sys.exit(_selftest())
    if args[0] == "--batch":
        sys.exit(_batch(args[1], args[2]))
    if args[0] == "--batch-plain":
        sys.exit(_batch(args[1], args[2], plain=True))
    sys.exit(0 if convert(args[0], args[1]) else 1)
