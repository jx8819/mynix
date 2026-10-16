# miloco-omp-agent — Miloco agent webhook → OMP RPC 桥。
# Miloco webhook 协议（{action,payload} → {code,message,data}）驱动受限的
# `omp --mode rpc` 子进程（仅米家设备/场景宿主工具）；webhook bearer / server token /
# API Key 等私密值只在调用方 options 里填文件路径，本包不内置。
{ python3, lib }:

python3.pkgs.buildPythonApplication {
  pname = "miloco-omp-agent";
  version = "1.0.0";
  format = "other";

  src = ./.;

  # 只用标准库（http.server / urllib / subprocess）。
  installPhase = ''
    install -Dm755 miloco_omp_agent.py $out/bin/miloco-omp-agent
  '';

  meta = {
    description = "Bridge Miloco's agent webhook to a restricted OMP agent with Mi Home device tools only";
    mainProgram = "miloco-omp-agent";
    license = lib.licenses.mit;
  };
}
