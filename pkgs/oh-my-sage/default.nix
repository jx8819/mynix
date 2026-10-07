{
  lib,
  buildNpmPackage,
  fetchFromGitHub,
  makeWrapper,
  nodejs,
}:

buildNpmPackage rec {
  pname = "oh-my-sage";
  version = "0.0.2-unstable-2026-09-22";

  src = fetchFromGitHub {
    owner = "allocnode";
    repo = "oh-my-sage";
    rev = "4fe2c03ce16c365838b58f70e955e987d1a23531";
    hash = "sha256-qMk43ZgRinnE3Etphgne1rBrlF/a6/oP00NRyhAjMoE=";
  };

  npmDepsHash = "sha256-5JmvGuH+EcOwwU/7C1HCR1sRBFJ0O2o+E45JaRvZuUI=";
  npmBuildScript = "build:mcp";
  npmFlags = [ "--ignore-scripts" ];

  nativeBuildInputs = [ makeWrapper ];

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/lib/oh-my-sage" "$out/bin" "$out/share/oh-my-sage/skills"
    cp -r dist/mcp node_modules package.json "$out/lib/oh-my-sage/"
    cp -r .agents/skills/mijia-automation "$out/share/oh-my-sage/skills/"

    makeWrapper ${nodejs}/bin/node "$out/bin/oh-my-sage-mcp" \
      --add-flags "$out/lib/oh-my-sage/mcp/mcp/index.js"

    runHook postInstall
  '';

  meta = {
    description = "MCP server for Xiaomi Home advanced automations";
    homepage = "https://github.com/allocnode/oh-my-sage";
    license = lib.licenses.mit;
    mainProgram = "oh-my-sage-mcp";
    platforms = lib.platforms.linux;
  };
}
