{ config, lib, pkgs, ... }:
# 广告列表同步模块：拉取与校验由 pkgs/adlist-sync 提供，本模块声明 options 与 systemd 单元。
# 发布目录、上游源与运行身份由服务实例传入。
let
  cfg = config.services.adlist-sync;
  inherit (lib) mkOption mkEnableOption types;

  pkg = pkgs.callPackage ../pkgs/adlist-sync {
    sources = cfg.sources;
    outputDir = cfg.outputDir;
    minLines = cfg.minLines;
  };
in
{
  options.services.adlist-sync = {
    # 开关：关掉时整个模块（包、服务、定时器）都不生效
    enable = mkEnableOption "广告过滤列表同步";

    # 上游列表源：name 即输出文件名（自动补 .txt），数量不限
    sources = mkOption {
      type = types.listOf (types.submodule {
        options = {
          name = mkOption {
            type = types.str;
            description = "输出文件名（自动补 .txt）。";
          };
          url = mkOption {
            type = types.str;
            description = "上游列表 URL。";
          };
        };
      });
      default = [];
      description = "上游广告过滤列表源（name|url）。";
    };

    # 输出目录：发布根目录（ReadWritePaths 直用本值），由实例传入
    outputDir = mkOption {
      type = types.str;
      example = "/var/lib/adlist";
      description = "输出目录（唯一可写目录 = ReadWritePaths，ProtectSystem=strict 下其余全只读）；勿放会被原子换版的目录下。";
    };

    # 拒收阈值：上游抽风返回空壳/错误页时保旧
    minLines = mkOption {
      type = types.int;
      default = 100;
      description = "非注释行数下限，低于则拒收保旧（防上游抽风空壳/错误页）。";
    };

    # 定时节奏：Persistent 与 OnCalendar 配对，关机错过触发点时开机自动补跑
    schedule = {
      onCalendar = mkOption {
        type = types.str;
        default = "*-*-01 03:00:00";
        description = "systemd timer OnCalendar（拉取频率：每月 1 日 03:00）。";
      };
      onBootSec = mkOption {
        type = types.str;
        default = "5min";
        description = "systemd timer OnBootSec（开机后首跑延迟）。";
      };
      randomizedDelaySec = mkOption {
        type = types.str;
        default = "2h";
        description = "systemd timer RandomizedDelaySec（随机抖动）。";
      };
      persistent = mkOption {
        type = types.bool;
        default = true;
        description = "Persistent：错过日历触发点（关机中）时开机补跑。";
      };
    };

    # 运行身份（用户/组由实例传入，须对输出目录可写）与兜底超时
    service = {
      user = mkOption {
        type = types.str;
        example = "adlist";
        description = "服务运行用户。";
      };
      group = mkOption {
        type = types.str;
        example = "adlist";
        description = "服务运行组。";
      };
      umask = mkOption {
        type = types.str;
        default = "0022";
        description = "服务进程 umask。";
      };
      timeoutSec = mkOption {
        type = types.int;
        default = 600;
        description = "TimeoutStartSec 秒数（oneshot 默认 infinity，兜底防挂死；多源下载留 10min）。";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    # 输出目录预建并交权给服务用户，tmpfiles 每次激活对齐属主/权限
    systemd.tmpfiles.rules = [
      "d ${cfg.outputDir} 0755 ${cfg.service.user} ${cfg.service.group} -"
    ];

    systemd.services.adlist-sync = {
      description = "Fetch and validate ad-filter lists";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [ pkgs.bash pkgs.curl pkgs.gnugrep pkgs.coreutils ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.service.user;
        Group = cfg.service.group;
        UMask = cfg.service.umask;
        ExecStart = "${pkg}/bin/adlist-sync";
        # 沙箱加固：仅 outputDir 可写，其余全只读
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.outputDir ];
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
    };

    systemd.timers.adlist-sync = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.schedule.onCalendar;
        Persistent = cfg.schedule.persistent;
        OnBootSec = cfg.schedule.onBootSec;
        RandomizedDelaySec = cfg.schedule.randomizedDelaySec;
      };
    };
  };
}
