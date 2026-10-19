"""Machine-readable PDF and resume quality gates."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from collections import defaultdict
import shutil
import subprocess


class _BBoxParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.page = (0.0, 0.0)
        self.words: list[list] = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): v for k, v in attrs}
        if tag == "page":
            self.page = (float(a["width"]), float(a["height"]))
        if tag == "word" and all(k in a for k in ("xmin", "ymin", "xmax", "ymax")):
            self.current = [float(a["xmin"]), float(a["ymin"]), float(a["xmax"]), float(a["ymax"]), ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[4] += data

    def handle_endtag(self, tag):
        if tag == "word" and self.current is not None:
            self.words.append(self.current)
            self.current = None


def _run(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def validate_pdf(pdf_path: Path, *, required_terms: list[str] | None = None, forbidden_terms: list[str] | None = None) -> dict:
    required_terms = required_terms or []
    forbidden_terms = forbidden_terms or []
    result = {"path": str(pdf_path), "errors": [], "warnings": []}
    pdfinfo = shutil.which("pdfinfo")
    pdftotext = shutil.which("pdftotext")
    if not pdfinfo or not pdftotext:
        result["errors"].append("pdfinfo and pdftotext are required for PDF validation")
        return result
    code, info = _run([pdfinfo, str(pdf_path)])
    if code:
        result["errors"].append(info.strip())
        return result
    fields = {}
    for line in info.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    result["pages"] = int(fields.get("Pages", "0"))
    result["page_size"] = fields.get("Page size", "")
    if result["pages"] != 1:
        result["errors"].append(f"expected exactly one page, found {result['pages']}")
    code, text = _run([pdftotext, "-layout", str(pdf_path), "-"])
    result["text"] = text
    if code or not text.strip():
        result["errors"].append("PDF text extraction failed or returned empty text")
    result["required_terms"] = {term: term.lower() in text.lower() for term in required_terms}
    result["missing_required_terms"] = [term for term, found in result["required_terms"].items() if not found]
    result["forbidden_terms"] = {term: term.lower() in text.lower() for term in forbidden_terms}
    result["forbidden_present"] = [term for term, found in result["forbidden_terms"].items() if found]
    if result["missing_required_terms"]:
        result["errors"].append("missing required extracted terms")
    if result["forbidden_present"]:
        result["errors"].append("forbidden terms present in extracted text")

    bbox_path = pdf_path.with_suffix(".bbox.html")
    code, _ = _run([pdftotext, "-bbox-layout", str(pdf_path), str(bbox_path)])
    if code == 0 and bbox_path.exists():
        parser = _BBoxParser()
        parser.feed(bbox_path.read_text(encoding="utf-8"))
        words = parser.words
        ys = [value for word in words for value in (word[1], word[3])]
        result["word_count"] = len(words)
        result["text_bounds"] = {"y_min": min(ys), "y_max": max(ys)} if ys else {}
        result["utilization"] = round((max(ys) - min(ys)) / parser.page[1], 4) if ys else 0.0
        lines = defaultdict(list)
        for word in words:
            lines[round(word[1], 1)].append(word)
        overlaps = []
        for key, line in lines.items():
            line.sort()
            for left, right in zip(line, line[1:]):
                if left[2] > right[0] + 0.05:
                    overlaps.append({"baseline": key, "left": left[4], "right": right[4]})
        result["same_line_overlaps"] = overlaps
        result["clipped_words"] = [word[4] for word in words if word[0] < 0 or word[1] < 0 or word[2] > parser.page[0] or word[3] > parser.page[1]]
        if overlaps:
            result["errors"].append("same-line text overlap detected")
        if result["clipped_words"]:
            result["errors"].append("clipped text detected")
    return result


def render_preview(pdf_path: Path, output_png: Path, dpi: int = 180) -> Path | None:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return None
    output_png.parent.mkdir(parents=True, exist_ok=True)
    prefix = output_png.with_suffix("")
    code, _ = _run([pdftoppm, "-f", "1", "-singlefile", "-png", "-r", str(dpi), str(pdf_path), str(prefix)])
    generated = prefix.with_suffix(".png")
    if code == 0 and generated.exists():
        return generated
    return None
