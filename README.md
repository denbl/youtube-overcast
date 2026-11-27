# yt-overcast

A lot of what I want to listen to lives on YouTube: two-hour interviews, talks,
podcasts that happen to have a video track. A podcast app handles that kind of
audio better than YouTube does. It has speed control, voice boost, a queue that
remembers where I stopped and syncs it across devices. The only hard part is
getting the audio in there.

This script does that. You save a video to a YouTube playlist from the app, one
tap, the way you already do. Within a few hours it shows up in your podcast
player as an episode, already downloaded.

The part I actually built it for is what happens next. You listen, you delete
the episode in the player, and the video disappears from the YouTube playlist
and its audio file is deleted. Before, that playlist slowly filled with things
I had already heard, and every couple of weeks I went in and cleaned it out by
hand. Now it only ever holds what I have not listened to yet, and I open it to
add things, never to tidy up.

Audio comes out as mono AAC at 64 kbps, roughly 29 MB per hour. For talk that
is plenty.

## What it is wired to

A personal tool, not a product. Replacing any of these means editing `sync.py`:

- macOS, for launchd scheduling, the Keychain and notifications
- GitHub Releases as the file host, through an authenticated `gh`
- Overcast, for the "I have listened to this" signal
- Chrome, and only if you want age-restricted videos to download

The feed itself plays in any podcast app. Overcast is only needed for emptying
the playlist, because it is the one that will tell a script what you have
heard.

Everything else lives in `config.json`: the playlist, the repo that stores the
files, the release tag, the feed title and where cookies go.

## What one run does

A launchd agent runs `sync.py` every 4 hours, and after wake if a run was
missed. Each run:

1. Asks Overcast which episodes are finished and drops those videos from the
   YouTube playlist. Finished means the OPML export says `played="1"` or
   `userDeleted="1"`. Partial progress does not count, so a half-listened
   episode stays put.
2. Reads the playlist. It can be public or unlisted, no login needed.
3. Downloads the audio of anything new and re-encodes it. If YouTube asks to
   confirm age, the script signs in and retries that one video.
4. Uploads the files as assets of a GitHub release.
5. Deletes the assets of videos that are no longer in the playlist, including
   the ones step 1 just removed.
6. Rebuilds `feed.xml` next to them.

If the cleanup half breaks, downloads carry on. It gets logged, you get one
notification, and the feed updates as usual.

## Install

```bash
brew install ffmpeg gh deno && gh auth login
git clone <this repo> && cd yt-overcast
python3 -m venv .venv && .venv/bin/pip install yt-dlp yt-dlp-ejs
cp config.json.example config.json   # then edit it
sed "s|__DIR__|$PWD|g" yt-overcast.plist.example > ~/Library/LaunchAgents/yt-overcast.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/yt-overcast.plist
```

In the repo that will store the files, create a release with the tag from
`config.json`, then run `python3 sync.py --rebuild` once to upload the cover.

Your feed URL is
`https://github.com/<owner>/<repo>/releases/download/<tag>/feed.xml`. Add it to
your player as a custom feed.

yt-dlp comes from a virtualenv instead of Homebrew on purpose, for reasons in
the JS solver section. Keep it current with
`.venv/bin/pip install -U yt-dlp yt-dlp-ejs`.

Both setups below are optional. Skip them and the feed still works; the script
logs what is missing and carries on.

## Letting Overcast empty the playlist

Overcast has no public API. What it does have is an extended OPML export, which
needs a signed-in session and is the only stable way to read your listening
state without driving a browser. Put the password in the login keychain so it
never sits in the repo:

```bash
security add-generic-password -s yt-overcast-overcast -a YOUR_OVERCAST_EMAIL -A -w
```

`-A` lets the script read it without a GUI prompt every run. It signs in once,
caches the session cookie under `state/`, and signs in again when that cookie
dies. Overcast allows around 10 exports a day and a 4-hour schedule uses 6.

Deleting a playlist item needs OAuth, so a YouTube API key will not do:

1. Create a project in the [Google Cloud Console](https://console.cloud.google.com).
2. Enable YouTube Data API v3.
3. On the OAuth consent screen choose External, add the scope
   `https://www.googleapis.com/auth/youtube.force-ssl`, then press Publish app.
4. Create an OAuth client ID of type Desktop app, save the JSON as
   `state/youtube_client.json`.
5. Run `python3 sync.py --auth-youtube` and approve it. Google shows an
   "unverified app" warning for your own app; choose Advanced and continue.

Do not skip step 3. While the app sits in Testing, Google revokes the refresh
token every 7 days and the cleanup quietly stops working until you authorise it
again.

Quota is 10 000 units a day. One deletion costs 50, listing the playlist costs 1.

## Age-restricted videos

These need a signed-in YouTube session, and on macOS that turned out to be the
fiddliest part of the whole thing.

Chrome 158 encrypts its cookie store with a scheme it labels `v11`, and yt-dlp
only decrypts that on Linux. On macOS every value comes back as ciphertext, so
you get `TypeError: expected str instance, bytes found` or a cookie file full
of binary. Full Disk Access changes nothing, because the problem is decryption
rather than access. Chrome will hand the cookies over in plain text through its
DevTools protocol though, and that is what `dump_cookies.py` asks it for.

Keep that session in a Chrome profile you use for nothing else, so normal
browsing never rotates it:

```bash
open -na "Google Chrome" --args --user-data-dir=$PWD/state/chrome-profile https://www.youtube.com
```

Sign in once there and close the window. After that it runs itself: the script
downloads signed out, and only when YouTube asks to confirm age does it start
that profile headless, take the current cookies and retry the one video. If the
session expires you get a notification and sign in again in the same window.

Signing in is a fallback rather than the default, and that is deliberate. A
signed-in request makes YouTube demand a solved JS challenge, so spending
cookies on videos that download fine without them would only add a way to fail.

## Why yt-dlp comes from a virtualenv

`yt-dlp-ejs` is what solves that JS challenge. The Homebrew build of yt-dlp
ships a solver of its own, and with deno 2.9.x it dies on
`TypeError: a.addOnDisposeCallback is not a function`. Anonymous requests
survive a failed challenge. Signed-in ones do not: you get an empty format list
and `Requested format is not available`. So the two live together in a
virtualenv at a combination known to work, and `sync.py` prefers
`.venv/bin/yt-dlp` when it finds it.

## Commands

```bash
python3 sync.py                 # one normal run
python3 sync.py --dry-run       # build into ./out, report planned deletions, touch nothing
python3 sync.py --rebuild       # re-upload the cover and feed.xml
python3 sync.py --auth-youtube  # grant write access to the playlist, once
launchctl kickstart gui/$(id -u)/yt-overcast   # run the agent now
```

## Notes

- `state/` holds everything private: credentials, cookies, the episode list and
  the log. It is not committed, and neither is `config.json`.
- `state/cleaned.json` remembers videos already dropped from the playlist, so
  none is ever removed twice. Re-add something you have already heard and it
  stays until you delete it yourself.
- When downloads start failing, update yt-dlp first and read `state/sync.log`.

## About the files being public

Release assets of a public repo are public. Anyone with the URL, or anyone
browsing the owner's profile, can download the episodes. A private repo is not
an option, because the player has to fetch the feed without credentials.

It also means you are re-hosting someone else's YouTube audio at a public URL.
YouTube's terms do not allow that, and a rights holder could file a DMCA notice
over it. For one person's commute the risk is small, but it exists, and it is
worth deciding about before you point anyone at your feed. Keeping the files in
a repo of their own, separate from this code, at least leaves the two
unconnected.

## License

MIT, see `LICENSE`.
