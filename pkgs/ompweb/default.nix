# ompweb publishes its compiled .next output, but not its node_modules. The
# upstream release lockfile pins the exact production dependency graph.
# package-lock.json is the unmodified upstream 0.5.1 lockfile at gitHead
# 38bad6af80e9e5989a2603b0cf5b7b22c82adca3.
{ lib, stdenv, bash, nodejs, fetchurl, importNpmLock }:

let
  version = "0.5.1";
  lock = builtins.fromJSON (builtins.readFile ./package-lock.json);
  # This temporary manifest only installs dependencies; the published package
  # retains its scoped name. Nix derivation names cannot contain @ or /.
  package = {
    name = "ompweb";
    inherit version;
    inherit (lock.packages."") dependencies;
  };
  # The published bundle needs runtime dependencies, not the build/test tools.
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
      # Release packages already carry their native Linux binaries (notably SWC).
      npmRebuildFlags = "--ignore-scripts";
    };
  };
in
stdenv.mkDerivation {
  pname = "ompweb";
  inherit version;

  src = fetchurl {
    url = "https://registry.npmjs.org/@kahme247/ompweb/-/ompweb-${version}.tgz";
    hash = "sha256-gJxLsmGpH9R1TSd6c8dShRFdP3jfVizUgJgvAHjP4nI=";
  };

  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/lib/node_modules/@kahme247" "$out/bin"
    cp -r . "$out/lib/node_modules/@kahme247/ompweb"
    ln -s ${nodeModules}/node_modules "$out/lib/node_modules/@kahme247/ompweb/node_modules"

    cat > "$out/bin/ompweb" << WRAPPER
    #!${bash}/bin/bash
    export PATH="${nodejs}/bin:\$PATH"
    cd "$out/lib/node_modules/@kahme247/ompweb"
    exec ${nodejs}/bin/node node_modules/next/dist/bin/next start "\$@"
    WRAPPER
    chmod +x "$out/bin/ompweb"

    runHook postInstall
  '';

  meta = with lib; {
    description = "Web UI for the OMP coding agent";
    license = licenses.mit;
    platforms = platforms.linux;
    mainProgram = "ompweb";
  };
}
