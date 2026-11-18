# ha-xiaomi-miot — al-one/hass-xiaomi-miot 自定义集成（domain: xiaomi_miot）。
# 固定上游 tag 打包（nixpkgs 仍停 1.1.4）；Python 依赖取自 HA 自己的包集，
# 保证解释器与集成本体一致。
{ lib
, home-assistant
, fetchFromGitHub
, buildHomeAssistantComponent
}:

buildHomeAssistantComponent rec {
  owner = "al-one";
  domain = "xiaomi_miot";
  version = "1.1.6";

  src = fetchFromGitHub {
    inherit owner;
    repo = "hass-xiaomi-miot";
    tag = "v${version}";
    hash = "sha256-gwqKTE0aTNfxTnzKkcFynRQP+dHlaYNUbSQkF2BN9Cs=";
  };

  dependencies = with home-assistant.python3Packages; [
    construct
    micloud
    python-miio
  ];

  meta = {
    changelog = "https://github.com/al-one/hass-xiaomi-miot/releases/tag/${src.tag}";
    description = "Automatic integrate all Xiaomi devices to Home Assistant via miot-spec";
    homepage = "https://github.com/al-one/hass-xiaomi-miot";
    license = lib.licenses.asl20;
  };
}