#!/usr/bin/python3
"""Run setup.py start to finish on a throwaway copy, answering it as a person would.

    python3 tests/drive_setup.py

This is the test that caught setup.py building its virtualenv with Apple's
Python 3.9, which yt-dlp cannot run on. It is slow, because it really does
create a virtualenv and install yt-dlp, so it is not part of tests/run.py.

Nothing of yours is touched: the copy lives in a temporary directory, HOME is
redirected so the launchd agent lands there, and that agent is unloaded again
at the end. The answers below deliberately skip every step that would write to
your keychain or sign you in anywhere.
"""
import os, pty, re, select, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["sync.py", "s3store.py", "dump_cookies.py", "setup.py",
         "config.json.example", "yt-overcast.plist.example", "cover.jpg"]
ANSWERS = [
    "https://www.youtube.com/playlist?list=PLAFRuxECUmqI",  # a real playlist id
    "Test Feed",
    "2",                                   # a GitHub release, so no bucket keys are written
    "nobody/this-repo-does-not-exist-9z",  # a repo that cannot exist
    "audio",
    "",                                    # no Overcast e-mail, so no keychain write
    "",                                    # Enter past the Google Cloud step
    "n",                                   # no Chrome profile, so no browser opens
]
PROMPT = re.compile(r"(: |press Enter )$")


def drive(workdir, home):
    primary, secondary = pty.openpty()
    proc = subprocess.Popen([sys.executable, "setup.py"], cwd=workdir,
                            env=dict(os.environ, HOME=str(home), TERM="dumb"),
                            stdin=secondary, stdout=secondary, stderr=secondary)
    os.close(secondary)
    transcript, pending, sent, deadline = "", "", 0, time.time() + 600
    while time.time() < deadline:
        if proc.poll() is not None and not select.select([primary], [], [], 0.2)[0]:
            break
        if select.select([primary], [], [], 1.0)[0]:
            try:
                chunk = os.read(primary, 4096).decode(errors="replace")
            except OSError:
                break
            if not chunk:
                break
            transcript += chunk
            pending += chunk
            if PROMPT.search(pending.split("\n")[-1]) and sent < len(ANSWERS):
                os.write(primary, (ANSWERS[sent] + "\n").encode())
                sent += 1
                pending = ""
    proc.wait(timeout=30)
    os.close(primary)
    return re.sub(r"\x1b\[[0-9;]*m", "", transcript), sent, proc.returncode


def main():
    scratch = Path(tempfile.mkdtemp(prefix="yt-overcast-setup-"))
    workdir, home = scratch / "copy", scratch / "home"
    workdir.mkdir()
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    for name in FILES:
        shutil.copy(ROOT / name, workdir / name)
    plist = home / "Library" / "LaunchAgents" / "yt-overcast.plist"
    try:
        transcript, sent, code = drive(workdir, home)
        print(transcript)
        print(f"--- answered {sent} of {len(ANSWERS)} prompts, setup.py exited {code} ---")
        wanted = ["Creating .venv with Python", "Saved config.json",
                  "Installed and loaded", "Checking everything"]
        missed = [w for w in wanted if w not in transcript]
        if sent != len(ANSWERS) or missed:
            print("FAILED: " + (f"missed {missed}" if missed else "not every prompt was answered"))
            return 1
        print("ok, setup.py ran every step")
        return 0
    finally:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
                       capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)
        print("cleaned up the copy and unloaded its agent")


if __name__ == "__main__":
    sys.exit(main())
