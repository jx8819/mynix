# 拉取、校验、过滤并按 mode 合并 IPTV 源；失败时原子保留旧产物。
{ lib
, writeShellScriptBin
, telecomUrl ? ""
, mobileUrl ? ""
, telecomMinChannels ? 1
, telecomRequirePattern ? "."
, telecomBlacklist ? []    # listOf str；作 awk 动态正则
, telecomDropGroups ? []   # listOf str；作 group-title 成员精确匹配串
, mobileMinChannels ? 1
, mobileRequirePattern ? "."
, mobileBlacklist ? []
, mobileDropGroups ? []
, mergeStrategy ? "smart"  # "smart" | "exact" | "off"
, multicastUrl ? ""
, multicastMinChannels ? 1
, multicastRequirePattern ? "."
, multicastBlacklist ? []
, multicastDropGroups ? []
, sportsUrl ? ""
, sportsMinChannels ? 1
, sportsRequirePattern ? "."
, sportsBlacklist ? []
, sportsDropGroups ? []
, telecomSourceLabel ? "telecom"
, mobileSourceLabel ? "mobile"
, multicastSourceLabel ? "multicast"
, sportsSourceLabel ? "sports"
}:
writeShellScriptBin "iptv-merge" ''
  # 五种 mode（由各 systemd 单元通过 IPTV_MODE 选择，可同时发布不同模式）:
  #   - "both"         第一源优先全量（原序）+ 第二源过滤后补第一源没有的台（原序）
  #   - "telecom-only" 只拉取第一源
  #   - "mobile-only"  只拉取第二源
  #   - "multicast"    使用组播订阅源，可添加 udpxy 地址
  #   - "fcc"          复用组播过滤，主源 URL 转为 rtp2httpd RTP + FCC，可选补充移动源
  # 每种模式均可用 IPTV_SPORTS=1 追加独立体育源；不参与基础源的去重或 URL 转换。
  # 失败语义（同目录 mktemp+mv 原子替换、trap 双清临时件，任一失败 DST 保旧）:
  #   - 电信阶段（下载/校验/过滤）失败 → 整体 abort
  #   - "both" / 启用移动补充的 "fcc"：移动阶段失败 → stderr 打 WARN、跳过移动、仍发布主源
  #   - "mobile-only" 阶段失败 → 整体 abort
  #   - 启用体育补充：下载/校验/过滤失败 → 整体 abort，禁止提前发布基础源
  #
  # 单元必须设置 IPTV_MODE/IPTV_DST；IPTV_SRC / IPTV_SRC_MOBILE / IPTV_SRC_MULTICAST / IPTV_SRC_SPORTS 可覆盖源 URL。
  #
  # 假设【契约】: 黑名单与 dropGroups 元素均为字面文本，不含 "|" 且不含单引号——
  #   包内 lib.concatStringsSep "|" 拼接后分别作 awk 动态正则（bl）与 split 成员匹配串（gs），
  #   元素含 | 会破坏拼接边界；元素含单引号会提前终止脚本所在的 Nix 缩进字符串。
  #   空黑名单编译成不匹配任何非空频道名的 ^$；非空黑名单按原有规则拼接。
  set -euo pipefail

  # ---- 单元运行参数 + 可由 IPTV_SRC* 覆盖的源与过滤配置 ----
  MODE="''${IPTV_MODE:?IPTV_MODE must be set}"
  DST="''${IPTV_DST:?IPTV_DST must be set}"
  SRC="''${IPTV_SRC:-${telecomUrl}}"
  SRC_M="''${IPTV_SRC_MOBILE:-${mobileUrl}}"
  SRC_MC="''${IPTV_SRC_MULTICAST:-${multicastUrl}}"
  SRC_SP="''${IPTV_SRC_SPORTS:-${sportsUrl}}"
  SPORTS="''${IPTV_SPORTS-0}"
  case "$SPORTS" in
    0|1) ;;
    *) echo 'iptv-merge: IPTV_SPORTS must be 0 or 1' >&2; exit 1 ;;
  esac
  UDPXY="''${IPTV_UDPXY:-}"
  if [ "$MODE" = "fcc" ]; then
    FCC_GATEWAY="''${IPTV_FCC_GATEWAY:-}"
    FCC_ADDRESS="''${IPTV_FCC_ADDRESS:-}"
    FCC_PORT="''${IPTV_FCC_PORT:-}"
    FCC_PROTOCOL="''${IPTV_FCC_PROTOCOL:-telecom}"
    FCC_INCLUDE_MOBILE="''${IPTV_FCC_INCLUDE_MOBILE-0}"
    case "$FCC_INCLUDE_MOBILE" in
      0|1) ;;
      *) echo 'iptv-merge: IPTV_FCC_INCLUDE_MOBILE must be 0 or 1' >&2; exit 1 ;;
    esac
    fcc_valid_port() {
      [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 ))
    }
    [[ "$FCC_GATEWAY" =~ ^[A-Za-z0-9._-]+:[0-9]{1,5}$ ]] &&
      fcc_valid_port "''${FCC_GATEWAY##*:}" ||
      { echo 'iptv-merge: fcc requires IPTV_FCC_GATEWAY as host:port' >&2; exit 1; }
    [[ "$FCC_ADDRESS" =~ ^[A-Za-z0-9._-]+$ ]] ||
      { echo 'iptv-merge: fcc requires IPTV_FCC_ADDRESS as a host or IPv4 address' >&2; exit 1; }
    fcc_valid_port "$FCC_PORT" ||
      { echo 'iptv-merge: fcc requires IPTV_FCC_PORT in 1..65535' >&2; exit 1; }
    case "$FCC_PROTOCOL" in
      telecom|huawei) ;;
      *) echo 'iptv-merge: IPTV_FCC_PROTOCOL must be telecom or huawei' >&2; exit 1 ;;
    esac
    # 仅 fcc 改用其专属网关请求源；旧模式的 IPTV_UDPXY/query 路径不变。
    UDPXY="$FCC_GATEWAY"
  fi
  if [ -n "$UDPXY" ]; then
    if [[ "$SRC_MC" == *"?"* ]]; then
      SRC_MC="''${SRC_MC}&udpxy=''${UDPXY}"
    else
      SRC_MC="''${SRC_MC}?udpxy=''${UDPXY}"
    fi
  fi
  # 第一源参数
  TC_MIN="${toString telecomMinChannels}"
  TC_REQ="${telecomRequirePattern}"
  TC_BL="${if telecomBlacklist == [] then "^$" else lib.concatStringsSep "|" telecomBlacklist}"
  TC_GS="${lib.concatStringsSep "|" telecomDropGroups}"
  # 第二源参数
  MO_MIN="${toString mobileMinChannels}"
  MO_REQ="${mobileRequirePattern}"
  MO_BL="${if mobileBlacklist == [] then "^$" else lib.concatStringsSep "|" mobileBlacklist}"
  # 第二源 bash 小写化（fold=1 判定侧双保险: 中文无影响，ASCII 模式防大小写漏网）
  MO_BL_LC="''${MO_BL,,}"
  MO_GS="${lib.concatStringsSep "|" mobileDropGroups}"
  # 组播源参数
  MC_MIN="${toString multicastMinChannels}"
  MC_REQ="${multicastRequirePattern}"
  MC_BL="${if multicastBlacklist == [] then "^$" else lib.concatStringsSep "|" multicastBlacklist}"
  MC_GS="${lib.concatStringsSep "|" multicastDropGroups}"
  # 可选体育补充源参数
  SP_MIN="${toString sportsMinChannels}"
  SP_REQ="${sportsRequirePattern}"
  SP_BL="${if sportsBlacklist == [] then "^$" else lib.concatStringsSep "|" sportsBlacklist}"
  SP_GS="${lib.concatStringsSep "|" sportsDropGroups}"
  STRATEGY="${mergeStrategy}"

  # ---- 模式合法性先于任何文件动作（未知模式 fail-fast，不碰 DST 目录）----
  case "$MODE" in
    telecom-only|mobile-only|both|multicast|fcc) ;;
    *) echo "iptv-merge: unknown mode: $MODE" >&2; exit 1 ;;
  esac

  # ---- 同目录临时件 + trap 双清（失败/信号一律清临时件、DST 保旧；空串参数 rm -f 静默跳过）----
  TMP=$(mktemp "''${DST%/*}/.iptv-unicast.XXXXXX")
  FILT=""; STAT=""; TMPM=""; SANM=""; FILTM=""; STAT_M=""; STAT_MG=""; MRG=""
  TMPMC=""; FILTMC=""; STAT_MC=""
  TMPSP=""; SANSP=""; FILTSP=""; STAT_SP=""
  trap 'rm -f "$TMP" "$TMP.hdr" "$FILT" "$STAT" "$TMPM" "$SANM" "$FILTM" "$FILTM.hdr" "$STAT_M" "$STAT_MG" "$MRG" "$MRG.hdr" "$TMPMC" "$TMPMC.hdr" "$FILTMC" "$STAT_MC" "$TMPSP" "$SANSP" "$FILTSP" "$STAT_SP"' EXIT
  trap 'rm -f "$TMP" "$TMP.hdr" "$FILT" "$STAT" "$TMPM" "$SANM" "$FILTM" "$FILTM.hdr" "$STAT_M" "$STAT_MG" "$MRG" "$MRG.hdr" "$TMPMC" "$TMPMC.hdr" "$FILTMC" "$STAT_MC" "$TMPSP" "$SANSP" "$FILTSP" "$STAT_SP"; exit 1' HUP INT TERM

  # v3-filter-begin —— 以下至 v3-filter-end 为底本 v3 过滤阶段（测试提取锚点，勿删）
  # 2.5) awk 单趟过滤：主规则只分类采集，判决与统计集中在 END 一趟完成（不回读文件、原序可回放）
  filter_stage() {
    # $1=输入（已过校验门的源文件） $2=输出（头部+存活对） $3=统计文件
    # $4=bl 黑名单 |-join 正则串  $5=gs dropGroups |-join 成员串  $6=fold（0=原字面 / 1=tolower 折叠）
    # $7=additive（1=仅结构校验/名字黑名单/组剔除，保持全部幸存对；0=基础源原有规则）
    awk -v stats="$3" -v bl="$4" -v gs="$5" -v fold="$6" -v additive="$7" '
      # ---- 采集段：每行只登记，不做任何删除决定（保证输出顺序=输入顺序）----
      NR == 1 {
        # 首行必须是 #EXTM3U；头部单独留档，输出时原样打头（头原样纪律）
        if ($0 !~ /^#EXTM3U/) { print "filter: first line not #EXTM3U" > "/dev/stderr"; err = 1; exit 1 }
        hdr = $0
        next
      }
      /^#EXTINF/ {
        # EXTINF 必须与下一行 URL 成对：还挂着上一条未配对 = 上一对缺 URL
        if (pend) { print "filter: EXTINF line " pe " has no URL" > "/dev/stderr"; err = 1; exit 1 }
        pend = 1
        pe = NR
        ext = $0
        next
      }
      {
        # 走到这必是「正挂着的 URL 行」；否则行型未知（只删不改：未知行型宁可报错也不静默透传）
        if (!pend) { print "filter: unexpected line " NR " (URL/comment without EXTINF)" > "/dev/stderr"; err = 1; exit 1 }
        # URL 落位行必须非空且不以 # 开头【审查 M3】：空行/EXTVLCOPT 型注释伪装 URL 曾静默成对
        #   （EXTINF/空行/EXTINF2/URL2 与 尾部 EXTINF/空行 两条坏路径都直通 sed/chmod/mv → 拒跑保旧）
        if ($0 == "" || $0 ~ /^#/) { print "filter: line " NR " after EXTINF is not a URL" > "/dev/stderr"; err = 1; exit 1 }
        n++
        line_ext[n] = ext
        line_url[n] = $0
        # 显示名 = 最后一个逗号之后（^.*, 左最右长，贪婪吃到最右逗号；属性区含逗号也不怕）
        disp[n] = ext
        # sub 返回次数=0（EXTINF 无逗号）或切出空名（尾逗号）= 显示名定义失效【审查 S7】，
        #   与无逗号同级按结构错误拒跑（不猜、不跳、不入组）
        if (sub(/^.*,/, "", disp[n]) == 0 || disp[n] == "") { print "filter: EXTINF line " pe " has invalid display name" > "/dev/stderr"; err = 1; exit 1 }
        # group-title="…" 字段（无则空串），供分组剔除规则使用。
        gt = ext
        if (gt ~ /group-title="/) { sub(/^.*group-title="/, "", gt); sub(/".*$/, "", gt); gname[n] = gt }
        else gname[n] = ""
        pend = 0
      }
      END {
        # 主规则 exit 会先落进 END——err 闸门保证错误路径零输出、零统计（shell 侧读空 stats 必败 → 保旧）
        if (err) exit 1
        # 文件尾 EXTINF 缺 URL（成对性最后一道闸）
        if (pend) { print "filter: trailing EXTINF line " pe " has no URL" > "/dev/stderr"; exit 1 }

        # ---- 终裁P3 前置：全局同名坍缩（先于 R3 与组员计数；文件序留首、后续整对丢弃、c_dp3++）----
        #   否则重复条目会经组员计数误触整组剔除并产生错误统计。
        for (i = 1; !additive && i <= n; i++) {
          if (drop[i]) continue
          if (cdup[disp[i]]++) { drop[i] = 1; c_dp3++ }
        }

        # ---- R3+R4 名字/组硬剔（只对坍缩幸存者；已 drop 者跳过防重复计数）----
        # ---- R3 名字黑名单硬剔（fold=0 按显示名原字面匹配；fold=1 折叠小写后匹配）----
        # ---- R4 分组剔除（group-title 精确 ∈ gs 成员集）；独立计数 c_group ----
        nga = split(gs, ga, "|")
        for (i = 1; i <= n; i++) {
          if (drop[i]) continue
          if ((fold ? tolower(disp[i]) : disp[i]) ~ bl) { drop[i] = 1; c_black++; continue }
          if (gname[i] != "") {
            for (k = 1; k <= nga; k++) {
              if (gname[i] == ga[k]) { drop[i] = 1; c_group++; break }
            }
          }
        }

        # ---- 基名分组索引：仅黑名单幸存者入组（黑名单先行的字面顺序在这里落地）----
        for (i = 1; !additive && i <= n; i++) {
          if (drop[i]) continue
          b = tolower(disp[i])
          # 变体标记剥离 i 语义 = 先 tolower 再剥小写表；长词优先：超高清 必须先于 高清（前者包含后者，
          #   反序会残留「超」把同组兄弟拆散）；hd/4k 同理剥净（名字中间的 HD/4K 属标记位也剥，字面如此）
          gsub(/超高清/, "", b)
          gsub(/高清/, "", b)
          gsub(/标清/, "", b)
          gsub(/超清/, "", b)
          gsub(/hd/, "", b)
          gsub(/4k/, "", b)
          grp[i] = b
          gcount[b]++
          # 组代表名存原样首见者（WARN 打印原名，不打印小写残基）
          if (!(b in grepr)) grepr[b] = disp[i]
        }

        # ---- 预扫描 keep 集资格：多员组中命中 /高清|4k/i 者为候选（决策点1 高清与4K并存皆命中）----
        #   「零标记」定义【审查 S4 终裁锁定】:= 组内无任一成员命中 R1 同一谓词（ghas 与 cand 同源）——
        #   若改按剥离表读法，未来 {X标清,X} 型组会绕过决策点2 落入 R1 静默清空、无 WARN 不计 block
        #   实现注：awk 正则字面量没有 /i 旗标（写了会被当成变量拼接，静默永不命中——已实测踩坑），
        #   i 语义改由 tolower 实现，与剥离表同一套小写化，命中面等价 /高清|4k/i
        for (i = 1; !additive && i <= n; i++) {
          if (drop[i]) continue
          if (gcount[grp[i]] > 1 && tolower(disp[i]) ~ /高清|4k|hd/) { cand[i] = 1; ghas[grp[i]] = 1 }
        }

        # ---- 逐条判决（R2 → 决策点2 → R1 → 决策点3），三计数各归各档、互不串账 ----
        for (i = 1; i <= n; i++) {
          if (drop[i]) continue
          b = grp[i]
          if (additive || gcount[b] == 1) {
            keep[i] = 1
            # 体育补充保持全部幸存对；基础源 R2：组员==1 独一无二保留（无标记也留）
          } else if (!ghas[b]) {
            drop[i] = 1
            # 决策点2：多员组零标记 → 字面全否；block 按组计数，成员不入 sibling；
            # 计数与 WARN 都锁在「首见该组」的闸门里（否则每个成员重复报一次组）
            if (!(b in bseen)) {
              bseen[b] = 1
              c_block++
              # WARN 明细走 stderr（service 的 journal 同收）
              print "WARN: zero-mark group blocked: " grepr[b] " members=" gcount[b] > "/dev/stderr"
            }
          } else if (!cand[i]) {
            drop[i] = 1
            c_sib++
            # R1：组内未命中标记 → 被兄弟挤掉（sibling 档）
          } else {
            keep[i] = 1
            # 进 keep 集（决策点1：命中者皆留；决策点3 已前置，此处不再裁决）
          }
        }

        # ---- 输出段：头部原样 + 存活对成对打印，顺序=输入顺序（只删不改）----
        for (i = 1; i <= n; i++) if (keep[i]) alive++
        # 全灭护栏：0 存活 = 过滤逻辑或上游形态已崩，宁可失败保旧也不产出空列表
        if (alive == 0) { print "filter: 0 survivors, refuse to rewrite" > "/dev/stderr"; exit 1 }
        print hdr
        for (i = 1; i <= n; i++) if (keep[i]) { print line_ext[i]; print line_url[i] }

        # ---- 统计段：固定单行落 stats 文件（shell 按前缀解析），错误路径到不了这里 ----
        printf "blacklist=%d sibling=%d block=%d dp3=%d group=%d\n", c_black, c_sib, c_block, c_dp3, c_group > stats
      }
    ' "$1" > "$2"
  }
  # v3-filter-end（四个源共用结构/黑名单/组规则；体育不启用基础源去重，统计格式不变）

  # ---- 第一源阶段（telecom-only / both 共用；任一失败整体 abort 保旧）----
  telecom_stage() {
    # 1) 下载到同目录临时文件（失败不改动 DST）。
    curl -fsSL --connect-timeout 10 --max-time 120 --retry 3 --retry-delay 10 "$SRC" -o "$TMP"

    # 2) 校验：首行 #EXTM3U、#EXTINF >= TC_MIN、含 requirePattern，失败不改动 DST。
    head -n1 "$TMP" | grep -q '^#EXTM3U' || { echo 'bad header' >&2; exit 1; }
    [ "$(grep -c '^#EXTINF' "$TMP")" -ge "$TC_MIN" ] || { echo 'too few channels' >&2; exit 1; }
    grep -qE "$TC_REQ" "$TMP" || { echo 'no rtsp url' >&2; exit 1; }

    FILT=$(mktemp "''${DST%/*}/.iptv-filtered.XXXXXX")
    STAT=$(mktemp "''${DST%/*}/.iptv-stats.XXXXXX")
    filter_stage "$TMP" "$FILT" "$STAT" "$TC_BL" "$TC_GS" 0 0  # 非零退出 → set -e 断 → trap 清临时件 → DST 保旧不 mv
    # 过滤阶段禁管道、禁 ||true/条件包裹【审查 S3】：awk 直重定向，set -e 直达、pipefail/SIGPIPE 零暴露
    # 读五计数（stats 行格式固定: blacklist=N sibling=N block=N dp3=N group=N）
    # 单位【审查 S1】: blacklist/sibling/dp3=条数、block=组数 → block>0 时各数之和≠删除条数（被否组成员不入按条桶）
    read -r f_blk f_sib f_blo f_dpb f_grp < "$STAT"
    n_black=''${f_blk#blacklist=}
    n_sib=''${f_sib#sibling=}
    n_block=''${f_blo#block=}
    n_dp3=''${f_dpb#dp3=}
    n_grp=''${f_grp#group=}
    rm -f "$STAT"
    STAT=""                                   # 已读走即清（trap 兜底残量）
    mv -f "$FILT" "$TMP"
    FILT=""                                   # 过滤结果接管 $TMP → 发布段读取本文件
  }

  # ---- 移动阶段（mobile-only 直跑；both / fcc 补充在受控子壳里跑，失败降级为主源）----
  # 阶段内任何失败 = 本阶段失败: mobile-only → set -e 整体 abort 保旧；补充路径 → 父壳 WARN 降级发布
  mobile_stage() {
    # 1) 下载移动源到同目录临时文件（三超时/重试与电信同参数，失败不改动 DST）
    curl -fsSL --connect-timeout 10 --max-time 120 --retry 3 --retry-delay 10 "$SRC_M" -o "$TMPM"

    # 2) sanitize（仅第二源路径；第一源路径不做 sanitize）：
    #    NR1 #EXTM3U 原样保留 → 丢空行 → 丢非 #EXTINF 的 # 行（注释/外部字幕行等）→ 其余原样。
    #    只删不改；输出仍须严格 EXTINF/URL 成对（filter_stage 成对闸兜底）
    awk 'NR == 1 { print; next }
         /^[[:space:]]*$/ { next }
         /^#/ { if ($0 !~ /^#EXTINF/) next; print; next }
         { print }' "$TMPM" > "$SANM"

    # 3) 校验门（sanitize 后、filter 前，顺序与电信同构）：首行 #EXTM3U → #EXTINF ≥ MO_MIN → requirePattern
    head -n1 "$SANM" | grep -q '^#EXTM3U' || { echo 'bad header' >&2; exit 1; }
    [ "$(grep -c '^#EXTINF' "$SANM")" -ge "$MO_MIN" ] || { echo 'too few channels' >&2; exit 1; }
    grep -qE "$MO_REQ" "$SANM" || { echo 'no http url' >&2; exit 1; }

    # 4) 过滤（fold=1；bl=MO_BL_LC 已 bash 小写化、gs=移动 dropGroups）
    filter_stage "$SANM" "$FILTM" "$STAT_M" "$MO_BL_LC" "$MO_GS" 1 0
    # 五计数不在此读——补充路径跑在子壳里（子壳变量出不了壳），由父壳回读 STAT_M 文件
  }

  # ---- 体育补充源：仅名字/分组过滤，不套用基础源的同名、清晰度或跨源去重 ----
  sports_stage() {
    TMPSP=$(mktemp "''${DST%/*}/.iptv-sports.XXXXXX")
    SANSP=$(mktemp "''${DST%/*}/.iptv-sportssan.XXXXXX")
    FILTSP=$(mktemp "''${DST%/*}/.iptv-sportsfilt.XXXXXX")
    STAT_SP=$(mktemp "''${DST%/*}/.iptv-sportsstats.XXXXXX")
    curl -fsSL --connect-timeout 10 --max-time 120 --retry 3 --retry-delay 10 "$SRC_SP" -o "$TMPSP"
    # 与移动源相同：只丢空行和非 EXTINF 注释，EXTINF 与 URL 原样交给结构校验。
    awk 'NR == 1 { print; next }
         /^[[:space:]]*$/ { next }
         /^#/ { if ($0 !~ /^#EXTINF/) next; print; next }
         { print }' "$TMPSP" > "$SANSP"
    head -n1 "$SANSP" | grep -q '^#EXTM3U' || { echo 'sports: bad header' >&2; exit 1; }
    [ "$(grep -c '^#EXTINF' "$SANSP")" -ge "$SP_MIN" ] || { echo 'sports: too few channels' >&2; exit 1; }
    grep -qE "$SP_REQ" "$SANSP" || { echo 'sports: missing required pattern' >&2; exit 1; }
    filter_stage "$SANSP" "$FILTSP" "$STAT_SP" "$SP_BL" "$SP_GS" 0 1
    read -r sp_blk sp_sib sp_blo sp_dpb sp_grp < "$STAT_SP"
    s_black=''${sp_blk#blacklist=}
    s_grp=''${sp_grp#group=}
    s_kept=$(grep -c '^#EXTINF' "$FILTSP")
    rm -f "$TMPSP" "$SANSP" "$STAT_SP"; TMPSP=""; SANSP=""; STAT_SP=""
  }

  # 所有基础模式只在此发布：先完成体育源，再加单个状态行，最后同目录原子替换。
  publish_output() {
    local candidate="$1"
    if [ "$SPORTS" = "1" ]; then
      sports_stage
      tail -n +2 "$FILTSP" >> "$candidate"
      rm -f "$FILTSP"; FILTSP=""
      UPD="$UPD sports=${sportsSourceLabel} sports_kept=$s_kept sports_filtered_out:blacklist=$s_black group=$s_grp"
    fi
    UPD="# updated $(date -u +%FT%TZ) channels=$(grep -c '^#EXTINF' "$candidate") $UPD"
    { head -n1 "$candidate"; echo "$UPD"; tail -n +2 "$candidate"; } > "$candidate.hdr"
    mv -f "$candidate.hdr" "$candidate"
    chmod 644 "$candidate"
    mv -f "$candidate" "$DST"
  }

  # ---- merge（both / fcc 补充、移动 filter 成功后）：单 awk 两文件趟（NR==FNR 第一趟建主源键集）----
  #   norm(x) = tolower → 依次 gsub 超高清/高清/标清/超清/hd/4k（与分组索引同序，超高清先于高清）
  #              → trim 首尾空白
  #   tcKeys  = 电信全部 survivor 的 norm(t)；tcCores：norm(t) 匹配 ^cctv[0-9] 者取
  #              match(^cctv[0-9]+\+?) 前缀入集（解决真实数据: 电信 CCTV1综合高清 vs 移动 CCTV1；
  #              同时移动 CCTV4K → norm 后 cctv、不满足 ^cctv[0-9] 也进不了 tcCores → 不被误杀）
  #   判定序（保移动原序，first-wins 只登记已接受移动键）: 移动内部 norm 键重复 → dedup；
  #              ∈ tcKeys → merged_dup；bare 式 ^cctv[0-9]+\+?$ 且 ∈ tcCores → merged_dup；否则 keep
  #   strategy: smart=键判重+tcCores 全套；exact=仅 norm 键判重（无 tcCores 分支）；off=纯追加不判重
  #   输出: 单个 #EXTM3U 头（电信头行）→ 电信 pairs 原序 → 移动 keep pairs 原序
  #   统计单行落 stats: mobile_kept=N telecom_kept=N mobile_total=N dedup=N merged_dup=N
  #   （mobile_total = 进入判定的移动过滤存活数 = kept+dedup+merged_dup；电信文件由全灭护栏保证非空，
  #    NR==FNR 两文件趟才不会把第二趟误当第一趟）
  merge_stage() {
    # $1=电信过滤后 $2=移动过滤后 $3=输出 $4=统计文件 $5=strategy
    awk -v stats="$4" -v strategy="$5" '
      function norm(s,   t) {
        t = tolower(s)
        gsub(/超高清/, "", t)
        gsub(/高清/, "", t)
        gsub(/标清/, "", t)
        gsub(/超清/, "", t)
        gsub(/hd/, "", t)
        gsub(/4k/, "", t)
        sub(/^[[:space:]]+/, "", t)
        sub(/[[:space:]]+$/, "", t)
        return t
      }
      # ---- 第一趟（file1 = 电信过滤后存活）：头留档 + 原序 pairs + 键集/tcCores ----
      NR == FNR {
        if (FNR == 1) {
          if ($0 !~ /^#EXTM3U/) { print "merge: telecom file first line not #EXTM3U" > "/dev/stderr"; err = 1; exit 1 }
          hdr = $0
          next
        }
        if (/^#EXTINF/) {
          if (pend_t) { print "merge: telecom EXTINF line " pe_t " has no URL" > "/dev/stderr"; err = 1; exit 1 }
          pend_t = 1
          pe_t = FNR
          text = $0
          next
        }
        if (!pend_t) { print "merge: unexpected telecom line " FNR > "/dev/stderr"; err = 1; exit 1 }
        if ($0 == "" || $0 ~ /^#/) { print "merge: telecom line " FNR " after EXTINF is not a URL" > "/dev/stderr"; err = 1; exit 1 }
        pend_t = 0
        tn++
        t_ext[tn] = text
        t_url[tn] = $0
        d = text
        sub(/^.*,/, "", d)
        nk = norm(d)
        tcKeys[nk] = 1
        if (nk ~ /^cctv[0-9]/ && match(nk, /^cctv[0-9]+\+?/)) tcCores[substr(nk, RSTART, RLENGTH)] = 1
        next
      }
      # ---- 第二趟（file2 = 移动过滤后存活）：头丢弃 + 原序 pairs 暂存（判决集中 END）----
      {
        if (FNR == 1) {
          if ($0 !~ /^#EXTM3U/) { print "merge: mobile file first line not #EXTM3U" > "/dev/stderr"; err = 1; exit 1 }
          next
        }
        if (/^#EXTINF/) {
          if (pend_m) { print "merge: mobile EXTINF line " pe_m " has no URL" > "/dev/stderr"; err = 1; exit 1 }
          pend_m = 1
          pe_m = FNR
          mext = $0
          next
        }
        if (!pend_m) { print "merge: unexpected mobile line " FNR > "/dev/stderr"; err = 1; exit 1 }
        if ($0 == "" || $0 ~ /^#/) { print "merge: mobile line " FNR " after EXTINF is not a URL" > "/dev/stderr"; err = 1; exit 1 }
        pend_m = 0
        m0++
        m_ext[m0] = mext
        m_url[m0] = $0
      }
      END {
        # err 闸门 = 输出零落盘（shell 侧读空 stats 必败 → abort → DST 保旧）
        if (err) exit 1
        if (pend_t) { print "merge: trailing telecom EXTINF line " pe_t " has no URL" > "/dev/stderr"; exit 1 }
        if (pend_m) { print "merge: trailing mobile EXTINF line " pe_m " has no URL" > "/dev/stderr"; exit 1 }
        # 头原样（电信头行）+ 电信全量原序（全灭护栏保证 tn>=1）
        print hdr
        for (i = 1; i <= tn; i++) { print t_ext[i]; print t_url[i] }
        # ---- 移动逐条判定（保移动原序；只删不改、只删不改的输出纪律同样适用）----
        for (i = 1; i <= m0; i++) {
          d = m_ext[i]
          sub(/^.*,/, "", d)
          m = norm(d)
          keep = 1
          if (strategy != "off") {
            if (m in mseen) { keep = 0; c_dedup++ }
            else if (m in tcKeys) { keep = 0; c_dup++ }
            else if (strategy == "smart" && m ~ /^cctv[0-9]+\+?$/ && (m in tcCores)) { keep = 0; c_dup++ }
            else mseen[m] = 1
          }
          if (keep) { print m_ext[i]; print m_url[i]; c_kept++ }
        }
        # ---- 统计段：固定单行落 stats 文件（与 filter 同纪律，错误路径到不了这里）----
        printf "mobile_kept=%d telecom_kept=%d mobile_total=%d dedup=%d merged_dup=%d\n", c_kept, tn, m0, c_dedup, c_dup > stats
      }
    ' "$1" "$2" > "$3"
  }

  # ---- 第一源单独发布（telecom-only 与 both 第二源失败降级共用）----
  publish_telecom_only() {
    # 3) 头部追加 # updated 时间注释（紧随 #EXTM3U；原头行 + filtered_out 后缀；channels= 为过滤后计数）
    #    date 格式字面锁死 %FT%TZ=[0-9T:-Z]，channels 与四计数(block/sibling/blacklist/group)均纯数字
    #    → 插行文本注入面为空【审查 S9】；勿改成 %c/%Z 类可变格式
    UPD="src=${telecomSourceLabel} filtered_out:block=$n_block sibling=$n_sib blacklist=$n_black group=$n_grp"
    publish_output "$TMP"
    TMP=""
  }
  # ---- 组播阶段（带 udpxy 参数下载、校验、过滤）----
  multicast_stage() {
    curl -fsSL --connect-timeout 10 --max-time 120 --retry 3 --retry-delay 10 "$SRC_MC" -o "$TMPMC"
    head -n1 "$TMPMC" | grep -q '^#EXTM3U' || { echo 'bad header' >&2; exit 1; }
    [ "$(grep -c '^#EXTINF' "$TMPMC")" -ge "$MC_MIN" ] || { echo 'too few channels' >&2; exit 1; }
    grep -qE "$MC_REQ" "$TMPMC" || { echo 'no multicast url' >&2; exit 1; }

    FILTMC=$(mktemp "''${DST%/*}/.iptv-mcfilt.XXXXXX")
    STAT_MC=$(mktemp "''${DST%/*}/.iptv-mcstats.XXXXXX")
    filter_stage "$TMPMC" "$FILTMC" "$STAT_MC" "$MC_BL" "$MC_GS" 0 0
    read -r mc_blk mc_sib mc_blo mc_dpb mc_grp < "$STAT_MC"
    n_mc_black=''${mc_blk#blacklist=}
    n_mc_sib=''${mc_sib#sibling=}
    n_mc_block=''${mc_blo#block=}
    n_mc_dp3=''${mc_dpb#dp3=}
    n_mc_grp=''${mc_grp#group=}
    rm -f "$STAT_MC"
    STAT_MC=""
    mv -f "$FILTMC" "$TMPMC"
    FILTMC=""
  }

  publish_multicast() {
    UPD="src=${multicastSourceLabel} udpxy=''${UDPXY:-none} filtered_out:block=$n_mc_block sibling=$n_mc_sib blacklist=$n_mc_black group=$n_mc_grp"
    publish_output "$TMPMC"
    TMPMC=""
  }
  # FCC 只变换已过滤的主源 URL；移动源尚未合并，转换失败则不发布。
  fcc_stage() {
    awk -v gateway="$FCC_GATEWAY" -v address="$FCC_ADDRESS" -v port="$FCC_PORT" -v protocol="$FCC_PROTOCOL" '
      function fail(message) {
        print "fcc: " message " at line " NR > "/dev/stderr"
        exit 1
      }
      NR == 1 || /^#EXTINF/ { print; next }
      {
        url = $0
        fragment = ""
        pos = index(url, "#")
        if (pos) { fragment = substr(url, pos); url = substr(url, 1, pos - 1) }
        query = ""
        pos = index(url, "?")
        if (pos) { query = substr(url, pos + 1); url = substr(url, 1, pos - 1) }
        if (url !~ /^http:\/\/[^/?#[:space:]]+\/(udp|rtp)\/[0-9.]+:[0-9]+$/)
          fail("unsupported multicast URL")
        sub(/^http:\/\/[^/]+\/(udp|rtp)\//, "", url)
        split(url, endpoint, ":")
        if (split(endpoint[1], octets, /[.]/) != 4) fail("invalid multicast address")
        for (i = 1; i <= 4; i++)
          if (octets[i] !~ /^[0-9]+$/ || octets[i] + 0 > 255)
            fail("invalid multicast address")
        if (octets[1] + 0 < 224 || octets[1] + 0 > 239) fail("not a multicast address")
        if (endpoint[2] + 0 < 1 || endpoint[2] + 0 > 65535) fail("invalid multicast port")
        kept = ""
        count = split(query, params, "&")
        for (i = 1; i <= count; i++) {
          key = params[i]
          sub(/=.*/, "", key)
          if (key == "fcc" || key == "fcc-type" || params[i] == "") continue
          kept = kept (kept == "" ? "" : "&") params[i]
        }
        print "http://" gateway "/rtp/" url "?" (kept == "" ? "" : kept "&") \
          "fcc=" address ":" port "&fcc-type=" protocol fragment
      }
    ' "$TMPMC" > "$TMPMC.hdr"
    mv -f "$TMPMC.hdr" "$TMPMC"
  }


  # ---- 五模式分派 ----
  case "$MODE" in
    telecom-only)
      # 只拉第一源；失败保留旧产物。
      telecom_stage
      publish_telecom_only
      # v3) 过滤统计入 journal（成功路径 stdout；明细 WARN 在过滤阶段已走 stderr）
      echo "filter: filtered_out block=$n_block sibling=$n_sib blacklist=$n_black dp3=$n_dp3 group=$n_grp"
      ;;
    mobile-only)
      # 只拉第二源。阶段失败 → set -e abort → trap 清件 → DST 保旧
      rm -f "$TMP"; TMP=""                    # 电信临时件本模式无用即清（trap 兜底残量）
      TMPM=$(mktemp "''${DST%/*}/.iptv-mobile.XXXXXX")
      SANM=$(mktemp "''${DST%/*}/.iptv-msan.XXXXXX")
      FILTM=$(mktemp "''${DST%/*}/.iptv-mfilt.XXXXXX")
      STAT_M=$(mktemp "''${DST%/*}/.iptv-mstats.XXXXXX")
      mobile_stage
      # 读移动五计数（stats 行格式与电信同: blacklist=N sibling=N block=N dp3=N group=N）
      read -r fm_blk fm_sib fm_blo fm_dpb fm_grp < "$STAT_M"
      m_black=''${fm_blk#blacklist=}
      m_sib=''${fm_sib#sibling=}
      m_block=''${fm_blo#block=}
      m_dp3=''${fm_dpb#dp3=}
      m_grp=''${fm_grp#group=}
      rm -f "$STAT_M"; STAT_M=""
      rm -f "$TMPM" "$SANM"; TMPM=""; SANM=""  # 下载/净化中间件即清（trap 兜底残量）
      UPD="src=${mobileSourceLabel} filtered_out:block=$m_block sibling=$m_sib blacklist=$m_black group=$m_grp"
      publish_output "$FILTM"
      FILTM=""
      echo "mobile-filter: filtered_out block=$m_block sibling=$m_sib blacklist=$m_black dp3=$m_dp3 group=$m_grp"
      ;;
    both)
      # 双源：第一源优先；第一源失败 = 整体 abort 保旧，第二源失败 = WARN 降级发布第一源
      telecom_stage
      TMPM=$(mktemp "''${DST%/*}/.iptv-mobile.XXXXXX")
      SANM=$(mktemp "''${DST%/*}/.iptv-msan.XXXXXX")
      FILTM=$(mktemp "''${DST%/*}/.iptv-mfilt.XXXXXX")
      STAT_M=$(mktemp "''${DST%/*}/.iptv-mstats.XXXXXX")
      # 移动阶段包进受控子壳: 父壳先 set +e 接住非零——若把子壳直接挂 if 条件位，bash 会把
      # 「条件上下文忽略 errexit」传染进子壳内部（set -e 失效、错误命令继续跑，已实测），
      # 故子壳以普通命令身份回传状态、壳内自开 set -e 恢复直达；父壳变量出不了子壳 →
      # 五计数走 STAT_M/STAT_MG 文件回读。
      set +e
      ( set -e; mobile_stage )
      mob_st=$?
      set -e
      if [ "$mob_st" -ne 0 ]; then
        # 移动阶段失败 → WARN 入 stderr（journal 同收）+ 跳过移动，仍发布电信-only
        echo "WARN: mobile source failed, publishing telecom-only" >&2
        rm -f "$TMPM" "$SANM" "$FILTM" "$STAT_M"; TMPM=""; SANM=""; FILTM=""; STAT_M=""
        publish_telecom_only
        # journal: 第一源单独发布的统计行末尾追加 mobile_skipped=1
        echo "filter: filtered_out block=$n_block sibling=$n_sib blacklist=$n_black dp3=$n_dp3 group=$n_grp mobile_skipped=1"
      else
        # ---- 移动成功: 父壳回读移动五计数（同 stats 行格式）----
        read -r fm_blk fm_sib fm_blo fm_dpb fm_grp < "$STAT_M"
        m_black=''${fm_blk#blacklist=}
        m_sib=''${fm_sib#sibling=}
        m_block=''${fm_blo#block=}
        m_dp3=''${fm_dpb#dp3=}
        m_grp=''${fm_grp#group=}
        rm -f "$STAT_M"; STAT_M=""
        rm -f "$TMPM" "$SANM"; TMPM=""; SANM=""
        # ---- merge: 两源过滤件 → 单头合并件（merge 失败 = 整体 abort 保旧）----
        STAT_MG=$(mktemp "''${DST%/*}/.iptv-mergestats.XXXXXX")
        MRG=$(mktemp "''${DST%/*}/.iptv-merged.XXXXXX")
        merge_stage "$TMP" "$FILTM" "$MRG" "$STAT_MG" "$STRATEGY"
        read -r gm_kep gm_tc gm_m0 gm_ded gm_dup < "$STAT_MG"
        n_kept=''${gm_kep#mobile_kept=}
        n_tck=''${gm_tc#telecom_kept=}
        n_m0=''${gm_m0#mobile_total=}
        n_ded=''${gm_ded#dedup=}
        n_dup=''${gm_dup#merged_dup=}
        rm -f "$STAT_MG"; STAT_MG=""
        rm -f "$TMP" "$FILTM"; TMP=""; FILTM=""  # 两源已被 MRG 吸收（trap 兜底残量）
        # ---- both 头部 # updated（单个 #EXTM3U 头=电信头行；channels= 最终文件 EXTINF 计数）----
        #     注入面纪律同审查 S9: date 锁 %FT%TZ、计数纯数字
        UPD="src=${telecomSourceLabel}+${mobileSourceLabel} merged=+$n_kept telecom_filtered_out:block=$n_block sibling=$n_sib blacklist=$n_black group=$n_grp mobile_filtered_out:block=$m_block sibling=$m_sib blacklist=$m_black group=$m_grp"
        publish_output "$MRG"
        MRG=""
        # ---- journal 单行: 电信行 + 空格 + mobile-filter 片段 + 空格 + merge 片段 ----
        echo "filter: filtered_out block=$n_block sibling=$n_sib blacklist=$n_black dp3=$n_dp3 group=$n_grp mobile-filter: filtered_out block=$m_block sibling=$m_sib blacklist=$m_black dp3=$m_dp3 group=$m_grp merge: mobile_kept=+$n_kept telecom_kept=$n_tck mobile_total=$n_m0 dedup=$n_ded merged_dup=$n_dup"
      fi
      ;;
    multicast)
      rm -f "$TMP"; TMP=""
      TMPMC=$(mktemp "''${DST%/*}/.iptv-mcast.XXXXXX")
      multicast_stage
      publish_multicast
      echo "multicast-filter: filtered_out block=$n_mc_block sibling=$n_mc_sib blacklist=$n_mc_black dp3=$n_mc_dp3 group=$n_mc_grp"
      ;;
    fcc)
      rm -f "$TMP"; TMP=""
      TMPMC=$(mktemp "''${DST%/*}/.iptv-mcast.XXXXXX")
      multicast_stage
      fcc_stage
      if [ "$FCC_INCLUDE_MOBILE" = "0" ]; then
        publish_multicast
        echo "fcc-filter: filtered_out block=$n_mc_block sibling=$n_mc_sib blacklist=$n_mc_black dp3=$n_mc_dp3 group=$n_mc_grp"
      else
        TMPM=$(mktemp "''${DST%/*}/.iptv-mobile.XXXXXX")
        SANM=$(mktemp "''${DST%/*}/.iptv-msan.XXXXXX")
        FILTM=$(mktemp "''${DST%/*}/.iptv-mfilt.XXXXXX")
        STAT_M=$(mktemp "''${DST%/*}/.iptv-mstats.XXXXXX")
        # 与 both 相同：普通子壳命令恢复 errexit，不能放在 if/! 条件上下文。
        set +e
        ( set -e; mobile_stage )
        mob_st=$?
        set -e
        if [ "$mob_st" -ne 0 ]; then
          echo "WARN: mobile source failed, publishing FCC primary only" >&2
          rm -f "$TMPM" "$SANM" "$FILTM" "$STAT_M"; TMPM=""; SANM=""; FILTM=""; STAT_M=""
          publish_multicast
          echo "fcc-filter: filtered_out block=$n_mc_block sibling=$n_mc_sib blacklist=$n_mc_black dp3=$n_mc_dp3 group=$n_mc_grp mobile_skipped=1"
        else
          read -r fm_blk fm_sib fm_blo fm_dpb fm_grp < "$STAT_M"
          m_black=''${fm_blk#blacklist=}
          m_sib=''${fm_sib#sibling=}
          m_block=''${fm_blo#block=}
          m_dp3=''${fm_dpb#dp3=}
          m_grp=''${fm_grp#group=}
          rm -f "$STAT_M"; STAT_M=""
          rm -f "$TMPM" "$SANM"; TMPM=""; SANM=""
          STAT_MG=$(mktemp "''${DST%/*}/.iptv-mergestats.XXXXXX")
          MRG=$(mktemp "''${DST%/*}/.iptv-merged.XXXXXX")
          merge_stage "$TMPMC" "$FILTM" "$MRG" "$STAT_MG" "$STRATEGY"
          read -r gm_kep gm_tc gm_m0 gm_ded gm_dup < "$STAT_MG"
          n_kept=''${gm_kep#mobile_kept=}
          n_tck=''${gm_tc#telecom_kept=}
          n_m0=''${gm_m0#mobile_total=}
          n_ded=''${gm_ded#dedup=}
          n_dup=''${gm_dup#merged_dup=}
          rm -f "$STAT_MG"; STAT_MG=""
          rm -f "$TMPMC" "$FILTM"; TMPMC=""; FILTM=""
          UPD="src=${multicastSourceLabel}+${mobileSourceLabel} udpxy=$UDPXY merged=+$n_kept multicast_filtered_out:block=$n_mc_block sibling=$n_mc_sib blacklist=$n_mc_black group=$n_mc_grp mobile_filtered_out:block=$m_block sibling=$m_sib blacklist=$m_black group=$m_grp"
          publish_output "$MRG"
          MRG=""
          echo "fcc-filter: filtered_out block=$n_mc_block sibling=$n_mc_sib blacklist=$n_mc_black dp3=$n_mc_dp3 group=$n_mc_grp mobile-filter: filtered_out block=$m_block sibling=$m_sib blacklist=$m_black dp3=$m_dp3 group=$m_grp merge: mobile_kept=+$n_kept multicast_kept=$n_tck mobile_total=$n_m0 dedup=$n_ded merged_dup=$n_dup"
        fi
      fi
      ;;
  esac

  trap - EXIT HUP INT TERM
  ''
