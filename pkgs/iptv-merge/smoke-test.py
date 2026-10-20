#!/usr/bin/env python3
"""Offline regression smoke for a built, default-configured iptv-merge executable."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


HEADER = '#EXTM3U x-tvg-url="https://epg.example/guide.xml?fcc=metadata"\n'
NEWS = '#EXTINF:-1 tvg-id="news" tvg-logo="https://logo.example/news?fcc=metadata" group-title="News",News\n'
SPORT = '#EXTINF:-1 tvg-id="sport" group-title="Sport",Sport\n'
CINEMA = '#EXTINF:-1 tvg-id="cinema" group-title="Cinema",Cinema\n'
FCC_GATEWAY = "192.0.2.10:5140"
LEGACY_GATEWAY = "192.0.2.20:4022"
FCC_ADDRESS = "198.51.100.30"
FCC_PORT = "8027"


def source(gateway):
    return (
        HEADER
        + NEWS
        + f"http://{gateway}/udp/239.1.2.3:1234\n"
        + SPORT
        + f"http://{gateway}/rtp/239.1.2.4:5678?token=a%26b&fcc=203.0.113.1:6000&fcc-type=telecom&quality=high&token=second&fcc=203.0.113.2:7000&fcc-type=huawei\n"
        + CINEMA
        + f"http://{gateway}/udp/239.1.2.5:9012?fcc-type=telecom&fcc=203.0.113.3:8000#track\n"
        + NEWS
        + f"http://{gateway}/udp/239.1.2.6:1234\n"
    )


def expected_fcc(protocol):
    return (
        HEADER
        + NEWS
        + f"http://{FCC_GATEWAY}/rtp/239.1.2.3:1234?fcc={FCC_ADDRESS}:{FCC_PORT}&fcc-type={protocol}\n"
        + SPORT
        + f"http://{FCC_GATEWAY}/rtp/239.1.2.4:5678?token=a%26b&quality=high&token=second&fcc={FCC_ADDRESS}:{FCC_PORT}&fcc-type={protocol}\n"
        + CINEMA
        + f"http://{FCC_GATEWAY}/rtp/239.1.2.5:9012?fcc={FCC_ADDRESS}:{FCC_PORT}&fcc-type={protocol}#track\n"
    )


def expected_legacy():
    # Existing duplicate-name filtering removes only the second News pair.
    return source(LEGACY_GATEWAY).rsplit(NEWS, 1)[0]


def playlist_content(path):
    # Compare playback directives and URLs, not generated status-comment wording.
    return "".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not line.startswith("# ")
    )


def main():
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} /path/to/built/iptv-merge")
    executable = Path(sys.argv[1]).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise SystemExit(f"not an executable file: {executable}")

    requests = []
    malformed = {
        "/bad-header.m3u": "not an M3U\n" + NEWS + "http://proxy.example:5140/udp/239.1.2.3:1234\n",
        "/unpaired.m3u": HEADER + NEWS + "http://proxy.example:5140/udp/239.1.2.3:1234\n" + SPORT,
        "/unsupported-path.m3u": HEADER + NEWS + "http://proxy.example:5140/udp/239.1.2.3:1234\n" + SPORT + "http://proxy.example:5140/play/239.1.2.4:5678\n",
        "/unsupported-protocol.m3u": HEADER + NEWS + "rtp://239.1.2.3:1234\n",
        "/bad-group.m3u": HEADER + NEWS + "http://proxy.example:5140/rtp/239.1.2.999:1234\n",
        "/unicast-group.m3u": HEADER + NEWS + "http://proxy.example:5140/rtp/192.0.2.1:1234\n",
        "/bad-port.m3u": HEADER + NEWS + "http://proxy.example:5140/rtp/239.1.2.3:65536\n",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = urlsplit(self.path)
            requests.append(request.path)
            if request.path == "/valid.m3u":
                body = source(LEGACY_GATEWAY)
            elif request.path in malformed:
                body = malformed[request.path]
            else:
                self.send_error(404)
                return
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="iptv-merge-smoke-") as directory:
            root = Path(directory)
            destination = root / "out.m3u"
            upstream = f"http://127.0.0.1:{server.server_port}"
            environment = {key: value for key, value in os.environ.items() if not key.startswith("IPTV_")}
            environment.update({
                "IPTV_MODE": "fcc",
                "IPTV_DST": str(destination),
                "IPTV_SRC_MULTICAST": upstream + "/valid.m3u?feed=fixture",
                "IPTV_UDPXY": LEGACY_GATEWAY,
                "IPTV_FCC_GATEWAY": FCC_GATEWAY,
                "IPTV_FCC_ADDRESS": FCC_ADDRESS,
                "IPTV_FCC_PORT": FCC_PORT,
                "IPTV_FCC_PROTOCOL": "huawei",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            })

            def run(settings, succeeds):
                result = subprocess.run(
                    [str(executable)], env=settings, cwd=root,
                    text=True, capture_output=True, timeout=30,
                )
                if (result.returncode == 0) != succeeds:
                    raise AssertionError(
                        f"unexpected exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                    )
                leftovers = [path for path in root.iterdir() if path != destination]
                if leftovers:
                    raise AssertionError(f"temporary files were not cleaned: {leftovers}")
                return result

            for protocol in ("huawei", "telecom"):
                settings = environment.copy()
                if protocol == "telecom":
                    del settings["IPTV_FCC_PROTOCOL"]  # The neutral CLI default.
                run(settings, True)
                assert playlist_content(destination) == expected_fcc(protocol)

            legacy = environment.copy()
            legacy.update({
                "IPTV_MODE": "multicast",
                "IPTV_FCC_GATEWAY": "not a gateway",
                "IPTV_FCC_ADDRESS": "",
                "IPTV_FCC_PORT": "0",
                "IPTV_FCC_PROTOCOL": "zte",
            })
            run(legacy, True)
            assert playlist_content(destination) == expected_legacy()

            def preserves_output(settings, config_failure=False):
                saved = b"#EXTM3U\n# existing destination must survive a failed sync\n"
                destination.write_bytes(saved)
                request_count = len(requests)
                run(settings, False)
                assert destination.read_bytes() == saved
                if config_failure:
                    assert len(requests) == request_count, "invalid config reached the network"

            invalid_config = [
                ("IPTV_FCC_GATEWAY", None),
                ("IPTV_FCC_GATEWAY", ""),
                ("IPTV_FCC_GATEWAY", "http://192.0.2.10:5140"),
                ("IPTV_FCC_GATEWAY", "192.0.2.10:65536"),
                ("IPTV_FCC_ADDRESS", None),
                ("IPTV_FCC_ADDRESS", ""),
                ("IPTV_FCC_ADDRESS", "198.51.100.30?fcc=other"),
                ("IPTV_FCC_PORT", None),
                ("IPTV_FCC_PORT", ""),
                ("IPTV_FCC_PORT", "0"),
                ("IPTV_FCC_PORT", "65536"),
                ("IPTV_FCC_PORT", "8027junk"),
                ("IPTV_FCC_PROTOCOL", "zte"),
            ]
            for key, value in invalid_config:
                settings = environment.copy()
                if value is None:
                    del settings[key]
                else:
                    settings[key] = value
                preserves_output(settings, config_failure=True)

            for route in malformed:
                settings = environment.copy()
                settings["IPTV_SRC_MULTICAST"] = upstream + route
                preserves_output(settings)

            print("PASS: FCC conversion/override/query preservation, shared filtering, legacy multicast, invalid config and malformed-source atomic preservation")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
