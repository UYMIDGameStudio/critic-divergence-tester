"""Verify copied portable instructions retain valid documentation links."""

from pathlib import Path
import re
import shutil
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from scripts import build_portable


class PortableGuideLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="portable-guide-test-")
        self.addCleanup(temporary.cleanup)
        self.bundle = Path(temporary.name)
        self.docs = self.bundle / "docs"
        self.docs.mkdir()

    def write_doc(self, name):
        path = self.docs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Bundled document\n", encoding="utf-8")

    def test_current_guide_links_resolve_from_bundle_root(self):
        shutil.copytree(build_portable.ROOT / "docs", self.docs, dirs_exist_ok=True)
        source = (self.docs / "portable-guide.md").read_text(encoding="utf-8")
        guide_path = self.bundle / "使用说明.md"
        guide_path.write_text(build_portable._rebase_guide_links(source, self.docs), encoding="utf-8")
        links = re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", guide_path.read_text(encoding="utf-8"))
        self.assertTrue(links)
        for destination in links:
            url = urlsplit(destination)
            if not url.scheme and not url.netloc and url.path:
                with self.subTest(destination=destination):
                    self.assertTrue(url.path.startswith("docs/"))
                    self.assertTrue((guide_path.parent / unquote(url.path)).is_file())

    def test_new_documents_need_no_filename_registry(self):
        self.write_doc("future/topic.md")
        self.write_doc("文档 新增.md")
        source = ('[Future](future/topic.md#details)\n'
                  '[Encoded](%E6%96%87%E6%A1%A3%20%E6%96%B0%E5%A2%9E.md?view=all#章节 "Title")\n')
        expected = ('[Future](docs/future/topic.md#details)\n'
                    '[Encoded](docs/%E6%96%87%E6%A1%A3%20%E6%96%B0%E5%A2%9E.md?view=all#章节 "Title")\n')
        self.assertEqual(build_portable._rebase_guide_links(source, self.docs), expected)

    def test_anchors_external_links_and_unrelated_text_stay_unchanged(self):
        source = ('[Here](#section) [Web](https://example.test/guide.md#section)\n'
                  '[Shared](//example.test/guide.md) [Email](mailto:guide.md@example.test)\n'
                  'Mention (guide.md), `guide.md`, and guide.md in ordinary prose.\n'
                  '![Image](diagram.png) [Download](sample.zip)\\[Literal](guide.md)\n')
        self.assertEqual(build_portable._rebase_guide_links(source, self.docs), source)

    def test_missing_document_stops_build_before_publishing_broken_link(self):
        with self.assertRaisesRegex(ValueError, "missing bundled document"):
            build_portable._rebase_guide_links("[Missing](unbundled.md)", self.docs)

    def test_unsafe_local_document_paths_are_rejected(self):
        (self.bundle / "outside.md").write_text("outside", encoding="utf-8")
        for destination in ("../outside.md", "%2e%2e/outside.md", "/absolute.md",
                            "nested/../../outside.md", r"nested\outside.md", "nested/%00outside.md"):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "Unsafe"):
                build_portable._rebase_guide_links(f"[Unsafe]({destination})", self.docs)

    def test_document_symlink_cannot_escape_bundled_docs(self):
        outside = self.bundle / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        try:
            (self.docs / "linked.md").symlink_to(outside)
        except OSError:
            self.skipTest("Creating symlinks is not available on this host")
        with self.assertRaisesRegex(ValueError, "escapes bundled docs"):
            build_portable._rebase_guide_links("[Link](linked.md)", self.docs)


if __name__ == "__main__":
    unittest.main()
