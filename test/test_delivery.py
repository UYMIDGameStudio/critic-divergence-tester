from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading
import http.client
from unittest.mock import patch
import zipfile
from dataclasses import replace

from document_review_studio import DocumentReviewProject, _atomic_write
from document_review_ui import StudioApp
from document_review_ingest import ingest_bytes
from document_review_word import preserve_docx, WordEditUnsupported
from project_lifecycle import transaction, create_backup, restore_backup, compatibility, _files
from test.test_document_review_studio import _docx


def ready(root):
    project = DocumentReviewProject.create(root, filename="draft.md", content="# 活动方案\n\n相关人员及时完成报名。\n\n第二段说明。\n".encode())
    project.confirm_extraction("confirm")
    project.confirm_context({"document_type": "活动策划案", "jurisdiction": "unknown", "effective_date": "unknown", "publisher_type": "作者", "audience": "读者", "publication_status": "internal-draft", **{"involves_" + key: False for key in ("contract", "fees", "intellectual_property", "minors", "personal_information", "sponsorship")}})
    project.run_local_prechecks(["expression_ambiguity"])
    return project


class DeliveryTests(unittest.TestCase):
    def test_batch_rejects_stale_selection_before_any_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            selected = [f.finding_id for f in project.findings()]
            with self.assertRaises(ValueError):
                project.decide_finding_batch([*selected, "missing-finding"], "accept", reason="scope")
            self.assertEqual(project._decisions(), {})
            project.decide_finding_batch(selected, "defer", reason="待补材料")
            self.assertTrue(all(f.status == "defer" for f in project.findings()))
            self.assertEqual(len(project._decisions()), len(selected))

    def test_http_rejects_rebinding_cross_origin_and_wrong_project_drafts(self):
        from unified_app import serve_unified_app
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            server, _ = serve_unified_app(data_dir=temp, project_dir=project.root, open_browser=False)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def request(method, path, body=None, **headers):
                    client = http.client.HTTPConnection(*server.server_address, timeout=5)
                    try:
                        client.request(method, path, body=body, headers=headers)
                        response = client.getresponse()
                        return response.status, response.read()
                    finally:
                        client.close()
                token = {"X-Document-Review-Token": server.app.token, "Content-Type": "application/json"}
                self.assertEqual(request("GET", "/", Host="attacker.example")[0], 403)
                self.assertEqual(request("GET", "/api/state", **{**token, "Origin": "https://attacker.example"})[0], 403)
                self.assertEqual(request("GET", "/api/state")[0], 403)
                self.assertEqual(request("GET", "/api/state", **token)[0], 200)
                payload = json.dumps({"project_directory": "other", "fields": {"audience": "leak"}})
                self.assertEqual(request("POST", "/api/draft", payload, **token)[0], 400)
                self.assertFalse((project.root / ".ui-draft.json").exists())
            finally:
                server.shutdown()
                worker.join(timeout=5)
                server.server_close()

    def test_backup_rejects_traversal_and_noncanonical_names(self):
        from project_lifecycle import _child
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ("../outside", "/absolute", "a/./b", "a//b", "a\\b", "C:/outside", "CON", "a.", "a "):
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    _child(root, relative)
            archive = root / "unsafe.zip"
            data = b"payload"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("project/../outside", data)
                z.writestr("backup.json", json.dumps({"version": 1, "files": {"../outside": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}}}))
            with self.assertRaises(ValueError):
                restore_backup(archive, root / "library")
            self.assertFalse((root / "outside").exists())
            with zipfile.ZipFile(root / "report.zip", "w") as z:
                z.writestr("audit.json", "{}")
            with self.assertRaisesRegex(ValueError, "完整项目备份"):
                restore_backup(root / "report.zip", root / "library")

    def test_backup_roundtrip_and_tamper_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = ready(root / "library")
            archive = create_backup(project.root, root / "backup.zip")
            restored = restore_backup(archive, root / "library")
            self.assertNotEqual(restored, project.root)
            self.assertEqual(DocumentReviewProject(restored).integrity_errors(), [])
            for relative, path in _files(project.root).items():
                self.assertEqual(path.read_bytes(), (restored / relative).read_bytes())
            with zipfile.ZipFile(archive) as z:
                data = {n: z.read(n) for n in z.namelist()}
            data["project/project.json"] = b"{}"
            with zipfile.ZipFile(root / "bad.zip", "w") as z:
                for n, content in data.items():
                    z.writestr(n, content)
            before = set((root / "library").iterdir())
            with self.assertRaisesRegex(ValueError, "校验"):
                restore_backup(root / "bad.zip", root / "library")
            self.assertEqual(set((root / "library").iterdir()), before)
            with self.assertRaises(ValueError):
                create_backup(project.root, archive)

    def test_recovery_restores_multi_file_write_after_process_death(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            original = {n: p.read_bytes() for n, p in _files(project.root).items()}
            code = "from pathlib import Path; import os,sys; from project_lifecycle import transaction; from document_review_studio import _atomic_write\nwith transaction(Path(sys.argv[1])):\n _atomic_write(Path(sys.argv[1])/'project.json',b'broken')\n _atomic_write(Path(sys.argv[1])/'partial'/'file.json',b'partial')\n os._exit(73)\n"
            result = subprocess.run([sys.executable, "-c", code, str(project.root)], cwd=Path(__file__).resolve().parents[1], timeout=20)
            self.assertEqual(result.returncode, 73)
            reopened = DocumentReviewProject(project.root)
            self.assertEqual(reopened.integrity_errors(), [])
            self.assertEqual({n: p.read_bytes() for n, p in _files(project.root).items()}, original)

    def test_disk_failure_rolls_back_complete_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            before = {n: p.read_bytes() for n, p in _files(project.root).items()}
            finding = project.findings()[0]
            with patch.object(DocumentReviewProject, "_append_event", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    project.decide_finding(finding.finding_id, "accept", reason="test")
            self.assertEqual(before, {n: p.read_bytes() for n, p in _files(project.root).items()})

    def test_future_schema_is_refused_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.json"
            raw = b'{"schema_version":999}\n'
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "999"):
                compatibility(Path(temp))
            self.assertEqual(path.read_bytes(), raw)

    def test_location_correction_invalidates_existing_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            finding = project.findings()[0]
            project.decide_finding(finding.finding_id, "accept", reason="first")
            block = project.document().blocks[-1]
            project.correct_finding_location(finding.finding_id, block.block_id, reason="wrong paragraph")
            current = next(f for f in project.findings() if f.finding_id == finding.finding_id)
            self.assertEqual(current.status, "open")
            self.assertEqual(current.location.block_id, block.block_id)
            self.assertEqual(project.integrity_errors(), [])

    def test_drafting_request_and_cross_paragraph_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            target = next(f for f in project.findings() if f.location.block_id == project.document().blocks[1].block_id)
            for finding in project.findings():
                project.decide_finding(finding.finding_id, "accept" if finding.finding_id == target.finding_id else "reject", reason="scope")
            action = project.prepare_revision_plan()["actions"][0]
            blocks = [b.block_id for b in project.document().blocks[1:]]
            project.set_revision_action_operation(action["action_id"], "replace_range", reason="combine and restructure", block_ids=blocks)
            request = project.revision_drafting_prompt(action["action_id"])
            response = {"request_sha256": request["request_sha256"], "action_id": action["action_id"], "after_text": "负责人于周五完成报名。\n\n具体流程见附件。", "rationale": "明确主体和截止时间"}
            hunk = project.import_revision_draft(action["action_id"], json.dumps(response, ensure_ascii=False))
            project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="checked")
            revision = project.finalize_revision()
            text = (revision / "修改稿.md").read_text(encoding="utf-8")
            self.assertNotIn("第二段说明", text)
            self.assertIn("负责人于周五", text)
            self.assertIn("具体流程见附件", text)
            self.assertEqual(project.integrity_errors(), [])

    def test_ui_request_replay_is_idempotent_and_project_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            app = StudioApp.create(Path(temp), project.root)
            payload = {"request_id": "operation-123456789", "project_directory": project.root.name,
                       "action": "decide_finding", "data": {"finding_id": project.findings()[0].finding_id, "decision": "reject", "reason": "checked"}}
            app.act(payload)
            app.act(payload)
            self.assertEqual(len(list((project.root / "finding-decisions").glob("*.json"))), 1)
            with self.assertRaises(ValueError):
                app.act({**payload, "project_directory": "other.document-review-studio"})
            with self.assertRaises(ValueError):
                app.act({**payload, "data": {**payload["data"], "decision": "accept"}})

    def test_word_preserves_parts_and_produces_native_revisions(self):
        raw = _docx()
        original = ingest_bytes("draft.docx", raw)
        target = next(b for b in original.blocks if b.text == "相关人员完成报名")
        revised = replace(original, blocks=[replace(b, text="项目负责人完成报名") if b.block_id == target.block_id else b for b in original.blocks])
        clean, report = preserve_docx(raw, original, revised)
        self.assertTrue(report["source_layout_preserved"])
        with zipfile.ZipFile(io.BytesIO(raw)) as before, zipfile.ZipFile(io.BytesIO(clean)) as after:
            for name in before.namelist():
                if name != "word/document.xml":
                    self.assertEqual(before.read(name), after.read(name))
        self.assertIn("项目负责人完成报名", ingest_bytes("changed.docx", clean).plain_text)
        tracked, report = preserve_docx(raw, original, revised, tracked=True)
        self.assertTrue(report["native_track_changes"])
        with zipfile.ZipFile(io.BytesIO(tracked)) as z:
            xml = z.read("word/document.xml").decode()
            self.assertIn("w:delText", xml)
            self.assertIn("w:ins", xml)

    def test_word_rejects_existing_tracked_changes(self):
        raw = _docx(revised=True)
        original = ingest_bytes("draft.docx", raw)
        with self.assertRaises(WordEditUnsupported):
            preserve_docx(raw, original, original)

    def test_all_hunks_rejected_exports_no_change_package(self):
        with tempfile.TemporaryDirectory() as temp:
            project = ready(Path(temp))
            for i, finding in enumerate(project.findings()):
                project.decide_finding(finding.finding_id, "accept" if i == 0 else "reject", reason="scope")
            action = project.prepare_revision_plan()["actions"][0]
            project.set_revision_action_operation(action["action_id"], "replace_block", reason="draft")
            hunk = project.propose_revision_hunk(action["action_id"], "另一个候选文本", rationale="draft")
            project.decide_revision_hunk(hunk["hunk_id"], "reject", reason="keep original")
            project.finalize_revision()
            output = project.export()
            self.assertTrue((output / "本轮未修改稿.docx").is_file())
            self.assertFalse((output / "修改稿.docx").exists())
            self.assertEqual(json.loads((output / "track-changes-capability.json").read_text(encoding="utf-8"))["completion"], "no-change")


if __name__ == "__main__":
    unittest.main()
