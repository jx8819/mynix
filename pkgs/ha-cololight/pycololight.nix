# pycololight — LifeSmart Cololight Python 封装（pycololight-2.1.0）
#
# nixpkgs 未收录，这里自打包。ha-cololight（v2.0.9）的 manifest 钉死
# `pycololight==2.1.0`，buildHomeAssistantComponent 的 manifestCheckPhase 会用
# home-assistant 的 python 校验版本，所以本包必须用 home-assistant.python3Packages
# 构建（该 python 与 pkgs.python3 版本不同，见 default.nix）。
# 纯 Python，无运行时依赖；构建后端为 poetry-core（上游 pyproject.toml 声明）。
{ lib
, buildPythonPackage
, fetchPypi
, poetry-core
}:

buildPythonPackage rec {
  pname = "pycololight";
  version = "2.1.0";

  pyproject = true;

  src = fetchPypi {
    inherit pname version;
    hash = "sha256-vGOADPC0i8HyXQQ+4BPaz05khorHdNGZWAqLPketJPo=";
  };

  build-system = [ poetry-core ];

  # 上游无测试套件（sdist 不含 tests/）。
  doCheck = false;
  pythonImportsCheck = [ "pycololight" ];

  meta = {
    description = "Python3 wrapper for interacting with LifeSmart Cololight";
    homepage = "https://github.com/BazaJayGee66/pycololight";
    license = lib.licenses.mit;
  };
}
