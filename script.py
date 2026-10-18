"""Build the CD collection website from collection.csv.

Covers are looked up automatically (iTunes, then MusicBrainz / Cover Art Archive)
and stored as covers/<slug>.jpg. To fix a wrong cover, replace that file by hand.
"""
import argparse
import csv
import html
import json
import re
import shutil
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

USER_AGENT = "GeorgeCDCollection/1.0 (https://github.com/George-Marchment/collection)"
COVERS_DIR = Path("covers")


# ---------- Reading and checking the CSV ----------

def read_collection(file_path):
    albums = []
    with open(file_path, mode="r", encoding="utf-8") as file:
        reader = csv.DictReader(file, delimiter=";")
        for line_number, row in enumerate(reader, start=2):
            artist = (row.get("Artist") or "").strip()
            album = (row.get("Album") or "").strip()
            year = (row.get("Year") or "").strip()
            if not (artist or album or year):
                continue
            if not artist or not album:
                warn(f"line {line_number}: missing artist or album, skipped")
                continue
            if not year.isdigit():
                warn(f"line {line_number}: year '{year}' is not a number")
            albums.append({"artist": artist, "album": album, "year": year})

    seen = Counter((normalise(a["artist"]), normalise(a["album"])) for a in albums)
    for (artist, album), count in seen.items():
        if count > 1:
            warn(f"duplicate entry: {artist} - {album}")
    return albums


def sort_key(entry):
    artist = normalise(entry["artist"])
    if artist.startswith("the "):
        artist = artist[4:]
    year = int(entry["year"]) if entry["year"].isdigit() else 0
    return (artist, year, normalise(entry["album"]))


# ---------- Helpers ----------

def warn(message):
    print(f"WARNING: {message}", file=sys.stderr)


def normalise(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("&", "and").replace("$", "s").lower()
    text = re.sub(r"[^\w\s']", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(entry):
    text = normalise(f"{entry['artist']} {entry['album']}").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "album"


def similarity(a, b):
    if not normalise(a) or not normalise(b):  # titles made only of symbols, e.g. "+"
        return 1.0 if a.strip().lower() == b.strip().lower() else 0.0
    if normalise(a) == normalise(b):
        return 1.0
    return max(SequenceMatcher(None, normalise(a), normalise(b)).ratio(),
               SequenceMatcher(None, short_title(a), short_title(b)).ratio())


def fetch(url, retries=3):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code in (403, 429, 503) and attempt < retries - 1:
                time.sleep(10 * (attempt + 1))  # rate limited, back off
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise
    return None


# ---------- Cover lookup ----------

def is_soundtrack(entry):
    # Soundtracks are written with the film as both artist and album, e.g. "Grease;Grease"
    return normalise(entry["artist"]) == normalise(entry["album"])


def short_title(text):
    """Title without edition suffixes such as "(Original Motion Picture Soundtrack)"."""
    return normalise(re.split(r" ?[(\[]| - |: ", text)[0])


def find_cover_itunes(entry):
    soundtrack = is_soundtrack(entry)
    term = f"{entry['album']} soundtrack" if soundtrack else f"{entry['artist']} {entry['album']}"
    query = urllib.parse.urlencode({"term": term, "entity": "album", "limit": 10})
    data = fetch(f"https://itunes.apple.com/search?{query}")
    if not data:
        return None
    best, best_score = None, 0
    for result in json.loads(data).get("results", []):
        title = result.get("collectionName", "")
        if soundtrack:
            if short_title(title) != normalise(entry["album"]) or "soundtrack" not in title.lower():
                continue
            score = 1
        else:
            artist_score = similarity(entry["artist"], result.get("artistName", ""))
            album_score = similarity(entry["album"], title)
            if artist_score < 0.7 or album_score < 0.85:
                continue
            score = artist_score + album_score
        if score > best_score and result.get("artworkUrl100"):
            best, best_score = result, score
    if best:
        return best["artworkUrl100"].replace("100x100bb", "600x600bb")
    return None


def find_cover_musicbrainz(entry):
    def escape(text):
        return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", text)

    soundtrack = is_soundtrack(entry)
    if soundtrack:
        query = f"releasegroup:({escape(entry['album'])}) AND secondarytype:soundtrack"
        if entry["year"].isdigit():
            query += f" AND firstreleasedate:{entry['year']}"
    else:
        query = f"releasegroup:({escape(entry['album'])}) AND artist:({escape(entry['artist'])})"
    url = "https://musicbrainz.org/ws/2/release-group/?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": 10})
    data = fetch(url)
    time.sleep(1.1)  # MusicBrainz allows one request per second
    if not data:
        return None
    for group in json.loads(data).get("release-groups", []):
        title = group.get("title", "")
        if soundtrack:
            if short_title(title) != normalise(entry["album"]):
                continue
        elif similarity(entry["album"], title) < 0.85:
            continue
        credit = "".join(c.get("name", "") + c.get("joinphrase", "")
                         for c in group.get("artist-credit", []))
        if not soundtrack and similarity(entry["artist"], credit) < 0.7:
            continue
        return f"https://coverartarchive.org/release-group/{group['id']}/front-500"
    return None


def get_cover(entry, fetch_dir):
    """Return the path of the cover image, downloading it if needed."""
    filename = f"{slugify(entry)}.jpg"
    for folder in (COVERS_DIR, fetch_dir):
        if (folder / filename).exists():
            return folder / filename

    label = f"{entry['artist']} - {entry['album']}"
    for finder in (find_cover_itunes, find_cover_musicbrainz):
        try:
            image_url = finder(entry)
            image = fetch(image_url) if image_url else None
        except Exception as error:  # network issue: carry on without this cover
            warn(f"{finder.__name__} failed for {label}: {error}")
            continue
        if image:
            fetch_dir.mkdir(parents=True, exist_ok=True)
            (fetch_dir / filename).write_bytes(image)
            print(f"Found cover: {label} -> {fetch_dir / filename}")
            return fetch_dir / filename
    warn(f"no cover found for {label} (add covers/{filename} by hand)")
    return None


# ---------- Website ----------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>George's CD Collection</title>
<style>
  :root {{
    --bg: #f6f5f2; --card: #ffffff; --text: #1d1d1f; --muted: #6e6e73;
    --border: #e2e0db; --accent: #b4442f;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #141414; --card: #1f1f1f; --text: #f2f2f2; --muted: #a1a1a6;
      --border: #333333; --accent: #e8735c;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  main {{ max-width: 1200px; margin: 0 auto; padding: 32px 16px 64px; }}
  h1 {{ margin: 0 0 8px; font-size: 2rem; }}
  .intro {{ color: var(--muted); margin: 0 0 20px; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
  .stat {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 14px; min-width: 120px;
  }}
  .stat strong {{ display: block; font-size: 1.3rem; }}
  .stat span {{ color: var(--muted); font-size: 0.85rem; }}
  #search {{
    width: 100%; max-width: 420px; padding: 10px 14px; font-size: 1rem;
    border: 1px solid var(--border); border-radius: 8px;
    background: var(--card); color: var(--text); margin-bottom: 24px;
  }}
  .grid {{
    display: grid; gap: 20px;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  }}
  .album {{ margin: 0; }}
  .album img, .album .placeholder {{
    width: 100%; aspect-ratio: 1; object-fit: cover; display: block;
    border-radius: 6px; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18);
    background: var(--border);
  }}
  .album .placeholder {{
    display: flex; align-items: center; justify-content: center;
    padding: 12px; text-align: center; color: var(--muted); font-size: 0.9rem;
  }}
  .album figcaption {{ margin-top: 8px; line-height: 1.3; }}
  .album .title {{ font-weight: 600; display: block; }}
  .album .artist {{ color: var(--muted); font-size: 0.9rem; }}
  .album .year {{ color: var(--accent); font-size: 0.85rem; }}
  .empty {{ color: var(--muted); display: none; }}
</style>
</head>
<body>
<main>
  <h1>George's CD Collection</h1>
  <p class="intro">{count} albums &middot; last updated {updated}</p>
  <div class="stats">{stats}</div>
  <input id="search" type="search" placeholder="Search artist, album or year..." aria-label="Search">
  <div class="grid" id="grid">
{albums}
  </div>
  <p class="empty" id="empty">No albums match your search.</p>
</main>
<script>
  const search = document.getElementById("search");
  const albums = document.querySelectorAll(".album");
  search.addEventListener("input", () => {{
    const query = search.value.trim().toLowerCase();
    let shown = 0;
    albums.forEach(album => {{
      const match = album.dataset.search.includes(query);
      album.hidden = !match;
      if (match) shown++;
    }});
    document.getElementById("empty").style.display = shown ? "none" : "block";
  }});
</script>
</body>
</html>
"""

ALBUM_TEMPLATE = """    <figure class="album" data-search="{search}">
      {image}
      <figcaption>
        <span class="title">{album}</span>
        <span class="artist">{artist}</span> <span class="year">{year}</span>
      </figcaption>
    </figure>"""


def build_stats(albums):
    artists = Counter(a["artist"] for a in albums)
    decades = Counter(f"{a['year'][:3]}0s" for a in albums if a["year"].isdigit())
    top_artist, top_count = artists.most_common(1)[0]
    top_decade, decade_count = decades.most_common(1)[0]
    stats = [
        (len(albums), "albums"),
        (len(artists), "artists"),
        (f"{top_artist}", f"most collected ({top_count})"),
        (top_decade, f"favourite decade ({decade_count})"),
    ]
    return "".join(
        f'<div class="stat"><strong>{html.escape(str(value))}</strong>'
        f"<span>{html.escape(label)}</span></div>"
        for value, label in stats)


def write_site(albums, covers, output_dir):
    if output_dir.exists():
        shutil.rmtree(output_dir)
    (output_dir / "covers").mkdir(parents=True)

    cards = []
    for entry in albums:
        cover = covers.get(slugify(entry))
        alt = html.escape(f"{entry['album']} by {entry['artist']}", quote=True)
        if cover:
            shutil.copy(cover, output_dir / "covers" / cover.name)
            image = f'<img src="covers/{cover.name}" alt="{alt}" loading="lazy">'
        else:
            image = f'<div class="placeholder">{html.escape(entry["album"])}</div>'
        search = " ".join([entry["artist"], entry["album"], entry["year"]]).lower()
        cards.append(ALBUM_TEMPLATE.format(
            search=html.escape(search, quote=True),
            image=image,
            album=html.escape(entry["album"]),
            artist=html.escape(entry["artist"]),
            year=html.escape(entry["year"]),
        ))

    page = PAGE_TEMPLATE.format(
        count=len(albums),
        updated=datetime.today().strftime("%d/%m/%Y"),
        stats=build_stats(albums),
        albums="\n".join(cards),
    )
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="collection.csv")
    parser.add_argument("--output", default="_site", type=Path,
                        help="folder the website is written to")
    parser.add_argument("--fetch-dir", default=COVERS_DIR, type=Path,
                        help="folder newly downloaded covers are saved to")
    args = parser.parse_args()

    albums = sorted(read_collection(args.input), key=sort_key)
    covers = {}
    for entry in albums:
        cover = get_cover(entry, args.fetch_dir)
        if cover:
            covers[slugify(entry)] = cover
    write_site(albums, covers, args.output)
    print(f"Built {args.output / 'index.html'}: {len(albums)} albums, "
          f"{len(covers)} with covers")


if __name__ == "__main__":
    main()
