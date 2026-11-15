# github:juspay/skills 的 Agent Plugins 技能包：原样安装 skills/<name>/，供 omp 以软链挂载。
# 只封装技能文件，不内置站点/主机选择——装哪些技能由 myconfig 的服务文件决定。
{ lib
, stdenv
, fetchFromGitHub
}:

stdenv.mkDerivation (finalAttrs: {
  pname = "juspay-skills";
  version = "unstable-2026-11-11";

  src = fetchFromGitHub {
    owner = "juspay";
    repo = "skills";
    rev = "5875f7508919a9216f2de7656c2b7d731c24279c";
    hash = "sha256-eXm6n2AN1H6Pu5QkwGfJZcMvn+kXv4eTdxZ+mTjc1Zw=";
  };

  dontBuild = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/juspay-skills
    cp -r skills $out/share/juspay-skills/skills
    cp README.md LICENSE plugin.json $out/share/juspay-skills/
    runHook postInstall
  '';

  meta = {
    description = "Juspay 的 Agent Plugins 技能包（Nix、写作、工具链）";
    homepage = "https://github.com/juspay/skills";
    license = lib.licenses.mit;
    platforms = lib.platforms.all;
  };
})