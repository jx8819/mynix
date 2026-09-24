{ python3, lib }:

python3.pkgs.buildPythonApplication {
  pname = "mesh-guardian";
  version = "1.0.0";
  format = "other";

  src = ./.;

  # Pure Python stdlib only — no extra deps.
  propagatedBuildInputs = [ ];

  installPhase = ''
    install -Dm755 mesh_guardian.py $out/bin/mesh-guardian
  '';

  meta = {
    description = "Xiaomi Mesh wired-backhaul recovery guardian";
    mainProgram = "mesh-guardian";
    license = lib.licenses.mit;
  };
}
