{
  description = "jx8819 的共享 Nix 库：omp maxwork 扩展、ompweb NixOS module、rules-sync（ros-rules-generator）NixOS module、mesh-guardian（Xiaomi Mesh 有线中继看门狗）NixOS module、hdsky-checkin（HDSky 自动签到）NixOS module、Herdr NixOS module、自建包（mktxp / nut-exporter / perftest / sas3ircu / yacd-meta / ompweb / ros-rules-generator / mesh-guardian / hdsky-checkin / herdr / oh-my-sage / ddddocr / ha-xiaomi-home）。机制公开，私密值全在调用方 options。";

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
      # HDSky（hdsky.me）自动签到 NixOS module
      nixosModules.hdsky-checkin = import ./modules/hdsky-checkin.nix;
      # Herdr terminal workspace manager NixOS module
      nixosModules.herdr = import ./modules/herdr.nix;
      # Home Assistant custom components module
      nixosModules.home-assistant-plugins = import ./modules/home-assistant-plugins.nix;
      # Miloco agent webhook → OMP bridge（只给米家设备/场景工具）NixOS module
      nixosModules.miloco-omp-agent = import ./modules/miloco-omp-agent.nix;
      # Lume VPS 探针监控 Agent NixOS module
      nixosModules.lume-agent = import ./modules/lume-agent.nix;

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
        hdsky-checkin = prev.callPackage ./pkgs/hdsky-checkin { };
        herdr = prev.callPackage ./pkgs/herdr { };
        oh-my-sage = prev.callPackage ./pkgs/oh-my-sage { };
        # Miloco agent webhook → OMP bridge（仅米家设备/场景工具）
        miloco-omp-agent = prev.callPackage ./pkgs/miloco-omp-agent { };
        # python 库（nixpkgs 未收录），hdsky-checkin 的依赖；也可单独用
        ddddocr = prev.python3.pkgs.callPackage ./pkgs/ddddocr { };
        # Home Assistant 自定义组件（Dyson，domain: dyson_local）
        ha-dyson = prev.callPackage ./pkgs/ha-dyson { };
        # Home Assistant 自定义组件（Cololight，domain: cololight）
        ha-cololight = prev.callPackage ./pkgs/ha-cololight { };
        # Home Assistant 官方小米集成（domain: xiaomi_home）
        ha-xiaomi-home = prev.callPackage ./pkgs/ha-xiaomi-home { };
        # Home Assistant 自定义组件（Home Connect Local，domain: homeconnect_ws）
        ha-homeconnect-local = prev.callPackage ./pkgs/ha-homeconnect-local { };
        # Lume VPS 监控客户端 (vpsmon-agent)
        lume-agent = prev.callPackage ./pkgs/lume-agent { };
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
          mesh-guardian
          hdsky-checkin
          herdr
          oh-my-sage
          miloco-omp-agent
          ddddocr
          ha-dyson
          ha-cololight
          ha-xiaomi-home
          ha-homeconnect-local
          lume-agent;
      };
    };
}
