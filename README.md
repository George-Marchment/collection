# George's CD Collection

🎵 **See the collection: https://george-marchment.github.io/collection/**

The website is built automatically from [`collection.csv`](collection.csv) each time something is pushed to `main`.

## Adding an album

1. Add a line to `collection.csv`: `Artist;Album;Year`
2. Commit and push.

The cover is found automatically (iTunes, then MusicBrainz). If the wrong cover shows up, or none at all,
put the right image in `covers/` using the file name the build log shows, e.g. `covers/blur-parklife.jpg`.


To preview locally: `python script.py`, then open `_site/index.html`.
