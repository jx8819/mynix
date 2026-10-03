{
  fetchurl,
  lib,
  stdenvNoCC,
}:

stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "herdr";
  version = "0.9.1";

  src = fetchurl {
    url = "https://github.com/herdrdev/herdr/releases/download/v${finalAttrs.version}/herdr-linux-x86_64";
    hash = "sha256-KgL+0WvrZR7wBuHUPwSPZSyk3FitBTzS1ERQVj1cVLc=";
  };

  dontUnpack = true;
  strictDeps = true;

  installPhase = ''
    runHook preInstall
    install -Dm755 "$src" "$out/bin/herdr"
    runHook postInstall
  '';

  meta = {
    description = "Terminal workspace manager for AI coding agents";
    homepage = "https://herdr.dev";
    changelog = "https://github.com/herdrdev/herdr/releases/tag/v${finalAttrs.version}";
    license = lib.licenses.asl20;
    mainProgram = "herdr";
    platforms = [ "x86_64-linux" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
})
