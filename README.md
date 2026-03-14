# Wallabag To Epub

Simple script to export many articles from wallabag as epub (or pdf).

Few features and almost no error handling, written as an exercice to use wallabag from python.

Forked from [Krysztophe/wallabag2epub](https://gitlab.com/Krysztophe/wallabag2epub).

## Features

* Export every article to separate file
* Format can be EPUB or PDF
* If format is EPUB and config option is `merge: true`, a download batch will end in creating additional merged_articles.epub file


## Running

1. Copy wallabag2epub.login.example into wallabag2epub.login and fill with your Wallabag API credentials.