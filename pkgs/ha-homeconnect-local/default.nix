# ha-homeconnect-local — Siemens/Bosch Home Connect Local 的 HA 集成（domain: homeconnect_ws）。
# 依赖 homeconnect-websocket（nixpkgs 未收录，见 ./homeconnect-websocket.nix），走局域网直连。
{ lib
, buildHomeAssistantComponent
, fetchFromGitHub
, home-assistant
}:

let
  homeconnect-websocket = home-assistant.python3Packages.callPackage ./homeconnect-websocket.nix { };
in
buildHomeAssistantComponent rec {
  owner = "chris-mc1";
  domain = "homeconnect_ws";
  version = "1.0.6";

  src = fetchFromGitHub {
    inherit owner;
    repo = "homeconnect_local_hass";
    tag = "${version}";
    hash = "sha256-7+2MM4sHcr9NcYJqXauiXKNjDLLfdgkDtc60Cc2jG4g=";
  };

  dependencies = [ homeconnect-websocket ];

  meta = {
    changelog = "https://github.com/chris-mc1/homeconnect_local_hass/releases/tag/${version}";
    description = "Home Connect integration for Home Assistant using direct communication over the local network";
    homepage = "https://github.com/chris-mc1/homeconnect_local_hass";
    license = lib.licenses.mit;
  };
}
