from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from argument_app import create_uploaded_project
from document_review_model import ReviewContext, validate_finding_dict
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_ui import StudioApp, render_studio_shell
from review_profiles import ACADEMIC_CRITICS, DOCUMENT_CRITICS, ALL_CRITICS
from unified_app import serve_unified_app


def context(profile="academic", kind="theoretical"):
    return {**ReviewContext("论文", "unknown", "unknown", "作者", "研究者").to_dict(),
            "review_profile": profile, "research_type": kind if profile != "document" else "unspecified",
            "discipline": "social-science" if profile != "document" else "general"}


def project_at(root, text="# 研究\n\n现象导致结果。[2]\n\n## 参考文献\n\n[1] 一项研究。\n", profile="academic", kind="theoretical"):
    project = DocumentReviewProject.create(root, filename="paper.md", content=text.encode())
    project.confirm_extraction("confirm")
    project.confirm_context(context(profile, kind))
    return project


class AcademicReviewTests(unittest.TestCase):
    def test_profiles_route_defaults_and_reject_empty_or_cross_profile_selection(self):
        for profile, expected in (("document", DOCUMENT_CRITICS), ("academic", ACADEMIC_CRITICS), ("mixed", ALL_CRITICS)):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as root:
                project = project_at(root, profile=profile)
                self.assertEqual(tuple(run.critic for run in project.run_local_prechecks()), expected)
                requests = project.prepare_ai_audits(provider="manual", model="test")
                self.assertEqual(tuple(row["critic"] for row in requests), expected)
                self.assertEqual(tuple(project.view()["review_critics"]), expected)
                self.assertEqual([run.critic for run in project.run_local_prechecks([expected[0], expected[0]])], [expected[0]])
                with self.assertRaises(ReviewStudioError):
                    project.run_local_prechecks([])
                with self.assertRaises(ReviewStudioError):
                    project.prepare_ai_audits([], provider="manual", model="test")
                if profile == "academic":
                    with self.assertRaises(ReviewStudioError):
                        project.run_local_prechecks([DOCUMENT_CRITICS[0]])
                self.assertEqual(project.integrity_errors(), [])

    def test_legacy_context_replay_and_academic_immutability(self):
        self.assertNotIn("review_profile", ReviewContext("通知").to_dict())
        with tempfile.TemporaryDirectory() as root:
            project = project_at(root)
            saved = (project.root / "context.json").read_bytes()
            project.confirm_context(context())
            self.assertEqual(saved, (project.root / "context.json").read_bytes())
            with self.assertRaises(ReviewStudioError):
                project.confirm_context(context(kind="empirical"))
            self.assertEqual(DocumentReviewProject(project.root).context().discipline, "social-science")

    def test_method_routing_and_citation_locations_are_conservative(self):
        for kind in ("theoretical", "empirical", "review", "engineering"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                project = project_at(root, kind=kind)
                runs = project.run_local_prechecks()
                findings = project.findings()
                missing = next(f for f in findings if f.check_id == "academic.citations.missing:2")
                self.assertIn("[2]", missing.evidence)
                self.assertEqual(missing.location.block_id, project.document().blocks[1].block_id)
                method_checks = [f.check_id for f in findings if f.critic == "academic_methods"]
                self.assertTrue(any(f"methods.{kind}." in check for check in method_checks))
                if kind != "empirical":
                    self.assertFalse(any("empirical" in check for check in method_checks))
                self.assertTrue(all(f.verification_state == "cannot-confirm" for f in findings))
                self.assertTrue(all(run.observations for run in runs))

    def test_verified_citation_requires_evidence_chain(self):
        with tempfile.TemporaryDirectory() as root:
            project = project_at(root)
            finding = project.run_local_prechecks(["academic_citations"])[0].findings[0].to_dict()
            finding["verification_state"] = "verified"
            self.assertTrue(validate_finding_dict(finding))
            finding["external_basis"].update(source_name="原始论文", locator="第 3 页", url_or_attachment="用户提供的摘录", application="该页仅支持相关关系，不支持因果")
            self.assertEqual(validate_finding_dict(finding), [])

    def test_academic_ui_precheck_generates_three_tasks(self):
        with tempfile.TemporaryDirectory() as root:
            project = project_at(root)
            app = StudioApp.create(root, project.root).act({"action": "run_local_prechecks", "data": {}})
            self.assertEqual(len(app.project.ai_requests()), 3)
            self.assertIn("3 份", app.notice)
            self.assertIn("academic_methods", project.prompt("academic_methods"))
            self.assertIn("theoretical", project.prompt("academic_methods"))
            shell = render_studio_shell("token")
            self.assertIn('id="review_profile"', shell)
            self.assertNotIn("__REVIEW_CONFIG__", shell)
            self.assertNotIn("五份", shell)

    def test_academic_external_review_revision_and_human_recheck(self):
        with tempfile.TemporaryDirectory() as root:
            project = project_at(root)
            critic = "academic_argument"
            proposal = project._deterministic_audit(critic, project.document(), project.context()).findings[0].to_dict()
            proposal["origin"] = "model-derived"
            proposal["check_id"] = None
            request = project.prepare_ai_audits([critic], provider="manual", model="reviewer")[0]
            response = {key: request[key] for key in ("request_id", "prompt_sha256", "provider", "model")}
            response.update(critic=critic, source_sha256=project.document().source.sha256, findings=[proposal])
            project.collect_model_audit(critic, json.dumps(response), provider="manual", model="reviewer")
            finding = project.findings()[0]
            project.decide_finding(finding.finding_id, "accept", reason="收窄因果主张")
            action = project.prepare_revision_plan()["actions"][0]
            project.set_revision_action_operation(action["action_id"], "replace_block", reason="仅收窄当前段落")
            hunk = project.propose_revision_hunk(action["action_id"], "现象与结果相关，现有材料不足以作出因果判断。", rationale="匹配证据强度")
            project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="已核对原文")
            revision_dir = project.finalize_revision()
            revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
            status = project.external_recheck_status(revision["revision_id"])
            external = status["requests"][0]
            self.assertEqual(external["original_request_id"], request["request_id"])
            self.assertIn("学术论证与反例", external["prompt"])
            response = {key: external[key] for key in ("request_id", "prompt_sha256")}
            response.update(revision_id=revision["revision_id"], revised_sha256=revision["revised_sha256"], critic=critic,
                            resolutions=[{"finding_id": finding.finding_id, "state": "resolved", "reason": "已收窄结论", "evidence": "现有材料不足以作出因果判断"}], new_findings=[])
            result = project.collect_external_recheck(revision["revision_id"], critic, json.dumps(response), provider="manual", model="reviewer")
            self.assertFalse(project.external_recheck_status(revision["revision_id"])["complete"])
            project.decide_external_resolution(revision["revision_id"], result["result_id"], finding.finding_id, "resolved", reason="人工核对证据与结论")
            audit = json.loads((project.export() / "audit.json").read_text(encoding="utf-8"))
            self.assertTrue(audit["external_recheck"]["complete"])
            self.assertEqual(audit["review_context"]["review_profile"], "academic")
            self.assertEqual(audit["independent_critics"], list(ACADEMIC_CRITICS))
            self.assertEqual(project.integrity_errors(), [])


class UnifiedServerTests(unittest.TestCase):
    def test_one_server_keeps_legacy_and_unified_projects_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            legacy = create_uploaded_project(root, filename="old.md", content=b"# Old\n\nOriginal.")
            server, url = serve_unified_app(data_dir=root, open_browser=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(path, data=None, token=True, research=False):
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["X-Argument-Workbench-Token" if research else "X-Document-Review-Token"] = server.app.token
                req = Request(url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
                with urlopen(req, timeout=10) as response:
                    return response.read().decode()

            try:
                home = request("")
                self.assertIn("文书与学术工作台", home)
                self.assertIn("review_profile", home)
                state = json.loads(request("api/state"))
                self.assertEqual(state["research_projects"][0]["directory"], legacy.name)
                with self.assertRaises(HTTPError) as caught:
                    request("research/api/open", {"directory": legacy.name}, token=False)
                self.assertEqual(caught.exception.code, 403)
                caught.exception.close()
                legacy_state = json.loads(request("research/api/open", {"directory": legacy.name}, research=True))
                self.assertEqual(legacy_state["selected"]["path"], str(legacy))
                research_shell = request("research/")
                self.assertIn("/research/api/state", research_shell)
                self.assertIn("/research/professional", research_shell)
                self.assertIn('href="/"', research_shell)
                upload = {"filename": "new.md", "content_base64": base64.b64encode(b"# New\n\nDraft.").decode()}
                state = json.loads(request("api/upload", upload))
                self.assertIsNotNone(state["selected"])
                self.assertEqual(server.research_app.project_dir, legacy)
                self.assertEqual(len(state["projects"]), 1)
                self.assertEqual(len(state["research_projects"]), 1)
                with self.assertRaises(HTTPError) as caught:
                    request("research/api/open", {"directory": "../old.argument-workbench"}, research=True)
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_explicit_legacy_project_opens_in_compatibility_view(self):
        with tempfile.TemporaryDirectory() as root:
            legacy = create_uploaded_project(root, filename="old.md", content=b"# Old")
            server, url = serve_unified_app(data_dir=root, project_dir=legacy, open_browser=False)
            try:
                self.assertTrue(url.endswith("/research/"))
                self.assertIsNone(server.app.project)
                self.assertEqual(server.research_app.project_dir, legacy)
            finally:
                server.server_close()

    def test_explicit_legacy_destination_does_not_require_suffix(self):
        from argument_workbench import initialize_workspace
        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root) / "custom-project"
            source = Path(__file__).parent / "fixtures" / "academic-review-paper.md"
            initialize_workspace(source, legacy)
            server, url = serve_unified_app(data_dir=root, project_dir=legacy, open_browser=False)
            try:
                self.assertTrue(url.endswith("/research/"))
                self.assertEqual(server.research_app.project_dir, legacy)
            finally:
                server.server_close()
