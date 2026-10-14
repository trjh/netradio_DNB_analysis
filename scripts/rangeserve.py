"""Single-part HTTP Range file serving — the seek mechanic, in one leaf module.

Any `http.server` handler can call it to serve a file with byte ranges. It imports nothing
outside the standard library, so a server that uses it needs nothing else from this repo.

A browser <audio> element can only seek a source it can ask byte ranges of:
against a 200-only route the seek bar works just within what has already
buffered, and Safari greys it out entirely. That was learned on more than one
route, so the logic lives here, once, instead of in each of them.

`bytes=-N` (a suffix range) means the LAST N bytes (RFC 9110 §14.1.2). A
range that names no bytes at all — `bytes=-`, or `bytes=-0` (zero-length
suffix, unsatisfiable) — falls through to the plain 200: better than a
whole-file 206 dressed up as a range, or a 206 whose headers promise a byte
the body does not carry.

Single-part ranges only, matched against the WHOLE header (fullmatch): a
multi-range request (`bytes=0-1,4-5`) must not be answered with a 206 that
silently drops every range after the first. Ignoring the Range header
entirely (plain 200) is always a compliant answer; a 206 missing requested
bytes is not.

Offsets are capped at 18 digits IN THE PATTERN (< 2^63, beyond any real file):
a thousands-of-digits offset is not a seek, and int() on one raises past
Python 3.11's conversion limit — the request must degrade to the plain 200,
not die mid-header.
"""

import os
import re
import shutil


def serve(handler, path, ctype, extra_headers=None):
    """Serve `path` through `handler` (a BaseHTTPRequestHandler) with Range support.

    `extra_headers` (an ordered dict of name → value) ride on the 200 and 206
    responses — never on the 416, which is an error about the request, not the
    representation.
    """
    file_size = os.path.getsize(path)
    m = re.fullmatch(r"bytes=(\d{0,18})-(\d{0,18})", handler.headers.get("Range", ""))
    # If-Range says "only the range IF my copy is still current". These responses
    # carry no ETag/Last-Modified to validate against, so the honest answer to any
    # If-Range is "cannot confirm" — RFC 9110 then requires ignoring Range and
    # sending the whole file, never a 206 that could splice two versions together.
    if handler.headers.get("If-Range"):
        m = None
    rng = None
    if m and file_size > 0:
        start_text, end_text = m.groups()
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
            if end_text and int(end_text) < start:
                pass          # descending: syntactically invalid → ignored (RFC 9110)
            elif start >= file_size:
                # wholly beyond EOF: unsatisfiable. The old copies clamped this to a
                # "successful" 206 carrying the file's LAST byte — data nobody asked
                # for. 416 with the star form tells the client the real size instead.
                handler.send_response(416)
                handler.send_header("Content-Range", "bytes */%d" % file_size)
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return None
            else:
                rng = (start, min(end, file_size - 1))
        elif end_text and int(end_text) > 0:  # bytes=-N: the final N bytes
            rng = (max(0, file_size - int(end_text)), file_size - 1)
    if rng:
        start, end = rng
        length = end - start + 1
        handler.send_response(206)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, file_size))
        handler.send_header("Content-Length", str(length))
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        with open(path, "rb") as handle:
            handle.seek(start)
            handler.wfile.write(handle.read(length))
        return None
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(file_size))
    for name, value in (extra_headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    with open(path, "rb") as handle:
        shutil.copyfileobj(handle, handler.wfile)
    return None
