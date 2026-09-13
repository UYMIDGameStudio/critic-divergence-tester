"""Repeatable real-engine OCR evidence, separate from mocked unit tests.

Fixture authoring needs Pillow/reportlab (the Codex bundled Python has both).
Verification uses the project's production ingest and PDF renderer and needs
only its PDF extras plus a separately provisioned Tesseract executable.
Neither mode installs components or changes system environment variables.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SAMPLES = {
    "eng": ("English", "arial.ttf", [
        "Evidence supports the argument.",
        "Review the source before accepting a claim.",
        "Numbers, quotations, and uncertainty need human checks."]),
    "chi_sim": ("简体中文", "msyh.ttc", [
        "证据支持这一论点。研究需要清楚的定义。",
        "审查者必须核对原始资料，并说明不确定性。",
        "相关人员及时完成报名。数字和引文需要人工确认。"]),
    "chi_tra": ("繁體中文", "msjh.ttc", [
        "證據支持這一論點。研究需要清楚的定義。",
        "審查者必須核對原始資料，並說明不確定性。",
        "相關人員及時完成報名。數字和引文需要人工確認。"]),
    "deu": ("Deutsch", "arial.ttf", [
        "Die Belege stützen diese Schlussfolgerung.",
        "Größe, Öffentlichkeit und Überprüfung sind wichtig.",
        "Ähnliche Gründe müssen sorgfältig geprüft werden."]),
    "fra": ("Français", "arial.ttf", [
        "Les preuves étayent cette conclusion.",
        "L'étude exige une vérification précise des sources.",
        "Le cœur de l'œuvre révèle une ambiguïté à résoudre."]),
    "jpn": ("日本語", "YuGothR.ttc", [
        "証拠はこの議論を支持する。",
        "研究者は原資料を確認し、不確実性を説明する。",
        "仮説と観察結果を区別することが重要です。"]),
    "rus": ("Русский", "arial.ttf", [
        "Доказательства подтверждают этот вывод.",
        "Исследователь проверяет источники и объясняет выводы.",
        "Точность, объём и надёжность требуют проверки."]),
    "lat": ("Latina", "arial.ttf", [
        "Argumentum testimoniis confirmatur.",
        "Rēs pūblica et cīvēs cōnsilium quaerunt.",
        "Causæ et fœdera diligenter examinantur."]),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalized(text):
    # TSV word boundaries introduce spaces in CJK; never discard letters,
    # punctuation, case, diacritics or ligatures when measuring recognition.
    return "".join(unicodedata.normalize("NFC", text).split())


def distance(expected, actual):
    previous = list(range(len(actual) + 1))
    for index, left in enumerate(expected, 1):
        current = [index]
        for offset, right in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[offset] + 1,
                               previous[offset - 1] + (left != right)))
        previous = current
    return previous[-1]


def compare(expected, actual):
    left, right = normalized(expected), normalized(actual)
    differences = []
    for kind, a, b, c, d in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if kind != "equal":
            differences.append({"kind": kind, "expected": left[a:b], "actual": right[c:d],
                                "expected_context": left[max(0, a - 12):min(len(left), b + 12)]})
    errors = distance(left, right)
    return {"expected_characters": len(left), "recognized_characters": len(right),
            "edit_distance": errors, "character_error_rate": errors / max(1, len(left)),
            "exact_ignoring_whitespace": left == right, "differences": differences,
            "missing_distinct_non_ascii_letters": sorted({c for c in left if ord(c) > 127 and c.isalpha() and c not in right})}


def make_fixtures(output, native_pdf):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from pypdf import PdfReader
    from document_review_pdf_render import open_pdf_renderer

    fixtures = output / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    dpi, width_pt, height_pt = 300, 595.28, 841.89
    records = []
    for language, (label, filename, lines) in SAMPLES.items():
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / filename
        font = ImageFont.truetype(str(font_path), round(16 * dpi / 72))
        unknown = bytes(font.getmask("\uffff"))
        missing = sorted({char for char in "".join(lines) if not char.isspace() and bytes(font.getmask(char)) == unknown})
        if missing:
            raise RuntimeError(f"fixture font lacks glyphs for {language}: {missing}")
        image = Image.new("L", (round(width_pt * dpi / 72), round(height_pt * dpi / 72)), "white")
        draw = ImageDraw.Draw(image)
        margin, y = 160, 240
        for line in lines:
            if draw.textbbox((0, 0), line, font=font)[2] > image.width - 2 * margin:
                raise RuntimeError(f"fixture line would clip: {language}: {line}")
            draw.text((margin, y), line, font=font, fill="black")
            y += 150
        png = fixtures / f"{language}.png"
        image.save(png, dpi=(dpi, dpi))
        pdf = fixtures / f"{language}-scan.pdf"
        document = canvas.Canvas(str(pdf), pagesize=(width_pt, height_pt), pageCompression=1)
        document.drawImage(ImageReader(image), 0, 0, width=width_pt, height=height_pt)
        document.showPage()
        document.save()
        expected = "\n".join(lines)
        (fixtures / f"{language}-expected.txt").write_text(expected + "\n", encoding="utf-8")
        records.append({"id": language, "label": label, "language": language,
                        "pdf": pdf.name, "expected_pages": [expected], "font": str(font_path),
                        "font_sha256": sha(font_path.read_bytes()), "font_missing_glyphs": missing,
                        "source_dpi": dpi, "font_pt": 16, "pdf_sha256": sha(pdf.read_bytes())})

    if native_pdf:
        data = native_pdf.read_bytes()
        # PDF content-stream order can place the footer before body text.
        # Layout extraction matches the visual reading order of this fixture;
        # inspect the saved page images before accepting a new native source.
        text_pages = [page.extract_text(extraction_mode="layout") or "" for page in PdfReader(io.BytesIO(data)).pages]
        target = fixtures / "native-office-mixed-scan.pdf"
        document = canvas.Canvas(str(target), pageCompression=1)
        with open_pdf_renderer(data) as renderer:
            for number in range(1, renderer.page_count + 1):
                png_bytes = renderer.render_page(number, scale=dpi / 72)
                (fixtures / f"native-office-page-{number}.png").write_bytes(png_bytes)
                image = Image.open(io.BytesIO(png_bytes))
                size = (image.width * 72 / dpi, image.height * 72 / dpi)
                document.setPageSize(size)
                document.drawImage(ImageReader(image), 0, 0, width=size[0], height=size[1])
                document.showPage()
        document.save()
        records.append({"id": "native-office-mixed", "label": "Native Word export, eight languages",
                        "language": "+".join(SAMPLES), "pdf": target.name,
                        "expected_pages": text_pages, "source_pdf": str(native_pdf),
                        "reference_method": "pypdf layout extraction; requires visual comparison",
                        "source_sha256": sha(data), "source_dpi": dpi, "pdf_sha256": sha(target.read_bytes())})
    for row in records:
        reader = PdfReader(fixtures / row["pdf"])
        if any((page.extract_text() or "").strip() for page in reader.pages):
            raise RuntimeError(f"fixture accidentally has a text layer: {row['id']}")
    write_json(fixtures / "manifest.json", {"fixtures": records})
    print(f"Created {len(records)} image-only PDF fixtures", flush=True)


def verify(output, executable, provenance):
    from pypdf import PdfReader
    from document_review_ingest import TesseractOCR, ingest_bytes
    from document_review_pdf_render import open_pdf_renderer, renderer_backend

    fixtures = output / "fixtures"
    rows = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
    evidence = output / "results"
    evidence.mkdir(exist_ok=True)
    packages = {}
    for name in ("pypdf", "pypdfium2", "Pillow", "reportlab", "PyMuPDF"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "engine": str(executable), "engine_sha256": sha(executable.read_bytes()),
              "engine_version": TesseractOCR(str(executable)).version,
              "python": sys.version, "platform": platform.platform(), "packages": packages,
              "code_sha256": {name: sha((REPO / name).read_bytes()) for name in
                              ("scripts/verify_real_ocr.py", "document_review_ingest.py", "document_review_pdf_render.py")},
              "renderer": renderer_backend()[0], "production_render_scale": None,
              "measurement": "NFC, whitespace removed only; Levenshtein character errors / expected characters",
              "scope": "Synthetic clean modern horizontal print; separate mixed-layout native Word export",
              "provenance": json.loads(provenance.read_text(encoding="utf-8")) if provenance else None,
              "results": []}
    for sample in rows:
        start = time.perf_counter()
        row = {"id": sample["id"], "language": sample["language"]}
        try:
            data = (fixtures / sample["pdf"]).read_bytes()
            if sha(data) != sample["pdf_sha256"]:
                raise RuntimeError("fixture bytes differ from manifest")
            reader = PdfReader(io.BytesIO(data))
            if any((page.extract_text() or "").strip() for page in reader.pages):
                raise RuntimeError("fixture has a text layer, so this would not test OCR")
            adapter = TesseractOCR(str(executable), language=sample["language"])
            available, detail = adapter.available()
            row["available"], row["availability_detail"] = available, detail
            if not available:
                raise RuntimeError(detail)
            document = ingest_bytes(sample["pdf"], data, ocr=adapter, ocr_language=sample["language"])
            render_metadata = document.metadata.get("ocr", {})
            render_scale = render_metadata.get("render_scale")
            if not isinstance(render_scale, (int, float)) or render_scale <= 0:
                raise RuntimeError("production OCR did not report its rendering scale")
            row["ocr_metadata"] = render_metadata
            report["production_render_scale"] = render_scale
            with open_pdf_renderer(data) as renderer:
                for number in range(1, renderer.page_count + 1):
                    (evidence / f"{sample['id']}-production-page-{number}.png").write_bytes(renderer.render_page(number, scale=render_scale))
            write_json(evidence / f"{sample['id']}-document.json", document.to_dict())
            if not document.blocks or any(not block.attrs.get("ocr") for block in document.blocks):
                raise RuntimeError("expected real OCR blocks for every recognized scan page")
            if document.source.sha256 != sha(data) or not document.quality.requires_confirmation:
                raise RuntimeError("source binding or human confirmation requirement was lost")
            page_results = []
            for number, expected in enumerate(sample["expected_pages"], 1):
                text = "\n".join(block.text for block in document.blocks if block.location.page == number)
                if not text.strip():
                    raise RuntimeError(f"no OCR text for page {number}")
                (evidence / f"{sample['id']}-page-{number}.txt").write_text(text + "\n", encoding="utf-8")
                page_results.append({"page": number, "expected": expected, "actual": text, **compare(expected, text)})
            total_chars = sum(page["expected_characters"] for page in page_results)
            total_errors = sum(page["edit_distance"] for page in page_results)
            row.update({"status": "pipeline_pass", "pages": page_results,
                        "character_error_rate": total_errors / max(1, total_chars),
                        "requires_confirmation": document.quality.requires_confirmation,
                        "warnings": [warning.to_dict() for warning in document.warnings],
                        "quality": document.quality.to_dict()})
        except Exception as error:
            row.update({"status": "pipeline_fail", "error": f"{type(error).__name__}: {error}"})
        row["seconds"] = round(time.perf_counter() - start, 3)
        report["results"].append(row)
        write_json(output / "report.json", report)
        print(f"{row['id']}: {row['status']}, CER={row.get('character_error_rate', 'n/a')}, {row['seconds']}s", flush=True)
    report["all_pipelines_passed"] = all(row["status"] == "pipeline_pass" for row in report["results"])
    write_json(output / "report.json", report)
    lines = ["# Real OCR acceptance evidence", "", f"Engine: {report['engine_version']}",
             f"Renderer: {report['renderer']}, actual production scale {report['production_render_scale']} ({72 * (report['production_render_scale'] or 0):g} DPI for normal PDF points).", "",
             "Pipeline pass confirms actual OCR execution, source binding and required human confirmation. It does not assert perfect transcription.", "",
             "Character error rate ignores whitespace only; case, punctuation, accents and ligatures remain significant.", "",
             "| Sample | Pipeline | Character error rate | Seconds |", "|---|---|---:|---:|"]
    for row in report["results"]:
        cer = f"{row['character_error_rate']:.2%}" if "character_error_rate" in row else "n/a"
        lines.append(f"| {row['id']} | {row['status']} | {cer} | {row['seconds']} |")
    lines += ["", "These fixtures cover clean modern horizontal print, not handwriting, damaged scans, historic type, mathematical notation, or vertical Japanese. Mixed-language/table reading order is measured separately. Full expected and recognized text, differences, warnings, hashes and package versions are in report.json."]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if report["all_pipelines_passed"] else 1


def compare_dpi(output, executable):
    """Controlled renderer/engine comparison; production code is unchanged."""
    from document_review_ingest import TesseractOCR
    from document_review_pdf_render import open_pdf_renderer

    fixtures = output / "fixtures"
    samples = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
    report = {"scope": "Same production renderer and Tesseract adapter; scale is the only changed input. Timings exclude archive writes.",
              "max_page_pixels": 16_000_000, "max_page_dimension": 8192, "results": []}
    for sample in samples:
        data = (fixtures / sample["pdf"]).read_bytes()
        if sha(data) != sample["pdf_sha256"]:
            raise RuntimeError("fixture bytes differ from manifest")
        for dpi in (144, 300):
            start = time.perf_counter()
            adapter = TesseractOCR(str(executable), language=sample["language"])
            pages = []
            with open_pdf_renderer(data, max_pixels=16_000_000, max_dimension=8192) as renderer:
                for number, expected in enumerate(sample["expected_pages"], 1):
                    image = renderer.render_page(number, scale=dpi / 72)
                    result = adapter.recognize_pdf_page(image, page_number=number, language=sample["language"])
                    (output / "results" / f"{sample['id']}-{dpi}dpi-page-{number}.png").write_bytes(image)
                    pages.append({"page": number, "expected": expected, "actual": result["text"], **compare(expected, result["text"])})
            total = sum(page["expected_characters"] for page in pages)
            row = {"id": sample["id"], "dpi": dpi, "seconds": round(time.perf_counter() - start, 3),
                   "character_error_rate": sum(page["edit_distance"] for page in pages) / max(1, total), "pages": pages}
            report["results"].append(row)
            print(f"{sample['id']} {dpi}DPI: CER={row['character_error_rate']:.4f}, {row['seconds']}s", flush=True)
            write_json(output / "dpi-comparison.json", report)
    return 0


def compare_models(output, executable, model_directory):
    """Measure optional best models without replacing the active fast data."""
    from document_review_ingest import TesseractOCR, ingest_bytes

    fixtures = output / "fixtures"
    samples = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
    old_prefix = os.environ.get("TESSDATA_PREFIX")
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "model_directory": str(model_directory),
              "provenance": json.loads((model_directory / "provenance.json").read_text(encoding="utf-8")), "results": []}
    try:
        os.environ["TESSDATA_PREFIX"] = str(model_directory)
        for sample in samples:
            if sample["id"] not in {"lat", "chi_tra"}:
                continue
            start = time.perf_counter()
            data = (fixtures / sample["pdf"]).read_bytes()
            if sha(data) != sample["pdf_sha256"]:
                raise RuntimeError("fixture bytes differ from manifest")
            adapter = TesseractOCR(str(executable), language=sample["language"])
            document = ingest_bytes(sample["pdf"], data, ocr=adapter, ocr_language=sample["language"])
            actual = "\n".join(block.text for block in document.blocks)
            if not actual or any(not block.attrs.get("ocr") for block in document.blocks):
                raise RuntimeError("comparison did not produce actual OCR blocks")
            row = {"id": sample["id"], "seconds": round(time.perf_counter() - start, 3),
                   "ocr_metadata": document.metadata["ocr"], "expected": sample["expected_pages"][0],
                   "actual": actual, **compare(sample["expected_pages"][0], actual)}
            report["results"].append(row)
            write_json(output / "results" / f"{sample['id']}-best-document.json", document.to_dict())
            print(f"{sample['id']} best model: CER={row['character_error_rate']:.4f}, {row['seconds']}s", flush=True)
        character_file = output / "lat-best.lstm-unicharset"
        subprocess.run([str(executable.with_name("combine_tessdata.exe")), "-e", str(model_directory / "lat.traineddata"), str(character_file)],
                       capture_output=True, check=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        characters = {line.split(" ")[0] for line in character_file.read_text(encoding="utf-8").splitlines()[1:]}
        report["latin_model_character_coverage"] = {char: char in characters for char in "ēūīōāæœ\u0304"}
    finally:
        if old_prefix is None:
            os.environ.pop("TESSDATA_PREFIX", None)
        else:
            os.environ["TESSDATA_PREFIX"] = old_prefix
    write_json(output / "best-model-comparison.json", report)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("fixtures", "verify", "compare-dpi", "compare-models"))
    parser.add_argument("--output", type=Path, default=REPO / "dist/real-ocr-0.2.2")
    parser.add_argument("--native-pdf", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--comparison-data", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if REPO not in output.parents:
        parser.error("evidence output must be a directory inside this repository")
    output.mkdir(parents=True, exist_ok=True)
    if args.mode == "fixtures":
        make_fixtures(output, args.native_pdf.resolve() if args.native_pdf else None)
        return 0
    if args.engine is None:
        parser.error("verification requires an explicit --engine; no automatic installation")
    if args.mode == "compare-dpi":
        return compare_dpi(output, args.engine.resolve())
    if args.mode == "compare-models":
        if args.comparison_data is None:
            parser.error("model comparison requires --comparison-data")
        return compare_models(output, args.engine.resolve(), args.comparison_data.resolve())
    return verify(output, args.engine.resolve(), args.provenance)


if __name__ == "__main__":
    raise SystemExit(main())
