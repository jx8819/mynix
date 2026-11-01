#!/usr/bin/env python3
"""Probe and atomically publish a source-driven, DRM-free UK IPTV catalogue.

Only the output-directory symlink is switched at publication. All network I/O is
bounded and goes through the explicitly supplied proxy (or directly for fixtures).
Packaged ffprobe only demuxes bounded local samples for actual video-height proof;
cryptography decrypts public AES-128 CBC samples without treating prefixes as EOF.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
from http.client import HTTPException
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
import xml.etree.ElementTree as ET


MANIFEST_LIMIT = 2 * 1024 * 1024
SOURCE_LIMIT = 4 * 1024 * 1024
INIT_LIMIT = 512 * 1024
MEDIA_LIMIT = 64 * 1024
REQUEST_LIMIT = 128
HLS_ROUTE_LIMIT = 32
DASH_ROUTE_LIMIT = 32
MIN_CHANNELS = 10
REQUIRED = {"bbc-one", "bbc-two"}
DEFAULT_AGENT = "Mozilla/5.0 (compatible; iptv-uk-sync/1.0)"
ATTR_RE = re.compile(r'([\w-]+)\s*=\s*(?:"([^"]*)"|\x27([^\x27]*)\x27|([^\s,]+))')
QUALITY_SUFFIX = re.compile(r"^(?:SD|HD|FHD|UHD|[248]K|\d{3,4}p)$", re.I)
BBC_CDN = re.compile(r"^vs-(?:cmaf|dash|hls)-[a-z0-9-]+-uk-live\.akamaized\.net$")
MP4_ENCRYPTION = (b"pssh", b"encv", b"enca", b"sinf", b"tenc")


class ProbeError(Exception):
    def __init__(self, status, message, height=0):
        super().__init__(message)
        self.status = status
        self.message = message
        self.height = height


def fail(message, status="unplayable", height=0):
    raise ProbeError(status, message, height)


def redacted_url(url):
    """Never put bearer query strings, embedded credentials or header tails in logs."""
    try:
        parts = urlsplit(url.split("|", 1)[0])
        host = parts.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parts.port:
            host += ":" + str(parts.port)
        return urlunsplit((parts.scheme, host, parts.path, "REDACTED" if parts.query else "", ""))
    except ValueError:
        return "[invalid URL]"


def http_url(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            fail("Only HTTP(S) playback URLs without embedded credentials are supported", "unsupported")
        parts.port
    except ValueError:
        fail("Malformed playback URL", "unsupported")
    return url


def split_quoted(text, delimiter=","):
    quote = None
    start = 0
    values = []
    for index, char in enumerate(text):
        if char in ('"', "'"):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        elif char == delimiter and quote is None:
            values.append(text[start:index])
            start = index + 1
    values.append(text[start:])
    return values


def tag_attrs(line):
    body = line.partition(":")[2]
    return {key.strip().upper(): value.strip().strip('"')
            for piece in split_quoted(body) if "=" in piece
            for key, value in [piece.split("=", 1)]}


@dataclass(frozen=True)
class Channel:
    index: int
    extinf: str
    name: str
    attrs: dict
    options: tuple
    url: str

    @property
    def key(self):
        identity = self.attrs.get("tvg-id", "").strip()
        base, marker, variant = identity.partition("@")
        if base.lower() in ("bbcone.uk", "bbctwo.uk") and not re.search(r"(?:plus|\+)\d+", variant, re.I):
            return "bbc-one" if base.lower() == "bbcone.uk" else "bbc-two"
        if identity:
            return "id:" + (base if marker and QUALITY_SUFFIX.fullmatch(variant) else identity).casefold()
        name = re.sub(r"\s*\[(?:geo-blocked|not 24/7)\]", "", self.name, flags=re.I)
        name = re.sub(r"\s*\(\d{3,4}p\)\s*$", "", name, flags=re.I).strip()
        if name.casefold() in ("bbc one", "bbc two"):
            return "bbc-one" if name.casefold() == "bbc one" else "bbc-two"
        return "name:" + name.casefold()

    def render(self, url):
        metadata, _ = extinf_parts(self.extinf)
        name = {"bbc-one": "BBC One", "bbc-two": "BBC Two"}.get(self.key, self.name)
        return "\n".join((metadata + "," + name, *self.options, url)) + "\n"


def extinf_parts(line):
    parts = split_quoted(line)
    if len(parts) < 2:
        fail("EXTINF is missing its display name", "invalid-source")
    return parts[0], ",".join(parts[1:])


def parse_playlist(text, base_url):
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or not lines[0].strip().startswith("#EXTM3U"):
        fail("Source is not an extended M3U playlist", "invalid-source")
    header = lines[0].strip()
    channels = []
    info = None
    options = []
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            if info is not None:
                fail("EXTINF is missing its playback URL", "invalid-source")
            info = line
            options = []
        elif line.startswith("#"):
            if info is not None:
                options.append(line)
        elif info is not None:
            metadata, name = extinf_parts(info)
            attrs = {m.group(1).lower(): next(v for v in m.groups()[1:] if v is not None)
                     for m in ATTR_RE.finditer(metadata)}
            path, separator, tail = line.partition("|")
            channels.append(Channel(len(channels), info, name, attrs, tuple(options),
                                    urljoin(base_url, path) + (separator + tail if separator else "")))
            info = None
        else:
            fail("Playback URL has no EXTINF metadata", "invalid-source")
    if info is not None or not channels:
        fail("Source is empty or incomplete", "invalid-source")
    return header, channels


def playback_request(channel):
    url, separator, tail = channel.url.partition("|")
    headers = {"User-Agent": DEFAULT_AGENT, "Accept-Encoding": "identity"}
    aliases = {"user-agent": "User-Agent", "http-user-agent": "User-Agent",
               "referer": "Referer", "referrer": "Referer", "http-referrer": "Referer",
               "http-referer": "Referer", "origin": "Origin", "http-origin": "Origin",
               "cookie": "Cookie", "http-cookie": "Cookie"}
    for key, value in channel.attrs.items():
        if key in aliases:
            headers[aliases[key]] = value
    for option in channel.options:
        lower = option.lower()
        if lower.startswith("#kodiprop:") and any(part in lower for part in
                ("license_key=", "license_type=", "drm=")):
            fail("Playback directives require a DRM licence", "drm")
        if lower.startswith("#extvlcopt:"):
            key, _, value = option.partition(":")[2].partition("=")
            if key.lower() in aliases:
                headers[aliases[key.lower()]] = value
            elif key.lower() == "http-header" and ":" in value:
                name, value = value.split(":", 1)
                headers[name.strip()] = value.strip()
        elif lower.startswith("#exthttp:"):
            try:
                values = json.loads(option.partition(":")[2])
            except (ValueError, TypeError):
                fail("Invalid EXTHTTP request headers", "unsupported")
            if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
                fail("Invalid EXTHTTP request headers", "unsupported")
            headers.update(values)
        elif lower.startswith("#kodiprop:inputstream.adaptive."):
            key, _, value = option.partition(":")[2].partition("=")
            if key.endswith(("stream_headers", "manifest_headers")):
                headers.update(parse_qsl(value, keep_blank_values=True))
    if separator:
        headers.update(parse_qsl(tail, keep_blank_values=True))
    for name, value in headers.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or "\r" in value or "\n" in value:
            fail("Invalid playback HTTP header", "unsupported")
    return http_url(url), (separator + tail if separator else ""), headers


@dataclass
class Fetched:
    data: bytes
    url: str
    headers: object
    whole: bool = True


class ExplicitProxyHandler(ProxyHandler):
    """An explicit UK proxy must never consult ambient NO_PROXY bypass rules."""
    def proxy_open(self, req, proxy, original_type):
        parts = urlsplit(proxy)
        req.set_proxy(parts.netloc, parts.scheme)
        if original_type == parts.scheme or original_type == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


class RedirectTracker(HTTPRedirectHandler):
    def __init__(self, client):
        self.client = client
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if time.monotonic() >= self.client.request_deadline:
            fail("HTTP redirects exceeded the request deadline", "probe-limit")
        self.client.track(http_url(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, proxy, timeout, headers=None, routing=True):
        self.timeout = timeout
        self.routing = routing
        self.deadline = time.monotonic() + timeout * 4
        self.headers = headers or {"User-Agent": DEFAULT_AGENT, "Accept-Encoding": "identity"}
        self.hosts = set()
        self.requests = 0
        self.keys = {}
        self.hls_proofs = {}
        self.opener = build_opener(ExplicitProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
                                   RedirectTracker(self))

    def track(self, url):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return
        try:
            host = parts.hostname.rstrip(".").lower().encode("idna").decode("ascii")
        except UnicodeError:
            fail("Invalid internationalized hostname", "unsupported")
        numeric = True
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Resolvers also accept shortened, integer, octal and hexadecimal
            # IPv4 forms; none can be represented by our DNS-only routing feed.
            numeric = re.fullmatch(r"(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}", host) is not None
        if numeric:
            if self.routing:
                fail("Literal IP playback/dependency hosts cannot use the DNS-only TV routing feed",
                     "unsupported-ip-host")
            return
        if len(host) <= 253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                   for label in host.split(".")):
            self.hosts.add(host)

    def fetch(self, url, limit=MANIFEST_LIMIT, byte_range=None, complete=True):
        http_url(url)
        self.track(url)
        self.requests += 1
        remaining = self.deadline - time.monotonic()
        if self.requests > REQUEST_LIMIT or remaining <= 0:
            fail("Candidate probe exhausted its bounded request/time allowance", "probe-limit")
        self.request_deadline = min(self.deadline, time.monotonic() + self.timeout)
        headers = dict(self.headers)
        if byte_range is not None:
            offset, length = byte_range
            if offset < 0 or length <= 0:
                fail("Invalid media byte range")
            if complete and length > limit:
                fail("Complete byte range exceeds its bounded size limit", "probe-limit")
            headers["Range"] = f"bytes={offset}-{offset + min(length, limit) - 1}"
        elif not complete:
            headers["Range"] = f"bytes=0-{limit - 1}"
        try:
            with self.opener.open(Request(url, headers=headers), timeout=min(self.timeout, remaining)) as response:
                self.track(response.url)
                if response.status not in (200, 206):
                    fail(f"Unexpected HTTP status {response.status}", "http-error")
                if byte_range is not None and byte_range[0] and response.status != 206:
                    fail("Server ignored a nonzero media byte range")
                range_end = range_total = None
                if response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                    expected_start = byte_range[0] if byte_range else 0
                    if not match or int(match.group(1)) != expected_start:
                        fail("Server returned an incorrect media byte range")
                    range_end = int(match.group(2))
                    range_length = range_end - expected_start + 1
                    range_total = None if match.group(3) == "*" else int(match.group(3))
                    if range_length <= 0 or (complete and byte_range and range_length != byte_range[1]):
                        fail("Server returned an incomplete or incorrect complete byte range", "network-error")
                    if complete and byte_range is None and (range_total is None or range_end + 1 != range_total):
                        fail("Complete resource request returned only a partial representation", "network-error")
                cap = min(limit, byte_range[1]) if byte_range else limit
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared = int(declared)
                    except ValueError:
                        fail("Invalid HTTP Content-Length", "network-error")
                    if declared < 0:
                        fail("Invalid HTTP Content-Length", "network-error")
                    if complete and declared > cap:
                        fail("Complete response exceeds its bounded size limit", "probe-limit")
                target = cap + (1 if complete else 0)
                chunks = []
                received = 0
                while received < target:
                    if time.monotonic() >= self.request_deadline:
                        fail("HTTP body exceeded the request deadline", "probe-limit")
                    chunk = response.read1(min(32768, target - received))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                data = b"".join(chunks)
                if received < target and declared is not None and received != declared:
                    fail("HTTP body ended before its declared length", "network-error")
                if complete and byte_range is not None and received != byte_range[1]:
                    fail("Complete byte-range body ended prematurely", "network-error")
                if response.status == 206 and received < cap and received != range_length:
                    fail("Partial HTTP response ended prematurely", "network-error")
                if complete and len(data) > cap:
                    fail("Manifest, initialization or key exceeds its bounded size limit", "probe-limit")
                if not data:
                    fail("Empty HTTP response")
                whole = (complete or (byte_range is not None and received == byte_range[1]) or
                         (response.status == 200 and declared is not None and received == declared) or
                         (response.status == 206 and range_total is not None and range_end + 1 == range_total))
                return Fetched(data, response.url, response.headers, whole)
        except HTTPError as exc:
            fail(f"HTTP status {exc.code}", "http-error")
        except (URLError, TimeoutError, OSError, HTTPException):
            fail("HTTP connection or read failed", "network-error")

    def key(self, url):
        if url not in self.keys:
            result = self.fetch(url, 16)
            if len(result.data) != 16:
                fail("Public AES-128 key is not exactly 16 bytes")
            self.keys[url] = result.data
        return self.keys[url]


def text_manifest(fetched):
    try:
        return fetched.data.decode("utf-8-sig")
    except UnicodeDecodeError:
        fail("Manifest is not UTF-8 text", "nonmedia")


def reject_encrypted(data):
    if any(marker in data for marker in MP4_ENCRYPTION):
        fail("Encrypted MP4 initialization or media", "drm")


def boxes(data):
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                break
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header:
            break
        end = offset + size
        yield kind, data[offset + header:min(end, len(data))], size, end <= len(data), offset
        if end > len(data):
            break
        offset = end


def valid_init(data):
    reject_encrypted(data)
    entries = list(boxes(data))
    moov = next((body for kind, body, _, complete, _ in entries if kind == b"moov" and complete), None)
    if not any(kind == b"ftyp" and complete for kind, _, _, complete, _ in entries) or moov is None:
        fail("Missing complete MP4 ftyp/moov initialization")
    if not all(marker in moov for marker in (b"trak", b"mdia", b"stsd")):
        fail("Initialization has no MP4 track/sample description")


def valid_media(data, has_init=False):
    reject_encrypted(data)
    entries = list(boxes(data))
    if any(kind == b"moof" and complete and b"traf" in body for kind, body, _, complete, _ in entries):
        if not has_init:
            fail("Fragmented MP4 media has no verified initialization")
        if any(kind == b"mdat" and len(body) > 0 for kind, body, _, _, _ in entries):
            return
        fail("Fragmented MP4 segment has no actual mdat payload")
    # Transport streams may have a leading ID3 tag; require multiple real packets.
    for offset in range(min(1024, max(0, len(data) - 376))):
        if data[offset] == data[offset + 188] == data[offset + 376] == 0x47:
            return
    # Elementary audio is valid HLS too, but HTML/error strings are not media.
    if len(data) >= 32 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:
        return
    if len(data) >= 32 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return
    fail("Response is not recognizable TS, fragmented MP4 or elementary audio", "nonmedia")


def byte_range(value, previous=0):
    match = re.fullmatch(r"(\d+)(?:@(\d+))?", value)
    if not match:
        fail("Invalid HLS byte range")
    return int(match.group(2) or previous), int(match.group(1))


def hls_key(attrs, base, client):
    method = attrs.get("METHOD", "").upper()
    if method == "NONE":
        return None
    if method != "AES-128" or attrs.get("KEYFORMAT", "identity").lower() != "identity":
        fail("HLS requires DRM/SAMPLE-AES or a non-identity key format", "drm")
    uri = attrs.get("URI", "")
    if not uri or re.search(r"(?:skd:|widevine|playready|licen[cs]e)", uri, re.I):
        fail("HLS key is a DRM or licence URI", "drm")
    url = http_url(urljoin(base, uri))
    client.track(url)
    iv = attrs.get("IV")
    if iv and not re.fullmatch(r"0[xX][0-9a-fA-F]{1,32}", iv):
        fail("Invalid AES-128 IV")
    return {"url": url, "iv": iv}


def inspect_local_media(path, ffprobe):
    """Demux local bounded bytes only; never let ffprobe make network requests."""
    if not ffprobe:
        fail("Media height proof requires the packaged local ffprobe", "unsupported")
    command = [ffprobe, "-v", "error", "-protocol_whitelist", "file,data",
               "-probesize", str(MEDIA_LIMIT), "-analyzeduration", "1000000",
               "-read_intervals", "%+0.25", "-show_entries",
               "stream=codec_name,codec_type,width,height:packet=size", "-of", "json", str(path)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=10, check=False)
        proof = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        fail("Local bounded ffprobe media inspection failed")
    streams = proof.get("streams", [])
    if (result.returncode or not any(stream.get("codec_type") in ("video", "audio") and
            stream.get("codec_name") not in (None, "unknown") for stream in streams) or
            not any(int(packet.get("size", 0)) > 0 for packet in proof.get("packets", []))):
        fail("Bounded media did not yield recognizable local streams and packets")
    return max((int(stream.get("height", 0)) for stream in streams if stream.get("codec_type") == "video"), default=0)


def clear_media_height(data, init, ffprobe):
    with tempfile.TemporaryDirectory(prefix="iptv-uk-media-") as directory:
        path = Path(directory) / ("sample.mp4" if init is not None else "sample.ts")
        path.write_bytes((init or b"") + data)
        return inspect_local_media(path, ffprobe)


def require_height(height, minimum):
    if height <= 0:
        fail("Video height could not be confirmed from actual media", "unknown-resolution")
    if height < minimum:
        fail(f"Actual media height {height} is below minimum {minimum}", "low-resolution", height)


def decrypt_aes_cbc(data, key, sequence=0, whole=False):
    """Decrypt complete CBC blocks, without inventing EOF padding for a prefix."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        fail("Public AES-128 proof requires the packaged cryptography library", "unsupported")
    if len(data) < 16 or len(data) % 16:
        fail("AES-128 sample is not a nonempty block-aligned ciphertext")
    iv = int(key["iv"], 16).to_bytes(16, "big") if key.get("iv") else sequence.to_bytes(16, "big")
    decryptor = Cipher(algorithms.AES(key["data"]), modes.CBC(iv)).decryptor()
    plaintext = decryptor.update(data)
    if whole:
        plaintext += decryptor.finalize()
        padding = plaintext[-1]
        if not 1 <= padding <= 16 or plaintext[-padding:] != bytes([padding]) * padding:
            fail("Public AES-128 key did not yield valid complete-object padding")
        plaintext = plaintext[:-padding]
    return plaintext


@dataclass
class Proof:
    protocol: str
    height: int = 0
    bandwidth: int = 0
    detail: str = ""


def hls_child(client, url, ffprobe, depth, inspect_only=False):
    cache_key = (url, inspect_only)
    if cache_key not in client.hls_proofs:
        client.hls_proofs[cache_key] = probe_hls(
            client, client.fetch(url), ffprobe, 0, depth, inspect_only)
    return client.hls_proofs[cache_key]


def probe_hls(client, fetched, ffprobe, min_height, depth=0, inspect_only=False):
    if depth > 4:
        fail("HLS nesting exceeds the probe limit", "probe-limit")
    text = text_manifest(fetched)
    if not text.lstrip().startswith("#EXTM3U"):
        fail("HLS child is not an M3U playlist", "nonmedia")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants = []
    renditions = []
    iframe_routes = []
    pending = None
    segments = []
    current_key = None
    current_map = None
    sequence = 0
    pending_range = None
    range_end = 0
    for line in lines:
        if line.startswith("#EXT-X-CONTENT-STEERING:"):
            fail("HLS content steering requires unverified downstream CDN discovery", "unsupported-steering")
        if line.startswith("#") and ":" in line:
            attrs = tag_attrs(line)
            for name in ("URI", "SERVER-URI"):
                if attrs.get(name):
                    client.track(urljoin(fetched.url, attrs[name]))
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
            key = hls_key(tag_attrs(line), fetched.url, client)
            if line.startswith("#EXT-X-KEY:"):
                current_key = key
            elif key:
                client.key(key["url"])
        elif line.startswith("#EXT-X-MEDIA:"):
            renditions.append(tag_attrs(line))
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = tag_attrs(line)
        elif line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            attrs = tag_attrs(line)
            if attrs.get("URI"):
                iframe_routes.append(urljoin(fetched.url, attrs["URI"]))
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.partition(":")[2])
            except ValueError:
                fail("Invalid HLS media sequence")
            if sequence < 0 or sequence >= 2 ** 128:
                fail("Invalid HLS media sequence")
        elif line.startswith("#EXT-X-MAP:"):
            attrs = tag_attrs(line)
            if not attrs.get("URI"):
                fail("HLS initialization map has no URI")
            current_map = (urljoin(fetched.url, attrs["URI"]),
                           byte_range(attrs["BYTERANGE"]) if "BYTERANGE" in attrs else None,
                           current_key)
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = byte_range(line.partition(":")[2], range_end)
            range_end = sum(pending_range)
        elif not line.startswith("#"):
            url = http_url(urljoin(fetched.url, line))
            client.track(url)
            if pending is not None:
                variants.append((pending, url))
                pending = None
            else:
                segments.append((url, pending_range, current_map, current_key, sequence + len(segments)))
                pending_range = None
    if variants:
        if len(variants) + len(renditions) + len(iframe_routes) > HLS_ROUTE_LIMIT:
            fail("HLS master exceeds its bounded rendition allowance", "probe-limit")
        def rank(variant):
            attrs, url = variant
            resolution = attrs.get("RESOLUTION", "0x0").lower().split("x")
            try:
                height = int(resolution[-1])
                bandwidth = int(attrs.get("AVERAGE-BANDWIDTH", attrs.get("BANDWIDTH", "0")))
            except ValueError:
                fail("Invalid HLS variant quality")
            return (-height, -bandwidth, url)
        proofs = []
        # The published URL remains the original master. Every selectable child
        # must therefore be inspected, not just the first successful rendition.
        for attrs, url in sorted(variants, key=rank):
            proof = hls_child(client, url, ffprobe, depth + 1, inspect_only)
            quality = rank((attrs, url))
            proofs.append(Proof("hls", proof.height, max(proof.bandwidth, -quality[1]),
                                "verified all HLS renditions/init/media and local codec-header height"))
        for rendition in renditions:
            if rendition.get("URI"):
                hls_child(client, urljoin(fetched.url, rendition["URI"]), ffprobe, depth + 1,
                          inspect_only or rendition.get("TYPE") not in ("AUDIO", "VIDEO"))
        for url in iframe_routes:
            hls_child(client, url, ffprobe, depth + 1, inspect_only)
        proof = max(proofs, key=lambda item: (item.height, item.bandwidth))
        if min_height:
            require_height(proof.height, min_height)
        return proof
    if not segments:
        fail("HLS has no complete media segments")
    # One segment behind the edge avoids publishing a not-yet-available live edge.
    selected = segments[-2] if len(segments) > 1 else segments[-1]
    url, media_range, init_map, key, sequence = selected
    init = None
    if init_map:
        init_url, init_range, map_key = init_map
        init = client.fetch(init_url, INIT_LIMIT, init_range).data
        if map_key:
            if not map_key.get("iv"):
                fail("Encrypted HLS initialization requires an explicit IV")
            init = decrypt_aes_cbc(init, dict(map_key, data=client.key(map_key["url"])), whole=True)
        valid_init(init)
    fetched_sample = client.fetch(url, MEDIA_LIMIT, media_range, complete=False)
    sample = fetched_sample.data
    if key:
        sample = decrypt_aes_cbc(sample, dict(key, data=client.key(key["url"])), sequence, fetched_sample.whole)
    if inspect_only:
        reject_encrypted(sample)
        if init is not None:
            valid_media(sample, has_init=True)
        elif not sample.lstrip().startswith((b"WEBVTT", b"<?xml", b"<tt", b"<tt:")):
            fail("Subtitle rendition has no recognizable media", "nonmedia")
        return Proof("hls", detail="verified subtitle playlist and dependency media")
    valid_media(sample, init is not None)
    height = clear_media_height(sample, init, ffprobe)
    return Proof("hls", height, detail="verified actual media and local codec-header height")


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def children(element, name):
    return [child for child in element if local_name(child) == name]


def first(element, name):
    return next(iter(children(element, name)), None)


def duration(value):
    if not value:
        return 0.0
    match = re.fullmatch(r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if not match:
        fail("Unsupported MPD duration", "unsupported")
    return sum(float(part or 0) * factor for part, factor in zip(match.groups(), (86400, 3600, 60, 1)))


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        fail("Dynamic MPD has no valid availability start time", "unsupported")


def inherited(nodes, name):
    attrs = {}
    timeline = None
    last_node = None
    for node in nodes:
        child = first(node, name)
        if child is not None:
            attrs.update(child.attrib)
            if first(child, "SegmentTimeline") is not None:
                timeline = first(child, "SegmentTimeline")
            last_node = child
    return attrs, timeline, last_node


def template_value(value, representation, number, segment_time):
    sentinel = "\x00"
    value = value.replace("$$", sentinel)
    values = {"RepresentationID": representation.get("id", ""),
              "Bandwidth": representation.get("bandwidth", "0"), "Number": number, "Time": segment_time}
    def replace(match):
        key, width = match.groups()
        result = values[key]
        if width:
            try:
                return str(int(result)).zfill(min(int(width), 20))
            except ValueError:
                fail("MPD numeric template contains a nonnumeric value")
        return str(result)
    value = re.sub(r"\$(RepresentationID|Bandwidth|Number|Time)(?:%0(\d{1,2})d)?\$", replace, value)
    if "$" in value:
        fail("Unsupported DASH template variable", "unsupported")
    return value.replace(sentinel, "$")


def dash_position(attrs, timeline, mpd, period):
    scale = int(attrs.get("timescale", "1"))
    start_number = int(attrs.get("startNumber", "1"))
    if scale <= 0 or start_number < 0:
        fail("Invalid DASH timescale or start number")
    dynamic = mpd.get("type") == "dynamic"
    period_seconds = duration(period.get("duration")) or duration(mpd.get("mediaPresentationDuration"))
    offset = int(attrs.get("presentationTimeOffset", "0"))
    available = None
    if dynamic:
        available = max(0, (time.time() - timestamp(mpd.get("availabilityStartTime")) -
                            duration(period.get("start"))) * scale + offset)
        if period.get("duration"):
            available = min(available, duration(period.get("duration")) * scale + offset)
    if timeline is not None:
        entries = children(timeline, "S")
        current = 0
        number = start_number
        recent = []
        for index, entry in enumerate(entries):
            current = int(entry.get("t", str(current)))
            length = int(entry.get("d", "0"))
            repeat = int(entry.get("r", "0"))
            if length <= 0 or repeat < -1:
                fail("Invalid DASH segment timeline")
            if repeat == -1:
                following = int(entries[index + 1].get("t", "0")) if index + 1 < len(entries) else None
                end = following if following is not None else available
                if end is None and period_seconds:
                    end = period_seconds * scale + offset
                if end is None:
                    fail("Unbounded DASH timeline", "unsupported")
                count = max(0, math.ceil((end - current) / length))
            else:
                count = repeat + 1
            usable = count
            if available is not None:
                usable = min(count, max(0, math.floor((available - current) / length)))
            for position in range(max(0, usable - 3), usable):
                recent.append((number + position, current + position * length))
            recent = recent[-3:]
            current += count * length
            number += count
        if not recent:
            fail("DASH timeline has no complete available media segment")
        return recent[-2] if len(recent) > 1 else recent[-1]
    length = int(attrs.get("duration", "0"))
    if length <= 0:
        fail("DASH template has no timeline or duration", "unsupported")
    if dynamic:
        position = max(0, math.floor((available - offset) / length) - 2)
    else:
        position = 0
    return start_number + position, position * length + offset


def inclusive_range(value):
    if not value:
        return None
    match = re.fullmatch(r"(\d+)-(\d+)", value)
    if not match or int(match.group(2)) < int(match.group(1)):
        fail("Invalid DASH byte range")
    return int(match.group(1)), int(match.group(2)) - int(match.group(1)) + 1


def base_url(nodes, manifest_url, client):
    url = manifest_url
    for node in nodes:
        bases = children(node, "BaseURL")
        for base in bases:
            if base.text:
                client.track(urljoin(url, base.text.strip()))
        if bases and bases[0].text:
            url = urljoin(url, bases[0].text.strip())
    return url


def track_dash_hosts(mpd, manifest_url, client):
    pending = [(mpd, manifest_url)]
    while pending:
        element, inherited_base = pending.pop()
        bases = children(element, "BaseURL")
        for base in bases:
            if base.text:
                client.track(urljoin(inherited_base, base.text.strip()))
        base = urljoin(inherited_base, bases[0].text.strip()) if bases and bases[0].text else inherited_base
        for name in ("initialization", "media", "sourceURL", "index", "bitstreamSwitching"):
            if element.get(name):
                client.track(urljoin(base, element.get(name)))
        if local_name(element) in ("Location", "PatchLocation") and element.text:
            client.track(urljoin(base, element.text.strip()))
        if local_name(element) == "UTCTiming" and element.get("value", "").startswith(("http://", "https://")):
            client.track(element.get("value"))
        pending.extend((child, base) for child in element if local_name(child) != "BaseURL")


def sidx_media(client, url, index_range):
    data = client.fetch(url, INIT_LIMIT, index_range).data
    for kind, body, size, complete, offset in boxes(data):
        if kind != b"sidx" or not complete:
            continue
        if len(body) < 24:
            break
        version = body[0]
        cursor = 12
        if version == 0:
            _, first_offset = struct.unpack_from(">II", body, cursor)
            cursor += 8
        elif version == 1 and len(body) >= 32:
            _, first_offset = struct.unpack_from(">QQ", body, cursor)
            cursor += 16
        else:
            break
        if cursor + 4 > len(body):
            break
        count = struct.unpack_from(">H", body, cursor + 2)[0]
        cursor += 4
        media_offset = index_range[0] + offset + size + first_offset
        if count and cursor + 12 <= len(body):
            reference = struct.unpack_from(">I", body, cursor)[0]
            if reference & 0x80000000:
                fail("Hierarchical DASH sidx is not supported", "unsupported")
            return media_offset, reference & 0x7FFFFFFF
    fail("DASH SegmentBase has no usable sidx media reference")


def dash_representation(client, mpd, period, adaptation, representation, manifest_url, ffprobe):
    nodes = (mpd, period, adaptation, representation)
    base = base_url(nodes, manifest_url, client)
    attrs, timeline, template = inherited(nodes, "SegmentTemplate")
    if template is not None:
        if not attrs.get("initialization") or not attrs.get("media"):
            fail("DASH template lacks initialization or media", "unsupported")
        number, segment_time = dash_position(attrs, timeline, mpd, period)
        init_url = urljoin(base, template_value(attrs["initialization"], representation, number, segment_time))
        media_url = urljoin(base, template_value(attrs["media"], representation, number, segment_time))
        init_range = media_range = None
    else:
        attrs, _, segment_list = inherited(nodes, "SegmentList")
        attrs_base, _, segment_base = inherited(nodes, "SegmentBase")
        selected = segment_list if segment_list is not None else segment_base
        if selected is None:
            fail("DASH has no bounded segment addressing", "unsupported")
        init = first(selected, "Initialization")
        if init is None:
            # Initialization elements can be inherited separately from list attributes.
            for node in reversed(nodes):
                owner = first(node, "SegmentList" if segment_list is not None else "SegmentBase")
                if owner is not None and first(owner, "Initialization") is not None:
                    init = first(owner, "Initialization")
                    break
        if init is None:
            fail("DASH has no initialization segment", "unsupported")
        init_url = urljoin(base, init.get("sourceURL", ""))
        init_range = inclusive_range(init.get("range"))
        if segment_list is not None:
            segments = children(segment_list, "SegmentURL")
            if not segments:
                for node in reversed(nodes):
                    owner = first(node, "SegmentList")
                    if owner is not None and children(owner, "SegmentURL"):
                        segments = children(owner, "SegmentURL")
                        break
            if not segments:
                fail("DASH SegmentList has no media")
            segment = segments[-2] if len(segments) > 1 else segments[0]
            media_url = urljoin(base, segment.get("media", ""))
            media_range = inclusive_range(segment.get("mediaRange"))
        else:
            index_range = inclusive_range(attrs_base.get("indexRange"))
            if index_range is None:
                fail("DASH SegmentBase has no index range", "unsupported")
            media_url = base
            media_range = sidx_media(client, base, index_range)
    initialization = client.fetch(init_url, INIT_LIMIT, init_range).data
    valid_init(initialization)
    media = client.fetch(media_url, MEDIA_LIMIT, media_range, complete=False).data
    valid_media(media, has_init=True)
    height = clear_media_height(media, initialization, ffprobe)
    return Proof("dash", height, int(representation.get("bandwidth", "0")),
                 "verified clear MP4 init/media and local codec-header height")


def probe_dash(client, fetched, ffprobe, min_height):
    text = text_manifest(fetched)
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        fail("MPD contains a DTD/entity declaration", "unsupported")
    try:
        mpd = ET.fromstring(text)
    except ET.ParseError:
        fail("Invalid DASH XML", "nonmedia")
    if local_name(mpd) != "MPD":
        fail("Response is not a DASH MPD", "nonmedia")
    for element in mpd.iter():
        if local_name(element).lower() in ("contentprotection", "pssh"):
            fail("DASH declares ContentProtection/pssh", "drm")
    track_dash_hosts(mpd, fetched.url, client)
    periods = children(mpd, "Period")
    if not periods:
        fail("DASH has no media periods")
    current_period = periods[-1] if mpd.get("type") == "dynamic" else periods[0]
    routes = []
    for period in periods:
        for adaptation in children(period, "AdaptationSet"):
            for representation in children(adaptation, "Representation"):
                media_type = representation.get("contentType", adaptation.get("contentType", ""))
                mime = representation.get("mimeType", adaptation.get("mimeType", ""))
                if not media_type:
                    media_type = mime.split("/", 1)[0]
                if not media_type:
                    media_type = "video" if representation.get("height", adaptation.get("height")) else "audio"
                if media_type in ("video", "audio"):
                    routes.append((period, media_type, adaptation, representation))
    if not routes:
        fail("DASH has no supported audio/video representations")
    if len(routes) > DASH_ROUTE_LIMIT:
        fail("DASH exceeds its bounded representation allowance", "probe-limit")
    routes.sort(key=lambda route: (
        route[0] is not current_period,
        -int(route[3].get("height", route[2].get("height", "0"))),
        -int(route[3].get("bandwidth", "0")), route[3].get("id", "")))
    proofs = []
    # The original MPD remains selectable in full. Inspect every A/V rendition,
    # including lower resolutions, rather than hiding untested DRM/CDN routes
    # behind the first successful high-quality representation.
    for period, media_type, adaptation, representation in routes:
        proof = dash_representation(client, mpd, period, adaptation, representation, fetched.url, ffprobe)
        if period is current_period and (media_type == "video" or proof.height > 0):
            proofs.append(proof)
    if not proofs:
        fail("Current DASH period has no confirmed video representation", "unknown-resolution")
    best = max(proofs, key=lambda proof: (proof.height, proof.bandwidth))
    require_height(best.height, min_height)
    return best


def bbc_hls_sibling(url):
    parts = urlsplit(url)
    if (BBC_CDN.fullmatch(parts.hostname or "") and
            re.search(r"/i=urn:bbc:pips:service:[a-zA-Z0-9_]+/", parts.path) and
            parts.path.endswith(".mpd")):
        return urlunsplit(parts._replace(path=parts.path.rsplit("/", 1)[0] + "/pc_hd_abr_v2.fmp4.m3u8"))
    return None


@dataclass
class Candidate:
    channel: Channel
    status: str
    message: str = ""
    url: str = ""
    proof: Proof = None
    hosts: set = field(default_factory=set)
    attempts: list = field(default_factory=list)
    selection: str = "rejected"
    observed_height: int = 0

    def report(self):
        result = {"index": self.channel.index, "name": self.channel.name,
                  "id": self.channel.attrs.get("tvg-id", ""), "canonical_key": self.channel.key,
                  "source_url": redacted_url(self.channel.url), "status": self.status,
                  "selection": self.selection, "attempts": self.attempts, "height": self.observed_height}
        if self.message:
            result["reason"] = self.message
        if self.proof:
            result.update(playback_url=redacted_url(self.url), protocol=self.proof.protocol,
                          height=self.proof.height, bandwidth=self.proof.bandwidth,
                          proof=self.proof.detail, height_source="local ffprobe codec headers", hosts=sorted(self.hosts))
        return result


def probe_channel(channel, proxy, timeout, ffprobe, min_height):
    attempts = []
    try:
        url, tail, headers = playback_request(channel)
    except ProbeError as exc:
        return Candidate(channel, exc.status, exc.message)
    sibling = bbc_hls_sibling(url)
    for attempt_url in ([sibling, url] if sibling else [url]):
        client = Client(proxy, timeout, headers)
        try:
            fetched = client.fetch(attempt_url)
            if fetched.data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U"):
                proof = probe_hls(client, fetched, ffprobe, min_height)
            elif fetched.data.lstrip().startswith(b"<"):
                proof = probe_dash(client, fetched, ffprobe, min_height)
            else:
                fail("Playback URL did not return HLS or DASH", "nonmedia")
            require_height(proof.height, min_height)
            attempts.append({"url": redacted_url(attempt_url), "status": "usable", "protocol": proof.protocol})
            return Candidate(channel, "usable", url=attempt_url + tail, proof=proof,
                             hosts=client.hosts, attempts=attempts)
        except ProbeError as exc:
            attempts.append({"url": redacted_url(attempt_url), "status": exc.status,
                             "reason": exc.message, "height": exc.height})
        except (ValueError, OverflowError, struct.error):
            attempts.append({"url": redacted_url(attempt_url), "status": "unplayable",
                             "reason": "Malformed numeric or binary manifest/media data"})
    last = attempts[-1]
    return Candidate(channel, last["status"], last["reason"], attempts=attempts, observed_height=last.get("height", 0))


def choose_candidates(candidates):
    groups = {}
    for candidate in candidates:
        if candidate.proof:
            groups.setdefault(candidate.channel.key, []).append(candidate)
    keys = sorted(groups, key=lambda key: (key not in REQUIRED, min(c.channel.index for c in groups[key])))
    selected = []
    used_urls = set()
    for key in keys:
        variants = sorted(groups[key], key=lambda candidate: (
            candidate.proof.protocol != "hls", -candidate.proof.height, -candidate.proof.bandwidth,
            candidate.url, candidate.channel.index))
        chosen = None
        for candidate in variants:
            if chosen is None and candidate.url not in used_urls:
                chosen = candidate
                candidate.selection = "selected"
                selected.append(candidate)
                used_urls.add(candidate.url)
            else:
                candidate.selection = "url-duplicate" if candidate.url in used_urls else "canonical-duplicate"
    return sorted(selected, key=lambda candidate: candidate.channel.index)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(output_dir, artifacts):
    """Commit all artifacts with one symlink rename, never three file renames."""
    if output_dir.exists() and not output_dir.is_symlink():
        fail("Output directory must be absent or a generation symlink; existing directory preserved", "publication-error")
    generations = output_dir.parent / ("." + output_dir.name + ".generations")
    if generations.is_symlink():
        fail("Generation storage must not be a symlink", "publication-error")
    generations.mkdir(mode=0o755, exist_ok=True)
    generation = Path(tempfile.mkdtemp(prefix="gen-", dir=generations))
    temporary_link = output_dir.parent / ("." + output_dir.name + ".next-" + uuid.uuid4().hex)
    committed = False
    try:
        generation.chmod(0o755)
        for name, content in artifacts.items():
            with (generation / name).open("x", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
        sync_directory(generation)
        sync_directory(generations)
        temporary_link.symlink_to(os.path.relpath(generation, output_dir.parent), target_is_directory=True)
        os.replace(temporary_link, output_dir)
        committed = True
        try:
            sync_directory(output_dir.parent)
        except OSError:
            # The commit already succeeded; only durability fsync is unavailable.
            pass
    finally:
        temporary_link.unlink(missing_ok=True)
        if not committed:
            shutil.rmtree(generation)
    # Keep the previous two generations for readers holding old path references.
    # Garbage collection cannot turn a successful commit into a reported failure.
    try:
        older = sorted((path for path in generations.iterdir() if path.name.startswith("gen-") and
                        path.is_dir() and not path.is_symlink()), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in older[3:]:
            if path != generation:
                shutil.rmtree(path)
    except OSError:
        pass


def run(args):
    output_dir = Path(os.path.abspath(args.output_dir))
    if output_dir == output_dir.parent:
        fail("Output directory cannot be the filesystem root", "publication-error")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists() and not output_dir.is_symlink():
        fail("Output directory must be absent or a generation symlink; existing directory preserved", "publication-error")
    with (output_dir.parent / ("." + output_dir.name + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Another UK catalogue sync is already running", "locked")
        source = Client(args.proxy, args.timeout, routing=False).fetch(args.source, SOURCE_LIMIT)
        header, channels = parse_playlist(text_manifest(source), source.url)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            candidates = list(pool.map(lambda channel: probe_channel(
                channel, args.proxy, args.timeout, args.ffprobe, args.min_height), channels))
        selected = choose_candidates(candidates)
        domains = sorted({host for candidate in selected for host in candidate.hosts})
        missing = sorted(REQUIRED - {candidate.channel.key for candidate in selected})
        report = {"generator": "iptv-uk-sync", "schema_version": 1,
                  "generated_at": datetime.now(timezone.utc).isoformat(), "source": redacted_url(args.source),
                  "source_channels": len(channels), "usable_candidates": sum(c.proof is not None for c in candidates),
                  "published_channels": len(selected), "domains": domains,
                  "requirements": {"minimum_channels": MIN_CHANNELS, "minimum_height": args.min_height, "missing": missing},
                  "published": not missing and len(selected) >= MIN_CHANNELS,
                  "candidates": [candidate.report() for candidate in candidates]}
        if not report["published"]:
            print(json.dumps(report, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
            fail("Fresh catalogue lacks BBC One/BBC Two or ten unique usable channels; previous generation preserved",
                 "catalogue-incomplete")
        artifacts = {"tv.m3u": header + "\n" + "".join(candidate.channel.render(candidate.url) for candidate in selected),
                     "tv-domains.txt": "\n".join(domains) + "\n",
                     "report.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n"}
        publish(output_dir, artifacts)
        print(f"iptv-uk-sync: published {len(selected)} unique channels and {len(domains)} exact hosts")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--proxy", default="", help="Explicit HTTP proxy; empty means direct, ignoring ambient proxy variables")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--min-height", type=int, default=720, help="Minimum confirmed video height (default: 720)")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 32 or not 0 < args.timeout <= 120:
        parser.error("workers must be in 1..32 and timeout must be in (0,120]")
    if not 1 <= args.min_height <= 16384:
        parser.error("min-height must be in 1..16384")
    try:
        http_url(args.source)
        if args.proxy:
            http_url(args.proxy)
        run(args)
    except ProbeError as exc:
        print(f"iptv-uk-sync: {exc.status}: {exc.message}", file=sys.stderr)
        return 1
    except OSError:
        print("iptv-uk-sync: filesystem operation failed; no partial generation was published", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
