"""EPUB merging: combine multiple EPUB files into one with a table of contents."""

import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

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
        opf_root = ET.fromstring(opf_bytes)

        # Register default namespace so find/findall work
        ET.register_namespace("", OPF_NS)
        ns = {"opf": OPF_NS}
        manifest = opf_root.find("opf:manifest", ns)
        if manifest is None:
            return epub_path

        ids_to_remove = []
        for item in manifest.findall("opf:item", ns):
            href = item.get("href")
            if not href:
                continue
            # Resolve path relative to OPF directory
            full = f"{opf_dir}/{href}" if opf_dir else href
            full = full.lstrip("/").replace("//", "/")
            if full not in names:
                ids_to_remove.append(item.get("id"))

        if not ids_to_remove:
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
            return tmp_path
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise


def _read_epub_robust(path: str, options: dict | None = None) -> "epub.EpubBook":
    """
    Read an EPUB, fixing manifest entries for missing archive items if needed
    so that ebooklib does not raise (e.g. 'There is no item named ...').
    """
    if options is None:
        options = {"ignore_ncx": True}
    try:
        return epub.read_epub(path, options=options)
    except (KeyError, Exception) as e:
        err_msg = str(e)
        if "There is no item named" not in err_msg and "no item named" not in err_msg.lower():
            raise
        fixed_path = _fix_epub_missing_manifest(path)
        try:
            return epub.read_epub(fixed_path, options=options)
        finally:
            if fixed_path != path and Path(fixed_path).exists():
                Path(fixed_path).unlink(missing_ok=True)


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

        for idx, path in enumerate(epub_paths):
            href_map = {}
            prefix = f"article_{idx:04d}_"
            try:
                book = _read_epub_robust(path, options={"ignore_ncx": True})
            except Exception as e:
                print(f"Warning: Could not read {path}: {e}")
                continue

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

                for old, new in sorted(href_map.items(), key=lambda x: -len(x[0])):
                    content = content.replace(f'href="{old}"', f'href="{new}"')
                    content = content.replace(f"href='{old}'", f"href='{new}'")
                    content = content.replace(f'src="{old}"', f'src="{new}"')
                    content = content.replace(f"src='{old}'", f"src='{new}'")

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
                    toc_label = (
                        f"Article {idx + 1} — {chapter_title}"
                        if (item.title or "").strip()
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
