#!/usr/bin/env python3
"""Offline regression smoke for a real built iptv-merge executable."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


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
SPORTS_PAIRS = {
    "guide": ('#EXTINF:-1 tvg-id="sports" tvg-name="咪咕体育" group-title="Guide",Github「使用说明」\n',
              "https://sports.example/hosts.txt?format=m3u#guide"),
    "first": ('#EXTINF:-1 tvg-id="sports" tvg-name="咪咕体育" tvg-logo="https://logo.example/sports,preview.png" group-title="Sport",News\n',
              "https://sports.example//index.m3u8?token=a%26b&fcc=provider%3A9000&fcc-type=source#main"),
    "blacklisted": ('#EXTINF:-1 tvg-id="sports" tvg-name="咪咕体育" group-title="Sport",睛彩竞技\n'
                    '#EXTVLCOPT:http-referrer=https://sports.example/excluded-only/\n',
                    "https://sports.example/excluded.m3u8?token=original"),
    "last": ('#EXTINF:-1 tvg-id="sports" tvg-name="咪咕体育" group-title="Sport",News标清\n',
             "http://sports.example:4022/udp/239.30.0.2:1234?token=second&fcc=original#track"),
}
BBC_PAIRS = {
    "guide": ('#EXTINF:-1 tvg-id="bbc" tvg-name="BBC" group-title="Guide",Github「使用说明」\n',
              "https://bbc.example/hosts.txt?format=m3u#guide"),
    "first": ('#EXTINF:-1 tvg-id="bbc" tvg-name="BBC" tvg-logo="https://logo.example/bbc,preview.png" group-title="Sport",News\n',
              "https://bbc.example//index.m3u8?token=a%26b&fcc=provider%3A9000&fcc-type=source#main"),
    "blacklisted": ('#EXTINF:-1 tvg-id="bbc" tvg-name="BBC" group-title="Sport",睛彩竞技\n'
                    '#EXTHTTP:{"Referer":"https://bbc.example/excluded-only/"}\n',
                    "https://bbc.example/excluded.m3u8?token=original"),
    "last": ('#EXTINF:-1 tvg-id="bbc" tvg-name="BBC" group-title="Sport",News标清\n',
             "http://bbc.example:4022/udp/239.30.0.2:1234?token=second&fcc=original#track"),
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


def expected_supplemented(strategy, mobile_filters, fcc=True):
    # Explicit fixture outcomes; do not duplicate the program's normalization/filter algorithms.
    accepted = {
        "smart": ["first", "blacklisted", "group-dropped", "cctv-4k", "last"],
        "exact": ["first", "cctv1", "blacklisted", "group-dropped", "cctv5-plus", "cctv-4k", "last"],
        "off": list(MOBILE_PAIRS),
    }[strategy]
    if mobile_filters:
        accepted = [key for key in accepted if key not in ("blacklisted", "group-dropped")]
    return (expected_primary_fcc() if fcc else playlist(PRIMARY_PAIRS)) + playlist(
        [MOBILE_PAIRS[key] for key in accepted], header="",
    )


def expected_sports(sports_filters):
    accepted = ["first", "last"] if sports_filters else list(SPORTS_PAIRS)
    return playlist([SPORTS_PAIRS[key] for key in accepted], header="")


def expected_bbc(bbc_filters):
    accepted = ["first", "last"] if bbc_filters else list(BBC_PAIRS)
    return playlist([BBC_PAIRS[key] for key in accepted], header="")


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


def reference_entry(path, display_name):
    """Read the standard per-entry HTTP options as a playlist consumer would."""
    selected = False
    headers = {}
    directives = []
    for line in path.read_text().splitlines():
        if line.startswith("#EXTINF:"):
            selected = line.rsplit(",", 1)[-1] == display_name
            headers = {}
            directives = []
        elif selected and line.startswith("#"):
            directives.append(line)
            if line.startswith("#EXTVLCOPT:"):
                key, separator, value = line[len("#EXTVLCOPT:"):].partition("=")
                if separator and key in ("http-referrer", "http-user-agent"):
                    headers[{"http-referrer": "Referer", "http-user-agent": "User-Agent"}[key]] = value
            elif line.startswith("#EXTHTTP:"):
                headers.update(json.loads(line[len("#EXTHTTP:"):]))
        elif selected and line:
            return line, headers, directives
    raise AssertionError(f"published playlist has no complete {display_name} entry")


def consume_video(ffmpeg, url, headers):
    """A real demux/decode, not a successful fetch of an arbitrary response body."""
    options = []
    if headers:
        options = ["-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items())]
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", *options,
         "-i", url, "-map", "0:v:0", "-frames:v", "1", "-f", "framemd5", "-"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "http_proxy": "", "no_proxy": "127.0.0.1,localhost"},
    )
    if result.returncode != 0 or not any(
        line.startswith("0,") for line in result.stdout.splitlines()
    ):
        raise AssertionError(f"published entry did not decode video:\n{result.stderr}\n{result.stdout}")


def check_module_unit_names(repo_root):
    """Regression for the modules/iptv-merge.nix suffix convention, e.g. a combined
    mode+sports+bbc output must be named iptv-fcc-sports-bbc-sync. Evaluates the NixOS
    module in place (no build); skipped when nix or the module is unavailable."""
    module = repo_root / "modules" / "iptv-merge.nix"
    nix = shutil.which("nix")
    if nix is None or not module.is_file():
        print("SKIP: module unit naming check (nix or modules/iptv-merge.nix unavailable)")
        return
    expr = f'''
let
  flake = builtins.getFlake "path:{repo_root}";
  pkgs = flake.inputs.nixpkgs.legacyPackages.x86_64-linux;
  inherit (pkgs) lib;
  stub = {{ lib, ... }}: {{
    options.systemd.services = lib.mkOption {{ type = lib.types.attrsOf lib.types.anything; default = {{}}; }};
    options.systemd.timers = lib.mkOption {{ type = lib.types.attrsOf lib.types.anything; default = {{}}; }};
    options.systemd.tmpfiles.rules = lib.mkOption {{ type = lib.types.listOf lib.types.str; default = []; }};
    options.assertions = lib.mkOption {{ type = lib.types.listOf lib.types.anything; default = []; }};
  }};
  evalOutputs = outputs: lib.evalModules {{
    specialArgs = {{ inherit pkgs; }};
    modules = [
      stub
      (import "${{flake}}/modules/iptv-merge.nix")
      ({{ ... }}: {{
        services.iptv-merge = {{
          enable = true;
          service = {{ user = "sync"; group = "sync"; }};
          inherit outputs;
        }};
      }})
    ];
  }};
  fcc = {{ mode = "fcc"; fcc = {{ gateway = "192.0.2.10:5140"; address = "198.51.100.30"; port = 8027; }}; }};
  mc = udpxy: {{ mode = "multicast"; inherit udpxy; }};
  combined = evalOutputs [ (fcc // {{ sports = true; bbc = true; outputPath = "/var/lib/iptv/combined.m3u"; }}) ];
  named = evalOutputs [
    (fcc // {{ name = "lp"; sports = true; bbc = true; outputPath = "/var/lib/iptv/lp-fcc.m3u"; }})
    ((mc "proxy.example:4022") // {{ name = "lp"; sports = true; outputPath = "/var/lib/iptv/lp-mc.m3u"; }})
    ((mc "proxy.example:4022") // {{ name = "lz"; bbc = true; outputPath = "/var/lib/iptv/lz-mc.m3u"; }})
    ((mc "proxy.example:4022") // {{ sports = true; bbc = true; outputPath = "/var/lib/iptv/mc-full.m3u"; }})
    ((mc "proxy.example:4022") // {{ outputPath = "/var/lib/iptv/mc-plain.m3u"; }})
  ];
in {{
  combined = builtins.attrNames combined.config.systemd.services;
  named = builtins.attrNames named.config.systemd.services;
  failed = map (a: a.message) (builtins.filter (a: !a.assertion) (combined.config.assertions ++ named.config.assertions));
}}
'''
    result = subprocess.run(
        [nix, "eval", "--impure", "--json", "--expr", expr],
        text=True, capture_output=True, timeout=600,
    )
    if result.returncode != 0:
        raise AssertionError(f"module unit naming eval failed:\n{result.stderr}")
    data = json.loads(result.stdout)
    if data["combined"] != ["iptv-fcc-sports-bbc-sync"]:
        raise AssertionError(f"combined mode+sports+bbc unit name: {data['combined']}")
    want_named = {
        "iptv-fcc-lp-sports-bbc-sync", "iptv-multicast-lp-sports-sync",
        "iptv-multicast-lz-bbc-sync", "iptv-multicast-sports-bbc-sync",
        "iptv-multicast-sync",
    }
    if set(data["named"]) != want_named:
        raise AssertionError(f"named output unit names: {sorted(data['named'])}, want {sorted(want_named)}")
    if data["failed"]:
        raise AssertionError(f"module assertions failed for sports/bbc combinations: {data['failed']}")
    print("PASS: module unit naming (mode+sports+bbc suffix, named outputs, assertions)")


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
    parser.add_argument(
        "--sports-filters", action="store_true",
        help='expect sportsBlacklist=["睛彩", "说明"] (neutral sports defaults otherwise)',
    )
    parser.add_argument(
        "--bbc-filters", action="store_true",
        help='expect bbcBlacklist=["睛彩", "说明"] (neutral bbc defaults otherwise)',
    )
    parser.add_argument(
        "--ffmpeg", type=Path, default=shutil.which("ffmpeg"),
        help="ffmpeg used to generate and decode local protected MPEG-TS (default: PATH)",
    )
    args = parser.parse_args()
    executable = args.executable.resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise SystemExit(f"not an executable file: {executable}")
    if args.ffmpeg is None or not args.ffmpeg.is_file():
        raise SystemExit("ffmpeg is required; supply --ffmpeg /path/to/ffmpeg")
    ffmpeg = args.ffmpeg.resolve()
    media = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=blue:s=32x32:r=25", "-t", "1", "-c:v", "mpeg2video",
         "-f", "mpegts", "pipe:1"],
        capture_output=True, check=True, timeout=30,
    ).stdout
    assert media, "ffmpeg produced an empty media fixture"
    required_headers = {
        "Referer": "https://stmify.com/",
        "User-Agent": "IPTV-merge-reference-consumer/1.0",
    }
    media_requests = []
    local_http = build_opener(ProxyHandler({}))

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
        "/sports.m3u": playlist(
            SPORTS_PAIRS.values(),
            header='#EXTM3U x-tvg-url="https://sports.example/guide.xml"\n\n# upstream sports comment\n',
        ) + "\n",
        "/sports-bad-header.m3u": playlist(SPORTS_PAIRS.values(), header="not an M3U\n"),
        "/sports-empty.m3u": HEADER,
        "/sports-unpaired.m3u": playlist([SPORTS_PAIRS["first"]]) + SPORT,
        "/sports-invalid-name.m3u": HEADER + '#EXTINF:-1 tvg-name="咪咕体育"\nhttps://sports.example/no-name.m3u8\n',
        "/sports-no-survivors.m3u": playlist([SPORTS_PAIRS["guide"], SPORTS_PAIRS["blacklisted"]]),
        "/bbc.m3u": playlist(
            BBC_PAIRS.values(),
            header='#EXTM3U x-tvg-url="https://bbc.example/guide.xml"\n\n# upstream bbc comment\n',
        ) + "\n",
        "/bbc-bad-header.m3u": playlist(BBC_PAIRS.values(), header="not an M3U\n"),
        "/bbc-empty.m3u": HEADER,
        "/bbc-unpaired.m3u": playlist([BBC_PAIRS["first"]]) + SPORT,
        "/bbc-invalid-name.m3u": HEADER + '#EXTINF:-1 tvg-name="BBC"\nhttps://bbc.example/no-name.m3u8\n',
        "/bbc-no-survivors.m3u": playlist([BBC_PAIRS["guide"], BBC_PAIRS["blacklisted"]]),
        **malformed,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = urlsplit(self.path)
            requests.append(request.path)
            if request.path in ("/protected/f1.ts", "/protected/nfl.ts"):
                allowed = all(self.headers.get(key) == value for key, value in required_headers.items())
                media_requests.append((request.path, allowed))
                if not allowed:
                    self.send_error(403, "Access Denied: Unauthorized domain.")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(media)))
                self.end_headers()
                self.wfile.write(media)
                return
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
            f1_directives = [
                "#EXTVLCOPT:http-referrer=" + required_headers["Referer"],
                "#EXTVLCOPT:http-user-agent=" + required_headers["User-Agent"],
                "#EXTVLCOPT:network-caching=1000",
                "#KODIPROP:inputstream=inputstream.ffmpegdirect",
                "# channel comment",
            ]
            nfl_directives = ["#EXTHTTP:" + json.dumps(required_headers)]
            fixtures["/protected-channels.m3u"] = (
                '#EXTM3U x-tvg-url="https://guide.example/uk.xml"\n'
                '#EXTVLCOPT:http-referrer=https://wrong-global.example/\n'
                '#EXTHTTP:{"User-Agent":"wrong-global-agent"}\n'
                '#EXTINF:-1 tvg-id="SkySportsF1.uk" group-title="UK",Sky Sports F1\n'
                + "\n".join(f1_directives) + "\n"
                + upstream + "/protected/f1.ts?token=a%26b&quality=high\n"
                '#EXTINF:-1 tvg-id="SkySportsNFL.uk" group-title="UK",Sky Sports NFL\n'
                + "\n".join(nfl_directives) + "\n"
                + upstream + "/protected/nfl.ts?token=c%26d&quality=high\n"
            )
            fixtures["/options-unpaired.m3u"] = (
                HEADER + NEWS + "\n".join(f1_directives) + "\n"
            )
            fixtures["/options-nested.m3u"] = (
                HEADER + NEWS + "\n".join(f1_directives) + "\n"
                + SPORT + upstream + "/protected/f1.ts\n"
            )
            fixtures["/options-extra-header.m3u"] = (
                HEADER + NEWS + "#EXTM3U\n" + upstream + "/protected/f1.ts\n"
            )
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
                saved = b'#EXTM3U\n#EXTINF:-1 tvg-id="old",Existing channel\nhttps://old.example/complete.m3u8?token=saved\n'
                destination.write_bytes(saved)
                request_count = len(requests)
                run(settings, False)
                assert destination.read_bytes() == saved
                if config_failure:
                    assert len(requests) == request_count, "invalid config reached the network"

            mobile_keys = [
                key for key in MOBILE_PAIRS
                if not args.mobile_filters or key not in ("blacklisted", "group-dropped")
            ]
            mobile_only = playlist(
                [MOBILE_PAIRS[key] for key in mobile_keys],
                header='#EXTM3U x-tvg-url="https://mobile.example/guide.xml"\n',
            )
            sports_outputs = []
            # Compare complete consumer playlists in every base mode. The sports pairs share
            # tvg-id/name and overlap the base display name; neither smart merge nor sibling
            # suppression may collapse them, and FCC must never touch their original URLs.
            for mode, include_mobile, expected_base in (
                ("telecom-only", False, playlist(PRIMARY_PAIRS)),
                ("mobile-only", False, mobile_only),
                ("both", False, expected_supplemented(args.strategy, args.mobile_filters, fcc=False)),
                ("multicast", False, expected_legacy()),
                ("fcc", False, expected_fcc("huawei")),
                ("fcc", True, expected_supplemented(args.strategy, args.mobile_filters)),
            ):
                settings = environment.copy()
                settings.update({
                    "IPTV_MODE": mode,
                    "IPTV_SRC": upstream + "/primary.m3u",
                    "IPTV_SRC_MOBILE": upstream + "/mobile.m3u",
                    "IPTV_SRC_MULTICAST": upstream + ("/primary.m3u" if include_mobile else "/valid.m3u?feed=fixture"),
                    "IPTV_SRC_SPORTS": upstream + "/sports.m3u?auth=source%26token",
                    "IPTV_FCC_INCLUDE_MOBILE": "1" if include_mobile else "0",
                    "IPTV_SPORTS": "0",
                })
                request_count = len(requests)
                run(settings, True)
                assert playlist_content(destination) == expected_base
                assert not any(
                    path.startswith("/sports") for path in requests[request_count:]
                ), f"disabled sports reached the network in {mode}"

                settings["IPTV_SPORTS"] = "1"
                run(settings, True)
                assert playlist_content(destination) == expected_base + expected_sports(args.sports_filters)
                assert sum(
                    line.startswith("#EXTM3U") for line in destination.read_text().splitlines()
                ) == 1
                sports_outputs.append(settings)

                broken = settings.copy()
                broken["IPTV_SRC_SPORTS"] = upstream + "/sports-bad-header.m3u"
                preserves_output(broken)

            # Exercise all sports failure gates against the combined FCC + mobile output.
            # No intermediate primary or primary+mobile list may replace the old destination.
            sports_failures = [
                "/sports-missing.m3u", "/sports-empty.m3u",
                "/sports-unpaired.m3u", "/sports-invalid-name.m3u",
            ]
            if args.sports_filters:
                sports_failures.append("/sports-no-survivors.m3u")
            for route in sports_failures:
                settings = sports_outputs[-1].copy()
                settings["IPTV_SRC_SPORTS"] = upstream + route
                preserves_output(settings)

            sports_fallback = sports_outputs[-1].copy()
            sports_fallback["IPTV_SRC_MOBILE"] = upstream + "/mobile-bad-header.m3u"
            run(sports_fallback, True)
            assert playlist_content(destination) == expected_primary_fcc() + expected_sports(args.sports_filters)
            sports_fallback["IPTV_SRC_SPORTS"] = upstream + "/sports-bad-header.m3u"
            preserves_output(sports_fallback)

            # BBC append mirrors sports but is always last: the consumer order must be
            # base group, sports group, bbc group. BBC pairs share tvg-id/name and overlap
            # the base display names like sports do; neither dedup nor FCC may touch them.
            bbc_outputs = []
            for mode, include_mobile, expected_base in (
                ("telecom-only", False, playlist(PRIMARY_PAIRS)),
                ("mobile-only", False, mobile_only),
                ("both", False, expected_supplemented(args.strategy, args.mobile_filters, fcc=False)),
                ("multicast", False, expected_legacy()),
                ("fcc", False, expected_fcc("huawei")),
                ("fcc", True, expected_supplemented(args.strategy, args.mobile_filters)),
            ):
                settings = environment.copy()
                settings.update({
                    "IPTV_MODE": mode,
                    "IPTV_SRC": upstream + "/primary.m3u",
                    "IPTV_SRC_MOBILE": upstream + "/mobile.m3u",
                    "IPTV_SRC_MULTICAST": upstream + ("/primary.m3u" if include_mobile else "/valid.m3u?feed=fixture"),
                    "IPTV_SRC_SPORTS": upstream + "/sports.m3u?auth=source%26token",
                    "IPTV_SRC_BBC": upstream + "/bbc.m3u?auth=source%26token",
                    "IPTV_FCC_INCLUDE_MOBILE": "1" if include_mobile else "0",
                    "IPTV_SPORTS": "1",
                    "IPTV_BBC": "0",
                })
                request_count = len(requests)
                run(settings, True)
                assert playlist_content(destination) == expected_base + expected_sports(args.sports_filters)
                assert not any(
                    path.startswith("/bbc") for path in requests[request_count:]
                ), f"disabled bbc reached the network in {mode}"

                settings["IPTV_BBC"] = "1"
                run(settings, True)
                assert playlist_content(destination) == (
                    expected_base + expected_sports(args.sports_filters) + expected_bbc(args.bbc_filters)
                )
                assert sum(
                    line.startswith("#EXTM3U") for line in destination.read_text().splitlines()
                ) == 1
                bbc_outputs.append(settings)

                broken = settings.copy()
                broken["IPTV_SRC_BBC"] = upstream + "/bbc-bad-header.m3u"
                preserves_output(broken)

            # Exercise all bbc failure gates against the combined FCC + mobile output.
            # No intermediate primary, primary+mobile or sports list may replace the old
            # destination: bbc aborts exactly like the sports stage.
            bbc_failures = [
                "/bbc-missing.m3u", "/bbc-empty.m3u",
                "/bbc-unpaired.m3u", "/bbc-invalid-name.m3u",
            ]
            if args.bbc_filters:
                bbc_failures.append("/bbc-no-survivors.m3u")
            for route in bbc_failures:
                settings = bbc_outputs[-1].copy()
                settings["IPTV_SRC_BBC"] = upstream + route
                preserves_output(settings)

            bbc_fallback = bbc_outputs[-1].copy()
            bbc_fallback["IPTV_SRC_MOBILE"] = upstream + "/mobile-bad-header.m3u"
            run(bbc_fallback, True)
            assert playlist_content(destination) == (
                expected_primary_fcc() + expected_sports(args.sports_filters) + expected_bbc(args.bbc_filters)
            )
            bbc_fallback["IPTV_SRC_BBC"] = upstream + "/bbc-bad-header.m3u"
            preserves_output(bbc_fallback)

            # Playback regression at the published consumer boundary: removing HTTP options
            # yields 403, while the complete entry must actually demux/decode local MPEG-TS.
            # Exercise both supplementary sanitizers in every mode, including FCC+mobile.
            for source_key in ("IPTV_SRC_SPORTS", "IPTV_SRC_BBC"):
                for base in bbc_outputs:
                    settings = base.copy()
                    settings[source_key] = upstream + "/protected-channels.m3u"
                    run(settings, True)
                    assert destination.read_text().count("#EXTM3U") == 1
                    for name, route, expected_directives in (
                        ("Sky Sports F1", "/protected/f1.ts?token=a%26b&quality=high", f1_directives),
                        ("Sky Sports NFL", "/protected/nfl.ts?token=c%26d&quality=high", nfl_directives),
                    ):
                        url, headers, directives = reference_entry(destination, name)
                        assert url == upstream + route
                        assert headers == required_headers
                        assert directives == expected_directives
                        try:
                            with local_http.open(url, timeout=5):
                                raise AssertionError("protected media accepted missing playlist options")
                        except HTTPError as error:
                            assert error.code == 403
                        before = len(media_requests)
                        consume_video(ffmpeg, url, headers)
                        assert any(allowed for _, allowed in media_requests[before:])
                    for route in (
                        "/options-unpaired.m3u", "/options-nested.m3u", "/options-extra-header.m3u",
                    ):
                        broken = settings.copy()
                        broken[source_key] = upstream + route
                        preserves_output(broken)

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
                ("IPTV_SPORTS", ""),
                ("IPTV_SPORTS", "true"),
                ("IPTV_SPORTS", "2"),
                ("IPTV_BBC", ""),
                ("IPTV_BBC", "true"),
                ("IPTV_BBC", "2"),
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

            check_module_unit_names(Path(__file__).resolve().parents[2])

            print(
                f"PASS: FCC conversion/query preservation, {args.strategy} mobile supplementation "
                f"(configured filters={args.mobile_filters}), unchanged mobile URLs/source order, "
                "mobile failure fallback, additive sports in all five modes including FCC+mobile "
                f"(configured sports filters={args.sports_filters}), disabled sports without fetching, "
                "sports failure atomic preservation, additive bbc after sports in all five modes "
                f"(configured bbc filters={args.bbc_filters}), disabled bbc without fetching, "
                "bbc failure atomic preservation, protected per-entry HTTP options with real media decode, "
                "legacy multicast, invalid config and malformed sources"
            )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
