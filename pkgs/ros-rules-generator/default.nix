{ lib, buildGoModule }:

buildGoModule {
  pname = "ros-rules-generator";
  version = "1.0.0";

  src = ./.;
  vendorHash = null;

  env.CGO_ENABLED = 0;

  ldflags = [
    "-s"
    "-w"
  ];

  meta = with lib; {
    description = "Self-contained RouterOS & Clash rules generator";
    license = licenses.mit;
    maintainers = [ ];
    mainProgram = "ros-rules-generator";
  };
}
