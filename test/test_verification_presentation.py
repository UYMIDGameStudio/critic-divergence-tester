"""Distinguish imported verification claims from application/source checks."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from document_review_model import review_verification_context
from test.test_delivery import ready


ROOT = Path(__file__).resolve().parents[1]
CRITIC = "expression_ambiguity"
MODEL_LABEL = "模型声明已核实（应用未独立核验）"


class VerificationPresentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = ready(Path(temporary.name))

    def import_verified(self):
        finding = self.project.findings()[0].to_dict()
        finding.update(finding_id="MODEL-VERIFIED", verification_state="verified",
                       external_basis={}, check_data={})
        request = self.project.prepare_ai_audits([CRITIC], provider="test", model="test-model")[0]
        payload = {
            **{key: request[key] for key in (
                "request_id", "prompt_sha256", "provider", "model", "critic", "source_sha256",
            )},
            "findings": [finding],
        }
        run = self.project.collect_model_audit(
            CRITIC, json.dumps(payload, ensure_ascii=False),
            provider=request["provider"], model=request["model"], request_id=request["request_id"],
        )
        return run.findings[0]

    def test_imported_verified_is_qualified_in_view_and_both_exports_without_rewriting_history(self):
        finding = self.import_verified()
        run_path = self.project._active_audit_run_records()[CRITIC][0]
        before = run_path.read_bytes()
        view = self.project.view()
        context = view["verification_context"][finding.finding_id]
        self.assertEqual(context["source_kind"], "model")
        self.assertEqual(context["declared_state"], "verified")
        self.assertEqual(context["display_label"], MODEL_LABEL)
        self.assertEqual(context["application_external_verification"], "not-established")
        self.assertEqual(view["findings"][0]["verification_state"], "verified")
        self.assertNotIn("verification_context", view["findings"][0])

        output = self.project.export_ai_reviews()
        snapshot = json.loads((output / "AI审查结果.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["verification_context"][finding.finding_id], context)
        self.assertEqual(snapshot["findings"][0]["verification_state"], "verified")
        ai_report = (output / "AI审查报告.md").read_text(encoding="utf-8")
        self.assertIn(MODEL_LABEL, ai_report)
        self.assertIn("Close-reading context checks were not supplied", ai_report)
        self.assertNotIn("核实状态：verified", ai_report)

        self.project.decide_finding(finding.finding_id, "defer", reason="Check the declared external basis.")
        output = self.project.export()
        audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["verification_context"][finding.finding_id], context)
        self.assertEqual(audit["findings"][0]["verification_state"], "verified")
        reports = [p.read_text(encoding="utf-8") for p in output.glob("*.md")]
        self.assertTrue(any(context["display_label_en"] in report for report in reports))
        self.assertTrue(any("Close-reading context checks were not supplied" in report for report in reports))
        self.assertEqual(run_path.read_bytes(), before)
        self.assertNotIn("verification_context", json.loads(before))
        self.assertEqual(self.project.integrity_errors(), [])

    def test_actual_local_run_is_rule_derived_and_presented_as_local(self):
        finding = self.project.findings()[0]
        self.assertEqual(finding.origin, "rule-derived")
        view = self.project.view()
        context = view["verification_context"][finding.finding_id]
        self.assertEqual(context["source_kind"], "local")
        self.assertEqual(context["display_label"], finding.verification_state)
        self.project.decide_finding(finding.finding_id, "defer", reason="Local check awaits review.")
        output = self.project.export()
        audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["verification_context"][finding.finding_id]["source_kind"], "local")
        reports = [p.read_text(encoding="utf-8") for p in output.glob("*.md")]
        self.assertFalse(any("Close-reading context checks were not supplied" in report for report in reports))

    def test_source_authority_covers_followup_and_unknown_records(self):
        # Legacy local findings used model-derived too; trust their parent run.
        finding = {"finding_id": "F1", "verification_state": "verified", "origin": "model-derived", "check_data": {}}
        before = copy.deepcopy(finding)
        for run, expected in (
            ({"model_label": "deterministic-local-rules"}, "local"),
            ({"model_label": "manual-import:test/model"}, "model"),
            ({"origin": "external-recheck-followup", "declared_model_metadata": {}}, "model"),
            ({}, "unknown"),
        ):
            with self.subTest(expected=expected):
                context = review_verification_context([{**run, "findings": [finding]}])["F1"]
                self.assertEqual(context["source_kind"], expected)
                self.assertNotEqual(context["display_label"], "verified")
        self.assertEqual(finding, before)

    def test_browser_labels_use_authoritative_context_in_english_and_traditional_chinese(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the browser presentation check")
        finding = self.project.findings()[0].to_dict()
        finding.update(verification_state="verified", check_data={})
        labels = json.loads((ROOT / "studio_web/locales-system.json").read_text(encoding="utf-8"))
        script = r"""
const fs = require('fs'), vm = require('vm');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = [];
for (const lang of ['en', 'zh-Hant']) {
  const sandbox = {
    state: {selected: {verification_context: data.context}}, critics: {},
    esc: value => String(value ?? ''),
    tr: value => data.labels[value]?.[lang] || value,
    ui: (parts, ...values) => parts.reduce((out, part, i) => out + part + (values[i] ?? ''), ''),
    pdfDisplayText: value => String(value ?? ''), adversarialFindingDetail: () => '',
  };
  vm.createContext(sandbox);
  vm.runInContext(data.source, sandbox);
  results.push(vm.runInContext('findingCard(' + JSON.stringify(data.finding) + ')', sandbox));
  sandbox.state.selected.verification_context = data.localContext;
  results.push(vm.runInContext('closeReadingDetail(' + JSON.stringify(data.finding) + ')', sandbox));
}
process.stdout.write(JSON.stringify(results));
"""
        payload = {
            "finding": finding, "labels": labels,
            "source": (ROOT / "studio_web/views.js").read_text(encoding="utf-8"),
            "context": review_verification_context([{"model_label": "manual-import:test/model", "findings": [finding]}]),
            "localContext": review_verification_context([{"model_label": "deterministic-local-rules", "findings": [finding]}]),
        }
        result = subprocess.run([node, "-e", script], input=json.dumps(payload), capture_output=True,
                                text=True, encoding="utf-8", check=True, timeout=20)
        english, local_en, traditional, local_zh = json.loads(result.stdout)
        self.assertIn(labels[MODEL_LABEL]["en"], english)
        self.assertIn(labels[MODEL_LABEL]["zh-Hant"], traditional)
        self.assertNotIn('<span class="pill">verified</span>', english)
        self.assertEqual(local_en, "")
        self.assertEqual(local_zh, "")


if __name__ == "__main__":
    unittest.main()
