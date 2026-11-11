"""Document Understanding & Generation Engine (PDF, DOCX, PPTX, XLSX) with sandboxed output and formula neutralization."""
from pathlib import Path
import re
import time

from docx import Document as DocxDocument
import openpyxl
from pptx import Presentation
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _sanitize_slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (name or "document").strip()).strip("-.")
    return s[:80] or f"doc-{int(time.time())}"


def _safe_cell(val):
    """Neutralize Excel formula injection (`=`, `+`, `-`, `@`, `\\t`, `\\r`) for untrusted text cells."""
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


def parse_document(path: Path, *, max_chars: int = 24_000) -> dict:
    """Extract readable structured text from PDF, DOCX, PPTX, XLSX, or UTF-8 text files."""
    p = Path(path).resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"Document not found: {p}")
    ext = p.suffix.lower()
    meta: dict = {"ext": ext, "bytes": p.stat().st_size}

    if ext == ".pdf":
        reader = PdfReader(str(p))
        pages = [page.extract_text() or "" for page in reader.pages]
        meta["pages"] = len(pages)
        text = "\n\n".join(f"[Page {i + 1}]\n{t.strip()}" for i, t in enumerate(pages) if t.strip())
    elif ext == ".docx":
        doc = DocxDocument(str(p))
        parts = [para.text.strip() for para in doc.paragraphs if para.text.strip()]
        for tbl in doc.tables:
            for row in tbl.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        meta["paragraphs"] = len(doc.paragraphs)
        text = "\n".join(parts)
    elif ext == ".pptx":
        prs = Presentation(str(p))
        slides_txt: list[str] = []
        for idx, slide in enumerate(prs.slides, 1):
            lines = [sh.text.strip() for sh in slide.shapes if getattr(sh, "has_text_frame", False) and sh.text.strip()]
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                note = slide.notes_slide.notes_text_frame.text.strip()
                if note:
                    lines.append(f"Notes: {note}")
            if lines:
                slides_txt.append(f"[Slide {idx}]\n" + "\n".join(lines))
        meta["slides"] = len(prs.slides)
        text = "\n\n".join(slides_txt)
    elif ext in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        sheets_txt: list[str] = []
        try:
            for sname in wb.sheetnames[:10]:
                ws = wb[sname]
                rows_out: list[str] = []
                for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                    if r_idx >= 100:
                        rows_out.append("... (rows truncated)")
                        break
                    cells = ["" if v is None else str(v) for v in row[:20]]
                    if any(cells):
                        rows_out.append(" | ".join(cells))
                sheets_txt.append(f"[Sheet: {sname}]\n" + "\n".join(rows_out))
            meta["sheets"] = list(wb.sheetnames)
        finally:
            wb.close()
        text = "\n\n".join(sheets_txt)
    else:
        text = p.read_text(encoding="utf-8", errors="replace")

    truncated = len(text) > max_chars
    return {
        "path": str(p),
        "name": p.name,
        "meta": meta,
        "chars": len(text),
        "truncated": truncated,
        "content": text[:max_chars],
    }


def generate_pdf(output_path: Path, *, title: str, sections: list[dict]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output_path), pagesize=letter, title=title)
    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles["Title"]), Spacer(1, 12)]
    for sec in sections:
        heading = sec.get("heading")
        body = sec.get("body")
        rows = sec.get("table")
        if heading:
            story.append(Paragraph(str(heading), styles["Heading2"]))
            story.append(Spacer(1, 6))
        if body:
            for para in str(body).split("\n"):
                if para.strip():
                    story.append(Paragraph(para.strip(), styles["BodyText"]))
                    story.append(Spacer(1, 4))
        if rows and isinstance(rows, list):
            tbl = Table(rows)
            tbl.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("PADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.extend([Spacer(1, 6), tbl, Spacer(1, 10)])
    doc.build(story)
    return output_path


def generate_docx(output_path: Path, *, title: str, sections: list[dict]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = DocxDocument()
    doc.add_heading(title, level=0)
    for sec in sections:
        if sec.get("heading"):
            doc.add_heading(str(sec["heading"]), level=2)
        if sec.get("body"):
            for para in str(sec["body"]).split("\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
        rows = sec.get("table")
        if rows and isinstance(rows, list) and rows[0]:
            t = doc.add_table(rows=len(rows), cols=len(rows[0]))
            for ri, row in enumerate(rows):
                for ci, val in enumerate(row):
                    t.cell(ri, ci).text = str(val)
    doc.save(str(output_path))
    return output_path


def generate_pptx(output_path: Path, *, title: str, slides: list[dict], subtitle: str = "Generated by Jarvis AI") -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs = Presentation()
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = title
    if len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = subtitle

    for sl in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = str(sl.get("title") or "Slide")
        tf = slide.placeholders[1].text_frame
        bullets = sl.get("bullets") or []
        for idx, b in enumerate(bullets):
            p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
            p.text = str(b)
        if sl.get("notes"):
            slide.notes_slide.notes_text_frame.text = str(sl["notes"])
    prs.save(str(output_path))
    return output_path


def generate_xlsx(output_path: Path, *, sheets: list[dict]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    first = True
    for sh in sheets:
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = str(sh.get("name") or "Sheet")[:31]
        for row in sh.get("rows") or []:
            ws.append([_safe_cell(c) for c in row])
    wb.save(str(output_path))
    return output_path


class DocumentService:
    """Sandboxed multi-format Document Generator & Parser."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _safe_out(self, filename: str, ext: str) -> Path:
        stem = _sanitize_slug(Path(filename).stem)
        dest = (self.output_dir / f"{stem}{ext}").resolve()
        if dest.parent != self.output_dir:
            raise PermissionError("Output path escapes sandboxed document directory")
        return dest

    def generate(self, fmt: str, *, filename: str, title: str, payload: dict) -> dict:
        kind = fmt.strip().lower()
        if kind == "pdf":
            p = generate_pdf(self._safe_out(filename, ".pdf"), title=title, sections=payload.get("sections") or [{"body": payload.get("text", "")}])
        elif kind in ("docx", "word"):
            p = generate_docx(self._safe_out(filename, ".docx"), title=title, sections=payload.get("sections") or [{"body": payload.get("text", "")}])
        elif kind in ("pptx", "powerpoint", "slides"):
            p = generate_pptx(self._safe_out(filename, ".pptx"), title=title, slides=payload.get("slides") or [{"title": title, "bullets": payload.get("bullets", [])}])
        elif kind in ("xlsx", "excel", "spreadsheet"):
            p = generate_xlsx(self._safe_out(filename, ".xlsx"), sheets=payload.get("sheets") or [{"name": "Data", "rows": payload.get("rows", [])}])
        else:
            raise ValueError(f"Unsupported export format: {fmt!r}")
        return {"ok": True, "format": kind, "path": str(p), "name": p.name, "bytes": p.stat().st_size}

    def list_generated(self) -> list[dict]:
        return [
            {"name": p.name, "path": str(p), "ext": p.suffix.lower(), "bytes": p.stat().st_size, "mtime": p.stat().st_mtime}
            for p in sorted(self.output_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)
            if p.is_file()
        ]

