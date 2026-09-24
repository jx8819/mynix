{
  description = "jx8819 的共享 Nix 库：omp maxwork 扩展、ompweb NixOS module、rules-sync（ros-rules-generator）NixOS module、mesh-guardian（Xiaomi Mesh 有线中继看门狗）NixOS module、自建包（mktxp / nut-exporter / perftest / sas3ircu / yacd-meta / ompweb / ros-rules-generator / mesh-guardian）。机制公开，私密值全在调用方 options。";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [ self.overlays.default ];
      };
    in
    {
      # maxwork 全力模式扩展（/maxwork）
      nixosModules.default = import ./module.nix;
      # ompweb Web UI NixOS module
      nixosModules.ompweb = import ./modules/ompweb.nix;
      # Supermicro IPMI 风扇调速 NixOS module
      nixosModules.fan-control = import ./modules/fan-control.nix;
      # ros-rules-generator 规则列表同步（systemd timer）NixOS module
      nixosModules.rules-sync = import ./modules/rules-sync.nix;
      # Xiaomi Mesh 有线中继恢复看门狗 NixOS module
      nixosModules.mesh-guardian = import ./modules/mesh-guardian.nix;

      # 包 overlay：消费方把它加进自己 nixpkgs.overlays，然后直接用 pkgs.<name>
      overlays.default = final: prev: {
        mk-exporter = prev.callPackage ./pkgs/mk-exporter { };
        nut-exporter = prev.callPackage ./pkgs/nut-exporter { };
        perftest = prev.callPackage ./pkgs/perftest { };
        sas3ircu = prev.callPackage ./pkgs/sas3ircu { };
        yacd-meta = prev.callPackage ./pkgs/yacd-meta { };
        ompweb = prev.callPackage ./pkgs/ompweb { };
        ros-rules-generator = prev.callPackage ./pkgs/ros-rules-generator { };
        mesh-guardian = prev.callPackage ./pkgs/mesh-guardian { };
      };

      # 临时使用：nix run github:jx8819/mynix#<name>
      packages.${system} = {
        inherit (pkgs)
          mk-exporter
          nut-exporter
          perftest
          ros-rules-generator
          sas3ircu
          yacd-meta
          ompweb
          mesh-guardian;
      };
    };
}
