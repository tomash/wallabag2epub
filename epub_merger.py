"""EPUB merging: combine multiple EPUB files into one with a table of contents."""

import argparse
import hashlib
import http.client
import os
import re
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import html as html_std

try:
    from ebooklib import epub
    EBOOKLIB_AVAILABLE = True
except ImportError:
    EBOOKLIB_AVAILABLE = False
    epub = None

# OPF namespace used in content.opf
OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# Base filenames that are not the main article content (covers, TOC pages)
_AUXILIARY_BASES = frozenset({"CoverPage.xhtml", "Cover2.html", "epub3toc.xhtml"})


def _is_auxiliary_doc(file_name: str) -> bool:
    """Return True if this document is a cover/TOC page, not main article content."""
    base = file_name.split("/")[-1] if "/" in file_name else file_name
    if base in _AUXILIARY_BASES:
        return True
    if base.endswith("_cover.html"):
        return True
    return False


_XML10_FORBIDDEN_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_BARE_AMP_RE = re.compile(r"&(?!#\d+;|#x[0-9A-Fa-f]+;|[A-Za-z][A-Za-z0-9]+;)")
_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg)(?:$|[?#])", re.I)


def _looks_like_missing_archive_image_name(name: str) -> bool:
    n = name or ""
    return bool(_IMAGE_EXT_RE.search(n)) and "images/" in n.lower()


def _extract_missing_archive_name(err_msg: str) -> str | None:
    """
    Try to extract the missing archive member name from an ebooklib error.

    Observed formats:
      - "There is no item named 'OEBPS/images/https:/...jpg' in the archive"
      - "OEBPS/images/image?url=/images/...png"
    """
    # ebooklib sometimes wraps the filename in a KeyError string using
    # backslash-escaped quotes (e.g. "There is no item named \\'...\\' in the archive").
    # Normalize those first so the regexes below can match reliably.
    err_msg = (err_msg or "").replace("\\'", "'").replace('\\"', '"')

    # Most specific form first.
    m = re.search(r"There is no item named '([^']+)' in the archive", err_msg)
    if m:
        return m.group(1)

    # Fallback: try to locate any images/...<ext> substring.
    # Keep it conservative to avoid grabbing unrelated content.
    m = re.search(
        r"((?:OEBPS|OPS)?/?images/[^'\"\s]+?\.(?:png|jpe?g|gif|webp|svg)(?:\?[^'\"\s]*)?)",
        err_msg,
        flags=re.I,
    )
    if m:
        candidate = m.group(1)
        return candidate
    return None


def _sanitize_xhtml_text(text: str) -> str:
    """
    Best-effort sanitizer for XHTML/XML-ish content.

    Common breakages seen in exported epubs:
    - Control characters forbidden by XML 1.0
    - Bare '&' in URLs or text
    - HTML entities like &nbsp; used in XHTML without a DTD
    """
    text = _XML10_FORBIDDEN_RE.sub("", text)
    # Keep a few common HTML entities usable in XML by converting them.
    text = text.replace("&nbsp;", "&#160;")
    # Escape bare ampersands (while keeping well-formed entities intact).
    text = _BARE_AMP_RE.sub("&amp;", text)

    # Last-resort: run through a tolerant HTML parser and serialize back to
    # XML-ish markup so it becomes well-formed for ElementTree.
    # This helps with "not well-formed (invalid token)" coming from
    # malformed escaping or broken tag structure.
    try:
        from lxml import html  # type: ignore

        doc = html.fromstring(text)
        # method="xml" helps produce well-formed output.
        serialized = html.tostring(doc, method="xml", encoding="unicode")
        return serialized
    except Exception:
        return text


def _download_url_bytes(url: str, timeout_s: int = 20) -> bytes:
    # Progress output: show which external URL we are downloading.
    # Keep this in stdout so users can see progress while merge runs.
    print(f"Downloading image: {url}", flush=True)
    req = Request(
        url,
        headers={
            "User-Agent": "wallabag2epub/epub-merger (urllib)",
            "Accept": "*/*",
        },
    )
    with urlopen(req, timeout=timeout_s) as resp:  # nosec - user-controlled URLs
        return resp.read()


def _guess_image_url_from_href(href: str) -> str | None:
    """
    Attempt to recover an absolute URL from a broken/encoded href.

    Examples observed:
    - 'https:/example.com/a.jpg' (missing one slash)
    - 'image?url=https:/example.com/a.jpg'
    - 'OEBPS/images/https:/example.com/a.jpg' (href inside OPF)
    """
    if not href:
        return None

    # Direct absolute URL.
    if href.startswith(("http://", "https://")):
        return href

    # Query param wrapper like image?url=...
    if "url=" in href:
        try:
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            raw = (qs.get("url") or [None])[0]
            if raw:
                raw = unquote(raw)
                href = raw
        except Exception:
            pass

    # Extract a url-looking substring.
    m = re.search(r"https?:/[^/]", href)
    if m:
        href = href[m.start() :]

    # Fix missing slash in scheme (https:/ -> https://)
    href = re.sub(r"^(https?):/([^/])", r"\1://\2", href)
    if href.startswith("//"):
        href = "https:" + href

    parsed = urlparse(href)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return href
    return None


def _looks_like_image_url(url: str) -> bool:
    p = urlparse(url)
    path = (p.path or "").lower()
    return any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"))


_XHTML_IMG_ATTR_RE = re.compile(
    r"""(?ix)
    \b(?:src|href)\s*=\s*(?P<q>["'])(?P<url>[^"']+)(?P=q)
    """
)
_XHTML_CSS_URL_RE = re.compile(
    r"""(?ix)
    url\(\s*(?P<q>["']?)(?P<url>[^"')\s]+)(?P=q)\s*\)
    """
)


_REMOTE_IMAGE_URL_RE = re.compile(
    r"""(?ix)
    \b
    (?:
        https?://
      | https?:/
      | //
    )
    [^\s"'<>(),]+?
    \.(?:png|jpe?g|gif|webp|svg)
    (?:\?[^"'<>(),]*)?
    (?:\#[^"'<>(),]*)?
    """,
)


def _iter_remote_image_urls_from_text(text: str) -> list[str]:
    """
    Extract remote image URLs from arbitrary XHTML/HTML text.

    Supports:
    - https?://... (normal absolute URLs)
    - https:/...  (broken scheme, one slash)
    - //...       (protocol-relative)
    """
    if not text:
        return []
    return [m.group(0) for m in _REMOTE_IMAGE_URL_RE.finditer(text)]


def _iter_image_urls_in_xhtml(xhtml: str) -> list[str]:
    """
    Extract URL-ish values from typical image references in XHTML:
    - src/href attributes
    - CSS url(...) fragments
    """
    urls: list[str] = []
    for m in _XHTML_IMG_ATTR_RE.finditer(xhtml):
        urls.append(m.group("url"))
    for m in _XHTML_CSS_URL_RE.finditer(xhtml):
        urls.append(m.group("url"))
    return urls


def _normalize_remote_image_url(url_raw: str) -> str | None:
    """
    Normalize a markup URL value into a downloadable absolute URL.
    Returns None when the URL is not a remote http(s) image.
    """
    if not url_raw:
        return None

    url_unescaped = html_std.unescape(url_raw.strip())
    # srcset-like descriptor values can get captured as part of the URL
    # (e.g. "...ssl=1 300w"). urlopen/http.client rejects whitespace/control
    # characters, so strip anything after the first whitespace.
    url_unescaped = re.split(r"\s+", url_unescaped, maxsplit=1)[0]

    if url_unescaped.startswith(("data:", "blob:", "cid:")):
        return None

    if url_unescaped.startswith("//"):
        url_unescaped = "https:" + url_unescaped

    url_norm = _guess_image_url_from_href(url_unescaped)
    if not url_norm or not _looks_like_image_url(url_norm):
        return None
    return url_norm


def _media_type_for_image_url(url: str) -> str:
    path = (urlparse(url).path or "").lower()
    if path.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".gif"):
        return "image/gif"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return "application/octet-stream"


def _main_content_id(book: "epub.EpubBook") -> str | None:
    """Return the item id of the first spine document that is main content (not auxiliary)."""
    id_to_item = {item.get_id(): item for item in book.get_items() if item.get_id()}

    def _item_for_entry(entry):
        idref = entry[0] if isinstance(entry, (list, tuple)) else entry
        if hasattr(idref, "get_id"):
            idref = idref.get_id()
        return id_to_item.get(idref) if isinstance(idref, str) else None

    for entry in book.spine:
        item = _item_for_entry(entry)
        if not item or not isinstance(item, epub.EpubHtml):
            continue
        if not _is_auxiliary_doc(item.get_name() or ""):
            return item.get_id()
    for entry in book.spine:
        item = _item_for_entry(entry)
        if item and isinstance(item, epub.EpubHtml):
            return item.get_id()
    return None


def _fix_epub_missing_manifest(epub_path: str) -> str:
    """
    Create a temporary EPUB with manifest entries removed for files that are
    missing from the archive. ebooklib fails when the manifest references
    items (e.g. 'OEBPS/media') that do not exist in the zip.
    If a missing manifest item looks like a downloadable remote image URL,
    download it and embed it into the temporary EPUB instead of removing it.
    Returns path to the temporary fixed EPUB file.
    """
    path = Path(epub_path).resolve()
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())

        # Find content.opf path from container
        container = zf.read("META-INF/container.xml").decode("utf-8")
        root = ET.fromstring(container)
        opf_path = root.find(
            ".//{%s}rootfile[@media-type='application/oebps-package+xml']"
            % CONTAINER_NS
        )
        if opf_path is None:
            opf_path = root.find(".//{%s}rootfile" % CONTAINER_NS)
        if opf_path is None:
            raise ValueError("No rootfile in container.xml")
        opf_name = opf_path.get("full-path", "content.opf")
        opf_dir = str(Path(opf_name).parent) if "/" in opf_name else ""

        opf_bytes = zf.read(opf_name)
        try:
            opf_root = ET.fromstring(opf_bytes)
        except ET.ParseError:
            # Some exported EPUBs have mildly broken OPF XML; if we can't parse
            # it safely, fall back to the original EPUB and let other
            # best-effort recovery paths handle missing items.
            return epub_path

        # Register default namespace so find/findall work
        ET.register_namespace("", OPF_NS)
        ns = {"opf": OPF_NS}
        manifest = opf_root.find("opf:manifest", ns)
        if manifest is None:
            return epub_path

        ids_to_remove: list[str | None] = []
        embedded_downloads: dict[str, bytes] = {}
        for item in manifest.findall("opf:item", ns):
            href = item.get("href")
            if not href:
                continue
            # Resolve path relative to OPF directory
            full = f"{opf_dir}/{href}" if opf_dir else href
            full = full.lstrip("/").replace("//", "/")
            if full not in names:
                # If this missing item is a remote image URL (or wrapper),
                # try to download and embed it under the expected internal path.
                url = _guess_image_url_from_href(href)
                if url and _looks_like_image_url(url):
                    try:
                        embedded_downloads[full] = _download_url_bytes(url)
                        continue
                    except Exception:
                        pass
                ids_to_remove.append(item.get("id"))

        if not ids_to_remove and not embedded_downloads:
            return epub_path

        # Remove missing items from manifest
        for item in list(manifest):
            if item.get("id") in ids_to_remove:
                manifest.remove(item)

        # Remove spine itemrefs that reference removed items
        spine = opf_root.find("opf:spine", ns)
        if spine is not None:
            for itemref in list(spine):
                if itemref.get("idref") in ids_to_remove:
                    spine.remove(itemref)
            # Clear toc so reader does not resolve NCX (avoids issues after manifest edits)
            if ids_to_remove:
                spine.attrib.pop("toc", None)

        # Re-serialize OPF (ElementTree uses ns0 for the default OPF namespace)
        new_opf = ET.tostring(
            opf_root,
            encoding="unicode",
            method="xml",
        )
        # Remove ns0 prefix so ebooklib sees <package> etc.; keep dc:, dcterms: as-is
        new_opf = new_opf.replace("ns0:", "")
        if 'xmlns="' not in new_opf.split(">")[0]:
            new_opf = new_opf.replace("<package ", "<package xmlns=%r " % OPF_NS, 1)

        fd, tmp_path = tempfile.mkstemp(suffix=".epub")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as out:
                for name in names:
                    if name == opf_name:
                        out.writestr(name, new_opf.encode("utf-8"))
                    else:
                        out.writestr(name, zf.read(name))
                for embedded_name, payload in embedded_downloads.items():
                    out.writestr(embedded_name, payload)
            return tmp_path
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise


def _sanitize_epub_xhtml(epub_path: str) -> str:
    """
    Create a temporary EPUB where all .xhtml/.html files are sanitized to be
    more XML-friendly (fixes 'not well-formed (invalid token)' failures).
    """
    path = Path(epub_path).resolve()
    fd, tmp_path = tempfile.mkstemp(suffix=".epub")
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "r") as zf, zipfile.ZipFile(
            tmp_path, "w", zipfile.ZIP_DEFLATED
        ) as out:
            for name in zf.namelist():
                data = zf.read(name)
                lower = name.lower()
                if lower.endswith((".xhtml", ".html", ".htm")):
                    try:
                        text = data.decode("utf-8", errors="replace")
                        text2 = _sanitize_xhtml_text(text)
                        data = text2.encode("utf-8")
                    except Exception:
                        # Keep original bytes if sanitization fails.
                        pass
                out.writestr(name, data)
        return tmp_path
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _embed_missing_archive_item(epub_path: str, missing_name: str) -> str:
    """
    Create a temporary EPUB where a specific missing archive item is added.

    This is a second-level fix for errors like:
        "There is no item named 'OEBPS/images/https:/...jpg' in the archive"
    which may come from broken src/href references that are not part of the OPF
    manifest. When the missing name looks like a downloadable image URL (or
    contains one), download and embed it under exactly that missing_name.
    """
    url = _guess_image_url_from_href(missing_name)
    if url and _looks_like_image_url(url):
        # If the URL returns 4xx (e.g. missing/blocked), embed a placeholder so
        # we can still finish merging.
        try:
            payload = _download_url_bytes(url)
        except HTTPError as http_err:
            if 400 <= int(getattr(http_err, "code", 0) or 0) < 500:
                payload = _placeholder_image_bytes(missing_name)
            else:
                raise
        except (http.client.InvalidURL, ValueError, Exception):
            # Don't fail the whole merge due to one broken URL.
            payload = _placeholder_image_bytes(missing_name)
    else:
        # We couldn't derive a downloadable URL (common for relative
        # "/images/...png" values embedded into ebook member names). If the
        # missing archive member looks like an image, insert a placeholder.
        if not _looks_like_missing_archive_image_name(missing_name):
            raise FileNotFoundError(missing_name)
        payload = _placeholder_image_bytes(missing_name)


    src_path = Path(epub_path).resolve()
    fd, tmp_path = tempfile.mkstemp(suffix=".epub")
    os.close(fd)
    try:
        with zipfile.ZipFile(src_path, "r") as zf_in, zipfile.ZipFile(
            tmp_path, "w", zipfile.ZIP_DEFLATED
        ) as zf_out:
            for name in zf_in.namelist():
                zf_out.writestr(name, zf_in.read(name))
            # Embed exactly under the name zipfile/ebooklib is asking for.
            zf_out.writestr(missing_name, payload)
        return tmp_path
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _placeholder_image_bytes(missing_name: str) -> bytes:
    """
    Return a small placeholder image (encoded to the best effort based on filename).
    Uses Pillow when available, otherwise falls back to a tiny PNG.
    """
    ext = (Path(missing_name).suffix or "").lower().lstrip(".")
    # Default: PNG is most universally safe in EPUB tooling.
    fmt = "PNG"

    if ext in ("jpg", "jpeg"):
        fmt = "JPEG"
    elif ext == "gif":
        fmt = "GIF"
    elif ext == "webp":
        fmt = "WEBP"

    try:
        from PIL import Image  # type: ignore

        # Tiny opaque placeholder; readers generally do not care about aesthetics.
        img = Image.new("RGB", (8, 8), color=(200, 200, 200))
        buf = BytesIO()
        save_kwargs = {}
        if fmt == "JPEG":
            save_kwargs["quality"] = 75
        img.save(buf, format=fmt, **save_kwargs)
        return buf.getvalue()
    except Exception:
        # 1x1 transparent-ish PNG (base64) fallback.
        import base64

        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/w8AAn8B9o5GxwAAAABJRU5ErkJggg=="
        )
        return base64.b64decode(png_b64)


def _read_epub_robust(path: str, options: dict | None = None) -> "epub.EpubBook":
    """
    Read an EPUB, fixing manifest entries for missing archive items if needed
    so that ebooklib does not raise (e.g. 'There is no item named ...').
    """
    if options is None:
        options = {"ignore_ncx": True}
    try:
        return epub.read_epub(path, options=options)
    except Exception as e:
        err_msg = str(e)
        # 1) Missing archive items.
        missing_name = _extract_missing_archive_name(err_msg)
        if missing_name:
            fixed_path = _fix_epub_missing_manifest(path)
            try:
                try:
                    return epub.read_epub(fixed_path, options=options)
                except Exception:
                    # Only attempt the 2nd-level "embed the missing item"
                    # for image-like missing paths. For other media references,
                    # just removing dangling manifest/spine entries is usually
                    # enough to let ebooklib proceed.
                    if _looks_like_missing_archive_image_name(missing_name):
                        embedded_path = _embed_missing_archive_item(
                            fixed_path, missing_name
                        )
                        try:
                            try:
                                return epub.read_epub(
                                    embedded_path, options=options
                                )
                            except Exception:
                                # If inserting an image placeholder didn't fix the read
                                # (e.g. other XHTML issues), sanitize XHTML and retry.
                                sanitized_path = _sanitize_epub_xhtml(
                                    embedded_path
                                )
                                try:
                                    return epub.read_epub(
                                        sanitized_path, options=options
                                    )
                                finally:
                                    if sanitized_path != embedded_path and Path(
                                        sanitized_path
                                    ).exists():
                                        Path(sanitized_path).unlink(
                                            missing_ok=True
                                        )
                        finally:
                            if embedded_path != path and Path(
                                embedded_path
                            ).exists():
                                Path(embedded_path).unlink(missing_ok=True)
                    # If it still fails, let the outer fallbacks handle it
                    # (e.g. XHTML sanitization below).
            finally:
                if fixed_path != path and Path(fixed_path).exists():
                    Path(fixed_path).unlink(missing_ok=True)

        # 2) Broken XHTML/XML content.
        if "not well-formed" in err_msg.lower() or "invalid token" in err_msg.lower():
            sanitized_path = _sanitize_epub_xhtml(path)
            try:
                return epub.read_epub(sanitized_path, options=options)
            finally:
                if sanitized_path != path and Path(sanitized_path).exists():
                    Path(sanitized_path).unlink(missing_ok=True)

        raise


class EpubMerger:
    """Merges multiple EPUB files into a single combined EPUB with table of contents."""

    @classmethod
    def is_available(cls) -> bool:
        """Return True if ebooklib is installed and merging is possible."""
        return EBOOKLIB_AVAILABLE

    def merge(
        self,
        epub_paths: list[str],
        output_path: str,
        title: str = "Merged Articles",
    ) -> str:
        """
        Merge multiple EPUB files into a single combined EPUB with table of contents.

        Args:
            epub_paths: List of paths to EPUB files (order preserved)
            output_path: Path for the merged output EPUB
            title: Title for the merged book

        Returns:
            Path to the created file.

        Raises:
            ImportError: If ebooklib is not available.
        """
        if not EBOOKLIB_AVAILABLE:
            raise ImportError(
                "ebooklib is required for merge. Install with: pip install ebooklib"
            )

        # Use a single ISO datetime for both title and output filename.
        # Keep the title as a true ISO 8601 string; make the filename variant filesystem-safe.
        created_iso = datetime.now().astimezone().replace(microsecond=0).isoformat()
        created_iso_for_filename = created_iso.replace(":", "-")

        # TOC/book title
        title = f"Wallabag Export from {created_iso}"

        # Output filename
        out_p = Path(output_path)
        output_path = str(
            out_p.with_name(f"{out_p.stem}_{created_iso_for_filename}{out_p.suffix}")
        )

        merged = epub.EpubBook()
        merged.set_identifier("merged-" + str(hash(tuple(epub_paths)))[:12])
        merged.set_title(title)
        merged.set_language("en")

        toc_entries = []
        spine_items = ["nav"]
        chapters_for_cover_remap = []  # EpubHtml items to fix CoverPage/toc links later
        downloaded_image_internal_files: dict[str, str] = {}  # url_norm -> file_name

        for idx, path in enumerate(epub_paths):
            print(f"Merging EPUB {idx + 1}/{len(epub_paths)}: {path}", flush=True)
            href_map = {}
            prefix = f"article_{idx:04d}_"
            try:
                book = _read_epub_robust(path, options={"ignore_ncx": True})
            except Exception as e:
                print(f"Warning: Could not read {path}: {e}")
                continue

            # Best-effort article title for TOC labels (wallabag often stores it on the book, not on the HTML item).
            book_level_title = (getattr(book, "title", None) or "").strip()
            main_content_id = _main_content_id(book)
            added_toc_for_article = False

            doc_items = []
            resource_items = []

            for item in book.get_items():
                if isinstance(item, epub.EpubHtml):
                    doc_items.append(item)
                else:
                    resource_items.append(item)

            for item in resource_items:
                if isinstance(item, (epub.EpubNcx, epub.EpubNav)):
                    continue
                if not isinstance(item, epub.EpubItem):
                    continue
                old_href = item.get_name()
                base = old_href.split("/")[-1] if "/" in old_href else old_href
                new_href = f"{prefix}res/{base}"
                new_id = prefix + (item.get_id() or base).replace(".", "_").replace(
                    "/", "_"
                )

                href_map[old_href] = new_href
                href_map[f"./{old_href}"] = f"./{new_href}"

                new_item = epub.EpubItem(
                    uid=new_id,
                    file_name=new_href,
                    media_type=getattr(
                        item, "media_type", "application/octet-stream"
                    ),
                    content=item.get_content(),
                )
                merged.add_item(new_item)

            for item in doc_items:
                old_href = item.get_name()
                base = old_href.split("/")[-1] if "/" in old_href else old_href
                if not base.endswith((".xhtml", ".html")):
                    base = base + ".xhtml"
                new_href = f"{prefix}{base}"
                new_id = prefix + (item.get_id() or base).replace(".", "_").replace(
                    "/", "_"
                )

                href_map[old_href] = new_href
                href_map[f"./{old_href}"] = f"./{new_href}"

                content = item.get_content()
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")

                # Download remote images referenced from this XHTML and embed them locally.
                # We replace the raw remote URL substrings everywhere they occur (src, href, srcset, CSS, ...),
                # so the merged EPUB never points to remote image URLs.
                remote_href_updates: dict[str, str] = {}  # url_raw -> internal_file
                url_candidates = set(_iter_image_urls_in_xhtml(content))
                url_candidates.update(_iter_remote_image_urls_from_text(content))
                for url_raw in url_candidates:
                    url_norm = _normalize_remote_image_url(url_raw)
                    if not url_norm:
                        continue

                    internal_file = downloaded_image_internal_files.get(url_norm)
                    if not internal_file:
                        # Choose extension from the normalized URL path.
                        ext = (Path(urlparse(url_norm).path).suffix or "").lower()
                        if not ext:
                            ext = ".img"
                        digest = hashlib.sha256(url_norm.encode("utf-8")).hexdigest()[:16]
                        internal_file = f"downloaded_images/{digest}{ext}"
                        uid = f"img_{digest}"
                        media_type = _media_type_for_image_url(url_norm)

                        try:
                            payload = _download_url_bytes(url_norm)
                        except HTTPError as http_err:
                            code = int(getattr(http_err, "code", 0) or 0)
                            if 400 <= code < 500:
                                payload = _placeholder_image_bytes(url_norm)
                            else:
                                raise
                        except (http.client.InvalidURL, ValueError, Exception) as dl_err:
                            print(
                                f"Download failed (using placeholder): {url_norm} ({dl_err})",
                                file=sys.stderr,
                                flush=True,
                            )
                            payload = _placeholder_image_bytes(url_norm)

                        merged.add_item(
                            epub.EpubItem(
                                uid=uid,
                                file_name=internal_file,
                                media_type=media_type,
                                content=payload,
                            )
                        )
                        downloaded_image_internal_files[url_norm] = internal_file

                    remote_href_updates[url_raw] = internal_file

                # Update local href mappings (resource file renames).
                for old, new in sorted(href_map.items(), key=lambda x: -len(x[0])):
                    content = content.replace(f'href="{old}"', f'href="{new}"')
                    content = content.replace(f"href='{old}'", f"href='{new}'")
                    content = content.replace(f'src="{old}"', f'src="{new}"')
                    content = content.replace(f"src='{old}'", f"src='{new}'")

                # Replace remote image URL substrings everywhere (srcset, CSS, ...).
                for old, new in sorted(
                    remote_href_updates.items(), key=lambda x: -len(x[0])
                ):
                    content = content.replace(old, new)

                chapter_title = item.title or f"Article {idx + 1}"
                new_chapter = epub.EpubHtml(
                    title=chapter_title,
                    file_name=new_href,
                    lang="en",
                    uid=new_id,
                )
                new_chapter.set_content(
                    content.encode("utf-8") if isinstance(content, str) else content
                )
                merged.add_item(new_chapter)
                # Only one TOC entry per article: the main content (not covers/toc pages)
                if not added_toc_for_article and (
                    main_content_id is None or item.get_id() == main_content_id
                ):
                    best_title = ((item.title or "").strip() or book_level_title or chapter_title).strip()
                    toc_label = (
                        f"Article {idx + 1} — {best_title}"
                        if best_title and best_title != f"Article {idx + 1}"
                        else f"Article {idx + 1}"
                    )
                    toc_entries.append(epub.Link(new_href, toc_label, new_id))
                    added_toc_for_article = True
                spine_items.append(new_chapter)
                chapters_for_cover_remap.append(new_chapter)

        merged.toc = tuple(toc_entries)
        merged.spine = spine_items
        merged.add_item(epub.EpubNcx())
        merged.add_item(epub.EpubNav())

        # Point each article's CoverPage/toc links to the merged TOC (nav)
        nav_item = next(
            (i for i in merged.get_items() if isinstance(i, epub.EpubNav)), None
        )
        if nav_item:
            nav_href = nav_item.get_name() or ""
            if nav_href:
                for chapter in chapters_for_cover_remap:
                    raw = chapter.get_content()
                    if raw is None:
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    # Remap article_*_CoverPage.xhtml and article_*_epub3toc.xhtml -> nav
                    new_text = re.sub(
                        r"article_\d+_CoverPage\.xhtml",
                        nav_href,
                        text,
                    )
                    new_text = re.sub(
                        r"article_\d+_epub3toc\.xhtml",
                        nav_href,
                        new_text,
                    )
                    if new_text != text:
                        chapter.set_content(
                            new_text.encode("utf-8")
                            if isinstance(new_text, str)
                            else new_text
                        )

        epub.write_epub(output_path, merged)
        return output_path


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge multiple EPUB files into a single EPUB with a unified table of contents.",
    )
    parser.add_argument(
        "epub_files",
        nargs="+",
        help="Paths to EPUB files to merge (order is preserved).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="merged_articles.epub",
        help="Base output filename (default: %(default)s). A timestamp suffix is always added.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if not EBOOKLIB_AVAILABLE:
        parser.error(
            "ebooklib is required to run the merger. Install it with: pip install ebooklib"
        )

    epub_paths = [str(Path(p)) for p in args.epub_files]
    output_path = str(Path(args.output))

    merger = EpubMerger()
    try:
        result = merger.merge(epub_paths, output_path=output_path)
    except Exception as exc:  # pragma: no cover - CLI surface
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
