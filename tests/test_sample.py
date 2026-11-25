"""The sample maker: only CC BY articles, a Word file with live citations, a note on the source."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sample  # noqa: E402

JATS = """<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
<article-id pub-id-type="doi">10.5555/sample.1</article-id>
<title-group><article-title>A made-up sample article</article-title></title-group>
<contrib-group><contrib contrib-type="author"><name><surname>Doe</surname><given-names>Jane</given-names></name></contrib></contrib-group>
<permissions><license{lic}><license-p>Licence text.</license-p></license></permissions>
<abstract><p>We study a thing.</p></abstract></article-meta></front>
<body><sec><title>Introduction</title><p>Things matter <xref ref-type="bibr" rid="r1">1</xref>.</p></sec></body>
<back><ref-list><ref id="r1"><element-citation publication-type="journal"><person-group person-group-type="author">
<name><surname>Roe</surname><given-names>R</given-names></name></person-group><article-title>Earlier work</article-title>
<source>J Tests</source><year>2020</year><volume>1</volume><fpage>1</fpage><pub-id pub-id-type="doi">10.5555/roe</pub-id>
</element-citation></ref></ref-list></back></article>"""


class _Resp:
    def __init__(self, body: str, code: int = 200):
        self.content, self.status_code = body.encode(), code


def test_cc_by_sample_is_built(tmp_path):
    body = JATS.format(lic=' xlink:href="https://creativecommons.org/licenses/by/4.0/"')
    r = sample.make("PPR0", tmp_path / "s", "zotero", fetch=lambda u: _Resp(body))
    assert Path(r["docx"]).exists() and r["references"] == 1
    about = (tmp_path / "s" / "ABOUT_THIS_SAMPLE.txt").read_text(encoding="utf-8")
    assert "creativecommons.org/licenses/by/4.0" in about and "10.5555/sample.1" in about


def test_non_cc_by_is_refused(tmp_path):
    body = JATS.format(lic=' xlink:href="https://creativecommons.org/licenses/by-nc-nd/4.0/"')
    with pytest.raises(SystemExit):
        sample.make("PPR0", tmp_path / "s", fetch=lambda u: _Resp(body))
    assert not (tmp_path / "s" / "sample_manuscript.docx").exists()


def test_bad_download_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        sample.make("PPR0", tmp_path / "s", fetch=lambda u: _Resp("Not found", 404))


def test_converter_keeps_only_the_main_abstract():
    from lxml import etree
    import jats_to_docx as jd
    root = etree.fromstring(
        b"<article><front><article-meta>"
        b"<abstract abstract-type='graphical'><p>Picture words.</p></abstract>"
        b"<abstract><p>The main abstract.</p></abstract>"
        b"<abstract abstract-type='highlights'><p>Point one.</p></abstract>"
        b"</article-meta></front></article>")
    assert jd.text(jd.main_abstract(root)) == "The main abstract."
