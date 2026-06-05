"""XML / WordPress-WXR parser: per-<item> page extraction + generic fallback."""
from __future__ import annotations

from pathlib import Path

import pytest

from parsers.markup_xml import EmptyParse, parse

WXR = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
  <title>Practice of Life</title>
  <wp:wxr_version>1.2</wp:wxr_version>
  <item>
    <title>Swipe Less, Live More</title>
    <link>/young-pro-swipe-less-live-more</link>
    <wp:post_type>page</wp:post_type>
    <wp:status>publish</wp:status>
    <content:encoded><![CDATA[<h1>Swipe Less</h1><p>Get your <strong>time</strong> back.</p><script>evil()</script>]]></content:encoded>
  </item>
  <item>
    <title>Empty Draft</title>
    <link>/empty</link>
    <wp:post_type>page</wp:post_type>
    <wp:status>draft</wp:status>
    <content:encoded><![CDATA[]]></content:encoded>
  </item>
</channel>
</rss>
"""

GENERIC_XML = """<?xml version="1.0"?>
<catalog>
  <book><title>Dune</title><author>Herbert</author></book>
  <book><title>Solaris</title><author>Lem</author></book>
</catalog>
"""


def test_wxr_extracts_one_doc_per_item(tmp_path: Path) -> None:
    p = tmp_path / "export.xml"
    p.write_text(WXR, encoding="utf-8")

    res = parse(p)

    assert res.kind == "xml"
    assert res.parser == "wxr"
    # The empty-body draft is dropped; only the real page survives.
    assert len(res.chunks) == 1
    chunk = res.chunks[0]
    assert chunk.role == "page"
    assert chunk.meta["item_title"] == "Swipe Less, Live More"
    assert chunk.meta["item_link"] == "/young-pro-swipe-less-live-more"
    assert chunk.meta["post_type"] == "page"
    assert chunk.meta["status"] == "publish"
    # Body is de-HTML'd: text present, tags and <script> stripped.
    assert "Get your" in chunk.text and "time" in chunk.text
    assert "<p>" not in chunk.text and "<strong>" not in chunk.text
    assert "evil()" not in chunk.text


def test_generic_xml_falls_back_to_text(tmp_path: Path) -> None:
    p = tmp_path / "catalog.xml"
    p.write_text(GENERIC_XML, encoding="utf-8")

    res = parse(p)

    assert res.kind == "xml"
    assert res.parser == "xml-text"
    joined = "\n".join(c.text for c in res.chunks)
    assert "Dune" in joined and "Herbert" in joined and "Solaris" in joined


def test_empty_xml_raises_emptyparse(tmp_path: Path) -> None:
    p = tmp_path / "empty.xml"
    p.write_text('<?xml version="1.0"?><root></root>', encoding="utf-8")
    with pytest.raises((EmptyParse, ValueError)):
        parse(p)
