"""Explicit native Office acceptance checks; never opens a user document.

Run the paired PowerShell fixture generator first, then this script, then the
PowerShell verifier, then this script with --verify-native. All evidence lives
under the chosen repository dist directory and uses real production parsers.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from document_review_ingest import ingest_bytes
from document_review_model import DocumentBlock
from document_review_studio import DocumentReviewProject
from document_review_word import preserve_docx

SAMPLES = (
    "Evidence supports the argument.", "证据支持这一论点。", "證據支持這一論點。",
    "Die Belege stützen diese Schlussfolgerung.", "Les preuves étayent cette conclusion.",
    "証拠はこの議論を支持する。", "Доказательства подтверждают этот вывод.",
    "Argumentum testimoniis confirmatur.",
)


def prepare(directory):
    result = {"legacy_imports": [], "word_preservation": {}, "project_export": {}}
    run_directory = Path(tempfile.mkdtemp(prefix="production-run-", dir=directory))
    for suffix in ("doc", "xls", "ppt"):
        path = directory / ("source." + suffix)
        raw = path.read_bytes()
        assert raw.startswith(bytes.fromhex("d0cf11e0a1b11ae1")), "Fixture must be real binary Office"
        project = DocumentReviewProject.create(run_directory, filename=path.name, content=raw)
        document = project.document()
        assert document, project.manifest()
        for sample in SAMPLES:
            assert sample in document.plain_text, (suffix, sample, document.plain_text)
        assert document.source.sha256 == hashlib.sha256(raw).hexdigest()
        assert document.source.byte_size == len(raw)
        assert project.integrity_errors() == []
        receipt = document.metadata["legacy_conversion"]
        assert receipt["input"]["sha256"] != receipt["output"]["sha256"]
        assert receipt["converter"]["version_observed"]
        if suffix == "xls":
            assert any(b.text == "42" for b in document.blocks), "Cached Excel formula result missing"
        result["legacy_imports"].append({"file": path.name, "blocks": len(document.blocks), "all_eight_languages": True, "receipt": receipt})

    raw = (directory / "source.docx").read_bytes()
    original = ingest_bytes("source.docx", raw)
    replacements = {"Alpha BETA omega": "Alpha GAMMA omega", "First paragraph.Second paragraph.": "First paragraph.Updated paragraph."}
    for before in replacements:
        assert any(b.text == before for b in original.blocks), (before, [b.text for b in original.blocks])
    revised = replace(original, blocks=[replace(b, text=replacements.get(b.text, b.text)) for b in original.blocks])
    for name, tracked in (("clean", False), ("tracked", True)):
        output, report = preserve_docx(raw, original, revised, tracked=tracked)
        (directory / (name + ".docx")).write_bytes(output)
        result["word_preservation"][name] = report
    split_blocks = []
    for block in original.blocks:
        if block.text == "相关人员及时完成报名。":
            split_blocks.extend((replace(block, text="项目负责人于周五完成报名。"), DocumentBlock("native-new-paragraph", "paragraph", "具体流程见附件。", attrs={"split_from_block_id": block.block_id})))
        else:
            split_blocks.append(block)
    output, report = preserve_docx(raw, original, replace(original, blocks=split_blocks))
    (directory / "split.docx").write_bytes(output)
    result["word_preservation"]["split"] = report

    # Exercise the same Word source through author decisions and actual project export.
    project = DocumentReviewProject.create(run_directory, filename="source.docx", content=raw)
    project.confirm_extraction("confirm")
    project.confirm_context({"document_type": "活动策划案", "jurisdiction": "unknown", "effective_date": "unknown", "publisher_type": "作者", "audience": "读者", "publication_status": "internal-draft", **{"involves_" + k: False for k in ("contract", "fees", "intellectual_property", "minors", "personal_information", "sponsorship")}})
    project.run_local_prechecks(["expression_ambiguity"])
    block_id = next(b.block_id for b in project.document().blocks if b.text == "相关人员及时完成报名。")
    chosen = next(f.finding_id for f in project.findings() if f.location.block_id == block_id)
    for finding in project.findings():
        project.decide_finding(finding.finding_id, "accept" if finding.finding_id == chosen else "reject", reason="Fixture review scope")
    action = project.prepare_revision_plan()["actions"][0]
    project.set_revision_action_operation(action["action_id"], "replace_block", reason="Specify responsible person and deadline")
    hunk = project.propose_revision_hunk(action["action_id"], "项目负责人于周五完成报名。", rationale="Author-approved clarification")
    project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="Checked against source")
    project.finalize_revision()
    exported = project.export()
    assert project.integrity_errors() == []
    for name, target in (("修改稿.docx", "project-clean.docx"), ("Word修订标记.docx", "project-tracked.docx")):
        candidate = exported / name
        assert candidate.is_file(), candidate
        shutil.copy2(candidate, directory / target)
    assert (exported / "修改稿.docx").is_file()
    result["project_export"] = {"integrity_errors": [], "files": sorted(p.name for p in exported.iterdir() if p.is_file())}
    (directory / "production-checks.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def verify_native(directory):
    result = json.loads((directory / "verify-native.json").read_text(encoding="utf-8-sig"))
    rows = {Path(row["file"]).stem: row for row in result["results"]}
    assert rows["tracked"]["revisions"] > 0
    assert rows["tracked"]["accepted_text"] == rows["clean"]["text"]
    assert result["rejected_text"] == rows["source"]["text"]
    assert rows["project-tracked"]["revisions"] > 0
    assert rows["project-tracked"]["accepted_text"] == rows["project-clean"]["text"]
    assert result["project_rejected_text"] == rows["source"]["text"]
    assert "项目负责人于周五完成报名。" in rows["project-clean"]["text"]
    for row in rows.values():
        assert (row["pages"], row["sections"], row["tables"]) == (2, 2, 1), row
    assert "项目负责人于周五完成报名。\r具体流程见附件。" in rows["split"]["text"]
    assert "Alpha GAMMA omega" in rows["clean"]["text"]
    assert "First paragraph.\rUpdated paragraph." in rows["clean"]["text"]
    for sample in SAMPLES:
        assert sample in rows["clean"]["text"]
    print("Native Word: opens without repair; 2 pages / 2 sections / table retained; accepts to clean and rejects to original; eight languages retained")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--verify-native", action="store_true")
    parser.add_argument("--libreoffice-directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(ROOT / "dist"):
        parser.error("Evidence directory must be inside this repository's dist directory")
    if args.libreoffice_directory:
        os.environ["PATH"] = str(args.libreoffice_directory.resolve()) + os.pathsep + os.environ.get("PATH", "")
    (verify_native if args.verify_native else prepare)(directory)
