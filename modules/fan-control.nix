# Supermicro IPMI 风扇调速模块（options 化）
#
# raw 命令格式（依据 STH: Supermicro X9/X10/X11 Fan Speed Control）：
#   ipmitool raw 0x30 0x70 0x66 0x01 0x<zone> 0x<duty>
#     zone 0x00 = CPU zone（FAN1/FAN2...），0x01 = Peripheral zone（FANA/FANB...）
#     duty 0x00-0x64 = 0-100%，所以百分号直接 printf '%02x'（不是 0x00-0xFF）
# 实测 X10 系（nix-media）与 H12SSL-i（nix-nas）都认这套命令。
#
# 需要每台机器在 imports 里加上本文件，然后用 services.fanControl.* 调。
# 脚本打包在 pkgs/fan-control/，本文件只管 options + systemd 单元声明。
#
{ config, lib, pkgs, ... }:

let
  cfg = config.services.fanControl;

  PATH' = lib.makeBinPath (with pkgs; [
    ipmitool
    hddtemp
    smartmontools
    gawk
    gnugrep
    coreutils
    config.boot.zfs.package
  ]);

  strings = lib.types.listOf lib.types.str;
  ints = lib.types.listOf lib.types.int;
  dutyList = lib.types.listOf (lib.types.ints.between 0 100);

  zoneDesc = name: ''
    ${name} 的 duty 档位（%）。必须与下面的 upTemps 一一对应、从小到大排列。
    最后一个档位是最高档。<duty,<temp>> 相邻两点之间是阶梯，不是插值。
  '';
in
{
  options.services.fanControl = {
    enable = lib.mkEnableOption "基于硬盘平均温度 + CPU 温度的 Supermicro IPMI 双区风扇调速";

    pools = lib.mkOption {
      type = strings;
      default = [];
      description = ''
        要跟踪的 zpool 名称列表。留空 = 自动发现本机全部池。
        nix-nas 只跟踪 Home 池（SSD 池是单块 NVMe，温度量级跟机械盘不一样）。
      '';
    };

    peripheral.duty = lib.mkOption { type = dutyList; default = [ 30 40 50 65 80 100 ]; description = zoneDesc "Peripheral zone"; };
    peripheral.upTemps = lib.mkOption { type = ints; default = [ 0 44 48 51 54 58 ]; description = ''
      对应 ${"`services.fanControl.peripheral.duty`"} 每一档的升档温度（°C，硬盘平均温度）。
      温度一越线立即升档；降档则要求温度低于「升档温度 - hysteresis」并连续 hold 个周期。
    ''; };

    cpu.duty = lib.mkOption { type = dutyList; default = [ 25 40 55 75 100 ]; description = zoneDesc "CPU zone"; };
    cpu.upTemps = lib.mkOption { type = ints; default = [ 0 55 62 70 78 ]; description = ''
      对应 ${"`services.fanControl.cpu.duty`"} 每一档的升档温度（°C，IPMI 的 CPU 温度）。
    ''; };

    hysteresis = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 3;
      description = "迟滞带宽（°C）。降档必须比升档低这么多才算数，用来防止风扇频繁起停。";
    };

    hold = lib.mkOption {
      type = lib.types.ints.positive;
      default = 3;
      description = "连续几个周期满足降档条件才真的降一档（周期长度见 interval）。";
    };

    diskHot = lib.mkOption {
      type = lib.types.int;
      default = 58;
      description = ''
        任意一块盘到这个温度，Peripheral zone 立刻满速（100%）。
        这是平均值之外的兜底：不会因为一块热点盘被平均掉就放着不管。
      '';
    };

    diskHotClear = lib.mkOption {
      type = lib.types.int;
      default = 56;
      description = "全部盘低于这个温度才解除上面的满速锁定，防止在阈值附近反复跳。";
    };

    failsafePeripheral = lib.mkOption {
      type = lib.types.ints.between 0 100;
      default = 65;
      description = "读不到任何硬盘温度时，Peripheral zone 采用的保守 duty（%）。";
    };

    failsafeCpu = lib.mkOption {
      type = lib.types.ints.between 0 100;
      default = 50;
      description = "读不到 CPU 温度时，CPU zone 采用的保守 duty（%）。";
    };

    cpuSensor = lib.mkOption {
      type = lib.types.str;
      default = "CPU Temp";
      description = "ipmitool sensor list 里 CPU 温度那一项的名字。";
    };

    setFullMode = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        开机时是否把 IPMI 风扇模式设成 Full。不设的话 BMC 会在几分钟内用自己的策略
        覆盖掉下面写的 duty cycle，所以正常都应该开着。
      '';
    };

    interval = lib.mkOption {
      type = lib.types.str;
      default = "2min";
      description = "调速检查周期（systemd 时间格式）。";
    };

    bootDelay = lib.mkOption {
      type = lib.types.str;
      default = "30s";
      description = "开机后多久跑第一次调速。";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = builtins.length cfg.peripheral.duty == builtins.length cfg.peripheral.upTemps
          && builtins.length cfg.cpu.duty == builtins.length cfg.cpu.upTemps;
        message = "fan-control: peripheral.duty/peripheral.upTemps 与 cpu.duty/cpu.upTemps 必须一一对应";
      }
      {
        assertion = builtins.length cfg.peripheral.duty >= 2 && builtins.length cfg.cpu.duty >= 2;
        message = "fan-control: 每个 zone 至少要两档（末档就是最高转速）";
      }
      {
        assertion = builtins.all (xs: xs == builtins.sort builtins.lessThan xs) [
          cfg.peripheral.duty
          cfg.peripheral.upTemps
          cfg.cpu.duty
          cfg.cpu.upTemps
        ];
        message = "fan-control: duty 与 upTemps 都必须从小到大排列";
      }
      {
        assertion = cfg.diskHotClear < cfg.diskHot;
        message = "fan-control: diskHotClear 必须小于 diskHot，否则满速锁定解不开";
      }
    ];

    # ── 开机后锁定 Full 模式，防止 BMC 覆盖手动 duty cycle ──
    # 只能在开机设一次：运行中重设 Full 会让 BMC 先把风扇拉到 100%，
    # 和下面的 duty 控制打架。
    systemd.services.fan-mode-init = lib.mkIf cfg.setFullMode {
      description = "Set IPMI fan mode to Full so BMC won't override duty cycle";
      after = [ "local-fs.target" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${pkgs.ipmitool}/bin/ipmitool raw 0x30 0x45 0x01 0x01";
      };
    };

    # ── 周期性调速：CPU zone 与 Peripheral zone 各自独立调档 ──
    # 脚本打包在 pkgs/fan-control/，本处只声明 systemd 单元。
    systemd.services.fan-control = {
      description = "IPMI dual-zone fan duty cycle control (zpool HDD average + CPU)";
      # fan-mode-init 只在 setFullMode 时存在（上面 mkIf），after/requires 同步条件化，
      # 否则 setFullMode=false 时 Requires 找不到单元、本服务直接被拒启动。
      after = lib.optional cfg.setFullMode "fan-mode-init.service";
      requires = lib.optional cfg.setFullMode "fan-mode-init.service";
      serviceConfig = {
        Type = "oneshot";
        # 状态文件放 RuntimeDirectory（/run/fan-control，root 0700、开机清空）：
        # 不能再放 /tmp —— 那里全局可写且 nspawn 容器与宿主共享，任意本地进程
        # 预写文件就会被本服务（root）source，等于本地提权。/run 与 /tmp 同为
        # tmpfs 开机清空，不影响「开机 applied 初值=100」的假设。
        RuntimeDirectory = "fan-control";
        RuntimeDirectoryMode = "0700";
        Environment = "PATH=${PATH'}";
        ExecStart = pkgs.callPackage ../pkgs/fan-control {
          zfs = config.boot.zfs.package;
          pools = cfg.pools;
          peripheralDuty = cfg.peripheral.duty;
          peripheralUpTemps = cfg.peripheral.upTemps;
          cpuDuty = cfg.cpu.duty;
          cpuUpTemps = cfg.cpu.upTemps;
          inherit (cfg) hysteresis hold diskHot diskHotClear failsafePeripheral failsafeCpu cpuSensor;
        };
      };
    };

    systemd.timers.fan-control = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = cfg.bootDelay;
        OnUnitActiveSec = cfg.interval;
        AccuracySec = "5s";
        Persistent = true;
      };
    };
  };
}
