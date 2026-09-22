{ lib, stdenv, nodejs, pnpm, pnpmConfigHook, fetchPnpmDeps, fetchurl, jq }:

# Yacd-meta 仪表盘（MetaCubeX/Yacd-meta），作为 mihomo 的 external-ui。
#
# ⚠️ 钉住 0.3.8，不要更新：新版有 bug（2026-09-21 所有者决定）。升级本包前请先确认。
#
# v0.3.8 没有官方预构建产物（release 无 asset、gh-pages 只保留最新构建），从源码构建：
#   - 构建系统为 pnpm（lockfileVersion 9.0，对齐 nixpkgs 26.05 的 pnpm 11）+ vite 4.0.4。
#   - vite.config 里 base:'./'、outDir:'public'，产物是相对路径、可直接在 mihomo 的 /ui/ 前缀下加载。
#   - 源码和依赖都走固定输出 derivation（fetchurl / fetchPnpmDeps），构建期完全离线，
#     默认 Linux 构建沙箱可直接构建，不再需要 sandbox=relaxed。
#   - fetchurl 的 sha256 与旧版 curl 校验值一致（nix-base32 形式，已核对）；
#     网关偶发改坏下载字节时哈希不匹配、构建直接失败，不会静默产出坏包。
#   - 跳过 prepare(husky) 脚本，避免无 .git 时 husky install 报错。
stdenv.mkDerivation (finalAttrs: {
  pname = "yacd-meta";
  version = "0.3.8"; # tag e041c1975376247a1d43b650e68289ec974a182c

  src = fetchurl {
    url = "https://github.com/MetaCubeX/Yacd-meta/archive/e041c1975376247a1d43b650e68289ec974a182c.tar.gz";
    # 已验证的源码 tarball sha256（hex 2286f9fe…f12a 的 nix-base32 形式）
    hash = "sha256:0apig8qn40rv6zqgyjh8prdcn4lll8b0s3fvhsld7ygafbzgk1i2";
  };

  nativeBuildInputs = [ nodejs jq pnpm pnpmConfigHook ];

  # pnpm 依赖经固定输出 fetcher 离线预取（hash 在 nix-media 上按本仓库锁定的
  # nixpkgs rev 4c78701 构建 FOD 求得；lockfile 或 fetcherVersion 变动时需重算）。
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
    # vite 的 outDir 是 public/（见 vite.config.ts：build.outDir='public'）
    cp -r public/. "$out/"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Yet Another Clash Dashboard (Yacd-meta) — mihomo external-ui";
    license = licenses.mit;
    platforms = with platforms; all;
  };
})
