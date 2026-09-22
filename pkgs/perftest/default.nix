 { stdenv, lib, fetchFromGitHub, libtool, autoconf, automake, gnum4, rdma-core, pciutils }:

 stdenv.mkDerivation rec {
   pname = "perftest";
   # 2026-09-21 从 24.01.0-0.38 升到上游最新 release（linux-rdma/perftest）。
   version = "26.04.17";

   src = fetchFromGitHub {
     owner = "linux-rdma";
     repo = "perftest";
     rev = "${version}";
     sha256 = "sha256-oNvzQubmslZ4JUNww/wvWd54JDsDLamCDlorHWlNtaY=";
   };

   nativeBuildInputs = [ autoconf automake rdma-core gnum4 libtool pciutils ];
   buildInputs = [ rdma-core ];

   postUnpack =  ''
     patchShebangs .
   '';

   configurePhase = ''
     runHook preConfigure
     ./autogen.sh
     ./configure --prefix=$out
     runHook postConfigure
   '';
  meta = with lib; {
    description = "Infiniband Verbs Performance Tests";
    license = licenses.gpl2Only;
    platforms = platforms.linux;
    maintainers = with maintainers; [ ];
  };

 }
