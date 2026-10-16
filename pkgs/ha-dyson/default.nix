# ha-dyson — Dyson 设备的 HA 自定义组件（domain: dyson_local）。
# 依赖 libdyson-neon（nixpkgs 未收录，见 ./libdyson.nix）；另需 HA 内建 mqtt/zeroconf 集成，无额外 python 包。
{ lib
, home-assistant
, fetchFromGitHub
, buildHomeAssistantComponent
}:

buildHomeAssistantComponent rec {
  owner = "libdyson-wg";
  domain = "dyson_local";
  version = "1.7.0";

  src = fetchFromGitHub {
    owner = "libdyson-wg";
    repo = "ha-dyson";
    tag = "v${version}";
    hash = "sha256-C5UDK0st0IR3PRsbiG9M9ZfGpDrPYqBcPw/8/2iWJXw=";
  };

  # 必须用 home-assistant 的 python 包集，否则解释器对不上 buildHomeAssistantComponent。
  dependencies = [
    (home-assistant.python3Packages.callPackage ./libdyson.nix { })
  ];

  meta = {
    changelog = "https://github.com/libdyson-wg/ha-dyson/releases/tag/${src.tag}";
    description = "Home Assistant custom integration for Wi-Fi connected Dyson devices";
    homepage = "https://github.com/libdyson-wg/ha-dyson";
    license = lib.licenses.mit;
  };
}
