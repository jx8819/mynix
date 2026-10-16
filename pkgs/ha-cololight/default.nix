# ha-cololight — LifeSmart Cololight 的 HA 自定义集成（domain: cololight）。
# manifest 唯一 Python 依赖 pycololight nixpkgs 未收录（见 ./pycololight.nix），
# 用 home-assistant 的 python 包集构建，保证解释器与集成本体一致。
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

  # 上游测试套件在 custom_components/tests/，产物用不到、且依赖 pytest 套件，不分发。
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
