{ lib, stdenv, fetchFromGitHub, cmake }:

stdenv.mkDerivation (finalAttrs: {
  pname = "rtp2httpd";
  version = "3.17.2";

  src = fetchFromGitHub {
    owner = "stackia";
    repo = "rtp2httpd";
    tag = "v${finalAttrs.version}";
    hash = "sha256-jOxWN2I5rq2Fbb61EjjIuG8q64Nklfg3l8iJ9RM8cUE=";
  };

  nativeBuildInputs = [ cmake ];
  env.RELEASE_VERSION = finalAttrs.version;

  # Release tags include the generated web UI header; no Node.js runtime or rebuild is needed.
  meta = with lib; {
    description = "IPTV multicast RTP/UDP, RTSP and HTTP streaming proxy";
    homepage = "https://github.com/stackia/rtp2httpd";
    license = licenses.gpl2Only;
    platforms = platforms.linux;
    mainProgram = "rtp2httpd";
  };
})
