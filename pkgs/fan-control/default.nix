# Supermicro IPMI 风扇调速脚本：把 fan-control.sh 模板按 services.fanControl.* 参数替换后打包。
# 由 modules/fan-control.nix callPackage 调用；raw 命令 0x30 0x70 0x66 0x01 <zone> <duty>，
# zone 0x00=CPU、0x01=Peripheral，duty 为 0-100 百分数（printf '%02x' 写入）。
{ lib, writeShellScript
, ipmitool, hddtemp, smartmontools, gawk, gnugrep, zfs
# ── 以下参数由 modules/fan-control.nix 传入 ──
, pools
, peripheralDuty, peripheralUpTemps
, cpuDuty, cpuUpTemps
, hysteresis, hold, diskHot, diskHotClear
, failsafePeripheral, failsafeCpu, cpuSensor
}:

let
  ipmi' = "${ipmitool}/bin/ipmitool";
  hddtemp' = "${hddtemp}/bin/hddtemp";
  smartctl' = "${smartmontools}/bin/smartctl";
  zpool' = "${zfs}/bin/zpool";
  awk' = "${gawk}/bin/awk";
  grep' = "${gnugrep}/bin/grep";
  nlist = xs: lib.concatMapStringsSep " " toString xs;

  # fan-control.sh 中的 @...@ 是参数替换位，替换后即为最终安装的脚本内容。
  script = builtins.replaceStrings
    [
      "@IPMI@"
      "@HDDTEMP@"
      "@SMARTCTL@"
      "@ZPOOL@"
      "@AWK@"
      "@GREP@"
      "@POOLS@"
      "@PERI_DUTY@"
      "@PERI_UP@"
      "@CPU_DUTY@"
      "@CPU_UP@"
      "@HYST@"
      "@HOLD@"
      "@HDD_HOT@"
      "@HDD_HOT_CLEAR@"
      "@FAILSAFE_PERI@"
      "@FAILSAFE_CPU@"
      "@CPU_SENSOR@"
    ]
    [
      ipmi'
      hddtemp'
      smartctl'
      zpool'
      awk'
      grep'
      (lib.concatStringsSep " " pools)
      (nlist peripheralDuty)
      (nlist peripheralUpTemps)
      (nlist cpuDuty)
      (nlist cpuUpTemps)
      (toString hysteresis)
      (toString hold)
      (toString diskHot)
      (toString diskHotClear)
      (toString failsafePeripheral)
      (toString failsafeCpu)
      cpuSensor
    ]
    (builtins.readFile ./fan-control.sh);
in
writeShellScript "fan-control.sh" script
