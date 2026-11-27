"""Signing in must happen only on an age gate, and only when it can."""
import atexit, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="yt-overcast-tests-"))
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

AGE = "ERROR: [youtube] X: Sign in to confirm your age. This video may be inappropriate"
OTHER = "ERROR: [youtube] X: Video unavailable"

profile = SCRATCH / "fake-profile"; profile.mkdir(exist_ok=True)
target = SCRATCH / "fake-cookies.txt"
dumped, notes, calls = [], [], []
sync.notify = lambda m: notes.append(m)

def fake_run(cmd, **kw):
    dumped.append(cmd)
    target.write_text("# Netscape HTTP Cookie File\n")
    return "saved 49 cookies\n"
sync.run = fake_run

def make_fetch(fails_without, err, fails_with=False):
    def f(vid, workdir, cookies=()):
        calls.append(list(cookies))
        if (not cookies and fails_without) or (cookies and fails_with):
            raise subprocess.CalledProcessError(1, ["yt-dlp"], stderr=err)
        return {"id": vid}, Path(workdir) / f"{vid}.m4a"
    return f

def reset():
    dumped.clear(); notes.clear(); calls.clear()
    target.unlink(missing_ok=True)
    sync.CHROME_PROFILE, sync.COOKIES_FILE = str(profile), str(target)

print("refresh_cookies")
reset()
check("returns yt-dlp arguments", sync.refresh_cookies(), ["--cookies", str(target)])
check("runs the dumper", dumped and str(sync.DUMPER) in dumped[0], True)

reset(); sync.CHROME_PROFILE = ""
check("not configured, so nothing", sync.refresh_cookies(), [])
check("the dumper never ran", dumped, [])

reset(); sync.CHROME_PROFILE = str(SCRATCH / "no-such-profile")
check("no profile, so nothing", sync.refresh_cookies(), [])

reset()
def boom(cmd, **kw):
    raise subprocess.CalledProcessError(1, cmd, stderr="Chrome did not open its debugging port")
sync.run = boom
check("a failed dump returns nothing and does not raise", sync.refresh_cookies(), [])
sync.run = fake_run

print("fetch_with_cookies")
reset(); sync.fetch_episode = make_fetch(False, AGE)
sync.fetch_with_cookies("v", "/tmp")
check("downloaded first try, cookies untouched", calls, [[]])
check("Chrome never started", dumped, [])

reset(); sync.fetch_episode = make_fetch(True, AGE)
sync.fetch_with_cookies("v", "/tmp")
check("age gate, so a retry with cookies", calls, [[], ["--cookies", str(target)]])
check("no notification", notes, [])

reset(); sync.fetch_episode = make_fetch(True, OTHER)
try:
    sync.fetch_with_cookies("v", "/tmp"); check("any other error propagates", "no raise", "raise")
except subprocess.CalledProcessError:
    check("any other error propagates", "raise", "raise")
check("no retry happened", calls, [[]])
check("Chrome never started", dumped, [])

reset(); sync.CHROME_PROFILE = ""; sync.fetch_episode = make_fetch(True, AGE)
try:
    sync.fetch_with_cookies("v", "/tmp"); check("age gate without a profile propagates", "no raise", "raise")
except subprocess.CalledProcessError:
    check("age gate without a profile propagates", "raise", "raise")

print("an expired sign-in")
reset(); sync.fetch_episode = make_fetch(True, AGE, fails_with=True)
try:
    sync.fetch_with_cookies("v", "/tmp")
except subprocess.CalledProcessError:
    pass
check("the retry happened", len(calls), 2)
check("warned about the sign-in", len(notes) == 1 and "Sign in again" in notes[0], True)

print(f"\n{ok} ok, {fail} failed")
sys.exit(1 if fail else 0)