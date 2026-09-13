"""Paired local-rule fixtures; these do not measure model or peer-review accuracy."""

from __future__ import annotations

import copy
import unittest

from academic_review import academic_precheck_capabilities, academic_prechecks
from document_review_ingest import ingest_bytes
from document_review_model import ReviewContext
from review_profiles import ACADEMIC_PROTOCOLS, academic_protocol


# Different languages express scope and inferential limits without the former
# simplified-Chinese/English checklist words. They must never acquire a defect
# merely because a local lexicon cannot read their account.
LANGUAGE_CASES = {
    "en": ("The account concerns these two letters only. A rival reading treats the speaker as ironic; the repeated salutation favors the sincere reading, without settling the author's intentions elsewhere.", "References"),
    "zh-Hans": ("本文只解释这两封信。另一种读法把说话人视为反讽者；反复出现的问候支持真诚的读法，但不能据此推断作者在其他作品中的意图。", "参考文献"),
    "zh-Hant": ("本文只解釋這兩封信。另一種讀法把說話人視為反諷者；反覆出現的問候支持真誠的讀法，但不能據此推斷作者在其他作品中的意圖。", "參考文獻"),
    "de": ("Die Deutung gilt nur für diese zwei Briefe. Eine andere Lesart versteht die Stimme als ironisch; die wiederholte Begrüßung spricht für Aufrichtigkeit, entscheidet aber nichts über übrige Werke.", "Literaturverzeichnis"),
    "fr": ("Cette lecture porte seulement sur ces deux lettres. Une autre interprétation suppose une voix ironique ; la salutation répétée favorise la sincérité, sans établir les intentions de l'auteur ailleurs.", "Références"),
    "ja": ("この読みは二通の手紙だけを対象とする。別の読みでは語り手を皮肉屋とみなすが、繰り返される挨拶は誠実な読みを支える。他の作品での意図までは決められない。", "参考文献"),
    "ru": ("Толкование относится только к двум письмам. Другое прочтение считает голос ироническим; повторное приветствие говорит в пользу искренности, но не определяет замысел остальных произведений.", "Список литературы"),
    "la": ("Hæc interpretātiō ad duas epistulās tantum pertinet. Altera lectiō vocem ironicam putat; salūtātiō repetīta sinceritātem suādet, neque auctōris cœtera cōnsilia dēfinit.", "Bibliographia"),
}


def _context(kind="theoretical", discipline="humanities"):
    return ReviewContext("论文", review_profile="academic", research_type=kind, discipline=discipline)


def _check(text, critic, kind="theoretical"):
    document = ingest_bytes("paper.md", text.encode("utf-8"))
    before = document.to_dict()
    results = list(academic_prechecks(critic, document, _context(kind)))
    assert document.to_dict() == before, "Prechecks must not change original wording or IR"
    for block, finding in results:
        assert finding["evidence"] in block.text
        assert finding["verification_state"] == "cannot-confirm"
    return document, results


class AcademicPrecheckPrecisionTests(unittest.TestCase):
    def test_eight_languages_never_turn_unmatched_keywords_into_absence_verdicts(self):
        for language, (body, _) in LANGUAGE_CASES.items():
            for critic in ("academic_argument", "academic_methods", "academic_citations"):
                with self.subTest(language=language, critic=critic):
                    document, results = _check("# Study\n\n" + body, critic)
                    self.assertIn(body, document.plain_text)
                    self.assertEqual(results, [])

    def test_eight_language_numbered_citation_clean_defect_pairs_have_real_anchors(self):
        for language, (body, heading) in LANGUAGE_CASES.items():
            with self.subTest(language=language):
                template = "# Study\n\n" + body + " [{number}]\n\n## " + heading + "\n\n[1] " + body + "\n"
                _, clean = _check(template.format(number=1), "academic_citations")
                document, defect = _check(template.format(number=2), "academic_citations")
                self.assertEqual(clean, [])
                self.assertEqual(len(defect), 1)
                block, finding = defect[0]
                self.assertEqual(finding["check_id"], "academic.citations.missing:2")
                self.assertEqual(block.block_id, document.blocks[1].block_id)
                self.assertIn("[2]", finding["evidence"])
                self.assertEqual(finding["check_data"]["recognized_numbers"], [1])
                self.assertEqual(finding["check_data"]["reference_section_block_id"], document.blocks[2].block_id)

    def test_duplicate_bibliography_pair_preserves_exact_entry_evidence(self):
        for language, (body, heading) in LANGUAGE_CASES.items():
            with self.subTest(language=language):
                prefix = "# Study\n\n" + body + " [1]\n\n## " + heading + "\n\n[1] " + body + "\n\n"
                _, clean = _check(prefix + "[2] Second entry.", "academic_citations")
                document, defect = _check(prefix + "[1] Second entry.", "academic_citations")
                self.assertEqual(clean, [])
                self.assertEqual(len(defect), 1)
                block, finding = defect[0]
                self.assertEqual(finding["check_id"], "academic.citations.duplicate:1")
                self.assertEqual(finding["evidence"], "[1] Second entry.")
                self.assertEqual(finding["check_data"]["entry_block_ids"], [document.blocks[3].block_id, block.block_id])

    def test_unknown_styles_ranges_and_non_citation_markers_are_outside_rule_scope(self):
        for text in (
            "A claim [2] with footnotes elsewhere.",
            "A claim [2].\n\n## References\n\nSmith (2020), A Book.",
            "Ranges [1–3] and x[2] or [4](https://example.test) are not single citations.\n\n## References\n\n[1] A Book.",
            "```python\nx[2]\n```\n\n## References\n\n[1] A Book.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_check(text, "academic_citations")[1], [])

    def test_appendix_numbering_does_not_extend_the_bibliography(self):
        text = "A claim [1].\n\n## 7. References:\n\n[1] A Book.\n\n## Appendix\n\n[1] An example, not a reference."
        self.assertEqual(_check(text, "academic_citations")[1], [])

    def test_methods_pairs_only_report_explicit_scoped_editorial_placeholders(self):
        for kind, heading in (("theoretical", "Proof"), ("empirical", "Sampling"), ("review", "Search strategy"), ("engineering", "Evaluation")):
            with self.subTest(kind=kind):
                prefix = "# Study\n\n## " + heading + "\n\n"
                _, clean = _check(prefix + "The account is complete for the stated task.", "academic_methods", kind)
                document, defect = _check(prefix + "[TODO: complete this account]", "academic_methods", kind)
                self.assertEqual(clean, [])
                self.assertEqual(len(defect), 1)
                self.assertEqual(defect[0][1]["check_id"], "academic.methods." + kind + ".draft_placeholder")
                self.assertEqual(defect[0][1]["check_data"]["section_block_id"], document.blocks[1].block_id)
        for text in ("## Introduction\n\nTODO", "## Methods\n\nThe archive contains a note marked TODO.", "## Methods\n\n## Results\n\nTODO", "## Methods\n\n```\nTODO\n```", "## Evaluation\n\nTODO"):
            with self.subTest(non_method_placeholder=text):
                self.assertEqual(_check(text, "academic_methods")[1], [])

    def test_causal_pairs_distinguish_literal_claim_from_negation_and_attribution(self):
        for text in (
            "The intervention does not cause this outcome.",
            "There is no evidence that the intervention causes the outcome.",
            "We ask whether the intervention causes the outcome.",
            'The archive states "the intervention causes the outcome." We examine that claim.',
            "作者写道：“现象导致结果。”本文只讨论该句话的修辞。",
            "現有材料不能證明現象導致結果。",
            "> Phenomenon causes outcome.",
            "```text\nPhenomenon causes outcome.\n```",
            "## References\n\n[1] What causes outcomes?",
        ):
            with self.subTest(nonassertion=text):
                self.assertEqual(_check(text, "academic_argument")[1], [])
        for text in ("现象导致结果。", "現象導致結果。", "The intervention causes the outcome."):
            with self.subTest(assertion=text):
                _, findings = _check(text, "academic_argument")
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0][1]["check_id"], "academic.argument.causal_bridge")
                self.assertIn("不能仅因", findings[0][1]["suggested_action"])

    def test_capability_disclosure_is_explicit_and_not_an_accuracy_claim(self):
        capabilities = academic_precheck_capabilities(_context())
        self.assertEqual(capabilities["literal_cue_languages"], ["zh-Hans", "zh-Hant", "en"])
        for key in ("semantic_language_detection", "semantic_completeness_checked", "negative_absence_findings", "external_sources_checked"):
            self.assertIs(capabilities[key], False)
        self.assertEqual(len(capabilities["notes"]), 3)
        _, pending_type = _check("A paper.", "academic_methods", "unspecified")
        self.assertEqual([row[1]["check_id"] for row in pending_type], ["academic.methods.research_type"])


class AcademicProtocolScopeTests(unittest.TestCase):
    def test_selected_research_task_replaces_universal_method_checklist(self):
        theory = academic_protocol("academic_methods", discipline="humanities", research_type="theoretical")
        empirical = academic_protocol("academic_methods", discipline="social-science", research_type="empirical")
        review = academic_protocol("academic_methods", research_type="review")
        engineering = academic_protocol("academic_methods", research_type="engineering")
        self.assertEqual(theory["confirmed_scope"], {"discipline": "humanities", "research_type": "theoretical"})
        self.assertNotEqual(theory["checks"], empirical["checks"])
        self.assertTrue(all("实证：" not in check for check in theory["checks"]))
        self.assertIn("不得套用抽样代表性", theory["discipline_focus"])
        self.assertIn("叙述性", review["research_type_focus"])
        self.assertIn("仅在声称", review["research_type_focus"])
        self.assertIn("如归因于某组件", " ".join(engineering["checks"]))
        self.assertIn("限定词、否定和引述归属", theory["applicability"])

    def test_scoped_protocol_is_detached_from_shared_historical_templates(self):
        before = copy.deepcopy(ACADEMIC_PROTOCOLS)
        returned = academic_protocol("academic_argument", research_type="theoretical")
        returned["checks"].clear()
        returned["confirmed_scope"]["research_type"] = "empirical"
        self.assertEqual(ACADEMIC_PROTOCOLS, before)
        self.assertTrue(academic_protocol("academic_argument")["checks"])
        with self.assertRaises(ValueError):
            academic_protocol("academic_methods", research_type="randomized-only")


if __name__ == "__main__":
    unittest.main()
