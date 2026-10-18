"""Build the CD collection website from collection.csv.

Covers are looked up automatically (iTunes, then MusicBrainz / Cover Art Archive)
and stored as covers/<slug>.jpg. To fix a wrong cover, replace that file by hand.
Genres and Apple Music links are stored in covers/metadata.json, which can also be
edited by hand.
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
METADATA_FILE = "metadata.json"


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
            albums.append({"artist": artist, "album": album, "year": year,
                           "added": len(albums)})

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


# ---------- Cover and genre lookup ----------

def is_soundtrack(entry):
    # Soundtracks are written with the film as both artist and album, e.g. "Grease;Grease"
    return normalise(entry["artist"]) == normalise(entry["album"])


def short_title(text):
    """Title without edition suffixes such as "(Original Motion Picture Soundtrack)"."""
    return normalise(re.split(r" ?[(\[]| - |: ", text)[0])


_itunes_results = {}


def itunes_match(entry):
    """Best matching iTunes album for this entry (cached for the run), or None."""
    slug = slugify(entry)
    if slug not in _itunes_results:
        # "Grease;Grease" is probably a soundtrack, but "Blur;Blur" is a self-titled
        # album, so the soundtrack search only counts if it finds a soundtrack.
        result = search_itunes(entry, soundtrack=True) if is_soundtrack(entry) else None
        _itunes_results[slug] = result or search_itunes(entry, soundtrack=False)
    return _itunes_results[slug]


def search_itunes(entry, soundtrack):
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
        if score > best_score:
            best, best_score = result, score
    return best


def find_cover_itunes(entry):
    result = itunes_match(entry)
    if result and result.get("artworkUrl100"):
        return result["artworkUrl100"].replace("100x100bb", "600x600bb")
    return None


_musicbrainz_results = {}


def musicbrainz_match(entry):
    """Best matching MusicBrainz release group (cached for the run), or None."""
    slug = slugify(entry)
    if slug not in _musicbrainz_results:
        result = search_musicbrainz(entry, soundtrack=True) if is_soundtrack(entry) else None
        _musicbrainz_results[slug] = result or search_musicbrainz(entry, soundtrack=False)
    return _musicbrainz_results[slug]


def search_musicbrainz(entry, soundtrack):
    def escape(text):
        return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", text)

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
        return group
    return None


def find_cover_musicbrainz(entry):
    group = musicbrainz_match(entry)
    if group:
        return f"https://coverartarchive.org/release-group/{group['id']}/front-500"
    return None


def find_genre_musicbrainz(entry):
    """Most voted genre of the album, or failing that of the artist."""
    group = musicbrainz_match(entry)
    if not group:
        return None
    urls = [f"https://musicbrainz.org/ws/2/release-group/{group['id']}?inc=genres&fmt=json"]
    credits = group.get("artist-credit", [])
    if credits and credits[0].get("artist", {}).get("id") and "Soundtrack" not in group.get("secondary-types", []):
        urls.append(f"https://musicbrainz.org/ws/2/artist/{credits[0]['artist']['id']}?inc=genres&fmt=json")
    for url in urls:
        data = fetch(url)
        time.sleep(1.1)
        genres = json.loads(data).get("genres", []) if data else []
        if genres:
            return max(genres, key=lambda g: g.get("count", 0))["name"]
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


def load_metadata(*folders):
    metadata = {}
    for folder in reversed(folders):  # earlier folders win
        path = folder / METADATA_FILE
        if path.exists():
            metadata.update(json.loads(path.read_text(encoding="utf-8")))
    return metadata


def get_metadata(entry, metadata, fetch_dir):
    """Return {"genre": ..., "link": ...}, looking it up on iTunes if needed."""
    slug = slugify(entry)
    if slug in metadata:
        return metadata[slug]
    label = f"{entry['artist']} - {entry['album']}"
    try:
        result = itunes_match(entry) or {}
    except Exception as error:
        warn(f"genre lookup failed for {label}: {error}")
        return {"genre": None, "link": None}  # not saved, so it is retried next time
    genre = result.get("primaryGenreName")
    if not genre:
        try:
            genre = find_genre_musicbrainz(entry)
        except Exception as error:
            warn(f"genre lookup failed for {label}: {error}")
            return {"genre": None, "link": result.get("collectionViewUrl")}
    info = {"genre": genre, "link": result.get("collectionViewUrl")}
    if not genre:
        warn(f"no genre found for {label} (set it in covers/{METADATA_FILE})")

    metadata[slug] = info
    saved = load_metadata(fetch_dir)
    saved[slug] = info
    fetch_dir.mkdir(parents=True, exist_ok=True)
    (fetch_dir / METADATA_FILE).write_text(
        json.dumps(saved, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return info


# ---------- Website ----------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>George's CD Collection</title>
<style>
  :root {
    --bg: #f6f5f2; --card: #ffffff; --text: #1d1d1f; --muted: #6e6e73;
    --border: #e2e0db; --accent: #b4442f; --bar: #c8553d; --track: #eeece7;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141414; --card: #1f1f1f; --text: #f2f2f2; --muted: #a1a1a6;
      --border: #333333; --accent: #e8735c; --bar: #d9664e; --track: #2a2a2a;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  main { max-width: 1200px; margin: 0 auto; padding: 32px 16px 64px; }
  h1 { margin: 0 0 8px; font-size: 2rem; }
  h2 { font-size: 1rem; margin: 0 0 12px; }
  .intro { color: var(--muted); margin: 0 0 20px; }

  .stats { display: grid; gap: 12px; margin-bottom: 16px; grid-template-columns: repeat(4, 1fr); }
  @media (max-width: 700px) { .stats { grid-template-columns: repeat(2, 1fr); } }
  .stat { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 14px; }
  .stat strong { display: block; font-size: 1.2rem; overflow-wrap: anywhere; }
  .stat span { color: var(--muted); font-size: 0.85rem; }

  details.panel { background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; margin-bottom: 24px; }
  details.panel summary { cursor: pointer; padding: 12px 16px; font-weight: 600; }
  .charts { display: grid; gap: 28px; padding: 4px 16px 20px;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
  .bars { display: flex; flex-direction: column; gap: 2px; }
  .bar { display: grid; grid-template-columns: 7.5rem 1fr 2rem; align-items: center;
    gap: 8px; padding: 3px 4px; border: 0; border-radius: 4px; background: none;
    color: var(--text); font: inherit; font-size: 0.85rem; text-align: left; cursor: pointer; }
  .bar:hover, .bar:focus-visible { background: var(--track); outline: none; }
  .bar .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar .track { height: 12px; }
  .bar .fill { height: 100%; background: var(--bar); border-radius: 0 4px 4px 0; min-width: 2px; }
  .bar .value { color: var(--muted); text-align: right; font-variant-numeric: tabular-nums; }
  .bar.active .label { font-weight: 700; }
  .chart-note { color: var(--muted); font-size: 0.8rem; margin: 8px 0 0; }

  .controls { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
  .controls input, .controls select, .controls button {
    padding: 9px 12px; font-size: 0.95rem; border: 1px solid var(--border);
    border-radius: 8px; background: var(--card); color: var(--text); font-family: inherit;
  }
  .controls input { flex: 1 1 240px; min-width: 0; }
  .controls select { flex: 0 1 auto; max-width: 100%; }
  .controls button { cursor: pointer; }
  .controls button:hover { border-color: var(--accent); }
  @media (max-width: 700px) {
    .controls { display: grid; grid-template-columns: 1fr 1fr; }
    .controls input { grid-column: 1 / -1; }
    .controls select, .controls button { width: 100%; }
  }
  .status { color: var(--muted); font-size: 0.9rem; margin: 0 0 20px; }
  .status button { border: 0; background: none; color: var(--accent); cursor: pointer;
    font: inherit; padding: 0; margin-left: 8px; text-decoration: underline; }

  .grid { display: grid; gap: 20px;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .album { margin: 0; border-radius: 8px; transition: transform 0.15s; }
  .album a { display: block; }
  .album img, .album .placeholder {
    width: 100%; aspect-ratio: 1; object-fit: cover; display: block;
    border-radius: 6px; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.18); background: var(--border);
  }
  .album a:hover img { transform: translateY(-3px); transition: transform 0.15s; }
  .album .placeholder { display: flex; align-items: center; justify-content: center;
    padding: 12px; text-align: center; color: var(--muted); font-size: 0.9rem; }
  .album figcaption { margin-top: 8px; line-height: 1.3; }
  .album .title { font-weight: 600; display: block; }
  .album .artist { color: var(--muted); font-size: 0.9rem; display: block; }
  .album .meta { font-size: 0.8rem; color: var(--muted); }
  .album .year { color: var(--accent); }
  .album.highlight { outline: 3px solid var(--accent); outline-offset: 6px; transform: scale(1.04); }
  .empty { color: var(--muted); display: none; }
</style>
</head>
<body>
<main>
  <h1>George's CD Collection</h1>
  <p class="intro">{{COUNT}} albums &middot; last updated {{UPDATED}}</p>
  <div class="stats">{{STATS}}</div>

  <details class="panel">
    <summary>Collection stats</summary>
    <div class="charts">
      <section><h2>Albums by decade</h2><div class="bars">{{DECADE_BARS}}</div></section>
      <section><h2>Albums by genre</h2><div class="bars">{{GENRE_BARS}}</div></section>
      <section><h2>Most collected artists</h2><div class="bars">{{ARTIST_BARS}}</div>
        <p class="chart-note">Click a bar to filter the albums.</p></section>
    </div>
  </details>

  <div class="controls">
    <input id="search" type="search" placeholder="Search artist, album, genre or year..." aria-label="Search">
    <select id="decade" aria-label="Decade"><option value="">All decades</option>{{DECADE_OPTIONS}}</select>
    <select id="genre" aria-label="Genre"><option value="">All genres</option>{{GENRE_OPTIONS}}</select>
    <select id="sort" aria-label="Sort by">
      <option value="artist">Sort: Artist A-Z</option>
      <option value="newest">Sort: Newest release</option>
      <option value="oldest">Sort: Oldest release</option>
      <option value="added">Sort: Recently added</option>
      <option value="title">Sort: Album title</option>
    </select>
    <button id="random" type="button">Pick a random album</button>
  </div>
  <p class="status"><span id="showing"></span><button id="clear" type="button" hidden>Clear filters</button></p>

  <div class="grid" id="grid">
{{ALBUMS}}
  </div>
  <p class="empty" id="empty">No albums match your filters.</p>
</main>
<script>
  const grid = document.getElementById("grid");
  const albums = Array.from(grid.querySelectorAll(".album"));
  const search = document.getElementById("search");
  const decade = document.getElementById("decade");
  const genre = document.getElementById("genre");
  const sort = document.getElementById("sort");
  const clear = document.getElementById("clear");

  function applyFilters() {
    const query = search.value.trim().toLowerCase();
    let shown = 0;
    albums.forEach(album => {
      const match = album.dataset.search.includes(query)
        && (!decade.value || album.dataset.decade === decade.value)
        && (!genre.value || album.dataset.genre === genre.value);
      album.hidden = !match;
      if (match) shown++;
    });
    document.getElementById("showing").textContent =
      shown === albums.length ? `Showing all ${shown} albums` : `Showing ${shown} of ${albums.length} albums`;
    document.getElementById("empty").style.display = shown ? "none" : "block";
    clear.hidden = !(query || decade.value || genre.value);
    document.querySelectorAll(".bar").forEach(bar => {
      const select = bar.dataset.filter === "decade" ? decade : genre;
      bar.classList.toggle("active", bar.dataset.filter !== "artist" && select.value === bar.dataset.value);
    });
  }

  function applySort() {
    const compare = {
      artist: (a, b) => a.dataset.order - b.dataset.order,
      newest: (a, b) => b.dataset.year - a.dataset.year || a.dataset.order - b.dataset.order,
      oldest: (a, b) => a.dataset.year - b.dataset.year || a.dataset.order - b.dataset.order,
      added: (a, b) => b.dataset.added - a.dataset.added,
      title: (a, b) => a.dataset.title.localeCompare(b.dataset.title),
    }[sort.value];
    albums.slice().sort(compare).forEach(album => grid.appendChild(album));
  }

  document.querySelectorAll(".bar").forEach(bar => bar.addEventListener("click", () => {
    if (bar.dataset.filter === "artist") {
      search.value = bar.dataset.value;
    } else {
      const select = bar.dataset.filter === "decade" ? decade : genre;
      select.value = select.value === bar.dataset.value ? "" : bar.dataset.value;
    }
    applyFilters();
    grid.scrollIntoView({ behavior: "smooth", block: "start" });
  }));

  document.getElementById("random").addEventListener("click", () => {
    const visible = albums.filter(album => !album.hidden);
    if (!visible.length) return;
    albums.forEach(album => album.classList.remove("highlight"));
    const pick = visible[Math.floor(Math.random() * visible.length)];
    pick.classList.add("highlight");
    pick.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => pick.classList.remove("highlight"), 2500);
  });

  clear.addEventListener("click", () => {
    search.value = decade.value = genre.value = "";
    applyFilters();
  });
  search.addEventListener("input", applyFilters);
  decade.addEventListener("change", applyFilters);
  genre.addEventListener("change", applyFilters);
  sort.addEventListener("change", applySort);
  applyFilters();
</script>
</body>
</html>
"""

ALBUM_TEMPLATE = """    <figure class="album" data-search="{search}" data-decade="{decade}" data-genre="{genre}"
            data-year="{year_number}" data-order="{order}" data-added="{added}" data-title="{title_sort}">
      {image}
      <figcaption>
        <span class="title">{album}</span>
        <span class="artist">{artist}</span>
        <span class="meta"><span class="year">{year}</span>{genre_label}</span>
      </figcaption>
    </figure>"""

UNKNOWN_GENRE = "Unknown"

# iTunes and MusicBrainz name genres differently ("Alternative", "indie rock",
# "Prog-Rock/Art Rock"...), so they are grouped into broad genres. Checked in order.
GENRE_GROUPS = [
    ("Soundtrack", ["soundtrack", "musical", "show tunes"]),
    ("Hip-Hop/Rap", ["hip hop", "hip-hop", "rap", "trap", "grime", "drill", "boom bap", "afroswing"]),
    ("R&B/Soul", ["r&b", "rnb", "soul", "funk", "motown"]),
    ("Jazz", ["jazz", "swing", "vocal"]),
    ("Alternative/Indie", ["alternative", "indie", "post-punk", "shoegaze", "dream pop",
                           "new wave", "experimental"]),
    ("Folk", ["folk", "singer/songwriter", "singer-songwriter", "country"]),
    ("Electronic", ["electronic", "electro", "house", "techno", "dance", "synthwave"]),
    ("Rock", ["rock", "punk", "metal", "grunge", "britpop"]),
    ("Pop", ["pop"]),
]


def genre_group(genre):
    """Broad genre for a raw genre name; names that match nothing are kept as they are."""
    if not genre:
        return UNKNOWN_GENRE
    lowered = genre.lower()
    for group, keywords in GENRE_GROUPS:
        if any(keyword in lowered for keyword in keywords):
            return group
    return genre.strip().title()
MAX_GENRE_BARS = 8


def decade_of(entry):
    return f"{entry['year'][:3]}0s" if entry["year"].isdigit() else ""


def esc(value):
    return html.escape(str(value), quote=True)


def build_bars(counts, filter_name, total_label=None):
    """Horizontal bar list; each bar is a button that filters the albums."""
    if not counts:
        return ""
    biggest = max(count for _, count in counts)
    bars = []
    for label, count in counts:
        clickable = label != "Other"
        bars.append(
            f'<button type="button" class="bar" data-filter="{filter_name}" data-value="{esc(label)}"'
            f' title="{esc(label)}: {count} album{"s" if count != 1 else ""}"'
            f'{"" if clickable else " disabled"}>'
            f'<span class="label">{esc(label)}</span>'
            f'<span class="track"><span class="fill" style="width:{count / biggest * 100:.1f}%;display:block"></span></span>'
            f'<span class="value">{count}</span></button>')
    return "".join(bars)


def build_options(counts):
    return "".join(f'<option value="{esc(label)}">{esc(label)} ({count})</option>'
                   for label, count in counts)


def build_stats(albums):
    artists = Counter(a["artist"] for a in albums)
    decades = Counter(decade_of(a) for a in albums if decade_of(a))
    genres = Counter(a["genre"] for a in albums if a["genre"] != UNKNOWN_GENRE)
    top_artist, top_count = artists.most_common(1)[0]
    top_decade, decade_count = decades.most_common(1)[0]
    dated = [a for a in albums if a["year"].isdigit()]
    oldest = min(dated, key=lambda a: int(a["year"]))
    latest = max(albums, key=lambda a: a["added"])
    stats = [
        (len(albums), "albums"),
        (len(artists), "artists"),
        (len(genres), "genres"),
        (top_artist, f"most collected ({top_count} albums)"),
        (top_decade, f"favourite decade ({decade_count} albums)"),
        (oldest["year"], f"oldest: {oldest['album']}"),
        (latest["album"], f"latest addition, {latest['artist']}"),
    ]
    if genres:
        top_genre, genre_count = genres.most_common(1)[0]
        stats.insert(3, (top_genre, f"top genre ({genre_count} albums)"))
    return "".join(f'<div class="stat"><strong>{esc(value)}</strong><span>{esc(label)}</span></div>'
                   for value, label in stats)


def write_site(albums, covers, output_dir):
    if output_dir.exists():
        shutil.rmtree(output_dir)
    (output_dir / "covers").mkdir(parents=True)

    cards = []
    for order, entry in enumerate(albums):
        cover = covers.get(slugify(entry))
        alt = esc(f"{entry['album']} by {entry['artist']}")
        if cover:
            shutil.copy(cover, output_dir / "covers" / cover.name)
            image = f'<img src="covers/{cover.name}" alt="{alt}" loading="lazy">'
        else:
            image = f'<div class="placeholder">{esc(entry["album"])}</div>'
        if entry.get("link"):
            image = (f'<a href="{esc(entry["link"])}" target="_blank" rel="noopener" '
                     f'title="Listen on Apple Music">{image}</a>')
        genre = entry["genre"]
        search = " ".join([entry["artist"], entry["album"], entry["year"], genre]).lower()
        cards.append(ALBUM_TEMPLATE.format(
            search=esc(search),
            decade=esc(decade_of(entry)),
            genre=esc(genre),
            year_number=entry["year"] if entry["year"].isdigit() else 0,
            order=order,
            added=entry["added"],
            title_sort=esc(normalise(entry["album"]) or entry["album"]),
            image=image,
            album=esc(entry["album"]),
            artist=esc(entry["artist"]),
            year=esc(entry["year"]),
            genre_label="" if genre == UNKNOWN_GENRE else f" &middot; {esc(genre)}",
        ))

    decades = sorted(Counter(decade_of(a) for a in albums if decade_of(a)).items())
    genres = Counter(a["genre"] for a in albums).most_common()
    if len(genres) > MAX_GENRE_BARS + 1:  # never fold a single genre into "Other"
        top_genres = genres[:MAX_GENRE_BARS]
        top_genres.append(("Other", sum(count for _, count in genres[MAX_GENRE_BARS:])))
    else:
        top_genres = genres
    artists = Counter(a["artist"] for a in albums).most_common()
    cutoff = artists[min(7, len(artists) - 1)][1]  # top 8, plus anyone tied with 8th
    artists = [(artist, count) for artist, count in artists if count >= max(cutoff, 2)]

    page = PAGE_TEMPLATE
    for key, value in {
        "COUNT": str(len(albums)),
        "UPDATED": datetime.today().strftime("%d/%m/%Y"),
        "STATS": build_stats(albums),
        "DECADE_BARS": build_bars(decades, "decade"),
        "GENRE_BARS": build_bars(top_genres, "genre"),
        "ARTIST_BARS": build_bars(artists, "artist"),
        "DECADE_OPTIONS": build_options(decades),
        "GENRE_OPTIONS": build_options(sorted(genres)),
        "ALBUMS": "\n".join(cards),
    }.items():
        page = page.replace("{{" + key + "}}", value)
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="collection.csv")
    parser.add_argument("--output", default="_site", type=Path,
                        help="folder the website is written to")
    parser.add_argument("--fetch-dir", default=COVERS_DIR, type=Path,
                        help="folder newly downloaded covers and metadata are saved to")
    args = parser.parse_args()

    albums = sorted(read_collection(args.input), key=sort_key)
    metadata = load_metadata(COVERS_DIR, args.fetch_dir)
    covers = {}
    for entry in albums:
        cover = get_cover(entry, args.fetch_dir)
        if cover:
            covers[slugify(entry)] = cover
        info = get_metadata(entry, metadata, args.fetch_dir)
        entry["genre"] = genre_group(info.get("genre"))
        entry["link"] = info.get("link")
    write_site(albums, covers, args.output)
    print(f"Built {args.output / 'index.html'}: {len(albums)} albums, "
          f"{len(covers)} with covers")


if __name__ == "__main__":
    main()
