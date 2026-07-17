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
import shutil
import subprocess
import tempfile

# The one seam through which every aws invocation passes — swappable in tests.
_run = subprocess.run

PREFIX = "chroma/"                  # bucket prefix for signatures (same keys as the local cache)

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


def remote_size(key):
    """HEAD the object -> size in bytes, or None if absent/unreachable. Cached per session
    once seen (an immutable object's size does not change)."""
    if key in _verified:
        return _verified[key]
    cmd = _base_cmd() + ["s3api", "head-object", "--bucket", _bucket(),
                         "--key", PREFIX + key, "--query", "ContentLength", "--output", "text"]
    try:
        proc = _run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        size = int(proc.stdout.strip())
    except ValueError:
        return None
    _verified[key] = size
    return size


def have_remote(key):
    return enabled() and remote_size(key) is not None


def put(path, key):
    """Upload one signature and VERIFY it landed (remote size == local size). True on success."""
    if not enabled():
        return False
    try:
        local = os.path.getsize(path)
    except OSError:
        return False
    cmd = _base_cmd() + ["s3", "cp", path, "s3://%s/%s%s" % (_bucket(), PREFIX, key),
                         "--no-progress"]
    try:
        proc = _run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    _verified.pop(key, None)                      # force a fresh HEAD, not a stale cache entry
    return remote_size(key) == local


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
    # share a pathname.
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=key + ".part-")
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


def list_keys():
    """Every signature key in the bucket (u….npy under the prefix). None on failure —
    callers must treat 'unknown' differently from 'empty'."""
    if not enabled():
        return None
    keys, token = set(), None
    while True:
        cmd = _base_cmd() + ["s3api", "list-objects-v2", "--bucket", _bucket(),
                             "--prefix", PREFIX, "--query", "[Contents[].Key, NextToken]",
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
        except ValueError:
            return None
        for k in contents or []:
            name = k[len(PREFIX):]
            if name.startswith("u") and name.endswith(".npy") and "/" not in name:
                keys.add(name)
        if not token:
            return keys


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
    """Delete every cold signature from the working cache. Returns (evicted, bytes_freed)."""
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
                os.remove(path)
            except OSError:
                continue
            evicted += 1
            freed += size
    return evicted, freed
