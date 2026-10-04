# miloco-omp-agent — Miloco agent webhook → OMP RPC bridge
#
# 机制完全公开：Miloco 的 agent webhook 协议（{action,payload} → {code,message,data}）
# 驱动一个受限的 `omp --mode rpc` 子进程，子进程只有米家设备/场景宿主工具。
# 私密值（webhook bearer / Miloco server token / MiMo API Key）只在调用方的
# options 里填文件路径，本包不内置任何私密值。
{ python3, lib }:

python3.pkgs.buildPythonApplication {
  pname = "miloco-omp-agent";
  version = "1.0.0";
  format = "other";

  src = ./.;

  # 只用标准库（http.server / urllib / subprocess），无 propagatedBuildInputs。
  installPhase = ''
    install -Dm755 miloco_omp_agent.py $out/bin/miloco-omp-agent
  '';

  meta = {
    description = "Bridge Miloco's agent webhook to a restricted OMP agent with Mi Home device tools only";
    mainProgram = "miloco-omp-agent";
    license = lib.licenses.mit;
  };
}
