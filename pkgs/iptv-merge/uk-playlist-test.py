#!/usr/bin/env python3
"""Consumer-level, offline HTTP regressions for iptv-uk-sync.

Run with --binary /nix/store/.../bin/iptv-uk-sync --ffmpeg /nix/store/.../bin/ffmpeg
and optional --ffprobe, or omit --binary to exercise the adjacent Python entry point.
Fixture media is created locally, never downloaded. The HTTP proxy resolves every .test
and BBC fixture URL itself, so the tests require no public DNS/network access.
"""

import argparse
from dataclasses import dataclass, field
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode, urljoin, urlsplit


ARGS = None
BBC_HOST = "vs-cmaf-pushb-uk-live.akamaized.net"
SOURCE = "http://source.fixture.test/uk.m3u"
HEADER = '#EXTM3U x-tvg-url="https://epg.fixture.test/guide.xml?keep=original"'
BBC_API_TEMPLATE = ("https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/"
                    "mediaset/pc/vpid/{service}/format/json")
# Public, test-only key and self-signed CA: TLS stays inside the offline fixture.
TLS_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgk2YBqPBCwAhvI3pb
fn9KH8j/M76n+27twWHdQXw3vqShRANCAASyeygHdskTBrrkL/6YdAlhqAAr4lmK
wZNmG0Y/1bVLNTmwRQdmOoAYObSsyVQYkih2kZsTs1mFL3NSblKivWsA
-----END PRIVATE KEY-----
"""
TLS_CERT = """-----BEGIN CERTIFICATE-----
MIIB8jCCAZigAwIBAgIUOKl0zpK3r2KqzFneXBY5aZItnwwwCgYIKoZIzj0EAwIw
IjEgMB4GA1UEAwwXSVBUViBvZmZsaW5lIGZpeHR1cmUgQ0EwIBcNMjYxMTAyMDE0
MDE5WhgPMjEyNjEwMDkwMTQwMTlaMCIxIDAeBgNVBAMMF0lQVFYgb2ZmbGluZSBm
aXh0dXJlIENBMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEsnsoB3bJEwa65C/+
mHQJYagAK+JZisGTZhtGP9W1SzU5sEUHZjqAGDm0rMlUGJIodpGbE7NZhS9zUm5S
or1rAKOBqTCBpjAdBgNVHQ4EFgQUyI6hOWluHf57dSUa6JM52YvnXsowHwYDVR0j
BBgwFoAUyI6hOWluHf57dSUa6JM52YvnXsowDwYDVR0TAQH/BAUwAwEB/zAOBgNV
HQ8BAf8EBAMCAYYwEwYDVR0lBAwwCgYIKwYBBQUHAwEwLgYDVR0RBCcwJYIOKi5m
aXh0dXJlLnRlc3SCE29wZW4ubGl2ZS5iYmMuY28udWswCgYIKoZIzj0EAwIDSAAw
RQIgJVXRJxvhEDf77Iyg2mV2BAC0KDmXMqpRKT8Mbp7h5BgCIQCEr9U6kXMC6TiR
/DraLHUnHS40TLffb6LnLVmzgq8v/A==
-----END CERTIFICATE-----
"""


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


def corrupt_avc_vcl(packet):
    """Invalidate AVC slices without touching length fields, SPS/PPS or SEI."""
    damaged = bytearray(packet)
    offset = changed = 0
    while offset < len(packet):
        if offset + 4 > len(packet):
            raise AssertionError("AVC fixture has an incomplete NAL length")
        length = struct.unpack_from(">I", packet, offset)[0]
        start = offset + 4
        end = start + length
        if not length or end > len(packet):
            raise AssertionError("AVC fixture does not use complete four-byte NAL lengths")
        if 1 <= packet[start] & 0x1F <= 5:
            # forbidden_zero_bit=1 and nal_unit_type=0 cannot be decoded as
            # a slice. avcC and any parameter NALs still describe real 720p.
            damaged[start:end] = b"\x80" + b"\0" * (length - 1)
            changed += 1
        offset = end
    if not changed:
        raise AssertionError("AVC fixture packet had no genuine VCL NAL to corrupt")
    return bytes(damaged)


def corrupt_mdat(data, avc=False):
    damaged = bytearray(data)
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        if size < 8 or offset + size > len(data):
            break
        if kind == b"mdat":
            body = damaged[offset + 8:offset + size]
            damaged[offset + 8:offset + size] = corrupt_avc_vcl(body) if avc else b"\0" * len(body)
        offset += size
    return bytes(damaged)


@dataclass
class Response:
    body: bytes = b""
    status: int = 200
    headers: dict = field(default_factory=dict)
    require: dict = field(default_factory=dict)
    ranges: bool = True
    declared_length: int = None


class Fixture:
    def __init__(self, tls_context):
        self.routes = {}
        self.seen = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_CONNECT(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                plain_connection, plain_rfile, plain_wfile = self.connection, self.rfile, self.wfile
                try:
                    with tls_context.wrap_socket(plain_connection, server_side=True) as connection:
                        self.connection = connection
                        with connection.makefile("rb") as rfile, connection.makefile("wb") as wfile:
                            self.rfile, self.wfile = rfile, wfile
                            self.close_connection = False
                            self.handle_one_request()
                except (ssl.SSLError, OSError):
                    pass
                finally:
                    self.connection, self.rfile, self.wfile = plain_connection, plain_rfile, plain_wfile
                    self.close_connection = True

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
        cls.ca_file = directory / "fixture-ca.pem"
        cls.ca_file.write_text(TLS_CERT)
        key_file = directory / "fixture-key.pem"
        key_file.write_text(TLS_KEY)
        cls.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls_context.load_cert_chain(cls.ca_file, key_file)
        ffmpeg = ARGS.ffmpeg
        def generate(arguments):
            result = subprocess.run([ffmpeg, "-v", "error", "-nostdin", *arguments],
                                    capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise RuntimeError("Cannot generate local media fixtures: " + result.stderr.decode(errors="replace"))
        generate(["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=10", "-t", "1",
                  "-c:v", "mpeg2video", "-g", "1", "-threads", "1", "-f", "mpegts", str(directory / "sample.ts")])
        generate(["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=10", "-t", "1",
                  "-c:v", "libx264", "-preset", "ultrafast", "-g", "1", "-threads", "1", "-movflags",
                  "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(directory / "sample.mp4")])
        cls.ts = (directory / "sample.ts").read_bytes()
        cls.init, cls.fragment = split_fragmented_mp4((directory / "sample.mp4").read_bytes())
        cls.damaged_fragment = corrupt_mdat(cls.fragment, avc=True)
        generate(["-f", "lavfi", "-i",
                  "testsrc2=size=1280x720:rate=2,noise=alls=80:allf=t+u:all_seed=819",
                  "-frames:v", "1", "-c:v", "mpeg4", "-q:v", "4", "-threads", "1",
                  "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                  "-f", "mp4", str(directory / "large-idr.mp4")])
        cls.large_init, cls.large_fragment = split_fragmented_mp4((directory / "large-idr.mp4").read_bytes())
        cls.key = b"0123456789abcdef"
        (directory / "key.bin").write_bytes(cls.key)
        cls.iv = "0" * 31 + "5"
        (directory / "key.info").write_text("key.bin\n" + str(directory / "key.bin") + "\n" + cls.iv + "\n")
        generate(["-stream_loop", "-1", "-i", str(directory / "sample.ts"), "-t", "60",
                  "-c", "copy", "-hls_time", "120",
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
        generate(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", "-c:a", "aac",
                  "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                  "-f", "mp4", str(directory / "audio.mp4")])
        cls.audio_init, cls.audio_fragment = split_fragmented_mp4((directory / "audio.mp4").read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.media_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iptv-uk-test-")
        self.root = Path(self.temp.name)
        self.output = self.root / "uk-tv"
        self.http = Fixture(self.tls_context)
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

    def run_sync(self, success=True, entries=None, source_status=None, truncated_source=False, no_proxy=False,
                 source_url=SOURCE, extra_args=()):
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
        command += ["--ffprobe", ARGS.ffprobe, *extra_args]
        environment = dict(os.environ)
        environment["SSL_CERT_FILE"] = str(self.ca_file)
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

    def assert_packet_only_height_proof(self, sample):
        # Match the old publication predicate, including its original bounds.
        command = [ARGS.ffprobe, "-v", "error", "-protocol_whitelist", "file,data",
                   "-probesize", str(64 * 1024), "-analyzeduration", "1000000",
                   "-read_intervals", "%+0.25", "-show_entries",
                   "stream=codec_name,codec_type,width,height:packet=size", "-of", "json", str(sample)]
        result = subprocess.run(command, capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        proof = json.loads(result.stdout)
        self.assertTrue(any(stream.get("codec_type") == "video" and stream.get("codec_name") == "h264" and
                            stream.get("height") == 720 for stream in proof.get("streams", [])))
        self.assertTrue(any(int(packet.get("size", 0)) > 0 for packet in proof.get("packets", [])))

    def published_entry(self, identity):
        attrs = None
        for line in self.playlist().splitlines():
            if line.startswith("#EXTINF:"):
                attrs = {match.group(1).lower(): next(value for value in match.groups()[1:] if value is not None)
                         for match in re.finditer(r'''([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')''', line)}
            elif line and not line.startswith("#") and attrs is not None:
                if attrs.get("tvg-id") == identity:
                    return attrs, line
                attrs = None
        self.fail("Published channel not found: " + identity)

    def aptv_media(self, identity):
        """Reference consumer: use APTV's inline controls, not VLC/EXTHTTP options."""
        attrs, url = self.published_entry(identity)
        url = url.partition("|")[0]
        headers = {}
        if "http-header" in attrs:
            name, value = attrs["http-header"].split("=", 1)
            headers[name] = value
        if "http-referer" in attrs:
            headers["Referer"] = attrs["http-referer"]
        if "http-user-agent" in attrs:
            headers["User-Agent"] = attrs["http-user-agent"]

        def fetch(target):
            connection = HTTPConnection("127.0.0.1", self.http.server.server_port, timeout=2)
            try:
                connection.request("GET", target, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200, target)
                return response.read()
            finally:
                connection.close()

        manifest = fetch(url).decode()
        segment = next(line for line in manifest.splitlines() if line and not line.startswith("#"))
        return fetch(urljoin(url, segment))

    def assert_aptv_media_decodes(self, identity):
        sample = self.root / "aptv-consumer.ts"
        sample.write_bytes(self.aptv_media(identity))
        result = subprocess.run(
            [ARGS.ffprobe, "-v", "error", "-protocol_whitelist", "file,data", "-select_streams", "v:0",
             "-read_intervals", "%+0.25", "-show_entries", "frame=height", "-of", "json", str(sample)],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        frames = json.loads(result.stdout).get("frames", [])
        self.assertGreaterEqual(max((frame.get("height", 0) for frame in frames), default=0), 720)

    def bbc_api(self, service, urls, template=BBC_API_TEMPLATE, priorities=None):
        priorities = priorities or ["10"] * len(urls)
        connections = [{"protocol": "https", "transferFormat": "hls", "href": url, "priority": priority}
                       for url, priority in zip(urls, priorities)]
        payload = {"media": [
            {"kind": "captions", "encoding": "", "connection": [
                {"protocol": "https", "transferFormat": "hls", "href": "https://ignored.fixture.test/captions.m3u8"}]},
            {"kind": "video", "encoding": "vp9", "connection": [
                {"protocol": "https", "transferFormat": "hls", "href": "https://ignored.fixture.test/vp9.m3u8"}]},
            *({"kind": "video", "encoding": "h264", "bitrate": bitrate,
               "connection": [*connections,
                              {"protocol": "http", "transferFormat": "hls", "href": "http://ignored.fixture.test/live.m3u8"},
                              {"protocol": "https", "transferFormat": "dash", "href": "https://ignored.fixture.test/live.mpd"}]}
              for bitrate in ("3000", "10000"))]}
        return self.http.add(template.replace("{service}", service), json.dumps(payload))

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

    def test_clean_display_names_sort_without_changing_identity_or_http_controls(self):
        agent = "Fixture Chrome/155 (TV, APTV)"
        referer = "https://watch.fixture.test/player?keep=a%26b"
        require = {"User-Agent": agent, "Referer": referer}
        options = (f"#EXTVLCOPT:http-user-agent={agent}", f"#EXTVLCOPT:http-referrer={referer}")
        cases = (
            ("SkySportsF1.uk", "  Sky   Sports F1 (720p)", "Sky Sports F1"),
            ("CBeebies.uk", "CBeebies (United Kingdom) (720p) [Geo-blocked]", "CBeebies"),
            ("AuctionHunters.uk", "Auction Hunters (Germany) GB", "Auction Hunters GB"),
            ("JudgeZ.uk", "Judge Judy (Germany)", "Judge Judy"),
            ("JudgeA.uk", "Judge Judy [Not 24/7]", "Judge Judy"),
            ("Nested.uk", "Nested [region (United Kingdom)] (720p)", "Nested"),
            ("BBCOne.uk@Plus1", "BBC One (+1) (720p)", "BBC One +1"),
            ("Alpha.uk", "alpha [Not 24/7]", "alpha"),
        )
        self.entries[0] = (self.entries[0][0].replace(
            ",BBC One", ' tvg-name="BBC One London (720p)",BBC One'), *self.entries[0][1:])
        for identity, original, _ in cases:
            segment = f"http://protected-names.fixture.test/{identity}.ts?keep=a%26b"
            url = f"http://protected-names.fixture.test/{identity}.m3u8?keep=a%26b"
            self.http.add(segment, self.ts, require=require)
            self.http.add(url, "#EXTM3U\n#EXTINF:1,\n" + segment + "\n", require=require)
            metadata = (f" TvG-NaMe='{original}'" +
                        ' tvg-logo="https://logos.fixture.test/one,two.png" group-title="UK (HD)"' +
                        f' http-user-agent="{agent}"')
            self.append(identity, original, url, options, metadata)
        report = self.run_sync()
        text = self.playlist()
        self.assertEqual(text.splitlines()[0], HEADER)
        names = [line.rsplit(",", 1)[1] for line in text.splitlines() if line.startswith("#EXTINF:")]
        self.assertEqual(names, sorted(names, key=str.casefold))
        self.assertEqual(report["published_channels"], 10 + len(cases))
        for index, (identity, original, expected) in enumerate(cases, 10):
            with self.subTest(identity=identity):
                candidate = self.by_id(report, identity)
                self.assertEqual(candidate["selection"], "selected")
                self.assertEqual(candidate["name"], original)
                self.assertEqual(candidate["index"], index)
                attrs, url = self.published_entry(identity)
                self.assertEqual(url, f"http://protected-names.fixture.test/{identity}.m3u8?keep=a%26b")
                self.assertEqual(attrs["tvg-name"], expected)
                self.assertEqual(attrs["tvg-logo"], "https://logos.fixture.test/one,two.png")
                self.assertEqual(attrs["group-title"], "UK (HD)")
                self.assertEqual(attrs["http-user-agent"], agent)
                self.assertEqual(attrs["http-referer"], referer)
                info = next(line for line in text.splitlines()
                            if line.startswith("#EXTINF:") and f'tvg-id="{identity}"' in line)
                self.assertTrue(info.endswith("," + expected), info)
                self.assertIn("\n".join((*options, url)), text)
                self.assert_aptv_media_decodes(identity)
        self.assertEqual(names.count("Judge Judy"), 2)
        self.assertLess(text.index('tvg-id="JudgeA.uk"'), text.index('tvg-id="JudgeZ.uk"'))
        self.assertIn(",BBC Scotland\n", text)
        self.assertIn(",Tiny Pop +1\n", text)
        self.assertEqual(self.published_entry("BBCOne.uk@London")[0]["tvg-name"], "BBC One")

    def test_invalid_clean_display_names_preserve_complete_generation(self):
        self.run_sync()
        target = os.readlink(self.output)
        before = {name: (self.output / name).read_bytes() for name in ("tv.m3u", "tv-domains.txt", "report.json")}
        url = self.hls("invalid-display")
        for name in ("(720p) [Geo-blocked]", "Broken [annotation", """Both "double" and 'single'"""):
            with self.subTest(name=name):
                entry = ('#EXTINF:-1 tvg-id="InvalidDisplay.uk" tvg-name="upstream",' + name, (), url)
                self.run_sync(success=False, entries=[*self.entries, entry])
                self.assertEqual(os.readlink(self.output), target)
                self.assertEqual({name: (self.output / name).read_bytes() for name in before}, before)

    def test_metadata_headers_and_query_survive_actual_child_requests(self):
        url = "http://headers.fixture.test/live.m3u8?token=history-secret-fixture&value=a%26b"
        tail = "|X-Fixture=pipe-header&User-Agent=Agent%2F1+%28x%2C+y%29"
        require = {"User-Agent": "Agent/1 (x, y)", "Referer": "https://referer.fixture.test/page?keep=yes",
                   "Origin": "https://origin.fixture.test", "X-Fixture": "pipe-header"}
        options = ('#EXTVLCOPT:http-referrer=https://referer.fixture.test/page?keep=yes',
                   '#EXTVLCOPT:http-user-agent=Agent/1 (x, y)',
                   '#EXTHTTP:{"Origin":"https://origin.fixture.test"}')
        self.http.add(url, "#EXTM3U\n#EXTINF:1,\nhttp://headers.fixture.test/segment.ts?keep=a%26b\n", require=require)
        self.http.add("http://headers.fixture.test/segment.ts?keep=a%26b", self.ts, require=require)
        metadata = ' tvg-logo="https://logos.fixture.test/one,two.png?keep=a%26b" group-title="News" user-agent="Agent/1 (x, y)"'
        self.entries[5] = ('#EXTINF:-1 tvg-id="Fixture0.uk@HD"' + metadata + ',Fixture, With Comma', options, url + tail)
        report = self.run_sync()
        self.assertIn("\n".join((*options, url + tail)), self.playlist())
        attrs, published_url = self.published_entry("Fixture0.uk@HD")
        self.assertEqual(published_url, url + tail)
        self.assertEqual(attrs["tvg-logo"], "https://logos.fixture.test/one,two.png?keep=a%26b")
        self.assertEqual(attrs["group-title"], "News")
        self.assertEqual(attrs["user-agent"], "Agent/1 (x, y)")
        self.assertEqual(attrs["http-referer"], require["Referer"])
        self.assertEqual(attrs["http-user-agent"], require["User-Agent"])
        self.assertNotIn("history-secret-fixture", json.dumps(report))
        self.assertNotIn("pipe-header", json.dumps(report))
        self.assertEqual(self.by_id(report, "Fixture0.uk@HD")["name"], "Fixture, With Comma")
        self.assertTrue(any(path == "/segment.ts?keep=a%26b" for _, path, _ in self.http.seen))

    def test_aptv_inline_controls_fetch_real_header_protected_media(self):
        referer = "https://stmify.fixture.test/watch?keep=a%26b"
        agent = "Fixture Chrome/155 (TV, APTV)"
        require = {"Referer": referer, "User-Agent": agent}
        cases = {
            "LegacyInline": (f' http-referrer="{referer}" http-user-agent="{agent}"', ()),
            "CanonicalInline": (f' http-referer="{referer}" http-user-agent="{agent}"', ()),
            "HeaderInline": (f' http-header="Referer={referer}" http-user-agent="{agent}"', ()),
            "VLC": ("", (f"#EXTVLCOPT:http-referrer={referer}", f"#EXTVLCOPT:http-user-agent={agent}")),
            "EXTHTTP": ("", ("#EXTHTTP:" + json.dumps(require),)),
            "Repeated": (f' http-referrer="{referer}" http-header="Referer={referer}" http-user-agent="{agent}"',
                         (f"#EXTVLCOPT:http-referrer={referer}",
                          f"#EXTVLCOPT:http-user-agent={agent}",
                          "#EXTHTTP:" + json.dumps({"rEfErReR": referer, "USER-AGENT": agent}),
                          "#KODIPROP:inputstream.adaptive.stream_headers=" +
                          urlencode({"Referer": referer, "User-Agent": agent}),
                          "#KODIPROP:inputstream.adaptive.manifest_headers=" +
                          urlencode({"referrer": referer, "user-agent": agent}))),
        }
        for name, (metadata, options) in cases.items():
            segment = f"http://protected.fixture.test/{name}.ts"
            url = f"http://protected.fixture.test/{name}.m3u8"
            self.http.add(segment, self.ts, require=require)
            self.http.add(url, "#EXTM3U\n#EXTINF:1,\n" + segment + "\n", require=require)
            tail = "|" + urlencode({"rEfErEr": referer, "user-agent": agent}) if name == "Repeated" else ""
            self.append(name + ".uk", name, url + tail, options, metadata)
        conflict = "http://protected.fixture.test/conflict.m3u8"
        self.append("ConflictingControls.uk", "Contradictory controls", conflict,
                    ("#EXTHTTP:" + json.dumps(require),), ' http-referer="https://wrong.fixture.test/"')
        report = self.run_sync()
        for name in cases:
            with self.subTest(source=name):
                self.assertEqual(self.by_id(report, name + ".uk")["status"], "usable")
                attrs, _ = self.published_entry(name + ".uk")
                self.assertEqual(attrs["http-referer"], referer)
                self.assertEqual(attrs["http-user-agent"], agent)
                info = next(line for line in self.playlist().splitlines()
                            if line.startswith("#EXTINF:") and f'tvg-id="{name}.uk"' in line)
                self.assertEqual(len(re.findall(r"(?<![\w-])http-referer\s*=", info)), 1)
                self.assertEqual(len(re.findall(r"(?<![\w-])http-user-agent\s*=", info)), 1)
                self.assert_aptv_media_decodes(name + ".uk")
        self.assertEqual(self.by_id(report, "ConflictingControls.uk")["status"], "unsupported")
        self.assertFalse(any(path == "/conflict.m3u8" for _, path, _ in self.http.seen))
        ordinary, _ = self.published_entry("Fixture0.uk@HD")
        self.assertNotIn("http-user-agent", ordinary)
        self.assertNotIn("http-referer", ordinary)

    def test_conflicting_preserved_controls_are_rejected_before_protected_media_requests(self):
        referer = "https://stmify.fixture.test/watch"
        wrong = "https://wrong.fixture.test/"
        agent = "Fixture Chrome/155"
        require = {"Referer": referer, "User-Agent": agent, "X-Fixture": "B"}
        cases = {
            "VLCTail": ("", (f"#EXTVLCOPT:http-referrer={wrong}",)),
            "EXTHTTPTail": ("", ("#EXTHTTP:" + json.dumps({"referer": wrong}),)),
            "KodiTail": ("", ("#KODIPROP:inputstream.adaptive.stream_headers=" + urlencode({"Referrer": wrong}),)),
            "KodiBoth": ("", (
                "#KODIPROP:inputstream.adaptive.STREAM_HEADERS=" + urlencode({"Referrer": wrong}),
                "#KODIPROP:inputstream.adaptive.manifest_headers=" + urlencode({"Referer": referer}))),
            "LegacyInline": (f' http-referrer="{wrong}"', ()),
            "DuplicateInline": (f' http-referer="{wrong}" http-referer="{referer}"', ()),
            "DuplicateJSON": ("", (f'#EXTHTTP:{{"Referer":"{wrong}","Referer":"{referer}"}}',)),
            "HeaderAliases": ("", (f"#EXTVLCOPT:http-header=rEfErReR:{wrong}",
                                   "#EXTHTTP:" + json.dumps({"rEfErEr": referer}))),
            "UserAgentTail": ("", ("#EXTVLCOPT:http-user-agent=wrong agent",)),
            "HeaderNameCase": ("", ("#EXTVLCOPT:http-header=X-Fixture:A",)),
        }
        for name, (metadata, options) in cases.items():
            segment = f"http://protected-conflict.fixture.test/{name}.ts"
            url = f"http://protected-conflict.fixture.test/{name}.m3u8"
            self.http.add(segment, self.ts, require=require)
            self.http.add(url, "#EXTM3U\n#EXTINF:1,\n" + segment + "\n", require=require)
            self.append(name + ".uk", name, url + "|" + urlencode(require), options, metadata)
        report = self.run_sync()
        for name in cases:
            with self.subTest(source=name):
                self.assertEqual(self.by_id(report, name + ".uk")["status"], "unsupported")
                self.assertNotIn(f'tvg-id="{name}.uk"', self.playlist())
        self.assertFalse(any(host == "protected-conflict.fixture.test" for host, _, _ in self.http.seen))

    def test_bbc_api_recovers_broken_or_missing_one_and_english_news(self):
        low = self.fmp4_hls("bbc-api-sd", 360)
        high = self.fmp4_hls("bbc-api-hd", 720)
        masters = {}
        for service in ("bbc_one_london", "bbc_news_channel_hd"):
            masters[service] = self.http.add(
                f"https://bbc-live.fixture.test/{service}/master.m3u8",
                '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=640x360,BANDWIDTH=500000\n' + low +
                '\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=3000000\n' + high + "\n")
            self.bbc_api(service, [masters[service], masters[service]])
        damaged = self.http.add("http://broken.fixture.test/bbc-one.m4s", self.damaged_fragment)
        broken_one = self.hls("bbc-one-zero-frames", damaged, '#EXT-X-MAP:URI="http://init.fixture.test/init.mp4"\n')
        pashto = self.hls("pashto-valid", host="pashto.fixture.test")
        flags = ["--bbc-service", "BBCOne.uk=bbc_one_london",
                 "--bbc-service", "BBCNews.uk=bbc_news_channel_hd",
                 "--exclude-id", "bbcnewspashto.uk"]
        for mode in ("broken", "missing"):
            with self.subTest(source=mode):
                entries = self.entries[1:]
                if mode == "broken":
                    entries = [('#EXTINF:-1 tvg-id="BBCOne.uk@East",BBC One East', (), broken_one), *entries,
                               ('#EXTINF:-1 tvg-id="BBCNews.uk@UKHD",BBC News upstream', (), "http://192.0.2.10/news")]
                entries = [*entries, ('#EXTINF:-1 tvg-id="BBCNewsPashto.uk@HD",BBC News Pashto', (), pashto)]
                self.http.seen.clear()
                report = self.run_sync(entries=entries, extra_args=flags, no_proxy=True)
                for identity, service, label in (("BBCOne.uk", "bbc_one_london", "BBC One"),
                                                 ("BBCNews.uk", "bbc_news_channel_hd", "BBC News")):
                    candidates = [candidate for candidate in report["candidates"] if candidate["id"] == identity]
                    self.assertEqual(len(candidates), 1, "Duplicate bitrate records must yield one master probe")
                    self.assertEqual(candidates[0]["selection"], "selected")
                    self.assertEqual(candidates[0]["height"], 720)
                    self.assertGreaterEqual(candidates[0]["index"], report["source_channels"])
                    self.assertIn(masters[service] + "\n", self.playlist())
                    self.assertEqual(self.playlist().count("," + label + "\n"), 1)
                names = [line.rsplit(",", 1)[1] for line in self.playlist().splitlines()
                         if line.startswith("#EXTINF:")]
                self.assertEqual(names, sorted(names, key=str.casefold))
                self.assertEqual([name for name in names if name in ("BBC News", "BBC One", "BBC Two")],
                                 ["BBC News", "BBC One", "BBC Two"])
                excluded = self.by_id(report, "BBCNewsPashto.uk@HD")
                self.assertEqual(excluded["status"], "excluded")
                self.assertEqual(excluded["attempts"], [])
                self.assertFalse(any(host == "pashto.fixture.test" for host, _, _ in self.http.seen))
                self.assertFalse(any(host == "ignored.fixture.test" for host, _, _ in self.http.seen))
                domains = (self.output / "tv-domains.txt").read_text().splitlines()
                self.assertIn("bbc-live.fixture.test", domains)
                self.assertNotIn("open.live.bbc.co.uk", domains)
                self.assertNotIn(",BBC News Pashto\n", self.playlist())
                if mode == "broken":
                    self.assertEqual(self.by_id(report, "BBCOne.uk@East")["status"], "unknown-resolution")
                    self.assertEqual(self.by_id(report, "BBCNews.uk@UKHD")["status"], "unsupported-ip-host")

    def test_bbc_service_priority_selects_healthy_official_primary_not_lexical_cdn(self):
        upstream = self.fmp4_hls("bbc-upstream-1080", 1080)
        self.entries[0] = ('#EXTINF:-1 tvg-id="BBCOne.uk@East",BBC One East', (), upstream)
        default = self.run_sync()
        self.assertEqual(self.by_id(default, "BBCOne.uk@East")["selection"], "selected")
        child = self.fmp4_hls("official-bbc-720", 720)
        master = '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=3000000\n' + child + "\n"
        secondary = self.http.add("https://aa-cdn.fixture.test/master.m3u8", master)
        primary = self.http.add("https://zz-primary.fixture.test/master.m3u8", master)
        failed = "https://failed-primary.fixture.test/master.m3u8"
        self.bbc_api("bbc_one_london", [secondary, primary, failed], priorities=["20", "10", "1"])
        report = self.run_sync(extra_args=["--bbc-service", "BBCOne.uk=bbc_one_london"])
        official = [candidate for candidate in report["candidates"] if candidate["id"] == "BBCOne.uk"]
        selected = next(candidate for candidate in official if candidate["selection"] == "selected")
        self.assertEqual(selected["playback_url"], primary)
        self.assertEqual(selected["height"], 720)
        self.assertEqual(self.by_id(report, "BBCOne.uk@East")["selection"], "canonical-duplicate")
        self.assertTrue(any(candidate["status"] == "http-error" for candidate in official))
        self.assertIn(primary + "\n", self.playlist())
        self.assertNotIn(secondary + "\n", self.playlist())
        self.assertNotIn(upstream + "\n", self.playlist())

    def test_bbc_endpoint_override_and_api_errors_preserve_complete_generation(self):
        template = "http://api.fixture.test/select/{service}?format=json"
        master = self.http.add("https://bbc-live.fixture.test/news.m3u8",
                               "#EXTM3U\n#EXTINF:1,\nhttp://segments.fixture.test/actual.ts\n")
        endpoint = self.bbc_api("bbc_news_channel_hd", [master], template)
        flags = ["--bbc-service", "BBCNews.uk=bbc_news_channel_hd", "--bbc-api-template", template]
        report = self.run_sync(extra_args=flags)
        self.assertEqual(self.by_id(report, "BBCNews.uk")["selection"], "selected")
        self.assertNotIn("api.fixture.test", (self.output / "tv-domains.txt").read_text().splitlines())
        target = os.readlink(self.output)
        before = {name: (self.output / name).read_bytes() for name in ("tv.m3u", "tv-domains.txt", "report.json")}
        for body, status in (("not JSON", 200), ('{"media":[]}', 200), ("", 503)):
            with self.subTest(response=(body, status)):
                self.http.add(endpoint, body, status=status)
                self.run_sync(success=False, extra_args=flags)
                self.assertEqual(os.readlink(self.output), target)
                self.assertEqual({name: (self.output / name).read_bytes() for name in before}, before)
        self.bbc_api("bbc_news_channel_hd", [master], template)
        damaged = self.http.add("http://broken.fixture.test/required-news.m4s", self.damaged_fragment)
        zero_frames = ('#EXTM3U\n#EXT-X-MAP:URI="http://init.fixture.test/init.mp4"\n'
                       '#EXTINF:1,\n' + damaged + "\n")
        low = self.fmp4_hls("required-news-low", 360)
        for body, status, headers in ((zero_frames, 200, {}), ("", 404, {}),
                                      ("", 302, {"Location": low})):
            with self.subTest(media=(status, headers)):
                self.http.add(master, body, status=status, headers=headers)
                result = self.run_sync(success=False, extra_args=flags)
                failed = json.loads(next(line for line in result.stderr.splitlines() if line.startswith("{")))
                self.assertIn("bbc-news", failed["requirements"]["missing"])
                self.assertNotEqual(self.by_id(failed, "BBCNews.uk")["status"], "usable")
                self.assertEqual(os.readlink(self.output), target)
                self.assertEqual({name: (self.output / name).read_bytes() for name in before}, before)

    def test_bbc_arguments_cannot_inject_query_or_playlist_metadata(self):
        self.run_sync()
        target = os.readlink(self.output)
        before = {name: (self.output / name).read_bytes() for name in ("tv.m3u", "tv-domains.txt", "report.json")}
        cases = [
            ["--bbc-service", "BBCOne.uk=bbc_one_london?other=value"],
            ["--bbc-service", 'BBCOne.uk" injected="x=bbc_one_london'],
            ["--bbc-api-template", "https://api.fixture.test/{service.__class__}"],
            ["--bbc-api-template", "https://{service}.fixture.test/select"],
        ]
        for flags in cases:
            with self.subTest(arguments=flags):
                self.http.seen.clear()
                self.run_sync(success=False, extra_args=flags)
                self.assertEqual(self.http.seen, [])
                self.assertEqual(os.readlink(self.output), target)
                self.assertEqual({name: (self.output / name).read_bytes() for name in before}, before)

    def test_exclusions_precede_source_and_supplement_requests_without_bypassing_required_bbc(self):
        for identity in ("BBCNewsPashto.uk@HD", "bbcnewspashto.UK@Main"):
            self.append(identity, "BBC News Pashto", self.hls(identity, host="pashto.fixture.test"))
        flags = ["--exclude-id", "BBCNEWSPASHTO.UK", "--exclude-id", "Unlisted.uk"]
        report = self.run_sync(extra_args=flags)
        for identity in ("BBCNewsPashto.uk@HD", "bbcnewspashto.UK@Main"):
            candidate = self.by_id(report, identity)
            self.assertEqual(candidate["status"], "excluded")
            self.assertEqual(candidate["attempts"], [])
        self.assertEqual(report["published_channels"], 10)
        self.assertEqual(self.by_id(report, "BBCOne.uk@London")["selection"], "selected")
        self.assertEqual(self.by_id(report, "Fixture0.uk@HD")["selection"], "selected")
        self.assertFalse(any(host in ("pashto.fixture.test", "open.live.bbc.co.uk")
                             for host, _, _ in self.http.seen))
        target = os.readlink(self.output)
        before = {name: (self.output / name).read_bytes() for name in ("tv.m3u", "tv-domains.txt", "report.json")}
        self.http.seen.clear()
        result = self.run_sync(success=False, extra_args=[*flags, "--bbc-service",
                                                         "BBCNewsPashto.uk=bbc_news_channel_hd"])
        failed = json.loads(next(line for line in result.stderr.splitlines() if line.startswith("{")))
        self.assertEqual(self.by_id(failed, "BBCNewsPashto.uk")["status"], "excluded")
        self.assertIn("id:bbcnewspashto.uk", failed["requirements"]["missing"])
        self.assertFalse(any(host in ("pashto.fixture.test", "open.live.bbc.co.uk")
                             for host, _, _ in self.http.seen))
        self.assertEqual(os.readlink(self.output), target)
        self.assertEqual({name: (self.output / name).read_bytes() for name in before}, before)
        self.http.seen.clear()
        self.run_sync(success=False, extra_args=[*flags, "--exclude-id", "bbcONE.uk",
                                                "--bbc-service", "BBCOne.uk=bbc_one_london"])
        self.assertEqual(os.readlink(self.output), target)
        self.assertEqual({name: (self.output / name).read_bytes() for name in before}, before)
        self.assertFalse(any(host == "open.live.bbc.co.uk" or path == "/bbc-one/index.m3u8"
                             for host, path, _ in self.http.seen))

    def test_intact_video_metadata_and_packets_do_not_prove_decodable_video(self):
        sample = self.root / "zero-frame.mp4"
        sample.write_bytes(self.init + self.damaged_fragment)
        self.assert_packet_only_height_proof(sample)
        result = subprocess.run([ARGS.ffprobe, "-v", "error", "-protocol_whitelist", "file,data",
                                 "-show_frames", "-show_entries", "frame=media_type", "-of", "json", str(sample)],
                                capture_output=True, timeout=10, check=False)
        self.assertFalse(any(frame.get("media_type") == "video" for frame in json.loads(result.stdout).get("frames", [])))
        media = self.http.add("http://broken.fixture.test/zero-frame.m4s", self.damaged_fragment)
        hls = self.hls("zero-frame", media, '#EXT-X-MAP:URI="http://init.fixture.test/init.mp4"\n')
        dash = self.dash("zero-frame", media=media)
        self.append("ZeroFrameHLS.uk", "Unusable despite HD metadata", hls)
        self.append("ZeroFrameDASH.uk", "Unusable despite HD metadata", dash)
        report = self.run_sync()
        for identity, url in (("ZeroFrameHLS.uk", hls), ("ZeroFrameDASH.uk", dash)):
            candidate = self.by_id(report, identity)
            self.assertEqual(candidate["status"], "unknown-resolution")
            self.assertEqual(candidate["height"], 0)
            self.assertNotIn(url + "\n", self.playlist())

    def test_audio_frames_cannot_validate_an_undecodable_video_track(self):
        sample = self.root / "audio-video.mp4"
        result = subprocess.run(
            [ARGS.ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=2",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
             "-c:v", "libx264", "-preset", "ultrafast", "-g", "1",
             "-c:a", "aac", "-threads", "1", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
             "-f", "mp4", str(sample)], capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        result = subprocess.run(
            [ARGS.ffprobe, "-v", "error", "-protocol_whitelist", "file,data", "-select_streams", "v:0",
             "-show_entries", "packet=pos,size", "-of", "json", str(sample)],
            capture_output=True, timeout=10, check=False)
        packets = json.loads(result.stdout).get("packets", [])
        self.assertTrue(packets)
        data = bytearray(sample.read_bytes())
        for packet in packets:
            start, size = int(packet["pos"]), int(packet["size"])
            data[start:start + size] = corrupt_avc_vcl(data[start:start + size])
        sample.write_bytes(data)
        self.assert_packet_only_height_proof(sample)
        result = subprocess.run(
            [ARGS.ffprobe, "-v", "error", "-protocol_whitelist", "file,data", "-show_frames",
             "-show_entries", "frame=media_type", "-of", "json", str(sample)],
            capture_output=True, timeout=10, check=False)
        frames = json.loads(result.stdout).get("frames", [])
        self.assertTrue(any(frame.get("media_type") == "audio" for frame in frames))
        self.assertFalse(any(frame.get("media_type") == "video" for frame in frames))
        initialization, fragment = split_fragmented_mp4(data)
        init = self.http.add("http://broken.fixture.test/av-init.mp4", initialization)
        media = self.http.add("http://broken.fixture.test/av.m4s", fragment)
        hls = self.hls("audio-no-video", media, f'#EXT-X-MAP:URI="{init}"\n')
        self.append("AudioWithoutVideoHLS.uk", "A/V stream with no decoded video", hls)
        self.append("AudioWithoutVideoDASH.uk", "A/V stream with no decoded video",
                    self.dash("audio-no-video", init=init, media=media))
        report = self.run_sync()
        for identity in ("AudioWithoutVideoHLS.uk", "AudioWithoutVideoDASH.uk"):
            self.assertEqual(self.by_id(report, identity)["status"], "unknown-resolution")
            self.assertNotIn('tvg-id="' + identity + '"', self.playlist())

    def test_large_real_idr_is_decoded_within_the_bounded_media_budget(self):
        self.assertGreater(len(self.large_fragment), 64 * 1024)
        self.assertLess(len(self.large_fragment), 2 * 1024 * 1024)
        init = self.http.add("http://large.fixture.test/init.mp4", self.large_init)
        media = self.http.add("http://large.fixture.test/frame.m4s", self.large_fragment)
        hls = self.hls("large-idr", media, f'#EXT-X-MAP:URI="{init}"\n')
        dash = self.dash("large-idr", init=init, media=media)
        self.append("LargeHLS.uk", "Large genuine HD IDR", hls)
        self.append("LargeDASH.uk", "Large genuine HD IDR", dash)
        report = self.run_sync()
        for identity in ("LargeHLS.uk", "LargeDASH.uk"):
            candidate = self.by_id(report, identity)
            self.assertEqual(candidate["status"], "usable", candidate)
            self.assertEqual(candidate["height"], 720)
        requests = [headers["Range"] for host, path, headers in self.http.seen
                    if host == "large.fixture.test" and path == "/frame.m4s"]
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(int(value.split("-")[1]) < 2 * 1024 * 1024 for value in requests))

    def test_audio_renditions_require_real_decoded_audio_not_just_packets_or_video(self):
        init = self.http.add("http://bad-audio.fixture.test/init.mp4", self.audio_init)
        media = self.http.add("http://bad-audio.fixture.test/segment.m4s", corrupt_mdat(self.audio_fragment))
        bad_audio = self.hls("zero-frame-audio", media, f'#EXT-X-MAP:URI="{init}"\n')
        video_only_audio = self.hls("video-not-audio")
        for name, audio in (("ZeroAudio", bad_audio), ("VideoNotAudio", video_only_audio)):
            master = self.http.add(
                f"http://manifest.fixture.test/{name}/master.m3u8",
                '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",DEFAULT=YES,URI="' + audio + '"\n'
                '#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=3000000,AUDIO="a"\n' + self.entries[5][2] + "\n")
            self.append(name + ".uk", "Audio frame proof", master)
        audio_segment = self.http.add("http://audio.fixture.test/only-audio.ts", self.audio)
        audio_variant = self.hls("audio-as-video", audio_segment)
        master = self.http.add(
            "http://manifest.fixture.test/audio-as-video/master.m3u8",
            '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=3000000\n' + self.entries[5][2] +
            '\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=1000000\n' + audio_variant + "\n")
        self.append("AudioAsVideoVariant.uk", "Audio cannot prove advertised video", master)
        for name, codecs in (("AudioAsVideoCodecs", "avc1.4d401f,mp4a.40.2"),
                             ("GenuineAudioVariant", "mp4a.40.2")):
            master = self.http.add(
                f"http://manifest.fixture.test/{name}/master.m3u8",
                '#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=3000000\n' + self.entries[5][2] +
                '\n#EXT-X-STREAM-INF:CODECS="' + codecs + '",BANDWIDTH=1000000\n' + audio_variant + "\n")
            self.append(name + ".uk", "Codec declaration must match decoded media", master)
        report = self.run_sync()
        for identity in ("ZeroAudio.uk", "VideoNotAudio.uk", "AudioAsVideoVariant.uk", "AudioAsVideoCodecs.uk"):
            self.assertNotEqual(self.by_id(report, identity)["status"], "usable")
            self.assertNotIn('tvg-id="' + identity + '"', self.playlist())
        self.assertEqual(self.by_id(report, "AudioAsVideoCodecs.uk")["status"], "unknown-resolution")
        self.assertEqual(self.by_id(report, "GenuineAudioVariant.uk")["status"], "usable")
        self.assertEqual(self.by_id(report, "GenuineAudioVariant.uk")["height"], 720)

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
        self.assertGreater(len(self.cipher), 2 * 1024 * 1024, "AES fixture must exercise a truncated CBC prefix")
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
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe"))
    ARGS, remaining = parser.parse_known_args()
    if not ARGS.ffmpeg:
        parser.error("--ffmpeg is required to generate genuine local media and AES fixtures")
    if not ARGS.ffprobe:
        ARGS.ffprobe = str(Path(ARGS.ffmpeg).with_name("ffprobe"))
    unittest.main(argv=[sys.argv[0], *remaining])


if __name__ == "__main__":
    main()
