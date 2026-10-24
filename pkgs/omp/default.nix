# omp — 终端编码代理（can1357/oh-my-pi 源码打包）。
# 配方对齐 nixpkgs 的 omp：bun 打 JS bundle、cargo 构建 pi-natives N-API addon，源码钉固定 rev。
# 数据/凭据运行时在 $PI_CODING_AGENT_DIR / $HOME，不进 store。
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
  version = "18.12.6";

  strictDeps = true;
  __structuredAttrs = true;

  src = fetchFromGitHub {
    owner = "can1357";
    repo = "oh-my-pi";
    # 与现网部署的 omp 保持同源 pin。
    rev = "45c35e40c46166dc726ccc52f5d27f40f2449f99";
    hash = "sha256-Vx+zWGb0z/IgIFLbCv/gvK7iQobbKIkA1DhCjsYIm6o=";
  };

  cargoDeps = rustPlatform.fetchCargoVendor {
    inherit (finalAttrs) src;
    hash = "sha256-BTZP9pZ18s8K77P+XJ4IQyKtVyy06Xhd3KnDvaBog2s=";
  };

  postPatch = ''
    substituteInPlace packages/coding-agent/src/cli.ts \
      --replace-fail 'if (Bun.semver.order(Bun.version, MIN_BUN_VERSION) < 0)' 'if (false)'
  '';

  # node_modules 单独做固定输出：绕开 nixpkgs 里 bun 构建依赖的已知缺陷。
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

    # 不做 fixup，避免弄断 node_modules 里的符号链接。
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
    # 上游源码用到尚未稳定的 feature（result_option_map_or_default 等），本工具链是 stable：
    # 用 crate-attr 给全部 crate 统一开闸（免逐 crate 改源码），RUSTC_BOOTSTRAP 让 stable 编译器启用 gate。
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

    # 剪掉异平台二进制（死代码），避免 autoPatchelf 误处理。
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
