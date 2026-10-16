 { stdenv, lib, fetchFromGitHub, libtool, autoconf, automake, gnum4, rdma-core, pciutils }:

 stdenv.mkDerivation rec {
   pname = "perftest";
   # InfiniBand/RDMA verbs 性能测试工具（linux-rdma/perftest 源码打包）。
   version = "26.04.17";

   src = fetchFromGitHub {
     owner = "linux-rdma";
     repo = "perftest";
     rev = "${version}";
     sha256 = "sha256-oNvzQubmslZ4JUNww/wvWd54JDsDLamCDlorHWlNtaY=";
   };

   nativeBuildInputs = [ autoconf automake rdma-core gnum4 libtool pciutils ];
   buildInputs = [ rdma-core ];

   # 构建脚本 shebang 修正到 store 解释器。
   preConfigure = ''
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
