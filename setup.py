#!/usr/bin/python3
"""Walk through setting this up, doing every part that can be done for you.

Two parts cannot: creating an OAuth client in the Google Cloud console, and
creating a bucket in a storage provider's dashboard. For those this prints the
exact clicks and waits for you.

Run it as many times as you like. Steps that are already done are skipped.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
EXAMPLE = HERE / "config.json.example"
VENV = HERE / ".venv"
STATE = HERE / "state"
AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST = AGENTS / "yt-overcast.plist"


def say(text=""):
    print(text, flush=True)


def step(number, title):
    say(f"\n\033[1m{number}. {title}\033[0m")


def ask(question, default=""):
    suffix = f" [{default}]" if default else ""
    answer = input(f"   {question}{suffix}: ").strip()
    return answer or default


def wait(text):
    input(f"   {text}, then press Enter ")


def have(binary):
    return shutil.which(binary) is not None


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def main():
    if not sys.stdin.isatty():
        sys.exit("Run this in a terminal: python3 setup.py")
    say("Setting up yt-overcast in " + str(HERE))
    STATE.mkdir(exist_ok=True)

    step(1, "Command line tools")
    missing = [b for b in ("ffmpeg", "deno") if not have(b)]
    if missing:
        say(f"   Missing: {', '.join(missing)}")
        wait(f"Run: brew install {' '.join(missing)}")
    else:
        say("   ffmpeg and deno are installed")

    step(2, "yt-dlp and its JS challenge solver")
    if (VENV / "bin" / "yt-dlp").exists():
        say("   .venv already has yt-dlp")
    else:
        say("   Creating .venv and installing yt-dlp with yt-dlp-ejs")
        run([sys.executable, "-m", "venv", str(VENV)])
        run([str(VENV / "bin" / "pip"), "install", "--quiet", "--upgrade", "pip"])
        run([str(VENV / "bin" / "pip"), "install", "--quiet", "yt-dlp", "yt-dlp-ejs"])
        say("   done")

    step(3, "config.json")
    settings = json.loads(CONFIG.read_text()) if CONFIG.exists() else json.loads(EXAMPLE.read_text())
    say("   Make a YouTube playlist for the things you want to listen to.")
    say("   It has to be Public or Unlisted so the script can read it without logging in.")
    settings["playlist_url"] = ask("Playlist URL", settings.get("playlist_url", ""))
    settings["feed_title"] = ask("Name for the podcast", settings.get("feed_title", "My Feed"))

    step(4, "Where the audio files live")
    say("   1) A bucket (Cloudflare R2, Backblaze B2, S3). Unguessable address, recommended.")
    say("   2) A GitHub release. Nothing to sign up for, but the files are public to anyone")
    say("      who finds your repository.")
    choice = ask("Pick 1 or 2", "1" if settings.get("storage") == "s3" else "2")
    if choice == "1":
        settings["storage"] = "s3"
        say("\n   In the Cloudflare dashboard:")
        say("     R2 > Create bucket")
        say("     the bucket > Settings > Public Development URL > Enable")
        say("     R2 > Manage API tokens > Create Account API token")
        say("       permission: Object Read & Write, scoped to that one bucket")
        wait("Do that")
        settings["s3_bucket"] = ask("Bucket name", settings.get("s3_bucket", ""))
        settings["s3_endpoint"] = ask("S3 endpoint (https://<account-id>.r2.cloudflarestorage.com)",
                                      settings.get("s3_endpoint", ""))
        settings["public_base_url"] = ask("Public URL (https://pub-....r2.dev)",
                                          settings.get("public_base_url", "")).rstrip("/")
        settings["s3_prefix"] = settings.get("s3_prefix", "")
        key = ask("Access Key ID")
        say("   Paste the Secret Access Key when asked. It goes straight into the keychain.")
        subprocess.run(["security", "add-generic-password", "-U", "-s", "yt-overcast-s3",
                        "-a", key, "-A", "-w"])
    else:
        settings["storage"] = "github"
        settings["github_repo"] = ask("GitHub repo that will hold the files (owner/name)",
                                      settings.get("github_repo", ""))
        settings["release_tag"] = ask("Release tag", settings.get("release_tag", "audio"))
        if have("gh"):
            subprocess.run(["gh", "release", "create", settings["release_tag"],
                            "--repo", settings["github_repo"], "--notes", "audio"],
                           capture_output=True)
        else:
            wait("Install gh (brew install gh && gh auth login) and create that release")
    CONFIG.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    say("   Saved config.json")

    step(5, "Overcast password")
    say("   Only needed for emptying the playlist as you listen. Skip with an empty answer.")
    email = ask("Overcast e-mail", "")
    if email:
        say("   Paste your Overcast password when asked. It goes into the keychain.")
        subprocess.run(["security", "add-generic-password", "-U", "-s", "yt-overcast-overcast",
                        "-a", email, "-A", "-w"])

    step(6, "Permission to remove videos from the playlist")
    if (STATE / "youtube_token.json").exists():
        say("   Already granted")
    else:
        say("   In https://console.cloud.google.com :")
        say("     create a project, enable YouTube Data API v3")
        say("     OAuth consent screen: External, scope .../auth/youtube.force-ssl,")
        say("       then press Publish app (in Testing the token dies every 7 days)")
        say("     Credentials > OAuth client ID > Desktop app > download the JSON")
        wait(f"Save that JSON as {STATE / 'youtube_client.json'}")
        if (STATE / "youtube_client.json").exists():
            subprocess.run([sys.executable, "-I", str(HERE / "sync.py"), "--auth-youtube"])
        else:
            say("   No client file yet, run sync.py --auth-youtube later")

    step(7, "Age-restricted videos")
    profile = STATE / "chrome-profile"
    if profile.exists():
        say("   Chrome profile already there")
    elif ask("Set up a signed-in YouTube session for these? y/n", "y").lower().startswith("y"):
        profile.mkdir(exist_ok=True)
        subprocess.run(["open", "-na", "Google Chrome", "--args",
                        f"--user-data-dir={profile}", "https://www.youtube.com"])
        wait("Sign in to YouTube in the window that opened, then close it")

    step(8, "Running it every 4 hours")
    AGENTS.mkdir(parents=True, exist_ok=True)
    mine = str(HERE / "sync.py")
    already = [f for f in AGENTS.glob("*.plist")
               if f != PLIST and mine in f.read_text(errors="replace")]
    if already:
        say(f"   {already[0].name} already runs this copy, leaving it alone")
        return finish()
    PLIST.write_text((HERE / "yt-overcast.plist.example").read_text().replace("__DIR__", str(HERE)))
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)])
    say(f"   Installed and loaded {PLIST}")

    return finish()


def finish():
    step(9, "Checking everything")
    code = subprocess.run([sys.executable, "-I", str(HERE / "sync.py"), "--check"]).returncode
    say("\nReady, the feed URL is above." if code == 0
        else "\nFix what is listed above, then run this again.")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nStopped. Run it again whenever you like.")
