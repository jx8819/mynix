{ config, lib, pkgs, ... }:
# IPTV 多模式同步模块：过滤/合并引擎位于 pkgs/iptv-merge。
let
  cfg = config.services.iptv-merge;
  inherit (lib) mkOption mkEnableOption types;

  pkg = pkgs.callPackage ../pkgs/iptv-merge {
    telecomUrl = cfg.telecom.url;
    mobileUrl = cfg.mobile.url;
    telecomMinChannels = cfg.telecom.minChannels;
    telecomRequirePattern = cfg.telecom.requirePattern;
    telecomBlacklist = cfg.telecom.blacklist;
    telecomDropGroups = cfg.telecom.dropGroups;
    mobileMinChannels = cfg.mobile.minChannels;
    mobileRequirePattern = cfg.mobile.requirePattern;
    mobileBlacklist = cfg.mobile.blacklist;
    mobileDropGroups = cfg.mobile.dropGroups;
    mergeStrategy = cfg.merge.strategy;
    multicastUrl = cfg.multicast.url;
    multicastMinChannels = cfg.multicast.minChannels;
    multicastRequirePattern = cfg.multicast.requirePattern;
    multicastBlacklist = cfg.multicast.blacklist;
    multicastDropGroups = cfg.multicast.dropGroups;
    telecomSourceLabel = cfg.telecom.sourceLabel;
    mobileSourceLabel = cfg.mobile.sourceLabel;
    multicastSourceLabel = cfg.multicast.sourceLabel;
  };

  servicePath = [ pkgs.bash pkgs.curl pkgs.gnugrep pkgs.gnused pkgs.gawk pkgs.coreutils ];
  # 四模式各发布独立服务与定时器，模式 → 单元名固定映射。
  unitNames = {
    both = "iptv-sync";
    "telecom-only" = "iptv-telecom-sync";
    "mobile-only" = "iptv-mobile-sync";
    multicast = "iptv-multicast-sync";
  };

  mkServiceConfig = outputPath: {
    Type = "oneshot";
    User = cfg.service.user;
    Group = cfg.service.group;
    UMask = cfg.service.umask;
    ExecStart = "${pkg}/bin/iptv-merge";
    ProtectSystem = "strict";
    ReadWritePaths = [ (lib.dirOf outputPath) ];
    ProtectHome = "true";
    PrivateTmp = "true";
    NoNewPrivileges = "true";
    ProtectControlGroups = "true";
    ProtectKernelModules = "true";
    ProtectKernelTunables = "true";
    LockPersonality = "true";
    MemoryDenyWriteExecute = "true";
    RestrictRealtime = "true";
    RestrictSUIDSGID = "true";
    RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
    TimeoutStartSec = toString cfg.service.timeoutSec;
  };

  commonTimerConfig = {
    OnBootSec = cfg.schedule.onBootSec;
    OnUnitInactiveSec = cfg.schedule.onUnitInactiveSec;
    RandomizedDelaySec = cfg.schedule.randomizedDelaySec;
  };
in
{
  options.services.iptv-merge = {
    enable = mkEnableOption "多模式 IPTV 播放列表同步";

    outputs = mkOption {
      type = types.listOf (types.submodule {
        options = {
          mode = mkOption {
            type = types.enum [ "both" "telecom-only" "mobile-only" "multicast" ];
            description = "合并模式：both / telecom-only / mobile-only / multicast。";
          };
          outputPath = mkOption {
            type = types.str;
            description = "该模式的 m3u 输出路径（唯一可写目录由 dirOf 推导）。";
          };
          udpxy = mkOption {
            type = types.nullOr types.str;
            default = null;
            description = "组播模式使用的 udpxy 代理地址（例如 proxy.example:4022）。";
          };
        };
      });
      description = "独立发布的模式与对应路径；空列表不创建 IPTV 单元。";
    };

    # 第一源参数（电信）
    telecom = {
      url = mkOption {
        type = types.str;
        default = "";
        description = "第一源订阅 URL。";
      };
      sourceLabel = mkOption {
        type = types.str;
        default = "telecom";
        description = "第一源在生成的 M3U # updated 行内的标识。";
      };
      minChannels = mkOption {
        type = types.int;
        default = 1;
        description = "电信源最少 #EXTINF 条数，低于则拒收保旧。";
      };
      requirePattern = mkOption {
        type = types.str;
        default = ".";
        description = "电信源必须包含的 URL 特征（不匹配则拒收）。";
      };
      blacklist = mkOption {
        type = types.listOf types.str;
        default = [];
        description = "第一源频道名字黑名单。";
      };
      dropGroups = mkOption {
        type = types.listOf types.str;
        default = [];
        description = "按 group-title 精确匹配整组剔除的分组名。";
      };
    };

    # 第二源参数（移动）
    mobile = {
      url = mkOption {
        type = types.str;
        default = "";
        description = "第二源订阅 URL。";
      };
      sourceLabel = mkOption {
        type = types.str;
        default = "mobile";
        description = "第二源在生成的 M3U # updated 行内的标识。";
      };
      minChannels = mkOption {
        type = types.int;
        default = 1;
        description = "移动源最少 #EXTINF 条数，低于则拒收保旧。";
      };
      requirePattern = mkOption {
        type = types.str;
        default = ".";
        description = "移动源必须包含的 URL 特征（不匹配则拒收）。";
      };
      blacklist = mkOption {
        type = types.listOf types.str;
        default = [];
        description = "第二源频道名字黑名单。";
      };
      dropGroups = mkOption {
        type = types.listOf types.str;
        default = [];
        description = "按 group-title 精确匹配整组剔除的分组名。";
      };
    };

    # 组播源参数
    multicast = {
      url = mkOption {
        type = types.str;
        default = "";
        description = "组播源订阅 URL。";
      };
      sourceLabel = mkOption {
        type = types.str;
        default = "multicast";
        description = "组播源在生成的 M3U # updated 行内的标识。";
      };
      minChannels = mkOption {
        type = types.int;
        default = 1;
        description = "组播源最少 #EXTINF 条数，低于则拒收保旧。";
      };
      requirePattern = mkOption {
        type = types.str;
        default = ".";
        description = "组播源必须包含的 URL 特征（不匹配则拒收）。";
      };
      blacklist = mkOption {
        type = types.listOf types.str;
        default = [];
        description = "组播源频道名字黑名单。";
      };
      dropGroups = mkOption {
        type = types.listOf types.str;
        default = [];
        description = "按 group-title 精确匹配整组剔除的分组名。";
      };
    };

    # 合并策略（both 模式下移动源的取舍规则）
    merge = {
      strategy = mkOption {
        type = types.enum [ "smart" "exact" "off" ];
        default = "smart";
        description = ''
          双源合并策略：
          smart = norm 键去重（跨源 + 移动内部 first-wins，含 CCTV 裸号 core 规则）；
          exact = 仅按 norm 键判重（无 CCTV core 分支）；
          off   = 纯追加，不做任何判重。
        '';
      };
    };

    # 定时节奏：开机首跑、距上次完成再跑，带随机抖动
    schedule = {
      onBootSec = mkOption {
        type = types.str;
        default = "2min";
        description = "systemd timer OnBootSec（开机后首跑延迟）。";
      };
      onUnitInactiveSec = mkOption {
        type = types.str;
        default = "12h";
        description = "systemd timer OnUnitInactiveSec（距上次完成的间隔）。";
      };
      randomizedDelaySec = mkOption {
        type = types.str;
        default = "5m";
        description = "systemd timer RandomizedDelaySec（随机抖动）。";
      };
    };

    # 运行身份与兜底超时：user/group 由实例显式给出，须与输出目录属主一致
    service = {
      user = mkOption {
        type = types.str;
        description = "服务运行用户。";
      };
      group = mkOption {
        type = types.str;
        description = "服务运行组。";
      };
      umask = mkOption {
        type = types.str;
        default = "0022";
        description = "服务进程 umask。";
      };
      timeoutSec = mkOption {
        type = types.int;
        default = 300;
        description = "TimeoutStartSec 秒数（oneshot 默认 infinity，兜底防挂死）。";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.length (lib.unique (map (output: output.mode) cfg.outputs)) == lib.length cfg.outputs;
        message = "services.iptv-merge.outputs must have unique modes";
      }
      {
        assertion = lib.length (lib.unique (map (output: output.outputPath) cfg.outputs)) == lib.length cfg.outputs;
        message = "services.iptv-merge.outputs must have unique output paths";
      }
      {
        assertion = lib.all (output: output.mode != "multicast" || output.udpxy != null) cfg.outputs;
        message = "services.iptv-merge: multicast output requires a non-null udpxy address";
      }
    ];

    # ReadWritePaths 的目录须在 unit namespace 建立前存在，故声明式预建，不依赖手工残留。
    systemd.tmpfiles.rules = map
      (dir: "d ${dir} 0755 ${cfg.service.user} ${cfg.service.group} - -")
      (lib.unique (map (output: lib.dirOf output.outputPath) cfg.outputs));

    systemd.services = lib.listToAttrs (map (output: lib.nameValuePair unitNames.${output.mode} {
      description =
        if output.mode == "both"
        then "Merge IPTV unicast lists"
        else if output.mode == "multicast"
        then "Publish multicast IPTV list via udpxy (${if output.udpxy != null then output.udpxy else "none"})"
        else "Publish ${output.mode} IPTV unicast list";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = servicePath;
      environment = {
        IPTV_MODE = output.mode;
        IPTV_DST = output.outputPath;
      } // lib.optionalAttrs (output.udpxy != null) {
        IPTV_UDPXY = output.udpxy;
      };
      serviceConfig = mkServiceConfig output.outputPath;
    }) cfg.outputs);

    systemd.timers = lib.listToAttrs (map (output: lib.nameValuePair unitNames.${output.mode} {
      wantedBy = [ "timers.target" ];
      timerConfig = commonTimerConfig;
    }) cfg.outputs);
  };
}
