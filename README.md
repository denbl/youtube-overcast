# youtube-overcast

Turn a YouTube playlist into a private podcast feed, and let your podcast app
empty that playlist as you listen.

## The problem

Long interviews, lectures and podcasts on YouTube are mostly talk. A podcast
app plays that kind of audio better than YouTube does: speed control, voice
boost, downloads for offline, and a playback position that follows you across
devices. Getting the audio out of YouTube and into one is the missing piece.

A second problem shows up later. Whatever list you collect videos in fills up
with things you have already heard, and every couple of weeks you go in and
clean it out by hand.

## What this does

It keeps one YouTube playlist and one private podcast feed in sync, both ways.

- Save a video to the playlist from the YouTube app, one tap. Within a few
  hours it is an episode in your podcast player, already downloaded.
- Listen to it and delete it in the player. The video leaves the YouTube
  playlist and its audio file is deleted.

The playlist stays an inbox: it only ever holds what you have not listened to
yet. Nothing to tidy up.

It is built for listening while walking or running, where you are not looking
at a screen.

## What you end up with

A private podcast in your player that mirrors a playlist on YouTube. Audio is
mono AAC at 64 kbps, roughly 29 MB per hour, which is plenty for talk.

A Mac has to be on for the sync to run, since this is a launchd agent and not
a server.

## What you need before you start

- A Mac. Scheduling uses launchd, secrets live in the Keychain, notifications
  go through `osascript`.
- Python 3.11 or newer, because yt-dlp needs it. macOS ships 3.9, so this
  usually means `brew install python@3.13`. The script itself runs on the
  system Python; only its virtualenv needs the new one.
- `ffmpeg` and `deno`:

  ```bash
  brew install ffmpeg deno
  ```

  Add `gh` to that line if you are going to keep the files in a GitHub release
  rather than a bucket, and run `gh auth login`.
- A YouTube playlist, Public or Unlisted, so the script can read it without
  signing in.
- Somewhere to put the files. A Cloudflare R2 bucket is free at this volume and
  gives you an address nobody can guess; a GitHub release needs no signup but
  is public to anyone who finds the repo. More on that below.
- About 30 minutes, most of it clicking through two web consoles.

Two things are optional and the feed works without them. Emptying the playlist
as you listen needs an Overcast account and a Google Cloud OAuth client.
Downloading age-restricted videos needs a signed-in YouTube session.

## Setup

```bash
git clone https://github.com/denbl/youtube-overcast && cd youtube-overcast
python3 setup.py
```

`setup.py` asks a short question at a time and does everything it can itself:
creates the virtualenv, installs yt-dlp with its JS challenge solver, writes
`config.json`, puts your passwords in the Keychain without printing them,
installs the launchd agent, and checks the result.

Loading the agent starts the first sync straight away. Downloading and
re-encoding a few hours of audio takes a few minutes, so the feed will be empty
for a little while. Watch `state/sync.log` if you want to see it happen.

Run it again whenever you like. Steps that are already done are skipped.

Two things it cannot do for you, so it stops and tells you exactly which
buttons to press:

1. Creating the OAuth client in the Google Cloud console. There is no API for
   it. The one step people get wrong is the publishing status: while the app
   sits in Testing, Google revokes the refresh token every 7 days and the
   playlist cleanup silently stops. Press Publish app.
2. Creating the bucket in your storage provider's dashboard, and the API token
   that goes with it.

## Checking the setup

```bash
python3 sync.py --check
```

It tests every part separately, says what is missing, and prints the command
that fixes each gap. When nothing is missing it prints your feed URL, which is
the one thing you take to your podcast app.

```
Tools
   ok   yt-dlp                 2026.11.23  (.venv)
   ok   yt-dlp-ejs             installed
...
Credentials
  MISS  YouTube access         no youtube_client.json, download the OAuth client

To fix
  YouTube access:
    save the OAuth client as state/youtube_client.json, then run: python3 sync.py --auth-youtube
```

## Day to day

Nothing. Save videos to the playlist, listen in your player, delete episodes
there. The agent wakes every 4 hours, and after sleep if it missed a run.

When something looks wrong, `--check` first, then `state/sync.log`.

## What one run does

1. Asks Overcast which episodes are finished and drops those videos from the
   YouTube playlist. Finished means the OPML export says `played="1"` or
   `userDeleted="1"`. Partial progress does not count, so a half-listened
   episode stays put.
2. Reads the playlist.
3. Downloads the audio of anything new and re-encodes it. If YouTube asks to
   confirm age, the script signs in and retries that one video.
4. Uploads the files to your bucket or release.
5. Deletes the files of videos no longer in the playlist, including the ones
   step 1 just removed.
6. Rebuilds `feed.xml` next to them.

If the cleanup half breaks, downloads carry on. It gets logged, you get one
notification, and the feed updates as usual.

## Where the files live

By default the audio and `feed.xml` go into a GitHub release, which needs
nothing but an authenticated `gh`. The catch is that release assets of a public
repo are public, and the repo shows up on your profile.

The alternative is any S3-compatible bucket. Cloudflare R2 fits this well:
10 GB free, no charge for traffic out, and a public address of the form
`https://pub-<random>.r2.dev` that nobody can guess or stumble onto. Backblaze
B2 and AWS S3 speak the same protocol and work with the same code. Enabling R2
does ask for a payment method even though the usage stays free.

Moving across later takes one command: set `"storage": "s3"` with the bucket
keys, then `python3 sync.py --migrate`. It copies the cover and every episode
and rebuilds the feed against the new address. Your player sees a different
feed URL, so you resubscribe once and anything half-listened starts over.

Signing is plain AWS Signature Version 4 written against the standard library,
checked against botocore's output on seven kinds of request.

## Age-restricted videos

These need a signed-in YouTube session. Getting one on macOS takes more work
than it sounds, for one specific reason.

Chrome 158 encrypts its cookie store with a scheme it labels `v11`, and yt-dlp
only decrypts that on Linux. On macOS every value comes back as ciphertext, so
you get `TypeError: expected str instance, bytes found` or a cookie file full
of binary. Full Disk Access changes nothing, because the problem is decryption
rather than access. Chrome will hand the cookies over in plain text through its
DevTools protocol though, and that is what `dump_cookies.py` asks it for.

The session lives in a Chrome profile used for nothing else, so normal browsing
never rotates it. Sign in there once and close the window. After that the
script downloads signed out, and only when YouTube asks to confirm age does it
start that profile headless, take the current cookies and retry the one video.
If the session expires you get a notification and sign in again in the same
window.

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

Keep it current with `.venv/bin/pip install -U yt-dlp yt-dlp-ejs`.

## Commands

```bash
python3 setup.py                # set everything up, or fill in what is missing
python3 sync.py --check         # what works, what does not, and how to fix it
python3 sync.py                 # one normal run
python3 sync.py --dry-run       # build into ./out, report planned deletions, touch nothing
python3 sync.py --rebuild       # re-upload the cover and feed.xml
python3 sync.py --auth-youtube  # grant permission to edit the playlist, once
python3 sync.py --migrate       # copy a GitHub release into a bucket
launchctl kickstart gui/$(id -u)/youtube-overcast   # run the agent now
```

## Tests

```bash
python3 tests/run.py
```

57 checks over the parts that are easy to get wrong: reading Overcast's export,
deciding what counts as listened, the cookie fallback, which storage a file
goes to, and the migration. None of them reach the network or a live account.

`tests/test_sigv4.py` compares the hand-written AWS signature against botocore
on seven kinds of request. It skips itself unless botocore is there:

```bash
.venv/bin/pip install botocore && .venv/bin/python tests/run.py
```

`tests/drive_setup.py` is kept out of that run because it is slow. It drives
`setup.py` start to finish on a throwaway copy, answering it the way a person
would, and that is the test that caught the installer building its virtualenv
with a Python too old for yt-dlp. It uses a temporary directory and its own
HOME, and unloads the launchd agent it installs.

## Settings

`config.json`, written for you by `setup.py`:

| key | what it is |
|---|---|
| `playlist_url` | the YouTube playlist that acts as your inbox |
| `feed_title` | the name your podcast app shows |
| `storage` | `s3` or `github` |
| `s3_endpoint`, `s3_bucket`, `s3_prefix` | where files go with `s3` |
| `public_base_url` | the address your player fetches them from |
| `github_repo`, `release_tag` | where files go with `github` |
| `cookies_file`, `chrome_profile` | the signed-in session for age-restricted videos |

Secrets are never in here. The Overcast password and the bucket keys live in
the login Keychain, the YouTube token in `state/`, and none of it is committed.

## Files

- `sync.py` – the whole sync, standard library only.
- `s3store.py` – a small S3 client, standard library only.
- `dump_cookies.py` – asks Chrome for its cookies.
- `setup.py` – the guided install.
- `tests/` – the test suite, see above.
- `state/` – credentials, episode list, log. Not committed.

## About the files being public

Release assets of a public repo are public. Anyone with the URL, or anyone
browsing the owner's profile, can download the episodes. A private repo is not
an option, because the player has to fetch the feed without credentials. Moving
to a bucket takes care of the browsing half: the address stays unguessable and
nothing links to it.

It also means you are re-hosting someone else's YouTube audio at a public URL.
YouTube's terms do not allow that, and a rights holder could file a DMCA notice
over it. For one person's commute the risk is small, but it exists, and it is
better to decide about it before you point anyone at your feed.

## License

MIT, see `LICENSE`.
