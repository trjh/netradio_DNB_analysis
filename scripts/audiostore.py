"""Where the listen queue's audio already is: the player's download root, then the audio bucket.

The harvester used to fetch every candidate from the web itself, with yt-dlp, while the player
fetched the same audio a second time for listening. This module is the harvester's READ side of
the player's audio cache, so one transfer serves both: the player downloads a queue entry once
(and copies it to a bucket), and the harvester analyses that copy.

Audio is addressed BY QUEUE ID, never by URL. The id is the listen-queue entry's uuid; a chunk
of a long recording is an ordinary entry with its own id, so its audio is found the same way.

Two places, in this order:

  1. **The download root.** With `NETRADIO_DOWNLOAD_ROOT` set, the player's `index.json` there
     says which entries have a file (`entries[<id>].file`, relative to the root). Read-only,
     never written, and a missing or torn file reads as "nothing local" rather than an error --
     the player owns that file and may be rewriting it.
  2. **The audio bucket.** `audio/<id>.<ext>` in `NETRADIO_AUDIO_BUCKET`, read with the aws CLI
     exactly as `sigstore` reads the signature bucket. The extension is not known in advance
     (it is whatever the downloading machine's yt-dlp produced), so the object is found by
     listing the `audio/<id>.` prefix -- the dot keeps one id from reaching into another.

Nothing here ever deletes: not a local file, not an object. The player owns both; this side only
reads. There is no `put` either -- the uploading machine is the one that downloaded.

Dark unless configured, like every optional integration: no bucket name means no aws call, no
download root means no index read. Env is read lazily, because the harvester's own `.env_vars`
may be loaded after import.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time

# The one seam through which every aws invocation passes -- swappable in tests.
_run = subprocess.run

PREFIX = "audio/"
LIST_TTL_S = 300            # one listing answers "is this id in the bucket" for five minutes
LIST_MAX_PAGES = 1000       # a paging fault must never loop forever; past this the answer is unknown
LIST_TIMEOUT_S = 120
CP_TIMEOUT_S = 900          # a two-hour part is ~150 MB; the copy is local-network at worst


def _bucket():
    return os.environ.get("NETRADIO_AUDIO_BUCKET", "").strip()


def _endpoint():
    """The audio bucket's endpoint, else the signature bucket's, else the CLI's own default. The
    override exists because the two buckets may live in different regions or on different
    accounts; `sigstore`'s variable is the fallback so one configured store serves both."""
    return (os.environ.get("NETRADIO_AUDIO_S3_ENDPOINT", "").strip()
            or os.environ.get("NETRADIO_SIG_S3_ENDPOINT", "").strip())


def _profile():
    return (os.environ.get("NETRADIO_AUDIO_AWS_PROFILE", "").strip()
            or os.environ.get("NETRADIO_SIG_AWS_PROFILE", "").strip())


def _aws_cli():
    return os.environ.get("NETRADIO_AWS_CLI", "") or shutil.which("aws") or ""


def enabled():
    """Configured to read the bucket."""
    return bool(_bucket()) and bool(_aws_cli())


def _base_cmd():
    cmd = [_aws_cli()]
    if _endpoint():
        cmd += ["--endpoint-url", _endpoint()]
    if _profile():
        cmd += ["--profile", _profile()]
    return cmd


# --- the download root: audio the player holds on this machine ----------------------------------

def download_root():
    return os.path.expanduser(os.environ.get("NETRADIO_DOWNLOAD_ROOT", "").strip())


def local_enabled():
    return bool(download_root())


def local_files():
    """{queue id: absolute path} for every entry whose file is on disk right now.

    Reads the player's `index.json` once and stats each named file. A missing, torn or
    wrong-shaped index reads as {} -- the player may be mid-write, and "nothing local this pass"
    is the safe answer: the next pass reads it again, and the bucket still answers meanwhile.
    Never raises, never writes.
    """
    root = download_root()
    if not root:
        return {}
    try:
        with open(os.path.join(root, "index.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    out = {}
    for item_id, entry in entries.items():
        if not isinstance(item_id, str) or not isinstance(entry, dict):
            continue
        rel = entry.get("file")
        if not isinstance(rel, str) or not rel:
            continue
        # CONTAINMENT: the index names files relative to the root, and a tampered or corrupt
        # entry must not make the harvester open something elsewhere.
        path = os.path.realpath(os.path.join(root, rel))
        if not path.startswith(os.path.realpath(root) + os.sep):
            continue
        if os.path.isfile(path):
            out[item_id] = path
    return out


def local_path(item_id):
    """This one entry's local file, checked NOW (a stat, not the cached snapshot), or None."""
    return local_files().get(item_id)


# --- the bucket ---------------------------------------------------------------------------------

_LIST = {"at": 0.0, "ids": None}     # session cache of the whole `audio/` listing


def bucket_ids(max_age_s=LIST_TTL_S, force=False):
    """The set of queue ids the bucket holds an object for -- ONE paginated listing of the
    `audio/` prefix, cached for `max_age_s`. None when the store is dark or the listing has never
    succeeded: callers must not read that as "empty". A listing that fails after an earlier one
    succeeded returns the stale set, which is a real answer where "unknown" is not."""
    if not enabled():
        return None
    now = time.time()
    if not force and _LIST["ids"] is not None and now - _LIST["at"] < max_age_s:
        return _LIST["ids"]
    ids, token = set(), None
    for _page in range(LIST_MAX_PAGES):
        cmd = _base_cmd() + ["s3api", "list-objects-v2", "--bucket", _bucket(),
                             "--prefix", PREFIX, "--output", "json"]
        if token:
            cmd += ["--continuation-token", token]
        try:
            proc = _run(cmd, capture_output=True, text=True, timeout=LIST_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return _LIST["ids"]
        if proc.returncode != 0:
            return _LIST["ids"]
        try:
            payload = json.loads(proc.stdout or "{}") or {}
        except ValueError:
            return _LIST["ids"]
        for obj in payload.get("Contents") or []:
            key = (obj or {}).get("Key") or ""
            if not key.startswith(PREFIX):
                continue
            name = key[len(PREFIX):]
            if name and "/" not in name:
                ids.add(os.path.splitext(name)[0])
        token = payload.get("NextContinuationToken")
        if not token:
            break
    if token:
        # The page ceiling was hit with more to come. A partial set cached as complete would read
        # every id past it as "not in the bucket" -- worse than saying nothing. Unknown, or stale.
        return _LIST["ids"]
    _LIST.update(at=now, ids=ids)
    return ids


def _head(item_id):
    """{"key", "bytes"} for this id's object, or None. A LIST of the `audio/<id>.` prefix, not
    an S3 HEAD, because the extension is the uploader's choice and unknown here."""
    if not enabled():
        return None
    cmd = _base_cmd() + ["s3api", "list-objects-v2", "--bucket", _bucket(),
                         "--prefix", PREFIX + item_id + ".", "--max-keys", "2",
                         "--output", "json"]
    try:
        proc = _run(cmd, capture_output=True, text=True, timeout=LIST_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        contents = (json.loads(proc.stdout or "{}") or {}).get("Contents") or []
    except ValueError:
        return None
    contents = [c for c in contents if isinstance(c, dict) and c.get("Key")]
    if not contents:
        return None
    contents.sort(key=lambda c: c["Key"])          # two extensions for one id: take the first
    return {"key": contents[0]["Key"], "bytes": contents[0].get("Size")}


def fetch(item_id, dest_dir):
    """Copy this entry's object into `dest_dir`, named `audio<ext>`. Returns the path, or None
    on a miss, a failure, or when dark -- every failure looks like a miss, and the caller says
    "no audio" rather than guessing.

    Through a temporary name, renamed into place only once the size matches the listing: a copy
    that dies mid-stream must never leave a plausible file under the final name.
    """
    if not enabled():
        return None
    h = _head(item_id)
    if not h:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "audio" + os.path.splitext(h["key"])[1])
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix="audio.part-")
    os.close(fd)
    cmd = _base_cmd() + ["s3", "cp", "s3://%s/%s" % (_bucket(), h["key"]), tmp, "--no-progress"]
    try:
        try:
            proc = _run(cmd, capture_output=True, text=True, timeout=CP_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not os.path.exists(tmp):
            return None
        got = os.path.getsize(tmp)
        if got == 0 or (isinstance(h.get("bytes"), int) and got != h["bytes"]):
            return None
        os.replace(tmp, dest)
        return dest
    finally:
        try:
            os.unlink(tmp)                        # a no-op once the replace consumed it
        except OSError:
            pass


# --- the availability set ----------------------------------------------------------------------

def available_ids():
    """Every queue id with audio somewhere: the local files now, plus the bucket's listing (as
    cached). The union, so a machine with no bucket configured still analyses what it holds."""
    ids = set(local_files())
    remote = bucket_ids()
    if remote:
        ids |= remote
    return ids
