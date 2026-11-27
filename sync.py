#!/usr/bin/python3
"""Mirror a YouTube playlist into an audio-only podcast feed for Overcast.

Every run:
  1. Ask Overcast which episodes are finished (played, or deleted by hand) and
     drop those videos from the YouTube playlist, so the playlist holds only
     what is still unheard.
  2. Read the playlist (it must be "unlisted" or public, so no login is needed).
  3. For each new video: download the audio, re-encode to mono AAC 64 kbps,
     upload it as an asset of a GitHub release.
  4. For each video removed from the playlist: delete its asset.
  5. Rebuild feed.xml and upload it to the same release.

Settings live in config.json next to this file. State lives in state/.
Run with --dry-run to build files locally and only report planned deletions.
Run with --auth-youtube once to grant write access to the playlist.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
# A fresh clone has no config.json yet. Falling back to the example keeps
# --check runnable, and --check is what says the values are still the example.
CONFIG_FILE = HERE / "config.json"
CONFIG_IS_EXAMPLE = not CONFIG_FILE.exists()
if CONFIG_IS_EXAMPLE:
    CONFIG_FILE = HERE / "config.json.example"
CONFIG = json.loads(CONFIG_FILE.read_text())
STATE_DIR = HERE / "state"
STATE_FILE = STATE_DIR / "episodes.json"
CLEANED_FILE = STATE_DIR / "cleaned.json"  # videos already dropped from the playlist
COOKIE_FILE = STATE_DIR / "overcast_cookie.txt"
OPML_NEXT_FILE = STATE_DIR / "overcast_next.txt"  # no OPML export before this time
CLEANUP_ALERT_FILE = STATE_DIR / "cleanup_failing.txt"  # set once we have warned
YT_CLIENT_FILE = STATE_DIR / "youtube_client.json"  # OAuth client from Google Cloud
YT_TOKEN_FILE = STATE_DIR / "youtube_token.json"  # refresh token, written by --auth-youtube
LOCAL_DIR = HERE / "out"  # used only with --dry-run

PLAYLIST = CONFIG["playlist_url"]
REPO = CONFIG.get("github_repo", "")  # "owner/name", only used when storage is "github"
TAG = CONFIG.get("release_tag", "audio")
FEED_TITLE = CONFIG.get("feed_title", "My Feed")
COVER = "cover.jpg"  # uploaded once with: python3 sync.py --rebuild

# Age-restricted videos need a signed-in session. Chrome 158 encrypts its cookie
# store with a scheme yt-dlp cannot read on macOS, so the cookies are asked for
# from Chrome itself (dump_cookies.py) out of a profile kept just for this.
CHROME_PROFILE = CONFIG.get("chrome_profile", "")
COOKIES_FILE = CONFIG.get("cookies_file", "")
DUMPER = HERE / "dump_cookies.py"
AGE_GATE = "confirm your age"

# yt-dlp needs the yt-dlp-ejs solver for YouTube's JS challenge; the Homebrew
# build ships one that crashes on current deno. .venv holds a working pair.
VENV_YT_DLP = HERE / ".venv" / "bin" / "yt-dlp"
YT_DLP = str(VENV_YT_DLP) if VENV_YT_DLP.exists() else "yt-dlp"

KEYCHAIN_SERVICE = "yt-overcast-overcast"  # holds the Overcast e-mail and password
S3_KEYCHAIN = "yt-overcast-s3"  # holds the access key id and secret

# Where the audio and feed live. "github" puts them in a release, which is
# public to anyone who finds the repo; "s3" puts them in any S3-compatible
# bucket (Cloudflare R2, Backblaze B2, AWS) behind an unguessable URL.
STORAGE = CONFIG.get("storage", "github")
S3_ENDPOINT = CONFIG.get("s3_endpoint", "")
S3_BUCKET = CONFIG.get("s3_bucket", "")
S3_PREFIX = CONFIG.get("s3_prefix", "")
PUBLIC_BASE = CONFIG.get("public_base_url", "").rstrip("/")
CONTENT_TYPES = {".m4a": "audio/mp4", ".xml": "application/rss+xml",
                 ".jpg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml"}
YT_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
UA = "youtube-overcast/1.0"

DRY_RUN = "--dry-run" in sys.argv
REBUILD = "--rebuild" in sys.argv  # re-upload cover.jpg and feed.xml even if nothing changed
AUTH_YOUTUBE = "--auth-youtube" in sys.argv
MIGRATE = "--migrate" in sys.argv  # copy everything from the GitHub release into the bucket
CHECK = "--check" in sys.argv  # report what is set up and what is still missing
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")


class AuthExpired(Exception):
    """Overcast or YouTube needs the credentials to be set up again."""


class OvercastWait(Exception):
    """Too early to ask Overcast again: on schedule, or after a rate limit."""


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def run(cmd, **kw):
    # Nothing here may ever wait for typing: a sync runs unattended, and a tool
    # like gh will happily sit on a prompt forever if it is given a terminal.
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout


def notify(msg):
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "YouTube → Overcast"'])


def github_url(name):
    return f"https://github.com/{REPO}/releases/download/{TAG}/{name}"


def asset_url(name):
    if STORAGE == "s3":
        return f"{PUBLIC_BASE}/{S3_PREFIX}{name}"
    return github_url(name)


_bucket = None


def bucket():
    """The S3-compatible bucket from config.json, with keys from the keychain."""
    global _bucket
    if _bucket is None:
        # launchd runs this with python -I, which keeps the script's own
        # directory off sys.path, so s3store has to be found deliberately.
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import s3store
        for name, value in (("s3_endpoint", S3_ENDPOINT), ("s3_bucket", S3_BUCKET),
                            ("public_base_url", PUBLIC_BASE)):
            if not value:
                raise AuthExpired(f"storage is \"s3\" but {name} is empty in config.json")
        _bucket = s3store.Bucket(S3_ENDPOINT, S3_BUCKET,
                                 keychain("account", S3_KEYCHAIN),
                                 keychain("password", S3_KEYCHAIN))
    return _bucket


def content_type(name):
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


FEED_URL = asset_url("feed.xml")


def fetch(url, data=None, headers=None, method=None, follow=True):
    """One HTTP request. Returns (status, headers, body); never raises on status."""
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": UA, **(headers or {})})
    handlers = [] if follow else [NoRedirect()]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=180) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# --- Overcast: which episodes are finished -----------------------------------

def keychain(field, service=None):
    """Read the account name or password of a login keychain item."""
    service = service or KEYCHAIN_SERVICE
    if field == "password":
        return run(["security", "find-generic-password", "-s", service, "-w"]).strip()
    p = subprocess.run(["security", "find-generic-password", "-s", service],
                       text=True, capture_output=True)
    m = re.search(r'"acct"<blob>="([^"]*)"', p.stdout)
    if not m:
        raise AuthExpired(f"no keychain item for service {service}")
    return m.group(1)


def overcast_login():
    status, headers, _ = fetch("https://overcast.fm/login?then=account",
                               data={"email": keychain("account"), "password": keychain("password")},
                               follow=False)
    for header in headers.get_all("Set-Cookie") or []:
        m = re.match(r"\s*o=([^;]+)", header)
        if m:
            COOKIE_FILE.write_text(m.group(1))
            COOKIE_FILE.chmod(0o600)
            return m.group(1)
    raise AuthExpired(f"Overcast rejected the login (HTTP {status})")


# Overcast exports the whole account at once and allows roughly 10 exports a
# day. Cleaning the YouTube playlist is not urgent, so twice a day is plenty.
OPML_EVERY_HOURS = 12
OPML_PAUSE_HOURS = 24  # after a 429, ask once a day until Overcast answers again
SLACK = 15 * 60  # a sync starts a few seconds late; don't let that skip a turn


def opml_wait(hours):
    OPML_NEXT_FILE.write_text(str(time.time() + hours * 3600 - SLACK))


def overcast_opml():
    """Fetch the extended OPML export, at most every OPML_EVERY_HOURS.

    A 429 pushes the next try OPML_PAUSE_HOURS away: asking again while
    Overcast is still counting only keeps the limit in place.
    """
    next_try = float(OPML_NEXT_FILE.read_text()) if OPML_NEXT_FILE.exists() else 0
    if time.time() < next_try:
        raise OvercastWait("next look at Overcast after "
                           + time.strftime("%d.%m %H:%M", time.localtime(next_try)))
    cookie = COOKIE_FILE.read_text().strip() if COOKIE_FILE.exists() else ""
    for attempt in (1, 2):
        if not cookie:
            cookie = overcast_login()
        status, _, body = fetch("https://overcast.fm/account/export_opml/extended",
                                headers={"Cookie": f"o={cookie}"}, follow=False)
        if status == 200 and body.lstrip().startswith(b"<?xml"):
            opml_wait(OPML_EVERY_HOURS)
            return body
        if status == 429:
            opml_wait(OPML_PAUSE_HOURS)
            raise OvercastWait(f"Overcast rate-limited the OPML export (HTTP 429); "
                               f"next try in {OPML_PAUSE_HOURS} hours")
        if attempt == 1:
            cookie = ""  # session died, or we were handed the login page
            continue
        raise AuthExpired(f"Overcast OPML export failed (HTTP {status})")


def overcast_finished():
    """Return {video_id: episode_title} for episodes played or deleted in Overcast."""
    root = ET.fromstring(overcast_opml())
    feeds = root.findall(".//outline[@type='rss']")
    ours = [f for f in feeds if f.get("xmlUrl") == FEED_URL]
    if not ours:  # Overcast may have stored a slightly different feed URL
        marker = asset_url("").split("://", 1)[-1]
        ours = [f for f in feeds
                if any(marker in (e.get("enclosureUrl") or "")
                       for e in f.findall("./outline[@type='podcast-episode']"))]
    if not ours:
        raise AuthExpired(f"Overcast has no feed {FEED_URL}")
    done = {}
    for feed in ours:
        for ep in feed.findall("./outline[@type='podcast-episode']"):
            if ep.get("played") != "1" and ep.get("userDeleted") != "1":
                continue
            m = re.search(r"/([A-Za-z0-9_-]{6,})\.m4a", ep.get("enclosureUrl") or "")
            if m:
                done[m.group(1)] = ep.get("title") or m.group(1)
    return done


# --- YouTube: drop finished videos from the playlist -------------------------

def playlist_id():
    ids = urllib.parse.parse_qs(urllib.parse.urlparse(PLAYLIST).query).get("list")
    if not ids:
        raise AuthExpired(f"playlist_url has no list= parameter: {PLAYLIST}")
    return ids[0]


def yt_client():
    data = json.loads(YT_CLIENT_FILE.read_text())
    c = data.get("installed") or data.get("web") or data
    return c["client_id"], c["client_secret"]


def yt_auth():
    """One-time consent flow. Saves a refresh token to state/youtube_token.json."""
    client_id, secret = yt_client()
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            got.update({k: v[0] for k, v in
                        urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Done. You can close this window.</h2>".encode())

        def log_message(self, *args):
            pass

    with TCPServer(("127.0.0.1", 0), Handler) as srv:
        srv.timeout = 5
        redirect_uri = f"http://127.0.0.1:{srv.server_address[1]}/"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
            {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": YT_SCOPE, "access_type": "offline", "prompt": "consent"})
        print("Open this and allow access to your playlists:\n\n" + url + "\n")
        webbrowser.open(url)
        deadline = time.time() + 300
        while not got and time.time() < deadline:
            srv.handle_request()
    if "code" not in got:
        raise AuthExpired(f"Google returned no code: {got or 'timed out'}")

    status, _, body = fetch("https://oauth2.googleapis.com/token",
                            data={"code": got["code"], "client_id": client_id,
                                  "client_secret": secret, "redirect_uri": redirect_uri,
                                  "grant_type": "authorization_code"})
    token = json.loads(body or b"{}")
    if "refresh_token" not in token:
        raise AuthExpired(f"no refresh token (HTTP {status}): {token}")
    STATE_DIR.mkdir(exist_ok=True)
    YT_TOKEN_FILE.write_text(json.dumps({"refresh_token": token["refresh_token"]}, indent=1))
    YT_TOKEN_FILE.chmod(0o600)
    log(f"YouTube access saved to {YT_TOKEN_FILE.name}")


def yt_token():
    client_id, secret = yt_client()
    refresh = json.loads(YT_TOKEN_FILE.read_text())["refresh_token"]
    status, _, body = fetch("https://oauth2.googleapis.com/token",
                            data={"client_id": client_id, "client_secret": secret,
                                  "refresh_token": refresh, "grant_type": "refresh_token"})
    token = json.loads(body or b"{}")
    if "access_token" not in token:
        raise AuthExpired(f"YouTube refused a token (HTTP {status}): "
                          f"{token.get('error_description') or token.get('error')}")
    return token["access_token"]


def yt_playlist_items(token):
    """Return {video_id: playlist_item_id} for the whole playlist."""
    items, page = {}, None
    while True:
        query = {"part": "snippet", "playlistId": playlist_id(), "maxResults": "50"}
        if page:
            query["pageToken"] = page
        status, _, body = fetch(
            "https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(query),
            headers={"Authorization": f"Bearer {token}"})
        data = json.loads(body or b"{}")
        if status != 200:
            raise AuthExpired(f"YouTube would not list the playlist (HTTP {status}): {data}")
        for item in data.get("items", []):
            vid = item["snippet"]["resourceId"].get("videoId")
            if vid:
                items[vid] = item["id"]
        page = data.get("nextPageToken")
        if not page:
            return items


def yt_delete(token, item_id):
    status, _, body = fetch(
        "https://www.googleapis.com/youtube/v3/playlistItems?id=" + urllib.parse.quote(item_id),
        headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    if status not in (200, 204, 404):  # 404: already gone, nothing to do
        raise AuthExpired(f"YouTube would not delete the item (HTTP {status}): {body[:300]!r}")


def missing_setup():
    missing = []
    if not COOKIE_FILE.exists():
        try:
            keychain("account")
        except (AuthExpired, subprocess.CalledProcessError):
            missing.append(f"Overcast password in Keychain (service {KEYCHAIN_SERVICE})")
    if not YT_CLIENT_FILE.exists():
        missing.append(f"Google Cloud OAuth client at state/{YT_CLIENT_FILE.name}")
    elif not YT_TOKEN_FILE.exists():
        missing.append("python3 sync.py --auth-youtube")
    return missing


def clean_playlist():
    """Drop videos the user has finished in Overcast. Returns the removed ids."""
    missing = missing_setup()
    if missing:
        log("playlist cleanup off, not configured: " + "; ".join(missing))
        return set()

    cleaned = set(json.loads(CLEANED_FILE.read_text())) if CLEANED_FILE.exists() else set()
    todo = {v: t for v, t in overcast_finished().items() if v not in cleaned}
    if not todo:
        return set()
    if DRY_RUN:
        for vid, title in todo.items():
            log(f"would remove from playlist: {title} ({vid})")
        return set()

    token = yt_token()
    items = yt_playlist_items(token)
    removed = set()
    for vid, title in todo.items():
        if vid in items:
            yt_delete(token, items[vid])
            log(f"finished in Overcast, removed from playlist: {title}")
            removed.add(vid)
        cleaned.add(vid)  # remember it even if it had already left the playlist
        CLEANED_FILE.write_text(json.dumps(sorted(cleaned), indent=1))
    if removed:
        notify(f"Listened, so removed from the playlist: {len(removed)}")
    return removed


# --- the feed ----------------------------------------------------------------

def playlist_ids():
    out = run([YT_DLP, "--flat-playlist", "-J", PLAYLIST])
    return [e["id"] for e in json.loads(out)["entries"] if e and e.get("id")]


def under(path):
    """Resolve a config path against the script's own directory."""
    path = Path(path)
    return path if path.is_absolute() else HERE / path


def refresh_cookies():
    """Write a fresh cookie file out of Chrome. Returns yt-dlp arguments, or []."""
    if not (CHROME_PROFILE and COOKIES_FILE):
        return []
    profile, target = under(CHROME_PROFILE), under(COOKIES_FILE)
    if not profile.exists():
        log(f"no Chrome profile at {profile}, downloading signed out")
        return []
    try:
        log(run([sys.executable, "-I", str(DUMPER), str(profile), str(target)]).strip())
    except subprocess.CalledProcessError as e:
        log(f"could not read cookies from Chrome: {(e.stderr or '').strip()[-300:]}")
        return []
    return ["--cookies", str(target)] if target.exists() else []


def fetch_with_cookies(vid, workdir):
    """Download a video, signing in only if YouTube asks to confirm age.

    Signing in is a fallback rather than the default: cookies make YouTube
    demand a solved JS challenge, so they must not be spent on videos that
    download fine without them.
    """
    try:
        return fetch_episode(vid, workdir)
    except subprocess.CalledProcessError as e:
        if AGE_GATE not in (e.stderr or ""):
            raise
        cookies = refresh_cookies()
        if not cookies:
            raise
        log(f"age-restricted, retrying signed in: {vid}")
        try:
            return fetch_episode(vid, workdir, cookies)
        except subprocess.CalledProcessError as again:
            if AGE_GATE in (again.stderr or ""):
                notify("YouTube sign-in expired. Sign in again in state/chrome-profile.")
            raise


def fetch_episode(vid, workdir, cookies=()):
    url = f"https://www.youtube.com/watch?v={vid}"
    info = json.loads(run([YT_DLP, "-J", "--no-playlist", *cookies, url]))
    raw = Path(workdir) / f"{vid}.raw.m4a"
    final = Path(workdir) / f"{vid}.m4a"
    run([YT_DLP, "--no-playlist", *cookies,
         "-f", "bestaudio[ext=m4a]/bestaudio", "-o", str(raw), url])
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
         "-map", "0:a", "-map_metadata", "0",
         "-metadata", f"title={info.get('title', vid)}",
         "-metadata", f"artist={info.get('uploader', '')}",
         "-c:a", "aac", "-ac", "1", "-b:a", "64k", "-f", "mp4", str(final)])
    episode = {
        "id": vid,
        "title": info.get("title", vid),
        "channel": info.get("uploader", ""),
        "description": (info.get("description") or "")[:4000],
        "duration": int(info.get("duration") or 0),
        "image": info.get("thumbnail", ""),
        "link": url,
        "added": time.time(),
        "file": final.name,
        "size": final.stat().st_size,
    }
    return episode, final


def upload(path):
    if DRY_RUN:
        LOCAL_DIR.mkdir(exist_ok=True)
        Path(path).replace(LOCAL_DIR / Path(path).name)
        return
    if STORAGE == "s3":
        name = Path(path).name
        bucket().put(S3_PREFIX + name, Path(path), content_type(name))
        return
    if not REPO:
        raise AuthExpired('storage is "github" but github_repo is empty in config.json')
    run(["gh", "release", "upload", TAG, str(path), "--clobber", "--repo", REPO])


def upload_copy(path):
    """Upload a file that must stay in place (upload() moves files in --dry-run)."""
    tmp = Path(tempfile.gettempdir()) / Path(path).name
    tmp.write_bytes(Path(path).read_bytes())
    upload(tmp)


def delete_asset(name):
    if DRY_RUN:
        (LOCAL_DIR / name).unlink(missing_ok=True)
        return
    if STORAGE == "s3":
        try:
            bucket().delete(S3_PREFIX + name)
        except RuntimeError as e:
            log(f"could not delete {name}: {e}")
        return
    subprocess.run(["gh", "release", "delete-asset", TAG, name, "--yes", "--repo", REPO],
                   capture_output=True, text=True)


def build_feed(episodes):
    items = []
    for ep in sorted(episodes, key=lambda e: e["added"], reverse=True):
        h, m, s = ep["duration"] // 3600, ep["duration"] % 3600 // 60, ep["duration"] % 60
        items.append(f"""    <item>
      <title>{escape(ep['title'])}</title>
      <itunes:author>{escape(ep['channel'])}</itunes:author>
      <description>{escape(ep['channel'] + chr(10) + ep['link'] + chr(10) + chr(10) + ep['description'])}</description>
      <link>{escape(ep['link'])}</link>
      <guid isPermaLink="false">yt-{ep['id']}</guid>
      <pubDate>{formatdate(ep['added'])}</pubDate>
      <enclosure url="{escape(asset_url(ep['file']))}" length="{ep['size']}" type="audio/mp4"/>
      <itunes:duration>{h:02d}:{m:02d}:{s:02d}</itunes:duration>
      <itunes:image href="{escape(ep['image'])}"/>
    </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(FEED_TITLE)}</title>
    <link>{escape(PLAYLIST)}</link>
    <description>Audio from a personal YouTube playlist.</description>
    <language>ru</language>
    <itunes:block>Yes</itunes:block>
    <itunes:image href="{escape(asset_url(COVER))}"/>
    <image><url>{escape(asset_url(COVER))}</url><title>{escape(FEED_TITLE)}</title><link>{escape(PLAYLIST)}</link></image>
{chr(10).join(items)}
  </channel>
</rss>
"""


def migrate():
    """Copy the cover and every episode from the GitHub release into the bucket."""
    if STORAGE != "s3":
        raise AuthExpired('set "storage": "s3" in config.json before migrating')
    episodes = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    names = [COVER] + [e["file"] for e in episodes]
    moved = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in names:
            status, _, body = fetch(github_url(name))
            if status != 200 or not body:
                log(f"skipped {name}: GitHub answered HTTP {status}")
                continue
            local = Path(tmp) / name
            local.write_bytes(body)
            bucket().put(S3_PREFIX + name, local, content_type(name))
            log(f"moved {name} ({len(body) // 1_000_000} MB)")
            moved += 1
    feed = Path(tempfile.gettempdir()) / "feed.xml"
    feed.write_text(build_feed(episodes))
    upload(feed)
    log(f"moved {moved} files; the feed now points at {PUBLIC_BASE}")
    return 0


def check():
    """Report what is set up and what is not, with the fix for each gap."""
    results = []
    section = [""]

    def test(name, fn, fix="", optional=False):
        try:
            results.append((True, section[0], name, str(fn() or "ok"), "", optional))
        except Exception as e:
            results.append((False, section[0], name, why(e), fix, optional))

    def tool(binary, *args):
        return run([binary, *args]).strip().splitlines()[0][:58]

    section[0] = "Tools"
    test("yt-dlp", lambda: f"{tool(YT_DLP, '--version')}  ({'.venv' if YT_DLP != 'yt-dlp' else 'PATH'})",
         "python3 -m venv .venv && .venv/bin/pip install yt-dlp yt-dlp-ejs")
    test("yt-dlp-ejs", lambda: "installed" if list((HERE / ".venv").rglob("yt_dlp_ejs"))
         else fail("missing, so a signed-in download cannot solve YouTube's JS challenge"),
         ".venv/bin/pip install yt-dlp-ejs")
    test("ffmpeg", lambda: tool("ffmpeg", "-version"), "brew install ffmpeg")
    test("deno", lambda: tool("deno", "--version"), "brew install deno")
    if STORAGE == "github":
        test("gh", lambda: tool("gh", "auth", "status"), "brew install gh && gh auth login")

    section[0] = "Config"
    test("config.json", lambda: fail("missing, running on config.json.example")
         if CONFIG_IS_EXAMPLE else "yours",
         "cp config.json.example config.json, then edit it (or run setup.py)")
    test("playlist", lambda: real(playlist_id(), "playlist_url"),
         'put your own playlist URL in "playlist_url"')
    test("storage", lambda: STORAGE if STORAGE in ("github", "s3")
         else fail(f'unknown storage "{STORAGE}"'), 'set "storage" to "github" or "s3"')
    if STORAGE == "github":
        test("github_repo", lambda: real(REPO, "github_repo"),
             'set "github_repo" to your own owner/name')
        test("release reachable", lambda: str(len(json.loads(run(
            ["gh", "release", "view", TAG, "--repo", REPO, "--json", "assets"]))["assets"]))
            + " files", f"gh release create {TAG} --repo {REPO} --notes ''")
    else:
        test("bucket settings", lambda: real(S3_BUCKET, "s3_bucket")
             and real(S3_ENDPOINT, "s3_endpoint") and real(PUBLIC_BASE, "public_base_url"),
             "fill the s3_* keys in config.json with your own bucket")

    section[0] = "Credentials"
    test("Overcast password", lambda: mask(keychain("account")),
         f"security add-generic-password -s {KEYCHAIN_SERVICE} -a YOUR_EMAIL -A -w")
    test("YouTube access", lambda: f"token refreshes ({len(youtube_ready())} chars)",
         "save the OAuth client as state/youtube_client.json, then run: python3 sync.py --auth-youtube")
    if STORAGE == "s3":
        test("bucket keys", lambda: mask(keychain("account", S3_KEYCHAIN)),
             f"security add-generic-password -s {S3_KEYCHAIN} -a YOUR_ACCESS_KEY_ID -A -w")
        test("bucket reachable", lambda: f"{len(bucket().list())} objects")
        test("feed served publicly", lambda: http_head(FEED_URL),
             "run: python3 sync.py --rebuild")
    test("cover art uploaded", lambda: http_head(asset_url(COVER)),
         "run: python3 sync.py --rebuild")
    test("age-gate sign-in", lambda: "Chrome profile ready" if CHROME_PROFILE
         and under(CHROME_PROFILE).exists() else fail("no Chrome profile, age-restricted videos will fail"),
         'open -na "Google Chrome" --args '
         f'--user-data-dir=$PWD/{CHROME_PROFILE or "state/chrome-profile"} https://www.youtube.com',
         optional=True)

    section[0] = "Runtime"
    test("state directory", lambda: "writable" if os.access(STATE_DIR, os.W_OK)
         else fail("not writable"))
    test("scheduled agent", agent_status,
         'sed "s|__DIR__|$PWD|g" youtube-overcast.plist.example > ~/Library/LaunchAgents/youtube-overcast.plist'
         " && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/youtube-overcast.plist",
         optional=True)

    shown = ""
    for ok, group, name, detail, _fix, optional in results:
        if group != shown:
            print(f"\n{group}")
            shown = group
        mark = " ok " if ok else ("    " if optional else "MISS")
        print(f"  {mark}  {name:<22} {detail}")

    gaps = [(n, f) for ok, _g, n, _d, f, _o in results if not ok and f]
    if gaps:
        print("\nTo fix")
        for name, fix in gaps:
            print(f"  {name}:\n    {fix}")
    missing = [n for ok, _g, n, _d, _f, opt in results if not ok and not opt]
    print(f"\n{len(results) - len(missing)} of {len(results)} ready"
          + (f". Still needed: {', '.join(missing)}" if missing else ". Everything is set up."))
    if not missing:
        print(f"\nAdd this to your podcast app as a custom feed:\n  {FEED_URL}")
    return 1 if missing else 0


def fail(message):
    raise RuntimeError(message)


def why(error):
    """One readable line out of whatever a check raised."""
    if isinstance(error, subprocess.CalledProcessError):
        text = (error.stderr or error.stdout or "").strip().splitlines()
        return (text[-1] if text else f"{error.cmd[0]} failed")[:80]
    if isinstance(error, FileNotFoundError):
        return f"{error.filename or 'command'} not found"
    return (str(error) or type(error).__name__)[:80]


def mask(value):
    """Shorten an identifier so the check output can be pasted anywhere."""
    if "@" in value:
        name, _, domain = value.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"{value[:6]}...{value[-4:]}" if len(value) > 14 else value


def stored(name):
    """Is this file already in the feed's storage? Never raises, so a run that
    cannot tell simply does not re-upload."""
    try:
        http_head(asset_url(name))
        return True
    except Exception:
        return False


def http_head(url):
    request = urllib.request.Request(url, method="HEAD")
    request.add_header("User-Agent", UA)
    with urllib.request.urlopen(request, timeout=30) as response:
        return f"HTTP {response.status}, {response.headers.get('Content-Type')}"


def agent_status():
    """An agent counts only if it runs this copy of sync.py, not another one."""
    mine = str(HERE / "sync.py")
    agents = Path.home() / "Library" / "LaunchAgents"
    labels = [f.stem for f in agents.glob("*.plist")
              if mine in f.read_text(errors="replace")] if agents.exists() else []
    if not labels:
        return fail(f"no launchd agent runs {mine}")
    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    for label in labels:
        if any(line.endswith(label) for line in listed.splitlines()):
            return f"{label}, loaded"
    return fail(f"{labels[0]} is installed but not loaded")


def real(value, key):
    """Reject the untouched values from config.json.example."""
    if not value:
        fail(f"{key} is empty")
    if any(m in str(value).lower() for m in ("xxxx", "yourname", "your-", "<", "example.com")):
        fail(f"{key} is still the example value")
    return value


def youtube_ready():
    if not YT_CLIENT_FILE.exists():
        fail(f"no {YT_CLIENT_FILE.name}, download the OAuth client from Google Cloud")
    if not YT_TOKEN_FILE.exists():
        fail(f"no {YT_TOKEN_FILE.name}, access was never granted")
    return yt_token()


def main():
    STATE_DIR.mkdir(exist_ok=True)
    if AUTH_YOUTUBE:
        yt_auth()
        return 0
    if CHECK:
        return check()
    if MIGRATE:
        return migrate()

    episodes = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    known = {e["id"] for e in episodes}

    # A failing cleanup must never block the downloads.
    # Waiting for Overcast is normal, so it only goes to the log. A real failure
    # warns once, not on every run, until a cleanup works again.
    removed = set()
    try:
        removed = clean_playlist()
        CLEANUP_ALERT_FILE.unlink(missing_ok=True)
    except OvercastWait as e:
        log(f"playlist cleanup: {e}")
    except Exception as e:  # noqa: BLE001 - the feed matters more than the cleanup
        if isinstance(e, AuthExpired):
            log(f"playlist cleanup failed: {e}")
        else:
            log(f"playlist cleanup crashed: {type(e).__name__}: {e}")
        if not CLEANUP_ALERT_FILE.exists():
            CLEANUP_ALERT_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            notify("Cannot clean the YouTube playlist. See state/sync.log")

    wanted = [v for v in playlist_ids() if v not in removed]
    log(f"playlist: {len(wanted)} videos, feed: {len(episodes)} episodes")
    changed = False

    for ep in [e for e in episodes if e["id"] not in wanted]:
        log(f"removed from playlist: {ep['title']}")
        delete_asset(ep["file"])
        episodes.remove(ep)
        changed = True

    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for vid in [v for v in wanted if v not in known]:
            try:
                ep, path = fetch_with_cookies(vid, tmp)
                upload(path)
                episodes.append(ep)
                STATE_FILE.write_text(json.dumps(episodes, ensure_ascii=False, indent=1))
                log(f"added: {ep['title']} ({ep['size'] // 1_000_000} MB)")
                changed = True
            except subprocess.CalledProcessError as e:
                log(f"FAILED {vid}: {(e.stderr or '').strip()[-500:]}")
                failures.append(vid)

    if REBUILD or (changed and not stored(COVER)):
        upload_copy(HERE / COVER)
    if changed or REBUILD:
        STATE_FILE.write_text(json.dumps(episodes, ensure_ascii=False, indent=1))
        feed = Path(tempfile.gettempdir()) / "feed.xml"
        feed.write_text(build_feed(episodes))
        upload(feed)
        log("feed updated")

    if failures:
        notify(f"Could not download {len(failures)} video(s). See state/sync.log")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AuthExpired as e:
        log(f"FAILED: {e}")
        notify("Credentials need renewing. See state/sync.log")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        log(f"FAILED: {' '.join(e.cmd[:3])}: {(e.stderr or '').strip()[-500:]}")
        notify("The sync crashed. See state/sync.log")
        sys.exit(1)
