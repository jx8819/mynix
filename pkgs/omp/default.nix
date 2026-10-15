# omp — terminal-based coding agent (can1357/oh-my-pi), built from source.
#
# 配方沿用 nixpkgs pkgs/by-name/om/omp/package.nix（bun 打 JS bundle +
# cargo 构建 pi-natives N-API addon），源码 pin 到指定 rev，替代更新滞后的
# nixpkgs unstable omp。数据/凭据运行时在 $PI_CODING_AGENT_DIR / $HOME，不进 store。
{
  lib,
  stdenv,
  fetchFromGitHub,
  bun,
  cargo,
  rustc,
  cmake,
  ninja,
  pkg-config,
  rustPlatform,
  autoPatchelfHook,
  installShellFiles,
  makeWrapper,
  libopus,
  alsa-lib,
  libpulseaudio,
  pipewire,
  versionCheckHook,
  writableTmpDirAsHomeHook,
  pipewireSupport ? true,
}:
let
  addonName =
    let
      inherit (stdenv.hostPlatform) node isx86_64;
      arch = if isx86_64 then "x64-baseline" else node.arch;
    in
    "pi_natives.${node.platform}-${arch}.node";
in
stdenv.mkDerivation (finalAttrs: {
  pname = "omp";
  version = "18.11.0";

  strictDeps = true;
  __structuredAttrs = true;

  src = fetchFromGitHub {
    owner = "can1357";
    repo = "oh-my-pi";
    # 统一 pin：与 myconfig 现网 omp 对齐
    rev = "d0b56045fff4367a23d839bf8250d49c05486566";
    hash = "sha256-YNPG9fDkEa+wKLip2q0mV9enBJAQUEeZH/M7UVGCqQ4=";
  };

  cargoDeps = rustPlatform.fetchCargoVendor {
    inherit (finalAttrs) src;
    hash = "sha256-j/4POlwE819bZHvlO8UsQNGUPzG/RblP4R8nhSv0AjU=";
  };

  postPatch = ''
    substituteInPlace packages/coding-agent/src/cli.ts \
      --replace-fail 'if (Bun.semver.order(Bun.version, MIN_BUN_VERSION) < 0)' 'if (false)'
  '';

  # required until https://github.com/NixOS/nixpkgs/issues/255890 & https://github.com/NixOS/nixpkgs/issues/335534 are fixed
  node_modules = stdenv.mkDerivation {
    pname = "${finalAttrs.pname}-node_modules";
    inherit (finalAttrs) version src;

    nativeBuildInputs = [
      bun
      writableTmpDirAsHomeHook
    ];

    dontConfigure = true;

    buildPhase = ''
      runHook preBuild

      bun install \
        --cpu="*" \
        --frozen-lockfile \
        --ignore-scripts \
        --no-progress \
        --os="*"

      runHook postBuild
    '';

    installPhase = ''
      runHook preInstall

      mkdir -p $out
      find . -type d -name node_modules -exec cp -R --parents {} $out \;

      runHook postInstall
    '';

    # Prevents breaking symlinks in node_modules
    dontFixup = true;

    outputHash = "sha256-sgmnI13GsXa4RclWfFz/1Kpw2CqHbs+SidbYoEyxB/E=";
    outputHashMode = "recursive";
  };

  nativeBuildInputs = [
    bun
    cargo
    cmake
    installShellFiles
    makeWrapper
    ninja
    pkg-config
    rustc
    rustPlatform.bindgenHook
    rustPlatform.cargoSetupHook
    writableTmpDirAsHomeHook
    autoPatchelfHook
  ];

  buildInputs = [
    libopus
    stdenv.cc.cc.lib
  ]
  ++ lib.optionals pipewireSupport [ pipewire ];

  dontStrip = true;

  env = {
    RUSTC_BOOTSTRAP = "1";
    # rustc 1.95（nixpkgs 26.05）里 result_option_map_or_default 尚未稳定，
    # 上游源码已直接使用（其构建工具链更新）。用 crate-attr 给全部 crate 统一
    # 开闸，避免逐 crate 改源码；RUSTC_BOOTSTRAP 允许 stable 编译器启用 gate。
    RUSTFLAGS = "-Zcrate-attr=feature(result_option_map_or_default,isolate_most_least_significant_one)";
  };

  configurePhase = ''
    runHook preConfigure

    cp -R ${finalAttrs.node_modules}/* .

    runHook postConfigure
  '';

  buildPhase = ''
    runHook preBuild

    echo "Building pi-natives"
    cargo build --release -p pi-natives ${lib.optionalString pipewireSupport "--features wayland-pipewire"}
    install -Dm755 "target/release/libpi_natives${stdenv.hostPlatform.extensions.sharedLibrary}" \
      "packages/natives/native/${addonName}"

    autoPatchelf -- "packages/natives/native/${addonName}"
    patchelf --add-rpath "${
      lib.makeLibraryPath [
        libpulseaudio
        alsa-lib
      ]
    }" \
      "packages/natives/native/${addonName}"

    echo "Building JS bundle"
    bun --cwd="$PWD/packages/coding-agent" run prepack

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/omp
    cp -R packages python node_modules $out/lib/omp/
    chmod -R u+w $out/lib/omp

    # Prune foreign binaries to eliminate dead code and prevent autoPatchelf issues
    find $out/lib/omp/node_modules -type d \( ${
      lib.concatStringsSep " -o " (
        lib.map (n: "-name '*${n}*'") (
          [
            "musl"
            "sunos"
            "win32"
            "aix"
            "openbsd"
            "freebsd"
            "darwin"
          ]
          ++ lib.optionals stdenv.hostPlatform.isx86_64 [ "arm64" ]
        )
      )
    } \) -exec rm -rf {} +
    find $out/lib/omp/node_modules -type f -name '*.exe' -delete

    makeWrapper "${lib.getExe bun}" "$out/bin/omp" --add-flags "$out/lib/omp/packages/coding-agent/dist/cli.js"

    runHook postInstall
  '';

  postInstall = lib.optionalString (stdenv.buildPlatform.canExecute stdenv.hostPlatform) ''
    installShellCompletion --cmd omp \
      ${lib.concatMapStringsSep " " (sh: "--${sh} <($out/bin/omp completions ${sh})") [
        "bash"
        "zsh"
        "fish"
      ]}
  '';

  doInstallCheck = true;
  nativeInstallCheckInputs = [
    writableTmpDirAsHomeHook
    versionCheckHook
  ];

  meta = {
    description = "Terminal-based coding agent with multi-model support";
    homepage = "https://omp.sh";
    license = lib.licenses.mit;
    mainProgram = "omp";
    platforms = lib.platforms.linux;
  };
})
