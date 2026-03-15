"""Unit tests for EpubMerger class."""

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from epub_merger import (
    EBOOKLIB_AVAILABLE,
    EpubMerger,
    _read_epub_robust,
)


class TestEpubMergerInit:
    """Tests for EpubMerger availability."""

    def test_is_available_matches_ebooklib(self):
        assert EpubMerger.is_available() is EBOOKLIB_AVAILABLE


class TestEpubMergerMerge:
    """Tests for merge method."""

    @pytest.mark.skipif(not EBOOKLIB_AVAILABLE, reason="ebooklib not installed")
    def test_merge_creates_output_file(self, tmp_path):
        # Create minimal valid EPUB files using ebooklib
        from ebooklib import epub

        epub1 = tmp_path / "a.epub"
        epub2 = tmp_path / "b.epub"
        out = tmp_path / "merged.epub"

        book1 = epub.EpubBook()
        book1.set_identifier("id1")
        book1.set_title("Article 1")
        book1.set_language("en")
        c1 = epub.EpubHtml(title="Ch1", file_name="ch1.xhtml", lang="en")
        c1.set_content(b"<html><body>Content 1</body></html>")
        book1.add_item(c1)
        book1.spine = ["nav", c1]
        epub.write_epub(str(epub1), book1)

        book2 = epub.EpubBook()
        book2.set_identifier("id2")
        book2.set_title("Article 2")
        book2.set_language("en")
        c2 = epub.EpubHtml(title="Ch2", file_name="ch2.xhtml", lang="en")
        c2.set_content(b"<html><body>Content 2</body></html>")
        book2.add_item(c2)
        book2.spine = ["nav", c2]
        epub.write_epub(str(epub2), book2)

        merger = EpubMerger()
        result = merger.merge(
            [str(epub1), str(epub2)], str(out), title="Merged"
        )

        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_merge_raises_without_ebooklib(self):
        with patch("epub_merger.EBOOKLIB_AVAILABLE", False):
            with pytest.raises(ImportError, match="ebooklib is required"):
                EpubMerger().merge([], "/tmp/out.epub")

    @pytest.mark.skipif(not EBOOKLIB_AVAILABLE, reason="ebooklib not installed")
    def test_merge_epub_with_missing_manifest_entry(self, tmp_path):
        """EPUB that references a manifest item not present in the zip is still merged."""
        from ebooklib import epub

        # Create a valid minimal EPUB
        valid_epub = tmp_path / "valid.epub"
        book = epub.EpubBook()
        book.set_identifier("id1")
        book.set_title("Article With Broken Manifest")
        book.set_language("en")
        chapter = epub.EpubHtml(
            title="Chapter One",
            file_name="chapter.xhtml",
            lang="en",
        )
        chapter.set_content(b"<html><body><p>Content here.</p></body></html>")
        book.add_item(chapter)
        book.spine = ["nav", chapter]
        epub.write_epub(str(valid_epub), book)

        # Inject a manifest entry for a file that does not exist in the zip
        # (simulates e.g. wallabag export with phantom "media" reference)
        broken_epub = tmp_path / "broken_manifest.epub"
        with zipfile.ZipFile(valid_epub, "r") as zin:
            names = zin.namelist()
            opf_name = next(n for n in names if n.endswith(".opf"))
            opf_bytes = zin.read(opf_name).decode("utf-8")
        # Add <item id="phantom" href="media" ... /> so path OEBPS/media is missing
        bad_item = '  <item id="phantom" href="media" media-type="application/octet-stream" />\n  '
        if "</manifest>" in opf_bytes:
            opf_bytes = opf_bytes.replace("</manifest>", bad_item + "</manifest>")
        else:
            opf_bytes = opf_bytes.replace("</opf:manifest>", bad_item + "</opf:manifest>")

        with zipfile.ZipFile(broken_epub, "w", zipfile.ZIP_DEFLATED) as zout:
            with zipfile.ZipFile(valid_epub, "r") as zin:
                for name in zin.namelist():
                    if name == opf_name:
                        zout.writestr(name, opf_bytes.encode("utf-8"))
                    else:
                        zout.writestr(name, zin.read(name))

        # Robust read should succeed and return the book with the real chapter
        loaded = _read_epub_robust(str(broken_epub), options={"ignore_ncx": True})
        items = list(loaded.get_items())
        html_items = [i for i in items if isinstance(i, epub.EpubHtml)]
        assert len(html_items) >= 1
        assert any("Content here" in (i.get_content() or b"").decode("utf-8", errors="replace") for i in html_items)

        # Merge should include this EPUB in the output
        out_epub = tmp_path / "merged.epub"
        merger = EpubMerger()
        result = merger.merge(
            [str(broken_epub)],
            str(out_epub),
            title="Merged",
        )
        assert Path(result).exists()
        merged_book = epub.read_epub(result, options={"ignore_ncx": True})
        merged_items = list(merged_book.get_items())
        merged_html = [i for i in merged_items if isinstance(i, epub.EpubHtml)]
        assert len(merged_html) >= 1
        content = b"".join(
            i.get_content() or b""
            for i in merged_html
        ).decode("utf-8", errors="replace")
        assert "Content here" in content
