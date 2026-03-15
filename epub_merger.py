"""EPUB merging: combine multiple EPUB files into one with a table of contents."""

import os
import tempfile
import zipfile
import xml.etree.ElementTree as ET
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

        merged = epub.EpubBook()
        merged.set_identifier("merged-" + str(hash(tuple(epub_paths)))[:12])
        merged.set_title(title)
        merged.set_language("en")

        toc_entries = []
        spine_items = ["nav"]

        for idx, path in enumerate(epub_paths):
            href_map = {}
            prefix = f"article_{idx:04d}_"
            try:
                book = _read_epub_robust(path, options={"ignore_ncx": True})
            except Exception as e:
                print(f"Warning: Could not read {path}: {e}")
                continue

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
                toc_entries.append(epub.Link(new_href, chapter_title, new_id))
                spine_items.append(new_chapter)

        merged.toc = tuple(toc_entries)
        merged.spine = spine_items
        merged.add_item(epub.EpubNcx())
        merged.add_item(epub.EpubNav())

        epub.write_epub(output_path, merged)
        return output_path
