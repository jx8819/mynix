# ompweb — Web UI for the OMP coding agent（upstream: kahme247/ompweb）
#
# 上游 npm 包 @kahme247/ompweb 已包含编译好的 Next.js 输出（.next），
# 直接从 npm registry 取 tarball 解包，避免在仓库里 vendor 26M 产物。
#
# 运行期依赖：nodeModulesDir 下必须有该包完整的 node_modules（`npm install`
# 装出 @kahme247/ompweb 的依赖树）。Next 的依赖没有随包发布，包装器从那里启动。
{ lib, stdenv, bash, nodejs, fetchurl, nodeModulesDir ? "/opt/ompweb/lib/node_modules" }:

let
  version = "0.5.0";
in
stdenv.mkDerivation {
  pname = "ompweb";
  inherit version;

  src = fetchurl {
    url = "https://registry.npmjs.org/@kahme247/ompweb/-/ompweb-${version}.tgz";
    hash = "sha256-eP/35LelvG0LitCQTtLQvaQEojoriFM7H/viilcl15M=";
  };

  preferLocalBuild = true;
  allowSubstitutes = false;

  installPhase = ''
    mkdir -p $out/lib/node_modules/ompweb $out/bin
    tar -xzf $src -C $out/lib/node_modules/ompweb --strip-components=1
    ln -s ${nodeModulesDir}/@kahme247/ompweb/node_modules $out/lib/node_modules/ompweb/node_modules

    cat > $out/bin/ompweb << WRAPPER
    #!${bash}/bin/bash
    export PATH="${nodejs}/bin:\$PATH"
    cd ${nodeModulesDir}/@kahme247/ompweb
    exec ${nodejs}/bin/node node_modules/.bin/next start "\$@"
    WRAPPER
    chmod +x $out/bin/ompweb
  '';

  meta = with lib; {
    description = "Web UI for the OMP coding agent";
    license = licenses.mit;
    platforms = platforms.linux;
    mainProgram = "ompweb";
  };
}
