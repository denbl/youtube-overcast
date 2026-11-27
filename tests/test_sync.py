import atexit, json, shutil, sys, tempfile, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="yt-overcast-tests-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
sys.path.insert(0, str(ROOT))
import sync

ORIGINAL_MISSING = sync.missing_setup
sync.CLEANED_FILE = SCRATCH / "cleaned.json"
sync.CLEANED_FILE.unlink(missing_ok=True)
ok = fail = 0

def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1; print(f"  ok   {name}")
    else:
        fail += 1; print(f"  FAIL {name}\n       got  {got!r}\n       want {want!r}")

OURS = "https://github.com/denbl/yt-overcast-feed/releases/download/audio"

def opml(xml_url, eps, base=OURS):
    rows = "".join(
        f'<outline type="podcast-episode" overcastId="{i}" title="{t}" '
        f'enclosureUrl="{base}/{v}.m4a" played="{p}" userDeleted="{d}" progress="{pr}" />'
        for i, (v, t, p, d, pr) in enumerate(eps))
    return (f'<?xml version="1.0" encoding="UTF-8"?><opml version="1.0"><body>'
            f'<outline text="feeds"><outline type="rss" overcastId="5975858" title="Morning Runs" '
            f'xmlUrl="{xml_url}" subscribed="1">{rows}</outline>'
            f'<outline type="rss" overcastId="42" title="Someone else" xmlUrl="https://other.example/f.xml">'
            f'<outline type="podcast-episode" overcastId="99" title="Not mine" '
            f'enclosureUrl="https://other.example/zzzzzzzzzzz.m4a" played="1" /></outline>'
            f'</outline></body></opml>').encode()

EPS = [("x8A-laOF4nA", "Big numbers", "1", "0", "4100"),   # played
       ("OwR5eard0Ek", "Stem cells", "0", "1", "600"),  # deleted by hand
       ("ONmgDCCsmOY", "Education", "0", "0", "300"),       # started, keep
       ("IsdVx5U1Y9g", "New York", "0", "0", "0")]            # untouched, keep

print("overcast_finished")
sync.overcast_opml = lambda: opml(sync.FEED_URL, EPS)
check("feed matched by xmlUrl", sync.overcast_finished(),
      {"x8A-laOF4nA": "Big numbers", "OwR5eard0Ek": "Stem cells"})
check("other podcasts ignored", "zzzzzzzzzzz" in sync.overcast_finished(), False)

sync.overcast_opml = lambda: opml(f"{OURS}/feed.xml?x=1", EPS)
check("fallback match by enclosure path", set(sync.overcast_finished()),
      {"x8A-laOF4nA", "OwR5eard0Ek"})

sync.overcast_opml = lambda: opml("https://nowhere.example/f.xml",
                                  [("aaaaaaaaaaa", "Alien", "1", "0", "1")],
                                  base="https://nowhere.example/files")
try:
    sync.overcast_finished(); check("missing feed raises", "no raise", "AuthExpired")
except sync.AuthExpired:
    check("missing feed raises", "AuthExpired", "AuthExpired")

print("playlist_id")
check("parsed from config", sync.playlist_id(),
      urllib.parse.parse_qs(urllib.parse.urlparse(sync.PLAYLIST).query)["list"][0])

print("missing_setup on a fresh machine")
blank = SCRATCH / "blank-state"; blank.mkdir(exist_ok=True)
sync.COOKIE_FILE = blank / "overcast_cookie.txt"
sync.YT_CLIENT_FILE = blank / "youtube_client.json"
sync.YT_TOKEN_FILE = blank / "youtube_token.json"
sync.KEYCHAIN_SERVICE = "yt-overcast-no-such-service"
missing = ORIGINAL_MISSING()
check("reports keychain", any("Keychain" in m for m in missing), True)
check("reports oauth client", any("youtube_client.json" in m for m in missing), True)

print("clean_playlist (dry run)")
sync.overcast_opml = lambda: opml(sync.FEED_URL, EPS)
sync.missing_setup = lambda: []
sync.DRY_RUN = True
check("dry run deletes nothing", sync.clean_playlist(), set())
check("dry run writes no state", sync.CLEANED_FILE.exists(), False)

print("clean_playlist (live, faked HTTP)")
sync.DRY_RUN = False
sync.yt_token = lambda: "fake-token"
sync.notify = lambda msg: None
deleted = []
PAGES = [{"items": [{"id": "item-x8A", "snippet": {"resourceId": {"videoId": "x8A-laOF4nA"}}},
                    {"id": "item-ONm", "snippet": {"resourceId": {"videoId": "ONmgDCCsmOY"}}}],
          "nextPageToken": "p2"},
         {"items": [{"id": "item-Isd", "snippet": {"resourceId": {"videoId": "IsdVx5U1Y9g"}}}]}]

def fake_fetch(url, data=None, headers=None, method=None, follow=True):
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if method == "DELETE":
        deleted.append(query["id"][0])
        return 204, {}, b""
    return 200, {}, json.dumps(PAGES[1] if query.get("pageToken") else PAGES[0]).encode()

sync.fetch = fake_fetch
check("removes only the finished one still in playlist", sync.clean_playlist(), {"x8A-laOF4nA"})
check("deleted the right playlist item", deleted, ["item-x8A"])
check("remembers both finished videos", set(json.loads(sync.CLEANED_FILE.read_text())),
      {"x8A-laOF4nA", "OwR5eard0Ek"})
check("kept the unfinished ones", {"ONmgDCCsmOY", "IsdVx5U1Y9g"} & set(json.loads(sync.CLEANED_FILE.read_text())), set())

deleted.clear()
check("second run is a no-op", sync.clean_playlist(), set())
check("no extra deletes", deleted, [])

print("video re-added after being cleaned")
PAGES = [{"items": [{"id": "item-x8A-again", "snippet": {"resourceId": {"videoId": "x8A-laOF4nA"}}}]}]
check("left alone, no second delete", sync.clean_playlist(), set())
check("no delete call", deleted, [])

print("yt_delete tolerates a missing item")
sync.fetch = lambda *a, **kw: (404, {}, b"")
try:
    sync.yt_delete("t", "gone"); check("404 is not an error", "ok", "ok")
except sync.AuthExpired:
    check("404 is not an error", "raised", "ok")
sync.fetch = lambda *a, **kw: (403, {}, b'{"error":"quota"}')
try:
    sync.yt_delete("t", "x"); check("403 raises", "no raise", "AuthExpired")
except sync.AuthExpired:
    check("403 raises", "AuthExpired", "AuthExpired")

print(f"\n{ok} ok, {fail} failed")
sys.exit(1 if fail else 0)