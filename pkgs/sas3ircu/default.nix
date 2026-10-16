{ fetchzip, lib, stdenv }:

# Broadcom SAS3 IR 配置工具（SAS3IRCU）：上游只发预编译闭源二进制，原样打包。
stdenv.mkDerivation rec {
  pname = "sas3ircu";
  version = "P16";

  src = fetchzip {
    url = "https://docs.broadcom.com/docs-and-downloads/host-bus-adapters/host-bus-adapters-common-files/sas_sata_12g_p16/SAS3IRCU_P16.zip";
    sha256 = "uEtO7Nq3sI2pK6AhZQEOvKZd1a110J2fMtARwm2cUTk=";
  };

  installPhase = ''
    install -Dm755 sas3ircu_rel/sas3ircu/sas3ircu_linux_x64_rel/sas3ircu $out/bin/sas3ircu
  '';

  meta = {
    description = "SAS3 Integrated RAID Configuration Utility (Broadcom/LSI)";
    license = lib.licenses.unfree;
    mainProgram = "sas3ircu";
  };
}
