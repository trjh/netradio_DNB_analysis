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


class FakeAws:
    def __init__(self, page_size=None):
        self.objects = {}           # (bucket, key) -> {"body": bytes, "meta": {name: value}}
        self.calls = []             # (args, full command, timeout)
        self.page_size = page_size      # None: the call's own --max-items, as the CLI does
        self.served = {}            # (bucket, key) -> bytes a download writes instead
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
            self.objects[(bucket, key)] = {"body": fh.read(), "meta": meta}
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
            "ETag": '"%s"' % hashlib.md5(obj["body"]).hexdigest(),
            "Metadata": obj["meta"]}))

    def _list_objects_v2(self, args):
        bucket, prefix = self._opt(args, "--bucket"), self._opt(args, "--prefix") or ""
        keys = sorted(k for b, k in self.objects if b == bucket and k.startswith(prefix))
        size = self.page_size or int(self._opt(args, "--max-items") or len(keys) or 1)
        start = int(self._opt(args, "--starting-token") or 0)
        page = keys[start:start + size]
        more = start + size < len(keys)
        contents = [[k, len(self.objects[(bucket, k)]["body"]),
                     '"%s"' % hashlib.md5(self.objects[(bucket, k)]["body"]).hexdigest()]
                    for k in page]
        return FakeProc(stdout=json.dumps([contents or None,
                                           str(start + size) if more else None]))


def timeout_error(cmd="aws", seconds=60):
    return subprocess.TimeoutExpired(cmd, seconds)
