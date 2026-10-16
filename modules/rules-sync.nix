{ config, pkgs, lib, ... }:

let
  cfg = config.services.rulesSync;

  # 域名列表落盘为逐行文本（末尾补换行）；同名同内容生成同一 store 路径
  mkLinesFile = name: domains:
    pkgs.writeText name (lib.concatStringsSep "\n" domains + "\n");

  directDomainsFile = mkLinesFile "rules-sync-direct-domains.txt" cfg.directDomains;
  proxyDomainsFile = mkLinesFile "rules-sync-proxy-domains.txt" cfg.proxyDomains;
  cleanIpDomainsFile = mkLinesFile "rules-sync-clean-ip-domains.txt" cfg.cleanIpDomains;
  tmdbDomainsFile = mkLinesFile "rules-sync-tmdb-domains.txt" cfg.tmdbDns.domains;

  sourcesFile = pkgs.writeText "rules-sync-sources.json" (builtins.toJSON {
    gfwlist = cfg.gfwlistSources;
    ai = cfg.aiSources;
    google = cfg.googleSources;
    rules = cfg.ruleSources;
  });

  dnsmasqForwardsFile = pkgs.writeText "rules-sync-dnsmasq-forwards.json"
    (builtins.toJSON cfg.dnsmasqForwards);

  # 生成器命令行参数按固定顺序拼接；-tmdb-* 仅在 tmdbDns 开启时追加，
  # -tmdb-edns-subnet 仅在 ECS 非空时追加（空值即关闭），-debug 收尾
  generatorArgs =
    [
      "-out" "${cfg.outputDir}"
      "-direct-domains-file" "${directDomainsFile}"
      "-proxy-domains-file" "${proxyDomainsFile}"
      "-clean-ip-domains-file" "${cleanIpDomainsFile}"
      "-sources" "${sourcesFile}"
      "-dnsmasq-forwards-file" "${dnsmasqForwardsFile}"
    ]
    ++ lib.optionals cfg.tmdbDns.enable (
      [ "-tmdb-domains-file" "${tmdbDomainsFile}" ]
      ++ lib.optionals (cfg.tmdbDns.ednsClientSubnet != "") [ "-tmdb-edns-subnet" "${cfg.tmdbDns.ednsClientSubnet}" ]
    )
    ++ lib.optional cfg.debug "-debug";

  # 上游源子模块公共字段：name 供日志/报错定位，url 为下载地址；extra 为各源特有字段
  mkSourceSubmodule = urlDescription: extra:
    lib.types.submodule {
      options = {
        name = lib.mkOption {
          type = lib.types.str;
          description = "Unique source identifier, used in logs and errors.";
        };
        url = lib.mkOption {
          type = lib.types.str;
          description = urlDescription;
        };
      } // extra;
    };
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
      default = "rules-sync";
      description = "Account the service runs as; created unconditionally by this module.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "github-sync";
      description = "Group of the service account; created unconditionally by this module.";
    };

    directDomains = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Direct whitelist domains excluded from proxy rules.";
    };

    proxyDomains = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Custom proxy domains included in proxy rules.";
    };

    cleanIpDomains = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Clean IP domains routed through clean egress proxies (generated into rules/clean-ip.txt and domain.rsc).";
    };

    tmdbDns = {
      enable = lib.mkEnableOption "DNS-TMDB 静态映射生成（tmdb.rsc）：DoH 取 TMDB 域名真实 A 记录并经 TLS SNI 校验后输出 /ip dns static 映射，供全网直连";
      domains = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "api.themoviedb.org" "image.tmdb.org" "www.themoviedb.org" ];
        description = "TMDB 域名列表，默认为其三个常用域名，可增删。";
      };
      ednsClientSubnet = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "DoH 查询的 EDNS Client Subnet 提示（只影响 CDN 选边，不影响正确性）；空字符串关闭。";
      };
    };

    dnsmasqForwards = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption {
            type = lib.types.str;
            description = "Output name: default writes domain.dnsmasq.conf; other names write domain.dnsmasq.<name>.conf.";
          };
          target = lib.mkOption {
            type = lib.types.str;
            description = "DNS upstream IP address or hostname for server=/domain/target#port entries.";
          };
          port = lib.mkOption {
            type = lib.types.ints.between 1 65535;
            description = "DNS upstream port.";
          };
        };
      });
      default = [];
      example = [
        { name = "default"; target = "127.0.0.1"; port = 1053; }
        { name = "backup"; target = "192.0.2.53"; port = 53; }
      ];
      description = "Named dnsmasq forwarding variants generated from the same clean domain list; an empty list generates none.";
    };

    gfwlistSources = lib.mkOption {
      type = lib.types.listOf (mkSourceSubmodule "Download URL of the GFW list source." {
        format = lib.mkOption {
          type = lib.types.enum [ "gfwlist" "fancyss" "plain" "microsoft" ];
          description = "Parser applied to the downloaded source: gfwlist (base64 autoproxy), fancyss (dnsmasq ipset lines), plain (raw domain lines), microsoft (DOMAIN-SUFFIX rules).";
        };
      });
      default = [];
      description = "GFW list sources for clean-list generation, processed in list order; empty by default, populate in your private consumer.";
    };

    aiSources = lib.mkOption {
      type = lib.types.listOf (mkSourceSubmodule "Download URL of the clash-ai-rules YAML source." {});
      default = [];
      description = "AI rule sources for rules/ai.yaml and the clean-ai domain set; empty by default, populate in your private consumer.";
    };

    googleSources = lib.mkOption {
      type = lib.types.listOf (mkSourceSubmodule "Download URL of the Google rules source." {});
      default = [];
      description = "Google rule sources concatenated in list order into rules/google_rules.yaml; empty by default, populate in your private consumer.";
    };

    ruleSources = lib.mkOption {
      type = lib.types.listOf (mkSourceSubmodule "Download URL of the rule file." {
        output = lib.mkOption {
          type = lib.types.str;
          description = "Output path relative to outputDir, without .. segments; .yaml outputs get non-ASCII payload filtering.";
        };
      });
      default = [];
      description = "Rule files downloaded verbatim to their configured output paths; empty by default, populate in your private consumer.";
    };

    calendar = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 05:30:00";
      description = "systemd OnCalendar specification for the scheduled generator run.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.all (forward: builtins.match "[A-Za-z0-9][A-Za-z0-9_-]*" forward.name != null) cfg.dnsmasqForwards;
        message = "services.rulesSync.dnsmasqForwards names must contain only ASCII letters, digits, underscore, or hyphen, beginning with a letter or digit.";
      }
      {
        assertion = builtins.length (lib.unique (map (forward: forward.name) cfg.dnsmasqForwards))
          == builtins.length cfg.dnsmasqForwards;
        message = "services.rulesSync.dnsmasqForwards names must be unique.";
      }
      {
        assertion = lib.all (forward: forward.target != "") cfg.dnsmasqForwards;
        message = "services.rulesSync.dnsmasqForwards targets must be non-empty IP addresses or hostnames.";
      }
    ];

    # 服务账号/组由本模块无条件创建，不依赖特定账号名
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      description = "ros-rules-generator lists sync";
    };

    users.groups.${cfg.group} = {};

    systemd.tmpfiles.rules = [
      # 预建输出目录父目录并交权服务账号；0755 保证服务可写、下游读取进程可进入
      "d ${builtins.dirOf cfg.outputDir} 0755 ${cfg.user} ${cfg.group} - -"
    ];

    systemd.services.rules-sync = {
      description = "Generate RouterOS and Clash DNS/IP routing rules";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        # 参数表经 escapeShellArgs 转义拼接，值含空格等特殊字符时自动加引号
        ExecStart = lib.escapeShellArgs ([ "${cfg.package}/bin/ros-rules-generator" ] ++ generatorArgs);

        # UMask 0022：生成物权限固化为目录 0755、文件 0644
        UMask = "0022";

        # 沙箱加固：限制服务权限面
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
