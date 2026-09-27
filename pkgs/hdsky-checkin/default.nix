# hdsky-checkin — HDSky（hdsky.me）自动签到
#
# 机制完全公开（站点三步签到 + ddddocr 验证码识别）；Cookie 等私密值只在调用方
# 的 options 里填，且经 sops 加密。参见 modules/hdsky-checkin.nix。
{ python3, lib }:

let
  ddddocr = python3.pkgs.callPackage ../ddddocr { };
in
python3.pkgs.buildPythonApplication {
  pname = "hdsky-checkin";
  version = "1.0.0";
  format = "other";

  src = ./.;

  propagatedBuildInputs = [
    python3.pkgs.requests
    ddddocr
  ];

  installPhase = ''
    install -Dm755 hdsky_checkin.py $out/bin/hdsky-checkin
  '';

  meta = {
    description = "HDSky (hdsky.me) automatic check-in with captcha OCR";
    mainProgram = "hdsky-checkin";
    license = lib.licenses.mit;
  };
}
