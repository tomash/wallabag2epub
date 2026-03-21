"""Optimize embedded PNG/JPEG images inside an EPUB (resize, re-encode as JPEG)."""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

try:
    from ebooklib import ITEM_IMAGE, epub
except ImportError:
    ITEM_IMAGE = None
    epub = None

try:
    from PIL import Image, ImageFile

    PIL_AVAILABLE = True
except ImportError:
    Image = None
    ImageFile = None
    PIL_AVAILABLE = False

EBOOKLIB_AVAILABLE = epub is not None

# Medium–high JPEG quality; max dimension for the longer side (aspect preserved).
JPEG_QUALITY = 85
MAX_DIMENSION = 1080

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})


def _posix_relpath(target: str, start: str) -> str:
    """POSIX relative path from directory `start` to file `target`."""
    target = target.replace("\\", "/")
    start = (start.replace("\\", "/").rstrip("/") or ".") if start else "."
    return os.path.relpath(target, start).replace("\\", "/")


def _jpeg_path_for(file_name: str) -> str:
    return str(Path(file_name).with_suffix(".jpg"))


def _normalize_epub_path(p: str) -> str:
    return p.replace("\\", "/")


def _image_bytes_to_jpeg(data: bytes, *, max_dim: int, quality: int) -> bytes:
    """Resize (preserve aspect, fit inside max_dim square) and encode as JPEG."""
    # Many EPUBs contain slightly truncated JPEG/PNG streams; Pillow can still decode them.
    if ImageFile is not None:
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    with Image.open(io.BytesIO(data)) as im:
        if im.mode == "P":
            im = im.convert("RGBA")
        if im.mode == "RGBA":
            background = Image.new("RGB", im.size, (255, 255, 255))
            background.paste(im, mask=im.split()[3])
            im = background
        elif im.mode == "CMYK":
            im = im.convert("RGB")
        elif im.mode != "RGB":
            im = im.convert("RGB")

        im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()


def _try_image_bytes_to_jpeg(
    data: bytes, *, max_dim: int, quality: int
) -> bytes | None:
    """Like `_image_bytes_to_jpeg`, or None if the payload is not decodable."""
    try:
        return _image_bytes_to_jpeg(data, max_dim=max_dim, quality=quality)
    except (OSError, ValueError, TypeError):
        # OSError includes Pillow's UnidentifiedImageError / truncated-stream errors.
        return None


def _sanitize_toc_link_uids(book: "epub.EpubBook") -> None:
    """
    ebooklib's NCX writer uses Link.uid as XML id; merged books may leave it None.
    """

    def visit(obj: object, counter: list[int]) -> None:
        if obj is None:
            return
        if isinstance(obj, epub.Link):
            if not getattr(obj, "uid", None):
                obj.uid = "toc-link-%d" % counter[0]
                counter[0] += 1
            return
        if isinstance(obj, (list, tuple)):
            for x in obj:
                visit(x, counter)

    visit(getattr(book, "toc", None), [0])


def _should_optimize_epub_image(item: "epub.EpubItem") -> bool:
    if not item.file_name:
        return False
    ext = Path(item.file_name).suffix.lower()
    if ext not in _IMAGE_EXTENSIONS:
        return False
    if isinstance(item, epub.EpubCover):
        return True
    if item.get_type() == ITEM_IMAGE:
        return True
    return False


def _patch_epub_cover_html_image_names(book: "epub.EpubBook", path_map: dict[str, str]) -> None:
    """Update EpubCoverHtml.image_name when the cover image path changes."""
    for item in book.get_items():
        if isinstance(item, epub.EpubCoverHtml):
            name = _normalize_epub_path(item.image_name)
            if name in path_map:
                item.image_name = path_map[name]


def _replace_image_refs_in_text_items(
    book: "epub.EpubBook",
    path_pairs: list[tuple[str, str]],
) -> None:
    """
    For each non-binary item, replace relative references from old image paths to new.

    path_pairs: (old_abs_path, new_abs_path) inside the EPUB archive (forward slashes).
    """
    # Longest paths first to avoid partial substring collisions.
    sorted_pairs = sorted(path_pairs, key=lambda x: len(x[0]), reverse=True)

    for item in book.get_items():
        if isinstance(item, epub.EpubCover):
            continue
        if item.get_type() == ITEM_IMAGE:
            continue

        raw = item.get_content()
        if not raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

        doc_path = item.file_name or ""
        doc_dir = str(Path(doc_path).parent)
        doc_dir = _normalize_epub_path(doc_dir) if doc_dir else ""

        changed = text
        for old_abs, new_abs in sorted_pairs:
            if old_abs == new_abs:
                continue
            o = _normalize_epub_path(old_abs)
            n = _normalize_epub_path(new_abs)
            rel_old = _posix_relpath(o, doc_dir)
            rel_new = _posix_relpath(n, doc_dir)
            if rel_old != rel_new:
                changed = changed.replace(rel_old, rel_new)
            # Cover pages and some exports use full manifest-style paths in src/href (not relative).
            if o != n:
                changed = changed.replace(o, n)

        if changed != text:
            item.set_content(changed.encode("utf-8"))


class EpubOptimizer:
    """Resize and re-encode EPUB images as JPEG; update internal references."""

    def __init__(self, *, jpeg_quality: int = JPEG_QUALITY, max_dimension: int = MAX_DIMENSION):
        self.jpeg_quality = jpeg_quality
        self.max_dimension = max_dimension

    @classmethod
    def is_available(cls) -> bool:
        return EBOOKLIB_AVAILABLE and PIL_AVAILABLE

    def optimize(self, epub_path: str | Path) -> Path:
        """
        Read an EPUB, optimize embedded PNG/JPEG images, write ``name_optimized.epub``.

        Returns:
            Path to the written file.
        """
        if not EBOOKLIB_AVAILABLE:
            raise ImportError(
                "ebooklib is required. Install with: pip install ebooklib"
            )
        if not PIL_AVAILABLE:
            raise ImportError(
                "Pillow is required. Install with: pip install Pillow"
            )

        src = Path(epub_path).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Not a file: {src}")

        out = src.parent / f"{src.stem}_optimized.epub"

        # Default reader options; avoid ignore_ncx here — it can break spine/NCX loading
        # on minimal ebooklib-written EPUBs (get_item_with_id for toc returns None).
        book = epub.read_epub(str(src))
        # Reader may set book.toc to a bare Link; write_epub's NCX builder expects a list of sections.
        _toc = getattr(book, "toc", None)
        if _toc is not None and not isinstance(_toc, (list, tuple)):
            book.toc = [_toc]
        _sanitize_toc_link_uids(book)

        planned: list[tuple[object, str, str, bytes]] = []
        path_map: dict[str, str] = {}

        for item in book.get_items():
            if not _should_optimize_epub_image(item):
                continue
            old_path = _normalize_epub_path(item.file_name)
            data = item.get_content()
            if not data:
                continue
            jpeg_bytes = _try_image_bytes_to_jpeg(
                data,
                max_dim=self.max_dimension,
                quality=self.jpeg_quality,
            )
            if jpeg_bytes is None:
                print(
                    f"Skipping unreadable image (left unchanged): {old_path}",
                    file=sys.stderr,
                )
                continue
            new_path = _jpeg_path_for(old_path)
            planned.append((item, old_path, new_path, jpeg_bytes))
            if old_path != new_path:
                path_map[old_path] = new_path

        path_pairs = [(o, n) for _, o, n, _ in planned if o != n]

        _patch_epub_cover_html_image_names(book, path_map)
        _replace_image_refs_in_text_items(book, path_pairs)

        for item, old_path, new_path, jpeg_bytes in planned:
            item.file_name = new_path
            item.media_type = "image/jpeg"
            item.set_content(jpeg_bytes)

        epub.write_epub(str(out), book, {})

        return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Optimize PNG/JPEG images in an EPUB (max %dx%d JPEG, quality %d) "
            "and write <name>_optimized.epub."
            % (MAX_DIMENSION, MAX_DIMENSION, JPEG_QUALITY)
        )
    )
    parser.add_argument(
        "epub",
        type=Path,
        help="Path to the input .epub file",
    )
    args = parser.parse_args(argv)

    try:
        out = EpubOptimizer().optimize(args.epub)
    except (ImportError, FileNotFoundError) as e:
        print(str(e), file=sys.stderr)
        return 1

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
