"""Native Word text edits keep link ownership and reversible revision markup."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from document_review_ingest import ingest_bytes
from document_review_word import preserve_docx, WordEditUnsupported
from test.test_document_review_word import package, paragraph, body_paragraphs, rendered_text, formatted_characters, W, R, NS


def run(text, style=''):
    return '<w:r>' + ('<w:rPr>' + style + '</w:rPr>' if style else '') + '<w:t xml:space="preserve">' + escape(text) + '</w:t></w:r>'


def linked(text='CITE', anchor='target', style='<w:i/>'):
    return '<w:hyperlink w:anchor="' + anchor + '" w:tooltip="Source tip">' + run(text, style) + '</w:hyperlink>'


def edit(raw, before, after, tracked=False):
    original = ingest_bytes('paper.docx', raw)
    revised = deepcopy(original)
    next(block for block in revised.blocks if block.text == before).text = after
    return preserve_docx(raw, original, revised, tracked=tracked)


class WordHyperlinkEditTests(unittest.TestCase):
    def assert_roundtrip(self, raw, before, after):
        clean, report = edit(raw, before, after)
        tracked, _ = edit(raw, before, after, True)
        clean_p, tracked_p, original_p = body_paragraphs(clean)[0], body_paragraphs(tracked)[0], body_paragraphs(raw)[0]
        self.assertTrue(report['source_layout_preserved'])
        self.assertEqual(rendered_text(clean_p), after)
        self.assertEqual(rendered_text(tracked_p), after)
        self.assertEqual(rendered_text(tracked_p, accept=False), before)
        self.assertEqual(formatted_characters(tracked_p), formatted_characters(clean_p))
        self.assertEqual(formatted_characters(tracked_p, accept=False), formatted_characters(original_p))
        for output in (clean, tracked):
            with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(io.BytesIO(output)) as new:
                for name in old.namelist():
                    if name != 'word/document.xml': self.assertEqual(old.read(name), new.read(name), name)
        self.assertEqual([node.attrib for node in original_p.findall('w:hyperlink', NS)], [node.attrib for node in clean_p.findall('w:hyperlink', NS)])
        return clean_p, tracked_p

    def test_surrounding_text_edits_keep_the_original_link_and_styles(self):
        raw = package('<w:p>' + run('Alpha ', '<w:b/>') + linked() + run(' omega', '<w:u w:val="single"/>') + '</w:p>')
        clean, _ = self.assert_roundtrip(raw, 'Alpha CITE omega', 'Better CITE omega')
        self.assertEqual(rendered_text(clean.find('w:hyperlink', NS)), 'CITE')

    def test_link_label_edits_keep_target_and_revision_ownership(self):
        raw = package('<w:p>' + run('Read ') + linked() + run(' today.') + '</w:p>')
        clean, tracked = self.assert_roundtrip(raw, 'Read CITE today.', 'Read STUDY today.')
        self.assertEqual(rendered_text(clean.find('w:hyperlink', NS)), 'STUDY')
        self.assertTrue(tracked.find('w:hyperlink', NS).findall('w:ins', NS))

    def test_boundary_insertions_are_plain_text_and_do_not_expand_a_link(self):
        raw = package('<w:p>' + linked('ONE') + linked('TWO', 'second') + '</w:p>')
        for after, expected in [('xONETWO', ['ONE','TWO']), ('ONE xTWO', ['ONE','TWO']), ('ONETWO x', ['ONE','TWO'])]:
            with self.subTest(after=after):
                clean, _ = self.assert_roundtrip(raw, 'ONETWO', after)
                self.assertEqual([rendered_text(node) for node in clean.findall('w:hyperlink', NS)], expected)

    def test_deletion_can_cross_boundaries_without_reassigning_new_text(self):
        raw = package('<w:p>' + run('Keep before ') + linked('citation') + run(' after end') + '</w:p>')
        self.assert_roundtrip(raw, 'Keep before citation after end', 'Keep end')

    def test_replacement_across_link_boundaries_is_rejected_as_ambiguous(self):
        raw = package('<w:p>' + run('AB') + linked('CD') + run('EF') + '</w:p>')
        for tracked in (False,True):
            with self.subTest(tracked=tracked), self.assertRaisesRegex(WordEditUnsupported, '超链接边界'):
                edit(raw, 'ABCDEF', 'XY', tracked)

    def test_eight_language_label_changes_keep_the_same_target(self):
        for label in ['English','简体中文','繁體中文','Deutsch Straße','Français été','日本語','Русский','Latīna Rōma']:
            with self.subTest(label=label):
                raw = package('<w:p>' + linked(label) + '</w:p>')
                after=label[0]+'!'+label[1:]
                clean,_ = self.assert_roundtrip(raw,label,after)
                self.assertEqual(rendered_text(clean.find('w:hyperlink',NS)),after)

    def test_multiple_styled_link_runs_and_separate_edits_keep_ownership(self):
        raw=package('<w:p>'+run('Before AAA ')+ '<w:hyperlink w:anchor="target">'+run('CIT','<w:b/>')+run('ATION','<w:i/>')+'</w:hyperlink>'+run(' ZZZ after')+'</w:p>')
        clean,_=self.assert_roundtrip(raw,'Before AAA CITATION ZZZ after','Before BBB CITING ZZZ after')
        self.assertEqual(rendered_text(clean.find('w:hyperlink',NS)),'CITING')

    def test_table_and_note_edits_keep_part_scoped_relationships(self):
        from test import test_docx_hyperlinks as fixtures
        raw=fixtures.linked_package();original=ingest_bytes('paper.docx',raw);revised=deepcopy(original)
        for block in revised.blocks:
            if block.text=='Appendix':block.text='Appendices'
            if block.text=='See here':block.text='See there'
        for tracked in (False,True):
            with self.subTest(tracked=tracked):
                result,_=preserve_docx(raw,original,revised,tracked=tracked)
                with zipfile.ZipFile(io.BytesIO(raw)) as before,zipfile.ZipFile(io.BytesIO(result)) as after:
                    for name in before.namelist():
                        if name not in {'word/document.xml','word/footnotes.xml'}:self.assertEqual(before.read(name),after.read(name),name)
                    foot=ET.fromstring(after.read('word/footnotes.xml')).find('w:footnote[@w:id="7"]',NS)
                    cell=ET.fromstring(after.read('word/document.xml')).find('.//w:tc',NS)
                    self.assertEqual(rendered_text(foot),'See there')
                    self.assertEqual(rendered_text(cell),'Appendices')
                    if tracked:
                        self.assertEqual(rendered_text(foot,accept=False),'See here')
                        self.assertEqual(rendered_text(cell,accept=False),'Appendix')

    def test_real_approved_export_keeps_native_clean_and_tracked_files(self):
        from test import test_document_review_word as fixtures
        raw=package('<w:p>'+run('相关人员及时完成报名。')+linked('详见研究')+'</w:p>')
        with tempfile.TemporaryDirectory() as directory:
            helper=fixtures.WordPreservationTests()
            project,_=helper.finalize_project(Path(directory),raw,'相关人员周五前完成报名。详见研究')
            output=project.export()
            report=json.loads((output/'track-changes-capability.json').read_text(encoding='utf-8'))
            self.assertTrue(report['source_layout_preserved'])
            self.assertTrue(report['native_track_changes'])
            self.assertTrue((output/'Word修订标记.docx').is_file())
            self.assertEqual((project.root/'source/source.docx').read_bytes(),raw)

    def test_empty_links_do_not_capture_inserted_text(self):
        raw = package('<w:p>' + linked('') + linked('', 'second') + run('Body') + '</w:p>')
        clean,_ = self.assert_roundtrip(raw,'Body','New Body')
        self.assertEqual([rendered_text(node) for node in clean.findall('w:hyperlink',NS)],['',''])

    def test_fields_bookmarks_nested_links_and_existing_revisions_stay_protected(self):
        for content in ['<w:fldSimple w:instr="HYPERLINK x"/>', '<w:bookmarkStart w:id="4" w:name="x"/>', linked('nested')]:
            with self.subTest(content=content):
                raw=package('<w:p><w:hyperlink w:anchor="target">'+run('Read')+content+'</w:hyperlink></w:p>')
                original=ingest_bytes('paper.docx',raw); revised=deepcopy(original)
                revised.blocks[0].text += '!'
                with self.assertRaises(WordEditUnsupported): preserve_docx(raw,original,revised)

    def test_linked_paragraph_split_requires_explicit_link_mapping(self):
        raw=package('<w:p>'+run('Read ')+linked()+'</w:p>')
        original=ingest_bytes('paper.docx',raw);revised=deepcopy(original)
        revised.blocks[0].text='Read'
        new=deepcopy(original.blocks[0]);new.block_id='new';new.text='CITE';new.attrs['split_from_block_id']=original.blocks[0].block_id
        revised.blocks.insert(1,new)
        with self.assertRaisesRegex(WordEditUnsupported,'超链接.*拆分'): preserve_docx(raw,original,revised)

    def test_cell_replacement_cannot_move_text_across_linked_paragraphs(self):
        raw=package('<w:tbl><w:tr><w:tc><w:p>'+linked('AB')+'</w:p>'+paragraph('CD')+'</w:tc></w:tr></w:tbl>')
        with self.assertRaisesRegex(WordEditUnsupported,'单元格.*超链接'):
            edit(raw,'ABCD','XY')

    def test_note_markers_inside_links_keep_their_existing_guard_and_global_limit(self):
        from test import test_docx_notes as fixtures
        reference='<w:footnoteReference w:id="7"/>'
        raw=fixtures.note_package(body='<w:p><w:hyperlink w:anchor="target"><w:r><w:t>A</w:t>'+reference+'<w:t>B</w:t></w:r></w:hyperlink></w:p>')
        with self.assertRaisesRegex(WordEditUnsupported,'注释引用'):edit(raw,'AB','XY')
        body='<w:p>'+''.join('<w:hyperlink w:anchor="target"><w:r><w:t>X</w:t>'+reference*260+'</w:r></w:hyperlink>' for _ in range(4))+'</w:p>'
        raw=fixtures.note_package(body=body)
        with self.assertRaisesRegex(WordEditUnsupported,'标记过多'):edit(raw,'XXXX','Prefix XXXX')

    def test_many_unchanged_links_survive_a_small_prefix_edit(self):
        labels=['L'+str(i).zfill(4) for i in range(500)]
        raw=package('<w:p>'+''.join(linked(label,'target'+str(i)) for i,label in enumerate(labels))+'</w:p>')
        clean,_=self.assert_roundtrip(raw,''.join(labels),'Prefix '+''.join(labels))
        self.assertEqual([rendered_text(node) for node in clean.findall('w:hyperlink',NS)],labels)

    def test_repeated_text_cannot_choose_between_distinct_link_owners(self):
        cases=[(linked('Smith2020','paperA')+linked('Smith2020','paperB'),'Smith2020Smith2020','Smith2020'),
               (run('a')+linked('a')+run('a'),'aaa','aa'),
               (linked('aa'),'aa','aaa')]
        for content,before,after in cases:
            with self.subTest(before=before,after=after):
                raw=package('<w:p>'+content+'</w:p>')
                with self.assertRaisesRegex(WordEditUnsupported,'重复文字'):
                    edit(raw,before,after)
        # Repeated letters wholly inside one link do not change ownership.
        self.assert_roundtrip(package('<w:p>'+linked('aa')+'</w:p>'),'aa','a')
        raw=package('<w:p>'+run('a'*12010)+linked('B')+'</w:p>')
        with self.assertRaisesRegex(WordEditUnsupported,'安全上限'):
            edit(raw,'a'*12010+'B','a'*12009+'B')


if __name__=='__main__': unittest.main()
