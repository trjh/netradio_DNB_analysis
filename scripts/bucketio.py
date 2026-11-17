"""One S3-compatible bucket client: list a prefix, look at one object, fetch one object safely,
and upload one with its SHA-256.

Every bucket call in this repo goes through this module. It speaks S3 through the `aws` CLI, an
external tool like ffmpeg, so the repo keeps its zero-pip-dependency posture for storage too.

A caller reads its own settings and passes a bucket value in; this module reads no setting and
knows nothing of any caller's caches. The cache rules (a lock per name, pinning, `reserve` before
a write) stay with the callers.

The interface:

    Bucket(name, endpoint="", profile="", aws_cli="")
        A bucket value. Checked when it is defined, on its fields only: the name is set, and an
        `aws` CLI is found (at the path or command name the caller gives, else `aws` on the
        PATH). Raises BucketError otherwise. Defining one runs no command: the first real call
        reports any trouble with the bucket itself. An empty endpoint or profile adds no flag,
        so the CLI's own default applies.

    listing(bucket, prefix, max_age=LISTING_MAX_AGE_S) -> ({key: Listed(size, etag)}, None)
                                                        | (None, detail)
        The keys under one prefix, with their sizes and ETags, held in memory for `max_age`
        seconds (about a minute by default; 0 asks the bucket every time). A listing carries no
        user metadata, so it gives sizes and never a SHA-256: it is what a membership check reads.

    head(bucket, key) -> (Head(size, sha256, etag), None)   the object is there
                       | (None, None)                       the bucket answered: no such object
                       | (None, detail)                     no answer (timeout, credentials, ...)
        One `head-object`. `sha256` is "" when the object carries no SHA-256 metadata.

    fetch_verified(bucket, key, dest, size=None, sha256=None, require_sha256=True)
        -> None on success, else the reason it refused.
        What a size or hash not passed would check against comes from one `head` of the object.
        By default an object with no SHA-256 metadata is refused, so no object is accepted on its
        size alone; `require_sha256=False` is for older objects written before `put` carried the
        hash, which are then checked by size, and by hash where they do carry one. The bytes land
        in a `.part` file beside `dest` and are renamed into place only after the checks pass;
        the `.part` file is always removed.

    put(bucket, path, key) -> (Head, None) | (None, detail)
        Uploads `path` with its SHA-256 stored as object metadata (always computed here, from
        the file being uploaded; never taken from the caller), then heads the object and
        requires the size, the hash and the ETag to match. The ETag is what proves the bytes:
        see `expected_etag`.

    run(bucket, args, kind) -> (stdout, None) | (None, detail)
        The one command runner: builds and runs one `aws` command for a bucket value under the
        timeout of its kind ("copy", "head" or "list"). The four functions above call it, and so
        may a caller that needs a raw `s3`/`s3api` call this module does not offer.

Why a SHA-256 in the metadata: a multipart upload's ETag is not the file's MD5, so the bucket's
own checksum cannot prove a file. The uploader computes the hash and stores it on the object;
only `head-object` returns it (a listing does not).
"""

import collections
import contextlib
import dataclasses
import glob
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

# The one seam through which every aws invocation passes: swappable in tests.
_run = subprocess.run

_lock = threading.Lock()        # guards the module's in-memory state

# One timeout for each kind of call, defined here once. A copy moves a whole file (a two-hour
# capture is a few hundred MB), so it gets minutes; a head and one page of a listing are a
# metadata round trip each.
TIMEOUTS = {"copy": 600, "head": 60, "list": 60}

LISTING_MAX_AGE_S = 60          # how long a listing is held in memory by default

# Keys asked for per `list-objects-v2` call. With `--max-items` set, the CLI stops after that
# many keys and prints a NextToken to resume from, so each command is one page and the list
# timeout bounds one page, never a whole bucket (without it, the CLI pages through
# everything inside one process).
LIST_PAGE_KEYS = 1000

# The part size the aws CLI uses for a multipart upload unless its own configuration says
# otherwise (`multipart_chunksize`), and the size above which it uploads in parts
# (`multipart_threshold`). Both are the CLI's defaults, 8 MiB.
MULTIPART_PART_BYTES = 8 * 1024 * 1024

# The user-metadata name the hash is stored under. S3 lowercases user metadata names, and the
# lookup in `head` ignores case, so another CLI version cannot turn a hash into a miss.
SHA256_META = "sha256"

Listed = collections.namedtuple("Listed", "size etag")
Head = collections.namedtuple("Head", "size sha256 etag")


class BucketError(ValueError):
    """A bucket value that cannot be defined: no name, or no `aws` CLI to be found."""


@dataclasses.dataclass(frozen=True)
class Bucket:
    name: str
    endpoint: str = ""
    profile: str = ""
    aws_cli: str = ""
    cli: str = dataclasses.field(init=False, default="")    # the resolved executable

    def __post_init__(self):
        name = (self.name or "").strip()
        if not name:
            raise BucketError("no bucket name")
        wanted = (self.aws_cli or "").strip()
        cli = shutil.which(wanted) if wanted else shutil.which("aws")
        if not cli:
            raise BucketError("no aws CLI found at %r" % wanted if wanted
                              else "no aws CLI found on the PATH")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "endpoint", (self.endpoint or "").strip())
        object.__setattr__(self, "profile", (self.profile or "").strip())
        object.__setattr__(self, "cli", cli)

    def argv(self, args):
        """The command line for one call: the CLI, the endpoint and profile when set, then the
        call's own arguments. No credentials ever go on the command line: the profile (or the
        CLI's default credential chain) holds them."""
        cmd = [self.cli]
        if self.endpoint:
            cmd += ["--endpoint-url", self.endpoint]
        if self.profile:
            cmd += ["--profile", self.profile]
        return cmd + list(args)

    def url(self, key):
        return "s3://%s/%s" % (self.name, key)


def run(bucket, args, kind):
    """Run one aws invocation. Returns (stdout, None) on success, (None, detail) on failure.

    Every failure collapses to a short detail string: a missing binary (OSError), a hung call
    (TimeoutExpired) and a refusal from the bucket (a non-zero exit, with the tail of its
    stderr) all mean "the call did not happen, and this is why"."""
    timeout = TIMEOUTS[kind]
    try:
        proc = _run(bucket.argv(args), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)[:200] or type(exc).__name__
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "aws exited %d" % proc.returncode).strip()
        return None, detail[-200:]
    return (proc.stdout or ""), None


_HEAD_404 = "(404) when calling the HeadObject operation"


def bucket_said_no(detail):
    """True when a failed head is the bucket's answer (the object is not there) rather than no
    answer at all. The CLI reports a missing object as exactly "An error occurred (404) when
    calling the HeadObject operation: Not Found", and only that text counts; a timeout, a
    refused connection, a 403 from bad credentials, a missing CLI or any other error says
    nothing about the object. A HEAD carries no error body, so a bucket that does not exist
    answers the same 404; the caller's bucket value is what names the bucket."""
    return _HEAD_404 in detail


Digests = collections.namedtuple("Digests", "sha256 md5 part_md5s")

_digests = collections.OrderedDict()    # (realpath, size, mtime_ns) -> Digests
_DIGESTS_HELD = 256


def _digest_file(path):
    """One pass over a file: its SHA-256, its MD5, and the MD5 of each MULTIPART_PART_BYTES
    part (what a multipart ETag is built from)."""
    sha, md5, parts = hashlib.sha256(), hashlib.md5(), []
    with open(path, "rb") as fh:
        while True:
            part = fh.read(MULTIPART_PART_BYTES)
            if not part:
                break
            sha.update(part)
            md5.update(part)
            parts.append(hashlib.md5(part).digest())
    return Digests(sha.hexdigest(), md5.hexdigest(), tuple(parts))


def digests(path):
    """The file's Digests, read once per (path, size, mtime): a file asked about again, unchanged
    since, is not read again. A changed file has a new size or mtime, so it is read afresh."""
    st = os.stat(path)
    slot = (os.path.realpath(path), st.st_size, st.st_mtime_ns)
    with _lock:
        if slot in _digests:
            _digests.move_to_end(slot)
            return _digests[slot]
    got = _digest_file(path)
    with _lock:
        _digests[slot] = got
        while len(_digests) > _DIGESTS_HELD:
            _digests.popitem(last=False)
    return got


def sha256_file(path):
    """The SHA-256 of a file, as lowercase hex (held as `digests` holds it)."""
    return digests(path).sha256


def expected_etag(size, got):
    """The ETag the bucket gives an object holding exactly these bytes, as the aws CLI uploads
    them with its default settings: the plain MD5 for a file at or under the multipart
    threshold, else the MD5 of the parts' MD5s followed by `-<number of parts>`."""
    if size <= MULTIPART_PART_BYTES:
        return got.md5
    return "%s-%d" % (hashlib.md5(b"".join(got.part_md5s)).hexdigest(), len(got.part_md5s))


# --- listing -----------------------------------------------------------------------

_listings = {}                  # (bucket, prefix) -> (monotonic time, {key: Listed})
_listing_locks = {}             # (bucket, prefix) -> Lock: one LIST at a time per prefix


def _list_now(bucket, prefix):
    objects, token = {}, None
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket.name, "--prefix", prefix,
                "--max-items", str(LIST_PAGE_KEYS), "--page-size", str(LIST_PAGE_KEYS),
                "--query", "[Contents[].[Key, Size, ETag], NextToken]", "--output", "json"]
        if token:
            args += ["--starting-token", token]
        out, err = run(bucket, args, "list")
        if err is not None:
            return None, err
        try:
            contents, token = json.loads(out or "[[], null]")
        except (ValueError, TypeError):
            return None, "unreadable list-objects-v2 output"
        for entry in contents or []:
            try:
                key, size, etag = entry[0], entry[1], entry[2]
            except (TypeError, IndexError):
                continue            # a shape the S3 API does not describe: skip it
            if isinstance(key, str) and isinstance(size, int):
                objects[key] = Listed(size, (etag or "").strip('"') or None)
        if not token:
            return objects, None


def listing(bucket, prefix, max_age=LISTING_MAX_AGE_S):
    """{key: Listed(size, etag)} for every object under `prefix` (keys in full), or
    (None, detail) when the bucket did not answer. A failed listing is never held, so the next
    call asks again; a caller must treat "unknown" differently from "empty"."""
    slot = (bucket, prefix)
    with _lock:
        lock = _listing_locks.setdefault(slot, threading.Lock())
    with lock:
        with _lock:
            held = _listings.get(slot)
        if held and max_age > 0 and time.monotonic() - held[0] < max_age:
            return dict(held[1]), None
        objects, err = _list_now(bucket, prefix)
        if err is not None:
            return None, err
        with _lock:
            _listings[slot] = (time.monotonic(), objects)
        return dict(objects), None


def _forget_listings(bucket, key):
    """A put changes what a listing of any prefix of `key` would say: drop those."""
    with _lock:
        for slot in [s for s in _listings if s[0] == bucket and key.startswith(s[1])]:
            del _listings[slot]


# --- head --------------------------------------------------------------------------

def head(bucket, key):
    """One `head-object`: (Head, None), (None, None) for no such object, or (None, detail) when
    the bucket did not answer."""
    out, err = run(bucket, ["s3api", "head-object", "--bucket", bucket.name, "--key", key,
                            "--output", "json"], "head")
    if err is not None:
        return None, (None if bucket_said_no(err) else err)
    try:
        meta = json.loads(out or "{}")
    except ValueError:
        return None, "unreadable head-object output"
    size = meta.get("ContentLength") if isinstance(meta, dict) else None
    if not isinstance(size, int):
        return None, "head-object gave no size"
    sha = ""
    for name, value in (meta.get("Metadata") or {}).items():
        if name.lower() == SHA256_META:
            sha = (value or "").strip().lower()
    return Head(size, sha, (meta.get("ETag") or "").strip('"') or None), None


# --- fetch -------------------------------------------------------------------------

def fetch_verified(bucket, key, dest, size=None, sha256=None, require_sha256=True):
    """Copy object `key` to `dest`, checked before it is renamed into place. Returns None, or
    the reason it refused. See the module docstring for the rules."""
    if sha256 is not None and not sha256.strip():
        return "refused: the caller passed an empty sha256 for %s" % key
    if size is None or sha256 is None:
        got, err = head(bucket, key)
        if got is None:
            return err or "no such object: %s" % key
        if size is None:
            size = got.size
        if sha256 is None:
            sha256 = got.sha256 or None
    if sha256:
        sha256 = sha256.strip().lower()
    if require_sha256 and not sha256:
        return "no sha256 metadata on the object: %s" % key
    folder = os.path.dirname(os.path.abspath(dest))
    try:
        os.makedirs(folder, exist_ok=True)
        # A unique name per call, in dest's own directory so the rename stays atomic. It ends
        # `.part`, the cache policy's mark for a write in progress, so an eviction run does not
        # take a download out from under its own rename.
        fd, part = tempfile.mkstemp(dir=folder, prefix=os.path.basename(dest) + ".",
                                    suffix=".part")
        os.close(fd)
    except OSError as exc:
        return str(exc)
    try:
        _out, err = run(bucket, ["s3", "cp", bucket.url(key), part, "--no-progress"], "copy")
        if err is not None:
            return err
        got_size = os.path.getsize(part)
        if got_size != size:
            return "size mismatch: bucket object %s, expected %s" % (got_size, size)
        if sha256 and _digest_file(part).sha256 != sha256:
            return "sha256 mismatch: %s" % key
        try:
            os.replace(part, dest)
        except OSError as exc:
            # Another fetch of the same object may have won the race. A complete copy of the
            # right size is the right copy; anything else is a failure.
            if os.path.isfile(dest) and os.path.getsize(dest) == size:
                return None
            return str(exc)
        return None
    except OSError as exc:
        return str(exc)
    finally:
        # The aws CLI writes a download to its own temporary name beside the target
        # (`<target>.<random>`) and renames it at the end, so a copy killed at its timeout
        # leaves that file behind. Its name does not end `.part`, so nothing else would ever
        # take it away.
        for leftover in [part] + glob.glob(glob.escape(part) + ".*"):
            with contextlib.suppress(OSError):
                os.remove(leftover)     # `part` itself is a no-op after the rename


# --- put ---------------------------------------------------------------------------

def put(bucket, path, key):
    """Upload `path` as `key` with its SHA-256 stored as object metadata, then head the object
    and require the size and the hash to match. Returns (Head, None), or (None, detail).

    The hash is always computed here, from the file being uploaded: a hash handed in could be
    stale or wrong, and the metadata is what every later fetch trusts. It is read once per
    unchanged file (`digests`), so a caller that asked for `sha256_file` first costs no second
    read.

    The SHA-256 the head returns is only the metadata this call sent, echoed back; the ETag is
    the bucket's own account of the bytes. A single-part object's ETag is its MD5. A multipart
    object's is the MD5 of its parts' MD5s, which depends on the part size: the check assumes
    the CLI's default part size, and a CLI configured with another one gets a refusal that says
    so, never a pass it cannot prove."""
    try:
        local = os.path.getsize(path)
        got_digests = digests(path)
        digest = got_digests.sha256
    except OSError as exc:
        return None, str(exc)
    _out, err = run(bucket, ["s3", "cp", path, bucket.url(key), "--no-progress",
                             "--metadata", "%s=%s" % (SHA256_META, digest)], "copy")
    if err is not None:
        return None, err
    _forget_listings(bucket, key)
    got, err = head(bucket, key)
    if got is None:
        return None, err or "the object is not there after the upload: %s" % key
    if got.size != local:
        return None, "size mismatch: local %s, bucket %s" % (local, got.size)
    if got.sha256 != digest:
        return None, "sha256 mismatch after the upload: %s" % key
    want = expected_etag(local, got_digests)
    if got.etag != want:
        if local > MULTIPART_PART_BYTES and got.etag and "-" in got.etag \
                and got.etag.rsplit("-", 1)[1] != want.rsplit("-", 1)[1]:
            return None, ("cannot prove the bytes of %s: uploaded in %s parts, not the %s of "
                          "the CLI's default part size" % (key, got.etag.rsplit("-", 1)[1],
                                                            want.rsplit("-", 1)[1]))
        return None, "etag mismatch after the upload: %s (the bytes differ)" % key
    return got, None
