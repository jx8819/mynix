{ fetchzip, lib, stdenv, unzip }:

stdenv.mkDerivation rec {
  pname = "sas3ircu";
  version = "P16";

  src = fetchzip {
    url = "https://docs.broadcom.com/docs-and-downloads/host-bus-adapters/host-bus-adapters-common-files/sas_sata_12g_p16/SAS3IRCU_P16.zip";
    sha256 = "uEtO7Nq3sI2pK6AhZQEOvKZd1a110J2fMtARwm2cUTk=";
  };


  installPhase = ''
    mkdir -p $out/bin
    cp sas3ircu_rel/sas3ircu/sas3ircu_linux_x64_rel/sas3ircu -d $out/bin
    chmod +x $out/bin/sas3ircu
  '';
}
