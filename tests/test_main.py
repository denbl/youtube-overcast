"""main() must survive missing credentials and leave everything as it was."""
import atexit, json, shutil, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="youtube-overcast-tests-"))
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
sys.path.insert(0, str(ROOT))
import sync

state = SCRATCH / "state"
state.mkdir()
episodes = [{"id": f"vid{n}", "title": f"Episode {n}", "channel": "c", "description": "",
             "duration": 60, "image": "", "link": f"https://youtu.be/vid{n}", "added": float(n),
             "file": f"vid{n}.m4a", "size": 1_000_000} for n in range(3)]
(state / "episodes.json").write_text(json.dumps(episodes))

sync.STATE_DIR = state
sync.STATE_FILE = state / "episodes.json"
sync.CLEANED_FILE = state / "cleaned.json"
sync.LOCAL_DIR = SCRATCH / "out"
sync.DRY_RUN = True
sync.AUTH_YOUTUBE = sync.MIGRATE = sync.CHECK = sync.REBUILD = False
sync.notify = lambda msg: print("   notified:", msg)

# no test may touch a live account, so the half that talks to Overcast and
# YouTube is replaced outright
sync.clean_playlist = lambda: set()
sync.playlist_ids = lambda: [e["id"] for e in episodes]        # playlist matches the feed
sync.fetch_episode = lambda *a, **k: sys.exit("!! tried to download")
sync.fetch_with_cookies = lambda *a, **k: sys.exit("!! tried to download")
sync.upload = lambda *a: sys.exit("!! tried to upload")
sync.delete_asset = lambda *a: sys.exit("!! tried to delete an asset")

code = sync.main()
after = json.loads((state / "episodes.json").read_text())
ok = code == 0 and after == episodes and not sync.CLEANED_FILE.exists()
print(f"  {'ok  ' if code == 0 else 'FAIL'} exit code {code}")
print(f"  {'ok  ' if after == episodes else 'FAIL'} episodes untouched")
print(f"  {'ok  ' if not sync.CLEANED_FILE.exists() else 'FAIL'} nothing cleaned without credentials")
print(f"\n{'3 ok, 0 failed' if ok else 'failed'}")
sys.exit(0 if ok else 1)
