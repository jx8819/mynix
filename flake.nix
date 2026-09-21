{
  description = "omp maxwork 全力模式扩展：/maxwork 一键最高模型 + advisor 预检 + codex CLI 联动（extension + NixOS module）";

  outputs = { self }: {
    nixosModules.default = import ./module.nix;
  };
}
