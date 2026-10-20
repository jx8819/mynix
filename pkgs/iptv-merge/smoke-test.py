#!/usr/bin/env python3
"""Offline regression smoke for a real built iptv-merge executable."""

import argparse
import os
from pathlib import Path
import subprocess
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
PRIMARY_PAIRS = [
    ('#EXTINF:-1 tvg-id="news-hd" group-title="News",News高清\n',
     "http://proxy.example:4022/udp/239.10.0.1:1234"),
    ('#EXTINF:-1 tvg-id="cctv1" group-title="News",CCTV1综合高清\n',
     "http://proxy.example:4022/rtp/239.10.0.2:1234"),
    ('#EXTINF:-1 tvg-id="cctv5-plus" group-title="Sport",CCTV5+体育赛事高清\n',
     "http://proxy.example:4022/udp/239.10.0.3:1234"),
]
MOBILE_PAIRS = {
    "overlap": ('#EXTINF:-1 group-title="News",NewsHD\n',
                "https://mobile.example/news.m3u8?token=news"),
    "first": ('#EXTINF:-1 group-title="Extra",Mobile FirstHD\n',
              "https://mobile.example/first.m3u8?token=a%26b&quality=high#video"),
    "cctv1": ('#EXTINF:-1 group-title="News",CCTV1\n',
              "http://mobile.example/cctv1/index.m3u8"),
    "first-4k": ('#EXTINF:-1 group-title="Extra",Mobile First4K\n',
                 "https://mobile.example/first-alt.m3u8?quality=4k"),
    "blacklisted": ('#EXTINF:-1 group-title="Extra",bLoCkEd Candidate\n',
                    "http://mobile.example/blocked.m3u8"),
    "group-dropped": ('#EXTINF:-1 group-title="Excluded",Group Candidate\n',
                      "https://mobile.example/excluded.m3u8"),
    "cctv5-plus": ('#EXTINF:-1 group-title="Sport",CCTV5+\n',
                   "https://mobile.example/cctv5-plus.m3u8"),
    "cctv-4k": ('#EXTINF:-1 group-title="News",CCTV4K\n',
                "https://mobile.example/cctv4k.m3u8"),
    "last": ('#EXTINF:-1 group-title="Extra",Mobile Last\n',
             "http://mobile.example:4022/udp/239.20.0.1:1234?token=original#track"),
}


def playlist(pairs, header=HEADER):
    return header + "".join(info + url + "\n" for info, url in pairs)


def expected_primary_fcc():
    return playlist([
        (PRIMARY_PAIRS[0][0],
         f"http://{FCC_GATEWAY}/rtp/239.10.0.1:1234?fcc={FCC_ADDRESS}:{FCC_PORT}&fcc-type=huawei"),
        (PRIMARY_PAIRS[1][0],
         f"http://{FCC_GATEWAY}/rtp/239.10.0.2:1234?fcc={FCC_ADDRESS}:{FCC_PORT}&fcc-type=huawei"),
        (PRIMARY_PAIRS[2][0],
         f"http://{FCC_GATEWAY}/rtp/239.10.0.3:1234?fcc={FCC_ADDRESS}:{FCC_PORT}&fcc-type=huawei"),
    ])


def expected_supplemented(strategy, mobile_filters):
    # Explicit fixture outcomes; do not duplicate the program's normalization/filter algorithms.
    accepted = {
        "smart": ["first", "blacklisted", "group-dropped", "cctv-4k", "last"],
        "exact": ["first", "cctv1", "blacklisted", "group-dropped", "cctv5-plus", "cctv-4k", "last"],
        "off": list(MOBILE_PAIRS),
    }[strategy]
    if mobile_filters:
        accepted = [key for key in accepted if key not in ("blacklisted", "group-dropped")]
    return expected_primary_fcc() + playlist(
        [MOBILE_PAIRS[key] for key in accepted], header="",
    )


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument(
        "--strategy", choices=("smart", "exact", "off"), default="smart",
        help="mergeStrategy compiled into the supplied executable (default: smart)",
    )
    parser.add_argument(
        "--mobile-filters", action="store_true",
        help='expect mobileBlacklist=["BLOCKED CANDIDATE"] and mobileDropGroups=["Excluded"]',
    )
    args = parser.parse_args()
    executable = args.executable.resolve()
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
    fixtures = {
        "/valid.m3u": source(LEGACY_GATEWAY),
        "/primary.m3u": playlist(PRIMARY_PAIRS),
        "/mobile.m3u": playlist(
            MOBILE_PAIRS.values(),
            header='#EXTM3U x-tvg-url="https://mobile.example/guide.xml"\n\n# upstream comment\n',
        ) + "\n",
        "/mobile-bad-header.m3u": playlist([MOBILE_PAIRS["first"]], header="not an M3U\n"),
        "/mobile-empty.m3u": HEADER,
        "/mobile-unpaired.m3u": playlist([MOBILE_PAIRS["first"]]) + SPORT,
        "/mobile-no-survivors.m3u": playlist([
            ('#EXTINF:-1 group-title="Extra",Only\n', "https://mobile.example/only.m3u8"),
            ('#EXTINF:-1 group-title="Extra",Only标清\n', "https://mobile.example/only-sd.m3u8"),
        ]),
        **malformed,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = urlsplit(self.path)
            requests.append(request.path)
            if request.path in fixtures:
                body = fixtures[request.path]
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

            supplemented = environment.copy()
            supplemented.update({
                "IPTV_SRC_MULTICAST": upstream + "/primary.m3u",
                "IPTV_SRC_MOBILE": upstream + "/mobile.m3u",
                "IPTV_FCC_INCLUDE_MOBILE": "1",
            })
            run(supplemented, True)
            assert playlist_content(destination) == expected_supplemented(
                args.strategy, args.mobile_filters,
            )

            primary_only = supplemented.copy()
            primary_only["IPTV_FCC_INCLUDE_MOBILE"] = "0"
            run(primary_only, True)
            assert playlist_content(destination) == expected_primary_fcc()

            # Mobile stage failures still publish the freshly converted primary, not the old
            # destination or a partial merge; all temporary files must be cleaned by run().
            for route in (
                "/mobile-bad-header.m3u", "/mobile-empty.m3u",
                "/mobile-unpaired.m3u", "/mobile-no-survivors.m3u",
            ):
                destination.write_text("#EXTM3U\n# old destination\n")
                settings = supplemented.copy()
                settings["IPTV_SRC_MOBILE"] = upstream + route
                result = run(settings, True)
                assert playlist_content(destination) == expected_primary_fcc()
                assert result.stderr.strip(), "mobile failure had no diagnostic"
            legacy = environment.copy()
            legacy.update({
                "IPTV_MODE": "multicast",
                "IPTV_FCC_GATEWAY": "not a gateway",
                "IPTV_FCC_ADDRESS": "",
                "IPTV_FCC_PORT": "0",
                "IPTV_FCC_PROTOCOL": "zte",
                "IPTV_FCC_INCLUDE_MOBILE": "not a flag",
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
                ("IPTV_FCC_INCLUDE_MOBILE", ""),
                ("IPTV_FCC_INCLUDE_MOBILE", "true"),
                ("IPTV_FCC_INCLUDE_MOBILE", "2"),
            ]
            for key, value in invalid_config:
                settings = environment.copy()
                if value is None:
                    del settings[key]
                else:
                    settings[key] = value
                preserves_output(settings, config_failure=True)

            for route in malformed:
                for base in (environment, supplemented):
                    settings = base.copy()
                    settings["IPTV_SRC_MULTICAST"] = upstream + route
                    preserves_output(settings)

            print(
                f"PASS: FCC conversion/query preservation, {args.strategy} mobile supplementation "
                f"(configured filters={args.mobile_filters}), unchanged mobile URLs/source order, "
                "mobile failure fallback, legacy multicast, invalid config and malformed-source atomic preservation"
            )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
