# ha-cololight — LifeSmart Cololight Home Assistant 自定义集成
#
# 上游：BazaJayGee66/homeassistant_cololight（HACS 集成，v2.0.9）。
# manifest.json 唯一 Python 依赖 pycololight==2.1.0（nixpkgs 未收录，见
# ./pycololight.nix，用 home-assistant 的 python 构建，与集成本体保持同一解释器）。
{ lib
, buildHomeAssistantComponent
, fetchFromGitHub
, home-assistant
}:

let
  pycololight = home-assistant.python3Packages.callPackage ./pycololight.nix { };
in
buildHomeAssistantComponent rec {
  owner = "BazaJayGee66";
  domain = "cololight";
  version = "2.0.9";

  src = fetchFromGitHub {
    inherit owner;
    repo = "homeassistant_cololight";
    tag = "v${version}";
    hash = "sha256-otdqlX+J6GRfoJVW/irKh0A+ho6GFGLcCVA8x715Lrw=";
  };

  dependencies = [ pycololight ];

  # 上游把测试套件放在 custom_components/tests/（构建产物里用不到，且依赖
  # pytest-homeassistant-custom-component），不随组件分发。
  postInstall = ''
    rm -rf $out/custom_components/tests
  '';

  meta = {
    changelog = "https://github.com/BazaJayGee66/homeassistant_cololight/blob/${src.tag}/CHANGELOG.md";
    description = "Home Assistant custom integration for LifeSmart Cololight lights";
    homepage = "https://github.com/BazaJayGee66/homeassistant_cololight";
    license = lib.licenses.mit;
  };
}
