# ddddocr — 带带弟弟 OCR（ONNX runtime 验证码识别），nixpkgs 未收录，自打包。
# hdsky-checkin 的依赖（只用 classification() 纯字符识别，不碰 detect/矩形），也可单独用。
{ lib
, buildPythonPackage
, fetchurl
, numpy
, onnxruntime
, pillow
, opencv-python-headless
}:

buildPythonPackage rec {
  pname = "ddddocr";
  version = "1.6.1";
  format = "wheel";

  # 上游只发 wheel（内含 ONNX 模型，包体大）；fetchPypi 的 wheel 名推导对不上，直接钉真实 URL。
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/0e/48/cbaed3981b8d8d51141b9b4779b811f4728e65d952a1e3e2e5e929539183/ddddocr-${version}-py3-none-any.whl";
    hash = "sha256-x8cPSuLQM1RArosnLupIyfaIjs70Z4X+IxHwyXoTOTU=";
  };

  propagatedBuildInputs = [
    numpy
    onnxruntime
    pillow
    opencv-python-headless
  ];

  # 上游无测试套件。
  doCheck = false;
  pythonImportsCheck = [ "ddddocr" ];

  meta = {
    description = "ddddocr captcha recognition (ONNX runtime based)";
    homepage = "https://github.com/sml2h3/ddddocr";
    license = lib.licenses.mit;
  };
}
