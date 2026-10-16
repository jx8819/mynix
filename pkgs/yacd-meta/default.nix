{ lib, stdenv, nodejs, pnpm, pnpmConfigHook, fetchPnpmDeps, fetchurl, jq }:

# Yacd-meta 仪表盘（MetaCubeX/Yacd-meta），作 mihomo 的 external-ui。
# 钉住当前版本勿升级：新版有 bug，升级前先确认；该版本无官方预构建产物，从源码构建。
# 源码/依赖全走固定输出离线构建；vite 产物为相对路径，可直接挂 mihomo /ui/ 前缀。
stdenv.mkDerivation (finalAttrs: {
  pname = "yacd-meta";
  version = "0.3.8";

  src = fetchurl {
    url = "https://github.com/MetaCubeX/Yacd-meta/archive/e041c1975376247a1d43b650e68289ec974a182c.tar.gz";
    hash = "sha256:0apig8qn40rv6zqgyjh8prdcn4lll8b0s3fvhsld7ygafbzgk1i2";
  };

  nativeBuildInputs = [ nodejs jq pnpm pnpmConfigHook ];

  # pnpm 依赖固定输出离线预取；lockfile 或 fetcherVersion 变动时需重算 hash。
  pnpmDeps = fetchPnpmDeps {
    inherit (finalAttrs) pname version src;
    hash = "sha256-es12GhAv5oSPBUj/aLMHamRi+LNqiKgDirspIbJaaXw=";
    fetcherVersion = 3;
  };

  postPatch = ''
    # 去掉 husky 的 prepare 脚本：无 .git 的源码树里 husky install 会让 pnpm install 失败
    jq 'del(.scripts.prepare)' package.json > package.json.tmp
    mv package.json.tmp package.json
  '';

  buildPhase = ''
    runHook preBuild
    pnpm build
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    # vite 构建产物在 public/。
    cp -r public/. "$out/"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Yet Another Clash Dashboard (Yacd-meta) — mihomo external-ui";
    license = licenses.mit;
    platforms = with platforms; all;
  };
})
