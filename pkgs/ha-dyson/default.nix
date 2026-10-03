# ha-dyson — Dyson 设备 Home Assistant 自定义组件（domain: dyson_local）
#
# 上游：https://github.com/libdyson-wg/ha-dyson（HACS 集成，替代旧 shenxn/ha-dyson）。
# manifest.json requirements: ["libdyson-neon==1.6.0"]，nixpkgs 未收录，见同目录
# libdyson.nix。组件额外依赖 HA 内建的 mqtt / zeroconf 集成，无需 python 包。
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

  # 必须用 home-assistant 的 python 包集（nixpkgs 对 custom-components 也这么接），
  # 否则 python 版本对不上 buildHomeAssistantComponent 的解释器。
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
