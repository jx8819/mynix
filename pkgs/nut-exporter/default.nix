{ rustPlatform, fetchFromGitHub, lib }:

rustPlatform.buildRustPackage rec {
  pname = "prometheus-nut-exporter";
  version = "1.2.1";

  src = fetchFromGitHub {
    owner = "HON95";
    repo = "prometheus-nut-exporter";
    rev = "v${version}";
    hash = "sha256-2456V5WsfpkaFg8jHZ2KboCT0QPoBCaXsAhHQ6LxGr4=";
  };

  cargoHash = "sha256-B/e+POAhVpOqF9xHTDrxJ5yQYgFV65dBSCmykVaR4JE=";


  meta = with lib; {
    description = "A Prometheus exporter for Network UPS Tools (NUT)";
    homepage = src.meta.homepage;
    license = licenses.gpl3;
    maintainers = with maintainers; [ ];
  };
}
