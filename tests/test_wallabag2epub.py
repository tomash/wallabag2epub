"""Unit tests for Wallabag2Epub class."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wallabag2epub import Wallabag2Epub, EBOOKLIB_AVAILABLE


@pytest.fixture
def client():
    """Create a Wallabag2Epub instance for testing."""
    return Wallabag2Epub(
        url="https://wallabag.example.com",
        user="testuser",
        password="testpass",
        client_id="client_id",
        client_secret="client_secret",
        extension="epub",
        starred=True,
        merge=False,
        nb_articles=5,
    )


@pytest.fixture
def sample_config():
    """Sample config dict for from_config_file tests."""
    return {
        "url": "https://mywallabag.example/",
        "user": "jane",
        "password": "secret",
        "client_id": "abc123",
        "client_secret": "xyz789",
        "extension": "pdf",
        "starred": False,
        "merge": True,
    }


class TestWallabag2EpubInit:
    """Tests for Wallabag2Epub initialization."""

    def test_init_stores_all_params(self, client):
        assert client.url == "https://wallabag.example.com"
        assert client.user == "testuser"
        assert client.password == "testpass"
        assert client.client_id == "client_id"
        assert client.client_secret == "client_secret"
        assert client.extension == "epub"
        assert client.starred is True
        assert client.merge is False
        assert client.nb_articles == 5

    def test_init_strips_trailing_slash_from_url(self):
        c = Wallabag2Epub(
            url="https://example.com/",
            user="u",
            password="p",
            client_id="c",
            client_secret="s",
        )
        assert c.url == "https://example.com"

    def test_init_defaults(self):
        c = Wallabag2Epub(
            url="https://x.com",
            user="u",
            password="p",
            client_id="c",
            client_secret="s",
        )
        assert c.extension == "epub"
        assert c.starred is True
        assert c.merge is False
        assert c.nb_articles == 50


class TestFromConfigFile:
    """Tests for from_config_file class method."""

    def test_from_config_file_loads_all_fields(self, sample_config):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            import yaml

            yaml.dump(sample_config, f)
            path = f.name

        try:
            client = Wallabag2Epub.from_config_file(path)
            # URL is normalized (trailing slash stripped)
            assert client.url == sample_config["url"].rstrip("/")
            assert client.user == sample_config["user"]
            assert client.password == sample_config["password"]
            assert client.client_id == sample_config["client_id"]
            assert client.client_secret == sample_config["client_secret"]
            assert client.extension == sample_config["extension"]
            assert client.starred == sample_config["starred"]
            assert client.merge == sample_config["merge"]
        finally:
            Path(path).unlink()

    def test_from_config_file_uses_defaults_for_missing_keys(self):
        minimal = {
            "url": "https://x.com",
            "user": "u",
            "password": "p",
            "client_id": "c",
            "client_secret": "s",
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            import yaml

            yaml.dump(minimal, f)
            path = f.name

        try:
            client = Wallabag2Epub.from_config_file(path)
            assert client.extension == "epub"
            assert client.starred is True
            assert client.merge is False
        finally:
            Path(path).unlink()


class TestAuthParams:
    """Tests for _auth_params."""

    def test_auth_params_contains_required_fields(self, client):
        params = client._auth_params()
        assert params["username"] == "testuser"
        assert params["password"] == "testpass"
        assert params["client_id"] == "client_id"
        assert params["client_secret"] == "client_secret"
        assert params["grant_type"] == "password"


class TestGetToken:
    """Tests for get_token."""

    @patch("wallabag2epub.requests.get")
    def test_get_token_returns_access_token(self, mock_get, client):
        mock_get.return_value.json.return_value = {"access_token": "abc123"}
        mock_get.return_value.status_code = 200

        token = client.get_token()

        assert token == "abc123"
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "oauth/v2/token" in call_args[0][0]
        assert call_args[1]["params"]["username"] == "testuser"

    @patch("wallabag2epub.requests.get")
    def test_get_token_uses_correct_url(self, mock_get, client):
        mock_get.return_value.json.return_value = {"access_token": "x"}
        mock_get.return_value.status_code = 200

        client.get_token()

        assert mock_get.call_args[0][0] == (
            "https://wallabag.example.com/oauth/v2/token"
        )


class TestGetArticles:
    """Tests for get_articles."""

    @patch("wallabag2epub.requests.get")
    def test_get_articles_returns_json(self, mock_get, client):
        mock_get.return_value.json.return_value = {
            "_embedded": {"items": [{"id": 1, "title": "Article 1"}]}
        }

        result = client.get_articles("token123")

        assert result["_embedded"]["items"][0]["id"] == 1
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert "api/entries" in call_args[0][0]
        assert call_args[0][0].endswith(".epub")
        # params passed as second positional arg
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("params", {})
        assert params["access_token"] == "token123"
        assert params["starred"] == 1
        assert params["perPage"] == 5


class TestExportArticle:
    """Tests for export_article."""

    @patch("wallabag2epub.requests.get")
    def test_export_article_returns_binary_content(self, mock_get, client):
        mock_get.return_value.content = b"epub binary content"

        result = client.export_article("token123", 42)

        assert result == b"epub binary content"
        call_args = mock_get.call_args
        assert "42" in call_args[0][0]
        assert "export.epub" in call_args[0][0]
        # params passed as second positional arg
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        assert params == {"access_token": "token123"}


class TestSetArticleAsRead:
    """Tests for set_article_as_read."""

    @patch("wallabag2epub.requests.patch")
    def test_set_article_as_read_calls_patch(self, mock_patch, client):
        mock_patch.return_value = MagicMock()

        client.set_article_as_read("token123", 99)

        mock_patch.assert_called_once()
        call_args = mock_patch.call_args
        assert "99" in call_args[0][0]
        # params passed as second positional arg (data=)
        params = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]
        assert params["archive"] == 1
        assert params["starred"] == 1


class TestSanitizeFilename:
    """Tests for _sanitize_filename."""

    def test_sanitize_keeps_alphanumeric_and_special_chars(self, client):
        assert client._sanitize_filename("Hello World 123") == "Hello World 123"
        assert client._sanitize_filename("a.b_c") == "a.b_c"

    def test_sanitize_removes_invalid_chars(self, client):
        assert client._sanitize_filename("Test/Article:Title?") == "TestArticleTitle"

    def test_sanitize_strips_trailing_whitespace(self, client):
        # rstrip() only removes trailing whitespace, not leading
        assert client._sanitize_filename("Title  ") == "Title"


class TestMergeEpubs:
    """Tests for merge_epubs static method."""

    @pytest.mark.skipif(not EBOOKLIB_AVAILABLE, reason="ebooklib not installed")
    def test_merge_epubs_creates_output_file(self, tmp_path):
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

        result = Wallabag2Epub.merge_epubs(
            [str(epub1), str(epub2)], str(out), title="Merged"
        )

        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_merge_epubs_raises_without_ebooklib(self):
        with patch("wallabag2epub.EBOOKLIB_AVAILABLE", False):
            with pytest.raises(ImportError, match="ebooklib is required"):
                Wallabag2Epub.merge_epubs([], "/tmp/out.epub")


class TestRun:
    """Tests for run method."""

    @patch.object(Wallabag2Epub, "set_article_as_read")
    @patch.object(Wallabag2Epub, "export_article")
    @patch.object(Wallabag2Epub, "get_articles")
    @patch.object(Wallabag2Epub, "get_token")
    def test_run_exports_articles_and_marks_read(
        self, mock_token, mock_articles, mock_export, mock_set_read, client
    ):
        mock_token.return_value = "token"
        mock_articles.return_value = {
            "_embedded": {
                "items": [
                    {"id": 1, "title": "First Article"},
                    {"id": 2, "title": "Second Article"},
                ]
            }
        }
        mock_export.return_value = b"content"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("builtins.open", create=True) as mock_open:
                mock_open.return_value.__enter__ = lambda s: s
                mock_open.return_value.__exit__ = lambda s, *a: None
                mock_open.return_value.write = MagicMock()

                with patch("os.getcwd", return_value=tmpdir):
                    result = client.run()

        assert len(result) == 2
        assert "FirstArticle.epub" in result[0] or "First Article.epub" in result[0]
        mock_token.assert_called_once()
        mock_articles.assert_called_once_with("token")
        assert mock_export.call_count == 2
        assert mock_set_read.call_count == 2
