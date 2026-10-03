# ha-xiaomi-home — Xiaomi 官方 Home Assistant 集成（domain: xiaomi_home）
#
# 上游：https://github.com/XiaoMi/ha_xiaomi_home
# 固定版本以避免 nixpkgs 更新延迟；manifest 的 Python 依赖全部取自
# Home Assistant 自己的 Python package set，保证解释器和依赖版本一致。
{ lib
, home-assistant
, fetchFromGitHub
, buildHomeAssistantComponent
}:

buildHomeAssistantComponent rec {
  owner = "XiaoMi";
  domain = "xiaomi_home";
  version = "0.5.0";

  src = fetchFromGitHub {
    inherit owner;
    repo = "ha_xiaomi_home";
    tag = "v${version}";
    hash = "sha256-m6Az3HN/MfhCrB+QZfDtoBW+lnmPYQsrlAGrF72q7fU=";
  };

  dependencies = with home-assistant.python3Packages; [
    construct
    paho-mqtt
    numpy
    cryptography
    psutil-home-assistant
  ];

  meta = {
    changelog = "https://github.com/XiaoMi/ha_xiaomi_home/releases/tag/${src.tag}";
    description = "Official Xiaomi Home integration for Home Assistant";
    homepage = "https://github.com/XiaoMi/ha_xiaomi_home";
    license = lib.licenses.unfree;
  };
}
