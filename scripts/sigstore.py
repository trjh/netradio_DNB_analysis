"""The signature store: an S3-compatible bucket as the pool's ONLY long-term home.

Chroma signatures used to live forever in `.chroma-cache/`. With the store configured, the
bucket is authoritative and the local dir demotes to a WORKING CACHE: a signature is on disk
only while it is being made, being scored, or has been pulled back for a re-score — and is
EVICTED (deleted locally) once it is (a) verified present in the bucket and (b) fully scored
against every current mystery. Space on this machine is for work in progress, not for the
archive.

Rules that keep this safe:

  * **Never evict what isn't verified remote.** "Uploaded" means a HEAD of the object returns
    the local file's exact size — not that a put command exited 0 once. A put also stores the
    file's SHA-256 as object metadata and checks it; signatures uploaded before that carry
    none, so the eviction check stays the size.
  * **Never evict what is still wanted.** Fully-scored is judged against the CURRENT mystery
    set; a new or re-cut mystery makes old signatures wanted again, and `fetch()` brings any of
    them back on demand (a signature is ~55KB; re-fetching it is nothing).
  * **Dark unless configured** (`NETRADIO_SIG_BUCKET`), like every optional integration; with
    the store dark, nothing is ever deleted and the harvester behaves exactly as before.

Every bucket call goes through `bucketio`, the repo's one bucket client, which speaks S3
through the aws CLI. This module reads the signature bucket's settings and passes the bucket
in. Env is read lazily (config may load after import).
"""

import os
import re
import sys

import bucketio                                     # the one bucket client: every call goes through it
import cache_budget                                 # the chroma cache's policy: deletions go through it

PREFIX = "chroma/"                  # bucket prefix for signatures (same keys as the local cache)

# The key's shape, the one rule everywhere the pool writes a name (`u` + 20 hex -- see
# docs/HARVEST_FEED.md). The listing admits nothing else under the prefix: the scan refuses
# any stem that is not this shape, so a misnamed object would seed a ledger row no local
# file can ever satisfy, and the pool's count would say one thing while the scan says another.
_KEY_NAME = re.compile(r"u[0-9a-f]{20}\.npy", re.ASCII)
# The sidecar beside every signature: `<key>.json`. The listing admits the same key shape,
# so a sidecar filed under any other name is invisible to the seeding that decides whether
# a signature is complete (both objects) or legacy (the signature alone).
_SIDECAR_NAME = re.compile(r"u[0-9a-f]{20}\.json", re.ASCII)

# Session memory: keys HEAD-verified this run, so eviction sweeps don't re-HEAD every pass.
_verified = {}                      # key -> (remote size, etag)


_reported = set()                   # store errors already printed by this process


def _define():
    """(the signature bucket as a `bucketio.Bucket`, None), or (None, why) when it cannot be
    defined: no bucket named, or no `aws` CLI to be found.

    The endpoint is optional: unset, no --endpoint-url flag is passed and the aws CLI's own
    default applies. Any S3-compatible provider is configured in `.env`, by the operator --
    this repo names none."""
    try:
        return bucketio.Bucket(os.environ.get("NETRADIO_SIG_BUCKET", ""),
                               endpoint=os.environ.get("NETRADIO_SIG_S3_ENDPOINT", ""),
                               profile=os.environ.get("NETRADIO_SIG_AWS_PROFILE", ""),
                               aws_cli=os.environ.get("NETRADIO_AWS_CLI", "")), None
    except bucketio.BucketError as exc:
        return None, str(exc)


def store_error():
    """Why the store is dark although a bucket IS named, or None. With no bucket named, dark is
    the configured state and no error; with one named, a store that cannot be defined (an
    `aws` CLI that does not resolve, a mistyped NETRADIO_AWS_CLI) is a misconfiguration: the
    store stays dark, so nothing is uploaded and nothing is evicted, but it is an error, never
    a silent local-only run."""
    if not os.environ.get("NETRADIO_SIG_BUCKET", "").strip():
        return None
    bucket, why = _define()
    if bucket is not None:
        return None
    return ("the signature store is dark: NETRADIO_SIG_BUCKET is set, but %s "
            "(check NETRADIO_AWS_CLI). Nothing is uploaded until it is fixed" % why)


def _bucket():
    """The signature bucket, or None when the store is dark. A dark store with a bucket named is
    printed as an ERROR, once per process (`store_error`)."""
    bucket, _why = _define()
    if bucket is None:
        err = store_error()
        if err and err not in _reported:
            _reported.add(err)
            print("ERROR: sigstore: " + err, file=sys.stderr, flush=True)
    return bucket


def enabled():
    return _bucket() is not None


def _head(key):
    """HEAD the object -> (size, etag), or (None, None) if absent/unreachable.

    One call answers both questions the callers ask, because they always arrive together:
    a put verifies its size and records the etag the ledger's row carries, and the eviction
    sweep asks for the size alone. `bucketio.head` strips the quotes the S3 API puts around
    an ETag.

    Cached per session once seen (an immutable object's size and ETag do not change)."""
    if key in _verified:
        return _verified[key]
    bucket = _bucket()
    if bucket is None:
        return (None, None)
    got, _err = bucketio.head(bucket, PREFIX + key)
    if got is None:
        return (None, None)
    _verified[key] = (got.size, got.etag)
    return _verified[key]


def verified_size(key):
    """The remote size a HEAD in this process has already returned for `key`, or None. Never
    asks the bucket: the cache policy's pin reads it for every entry on every run."""
    seen = _verified.get(key)
    return seen[0] if seen else None


def put(path, key):
    """Upload one object and VERIFY it landed: `bucketio.put` stores the file's SHA-256 as
    object metadata, then heads the object and requires the size and the hash to match.

    Returns the object's ETag on success, or None on any failure. A non-empty string is
    truthy, so a caller that only asks whether the object landed keeps reading the result
    as a bool; the ledger is the caller that wants the etag itself (its `uploaded_etag`
    is the mark a feeder reads)."""
    bucket = _bucket()
    if bucket is None:
        return None
    _verified.pop(key, None)                      # a fresh HEAD, never a stale cache entry
    got, _err = bucketio.put(bucket, path, PREFIX + key)
    if got is None:
        return None
    _verified[key] = (got.size, got.etag)
    return got.etag


def fetch(key, dest_dir):
    """Bring one signature back into the working cache. Returns its local path, or None.

    `bucketio.fetch_verified` heads the object, downloads it to a unique `.part` name beside
    the final one and renames it into place only once the size matches (and the SHA-256, on
    an object that carries one). A copy that times out or dies mid-stream never leaves a
    partial file under the final name, because "the final name exists" is exactly what
    callers use to mean "this signature is held", and a poisoned entry would suppress every
    later retry. The `.part` name is the cache policy's write-in-progress mark: a bucket pull
    lands inside the registered `chroma` cache, and a policy run answering another writer's
    `reserve` must hold a fresh download, not evict it out from under the rename.

    `require_sha256=False`: signatures uploaded before `put` stored the hash carry none, and
    they are checked by size, as they always were."""
    bucket = _bucket()
    if bucket is None:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, key)
    err = bucketio.fetch_verified(bucket, PREFIX + key, dest, require_sha256=False)
    return None if err else dest


def list_objects():
    """Every signature and sidecar object in the bucket: {name: etag}, or None on failure —
    callers must treat 'unknown' differently from 'empty'. The ETags ride along because the
    ledger's rows record them: one listing answers both "what is in the pool" and "which
    object each row points at". Asked fresh every call (`max_age=0`): the harvester holds
    its own copy for as long as it wants one.

    Both key shapes are admitted -- `<key>.npy` and `<key>.json` -- so a caller can tell a
    complete entry (both objects) from a legacy signature-only one (the sidecar the contract
    now requires was never written). Callers that want only the signatures filter to `.npy`
    themselves."""
    bucket = _bucket()
    if bucket is None:
        return None
    listed, _err = bucketio.listing(bucket, PREFIX, max_age=0)
    if listed is None:
        return None
    objects = {}
    for full, entry in listed.items():
        name = full[len(PREFIX):] if full.startswith(PREFIX) else ""
        if _KEY_NAME.fullmatch(name) or _SIDECAR_NAME.fullmatch(name):
            objects[name] = entry.etag
    return objects


def evictable(path, key, scored, qkeys):
    """Is this local signature COLD — verified remote AND scored against every current mystery?

    `scored` is state["scored"] (querykey -> [signature keys]); `qkeys` the current mystery
    query keys. With no mysteries loaded nothing is ever cold (an empty question set must not
    empty the cache)."""
    if not enabled() or not qkeys:
        return False
    for qk in qkeys:
        if key not in (scored.get(qk) or ()):
            return False
    try:
        local = os.path.getsize(path)
    except OSError:
        return False
    return _head(key)[0] == local


def evict_cold(cache_dir, scored, qkeys):
    """Delete every cold signature from the working cache. Returns (evicted, bytes_freed).

    Each deletion goes through the cache policy's one door, so it is refused (and recorded)
    for a path outside the registered `chroma` cache, and while the policy is dark nothing is
    deleted at all -- the callers refuse to run dark, so that is a belt, not a behaviour."""
    evicted, freed = 0, 0
    try:
        names = sorted(os.listdir(cache_dir))
    except OSError:
        return 0, 0
    for name in names:
        if not (name.startswith("u") and name.endswith(".npy")):
            continue
        path = os.path.join(cache_dir, name)
        if evictable(path, name, scored, qkeys):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if cache_budget.remove("chroma", path, "cold-verified"):
                evicted += 1
                freed += size
    return evicted, freed
