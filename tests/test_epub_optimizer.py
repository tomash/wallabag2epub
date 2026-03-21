"""Tests for EpubOptimizer."""

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from epub_optimizer import (
    EBOOKLIB_AVAILABLE,
    EpubOptimizer,
    PIL_AVAILABLE,
    _sanitize_toc_link_uids,
    main,
)


class TestEpubOptimizerInit:
    def test_is_available_matches_dependencies(self):
        assert EpubOptimizer.is_available() is (EBOOKLIB_AVAILABLE and PIL_AVAILABLE)


@pytest.mark.skipif(not EBOOKLIB_AVAILABLE, reason="ebooklib not installed")
def test_sanitize_toc_assigns_uids_to_links_with_none_uid():
    from ebooklib import epub

    book = epub.EpubBook()
    ln = epub.Link(href="ch.xhtml", title="Chapter", uid=None)
    book.toc = [ln]
    _sanitize_toc_link_uids(book)
    assert ln.uid == "toc-link-0"


@pytest.mark.skipif(not EBOOKLIB_AVAILABLE, reason="ebooklib not installed")
@pytest.mark.skipif(not PIL_AVAILABLE, reason="Pillow not installed")
class TestEpubOptimizerOptimize:
    def _png_bytes(self, size: tuple[int, int] = (32, 24)) -> bytes:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", size, color=(200, 10, 50)).save(buf, format="PNG")
        return buf.getvalue()

    def _jpeg_bytes(self, size: tuple[int, int]) -> bytes:
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", size, color=(10, 200, 50)).save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    def test_writes_optimized_epub_and_converts_png_to_jpeg(self, tmp_path):
        from ebooklib import epub

        src = tmp_path / "sample.epub"
        png_data = self._png_bytes()

        book = epub.EpubBook()
        book.set_identifier("opt-1")
        book.set_title("Opt test")
        book.set_language("en")
        # ebooklib writes spine toc="ncx" but omits NCX from the manifest unless added.
        book.add_item(epub.EpubNcx())

        img = epub.EpubImage()
        img.file_name = "OEBPS/images/figure.png"
        img.set_content(png_data)
        book.add_item(img)

        ch = epub.EpubHtml(
            title="Ch",
            file_name="OEBPS/chapter.xhtml",
            lang="en",
        )
        ch.set_content(
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b'<p><img src="images/figure.png" alt="x"/></p>'
            b"</body></html>"
        )
        book.add_item(ch)
        book.spine = [ch]
        epub.write_epub(str(src), book, {})

        out = EpubOptimizer().optimize(src)

        assert out == tmp_path / "sample_optimized.epub"
        assert out.is_file()

        with zipfile.ZipFile(out, "r") as zf:
            names = zf.namelist()
        assert any(n.endswith("images/figure.jpg") for n in names)
        assert not any(n.endswith("images/figure.png") for n in names)

        loaded = epub.read_epub(str(out))
        html_items = [
            i
            for i in loaded.get_items()
            if getattr(i, "file_name", "") == "OEBPS/chapter.xhtml"
        ]
        assert len(html_items) == 1
        body = (html_items[0].get_content() or b"").decode("utf-8")
        assert "images/figure.jpg" in body
        assert "figure.png" not in body

    def test_leaves_unreadable_image_unchanged(self, tmp_path, capsys):
        from ebooklib import epub

        src = tmp_path / "badimg.epub"
        bad_payload = b"\xff\xd8\xff not valid jpeg"
        book = epub.EpubBook()
        book.set_identifier("bad")
        book.set_title("Bad img")
        book.set_language("en")
        book.add_item(epub.EpubNcx())

        bad = epub.EpubImage()
        bad.file_name = "OEBPS/images/broken.jpg"
        bad.set_content(bad_payload)
        book.add_item(bad)

        ok = epub.EpubImage()
        ok.file_name = "OEBPS/images/fine.png"
        ok.set_content(self._png_bytes())
        book.add_item(ok)

        ch = epub.EpubHtml(
            title="Ch",
            file_name="OEBPS/chapter.xhtml",
            lang="en",
        )
        ch.set_content(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            b'<img src="images/broken.jpg"/><img src="images/fine.png"/>'
            b"</body></html>"
        )
        book.add_item(ch)
        book.spine = [ch]
        epub.write_epub(str(src), book, {})

        EpubOptimizer().optimize(src)
        err = capsys.readouterr().err
        assert "Skipping unreadable" in err
        assert "broken.jpg" in err

        out = tmp_path / "badimg_optimized.epub"
        with zipfile.ZipFile(out, "r") as zf:
            broken = zf.read(
                next(n for n in zf.namelist() if n.endswith("images/broken.jpg"))
            )
            assert broken == bad_payload
            assert any(n.endswith("images/fine.jpg") for n in zf.namelist())

    def test_resizes_large_jpeg(self, tmp_path):
        from ebooklib import epub
        from PIL import Image

        src = tmp_path / "big.epub"
        jpeg_data = self._jpeg_bytes((2000, 1500))

        book = epub.EpubBook()
        book.set_identifier("opt-2")
        book.set_title("Big")
        book.set_language("en")
        book.add_item(epub.EpubNcx())
        img = epub.EpubImage()
        img.file_name = "OEBPS/images/huge.jpg"
        img.set_content(jpeg_data)
        book.add_item(img)
        ch = epub.EpubHtml(
            title="C",
            file_name="OEBPS/c.xhtml",
            lang="en",
        )
        ch.set_content(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            b'<img src="images/huge.jpg"/></body></html>'
        )
        book.add_item(ch)
        book.spine = [ch]
        epub.write_epub(str(src), book, {})

        out = EpubOptimizer(max_dimension=1080).optimize(src)

        loaded = epub.read_epub(str(out))
        for item in loaded.get_items():
            if item.file_name == "OEBPS/images/huge.jpg":
                with Image.open(io.BytesIO(item.get_content())) as im:
                    w, h = im.size
                assert max(w, h) <= 1080
                return
        pytest.fail("optimized image not found")

    def test_cover_png_updates_cover_html_reference(self, tmp_path):
        from ebooklib import epub

        src = tmp_path / "covered.epub"
        png_data = self._png_bytes((64, 64))

        book = epub.EpubBook()
        book.set_identifier("opt-3")
        book.set_title("Cover test")
        book.set_language("en")
        book.add_item(epub.EpubNcx())
        book.set_cover("OEBPS/images/cover.png", png_data)

        ch = epub.EpubHtml(
            title="Body",
            file_name="OEBPS/body.xhtml",
            lang="en",
        )
        ch.set_content(
            b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>Hi</p></body></html>"
        )
        book.add_item(ch)
        book.spine = [ch]
        epub.write_epub(str(src), book, {})

        out = EpubOptimizer().optimize(src)

        with zipfile.ZipFile(out, "r") as zf:
            assert any(n.endswith("cover.jpg") for n in zf.namelist())
            assert not any(n.endswith("cover.png") for n in zf.namelist())
            cover_xhtml = next(
                n for n in zf.namelist() if n.endswith("cover.xhtml")
            )
            cover_html = zf.read(cover_xhtml).decode("utf-8")
        assert "cover.jpg" in cover_html
        assert "cover.png" not in cover_html

        loaded = epub.read_epub(str(out))
        cover_items = [
            i
            for i in loaded.get_items()
            if getattr(i, "file_name", "").endswith("cover.png")
            or getattr(i, "file_name", "").endswith("cover.jpg")
        ]
        assert any(
            getattr(i, "file_name", "").endswith("cover.jpg") for i in cover_items
        )


@pytest.mark.skipif(not EBOOKLIB_AVAILABLE, reason="ebooklib not installed")
class TestEpubOptimizerErrors:
    def test_raises_file_not_found(self):
        p = Path("/nonexistent/nope.epub")
        with pytest.raises(FileNotFoundError):
            EpubOptimizer().optimize(p)

    def test_raises_without_ebooklib(self):
        with patch("epub_optimizer.EBOOKLIB_AVAILABLE", False):
            with pytest.raises(ImportError, match="ebooklib"):
                EpubOptimizer().optimize("/tmp/x.epub")

    def test_raises_without_pillow(self):
        with patch("epub_optimizer.PIL_AVAILABLE", False):
            with pytest.raises(ImportError, match="Pillow"):
                EpubOptimizer().optimize("/tmp/x.epub")


def test_main_prints_path(tmp_path, capsys):
    pytest.importorskip("ebooklib")
    pytest.importorskip("PIL")

    from ebooklib import epub

    src = tmp_path / "cli.epub"
    book = epub.EpubBook()
    book.set_identifier("cli")
    book.set_title("CLI")
    book.set_language("en")
    book.add_item(epub.EpubNcx())
    ch = epub.EpubHtml(title="X", file_name="OEBPS/x.xhtml", lang="en")
    ch.set_content(b"<html xmlns='http://www.w3.org/1999/xhtml'><body/></html>")
    book.add_item(ch)
    book.spine = [ch]
    epub.write_epub(str(src), book, {})

    rc = main([str(src)])
    assert rc == 0
    captured = capsys.readouterr().out.strip()
    assert captured.endswith("_optimized.epub")
    assert Path(captured).exists()


def test_main_missing_file_returns_error(capsys):
    pytest.importorskip("ebooklib")
    pytest.importorskip("PIL")

    rc = main(["/nonexistent/missing.epub"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Not a file" in err or "missing" in err.lower()
