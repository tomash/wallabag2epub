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

# export only starred articles
starred=True
nb_articles=50

# for sanitization
keepcharacters = (' ','.','_')

# Load config

with open('wallabag2epub.login','r') as file:
    infocon=yaml.safe_load (file)
    wb_url = infocon['url']
    wb_user = infocon['user']
    wb_password = infocon['password']
    wb_client_id = infocon['client_id']
    wb_client_secret = infocon['client_secret']
    try:
        extension = infocon['extension']
    except (KeyError):
        extension = 'epub'
    try:
        merge_after_export = infocon.get('merge', False)
    except (KeyError):
        merge_after_export = False
    try:
        starred = infocon['starred']
    except (KeyError):
        starred = True

config = {'username': wb_user,
            'password': wb_password,
            'client_id': wb_client_id,
            'client_secret': wb_client_secret,
            'grant_type': 'password'
            }

def get_token (host, **params):
    """
    Gets a new token

    Params: host of the service, 

    params = {"grant_type": "password",
                "client_id": "a string",
                "client_secret": "a string",
                "username": "a login",
                "password": "a password"}

    :return: access token
    """
    r = requests.get('{}/oauth/v2/token'.format(host), params)
    access = r.json().get('access_token')
    print ('Token is: ', access)
    return access

def get_articles (host, token, starred, nb):
    """
    Retrieve articles list
    
    Params: host, token, starred (1 for NOT or 0 for Starred ?!), nb

    Returns: a big JSON with all articles
    
    """
    filter = dict({
                'Authorization': 'Bearer {}'.format(token),
                'access_token': token,
                'archive': 0,
                'starred': int(starred),  # strange, need 0 to get starred article
                'sort': 'created',
                'order': 'desc',
                'page': 1,
                'perPage': nb,
                'tags': '',
                'since': 0
                })
    print (filter)
    query='{}/api/entries.{ext}'.format(host,ext=extension)
    print (query)
    # Retrieve all entries. It could be filtered by many options.
    r = requests.get(query, filter)
    return (r.json())

def export_article (host, token, article_id):
    """
    Export the given article
    
    Returns: binary (EPUB, PDF...)
    """
    query='{}/api/entries/{entry}/export.{ext}'.format(host,entry=article_id,ext=extension)
    print (query)
    r = requests.get(query, {'access_token': token} )
    return r.content


def set_article_as_read (host, token, article_id, starred):
    """
    Set the article as 'archive'
    
    Returns: binary
    """
    props = dict({
                'Authorization': 'Bearer {}'.format(token),
                'access_token': token,
                'archive': 1,  # archived (and not 0!)
                'starred': int(starred),  # keep the previous status
                })
    query='{}/api/entries/{entry}.{ext}'.format(host,entry=article_id,ext=extension)
    print (query)
    r = requests.patch(query, props )
    print (r)
    return


def merge_epubs(epub_paths, output_path, title="Merged Articles"):
    """
    Merge multiple EPUB files (one per article) into a single combined EPUB
    with a working table of contents.

    Args:
        epub_paths: List of paths to EPUB files (order preserved)
        output_path: Path for the merged output EPUB
        title: Title for the merged book (default: "Merged Articles")

    Returns:
        Path to the created file, or None if ebooklib is not available.
    """
    if not EBOOKLIB_AVAILABLE:
        raise ImportError("ebooklib is required for merge_epubs. Install with: pip install ebooklib")

    merged = epub.EpubBook()
    merged.set_identifier("merged-" + str(hash(tuple(epub_paths)))[:12])
    merged.set_title(title)
    merged.set_language("en")

    toc_entries = []
    spine_items = ["nav"]

    for idx, path in enumerate(epub_paths):
        href_map = {}  # old href -> new href for this book only
        prefix = f"article_{idx:04d}_"
        try:
            book = epub.read_epub(path, options={"ignore_ncx": True})
        except Exception as e:
            print(f"Warning: Could not read {path}: {e}")
            continue

        # Collect items that are documents (chapters) vs resources (images, css)
        doc_items = []
        resource_items = []

        for item in book.get_items():
            if isinstance(item, epub.EpubHtml):
                doc_items.append(item)
            else:
                resource_items.append(item)

        # Add resource items first (images, styles) with unique names
        # Skip EpubNcx, EpubNav - we add our own at the end
        for item in resource_items:
            if isinstance(item, (epub.EpubNcx, epub.EpubNav)):
                continue
            if not isinstance(item, epub.EpubItem):
                continue
            old_href = item.get_name()
            base = old_href.split("/")[-1] if "/" in old_href else old_href
            new_href = f"{prefix}res/{base}"
            new_id = prefix + (item.get_id() or base).replace(".", "_").replace("/", "_")

            href_map[old_href] = new_href
            href_map[f"./{old_href}"] = f"./{new_href}"

            new_item = epub.EpubItem(
                uid=new_id,
                file_name=new_href,
                media_type=getattr(item, 'media_type', 'application/octet-stream'),
                content=item.get_content(),
            )
            merged.add_item(new_item)

        # Add document items (chapters) and fix internal references
        for item in doc_items:
            old_href = item.get_name()
            base = old_href.split("/")[-1] if "/" in old_href else old_href
            if not base.endswith((".xhtml", ".html")):
                base = base + ".xhtml"
            new_href = f"{prefix}{base}"
            new_id = prefix + (item.get_id() or base).replace(".", "_").replace("/", "_")

            href_map[old_href] = new_href
            href_map[f"./{old_href}"] = f"./{new_href}"

            content = item.get_content()
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")

            # Fix href/src references in content (images, links, stylesheets)
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
            new_chapter.set_content(content.encode("utf-8") if isinstance(content, str) else content)
            merged.add_item(new_chapter)
            toc_entries.append(epub.Link(new_href, chapter_title, new_id))
            spine_items.append(new_chapter)

    merged.toc = tuple(toc_entries)
    merged.spine = spine_items
    merged.add_item(epub.EpubNcx())
    merged.add_item(epub.EpubNav())

    epub.write_epub(output_path, merged)
    return output_path


# main

print ("Getting a token…")

# get a new token
token = get_token(host=wb_url, **config)

# get all the articles matching previous tags
articles_json = get_articles(wb_url, token, starred, nb_articles)
all_articles = articles_json['_embedded']['items']

exported_files = []
for article in all_articles:
    print("\n Exporting … ", article['id'], article['title'])
    contenu = export_article (wb_url, token, article['id'])
    fichier = "".join(c for c in article['title'] if c.isalnum() or c in keepcharacters).rstrip()+'.'+extension
    with open(fichier, "wb") as f:
        f.write(contenu)
    exported_files.append(fichier)
    print ("Exported ", fichier, ", now set article as read")
    print (set_article_as_read (wb_url, token, article['id'], starred))

print (str(nb_articles), ' articles exported')

if extension == 'epub' and merge_after_export and EBOOKLIB_AVAILABLE and exported_files:
    merged_path = merge_epubs(exported_files, "merged_articles.epub", title="Wallabag Export")
    print("Merged all articles into", merged_path)
