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

from epub_merger import EpubMerger


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
            and EpubMerger.is_available()
            and exported_files
        ):
            merged_path = EpubMerger().merge(
                exported_files, "merged_articles.epub", title="Wallabag Export"
            )
            print("Merged all articles into", merged_path)

        return exported_files


def main() -> None:
    """Entry point: load config and run export."""
    client = Wallabag2Epub.from_config_file("wallabag2epub.login")
    client.run()


if __name__ == "__main__":
    main()
