"""Compare our SigV4 signature with botocore's on the same requests."""
import datetime, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s3store

try:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
except ImportError:
    print("  skipped, botocore is not installed (pip install botocore to run this)")
    raise SystemExit(0)

ENDPOINT = "https://abc123.r2.cloudflarestorage.com"
BUCKET, AK, SK, REGION = "my-feed", "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "auto"
ok = fail = 0

def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1; print(f"  ok   {name}")
    else:
        fail += 1; print(f"  FAIL {name}\n       ours      {got}\n       botocore  {want}")

def freeze(stamp):
    fixed = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
    class Shim:
        timezone = datetime.timezone
        class datetime:
            @staticmethod
            def now(tz=None): return fixed
    s3store.datetime = Shim

def compare(name, method, key, query, body, extra):
    url = f"{ENDPOINT}/{BUCKET}" + (f"/{key}" if key else "")
    if query:
        # botocore signs the query exactly as given, so hand it the same
        # percent-encoding our client puts on the wire
        url += "?" + "&".join(f"{s3store._quote(k)}={s3store._quote(str(v))}"
                              for k, v in sorted(query.items()))
    headers = {"x-amz-content-sha256": s3store._sha256(body), **(extra or {})}
    req = AWSRequest(method=method, url=url, data=body or None, headers=headers)
    SigV4Auth(Credentials(AK, SK), "s3", REGION).add_auth(req)
    freeze(req.headers["X-Amz-Date"])
    bucket = s3store.Bucket(ENDPOINT, BUCKET, AK, SK, REGION)
    mine, _, _ = bucket._headers(method, key, query, s3store._sha256(body), extra)
    check(name, mine["authorization"], req.headers["Authorization"])

print("signatures for several request shapes")
compare("PUT a plain key", "PUT", "feed.xml", None, b"<rss/>", {"content-type": "application/rss+xml"})
compare("PUT a binary body", "PUT", "audio/x8A-laOF4nA.m4a", None, bytes(range(256)) * 40, {"content-type": "audio/mp4"})
compare("DELETE", "DELETE", "audio/old.m4a", None, b"", None)
compare("GET a listing", "GET", "", {"list-type": "2", "prefix": "audio/"}, b"", None)
compare("GET a continued listing", "GET", "", {"list-type": "2", "prefix": "a/b", "continuation-token": "1/abc+def=="}, b"", None)
compare("a key with slashes and hyphens", "PUT", "audio/5vAriTOghLY.m4a", None, b"x", {"content-type": "audio/mp4"})
compare("an empty body", "PUT", "empty", None, b"", None)

print(f"\n{ok} ok, {fail} failed")
sys.exit(1 if fail else 0)
