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

    listing(bucket, prefix, max_age=LISTING_MAX_AGE_S)
        -> ({key: Listed(size, etag, last_modified)}, None) | (None, detail)
        The keys under one prefix, with their sizes, ETags and last-modified times (ISO 8601
        text as the CLI prints it, such as "2026-11-23T10:00:00+00:00"; the form varies with
        the CLI version, so parse it before comparing), held in memory for `max_age`
        seconds (about a minute by default; 0 asks the bucket every time and holds nothing). A
        listing carries no user metadata, so it gives sizes and never a SHA-256: it is what a
        membership check reads. A prefix of more than LIST_MAX_PAGES pages is an error.

    head(bucket, key) -> (Head(size, sha256, etag), None)   the object is there
                       | (None, None)                       the bucket answered: no such object
                       | (None, detail)                     no answer (timeout, credentials, ...)
        One `head-object`. `sha256` is "" when the object carries no SHA-256 metadata.

    fetch_verified(bucket, key, dest, size=None, sha256=None, require_sha256=True,
                   staging_dir=None)
        -> None on success, else the reason it refused.
        What a size or hash not passed would check against comes from one `head` of the object.
        By default an object with no SHA-256 metadata is refused, so no object is accepted on its
        size alone; `require_sha256=False` is for older objects written before `put` carried the
        hash, which are then checked by size, and by hash where they do carry one. The bytes land
        in a `.part` file beside `dest` (or in `staging_dir`, when given) and are renamed into
        place only after the checks pass; the `.part` file is always removed.

    fetch_small(bucket, key, dest, size, staging_dir=None) -> None | the reason it refused
        A small object (a thumbnail, a sidecar) checked by its size alone: no head, and the copy
        runs under the "small" timeout. An object over SMALL_FETCH_MAX_BYTES is refused before
        any call; it belongs to `fetch_verified`.

    put(bucket, path, key, meta=None) -> (Head, None) | (None, Failure)
        Uploads `path` with its SHA-256 stored as object metadata (always computed here, from
        the file being uploaded; never taken from the caller), then heads the object and
        requires the size, the hash and the ETag to match. The ETag is what proves the bytes:
        see `etag_matches`. `meta` adds other user-metadata pairs, each value percent-encoded
        (S3 metadata is ASCII, and the CLI's `name=value,...` form splits on commas). A
        failure is a `Failure`: the detail text as a `str`, with `.kind` saying whether this
        call may have left an object: NOT_COPIED (it sent nothing), NOT_PROVED (it may have,
        unproved) or DIFFERS (it did, and the bytes are proved wrong).

    run(bucket, args, kind) -> (stdout, None) | (None, detail)
        The one command runner: builds and runs one `aws` command for a bucket value under the
        timeout of its kind ("copy", "head", "list" or "small"). The functions above call it, and so
        may a caller that needs a raw `s3`/`s3api` call this module does not offer.

Every ETag this module returns (`head`, `listing`, `put`) is bare: S3's surrounding double
quotes are taken off. `quoted_etag` puts them back for a record that keeps the quoted form,
and `bare_etag` takes them off one read from elsewhere, so the two forms compare.

Why a SHA-256 in the metadata: a multipart upload's ETag is not the file's MD5, so the bucket's
own checksum cannot prove a file. The uploader computes the hash and stores it on the object;
only `head-object` returns it (a listing does not).
"""

import collections
import contextlib
import dataclasses
import errno
import glob
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

# The one seam through which every aws invocation passes: swappable in tests.
_run = subprocess.run

_lock = threading.Lock()        # guards the module's in-memory state

# One timeout for each kind of call, defined here once. A copy moves a whole file (a two-hour
# capture is a few hundred MB), so it gets minutes; a head and one page of a listing are a
# metadata round trip each.
TIMEOUTS = {"copy": 600, "head": 60, "list": 60, "small": 60}

# The largest object `fetch_small` copies under the "small" timeout.
SMALL_FETCH_MAX_BYTES = 16 * 1024 * 1024

LISTING_MAX_AGE_S = 60          # how long a listing is held in memory by default

# Keys asked for per `list-objects-v2` call. With `--max-items` set, the CLI stops after that
# many keys and prints a NextToken to resume from, so each command is one page and the list
# timeout bounds one page, never a whole bucket (without it, the CLI pages through
# everything inside one process).
LIST_PAGE_KEYS = 1000

# The most pages one listing reads (ten million keys at LIST_PAGE_KEYS a page). A bucket that
# keeps answering with a NextToken past this is an error, never a loop without end.
LIST_MAX_PAGES = 10000

# The part size the aws CLI uses for a multipart upload unless its own configuration says
# otherwise (`multipart_chunksize`), and the size from which it uploads in parts
# (`multipart_threshold`). Both are the CLI's defaults, 8 MiB. Another part size is still
# proved (`etag_matches`); this one is only the first tried, from the digests already held.
MULTIPART_PART_BYTES = 8 * 1024 * 1024

MIB = 1024 * 1024
MIN_PART_BYTES = 5 * MIB            # S3's smallest part (every part but the last)
MAX_PART_BYTES = 5 * 1024 * MIB     # S3's largest part
MAX_PARTS = 10000                   # S3's most parts in one upload

# The most other part sizes `etag_matches` hashes for one upload. Each costs one MD5 over the
# file, all of them in a single read, so the check costs at most this many MD5 passes and one
# SHA-256 pass.
MAX_PART_SIZE_CANDIDATES = 16

# The user-metadata name the hash is stored under. S3 lowercases user metadata names, and the
# lookup in `head` ignores case, so another CLI version cannot turn a hash into a miss.
SHA256_META = "sha256"

Listed = collections.namedtuple("Listed", "size etag last_modified", defaults=(None,))
Head = collections.namedtuple("Head", "size sha256 etag")


class BucketError(ValueError):
    """A bucket value that cannot be defined: no name, or no `aws` CLI to be found."""


# Whether a failed `put` may have left an object. The kind is what a caller acts on; the text
# is for people.
NOT_COPIED = "not_copied"       # nothing was sent: the file could not be read, so this call
                                # left no object (one already at the key is still there)
NOT_PROVED = "not_proved"       # the copy command ran: an object may be there, and its bytes
                                # are not proved either way (this covers a copy that failed)
DIFFERS = "differs"             # copied, and the bucket's object is proved not to be the file
FAILURE_KINDS = (NOT_COPIED, NOT_PROVED, DIFFERS)


class Failure(str):
    """A failed `put`'s detail text, with `.kind` (one of FAILURE_KINDS) beside it. It is a
    `str`, so a caller that prints the detail, searches it or tests it for truth works as
    before."""

    def __new__(cls, kind, detail):
        if kind not in FAILURE_KINDS:
            raise ValueError("unknown failure kind %r" % kind)
        obj = super().__new__(cls, detail)
        obj.kind = kind
        return obj

    @property
    def detail(self):
        return str(self)

    def __reduce__(self):
        return (Failure, (self.kind, str(self)))


def bare_etag(etag):
    """An ETag without S3's surrounding double quotes (the form every return here uses), or
    None for an empty one."""
    return (etag or "").strip().strip('"') or None


def quoted_etag(etag):
    """An ETag in S3's quoted form, `"<etag>"`, as the raw API prints it; None stays None."""
    bare = bare_etag(etag)
    return '"%s"' % bare if bare else None


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
        detail = ((proc.stderr or "").strip() or (proc.stdout or "").strip()
                  or "aws exited %d" % proc.returncode)
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


def _multipart_etag(part_md5s):
    return "%s-%d" % (hashlib.md5(b"".join(part_md5s)).hexdigest(), len(part_md5s))


def expected_etag(size, got):
    """The ETag of `size` bytes with Digests `got` uploaded in the CLI's default 8 MiB parts."""
    if size <= MULTIPART_PART_BYTES:
        return got.md5
    return _multipart_etag(got.part_md5s)


def _all_part_size_candidates(size, parts):
    """Every whole-MiB part size `part_size_candidates` chooses from, ranked, before the cap."""
    if parts < 2 or parts > MAX_PARTS:
        return []
    low = max(MIN_PART_BYTES // MIB, -(-size // (parts * MIB)))
    high = min(MAX_PART_BYTES // MIB, -(-size // ((parts - 1) * MIB)) - 1)
    found = [m * MIB for m in range(low, high + 1)
             if m * MIB != MULTIPART_PART_BYTES and -(-size // (m * MIB)) == parts]

    def rank(p):
        m = p // MIB
        return (p & (p - 1) != 0, bool(m % 8 and m % 10 and m % 25), p)
    found.sort(key=rank)
    return found


def part_size_candidates(size, parts):
    """The part sizes, in bytes, that could have built a `parts`-part ETag for a file of `size`
    bytes: whole MiB from MIN_PART_BYTES up to MAX_PART_BYTES that split the file into exactly
    `parts` parts. The default part size is left out (the caller has tried it already). At most
    MAX_PART_SIZE_CANDIDATES come back: the powers of two first, as the sizes a configuration
    most often sets, then multiples of 8, 10 and 25 MiB, then the rest from the smallest up.

    The count is small where it matters. Exactly `parts` parts means a part size between
    size/parts and size/(parts - 1), a range of about size / (parts * (parts - 1)) bytes: under
    one MiB for a 300 MB file in 19 parts, so one or two candidates. Only a file of a few parts
    has a wide range, and the cap bounds that."""
    return _all_part_size_candidates(size, parts)[:MAX_PART_SIZE_CANDIDATES]


def _etags_for_part_sizes(path, part_sizes):
    """({part size: multipart ETag}, the SHA-256 of the bytes read) for each part size, from
    one read of the file. Every part size is whole MiB, so each part boundary falls on a block
    boundary; each block feeds one running MD5 per part size, and the one SHA-256."""
    sha = hashlib.sha256()
    running = {p: hashlib.md5() for p in part_sizes}
    done = {p: [] for p in part_sizes}
    offset = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(MIB)
            if not block:
                break
            offset += len(block)
            sha.update(block)
            for p in part_sizes:
                running[p].update(block)
                if offset % p == 0:
                    done[p].append(running[p].digest())
                    running[p] = hashlib.md5()
    for p in part_sizes:
        if offset % p:
            done[p].append(running[p].digest())
    return {p: _multipart_etag(done[p]) for p in part_sizes}, sha.hexdigest()


def etag_matches(etag, size, got, path):
    """True when the bucket's `etag` is the one these bytes give under some upload the CLI can
    make. `got` is the file's Digests; `path` is read again only for a part size other than the
    default.

    - No `-N` suffix: the ETag is the MD5 of the whole file.
    - `-1`: one part, so the MD5 of the file's MD5. The CLI uploads a file of exactly the
      threshold size this way.
    - `-N`, N over 1: the default part size is tried first from the digests already held, then
      each whole-MiB part size that gives exactly N parts (`part_size_candidates`). Any one that
      gives the same ETag proves the bytes, since a part size only changes how they were cut.
      That second read counts only when its SHA-256 equals `got.sha256`, so a file changed
      since its digests were taken proves nothing."""
    return etag_verdict(etag, size, got, path) is None


def etag_verdict(etag, size, got, path):
    """None when `etag_matches`, else the Failure kind a mismatch means.

    DIFFERS when the check was complete: a plain MD5 or a `-1` ETag that does not match, or an
    `-N` ETag where every whole-MiB part size giving N parts was tried (none was cut by the
    cap) and at least one was possible. NOT_PROVED otherwise: no ETag, one this module cannot
    read, a file that could not be read again or changed since its digests, a candidate list
    the cap cut short, or a part count no whole-MiB part size gives (a part size set in raw
    bytes cannot be ruled out)."""
    return _etag_check(etag, size, got, path)[0]


def _etag_check(etag, size, got, path):
    """(None, "") on a match, else (the Failure kind, why in a few words)."""
    if not etag:
        return NOT_PROVED, "the bucket gave no ETag"
    if "-" not in etag:
        return (None, "") if etag == got.md5 else (DIFFERS, "the MD5 differs")
    _body, _, count = etag.rpartition("-")
    try:
        parts = int(count)
    except ValueError:
        return NOT_PROVED, "an ETag this check cannot read"
    if parts == 1:
        if etag == _multipart_etag([bytes.fromhex(got.md5)]):
            return None, ""
        return DIFFERS, "the one-part ETag differs"
    default_parts = parts == len(got.part_md5s)
    if default_parts and etag == _multipart_etag(got.part_md5s):
        return None, ""
    every = _all_part_size_candidates(size, parts)
    candidates = every[:MAX_PART_SIZE_CANDIDATES]
    if candidates:
        try:
            etags, sha256 = _etags_for_part_sizes(path, candidates)
        except OSError as exc:
            return NOT_PROVED, "the file could not be read again: %s" % exc
        if sha256 != got.sha256:
            return NOT_PROVED, "the file changed during the upload"
        if etag in etags.values():
            return None, ""
    if len(every) > len(candidates):
        return NOT_PROVED, "%d of %d part sizes for %d parts were tried" % (
            len(candidates), len(every), parts)
    if not (candidates or default_parts):
        return NOT_PROVED, "no whole-MiB part size from %d MiB gives %d parts" % (
            MIN_PART_BYTES // MIB, parts)
    return DIFFERS, "no part size for %d parts gives its ETag" % parts


# --- listing -----------------------------------------------------------------------

_listings = {}                  # (bucket, prefix) -> (monotonic time, {key: Listed})
_listing_locks = {}             # (bucket, prefix) -> Lock: one LIST at a time per prefix


def _list_now(bucket, prefix):
    objects, token = {}, None
    for _page in range(LIST_MAX_PAGES):
        args = ["s3api", "list-objects-v2", "--bucket", bucket.name, "--prefix", prefix,
                "--max-items", str(LIST_PAGE_KEYS), "--page-size", str(LIST_PAGE_KEYS),
                "--query", "[Contents[].[Key, Size, ETag, LastModified], NextToken]",
                "--output", "json"]
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
            modified = entry[3] if len(entry) > 3 and isinstance(entry[3], str) else None
            if isinstance(key, str) and isinstance(size, int):
                objects[key] = Listed(size, bare_etag(etag), modified or None)
        if not token:
            return objects, None
    return None, "listing of %r stopped after %d pages: the bucket kept giving a NextToken" % (
        prefix, LIST_MAX_PAGES)


def listing(bucket, prefix, max_age=LISTING_MAX_AGE_S):
    """{key: Listed(size, etag, last_modified)} for every object under `prefix` (keys in full),
    the ETag bare and `last_modified` the CLI's ISO 8601 text (None if it gave none), or
    (None, detail) when the bucket did not answer. A failed listing is never held, so the next
    call asks again; a caller must treat "unknown" differently from "empty". With `max_age` 0
    (or less) the bucket is asked and nothing is held: no listing and no lock per prefix. A
    listing already held for the prefix stays as it was, so a later call with the default age
    can still read the older one."""
    if max_age <= 0:
        return _list_now(bucket, prefix)
    slot = (bucket, prefix)
    with _lock:
        lock = _listing_locks.setdefault(slot, threading.Lock())
    with lock:
        with _lock:
            held = _listings.get(slot)
        if held and time.monotonic() - held[0] < max_age:
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
    return Head(size, sha, bare_etag(meta.get("ETag"))), None


# --- fetch -------------------------------------------------------------------------

def fetch_verified(bucket, key, dest, size=None, sha256=None, require_sha256=True,
                   staging_dir=None):
    """Copy object `key` to `dest`, checked before it is renamed into place. Returns None, or
    the reason it refused. See the module docstring for the rules. `staging_dir` is where the
    `.part` is made and checked (default: beside `dest`); a staging directory on another file
    system costs one more copy, made beside `dest` and renamed, so `dest` still appears whole."""
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
    return _fetch(bucket, key, dest, size, sha256, "copy", staging_dir)


def fetch_small(bucket, key, dest, size, staging_dir=None):
    """Copy a small object `key` to `dest`, checked by `size` alone, with no head and under
    the "small" timeout. Returns None, or the reason it refused. For an object that is only
    a convenience (a thumbnail), where its size is known from a listing; anything that must
    be proved goes through `fetch_verified`."""
    if not isinstance(size, int) or size < 0:
        return "refused: fetch_small needs the object's size for %s" % key
    if size > SMALL_FETCH_MAX_BYTES:
        return "refused: %s is %d bytes, over the %d a small fetch takes" % (
            key, size, SMALL_FETCH_MAX_BYTES)
    return _fetch(bucket, key, dest, size, None, "small", staging_dir)


def _fetch(bucket, key, dest, size, sha256, kind, staging_dir):
    """The copy both fetches share: into a `.part`, checked, then renamed onto `dest`."""
    folder = os.path.dirname(os.path.abspath(dest))
    stage = os.path.abspath(staging_dir) if staging_dir else folder
    try:
        os.makedirs(folder, exist_ok=True)
        os.makedirs(stage, exist_ok=True)
        # A unique name per call, in dest's own directory by default so the rename stays
        # atomic. It ends `.part`, the cache policy's mark for a write in progress, so an
        # eviction run does not take a download out from under its own rename.
        fd, part = tempfile.mkstemp(dir=stage, prefix=os.path.basename(dest) + ".",
                                    suffix=".part")
        os.close(fd)
    except OSError as exc:
        return str(exc)
    moved = None
    try:
        _out, err = run(bucket, ["s3", "cp", bucket.url(key), part, "--no-progress"], kind)
        if err is not None:
            return err
        got_size = os.path.getsize(part)
        if got_size != size:
            return "size mismatch: bucket object %s, expected %s" % (got_size, size)
        if sha256 and _digest_file(part).sha256 != sha256:
            return "sha256 mismatch: %s" % key
        source = part
        while True:
            try:
                os.replace(source, dest)
                return None
            except OSError as exc:
                if exc.errno != errno.EXDEV or moved:
                    # Another fetch of the same object may have won the race. A complete copy
                    # of the right size is the right copy; anything else is a failure.
                    if os.path.isfile(dest) and os.path.getsize(dest) == size:
                        return None
                    return str(exc)
            # The staging directory is on another file system: copy beside dest first, so
            # dest itself only ever appears by a rename. A failure of this copy is a
            # failure, whatever sits at dest.
            fd, moved = tempfile.mkstemp(dir=folder, prefix=os.path.basename(dest) + ".",
                                         suffix=".part")
            os.close(fd)
            shutil.copyfile(part, moved)
            source = moved
    except OSError as exc:
        return str(exc)
    finally:
        # The aws CLI writes a download to its own temporary name beside the target
        # (`<target>.<random>`) and renames it at the end, so a copy killed at its timeout
        # leaves that file behind. Its name does not end `.part`, so nothing else would ever
        # take it away.
        for leftover in [part, moved] + glob.glob(glob.escape(part) + ".*"):
            if leftover:
                with contextlib.suppress(OSError):
                    os.remove(leftover)     # a no-op after the rename


# --- put ---------------------------------------------------------------------------

def _meta_arg(digest, meta):
    """The `--metadata` value: the hash first, then each other pair, names lowercased (S3
    lowercases them anyway) and values percent-encoded, so a comma, an equals sign or a
    character outside ASCII in a title cannot split or break the pair."""
    pairs = [(SHA256_META, digest)]
    for name, value in sorted((meta or {}).items()):
        name = str(name).strip().lower()
        if name and name != SHA256_META and all(c.isalnum() or c in "-_" for c in name):
            pairs.append((name, urllib.parse.quote(str(value), safe=" ")))
    return ",".join("%s=%s" % pair for pair in pairs)


def put(bucket, path, key, meta=None):
    """Upload `path` as `key` with its SHA-256 stored as object metadata, then head the object
    and require the size and the hash to match. Returns (Head, None), or (None, Failure): the
    detail text, with `.kind` saying whether this call may have left an object: NOT_COPIED
    (it sent nothing), NOT_PROVED (it may have, unproved) or DIFFERS (it did, and the bytes are
    proved wrong).

    The hash is always computed here, from the file being uploaded: a hash handed in could be
    stale or wrong, and the metadata is what every later fetch trusts. It is read once per
    unchanged file (`digests`), so a caller that asked for `sha256_file` first costs no second
    read. `meta` adds other user-metadata pairs (`_meta_arg`); a name that is not letters,
    digits, `-` and `_`, or that would replace the hash, is dropped.

    The SHA-256 the head returns is only the metadata this call sent, echoed back; the ETag is
    the bucket's own account of the bytes. A single-part object's ETag is its MD5. A multipart
    object's is the MD5 of its parts' MD5s, which depends on the part size the CLI chose:
    `etag_matches` accepts a whole-MiB part size (the likeliest MAX_PART_SIZE_CANDIDATES are
    tried) that gives the same number of parts and the same ETag. An ETag no such part size
    explains is a refusal, never a pass it cannot prove: DIFFERS when the check was complete,
    NOT_PROVED when it was not (`etag_verdict`)."""
    try:
        local = os.path.getsize(path)
        got_digests = digests(path)
        digest = got_digests.sha256
    except OSError as exc:
        return None, Failure(NOT_COPIED, str(exc))
    _out, err = run(bucket, ["s3", "cp", path, bucket.url(key), "--no-progress",
                             "--metadata", _meta_arg(digest, meta)], "copy")
    _forget_listings(bucket, key)       # even a failed copy may have left an object
    if err is not None:
        return None, Failure(NOT_PROVED, err)
    got, err = head(bucket, key)
    if got is None:
        return None, Failure(NOT_PROVED,
                             err or "the object is not there after the upload: %s" % key)
    if got.size != local:
        return None, Failure(DIFFERS, "size mismatch: local %s, bucket %s" % (local, got.size))
    if got.sha256 != digest:
        return None, Failure(DIFFERS, "sha256 mismatch after the upload: %s" % key)
    verdict, why = _etag_check(got.etag, local, got_digests, path)
    if verdict is None:
        return got, None
    if verdict == DIFFERS:
        return None, Failure(DIFFERS, "etag mismatch after the upload: %s (the bytes differ: "
                             "%s)" % (key, why))
    return None, Failure(NOT_PROVED, "cannot prove the bytes of %s: %s" % (key, why))
