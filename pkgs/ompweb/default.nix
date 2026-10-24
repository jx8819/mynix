# ompweb — OMP 编码代理 Web UI（上游 release tarball 打包）。
# 上游 release 不带 node_modules，生产依赖图由对应 release 精确上游 commit 的
# 原版 package-lock.json 钉住；node_modules 用 importNpmLock 离线重建。
{ lib, stdenv, nodejs, makeWrapper, fetchurl, importNpmLock }:

let
  version = "0.5.2";
  lock = builtins.fromJSON (builtins.readFile ./package-lock.json);
  # 临时 manifest 只用于装依赖；Nix derivation 名不能含 @ 和 /，故用无 scope 名字。
  package = {
    name = "ompweb";
    inherit version;
    inherit (lock.packages."") dependencies;
  };
  # 只保留运行时依赖，剔除 dev 依赖（构建/测试工具）。
  productionLock = lock // {
    name = package.name;
    packages = (lib.filterAttrs (_: entry: !(entry.dev or false)) lock.packages) // {
      "" = (builtins.removeAttrs lock.packages."" [ "devDependencies" ]) // {
        inherit (package) name;
      };
    };
  };
  nodeModules = importNpmLock.buildNodeModules {
    inherit nodejs package;
    packageLock = productionLock;
    derivationArgs = {
      pname = "ompweb-node-modules";
      inherit version;
      npmInstallFlags = "--omit=dev --offline";
      # release 包已自带原生 Linux 二进制（如 SWC），跳过 npm rebuild。
      npmRebuildFlags = "--ignore-scripts";
    };
  };
in
stdenv.mkDerivation {
  pname = "ompweb";
  inherit version;

  src = fetchurl {
    url = "https://registry.npmjs.org/@kahme247/ompweb/-/ompweb-${version}.tgz";
    hash = "sha256-YiP8JshiXqxCYDpqXc6i1W6/nOGK0EBfkd8u/S9Hups=";
  };

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/lib/node_modules/@kahme247" "$out/bin"
    cp -r . "$out/lib/node_modules/@kahme247/ompweb"
    ln -s ${nodeModules}/node_modules "$out/lib/node_modules/@kahme247/ompweb/node_modules"

    makeWrapper ${nodejs}/bin/node "$out/bin/ompweb" \
      --prefix PATH : ${nodejs}/bin \
      --chdir "$out/lib/node_modules/@kahme247/ompweb" \
      --add-flags "node_modules/next/dist/bin/next start"

    runHook postInstall
  '';

  meta = with lib; {
    description = "Web UI for the OMP coding agent";
    license = licenses.mit;
    platforms = platforms.linux;
    mainProgram = "ompweb";
  };
}
