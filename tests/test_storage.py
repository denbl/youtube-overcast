"""Storage dispatch: GitHub release vs S3-compatible bucket, and the migration."""
import atexit, json, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="youtube-overcast-tests-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
sys.path.insert(0, str(ROOT))
import sync

REAL_BUCKET = sync.bucket          # kept before stubbing, to test its own checks
ok = fail = 0

def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1; print(f"  ok   {name}")
    else:
        fail += 1; print(f"  FAIL {name}\n       got  {got!r}\n       want {want!r}")

class FakeBucket:
    def __init__(self): self.puts, self.deletes = [], []
    def put(self, key, path, ct="application/octet-stream"):
        data = Path(path).read_bytes(); self.puts.append((key, len(data), ct)); return len(data)
    def delete(self, key): self.deletes.append(key)

fake = FakeBucket()
sync.bucket = lambda: fake
sync.DRY_RUN = False
gh = []
sync.run = lambda cmd, **kw: (gh.append(cmd), "")[1]

state = SCRATCH / "sstate"; state.mkdir(exist_ok=True)
sync.STATE_FILE = state / "episodes.json"
tmpf = SCRATCH / "upload-me.m4a"

print("content_type")
for suffix, want in ((".m4a", "audio/mp4"), (".xml", "application/rss+xml"),
                     (".jpg", "image/jpeg"), (".bin", "application/octet-stream")):
    check(suffix, sync.content_type("x" + suffix), want)

print("storage = github")
sync.STORAGE = "github"
check("file address", sync.asset_url("x.m4a"),
      f"https://github.com/{sync.REPO}/releases/download/{sync.TAG}/x.m4a")
gh.clear(); fake.puts.clear(); tmpf.write_bytes(b"audio")
sync.upload(tmpf)
check("uploads through gh", gh[0][:3], ["gh", "release", "upload"])
check("nothing went to the bucket", fake.puts, [])

print("storage = s3")
sync.STORAGE, sync.PUBLIC_BASE, sync.S3_PREFIX = "s3", "https://pub-deadbeef.r2.dev", "feed/"
check("file address", sync.asset_url("x.m4a"), "https://pub-deadbeef.r2.dev/feed/x.m4a")
check("the GitHub address stays available for migration", sync.github_url("x.m4a"),
      f"https://github.com/{sync.REPO}/releases/download/{sync.TAG}/x.m4a")
gh.clear(); fake.puts.clear(); fake.deletes.clear(); tmpf.write_bytes(b"audio-bytes")
sync.upload(tmpf)
check("went to the bucket under the prefix", fake.puts, [("feed/upload-me.m4a", 11, "audio/mp4")])
check("gh was not called", gh, [])
sync.delete_asset("old.m4a")
check("deletes from the bucket", fake.deletes, ["feed/old.m4a"])

print("incomplete bucket settings")
sync._bucket = None
saved = sync.S3_ENDPOINT, sync.S3_BUCKET, sync.PUBLIC_BASE
sync.S3_ENDPOINT, sync.S3_BUCKET, sync.PUBLIC_BASE = "", "b", "https://x"
try:
    REAL_BUCKET(); check("an empty endpoint is caught", "no raise", "AuthExpired")
except sync.AuthExpired as e:
    check("an empty endpoint is caught", "s3_endpoint" in str(e), True)
sync.S3_ENDPOINT, sync.S3_BUCKET, sync.PUBLIC_BASE = saved

print("migrating a GitHub release into a bucket")
episodes = [{"id": "aaa", "file": "aaa.m4a", "title": "A", "channel": "c", "description": "",
             "duration": 60, "image": "", "link": "u", "added": 1.0, "size": 5},
            {"id": "bbb", "file": "bbb.m4a", "title": "B", "channel": "c", "description": "",
             "duration": 60, "image": "", "link": "u", "added": 2.0, "size": 5}]
sync.STATE_FILE.write_text(json.dumps(episodes))
fetched = []
def fake_fetch(url, **kw):
    fetched.append(url)
    return (404, {}, b"") if "bbb" in url else (200, {}, b"filebytes")
sync.fetch = fake_fetch
fake.puts.clear()
sync.migrate()
check("downloaded from GitHub", all("github.com" in u for u in fetched), True)
check("the cover too", any(sync.COVER in u for u in fetched), True)
keys = [k for k, _, _ in fake.puts]
check("moved the cover and the reachable episode", keys, ["feed/cover.jpg", "feed/aaa.m4a", "feed/feed.xml"])
check("an unreachable file is skipped, not fatal", "feed/bbb.m4a" in keys, False)
feed_ct = [ct for k, _, ct in fake.puts if k.endswith("feed.xml")]
check("the feed gets the right content type", feed_ct, ["application/rss+xml"])

print("migration requires storage=s3")
sync.STORAGE = "github"
try:
    sync.migrate(); check("refused with storage=github", "no raise", "AuthExpired")
except sync.AuthExpired:
    check("refused with storage=github", "AuthExpired", "AuthExpired")

print(f"\n{ok} ok, {fail} failed")
sys.exit(1 if fail else 0)