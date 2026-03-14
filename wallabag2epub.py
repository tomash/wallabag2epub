#!/usr/bin/env python3

"""
Script to mass download EPUB or PDF (or mobi, html) from your wallabag server
Starred articles are exported as epub (may be changed in config)
beginning from the newest, as far 50 at once
and are set as read after that.

No other filtering is done.

This was made as an example on how to export from Wallabag.
Inspired from code from https://pypi.org/project/wallabagapi/ (does not seem to work anymore, and upstream has disappeared)
https://doc.wallabag.org/en/developer/api/methods.html
https://gist.github.com/petermolnar/988ba2fa2770b71a443e437cd4052aeb

File wallabag2pdf.login must contain (see example file):

url: https://mywallabagserver/
user: username
password: yourtotallysecretpassword
client_id: idcreatedforthisappontheserver
client_secret: secretcreatedforthisappontheserver
extension: epub   # default, or 'xml', 'json', 'txt', 'csv', 'pdf', 'epub', 'mobi', 'html'...
starred: true     # default, or false
"""

import yaml
import requests

try:
    import ebooklib
    from ebooklib import epub
    EBOOKLIB_AVAILABLE = True
except ImportError:
    EBOOKLIB_AVAILABLE = False


class Wallabag2Epub:
    """Client for exporting articles from Wallabag to EPUB/PDF and other formats."""

    KEEP_CHARACTERS = (" ", ".", "_")
    DEFAULT_NB_ARTICLES = 50

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        client_id: str,
        client_secret: str,
        extension: str = "epub",
        starred: bool = True,
        merge: bool = False,
        nb_articles: int = 50,
    ):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.extension = extension
        self.starred = starred
        self.merge = merge
        self.nb_articles = nb_articles

    @classmethod
    def from_config_file(cls, path: str = "wallabag2epub.login") -> "Wallabag2Epub":
        """Create instance from YAML config file."""
        with open(path, "r") as f:
            infocon = yaml.safe_load(f)
        return cls(
            url=infocon["url"],
            user=infocon["user"],
            password=infocon["password"],
            client_id=infocon["client_id"],
            client_secret=infocon["client_secret"],
            extension=infocon.get("extension", "epub"),
            starred=infocon.get("starred", True),
            merge=infocon.get("merge", False),
            nb_articles=cls.DEFAULT_NB_ARTICLES,
        )

    def _auth_params(self) -> dict:
        """OAuth token request parameters."""
        return {
            "username": self.user,
            "password": self.password,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "password",
        }

    def get_token(self) -> str:
        """
        Gets a new OAuth token.

        Returns:
            access token
        """
        r = requests.get(
            "{}/oauth/v2/token".format(self.url),
            params=self._auth_params(),
        )
        access = r.json().get("access_token")
        print("Token is: ", access)
        return access

    def get_articles(self, token: str) -> dict:
        """
        Retrieve articles list.

        Args:
            token: OAuth access token

        Returns:
            JSON with all articles
        """
        params = {
            "Authorization": "Bearer {}".format(token),
            "access_token": token,
            "archive": 0,
            "starred": int(self.starred),
            "sort": "created",
            "order": "desc",
            "page": 1,
            "perPage": self.nb_articles,
            "tags": "",
            "since": 0,
        }
        print(params)
        query = "{}/api/entries.{ext}".format(self.url, ext=self.extension)
        print(query)
        r = requests.get(query, params)
        return r.json()

    def export_article(self, token: str, article_id: int) -> bytes:
        """
        Export the given article.

        Args:
            token: OAuth access token
            article_id: Article ID

        Returns:
            Binary content (EPUB, PDF, etc.)
        """
        query = "{}/api/entries/{entry}/export.{ext}".format(
            self.url, entry=article_id, ext=self.extension
        )
        print(query)
        r = requests.get(query, {"access_token": token})
        return r.content

    def set_article_as_read(self, token: str, article_id: int) -> None:
        """
        Set the article as archived (read).

        Args:
            token: OAuth access token
            article_id: Article ID
        """
        params = {
            "Authorization": "Bearer {}".format(token),
            "access_token": token,
            "archive": 1,
            "starred": int(self.starred),
        }
        query = "{}/api/entries/{entry}.{ext}".format(
            self.url, entry=article_id, ext=self.extension
        )
        print(query)
        r = requests.patch(query, params)
        print(r)
        return None

    def _sanitize_filename(self, title: str) -> str:
        """Sanitize article title for use as filename."""
        return "".join(
            c for c in title if c.isalnum() or c in self.KEEP_CHARACTERS
        ).rstrip()

    def run(self) -> list[str]:
        """
        Execute full export: get token, fetch articles, export each, mark as read.

        Returns:
            List of exported file paths.
        """
        print("Getting a token…")
        token = self.get_token()

        articles_json = self.get_articles(token)
        all_articles = articles_json["_embedded"]["items"]

        exported_files = []
        for article in all_articles:
            print("\n Exporting … ", article["id"], article["title"])
            content = self.export_article(token, article["id"])
            filename = (
                self._sanitize_filename(article["title"]) + "." + self.extension
            )
            with open(filename, "wb") as f:
                f.write(content)
            exported_files.append(filename)
            print("Exported ", filename, ", now set article as read")
            self.set_article_as_read(token, article["id"])

        print(str(self.nb_articles), " articles exported")

        if (
            self.extension == "epub"
            and self.merge
            and EBOOKLIB_AVAILABLE
            and exported_files
        ):
            merged_path = self.merge_epubs(
                exported_files, "merged_articles.epub", title="Wallabag Export"
            )
            print("Merged all articles into", merged_path)

        return exported_files

    @staticmethod
    def merge_epubs(
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
            Path to the created file, or None if ebooklib is not available.
        """
        if not EBOOKLIB_AVAILABLE:
            raise ImportError(
                "ebooklib is required for merge_epubs. Install with: pip install ebooklib"
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
                book = epub.read_epub(path, options={"ignore_ncx": True})
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


def main() -> None:
    """Entry point: load config and run export."""
    client = Wallabag2Epub.from_config_file("wallabag2epub.login")
    client.run()


if __name__ == "__main__":
    main()
