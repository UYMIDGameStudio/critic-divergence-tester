from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import argument_ui
import argument_workbench as workbench
from argument_app import render_product_shell
from studio_web.research import research_shell


SAMPLES = (
    ("English Finding accepted; original evidence.", "cp1252"),
    ("问题：新建项目，论证需要证据。", "gb18030"),
    ("問題：新建專案，論證需要證據。", "big5"),
    ("Die Straße: überprüfbare Gründe.", "cp1252"),
    ("Une démonstration étayée par des preuves.", "cp1252"),
    ("日本語の論証には証拠が必要です。", "shift_jis"),
    ("Русский аргумент требует доказательств.", "cp1251"),
    ("Rātiō et causae: æquus, œconomia.", "utf-8"),
)


def language_project(root: Path, text: str, encoding: str):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "original.txt"
    source.write_bytes(text.encode(encoding))
    paths = workbench.initialize_workspace(source, root / "project", encoding=encoding)
    raw = {
        "schema_version": 1, "artifact": "argument-ir", "scope": "social-science",
        "source": {"name": source.name, "sha256": workbench.sha256_bytes(source.read_bytes())},
        "claims": [{"id": "C1", "text": text, "source_quote": text, "position": "P1",
                    "types": ["conceptual"], "methods": ["conceptual-analysis"],
                    "role": "conclusion", "extraction": "explicit", "uncertainty": ""}],
        "evidence": [], "assumptions": [], "citations": [], "relations": [], "unverified": [],
    }
    workbench.collect_raw_attempt(paths, workbench.json_bytes(raw), method="file",
                                  source_name="response.json", producer_label="test")
    workbench.rebuild_workspace(paths)
    return paths, source


class ResearchUITests(unittest.TestCase):
    def test_eight_language_professional_views_use_bound_decoding(self):
        for text, encoding in SAMPLES:
            with self.subTest(encoding=encoding, text=text), tempfile.TemporaryDirectory() as temp:
                paths, _ = language_project(Path(temp), text, encoding)
                view = argument_ui.build_project_view(paths)
                self.assertEqual(view["manuscript"][0]["text"], text)
                self.assertEqual(view["claims"][0]["source_quote"], text)
                self.assertEqual(workbench.verify_project_versions(paths.root), [])

    def test_packaged_shells_have_safe_tokens_and_explicit_language_controls(self):
        for render in (render_product_shell, argument_ui.render_app_shell):
            shell = render('</script><script>alert("x")</script>')
            self.assertIn('lang="zh-Hant"', shell)
            self.assertIn('id="research-language"', shell)
            self.assertIn('value="en"', shell)
            self.assertNotIn('__RESEARCH_MESSAGES__', shell)
            self.assertNotIn('__STYLES__', shell)
            self.assertNotIn('__SCRIPTS__', shell)
            self.assertNotIn('</script><script>alert', shell)
        product = render_product_shell("test-token")
        self.assertIn('content_base64:', product)
        self.assertIn('file.arrayBuffer()', product)
        self.assertNotIn('await f.text()', product)

    def test_research_resource_loader_rejects_unknown_shell(self):
        with self.assertRaises(ValueError):
            research_shell("../shell")


if __name__ == "__main__":
    unittest.main()
