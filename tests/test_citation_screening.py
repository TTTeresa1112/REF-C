import io
import unittest
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from citation_screening.parsers import parse_manuscript
from citation_screening.pipeline import execute_screening, prepare_screening, run_screening


class ParserTests(unittest.TestCase):
    @staticmethod
    def _append_superscript_run(parent, text):
        run = OxmlElement("w:r")
        props = OxmlElement("w:rPr")
        align = OxmlElement("w:vertAlign")
        align.set(qn("w:val"), "superscript")
        props.append(align)
        run.append(props)
        value = OxmlElement("w:t")
        value.text = text
        run.append(value)
        parent.append(run)

    def test_word_superscript_range_and_reference_section(self):
        document = Document()
        paragraph = document.add_paragraph()
        paragraph.add_run("Drug A reduced inflammation ")
        first = paragraph.add_run("1")
        first.font.superscript = True
        dash = paragraph.add_run("–")
        dash.font.superscript = True
        last = paragraph.add_run("2")
        last.font.superscript = True
        paragraph.add_run(".")
        document.add_paragraph("References")
        document.add_paragraph("[1] This bibliography entry must not become a citation sentence.")
        buffer = io.BytesIO()
        document.save(buffer)

        sentences, refs = parse_manuscript(
            buffer.getvalue(), "paper.docx", "1. First ref\n2. Second ref"
        )
        self.assertEqual(len(sentences), 1)
        self.assertEqual(sentences[0]["citations"], [{"rid": "B1"}, {"rid": "B2"}])
        self.assertEqual(set(refs), {"B1", "B2"})

    def test_word_context_window(self):
        document = Document()
        document.add_paragraph("Earlier work established the mechanism. Drug A reduced inflammation [1]. The effect depended on dose.")
        buffer = io.BytesIO()
        document.save(buffer)
        sentences, _ = parse_manuscript(buffer.getvalue(), "paper.docx", "1. First ref")
        self.assertEqual(sentences[0]["context_before"], "Earlier work established the mechanism.")
        self.assertEqual(sentences[0]["sentence_text"], "Drug A reduced inflammation [1].")
        self.assertEqual(sentences[0]["context_after"], "The effect depended on dose.")

    def test_word_field_and_content_control_citations(self):
        document = Document()
        field_paragraph = document.add_paragraph("Field citation ")
        begin_run = OxmlElement("w:r")
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        begin_run.append(begin)
        field_paragraph._p.append(begin_run)
        instruction_run = OxmlElement("w:r")
        instruction = OxmlElement("w:instrText")
        instruction.text = " ADDIN EN.CITE DATA "
        instruction_run.append(instruction)
        field_paragraph._p.append(instruction_run)
        separate_run = OxmlElement("w:r")
        separate = OxmlElement("w:fldChar")
        separate.set(qn("w:fldCharType"), "separate")
        separate_run.append(separate)
        field_paragraph._p.append(separate_run)
        self._append_superscript_run(field_paragraph._p, "1")
        end_run = OxmlElement("w:r")
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        end_run.append(end)
        field_paragraph._p.append(end_run)
        field_paragraph.add_run(".")

        control_paragraph = document.add_paragraph("Control citation ")
        content_control = OxmlElement("w:sdt")
        content = OxmlElement("w:sdtContent")
        self._append_superscript_run(content, "2")
        content_control.append(content)
        control_paragraph._p.append(content_control)
        control_paragraph.add_run(".")

        buffer = io.BytesIO()
        document.save(buffer)
        sentences, _ = parse_manuscript(buffer.getvalue(), "fields.docx", "1. First\n2. Second")
        self.assertEqual(len(sentences), 2)
        self.assertEqual(sentences[0]["citations"], [{"rid": "B1"}])
        self.assertEqual(sentences[1]["citations"], [{"rid": "B2"}])
        self.assertNotIn("ADDIN EN.CITE", sentences[0]["sentence_text"])

    def test_namespaced_nlm_xml(self):
        xml = b'''<article xmlns="http://jats.nlm.nih.gov"><body><p>Claim <xref ref-type="bibr" rid="B1">[1]</xref>.</p></body><back><ref-list><ref id="B1"><label>1</label><element-citation><article-title>Article title</article-title><pub-id pub-id-type="doi">10.1/test</pub-id></element-citation></ref></ref-list></back></article>'''
        sentences, refs = parse_manuscript(xml, "paper.xml")
        self.assertEqual(sentences[0]["citations"], [{"rid": "B1"}])
        self.assertEqual(refs["B1"]["title"], "Article title")
        self.assertEqual(refs["B1"]["doi"], "10.1/test")


class PipelineTests(unittest.TestCase):
    @patch("citation_screening.pipeline.screen_pair")
    @patch("citation_screening.pipeline.fetch_metadata")
    def test_pipeline_and_html_without_external_calls(self, fetch_metadata, screen_pair):
        fetch_metadata.return_value = {
            "title": "Article title", "abstract": "Supporting abstract",
            "authors": [], "metadata_source": "PubMed", "metadata_error": "",
        }
        screen_pair.return_value = {"result": "匹配", "reason": "摘要支持主要主张。"}
        xml = b'''<article><body><p>Claim <xref ref-type="bibr" rid="B1">[1]</xref>.</p></body><back><ref-list><ref id="B1"><label>1</label><element-citation><article-title>Article title</article-title></element-citation></ref></ref-list></back></article>'''
        data = run_screening(xml, "paper.xml")
        self.assertEqual(data["statistics"]["匹配"], 1)
        self.assertIn("REF-C 引用内容初筛报告", data["html"])
        self.assertIn('id="statusFilter"', data["html"])
        self.assertIn('id="sourceFilter"', data["html"])
        self.assertIn("目标引用句", data["html"])
        self.assertEqual(data["results"][0]["result"], "匹配")

    @patch("citation_screening.pipeline.screen_pair")
    @patch("citation_screening.pipeline.fetch_metadata")
    def test_prepare_never_calls_ai_and_execute_uses_estimate(self, fetch_metadata, screen_pair):
        fetch_metadata.return_value = {
            "title": "Article title", "abstract": "Supporting abstract",
            "authors": [], "metadata_source": "PubMed", "metadata_error": "",
        }
        screen_pair.return_value = {
            "result": "匹配", "reason": "摘要支持。", "api_called": True,
        }
        xml = b'''<article><body><p>Before. Claim <xref ref-type="bibr" rid="B1">[1]</xref>. After.</p></body><back><ref-list><ref id="B1"><label>1</label><article-title>Article title</article-title></ref></ref-list></back></article>'''
        prepared = prepare_screening(xml, "paper.xml")
        screen_pair.assert_not_called()
        self.assertEqual(prepared["estimated_calls"], 1)
        result = execute_screening(prepared)
        self.assertEqual(result["actual_calls"], 1)
        self.assertEqual(screen_pair.call_args.args[2], "Before.")
        self.assertEqual(screen_pair.call_args.args[3], "After.")


if __name__ == "__main__":
    unittest.main()
