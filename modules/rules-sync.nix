{ config, pkgs, lib, ... }:

let
  cfg = config.services.rulesSync;

  directDomainsFile = pkgs.writeText "rules-sync-direct-domains.txt" (
    lib.concatStringsSep "\n" cfg.directDomains + "\n"
  );

  proxyDomainsFile = pkgs.writeText "rules-sync-proxy-domains.txt" (
    lib.concatStringsSep "\n" cfg.proxyDomains + "\n"
  );

  sourcesFile = pkgs.writeText "rules-sync-sources.json" (builtins.toJSON {
    gfwlist = cfg.gfwlistSources;
    ai = cfg.aiSources;
    google = cfg.googleSources;
    rules = cfg.ruleSources;
  });
in
{
  options.services.rulesSync = {
    enable = lib.mkEnableOption "RouterOS and Clash DNS/IP routing rules generator service";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../pkgs/ros-rules-generator {};
      description = "The package providing the ros-rules-generator binary.";
    };

    outputDir = lib.mkOption {
      type = lib.types.path;
      description = "Directory where generated rule files will be saved.";
      example = "/etc/nixos/services/caddy/web/lists";
    };

    debug = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to enable verbose debug logging to journalctl.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "github-sync";
      description = "User to run the generator service.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "github-sync";
      description = "Group to run the generator service.";
    };

    directDomains = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Direct whitelist domains to exclude from proxy rules";
    };

    proxyDomains = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Custom proxy domains to include in proxy rules";
    };

    gfwlistSources = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption {
            type = lib.types.str;
            description = "Unique source identifier, used in logs and errors.";
          };
          url = lib.mkOption {
            type = lib.types.str;
            description = "Download URL of the GFW list source.";
          };
          format = lib.mkOption {
            type = lib.types.enum [ "gfwlist" "fancyss" "plain" "microsoft" ];
            description = "Parser applied to the downloaded source: gfwlist (base64 autoproxy), fancyss (dnsmasq ipset lines), plain (raw domain lines), microsoft (DOMAIN-SUFFIX rules).";
          };
        };
      });
      default = [
        { name = "gfwlist1"; url = "https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt"; format = "gfwlist"; }
        { name = "gfwlist2"; url = "https://raw.githubusercontent.com/hq450/fancyss/master/rules/gfwlist.conf"; format = "fancyss"; }
        { name = "gfwlist3"; url = "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/gfw.txt"; format = "plain"; }
        { name = "gfwlist4"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Microsoft.list"; format = "microsoft"; }
        { name = "gfwlist5"; url = "https://raw.githubusercontent.com/Loukky/gfwlist-by-loukky/master/gfwlist.txt"; format = "gfwlist"; }
      ];
      description = "GFW list sources feeding clean-list generation, processed in list order. Any number of entries is allowed.";
    };

    aiSources = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption {
            type = lib.types.str;
            description = "Unique source identifier, used in logs and errors.";
          };
          url = lib.mkOption {
            type = lib.types.str;
            description = "Download URL of the clash-ai-rules YAML source.";
          };
        };
      });
      default = [
      ];
      description = "AI rule sources feeding rules/ai.yaml and the clean-ai domain set. Any number of entries is allowed.";
    };

    googleSources = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption {
            type = lib.types.str;
            description = "Unique source identifier, used in logs and errors.";
          };
          url = lib.mkOption {
            type = lib.types.str;
            description = "Download URL of the Google rules source.";
          };
        };
      });
      default = [
        { name = "google_list"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/refs/heads/master/PROXY/Google.list"; }
      ];
      description = "Google rules sources; each entry is transformed into rules/google_rules.yaml (payload sections are concatenated in list order).";
    };

    ruleSources = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption {
            type = lib.types.str;
            description = "Unique source identifier, used in logs and errors.";
          };
          url = lib.mkOption {
            type = lib.types.str;
            description = "Download URL of the rule file.";
          };
          output = lib.mkOption {
            type = lib.types.str;
            description = "Output path relative to outputDir. Must be a relative path without .. segments. .yaml outputs get non-ASCII payload filtering.";
          };
        };
      });
      default = [
        { name = "rule_Telegram"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/PROXY/Telegram.yaml"; output = "rules/Telegram.yaml"; }
        { name = "rule_YouTube"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/Global-Services/YouTube.yaml"; output = "rules/YouTube.yaml"; }
        { name = "rule_Netflix"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/Global-Services/Netflix.yaml"; output = "rules/Netflix.yaml"; }
        { name = "rule_GlobalMedia"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/GlobalMedia.yaml"; output = "rules/GlobalMedia.yaml"; }
        { name = "rule_PROXY"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/PROXY.yaml"; output = "rules/PROXY.yaml"; }
        { name = "rule_Apple"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/Apple.yaml"; output = "rules/Apple.yaml"; }
        { name = "rule_Game"; url = "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/Game.yaml"; output = "rules/Game.yaml"; }
        { name = "rule_proxy_txt"; url = "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/proxy.txt"; output = "rules/proxy.txt"; }
        { name = "rule_lancidr_txt"; url = "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/lancidr.txt"; output = "rules/lancidr.txt"; }
        { name = "rule_gfw_txt"; url = "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/gfw.txt"; output = "rules/gfw.txt"; }
        { name = "rule_greatfire"; url = "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/greatfire.txt"; output = "rules/greatfire.txt"; }
        { name = "rule_direct_txt"; url = "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/direct.txt"; output = "rules/direct.txt"; }
      ];
      description = "Rule files downloaded verbatim to their configured output path. Any number of entries is allowed.";
    };

    calendar = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 05:30:00";
      description = "systemd OnCalendar specification for the scheduled generator run.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.${cfg.user} = lib.mkIf (cfg.user == "github-sync") {
      isSystemUser = true;
      group = cfg.group;
      description = "ros-rules-generator lists sync";
    };

    users.groups.${cfg.group} = lib.mkIf (cfg.group == "github-sync") {};

    systemd.tmpfiles.rules = [
      # 确保父目录存在，属主为服务运行用户，0755 权限使 caddy 进程可进入
      "d ${builtins.dirOf cfg.outputDir} 0755 ${cfg.user} ${cfg.group} - -"
    ];

    # 平滑切换：彻底停用并禁用旧的 github.service 与 github.timer，防止与新服务发生写入竞态
    systemd.services.github = {
      enable = false;
      wantedBy = lib.mkForce [];
    };
    systemd.timers.github = {
      enable = false;
      wantedBy = lib.mkForce [];
    };

    # 规则生成 Oneshot 服务
    systemd.services.rules-sync = {
      description = "Generate RouterOS and Clash DNS/IP routing rules";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${cfg.package}/bin/ros-rules-generator -out ${cfg.outputDir} -direct-domains-file ${directDomainsFile} -proxy-domains-file ${proxyDomainsFile} -sources ${sourcesFile}${lib.optionalString cfg.debug " -debug"}";

        # 显式指定 UMask 为 0022，保证目录 0755 与文件 0644 的权限确定性
        UMask = "0022";

        # 沙箱加固防护
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectControlGroups = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;

        # 读写输出目录的父目录（允许创建/替换 outputDir 及其同级原子临时目录）
        ReadWritePaths = [
          (builtins.dirOf cfg.outputDir)
        ];
      };
    };

    # 规则更新定时器
    systemd.timers.rules-sync = {
      description = "Timer for RouterOS rules generator";
      wantedBy = [ "timers.target" ];
      partOf = [ "rules-sync.service" ];
      timerConfig = {
        OnBootSec = "15m";
        OnCalendar = cfg.calendar;
        OnUnitInactiveSec = "12h";
        Persistent = true;
        RandomizedDelaySec = "5m";
      };
    };
  };
}
