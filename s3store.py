#!/usr/bin/python3
"""Minimal S3-compatible storage: upload, delete, list. Standard library only.

Written for Cloudflare R2, but the signing is plain AWS Signature Version 4, so
it works against S3 and Backblaze B2 as well. Credentials come from the login
keychain, never from the repo.
"""
import datetime
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UNRESERVED_PATH = "-_.~/"
ALGORITHM = "AWS4-HMAC-SHA256"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _sign(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _quote(value, safe="-_.~"):
    return urllib.parse.quote(value, safe=safe)


class Bucket:
    """One S3-compatible bucket, addressed path-style (endpoint/bucket/key)."""

    def __init__(self, endpoint, bucket, access_key, secret_key, region="auto"):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.host = urllib.parse.urlparse(self.endpoint).netloc

    def _signing_key(self, stamp):
        key = f"AWS4{self.secret_key}".encode()
        for part in (stamp, self.region, "s3", "aws4_request"):
            key = _sign(key, part)
        return key

    def _headers(self, method, key, query, payload_hash, extra=None):
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        stamp = now.strftime("%Y%m%d")

        headers = {"host": self.host, "x-amz-content-sha256": payload_hash,
                   "x-amz-date": amz_date, **{k.lower(): v for k, v in (extra or {}).items()}}
        signed = ";".join(sorted(headers))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canonical_query = "&".join(
            f"{_quote(k)}={_quote(str(v))}" for k, v in sorted((query or {}).items()))
        path = _quote(f"/{self.bucket}/{key}" if key else f"/{self.bucket}", UNRESERVED_PATH)

        canonical = "\n".join([method, path, canonical_query, canonical_headers,
                               signed, payload_hash])
        scope = f"{stamp}/{self.region}/s3/aws4_request"
        to_sign = "\n".join([ALGORITHM, amz_date, scope, _sha256(canonical.encode())])
        signature = hmac.new(self._signing_key(stamp), to_sign.encode(),
                             hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"{ALGORITHM} Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}")
        return headers, path, canonical_query

    def _request(self, method, key, query=None, body=b"", extra=None):
        payload_hash = _sha256(body)
        headers, path, canonical_query = self._headers(
            method, key, query, payload_hash, extra)
        url = f"{self.endpoint}{path}" + (f"?{canonical_query}" if canonical_query else "")
        request = urllib.request.Request(url, data=body or None, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {key or '/'} failed "
                               f"(HTTP {e.code}): {e.read()[:400].decode(errors='replace')}")

    def put(self, key, path, content_type="application/octet-stream"):
        body = path.read_bytes() if hasattr(path, "read_bytes") else open(path, "rb").read()
        self._request("PUT", key, body=body, extra={"content-type": content_type})
        return len(body)

    def delete(self, key):
        self._request("DELETE", key)

    def list(self, prefix=""):
        """Return {key: size} for everything under prefix."""
        found, token = {}, None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            _, body = self._request("GET", "", query=query)
            root = ET.fromstring(body)
            ns = {"s3": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
            find = (lambda el, tag: el.find(f"s3:{tag}", ns)) if ns else (
                lambda el, tag: el.find(tag))
            for item in (root.findall("s3:Contents", ns) if ns else root.findall("Contents")):
                found[find(item, "Key").text] = int(find(item, "Size").text)
            truncated = find(root, "IsTruncated")
            if truncated is None or truncated.text != "true":
                return found
            token = find(root, "NextContinuationToken").text
