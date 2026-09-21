# Supermicro IPMI 风扇调速脚本（打包为 Nix derivation）
#
# 由 services/media/fan-control.nix 模块调用，传入 services.fanControl.* 参数。
# 两台机器（nix-media / nix-nas）共用同一份脚本，只是参数不同。
#
# raw 命令格式（依据 STH: Supermicro X9/X10/X11 Fan Speed Control）：
#   ipmitool raw 0x30 0x70 0x66 0x01 0x<zone> 0x<duty>
#     zone 0x00 = CPU zone（FAN1/FAN2...），0x01 = Peripheral zone（FANA/FANB...）
#     duty 0x00-0x64 = 0-100%，所以百分号直接 printf '%02x'（不是 0x00-0xFF）
# 实测 X10 系（nix-media）与 H12SSL-i（nix-nas）都认这套命令。
#
{ lib, writeShellScript
, ipmitool, hddtemp, smartmontools, gawk, gnugrep, coreutils, zfs
# ── 以下参数由 services/media/fan-control.nix 模块传入 ──
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
in
writeShellScript "fan-control.sh" ''
  set -uo pipefail

  IPMI="${ipmi'}"
  HDDTEMP="${hddtemp'}"
  SMARTCTL="${smartctl'}"
  ZPOOL="${zpool'}"
  AWK="${awk'}"
  GREP="${grep'}"

  state_file="''${RUNTIME_DIRECTORY:-/run/fan-control}/fan-control-state"

  # ───────── 来自 services.fanControl.* 的调节参数 ─────────
  POOLS="${lib.concatStringsSep " " pools}"
  PERI_DUTY=(${nlist peripheralDuty})
  PERI_UP=(   ${nlist peripheralUpTemps})
  CPU_DUTY=(  ${nlist cpuDuty})
  CPU_UP=(    ${nlist cpuUpTemps})
  HYST=${toString hysteresis}
  HOLD=${toString hold}
  HDD_HOT=${toString diskHot}
  HDD_HOT_CLEAR=${toString diskHotClear}
  FAILSAFE_PERI=${toString failsafePeripheral}
  FAILSAFE_CPU=${toString failsafeCpu}
  CPU_SENSOR="${cpuSensor}"

  # ───────── 上一次的状态 ─────────
  # 开机时 fan-mode-init 已经把模式设成 Full（=100%），所以 applied 初值
  # 写成 100，避免首周期误判「已经生效」而漏下发。
  peri_idx=0; peri_down=0; peri_applied=100
  cpu_idx=0;  cpu_down=0;  cpu_applied=100
  hot=0
  [[ -f "$state_file" ]] && source "$state_file"

  # ───────── 从 zpool 里找出所有数据盘 ─────────
  # POOLS 留空就是自动发现本机全部池；否则只用点名的池。
  # zpool status -LP 会把 cache / log / spare 段一并打出来，用 awk 跳过；
  # 再去掉分区号得到整块盘 —— 换盘、换槽位、加减盘都不用改脚本。
  collect_disks() {
    local pool_list pool dev out=""
    if [[ -n "$POOLS" ]]; then
      pool_list="$POOLS"
    else
      pool_list=$($ZPOOL list -H -o name 2>/dev/null || true)
    fi
    for pool in $pool_list; do
      $ZPOOL list "$pool" >/dev/null 2>&1 || continue
      while read -r dev; do
        [[ $dev == /dev/* ]] || continue
        if   [[ $dev =~ ^/dev/(sd[a-z]+)[0-9]*$ ]]; then dev=/dev/''${BASH_REMATCH[1]}
        elif [[ $dev =~ ^(/dev/nvme[0-9]+n[0-9]+)(p[0-9]+)?$ ]]; then dev=''${BASH_REMATCH[1]}
        fi
        [[ -b $dev ]] || continue
        case " $out " in *" $dev "*) continue ;; esac
        out="$out $dev"
      done < <($ZPOOL status -LP "$pool" 2>/dev/null | $AWK '
        NF==1 && $1 ~ /^(cache|logs|spares|special|dedup)$/ { skip = 1; next }
        $1 ~ /^\// && !skip { print $1 }')
    done
    printf '%s' "''${out# }"
  }

  # ───────── 带迟滞的档位决策 ─────────
  # 升档立即生效，降档必须连续 HOLD 个周期都低于「升档温度 - HYST」才降一档。
  # 这条不对称规则是消除风扇频繁起停的关键：老脚本每次都用瞬时温度重算
  # duty，±1°C 的抖动就足以让它在两档之间反复跳。
  # 输出 "<duty> <idx> <down>"，由调用方用 read 接回去（不能用 $()：
  # 命令替换在子 shell 里跑，改不回父 shell 的档位状态）。
  decide() {
    local -n _idx=$1 _down=$2 _duty=$3 _up=$4
    local t=$5 top i target=$_idx
    top=$(( ''${#_duty[@]} - 1 ))
    if (( t < 0 )); then
      printf '%s %s %s\n' "''${_duty[$_idx]}" "$_idx" "$_down"
      return
    fi
    for (( i = top; i > _idx; i-- )); do
      if (( t >= _up[i] )); then target=$i; break; fi
    done
    if (( target > _idx )); then
      _idx=$target; _down=0
    elif (( _idx > 0 && t < _up[_idx] - HYST )); then
      _down=$(( _down + 1 ))
      if (( _down >= HOLD )); then _idx=$(( _idx - 1 )); _down=0; fi
    else
      _down=0
    fi
    printf '%s %s %s\n' "''${_duty[$_idx]}" "$_idx" "$_down"
  }

  # ───────── 采样：硬盘（zpool 数据盘）+ CPU ─────────
  disks=($(collect_disks))
  temps=(); failed=0
  for d in "''${disks[@]}"; do
    # hddtemp 优先（快、不用解析）；万一换了新盘不在 hddtemp 的型号库里，
    # 退回 smartctl。-n standby 保证不会把睡着的盘叫醒。
    t=$($HDDTEMP -q -n "$d" 2>/dev/null | head -n1) || t=""
    t=''${t//[^0-9]/}
    if [[ -z $t ]]; then
      t=$($SMARTCTL -A -n standby "$d" 2>/dev/null | $AWK '
        /Temperature_Celsius/                 { print $10; exit }
        /^Temperature:[[:space:]]+[0-9]+/     { print $2;  exit }')
      t=''${t//[^0-9]/}
    fi
    if [[ -n $t ]]; then
      t=$((10#$t))
      if (( t >= 0 && t <= 100 )); then temps+=("$t"); else failed=$((failed + 1)); fi
    else
      failed=$((failed + 1))
    fi
  done

  n=''${#temps[@]}
  if (( n > 0 )); then
    sum=0; disk_max=0
    for t in "''${temps[@]}"; do
      sum=$((sum + t))
      (( t > disk_max )) && disk_max=$t
    done
    disk_avg=$(( (sum + n / 2) / n ))   # 四舍五入的平均值（整数运算）
  else
    disk_avg=-1; disk_max=-1
  fi

  # CPU 温度：ipmitool 给的是 "CPU Temp | 45.000 | degrees C | ..."，
  # 老版本用 ^[0-9]+$ 校验整数字面，永远匹配不上小数，导致 CPU zone
  # 从来没被真正控制过。这里按管道分字段后截掉小数位。
  cpu_temp=-1
  line=$($IPMI sensor list 2>/dev/null | $GREP -m1 "^$CPU_SENSOR" || true)
  if [[ -n $line ]]; then
    val=''${line#*|}
    val=''${val%%|*}
    val=''${val//[!0-9.]/}
    val=''${val%%.*}
    [[ -n $val ]] && cpu_temp=$((10#$val))
  fi

  # 读不到任何温度时不能就此退出：否则下面的 FAILSAFE 兜底逻辑根本走不到，
  # 风扇会停在之前的低转速。继续走决策 —— 两个 zone 都会落到 FAILSAFE_* 保守
  # 转速并照常下发。
  if (( disk_avg < 0 && cpu_temp < 0 )); then
    echo "警告：读不到任何温度传感器，两个 zone 按保守转速(FAILSAFE)下发" >&2
  fi

  # ───────── 决策 ─────────
  # Peripheral zone 由全池平均值驱动（十块盘的平均比单块稳得多），
  # 但保留单盘过热兜底：任一块到 HDD_HOT 就满速，全部回到 HDD_HOT_CLEAR
  # 以下才解锁 —— 既不会漏掉单独的热点盘，也不会被它长期锁死在满速。
  if (( disk_avg >= 0 )); then
    read -r peri_duty peri_idx peri_down < <(decide peri_idx peri_down PERI_DUTY PERI_UP "$disk_avg")
  else
    peri_duty=$FAILSAFE_PERI
  fi

  if (( cpu_temp >= 0 )); then
    read -r cpu_duty cpu_idx cpu_down < <(decide cpu_idx cpu_down CPU_DUTY CPU_UP "$cpu_temp")
  else
    cpu_duty=$FAILSAFE_CPU
  fi

  note=""
  (( disk_max >= HDD_HOT )) && hot=1
  (( disk_max >= 0 && disk_max < HDD_HOT_CLEAR )) && hot=0
  if (( hot )); then peri_duty=100; note=" [HOT]"; fi

  # ───────── 下发（只在 duty 真的变了时才写 BMC）─────────
  # 只有 IPMI 写成功才把 *_applied 记成目标值；写失败保留旧值 ——
  # 这样下个周期 duty != applied 依旧成立，会自动重试，而不是误以为已生效。
  apply_err=""
  if (( peri_duty != peri_applied )); then
    if $IPMI raw 0x30 0x70 0x66 0x01 0x01 "$(printf '0x%02x' "$peri_duty")" >/dev/null; then
      peri_applied=$peri_duty
    else
      apply_err="$apply_err Peripheral(目标:$peri_duty%)"
    fi
  fi
  if (( cpu_duty != cpu_applied )); then
    if $IPMI raw 0x30 0x70 0x66 0x01 0x00 "$(printf '0x%02x' "$cpu_duty")" >/dev/null; then
      cpu_applied=$cpu_duty
    else
      apply_err="$apply_err CPU(目标:$cpu_duty%)"
    fi
  fi
  [[ -n $apply_err ]] && echo "IPMI 调速失败:$apply_err，保留重试条件" >&2

  printf 'peri_idx=%s\nperi_down=%s\nperi_applied=%s\ncpu_idx=%s\ncpu_down=%s\ncpu_applied=%s\nhot=%s\n' \
    "$peri_idx" "$peri_down" "$peri_applied" "$cpu_idx" "$cpu_down" "$cpu_applied" "$hot" > "$state_file"

  if (( n > 0 )); then hdd="HDD avg=$disk_avg°C max=$disk_max°C n=$n"
  else hdd="HDD:N/A"; fi
  (( failed > 0 )) && hdd="$hdd (读不到:$failed)"
  c="CPU=$cpu_temp°C"; (( cpu_temp < 0 )) && c="CPU:N/A"
  echo "$hdd $c → Peripheral:$peri_duty% CPU:$cpu_duty%$note"
''
