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
"""

import yaml
import requests

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
        # default
        extension = 'epub'

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

# main

print ("Getting a token…")

# get a new token
token = get_token(host=wb_url, **config)

# get all the articles matching previous tags
articles_json = get_articles(wb_url, token, starred, nb_articles)
all_articles = articles_json['_embedded']['items']

for article in all_articles:
    print("\n Exporting … ", article['id'], article['title'])
    contenu = export_article (wb_url, token, article['id'])
    fichier = "".join(c for c in article['title'] if c.isalnum() or c in keepcharacters).rstrip()+'.'+extension
    with open(fichier, "wb") as f:
        f.write(contenu)
    print ("Exported ", fichier, ", now set article as read")
    print (set_article_as_read (wb_url, token, article['id'], starred))

print (str(nb_articles), ' articles exported')
