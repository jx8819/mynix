# homeconnect-websocket — Python client for local Home Connect WebSocket connection
#
# Upstream: https://github.com/chris-mc1/homeconnect_websocket
# Used by chris-mc1/homeconnect_local_hass
{ lib
, buildPythonPackage
, fetchPypi
, setuptools
, versioningit
, aiohttp
, xmltodict
, pycryptodome
}:

buildPythonPackage rec {
  pname = "homeconnect_websocket";
  version = "1.5.4";
  pyproject = true;

  src = fetchPypi {
    inherit pname version;
    hash = "sha256-NBdDVcIDJ+AXPG23BX5Kngk54MRePVN/LhnPOJ948iM=";
  };

  build-system = [
    setuptools
    versioningit
  ];

  dependencies = [
    aiohttp
    xmltodict
    pycryptodome
  ];

  doCheck = false;
  pythonImportsCheck = [ "homeconnect_websocket" ];

  meta = {
    description = "Control HomeConnect Appliances through a local Websocket connection";
    homepage = "https://github.com/chris-mc1/homeconnect_websocket";
    license = lib.licenses.mit;
  };
}
