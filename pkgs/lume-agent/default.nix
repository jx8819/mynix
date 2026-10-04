# lume-agent — Lume VPS 探针监控客户端 (vpsmon-agent)
#
# 上游：MostlyCodex/lume-monitor (agent/)
# 纯 Go 编写，单二进制，出站 HTTPS 上报，走 Linux 非特权 datagram ping socket
{ lib
, buildGoModule
, fetchFromGitHub
}:

buildGoModule rec {
  pname = "lume-agent";
  version = "1.1.0-unstable-2026-10-04";

  src = fetchFromGitHub {
    owner = "MostlyCodex";
    repo = "lume-monitor";
    rev = "f72fbc997f0909df21bd275d00962416169de3e8";
    hash = "sha256-Jx20rUADKeXR03CSE6AJt3z5vCaCk+nPHU+RAOjwXz4=";
  };
  vendorHash = "sha256-n2cQis6PSx5K08FYn/6DU5710g4AWobEys91USlZYOw=";
  modRoot = "agent";
  subPackages = [ "cmd/vpsmon-agent" ];

  ldflags = [
    "-s"
    "-w"
  ];

  postInstall = ''
    mv $out/bin/vpsmon-agent $out/bin/lume-agent || true
  '';

  meta = {
    description = "Lightweight monitoring agent for Lume";
    homepage = "https://github.com/MostlyCodex/lume-monitor";
    license = lib.licenses.mit;
    mainProgram = "lume-agent";
  };
}
