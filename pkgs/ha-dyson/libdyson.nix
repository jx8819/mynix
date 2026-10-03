# libdyson-neon — Dyson 设备 Python 库（PyPI: libdyson-neon，GitHub: libdyson-wg/libdyson-neon）
#
# nixpkgs 未收录，这里自打包。ha-dyson（dyson_local 组件）manifest.json 钉死
# `libdyson-neon==1.6.0`。依赖 paho-mqtt / cryptography / requests / zeroconf /
# attrs，nixpkgs 都有。
{ lib
, buildPythonPackage
, fetchFromGitHub
, paho-mqtt
, cryptography
, requests
, zeroconf
, attrs
}:

buildPythonPackage rec {
  pname = "libdyson-neon";
  version = "1.6.0";
  format = "setuptools";

  src = fetchFromGitHub {
    owner = "libdyson-wg";
    repo = "libdyson-neon";
    tag = "v${version}";
    hash = "sha256-pGDUglM3Rmd/Rn0ZlsSYiS1GyZMFBHtSY8EfLy6MLdc=";
  };

  propagatedBuildInputs = [
    paho-mqtt
    cryptography
    requests
    zeroconf
    attrs
  ];

  # 上游 tests/ 需要额外测试依赖，跳过；只验证可导入（包名 libdyson）。
  doCheck = false;
  pythonImportsCheck = [ "libdyson" ];

  meta = {
    description = "Python library for Dyson devices";
    homepage = "https://github.com/libdyson-wg/libdyson-neon";
    license = lib.licenses.asl20;
  };
}
