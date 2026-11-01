#!/usr/bin/env python3
"""Consumer-level, offline HTTP regressions for iptv-uk-sync.

Run with --binary /nix/store/.../bin/iptv-uk-sync --ffmpeg /nix/store/.../bin/ffmpeg,
or omit --binary to exercise the adjacent Python entry point. Fixture media is
created locally, never downloaded. The HTTP proxy fixture resolves every .test
and BBC fixture URL itself, so the tests require no public DNS/network access.
"""

import argparse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit


ARGS = None
BBC_HOST = "vs-cmaf-pushb-uk-live.akamaized.net"
SOURCE = "http://source.fixture.test/uk.m3u"
HEADER = '#EXTM3U x-tvg-url="https://epg.fixture.test/guide.xml?keep=original"'


def mp4_box(kind, body):
    return struct.pack(">I4s", len(body) + 8, kind) + body


def split_fragmented_mp4(data):
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        if kind == b"moof":
            return data[:offset], data[offset:]
        if size < 8:
            break
        offset += size
    raise AssertionError("ffmpeg did not produce fragmented MP4 fixture media")


@dataclass
class Response:
    body: bytes = b""
    status: int = 200
    headers: dict = field(default_factory=dict)
    require: dict = field(default_factory=dict)
    ranges: bool = True
    declared_length: int = None


class Fixture:
    def __init__(self):
        self.routes = {}
        self.seen = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                host = parts.hostname or self.headers.get("Host", "").split(":", 1)[0]
                path = parts.path + ("?" + parts.query if parts.query else "")
                with fixture.lock:
                    fixture.seen.append((host, path, dict(self.headers)))
                    response = fixture.routes.get((host, path), Response(status=404))
                if any(self.headers.get(name) != value for name, value in response.require.items()):
                    response = Response(status=403)
                data = response.body
                status = response.status
                headers = dict(response.headers)
                if response.ranges and status == 200 and self.headers.get("Range"):
                    match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers["Range"])
                    if match:
                        start, end = map(int, match.groups())
                        if start >= len(data):
                            status = 416
                            data = b""
                        else:
                            end = min(end, len(data) - 1)
                            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                            data = data[start:end + 1]
                            status = 206
                self.send_response(status)
                self.send_header("Content-Length", str(len(data) if response.declared_length is None else response.declared_length))
                self.send_header("Connection", "close")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if data:
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.proxy = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def add(self, url, body=b"", status=200, headers=None, require=None, ranges=True, declared_length=None):
        parts = urlsplit(url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        response = Response(body.encode() if isinstance(body, str) else body, status,
                            headers or {}, require or {}, ranges, declared_length)
        with self.lock:
            self.routes[parts.hostname, path] = response
        return url


class CatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.TemporaryDirectory(prefix="iptv-uk-test-media-")
        directory = Path(cls.media_dir.name)
        ffmpeg = ARGS.ffmpeg
        def generate(arguments):
            result = subprocess.run([ffmpeg, "-v", "error", "-nostdin", *arguments],
                                    capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise RuntimeError("Cannot generate local media fixtures: " + result.stderr.decode(errors="replace"))
        generate(["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=10", "-t", "1",
                  "-c:v", "mpeg2video", "-g", "1", "-threads", "1", "-f", "mpegts", str(directory / "sample.ts")])
        generate(["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=10", "-t", "1",
                  "-c:v", "mpeg4", "-g", "1", "-threads", "1", "-movflags",
                  "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(directory / "sample.mp4")])
        cls.ts = (directory / "sample.ts").read_bytes()
        cls.init, cls.fragment = split_fragmented_mp4((directory / "sample.mp4").read_bytes())
        cls.key = b"0123456789abcdef"
        (directory / "key.bin").write_bytes(cls.key)
        cls.iv = "0" * 31 + "5"
        (directory / "key.info").write_text("key.bin\n" + str(directory / "key.bin") + "\n" + cls.iv + "\n")
        generate(["-i", str(directory / "sample.ts"), "-c", "copy", "-hls_time", "10",
                  "-hls_key_info_file", str(directory / "key.info"), "-hls_segment_filename",
                  str(directory / "cipher-%d.ts"), "-f", "hls", str(directory / "cipher.m3u8")])
        cls.cipher = (directory / "cipher-0.ts").read_bytes()
        cls.height_media = {720: (cls.init, cls.fragment)}
        for height in (360, 719, 1080):
            source_height = height if height % 2 == 0 else height + 1
            target = directory / f"height-{height}.mp4"
            generate(["-f", "lavfi", "-i", f"color=c=black:s=1280x{source_height}:r=2", "-t", "1",
                      "-vf", f"crop=1280:{height}:0:0:exact=1", "-c:v", "mpeg4", "-g", "1",
                      "-threads", "1", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                      "-f", "mp4", str(target)])
            cls.height_media[height] = split_fragmented_mp4(target.read_bytes())
        generate(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", "-c:a", "aac",
                  "-f", "mpegts", str(directory / "audio.ts")])
        cls.audio = (directory / "audio.ts").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.media_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iptv-uk-test-")
        self.root = Path(self.temp.name)
        self.output = self.root / "uk-tv"
        self.http = Fixture()
        self.http.add("http://segments.fixture.test/actual.ts", self.ts)
        self.http.add("http://init.fixture.test/init.mp4", self.init)
        self.http.add("http://segments.fixture.test/actual.m4s", self.fragment)
        self.entries = []
        self.append("BBCOne.uk@London", "BBC One London (720p)", self.hls("bbc-one"))
        self.append("BBCTwo.uk@England", "BBC Two England (720p)", self.hls("bbc-two"))
        self.append("BBCScotland.uk@HD", "BBC Scotland", self.hls("bbc-scotland"))
        self.append("TinyPop.uk@SD", "Tiny Pop", self.hls("tiny-pop"))
        self.append("TinyPop.uk@Plus1", "Tiny Pop +1", self.hls("tiny-pop-plus1"))
        for index in range(5):
            self.append(f"Fixture{index}.uk@HD", f"Fixture {index}", self.hls(f"filler-{index}"))

    def tearDown(self):
        self.http.close()
        self.temp.cleanup()

    def hls(self, name, segment="http://segments.fixture.test/actual.ts", prefix="", host="manifest.fixture.test"):
        return self.http.add(f"http://{host}/{name}/index.m3u8",
                             "#EXTM3U\n#EXT-X-TARGETDURATION:10\n" + prefix + "#EXTINF:1,\n" + segment + "\n")
    def fmp4_hls(self, name, height):
        init, fragment = self.height_media[height]
        init_url = self.http.add(f"http://init.fixture.test/{name}.mp4", init)
        media_url = self.http.add(f"http://segments.fixture.test/{name}.m4s", fragment)
        return self.hls(name, media_url, f'#EXT-X-MAP:URI="{init_url}"\n')


    def append(self, identity, name, url, options=(), metadata=""):
        info = f'#EXTINF:-1 tvg-id="{identity}"{metadata},' + name
        self.entries.append((info, tuple(options), url))
        return url

    def source(self, entries=None):
        entries = self.entries if entries is None else entries
        text = HEADER + "\n" + "".join("\n".join((info, *options, url)) + "\n" for info, options, url in entries)
        self.http.add(SOURCE, text)
        return text

    def run_sync(self, success=True, entries=None, source_status=None, truncated_source=False, no_proxy=False, source_url=SOURCE):
        text = self.source(entries)
        if source_url != SOURCE:
            self.http.add(source_url, text)
        if truncated_source:
            self.http.add(source_url, text, declared_length=len(text.encode()) + 100)
        if source_status is not None:
            self.http.add(source_url, status=source_status)
        command = ([ARGS.binary] if ARGS.binary else [sys.executable, str(Path(__file__).with_name("uk-playlist.py"))])
        command += ["--source", source_url, "--output-dir", str(self.output), "--proxy", self.http.proxy,
                    "--workers", "4", "--timeout", "2", "--min-height", "720"]
        if not ARGS.binary:
            command += ["--ffprobe", str(Path(ARGS.ffmpeg).with_name("ffprobe"))]
        environment = dict(os.environ)
        for name in ("no_proxy", "NO_PROXY"):
            environment.pop(name, None)
        if no_proxy:
            environment.update(NO_PROXY="*", no_proxy="*")
        result = subprocess.run(command, capture_output=True, text=True, timeout=90, env=environment, check=False)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.output.is_symlink(), "Publication must switch one directory symlink")
            return json.loads((self.output / "report.json").read_text())
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def playlist(self):
        return (self.output / "tv.m3u").read_text()

    def by_id(self, report, identity):
        return next(candidate for candidate in report["candidates"] if candidate["id"] == identity)

    def dash(self, name, protection="", init=None, media=None, template=None):
        init = init or "http://init.fixture.test/init.mp4"
        media = media or "http://segments.fixture.test/actual.m4s"
        template = template or f'<SegmentTemplate timescale="1" duration="10" initialization="{init}" media="{media}"/>'
        return self.http.add(f"http://dash.fixture.test/{name}.mpd",
                             '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT30S">'
                             '<Period><AdaptationSet mimeType="video/mp4">' + protection +
                             '<Representation id="video" height="2160" bandwidth="9000000">' + template +
                             '</Representation></AdaptationSet></Period></MPD>')

    def test_bbc_regions_quality_timeshift_and_exact_url_dedup(self):
        high = self.http.add("http://manifest.fixture.test/bbc-high/master.m3u8",
                             '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=1920x1080\nchild.m3u8\n')
        self.http.add("http://manifest.fixture.test/bbc-high/child.m3u8",
                      '#EXTM3U\n#EXTINF:1,\nhttp://segments.fixture.test/actual.ts\n')
        self.append("BBCOne.uk@ScotlandHD", "BBC One Scotland HD", high)
        self.append("BBCOne.uk@East", "BBC One East (576p)", self.hls("bbc-east"))
        self.append("BBCTwo.uk@Wales", "BBC Two Wales", self.hls("bbc-wales"))
        self.append("AnotherAlias.uk", "Unrelated ID, exact same URL", self.entries[-1][2])
        self.append("DuplicateFixture.uk", "Exact URL alias", self.entries[5][2])
        self.append("BBCOne.uk@Plus1", "BBC One +1", self.hls("bbc-one-plus1"))
        report = self.run_sync()
        text = self.playlist()
        self.assertEqual(text.count(",BBC One\n"), 1)
        self.assertEqual(text.count(",BBC Two\n"), 1)
        self.assertIn(high, text)
        self.assertIn(",BBC Scotland\n", text)
        self.assertIn(",Tiny Pop\n", text)
        self.assertIn(",Tiny Pop +1\n", text)
        self.assertIn(",BBC One +1\n", text)
        self.assertEqual(self.by_id(report, "BBCOne.uk@ScotlandHD")["selection"], "selected")
        self.assertEqual(self.by_id(report, "DuplicateFixture.uk")["selection"], "url-duplicate")
        published_urls = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(published_urls), len(set(published_urls)))
        self.assertEqual(text.splitlines()[0], HEADER)

    def test_metadata_headers_and_query_survive_actual_child_requests(self):
        url = "http://headers.fixture.test/live.m3u8?token=history-secret-fixture&value=a%26b"
        tail = "|X-Fixture=pipe-header&User-Agent=Agent%2F1+%28x%2C+y%29"
        require = {"User-Agent": "Agent/1 (x, y)", "Referer": "https://referer.fixture.test/page?keep=yes",
                   "Origin": "https://origin.fixture.test", "X-Fixture": "pipe-header"}
        options = ('#EXTVLCOPT:http-referrer=https://referer.fixture.test/page?keep=yes',
                   '#EXTVLCOPT:http-user-agent=overridden agent',
                   '#EXTHTTP:{"Origin":"https://origin.fixture.test"}')
        self.http.add(url, "#EXTM3U\n#EXTINF:1,\nhttp://headers.fixture.test/segment.ts?keep=a%26b\n", require=require)
        self.http.add("http://headers.fixture.test/segment.ts?keep=a%26b", self.ts, require=require)
        metadata = ' tvg-logo="https://logos.fixture.test/one,two.png?keep=a%26b" group-title="News" user-agent="Agent (x, y)"'
        self.entries[5] = ('#EXTINF:-1 tvg-id="Fixture0.uk@HD"' + metadata + ',Fixture, With Comma', options, url + tail)
        report = self.run_sync()
        expected = "\n".join((self.entries[5][0], *options, url + tail))
        self.assertIn(expected, self.playlist())
        self.assertNotIn("history-secret-fixture", json.dumps(report))
        self.assertNotIn("pipe-header", json.dumps(report))
        self.assertEqual(self.by_id(report, "Fixture0.uk@HD")["name"], "Fixture, With Comma")
        self.assertTrue(any(path == "/segment.ts?keep=a%26b" for _, path, _ in self.http.seen))

    def test_hls_preferred_over_higher_quality_clear_dash_and_quality_aliases(self):
        self.append("Fixture0.uk@UHD", "Fixture 0 (2160p)", self.dash("filler-4k"))
        low = self.fmp4_hls("tiny-pop-low", 720)
        high = self.fmp4_hls("tiny-pop-high", 1080)
        self.append("TinyPop.uk@HD", "Tiny Pop (1080p)", high)
        self.append("TinyPop.uk@SD", "Tiny Pop (576p)", low)
        report = self.run_sync()
        self.assertEqual(self.by_id(report, "Fixture0.uk@UHD")["status"], "usable")
        self.assertEqual(self.by_id(report, "Fixture0.uk@UHD")["selection"], "canonical-duplicate")
        self.assertIn(high, self.playlist())
        self.assertNotIn(low + "\n", self.playlist())
        self.assertIn(",Tiny Pop +1\n", self.playlist())

    def test_official_bbc_sibling_is_actually_verified_and_non_bbc_not_rewritten(self):
        original = f"http://{BBC_HOST}/x=4/i=urn:bbc:pips:service:bbc_one_east/pc_hd_abr_v2.mpd?keep=a%26b"
        sibling = original.replace("pc_hd_abr_v2.mpd", "pc_hd_abr_v2.fmp4.m3u8")
        self.http.add(sibling, '#EXTM3U\n#EXT-X-MAP:URI="http://init.fixture.test/init.mp4"\n'
                      '#EXTINF:1,\nhttp://segments.fixture.test/actual.m4s\n')
        self.entries[0] = ('#EXTINF:-1 tvg-id="BBCOne.uk@East",BBC One East', (), original)
        bad_sibling_source = f"http://{BBC_HOST}/x=4/i=urn:bbc:pips:service:bbc_radio/pc_hd_abr_v2.mpd"
        self.http.add(bad_sibling_source.replace("pc_hd_abr_v2.mpd", "pc_hd_abr_v2.fmp4.m3u8"),
                      '#EXTM3U\n#EXTINF:1,\nhttp://broken.fixture.test/not-media\n')
        self.http.add("http://broken.fixture.test/not-media", b"<html>200 but not media</html>")
        self.append("UnusableBBC.uk", "No blind sibling fallback", bad_sibling_source)
        third_party = "http://thirdparty.fixture.test/i=urn:bbc:pips:service:bbc_one/pc_hd_abr_v2.mpd"
        self.http.add(third_party.replace("pc_hd_abr_v2.mpd", "pc_hd_abr_v2.fmp4.m3u8"),
                      '#EXTM3U\n#EXTINF:1,\nhttp://segments.fixture.test/actual.ts\n')
        self.append("ThirdParty.uk", "No third-party rewriting", third_party)
        report = self.run_sync()
        self.assertIn(sibling + "\n", self.playlist())
        self.assertNotIn(original + "\n", self.playlist())
        self.assertNotEqual(self.by_id(report, "UnusableBBC.uk")["status"], "usable")
        self.assertNotEqual(self.by_id(report, "ThirdParty.uk")["status"], "usable")
        self.assertFalse(any(host == "thirdparty.fixture.test" and path.endswith(".m3u8")
                             for host, path, _ in self.http.seen))

    def test_redirect_variant_audio_init_and_segment_exact_domains(self):
        origin = "http://redirect.fixture.test/start.m3u8?source=original"
        master = "http://edge.fixture.test/master.m3u8?cdn=redirect"
        self.http.add(origin, status=302, headers={"Location": master})
        self.http.add(master, '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",DEFAULT=YES,'
                      'URI="http://audio.fixture.test/index.m3u8"\n'
                      '#EXT-X-STREAM-INF:RESOLUTION=1920x1080,BANDWIDTH=9000000,AUDIO="a"\n'
                      'http://variant.fixture.test/index.m3u8\n')
        self.http.add("http://variant.fixture.test/index.m3u8",
                      '#EXTM3U\n#EXT-X-MAP:URI="http://map-redirect.fixture.test/init.mp4"\n'
                      '#EXTINF:1,\nhttp://media-redirect.fixture.test/media.m4s\n')
        self.http.add("http://map-redirect.fixture.test/init.mp4", status=302,
                      headers={"Location": "http://init.fixture.test/init.mp4"})
        self.http.add("http://media-redirect.fixture.test/media.m4s", status=302,
                      headers={"Location": "http://segments.fixture.test/actual.m4s"})
        self.http.add("http://audio.fixture.test/index.m3u8",
                      '#EXTM3U\n#EXTINF:1,\nhttp://audio-segments.fixture.test/audio.ts\n')
        self.http.add("http://audio-segments.fixture.test/audio.ts", self.audio)
        self.entries[5] = (self.entries[5][0], (), origin)
        report = self.run_sync()
        domains = set((self.output / "tv-domains.txt").read_text().splitlines())
        expected = {"redirect.fixture.test", "edge.fixture.test", "variant.fixture.test", "map-redirect.fixture.test",
                    "media-redirect.fixture.test", "init.fixture.test", "segments.fixture.test", "audio.fixture.test",
                    "audio-segments.fixture.test"}
        self.assertTrue(expected <= domains, expected - domains)
        self.assertNotIn("fixture.test", domains)
        self.assertNotIn("source.fixture.test", domains)
        self.assertNotIn("epg.fixture.test", domains)
        self.assertIn(origin + "\n", self.playlist())
        self.assertEqual(set(self.by_id(report, "Fixture0.uk@HD")["hosts"]), expected)

    def test_drm_rejected_but_public_aes_key_and_real_ciphertext_accepted(self):
        self.assertGreater(len(self.cipher), 65536, "AES fixture must exercise a truncated CBC prefix")
        encrypted = self.hls("public-aes", "http://cipher.fixture.test/sample.ts",
                             '#EXT-X-KEY:METHOD=AES-128,URI="http://keys.fixture.test/key.bin",IV=0x' + self.iv + "\n")
        self.http.add("http://cipher.fixture.test/sample.ts", self.cipher)
        self.http.add("http://keys.fixture.test/key.bin", status=302,
                      headers={"Location": "http://keys-edge.fixture.test/key.bin"})
        self.http.add("http://keys-edge.fixture.test/key.bin", self.key)
        self.append("PublicAES.uk", "Ordinary public AES-128", encrypted)
        implicit = self.hls("public-aes-implicit", "http://cipher.fixture.test/sample.ts",
                            '#EXT-X-MEDIA-SEQUENCE:5\n#EXT-X-KEY:METHOD=AES-128,URI="http://keys.fixture.test/key.bin"\n')
        self.append("PublicAESImplicit.uk", "Ordinary AES with sequence IV", implicit)
        drm_directives = {
            "SampleAES": '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://example"\n',
            "Widevine": '#EXT-X-KEY:METHOD=AES-128,KEYFORMAT="com.widevine",URI="http://drm.fixture.test/key"\n',
            "Licence": '#EXT-X-KEY:METHOD=AES-128,URI="http://drm.fixture.test/license"\n',
        }
        for identity, directive in drm_directives.items():
            self.append(identity + ".uk", identity, self.hls(identity, prefix=directive))
        self.append("DashDRM.uk", "DASH ContentProtection", self.dash("dash-drm", '<ContentProtection schemeIdUri="urn:uuid:widevine"/>'))
        self.append("DashPssh.uk", "DASH pssh", self.dash("dash-pssh", '<pssh xmlns="urn:mpeg:cenc:2013">AAAA</pssh>'))
        bad_init = self.http.add("http://drm.fixture.test/init.mp4", self.init + mp4_box(b"pssh", b"encrypted"))
        self.append("HiddenDRM.uk", "Encrypted initialization", self.dash("hidden-drm", init=bad_init))
        self.append("KodiDRM.uk", "DRM request directives", self.hls("kodi"),
                    options=("#KODIPROP:inputstream.adaptive.license_type=com.widevine.alpha",))
        short_key = self.hls("short-key", "http://cipher.fixture.test/sample.ts",
                             '#EXT-X-KEY:METHOD=AES-128,URI="http://keys.fixture.test/short.bin"\n')
        self.http.add("http://keys.fixture.test/short.bin", b"not-16-bytes")
        self.append("ShortKey.uk", "Invalid public AES key", short_key)
        self.http.add("http://keys.fixture.test/wrong.bin", b"fedcba9876543210")
        wrong_key = self.hls("wrong-key", "http://cipher.fixture.test/sample.ts",
                            '#EXT-X-KEY:METHOD=AES-128,URI="http://keys.fixture.test/wrong.bin",IV=0x' + self.iv + "\n")
        self.append("WrongKey.uk", "Wrong key is not playable", wrong_key)
        report = self.run_sync()
        self.assertEqual(self.by_id(report, "PublicAES.uk")["status"], "usable")
        self.assertEqual(self.by_id(report, "PublicAESImplicit.uk")["status"], "usable")
        self.assertIn(encrypted + "\n", self.playlist())
        for identity in (*drm_directives, "DashDRM", "DashPssh", "HiddenDRM", "KodiDRM"):
            self.assertEqual(self.by_id(report, identity + ".uk")["status"], "drm", identity)
        self.assertNotEqual(self.by_id(report, "ShortKey.uk")["status"], "usable")
        self.assertNotEqual(self.by_id(report, "WrongKey.uk")["status"], "usable")
        domains = (self.output / "tv-domains.txt").read_text().splitlines()
        self.assertIn("keys.fixture.test", domains)
        self.assertIn("keys-edge.fixture.test", domains)
        self.assertIn("cipher.fixture.test", domains)
        self.assertNotIn("drm.fixture.test", domains)

    def test_master_cannot_hide_drm_or_unvisited_redirect_dependencies(self):
        good = self.hls("master-good")
        drm = self.hls("hidden-drm", prefix='#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://drm"\n')
        hidden = self.http.add("http://master.fixture.test/hidden-drm.m3u8",
                               '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080,BANDWIDTH=9000000\n' + good +
                               '\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=4000000\n' + drm + '\n')
        self.append("HiddenChildDRM.uk", "DRM in a later variant", hidden)
        audio = self.hls("audio-good", "http://audio.fixture.test/actual.ts")
        self.http.add("http://audio.fixture.test/actual.ts", self.audio)
        audio_master = self.http.add("http://master.fixture.test/audio-drm.m3u8",
                                    '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",DEFAULT=YES,URI="' + audio + '"\n' +
                                    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="' + drm + '"\n' +
                                    '#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=4000000,AUDIO="a"\n' + good + '\n')
        self.append("HiddenAudioDRM.uk", "DRM in alternate audio", audio_master)
        second = self.hls("later-variant", "http://late-origin.fixture.test/segment.ts")
        self.http.add("http://late-origin.fixture.test/segment.ts", status=302,
                      headers={"Location": "http://late-edge.fixture.test/segment.ts"})
        self.http.add("http://late-edge.fixture.test/segment.ts", self.ts)
        clear = self.http.add("http://master.fixture.test/all-clear.m3u8",
                              '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080,BANDWIDTH=9000000\n' + good +
                              '\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=4000000\n' + second + '\n')
        self.append("AllRoutes.uk", "All variants inspected", clear)
        report = self.run_sync()
        self.assertEqual(self.by_id(report, "HiddenChildDRM.uk")["status"], "drm")
        self.assertEqual(self.by_id(report, "HiddenAudioDRM.uk")["status"], "drm")
        self.assertEqual(self.by_id(report, "AllRoutes.uk")["status"], "usable")
        self.assertIn("late-edge.fixture.test", (self.output / "tv-domains.txt").read_text().splitlines())
        self.assertIn(clear + "\n", self.playlist())

    def test_explicit_proxy_ignores_ambient_no_proxy(self):
        report = self.run_sync(no_proxy=True)
        self.assertTrue(report["published"])
        self.assertEqual(report["published_channels"], 10)

    def test_truncated_source_transfer_preserves_previous_whole_generation(self):
        self.run_sync()
        target = os.readlink(self.output)
        before = {name: (self.output / name).read_bytes() for name in ("tv.m3u", "tv-domains.txt", "report.json")}
        result = self.run_sync(success=False, truncated_source=True)
        self.assertIn("network-error", result.stderr)
        self.assertEqual(os.readlink(self.output), target)
        self.assertEqual({name: (self.output / name).read_bytes() for name in before}, before)

    def test_oversized_complete_init_range_is_not_a_clear_prefix_proof(self):
        size = 512 * 1024 + 32
        body = self.init + b"\0" * (size - len(self.init) - 12) + mp4_box(b"pssh", b"DRM!")
        init = self.http.add("http://oversized.fixture.test/init.mp4", body)
        url = self.hls("oversized-init", "http://segments.fixture.test/actual.m4s",
                       f'#EXT-X-MAP:URI="{init}",BYTERANGE="{len(body)}@0"\n')
        self.append("OversizedInit.uk", "Must inspect the whole initialization", url)
        report = self.run_sync()
        self.assertEqual(self.by_id(report, "OversizedInit.uk")["status"], "probe-limit")
        self.assertNotIn(url + "\n", self.playlist())

    def test_hls_and_dash_require_genuine_init_and_actual_media(self):
        empty_init = self.http.add("http://broken.fixture.test/empty-init.mp4", b"")
        bogus_init = self.http.add("http://broken.fixture.test/bogus-init.mp4", mp4_box(b"ftyp", b"isom") + mp4_box(b"moov", b"no tracks"))
        fake_media = self.http.add("http://broken.fixture.test/fake.ts", b"<html>HTTP 200 is not playback proof</html>")
        empty_fragment = self.http.add("http://broken.fixture.test/no-mdat.m4s", mp4_box(b"moof", mp4_box(b"traf", b"")))
        cases = {
            "EmptyInit": self.hls("empty-init", "http://segments.fixture.test/actual.m4s", f'#EXT-X-MAP:URI="{empty_init}"\n'),
            "BogusInit": self.hls("bogus-init", "http://segments.fixture.test/actual.m4s", f'#EXT-X-MAP:URI="{bogus_init}"\n'),
            "NoInit": self.hls("no-init", "http://segments.fixture.test/actual.m4s"),
            "FakeMedia": self.hls("fake-media", fake_media),
            "NoMdat": self.hls("no-mdat", empty_fragment, '#EXT-X-MAP:URI="http://init.fixture.test/init.mp4"\n'),
            "DashFakeMedia": self.dash("fake-media", media=fake_media),
            "DashNoInit": self.dash("empty-init", init=empty_init),
        }
        for name, url in cases.items():
            self.append(name + ".uk", name, url)
        good_hls = self.hls("good-fmp4", "http://segments.fixture.test/actual.m4s",
                             '#EXT-X-MAP:URI="http://init.fixture.test/init.mp4"\n')
        self.append("GoodHLS.uk", "Verified fMP4 HLS", good_hls)
        self.append("GoodDASH.uk", "Verified clear DASH", self.dash("good-dash"))
        report = self.run_sync()
        for name in cases:
            self.assertNotEqual(self.by_id(report, name + ".uk")["status"], "usable", name)
        self.assertEqual(self.by_id(report, "GoodHLS.uk")["status"], "usable")
        self.assertEqual(self.by_id(report, "GoodDASH.uk")["status"], "usable")
        self.assertTrue(any(host == "segments.fixture.test" and path == "/actual.m4s" and "Range" in headers
                            for host, path, headers in self.http.seen))

    def test_dash_timeline_template_list_and_segment_base_fetch_real_data(self):
        self.http.add("http://dash-segments.fixture.test/video-00006-110.m4s", self.fragment)
        timeline = '<SegmentTemplate timescale="1" startNumber="5" initialization="http://init.fixture.test/init.mp4" '
        timeline += 'media="http://dash-segments.fixture.test/$RepresentationID$-$Number%05d$-$Time$.m4s">'
        timeline += '<SegmentTimeline><S t="100" d="10" r="2"/></SegmentTimeline></SegmentTemplate>'
        self.append("Timeline.uk", "DASH timeline", self.dash("timeline", template=timeline))
        segment_list = '<SegmentList><Initialization sourceURL="http://init.fixture.test/init.mp4"/>'
        segment_list += '<SegmentURL media="http://segments.fixture.test/actual.m4s"/></SegmentList>'
        self.append("List.uk", "DASH segment list", self.dash("list", template=segment_list))
        # A real sidx points past itself to the genuine fragmented media fixture.
        # The full-box fields are version/flags, reference_ID, timescale,
        # earliest_presentation_time, first_offset, reserved/reference_count.
        sidx_body = struct.pack(">IIIIIHHIII", 0, 1, 1, 0, 0, 0, 1, len(self.fragment), 10, 0)
        sidx = mp4_box(b"sidx", sidx_body)
        combined = self.init + sidx + self.fragment
        self.http.add("http://dash-base.fixture.test/file.mp4", combined)
        segment_base = f'<BaseURL>http://dash-base.fixture.test/file.mp4</BaseURL><SegmentBase indexRange="{len(self.init)}-{len(self.init) + len(sidx) - 1}">'
        segment_base += f'<Initialization range="0-{len(self.init) - 1}"/></SegmentBase>'
        self.append("Base.uk", "DASH segment base", self.dash("base", template=segment_base))
        indexed = '<SegmentTemplate duration="10" initialization="http://init.fixture.test/init.mp4" '
        indexed += 'media="http://segments.fixture.test/actual.m4s" index="http://index.fixture.test/$Number$.sidx" '
        indexed += 'bitstreamSwitching="http://switch.fixture.test/track.mp4"/>'
        self.append("IndexHosts.uk", "DASH index and switching CDNs", self.dash("index-hosts", template=indexed))
        report = self.run_sync()
        for name in ("Timeline", "List", "Base"):
            self.assertEqual(self.by_id(report, name + ".uk")["status"], "usable", self.by_id(report, name + ".uk"))
        domains = (self.output / "tv-domains.txt").read_text().splitlines()
        self.assertIn("index.fixture.test", domains)
        self.assertIn("switch.fixture.test", domains)
        self.assertTrue(any(host == "dash-segments.fixture.test" and path == "/video-00006-110.m4s"
                            for host, path, _ in self.http.seen))
        ranges = [headers.get("Range") for host, _, headers in self.http.seen if host == "dash-base.fixture.test"]
        self.assertIn(f"bytes={len(self.init)}-{len(self.init) + len(sidx) - 1}", ranges)

    def test_ip_hosted_dependencies_are_rejected_without_claiming_drm(self):
        self.append("IPManifest.uk", "Literal IPv4 manifest", "http://192.0.2.10/live.m3u8")
        self.append("IPv6Manifest.uk", "Literal IPv6 manifest", "http://[2001:db8::10]/live.m3u8")
        self.append("IPShort.uk", "Shortened IPv4 manifest", "http://127.1/live.m3u8")
        self.append("IPInteger.uk", "Integer IPv4 manifest", "http://2130706433/live.m3u8")
        self.append("IPHex.uk", "Hexadecimal IPv4 manifest", "http://0x7f000001/live.m3u8")
        self.append("IPInit.uk", "IP-hosted initialization",
                    self.hls("ip-init", "http://segments.fixture.test/actual.m4s",
                             '#EXT-X-MAP:URI="http://198.51.100.20/init.mp4"\n'))
        self.append("IPKey.uk", "IP-hosted public AES key",
                    self.hls("ip-key", prefix='#EXT-X-KEY:METHOD=AES-128,URI="http://[2001:db8::20]/key.bin"\n'))
        self.append("IPMedia.uk", "IP-hosted segment", self.hls("ip-media", "http://203.0.113.30/segment.ts"))
        redirect = self.http.add("http://ip-redirect.fixture.test/master.m3u8", status=302,
                                 headers={"Location": "http://[2001:db8::30]/master.m3u8"})
        self.append("IPRedirect.uk", "Redirected numeric CDN", redirect)
        self.append("IPDashMedia.uk", "IP-hosted DASH media",
                    self.dash("ip-dash", media="http://198.51.100.40/segment.m4s"))
        # Ingesting a source from an IP does not itself require TV playback routing.
        report = self.run_sync(source_url="http://127.0.0.1/source.m3u")
        for identity in ("IPManifest", "IPv6Manifest", "IPShort", "IPInteger", "IPHex",
                         "IPInit", "IPKey", "IPMedia", "IPRedirect", "IPDashMedia"):
            candidate = self.by_id(report, identity + ".uk")
            self.assertEqual(candidate["status"], "unsupported-ip-host", candidate)
            self.assertEqual(candidate["selection"], "rejected")
            self.assertNotIn('tvg-id="' + identity + '.uk"', self.playlist())

    def test_hls_content_steering_is_explicitly_unsupported(self):
        steering = self.http.add("http://steering.fixture.test/control.json",
                                 '{"VERSION":1,"PATHWAY-CLONES":[{"ID":"alternate","URI-REPLACEMENT":{"HOST":"unverified.fixture.test"}}]}')
        url = self.hls("content-steering", prefix=f'#EXT-X-CONTENT-STEERING:SERVER-URI="{steering}"\n')
        self.append("Steering.uk", "Unproven steering CDN", url)
        report = self.run_sync()
        self.assertEqual(self.by_id(report, "Steering.uk")["status"], "unsupported-steering")
        self.assertNotIn(url + "\n", self.playlist())
        self.assertNotIn("unverified.fixture.test", (self.output / "tv-domains.txt").read_text().splitlines())

    def test_dash_inspects_protected_lower_rep_and_resolves_all_templated_hosts(self):
        hd_init, hd_media = self.height_media[1080]
        sd_init, sd_media = self.height_media[360]
        self.http.add("http://hd.dash-cdn.fixture.test/init.mp4", hd_init)
        self.http.add("http://hd.dash-cdn.fixture.test/part-1.m4s", hd_media)
        self.http.add("http://sd.dash-cdn.fixture.test/init.mp4", sd_init)
        self.http.add("http://sd.dash-cdn.fixture.test/part-1.m4s", status=302,
                      headers={"Location": "http://sd-media-edge.fixture.test/part-1.m4s"})
        self.http.add("http://sd-media-edge.fixture.test/part-1.m4s", sd_media)
        self.http.add("http://protected.dash-cdn.fixture.test/init.mp4", sd_init + mp4_box(b"pssh", b"hidden DRM"))
        self.http.add("http://protected.dash-cdn.fixture.test/part-1.m4s", sd_media)
        def master(name, lower_id):
            body = '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT30S">'
            body += '<Period><AdaptationSet mimeType="video/mp4">'
            body += '<SegmentTemplate duration="10" initialization="http://$RepresentationID$.dash-cdn.fixture.test/init.mp4" '
            body += 'media="http://$RepresentationID$.dash-cdn.fixture.test/part-$Number$.m4s"/>'
            body += '<Representation id="hd" height="1080" bandwidth="9000000"/>'
            body += f'<Representation id="{lower_id}" height="360" bandwidth="1000000"/>'
            body += '</AdaptationSet></Period></MPD>'
            return self.http.add(f"http://dash.fixture.test/{name}.mpd", body)
        protected = master("protected-lower", "protected")
        clear = master("clear-lower", "sd")
        self.append("ProtectedLower.uk", "Hidden DRM in a lower rendition", protected)
        self.append("TemplatedCDNs.uk", "HD with clear SD alternative", clear)
        report = self.run_sync()
        self.assertEqual(self.by_id(report, "ProtectedLower.uk")["status"], "drm")
        self.assertNotIn(protected + "\n", self.playlist())
        candidate = self.by_id(report, "TemplatedCDNs.uk")
        self.assertEqual(candidate["status"], "usable", candidate)
        self.assertEqual(candidate["height"], 1080)
        self.assertIn(clear + "\n", self.playlist())
        domains = (self.output / "tv-domains.txt").read_text().splitlines()
        self.assertIn("sd.dash-cdn.fixture.test", domains)
        self.assertIn("sd-media-edge.fixture.test", domains)
        self.assertFalse(any("$" in host for host in domains))

    def test_required_bbc_minimum_and_source_failures_preserve_whole_old_generation(self):
        self.run_sync()
        target = os.readlink(self.output)
        before = {name: (self.output / name).read_bytes() for name in ("tv.m3u", "tv-domains.txt", "report.json")}
        variants = [self.entries[1:], self.entries[:2] + self.entries[3:], self.entries]
        for index, entries in enumerate(variants):
            with self.subTest(failure=index):
                result = self.run_sync(success=False, entries=entries, source_status=503 if index == 2 else None)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(os.readlink(self.output), target)
                self.assertEqual({name: (self.output / name).read_bytes() for name in before}, before)
        # A subsequent good run replaces exactly one pointer, with all artifacts fresh.
        self.append("NewChannel.uk", "Fresh channel", self.hls("fresh", host="fresh-cdn.fixture.test"))
        report = self.run_sync()
        self.assertNotEqual(os.readlink(self.output), target)
        self.assertEqual(report["published_channels"], 11)
        self.assertIn("fresh-cdn.fixture.test", (self.output / "tv-domains.txt").read_text().splitlines())
        self.assertIn(",Fresh channel\n", self.playlist())

    def test_first_failure_and_non_symlink_directory_never_publish_partial_files(self):
        self.run_sync(success=False, entries=self.entries[:2])
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        (self.output / "guard").write_text("preserve")
        self.run_sync(success=False)
        self.assertFalse(self.output.is_symlink())
        self.assertEqual((self.output / "guard").read_text(), "preserve")
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), ["guard"])

    def test_actual_height_boundary_mislabel_unknown_and_bbc_eligibility(self):
        self.entries[0] = ('#EXTINF:-1 tvg-id="BBCOne.uk@London",BBC One London (1080p) HD', (),
                           self.fmp4_hls("bbc-low-mislabeled", 360))
        self.append("BBCOne.uk@East", "BBC One East", self.fmp4_hls("bbc-719", 719))
        self.append("BBCOne.uk@ScotlandHD", "BBC One Scotland", self.fmp4_hls("bbc-eligible", 720))
        self.append("BelowBoundary.uk", "Below boundary", self.fmp4_hls("boundary-719", 719))
        self.append("AtBoundary.uk", "At boundary", self.fmp4_hls("boundary-720", 720))
        self.append("Mislabeled.uk@HD", "Advertised HD (1080p)", self.fmp4_hls("mislabeled", 360))
        self.append("Unlabelled.uk", "No quality label", self.fmp4_hls("unlabelled", 1080))
        self.http.add("http://segments.fixture.test/audio.ts", self.audio)
        self.append("UnknownVideo.uk@HD", "Claims HD but no video", self.hls("unknown-video", "http://segments.fixture.test/audio.ts"))
        low_variant = self.fmp4_hls("master-falsely-high", 360)
        good_variant = self.fmp4_hls("master-confirmed-720", 720)
        master = self.http.add("http://manifest.fixture.test/fallback/master.m3u8",
                              '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080,BANDWIDTH=9000000\n' + low_variant +
                              '\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=4000000\n' + good_variant + '\n')
        self.append("ActualVariant.uk", "Choose confirmed eligible variant", master)
        report = self.run_sync()
        for identity, height in (("BelowBoundary.uk", 719), ("Mislabeled.uk@HD", 360), ("BBCOne.uk@East", 719)):
            candidate = self.by_id(report, identity)
            self.assertEqual(candidate["status"], "low-resolution", candidate)
            self.assertEqual(candidate["height"], height)
        self.assertEqual(self.by_id(report, "AtBoundary.uk")["height"], 720)
        self.assertEqual(self.by_id(report, "Unlabelled.uk")["height"], 1080)
        self.assertEqual(self.by_id(report, "Unlabelled.uk")["selection"], "selected")
        self.assertEqual(self.by_id(report, "UnknownVideo.uk@HD")["status"], "unknown-resolution")
        self.assertEqual(self.by_id(report, "ActualVariant.uk")["height"], 720)
        self.assertEqual(self.by_id(report, "BBCOne.uk@ScotlandHD")["selection"], "selected")
        self.assertEqual(self.playlist().count(",BBC One\n"), 1)
        self.assertNotIn(",Advertised HD (1080p)\n", self.playlist())

    def test_hls_byte_ranges_keep_query_and_check_offset(self):
        # An incorrect media offset lands in nonmedia padding, not another
        # otherwise-decodable copy of the same fragment.
        media_offset = len(self.init) + 128 * 1024
        combined = self.init + b"\0" * (media_offset - len(self.init)) + self.fragment
        file_url = "http://ranges.fixture.test/media.mp4?token=keep%26value"
        self.http.add(file_url, combined)
        prefix = f'#EXT-X-MAP:URI="{file_url}",BYTERANGE="{len(self.init)}@0"\n'
        prefix += f'#EXT-X-BYTERANGE:{len(self.fragment)}@{media_offset}\n'
        self.append("Ranges.uk", "Byte-ranged fMP4", self.hls("ranges", file_url, prefix))
        report = self.run_sync()
        self.assertEqual(self.by_id(report, "Ranges.uk")["status"], "usable")
        self.assertEqual(self.by_id(report, "Ranges.uk")["height"], 720)


def main():
    global ARGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    ARGS, remaining = parser.parse_known_args()
    if not ARGS.ffmpeg:
        parser.error("--ffmpeg is required to generate genuine local media and AES fixtures")
    unittest.main(argv=[sys.argv[0], *remaining])


if __name__ == "__main__":
    main()
