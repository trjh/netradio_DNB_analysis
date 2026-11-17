"""A fake `aws` CLI over `bucketio`'s one seam (`bucketio._run`), holding objects in memory.

It answers the four calls the module makes, with the JSON the real CLI prints (built with
`json.dumps`, so S3's quoted ETags are never hand-escaped):

    s3 cp <file> s3://<bucket>/<key> [--metadata name=value]    upload
    s3 cp s3://<bucket>/<key> <file>                             download
    s3api head-object --bucket B --key K                         404 when absent
    s3api list-objects-v2 --bucket B --prefix P --max-items N [--starting-token T]
                                                                 pages of N keys

Every call is recorded as (args after the CLI's global flags, the full command, the timeout).
No network, no real bucket, no process is ever started.

    fake = FakeAws()
    fake.install(self)                  # swaps bucketio._run, restored by addCleanup
    fake.add("b", "k", b"bytes", sha256=True)
    fake.fail_next("head-object", FakeProc(returncode=255, stderr="Could not connect"))
"""

import hashlib
import json
import subprocess

import bucketio


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


NOT_FOUND = ("An error occurred (404) when calling the HeadObject operation: Not Found")


def etag(body):
    """The ETag S3 gives an object the aws CLI uploaded with its default part size."""
    part = bucketio.MULTIPART_PART_BYTES
    if len(body) <= part:
        return hashlib.md5(body).hexdigest()
    parts = [hashlib.md5(body[i:i + part]).digest() for i in range(0, len(body), part)]
    return "%s-%d" % (hashlib.md5(b"".join(parts)).hexdigest(), len(parts))


class FakeAws:
    def __init__(self, page_size=None):
        self.objects = {}           # (bucket, key) -> {"body": bytes, "meta": {name: value}}
        self.calls = []             # (args, full command, timeout)
        self.page_size = page_size      # None: the call's own --max-items, as the CLI does
        self.served = {}            # (bucket, key) -> bytes a download writes instead
        self.stored = {}            # (bucket, key) -> bytes an upload stores instead
        self._fail = {}             # operation -> [FakeProc or exception], used first

    # --- set-up --------------------------------------------------------------------

    def install(self, test):
        original = bucketio._run
        bucketio._run = self
        test.addCleanup(setattr, bucketio, "_run", original)
        bucketio._listings.clear()
        test.addCleanup(bucketio._listings.clear)
        return self

    def add(self, bucket, key, body, sha256=False, meta=None):
        meta = dict(meta or {})
        if sha256 is True:
            meta["sha256"] = hashlib.sha256(body).hexdigest()
        elif sha256:
            meta["sha256"] = sha256
        self.objects[(bucket, key)] = {"body": body, "meta": meta}

    def fail_next(self, operation, result):
        """The next call of `operation` ("cp-up", "cp-down", "head-object", "list-objects-v2")
        answers with `result`: a FakeProc, or an exception to raise."""
        self._fail.setdefault(operation, []).append(result)

    def ops(self, operation=None):
        names = [self._operation(args) for args, _cmd, _t in self.calls]
        return names if operation is None else [n for n in names if n == operation]

    # --- the seam ------------------------------------------------------------------

    @staticmethod
    def _strip_globals(cmd):
        args, i = [], 1
        while i < len(cmd):
            if cmd[i] in ("--endpoint-url", "--profile"):
                i += 2
                continue
            args.append(cmd[i])
            i += 1
        return args

    @staticmethod
    def _operation(args):
        if args[:2] == ["s3", "cp"]:
            return "cp-down" if args[2].startswith("s3://") else "cp-up"
        return args[1] if len(args) > 1 else ""

    @staticmethod
    def _opt(args, name):
        return args[args.index(name) + 1] if name in args else None

    @staticmethod
    def _split(url):
        bucket, _, key = url[len("s3://"):].partition("/")
        return bucket, key

    def __call__(self, cmd, capture_output=False, text=False, timeout=None):
        args = self._strip_globals(cmd)
        self.calls.append((args, list(cmd), timeout))
        op = self._operation(args)
        queued = self._fail.get(op)
        if queued:
            result = queued.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return getattr(self, "_" + op.replace("-", "_"))(args)

    def _cp_up(self, args):
        bucket, key = self._split(args[3])
        meta = {}
        value = self._opt(args, "--metadata")
        if value:
            for pair in value.split(","):
                name, _, val = pair.partition("=")
                meta[name.lower()] = val
        with open(args[2], "rb") as fh:
            body = self.stored.get((bucket, key), fh.read())
        self.objects[(bucket, key)] = {"body": body, "meta": meta}
        return FakeProc()

    def _cp_down(self, args):
        bucket, key = self._split(args[2])
        obj = self.objects.get((bucket, key))
        if obj is None:
            return FakeProc(returncode=1, stderr="fatal error: " + NOT_FOUND)
        with open(args[3], "wb") as fh:
            fh.write(self.served.get((bucket, key), obj["body"]))
        return FakeProc()

    def _head_object(self, args):
        obj = self.objects.get((self._opt(args, "--bucket"), self._opt(args, "--key")))
        if obj is None:
            return FakeProc(returncode=254, stderr=NOT_FOUND)
        return FakeProc(stdout=json.dumps({
            "ContentLength": len(obj["body"]),
            "ETag": '"%s"' % obj.get("etag", etag(obj["body"])),
            "Metadata": obj["meta"]}))

    def _list_objects_v2(self, args):
        bucket, prefix = self._opt(args, "--bucket"), self._opt(args, "--prefix") or ""
        keys = sorted(k for b, k in self.objects if b == bucket and k.startswith(prefix))
        size = self.page_size or int(self._opt(args, "--max-items") or len(keys) or 1)
        start = int(self._opt(args, "--starting-token") or 0)
        page = keys[start:start + size]
        more = start + size < len(keys)
        contents = [[k, len(self.objects[(bucket, k)]["body"]),
                     '"%s"' % etag(self.objects[(bucket, k)]["body"])]
                    for k in page]
        return FakeProc(stdout=json.dumps([contents or None,
                                           str(start + size) if more else None]))


def timeout_error(cmd="aws", seconds=60):
    return subprocess.TimeoutExpired(cmd, seconds)


# --- the cache isolation helper ------------------------------------------------------------

AUDIO_BUCKET = "audio-test"


def isolate_caches(test, root, bucket=AUDIO_BUCKET, fake=None):
    """Give `test` its own cache policy under `root`: its own NETRADIO_CACHE_ROOT, an empty
    registry (restored after), a volume that never reaches the floor, every other cache
    setting removed, and the audio bucket named `bucket` (None: no bucket) over `fake` (a
    `FakeAws`, installed here when given). Every change is undone by the test's cleanups, the
    registry file this process writes under `root` included. Returns `fake`."""
    import os

    import cache_budget

    names = [k for k in os.environ
             if k.startswith("NETRADIO_") and ("CACHE" in k or k in (
                 "NETRADIO_DISK_MAX_PCT", "NETRADIO_DOWNLOAD_ROOT", "NETRADIO_READ_ONLY",
                 "NETRADIO_AUDIO_BUCKET", "NETRADIO_AUDIO_S3_ENDPOINT",
                 "NETRADIO_AUDIO_AWS_PROFILE", "NETRADIO_AWS_CLI", "NETRADIO_CAPTURES_DIR"))]
    saved = {k: os.environ.pop(k) for k in names}

    def _restore_env():
        for k in [k for k in os.environ if k.startswith("NETRADIO_")
                  and ("CACHE" in k or k.startswith("NETRADIO_AUDIO_")
                       or k in ("NETRADIO_DISK_MAX_PCT", "NETRADIO_AWS_CLI"))]:
            os.environ.pop(k)
        os.environ.update(saved)

    test.addCleanup(_restore_env)
    os.environ["NETRADIO_CACHE_ROOT"] = root
    os.environ["NETRADIO_DISK_MAX_PCT"] = "100"
    if bucket:
        os.environ["NETRADIO_AUDIO_BUCKET"] = bucket
        cli = os.path.join(root, "aws")             # resolvable, never run: the fake answers
        os.makedirs(root, exist_ok=True)
        with open(cli, "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(cli, 0o755)
        os.environ["NETRADIO_AWS_CLI"] = cli
    registry, stats = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)

    def _restore_registry():
        cache_budget._drop_own_row()
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(registry)
        cache_budget._STATS.clear()
        cache_budget._STATS.update(stats)

    test.addCleanup(_restore_registry)
    cache_budget._REGISTRY.clear()
    cache_budget._STATS.clear()
    original = cache_budget._disk_usage
    cache_budget._disk_usage = lambda _path: (10 ** 12, 0, 10 ** 12)
    test.addCleanup(setattr, cache_budget, "_disk_usage", original)
    if fake is not None:
        fake.install(test)
    return fake
