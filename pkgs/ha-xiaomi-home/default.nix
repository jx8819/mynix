# ha-xiaomi-home — 小米官方 HA 集成（domain: xiaomi_home）。
# 固定版本打包（不跟 nixpkgs）；Python 依赖全取自 HA 自己的包集，保证解释器与依赖一致。
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
