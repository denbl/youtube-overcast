"""The cover art has to reach storage on its own, without anyone running --rebuild."""
import atexit, json, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="youtube-overcast-tests-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
sys.path.insert(0, str(ROOT))
import sync

ok = fail = 0
def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1; print(f"  ok   {name}")
    else:
        fail += 1; print(f"  FAIL {name}\n       got  {got!r}\n       want {want!r}")

state = SCRATCH / "state"; state.mkdir()
uploaded = []
sync.STATE_DIR = state
sync.STATE_FILE = state / "episodes.json"
sync.CLEANED_FILE = state / "cleaned.json"
sync.DRY_RUN = sync.AUTH_YOUTUBE = sync.MIGRATE = sync.CHECK = sync.REBUILD = False
sync.clean_playlist = lambda: set()
sync.notify = lambda m: None
sync.upload = lambda path: uploaded.append(Path(path).name)
sync.upload_copy = lambda path: uploaded.append(Path(path).name)
sync.delete_asset = lambda name: None

episode = {"id": "new1", "title": "New", "channel": "c", "description": "", "duration": 60,
           "image": "", "link": "u", "added": 1.0, "file": "new1.m4a", "size": 10}
sync.playlist_ids = lambda: ["new1"]
sync.fetch_with_cookies = lambda vid, tmp: (dict(episode), Path(tmp) / "new1.m4a")

print("storage has no cover yet")
sync.stored = lambda name: False
uploaded.clear(); sync.STATE_FILE.unlink(missing_ok=True)
sync.main()
check("cover uploaded", sync.COVER in uploaded, True)
check("feed uploaded too", "feed.xml" in uploaded, True)

print("cover is already there")
sync.stored = lambda name: True
uploaded.clear(); sync.STATE_FILE.unlink(missing_ok=True)
sync.main()
check("cover not uploaded again", sync.COVER in uploaded, False)
check("feed still uploaded", "feed.xml" in uploaded, True)

print("nothing changed")
uploaded.clear()
sync.stored = lambda name: False
sync.main()   # state now matches the playlist, so nothing changes
check("no uploads at all", uploaded, [])

print("stored() survives a dead address")
import importlib
fresh = importlib.reload(sync)          # a copy with the real stored() back
fresh.STORAGE, fresh.PUBLIC_BASE = "s3", "https://127.0.0.1:9"
check("unreachable means not stored", fresh.stored("cover.jpg"), False)

print(f"\n{ok} ok, {fail} failed")
sys.exit(1 if fail else 0)
