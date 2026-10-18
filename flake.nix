{
  description = "Nix 软件包与 NixOS 模块。";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";

      # 单源包清单：包名只写这一遍（键 = pkgs/ 目录名），overlays.default 与 packages 共用。
      # 值 = callPackage 变体："default" 走 prev.callPackage，"python" 走 prev.python3.pkgs.callPackage。
      packageList = {
        # Prometheus NUT（UPS）指标导出器
        nut-exporter = "default";
        # InfiniBand verbs 性能测试工具集
        perftest = "default";
        # Broadcom SAS3 IR 配置工具（闭源二进制，unfree）
        sas3ircu = "default";
        # Yacd-meta 仪表盘（mihomo external-ui）
        yacd-meta = "default";
        # OMP 编码代理 Web UI
        ompweb = "default";
        # omp 终端编码代理（can1357/oh-my-pi 源码打包）
        omp = "default";
        # 米家自动化规则列表同步 CLI（rules-sync 模块用）
        ros-rules-generator = "default";
        # 广告过滤列表定时拉取与原子发布
        adlist-sync = "default";
        # IPTV 播放列表过滤、合并与组播地址转换
        iptv-merge = "default";
        # 小米 Mesh 有线组网恢复看门狗
        mesh-guardian = "default";
        # HDSky（hdsky.me）自动签到
        hdsky-checkin = "default";
        # 米家高级自动化 MCP 服务
        oh-my-sage = "default";
        # Miloco agent webhook → 受限 OMP 桥（仅米家设备/场景工具）
        miloco-omp-agent = "default";
        # 米家设备 MCP 服务
        miot-mcp = "default";
        # ddddocr 验证码识别 python 库（nixpkgs 未收录，hdsky-checkin 的依赖），也可单独用
        ddddocr = "python";
        # HA 自定义组件：Dyson（domain: dyson_local）
        ha-dyson = "default";
        # HA 自定义组件：Cololight（domain: cololight）
        ha-cololight = "default";
        # HA 官方小米集成（domain: xiaomi_home，unfree）
        ha-xiaomi-home = "default";
        # HA 自定义组件：Home Connect Local（domain: homeconnect_ws）
        ha-homeconnect-local = "default";
        # Lume VPS 探针监控客户端（vpsmon-agent）
        lume-agent = "default";
        # UDP-to-HTTP 组播转单播代理
        udpxy = "default";
      };

      # 按清单构造包：每个键对应 ./pkgs/<键>，ddddocr 是 python 库走 python3.pkgs.callPackage。
      mkOverlay = final: prev:
        builtins.mapAttrs (name: variant: {
          default = prev.callPackage;
          python = prev.python3.pkgs.callPackage;
        }.${variant} (./pkgs + "/${name}") { }) packageList;

      pkgs = import nixpkgs {
        inherit system;
        overlays = [ self.overlays.default ];
      };
    in
    {
      # ── NixOS modules ──
      # maxwork 全力模式扩展（/maxwork）
      nixosModules.default = import ./modules/omp-maxwork.nix;
      # ompweb Web UI 服务
      nixosModules.ompweb = import ./modules/ompweb.nix;
      # omp 编码代理服务
      nixosModules.omp = import ./modules/omp.nix;
      # Supermicro IPMI 风扇调速
      nixosModules.fan-control = import ./modules/fan-control.nix;
      # ros-rules-generator 规则列表同步（systemd timer）
      nixosModules.rules-sync = import ./modules/rules-sync.nix;
      # 广告过滤列表定时同步
      nixosModules.adlist-sync = import ./modules/adlist-sync.nix;
      # IPTV 多模式播放列表同步
      nixosModules.iptv-merge = import ./modules/iptv-merge.nix;
      # 小米 Mesh 有线组网恢复看门狗
      nixosModules.mesh-guardian = import ./modules/mesh-guardian.nix;
      # HDSky（hdsky.me）自动签到
      nixosModules.hdsky-checkin = import ./modules/hdsky-checkin.nix;
      # Herdr 终端工作区管理器
      nixosModules.herdr = import ./modules/herdr.nix;
      # Home Assistant 自定义组件集合
      nixosModules.home-assistant-plugins = import ./modules/home-assistant-plugins.nix;
      # Miloco agent webhook → OMP 桥（仅米家设备/场景工具）
      nixosModules.miloco-omp-agent = import ./modules/miloco-omp-agent.nix;
      # Lume VPS 探针监控 Agent
      nixosModules.lume-agent = import ./modules/lume-agent.nix;
      # udpxy 组播转单播服务
      nixosModules.udpxy = import ./modules/udpxy.nix;

      # 包 overlay：消费方把它加进自己 nixpkgs.overlays，然后直接用 pkgs.<name>
      overlays.default = mkOverlay;

      # 临时使用：nix run github:jx8819/mynix#<name>；取值与 overlay 注入 pkgs 的同名属性一致
      packages.${system} = builtins.intersectAttrs packageList pkgs;
    };
}
