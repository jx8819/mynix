{ lib, stdenv, fetchFromGitHub }:

stdenv.mkDerivation rec {
  pname = "udpxy";
  version = "1.0-25.2";

  src = fetchFromGitHub {
    owner = "pcherenkov";
    repo = "udpxy";
    rev = "1.0-25.2";
    hash = "sha256-v+w4Y6MyJqUrgwuYUYTZW0Zn1jhW4vEpgBEQyEjvkzg=";
  };

  postUnpack = "sourceRoot=$sourceRoot/chipmunk";

  preBuild = ''
    export CFLAGS="-W -Wall -Wno-error -DUDPXREC_MOD -DNDEBUG -DTRACE_MODULE"
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 udpxy $out/bin/udpxy
    ln -s udpxy $out/bin/udpxrec
    runHook postInstall
  '';

  meta = with lib; {
    description = "UDP-to-HTTP multicast traffic relay daemon";
    homepage = "https://github.com/pcherenkov/udpxy";
    license = licenses.gpl3Plus;
    platforms = platforms.linux;
  };
}
