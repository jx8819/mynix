# ddddocr — 带带弟弟 OCR（ddddocr-1.6.1）
#
# nixpkgs 未收录，这里自打包。上游只发 wheel（内含几个 ONNX 模型，包体 ~76MB），
# 依赖 numpy / onnxruntime / Pillow / opencv-python-headless，nixpkgs 都有。
# HDSky 签到只用它的 classification()（纯字符识别），不碰 detect/矩形那套。
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

  # 上游只发 wheel（`py3-none-any`，内含几个 ONNX 模型，~76MB）。fetchPypi 的
  # wheel 名字推导会给成 `py2.py3-none-any`，这里直接钉死真实 URL。
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
