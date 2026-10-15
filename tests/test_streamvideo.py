"""The stream video's inputs: this repo's own data, and the album art it may download.

Hermetic: fixture metadata in a temp directory, a fake opener in place of the network, no
ffmpeg and no Pillow (the planning and the art lookup do not need either).
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import streamvideo                      # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PUBLIC = "93.184.216.34"


def fake_resolve(table):
    """A getaddrinfo stand-in: `table` maps a host to its addresses; a missing host fails."""
    def resolve(host, port, *a, **kw):
        if host not in table:
            raise OSError("no such host: %s" % host)
        return [(2, 1, 6, "", (addr, 0)) for addr in table[host]]
    return resolve


class FakeResponse(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    """Stands in for urllib's urlopen: records each URL asked for, answers with `data`."""

    def __init__(self, data=JPEG, final_url=None, error=None):
        self.data, self.final_url, self.error, self.calls = data, final_url, error, []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.data, self.final_url or req.full_url)


def _fixture_meta():
    tracks = {
        "1": {"title": "One", "artist": "A", "album": "alb-a", "artwork": None,
              "master_begin_seconds": 0.0, "master_end_seconds": 100.0},
        "2": {"title": "Two", "artist": "B", "album": "alb-b", "artwork": None,
              "master_begin_seconds": 100.0, "master_end_seconds": 250.0},
        "3": {"title": "Three", "artist": "C", "album": "alb-a",
              "artwork": "track-3.jpg", "artwork_url": "https://example.invalid/t3.jpg",
              "master_begin_seconds": 250.0, "master_end_seconds": 400.0},
        "4": {"title": "Promo", "artist": "Net Radio", "album": "net-radio", "use_logo": True,
              "master_begin_seconds": 400.0, "master_end_seconds": 420.0},
    }
    albums = {
        "alb-a": {"title": "Album A", "artwork": "album-a.jpg",
                  "artwork_hires": "https://example.invalid/a-1200.jpg",
                  "artwork_url": "https://example.invalid/a-600.jpg"},
        "alb-b": {"title": "Album B", "artwork_url": "https://example.invalid/b.png"},
        "net-radio": {"title": "Net Radio"},
    }
    files = {"files": [
        {"mp3_filename": "x.mp3", "is_primary": True,
         "master_start_seconds": 0.0, "master_end_seconds": 180.0},
        {"mp3_filename": "y.mp3", "is_primary": True,
         "master_start_seconds": 90.0, "master_end_seconds": 500.0},
        {"mp3_filename": "z.mp3", "is_primary": False,
         "master_start_seconds": 0.0, "master_end_seconds": 500.0},
    ]}
    return tracks, albums, files


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="streamvideo-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.art = os.path.join(self.tmp, "art")
        self.tracks, self.albums, self.files = _fixture_meta()
        # No DNS in the suite: every fixture host resolves to one public address.
        patcher = mock.patch.object(streamvideo.socket, "getaddrinfo",
                                    fake_resolve({"example.invalid": [PUBLIC]}))
        patcher.start()
        self.addCleanup(patcher.stop)


class ReadsThisReposData(Base):
    def test_load_meta_reads_the_two_files_at_the_repo_root(self):
        with open(os.path.join(self.tmp, "track-metadata.json"), "w") as fh:
            json.dump({"tracks": self.tracks, "albums": self.albums}, fh)
        with open(os.path.join(self.tmp, "stream-files.json"), "w") as fh:
            json.dump(self.files, fh)
        tr, albums, sf = streamvideo.load_meta(self.tmp)
        self.assertEqual(tr["2"]["title"], "Two")
        self.assertIn("alb-a", albums)
        self.assertEqual(len(sf["files"]), 3)

    def test_the_committed_files_plan_a_span(self):
        """The repo's own track-metadata.json and stream-files.json are enough to plan the
        audio for the opening tracks: no other checkout is read."""
        tr, _albums, sf = streamvideo.load_meta(REPO)
        t0, t1, segs = streamvideo.plan_audio(tr, sf, 1, 3)
        self.assertLess(t0, t1)
        self.assertTrue(segs)
        self.assertAlmostEqual(segs[0]["m_start"], t0)
        self.assertAlmostEqual(segs[-1]["m_end"], t1)

    def test_the_generated_stream_files_carry_no_machine_paths(self):
        with open(os.path.join(REPO, "stream-files.json"), encoding="utf-8") as fh:
            sf = json.load(fh)
        for key in ("mp3_dir", "source_labels_dir", "source_tracklist"):
            self.assertNotIn(key, sf)
        self.assertNotIn("/Users/", json.dumps(sf))

    def test_the_seam_lands_on_a_track_boundary_inside_the_overlap(self):
        _t0, _t1, segs = streamvideo.plan_audio(self.tracks, self.files, 1, 3)
        self.assertEqual([s["file"]["mp3_filename"] for s in segs], ["x.mp3", "y.mp3"])
        self.assertEqual(segs[0]["m_end"], 100.0)            # track 2's start, in 90..180
        self.assertEqual(segs[1]["l_start"], 10.0)           # 100 on y's own clock

    def test_settings_come_from_the_environment_then_this_repos_env(self):
        env = {"NETRADIO_MP3_DIR": "/from/dotenv"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NETRADIO_MP3_DIR", None)
            self.assertEqual(streamvideo.setting("NETRADIO_MP3_DIR", env), "/from/dotenv")
            os.environ["NETRADIO_MP3_DIR"] = "/from/env"
            self.assertEqual(streamvideo.setting("NETRADIO_MP3_DIR", env), "/from/env")

    def test_no_other_checkout_is_named(self):
        with open(os.path.join(REPO, "scripts", "streamvideo.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("NETRADIO_PLAYER_DIR", src)
        self.assertNotIn("metadata/track-metadata.json", src)


class AlbumArt(Base):
    def _cover(self, n, fetch=True, opener=None):
        return streamvideo.track_cover(self.tracks, self.albums, n, art_dir=self.art,
                                       fetch=fetch, opener=opener)

    def test_a_cover_already_in_the_folder_is_used_without_a_download(self):
        os.makedirs(self.art)
        with open(os.path.join(self.art, "track-3.jpg"), "wb") as fh:
            fh.write(JPEG)
        opener = FakeOpener()
        self.assertEqual(self._cover(3, opener=opener), os.path.join(self.art, "track-3.jpg"))
        self.assertEqual(opener.calls, [])

    def test_a_missing_cover_is_downloaded_under_its_recorded_name(self):
        opener = FakeOpener()
        path = self._cover(1, opener=opener)                 # no track art: the album's
        self.assertEqual(path, os.path.join(self.art, "album-a.jpg"))
        self.assertEqual(opener.calls[0][0], "https://example.invalid/a-1200.jpg")
        self.assertEqual(opener.calls[0][1], streamvideo.ART_TIMEOUT_S)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), JPEG)
        self.assertEqual([n for n in os.listdir(self.art) if n.endswith(".part")], [])

    def test_an_entry_with_a_url_and_no_name_is_filed_under_the_album_id(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        path = self._cover(2, opener=FakeOpener(data=png))
        self.assertEqual(path, os.path.join(self.art, "album-alb-b.png"))

    def test_no_fetch_falls_back_to_the_logo(self):
        opener = FakeOpener()
        self.assertEqual(self._cover(1, fetch=False, opener=opener), streamvideo.LOGO)
        self.assertEqual(opener.calls, [])

    def test_a_station_promo_shows_the_logo(self):
        opener = FakeOpener()
        self.assertEqual(self._cover(4, opener=opener), streamvideo.LOGO)
        self.assertEqual(opener.calls, [])

    def test_a_failed_download_falls_back_to_the_logo(self):
        self.assertEqual(self._cover(1, opener=FakeOpener(error=OSError("down"))),
                         streamvideo.LOGO)
        self.assertFalse(os.path.exists(os.path.join(self.art, "album-a.jpg")))


class FetchIsGuarded(Base):
    def _fetch(self, url, **kw):
        dest = os.path.join(self.art, "c.jpg")
        return streamvideo.fetch_art(url, dest, **kw), dest

    def test_only_http_and_https_are_fetched(self):
        for url in ("file:///etc/passwd", "ftp://example.invalid/a.jpg", "javascript:x",
                    "https://", ""):
            opener = FakeOpener()
            ok, dest = self._fetch(url, opener=opener)
            self.assertFalse(ok, url)
            self.assertEqual(opener.calls, [], url)
            self.assertFalse(os.path.exists(dest))

    def test_a_redirect_off_http_is_refused(self):
        ok, dest = self._fetch("https://example.invalid/a.jpg",
                               opener=FakeOpener(final_url="file:///etc/passwd"))
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))

    def test_a_response_over_the_cap_is_refused(self):
        ok, dest = self._fetch("https://example.invalid/a.jpg",
                               opener=FakeOpener(data=JPEG + b"\x00" * 100), max_bytes=50)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))

    def test_a_response_that_is_not_an_image_is_refused(self):
        ok, dest = self._fetch("https://example.invalid/a.jpg",
                               opener=FakeOpener(data=b"<html>not found</html>"))
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))

    def test_a_recorded_name_with_a_directory_is_not_trusted(self):
        entry = {"artwork": "../escape.jpg", "artwork_url": "https://example.invalid/e.jpg"}
        opener = FakeOpener()
        self.assertIsNone(streamvideo.entry_cover(entry, "x", art_dir=self.art, opener=opener))
        self.assertEqual(opener.calls, [])


class AddressesAreChecked(Base):
    """The host is resolved first, and a recorded URL never reaches a non-public address."""

    REFUSED = ("127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.20", "169.254.169.254",
               "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1",
               "::ffff:127.0.0.1")

    def test_a_host_on_a_non_public_address_is_never_asked(self):
        for addr in self.REFUSED:
            opener = FakeOpener()
            dest = os.path.join(self.art, "c.jpg")
            ok = streamvideo.fetch_art("https://cover.test/a.jpg", dest, opener=opener,
                                       resolve=fake_resolve({"cover.test": [addr]}))
            self.assertFalse(ok, addr)
            self.assertEqual(opener.calls, [], addr)
            self.assertFalse(os.path.exists(dest))

    def test_one_non_public_address_among_public_ones_is_enough_to_refuse(self):
        opener = FakeOpener()
        ok = streamvideo.fetch_art("https://cover.test/a.jpg", os.path.join(self.art, "c.jpg"),
                                   opener=opener,
                                   resolve=fake_resolve({"cover.test": [PUBLIC, "127.0.0.1"]}))
        self.assertFalse(ok)
        self.assertEqual(opener.calls, [])

    def test_a_literal_private_address_in_the_url_is_refused(self):
        opener = FakeOpener()
        ok = streamvideo.fetch_art("http://127.0.0.1/a.jpg", os.path.join(self.art, "c.jpg"),
                                   opener=opener, resolve=fake_resolve({"127.0.0.1": ["127.0.0.1"]}))
        self.assertFalse(ok)
        self.assertEqual(opener.calls, [])

    def test_a_host_that_does_not_resolve_is_refused(self):
        opener = FakeOpener()
        ok = streamvideo.fetch_art("https://nowhere.test/a.jpg",
                                   os.path.join(self.art, "c.jpg"), opener=opener,
                                   resolve=fake_resolve({}))
        self.assertFalse(ok)
        self.assertEqual(opener.calls, [])

    def test_a_public_host_is_fetched(self):
        ok = streamvideo.fetch_art("https://cover.test/a.jpg", os.path.join(self.art, "c.jpg"),
                                   opener=FakeOpener(),
                                   resolve=fake_resolve({"cover.test": [PUBLIC]}))
        self.assertTrue(ok)

    def test_a_redirect_to_a_private_address_is_refused(self):
        resolve = fake_resolve({"cover.test": [PUBLIC], "inside.test": ["10.0.0.5"]})
        ok = streamvideo.fetch_art("https://cover.test/a.jpg", os.path.join(self.art, "c.jpg"),
                                   opener=FakeOpener(final_url="http://inside.test/a.jpg"),
                                   resolve=resolve)
        self.assertFalse(ok)

    def test_the_default_opener_checks_each_redirect_hop_before_following_it(self):
        handler = streamvideo._CheckedRedirect(
            fake_resolve({"cover.test": [PUBLIC], "inside.test": ["10.0.0.5"]}))
        req = streamvideo.urllib.request.Request("https://cover.test/a.jpg")
        with self.assertRaises(streamvideo.urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://inside.test/a.jpg")
        with self.assertRaises(streamvideo.urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
        nxt = handler.redirect_request(req, None, 302, "Found", {}, "https://cover.test/b.jpg")
        self.assertEqual(nxt.full_url, "https://cover.test/b.jpg")


class TrackCoverWinsOverAlbum(Base):
    """A track's value overrides its album's, as everywhere in track-metadata.json."""

    def setUp(self):
        super().setUp()
        self.tracks["5"] = {"title": "Single", "artist": "D", "album": "alb-a",
                            "artwork_url": "https://example.invalid/single.png",
                            "master_begin_seconds": 420.0, "master_end_seconds": 500.0}
        self.tracks["6"] = {"title": "Loose", "artist": "E",
                            "artwork_url": "https://example.invalid/loose.jpg",
                            "master_begin_seconds": 500.0, "master_end_seconds": 560.0}
        os.makedirs(self.art)
        with open(os.path.join(self.art, "album-a.jpg"), "wb") as fh:
            fh.write(JPEG)

    def _cover(self, n, fetch=True, opener=None):
        return streamvideo.track_cover(self.tracks, self.albums, n, art_dir=self.art,
                                       fetch=fetch, opener=opener)

    def test_a_track_url_wins_over_the_album_cover_already_in_the_folder(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        opener = FakeOpener(data=png)
        self.assertEqual(self._cover(5, opener=opener), os.path.join(self.art, "track-5.png"))
        self.assertEqual([c[0] for c in opener.calls], ["https://example.invalid/single.png"])

    def test_a_track_with_no_album_and_a_url_shows_the_downloaded_cover(self):
        self.assertEqual(self._cover(6, opener=FakeOpener()),
                         os.path.join(self.art, "track-6.jpg"))

    def test_no_fetch_shows_the_album_cover_as_before(self):
        opener = FakeOpener()
        self.assertEqual(self._cover(5, fetch=False, opener=opener),
                         os.path.join(self.art, "album-a.jpg"))
        self.assertEqual(self._cover(6, fetch=False, opener=opener), streamvideo.LOGO)
        self.assertEqual(opener.calls, [])


class CoverNamesStayInTheFolder(Base):
    def test_an_album_id_with_a_directory_is_refused_and_shows_the_logo(self):
        for album_id in ("../../escaped", "a/b", "a\\b", "x\0y"):
            albums = {album_id: {"artwork_url": "https://example.invalid/e.jpg"}}
            opener = FakeOpener()
            self.assertEqual(streamvideo.album_cover(albums, album_id, art_dir=self.art,
                                                     opener=opener), streamvideo.LOGO,
                             album_id)
            self.assertEqual(opener.calls, [], album_id)
        self.assertFalse(os.path.exists(self.art))       # nothing created, inside or out
        self.assertEqual(sorted(os.listdir(self.tmp)), [])

    def test_a_recorded_name_with_a_nul_is_refused_without_an_error(self):
        entry = {"artwork": "cover\0.jpg", "artwork_url": "https://example.invalid/e.jpg"}
        opener = FakeOpener()
        self.assertIsNone(streamvideo.entry_cover(entry, "x", art_dir=self.art, opener=opener))
        self.assertEqual(opener.calls, [])


class EmbeddedIPv4IsChecked(unittest.TestCase):
    def test_ipv6_forms_carrying_a_private_ipv4_address_are_refused(self):
        for addr in ("64:ff9b::a00:1", "64:ff9b::7f00:1", "::127.0.0.1", "::10.0.0.1",
                     "::ffff:10.0.0.1", "2002:a00:1::1", "2002:7f00:1::1"):
            self.assertFalse(streamvideo._public_ip(addr), addr)

    def test_public_addresses_pass(self):
        for addr in (PUBLIC, "2606:4700:4700::1111", "64:ff9b::5db8:d822"):
            self.assertTrue(streamvideo._public_ip(addr), addr)


class FakeSock:
    def __init__(self, peer):
        self.peer, self.closed = peer, False

    def getpeername(self):
        return (self.peer, 443)

    def close(self):
        self.closed = True


class ThePeerIsChecked(Base):
    """The connection resolves the host again; the address it reached is checked too."""

    def test_a_connection_that_reached_a_private_address_is_closed_and_refused(self):
        for peer in ("10.0.0.5", "127.0.0.1", "::1", "169.254.169.254"):
            sock = FakeSock(peer)
            with self.assertRaises(OSError):
                streamvideo._check_peer(sock)
            self.assertTrue(sock.closed, peer)

    def test_a_public_peer_is_kept(self):
        sock = FakeSock(PUBLIC)
        streamvideo._check_peer(sock)
        self.assertFalse(sock.closed)

    def test_both_connection_classes_check_the_peer_after_connecting(self):
        for cls, base in ((streamvideo._CheckedHTTPConnection, streamvideo.http.client.HTTPConnection),
                          (streamvideo._CheckedHTTPSConnection, streamvideo.http.client.HTTPSConnection)):
            conn = cls("cover.test")

            def fake_connect(self):
                self.sock = FakeSock("10.0.0.5")
            with mock.patch.object(base, "connect", fake_connect):
                with self.assertRaises(OSError):
                    conn.connect()
            self.assertTrue(conn.sock.closed, cls.__name__)

    def test_the_default_opener_uses_no_proxy_and_the_checked_handlers(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://proxy.test:3128",
                                          "http_proxy": "http://proxy.test:3128"}):
            opener = streamvideo._default_opener().__self__
        kinds = {type(h) for h in opener.handlers}
        self.assertIn(streamvideo._CheckedHTTPHandler, kinds)
        self.assertIn(streamvideo._CheckedHTTPSHandler, kinds)
        self.assertIn(streamvideo._CheckedRedirect, kinds)
        proxies = [h for h in opener.handlers
                   if isinstance(h, streamvideo.urllib.request.ProxyHandler)]
        # With the environment's proxies, the default ProxyHandler would be installed; the
        # empty one installs no proxy at all.
        self.assertFalse(any(h.proxies for h in proxies))
        self.assertFalse(any(type(h) is streamvideo.urllib.request.HTTPHandler
                             for h in opener.handlers))


class SlowResponse(FakeResponse):
    """Hands back one small chunk per read, while the fake clock moves 10 s per read."""

    def __init__(self, data, url, clock):
        super().__init__(data, url)
        self.clock = clock

    def read(self, n=-1):
        self.clock.now += 10
        return super().read(min(n, 4) if n and n > 0 else 4)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TheWholeDownloadHasADeadline(Base):
    def test_a_trickling_server_is_given_up_on_at_the_deadline(self):
        clock = FakeClock()

        def opener(req, timeout=None):
            return SlowResponse(JPEG, req.full_url, clock)
        dest = os.path.join(self.art, "c.jpg")
        ok = streamvideo.fetch_art("https://example.invalid/a.jpg", dest, opener=opener,
                                   deadline_s=60, clock=clock)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(dest))
        self.assertLessEqual(clock.now, 80)

    def test_a_download_inside_the_deadline_is_kept(self):
        clock = FakeClock()
        ok = streamvideo.fetch_art("https://example.invalid/a.jpg",
                                   os.path.join(self.art, "c.jpg"), opener=FakeOpener(),
                                   deadline_s=60, clock=clock)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
