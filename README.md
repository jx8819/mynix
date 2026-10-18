# mynix

Nix 软件包与 NixOS 模块。

- 查看输出：`nix flake show github:jx8819/mynix`
- 构建软件包：`nix build github:jx8819/mynix#<包名>`（x86_64-linux）
- 在 NixOS 配置中按需导入 `mynix.nixosModules.<名称>`；软件包可通过 `mynix.overlays.default` 使用。

源码见 `pkgs/`、`modules/`；输出清单以 `flake.nix` 为准。部分包需要 `allowUnfree`。
