"""The signature store: an S3-compatible bucket as the pool's ONLY long-term home.

Chroma signatures used to live forever in `.chroma-cache/`. With the store configured, the
bucket is authoritative and the local dir demotes to a WORKING CACHE: a signature is on disk
only while it is being made, being scored, or has been pulled back for a re-score — and is
EVICTED (deleted locally) once it is (a) verified present in the bucket and (b) fully scored
against every current mystery. Space on this machine is for work in progress, not for the
archive.

Rules that keep this safe:

  * **Never evict what isn't verified remote.** "Uploaded" means a HEAD of the object returns
    the local file's exact size — not that a put command exited 0 once.
  * **Never evict what is still wanted.** Fully-scored is judged against the CURRENT mystery
    set; a new or re-cut mystery makes old signatures wanted again, and `fetch()` brings any of
    them back on demand (a signature is ~55KB; re-fetching it is nothing).
  * **Dark unless configured** (`NETRADIO_SIG_BUCKET`), like every optional integration; with
    the store dark, nothing is ever deleted and the harvester behaves exactly as before.

Speaks S3 through the aws CLI — an external tool exactly like yt-dlp and ffmpeg, so this repo
keeps its zero-pip-dependency posture for storage too. Env is read lazily (config may load
after import).
"""

import os
import re
import shutil
import subprocess
import tempfile

import cache_budget                                 # the chroma cache's policy: deletions go through it

# The one seam through which every aws invocation passes — swappable in tests.
_run = subprocess.run

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
_verified = {}                      # key -> remote size


def _bucket():
    return os.environ.get("NETRADIO_SIG_BUCKET", "").strip()


def _endpoint():
    """Optional. Unset -> no --endpoint-url flag: the aws CLI's own default applies. Any
    S3-compatible provider is configured HERE, by the operator -- this repo names none."""
    return os.environ.get("NETRADIO_SIG_S3_ENDPOINT", "").strip()


def _profile():
    return os.environ.get("NETRADIO_SIG_AWS_PROFILE", "").strip()


def _aws_cli():
    return os.environ.get("NETRADIO_AWS_CLI", "") or shutil.which("aws") or ""


def enabled():
    return bool(_bucket()) and bool(_aws_cli())


def _base_cmd():
    cmd = [_aws_cli()]
    if _endpoint():
        cmd += ["--endpoint-url", _endpoint()]
    if _profile():
        cmd += ["--profile", _profile()]
    return cmd


def _head(key):
    """HEAD the object -> (size, etag), or (None, None) if absent/unreachable.

    One call answers both questions the callers ask, because they always arrive together:
    a put verifies its size and records the etag the ledger's row carries, and the eviction
    sweep asks for the size alone. The ETag comes back quoted by the S3 API (it is an MD5
    in quotes for un-multipart objects), so the quotes are stripped here, once.

    Cached per session once seen (an immutable object's size and ETag do not change)."""
    if key in _verified:
        return _verified[key]
    cmd = _base_cmd() + ["s3api", "head-object", "--bucket", _bucket(),
                         "--key", PREFIX + key, "--query", "[ContentLength, ETag]",
                         "--output", "json"]
    try:
        proc = _run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return (None, None)
    if proc.returncode != 0:
        return (None, None)
    import json
    try:
        size, etag = json.loads(proc.stdout or "[null, null]")
    except ValueError:
        return (None, None)
    if size is None:
        return (None, None)
    _verified[key] = (size, (etag or "").strip('"') or None)
    return _verified[key]


def remote_size(key):
    """HEAD the object -> size in bytes, or None if absent/unreachable. Cached per session
    once seen (an immutable object's size does not change)."""
    return _head(key)[0]


def have_remote(key):
    return enabled() and remote_size(key) is not None


def put(path, key):
    """Upload one object and VERIFY it landed (remote size == local size).

    Returns the object's ETag on success, or None on any failure. A non-empty string is
    truthy, so a caller that only asks whether the object landed keeps reading the result
    as a bool; the ledger is the caller that wants the etag itself (its `uploaded_etag`
    is the mark a feeder reads)."""
    if not enabled():
        return None
    try:
        local = os.path.getsize(path)
    except OSError:
        return None
    cmd = _base_cmd() + ["s3", "cp", path, "s3://%s/%s%s" % (_bucket(), PREFIX, key),
                         "--no-progress"]
    try:
        proc = _run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    _verified.pop(key, None)                      # force a fresh HEAD, not a stale cache entry
    size, etag = _head(key)
    return etag if size == local else None


def fetch(key, dest_dir):
    """Bring one signature back into the working cache. Returns its local path, or None.

    Downloads through a TEMPORARY name and renames into place only on success -- a copy that
    times out or dies mid-stream must never leave a partial file under the final name, because
    "the final name exists" is exactly what callers use to mean "this signature is held", and a
    poisoned entry would suppress every later retry."""
    if not enabled():
        return None
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, key)
    # A UNIQUE temp per invocation (mkstemp), same directory so the final rename stays atomic.
    # PID alone is not enough -- two threads of one process fetching the same key must not
    # share a pathname. The name ENDS .part, the cache policy's write-in-progress mark: a
    # bucket pull lands inside the registered `chroma` cache, and a policy run answering
    # another writer's `reserve` must hold a fresh download, not evict it out from under the
    # replace (past an hour it reads as a download that died part-way, and is evicted).
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=key + ".", suffix=".part")
    os.close(fd)
    cmd = _base_cmd() + ["s3", "cp", "s3://%s/%s%s" % (_bucket(), PREFIX, key), tmp,
                         "--no-progress"]
    try:
        try:
            proc = _run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            return None
        try:
            os.replace(tmp, dest)
        except OSError:
            # Racing replace: if someone else already published the file, that IS success --
            # the object is immutable, any complete copy is the right copy.
            return dest if os.path.exists(dest) else None
        return dest
    finally:
        try:
            os.unlink(tmp)                    # no-op if the replace consumed it
        except OSError:
            pass


def list_objects():
    """Every signature and sidecar object in the bucket: {name: etag}, or None on failure —
    callers must treat 'unknown' differently from 'empty'. The ETags ride along because the
    ledger's rows record them: one listing answers both "what is in the pool" and "which
    object each row points at".

    Both key shapes are admitted -- `<key>.npy` and `<key>.json` -- so a caller can tell a
    complete entry (both objects) from a legacy signature-only one (the sidecar the contract
    now requires was never written). Callers that want only the signatures filter to `.npy`
    themselves."""
    if not enabled():
        return None
    objects, token = {}, None
    while True:
        cmd = _base_cmd() + ["s3api", "list-objects-v2", "--bucket", _bucket(),
                             "--prefix", PREFIX, "--query", "[Contents[].[Key, ETag], NextToken]",
                             "--output", "json"]
        if token:
            cmd += ["--starting-token", token]
        try:
            proc = _run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        import json
        try:
            contents, token = json.loads(proc.stdout or "[[], null]")
        except (ValueError, TypeError):
            return None
        for entry in contents or []:
            try:
                name, etag = entry[0][len(PREFIX):], entry[1]
            except (TypeError, IndexError):
                continue                    # a shape the contract does not describe: skip it
            if _KEY_NAME.fullmatch(name) or _SIDECAR_NAME.fullmatch(name):
                objects[name] = (etag or "").strip('"') or None
        if not token:
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
    return remote_size(key) == local


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
