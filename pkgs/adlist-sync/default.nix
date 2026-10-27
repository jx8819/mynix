# 拉取广告过滤列表，校验非注释行数后同目录 mktemp+mv 原子落盘；失败保留旧产物。
{ lib
, writeShellScriptBin
, sources ? []            # listOf { name, url }；name 即输出文件名（自动补 .txt）
, outputDir ? "/var/lib/adlist"
, minLines ? 100           # 非注释行数下限，低于则拒收保旧
}:
writeShellScriptBin "adlist-sync" ''
  # env 测试口（覆盖 baked 值，service 不设故行为即默认字面）：
  #   ADLIST_SYNC_OUT / ADLIST_SYNC_MIN_LINES / ADLIST_SYNC_SOURCES（"name|url" 换行分隔）
  # 失败语义（同目录 mktemp+mv 原子替换，任一失败该源保旧）：
  #   - 单源下载/校验失败 → stderr 打 WARN、跳过该源、其余照常
  #   - 全部源失败 → exit 1（timer 日志可见，旧产物全部保留）
  set -euo pipefail
  OUT="''${ADLIST_SYNC_OUT:-${outputDir}}"
  MIN="''${ADLIST_SYNC_MIN_LINES:-${toString minLines}}"

  DEFAULT_SOURCES=$(cat <<'EOF'
${lib.concatMapStrings (s: "${s.name}|${s.url}\n") sources}
EOF
)
  SOURCES="''${ADLIST_SYNC_SOURCES:-$DEFAULT_SOURCES}"

  mkdir -p "$OUT"
  ok=0
  total=0
  while IFS='|' read -r name url; do
    [ -z "''${name:-}" ] && continue
    total=$((total+1))
    tmp=$(mktemp "$OUT/.$name.XXXXXX")
    if ! curl -fsSL --retry 3 --retry-delay 2 --max-time 300 "$url" -o "$tmp"; then
      echo "WARN: fetch failed: $name ($url)，保旧" >&2
      rm -f "$tmp"
      continue
    fi
    # 校验：非注释行计数（空行与纯空白行也计入；--count 自身在零匹配时 exit 1，|| true 防 set -e 连坐）
    lines=$(grep -cve '^[[:space:]]*#' "$tmp" || true)
    if [ "$lines" -lt "$MIN" ]; then
      echo "WARN: $name only $lines valid lines (< $MIN)，保旧" >&2
      rm -f "$tmp"
      continue
    fi
    # mktemp 固定创建 0600，UMask=0022 不会放宽；发布前显式允许 Web 服务读取。
    chmod 0644 "$tmp"
    mv "$tmp" "$OUT/$name.txt"
    echo "OK: $name -> $OUT/$name.txt ($lines valid lines)"
    ok=$((ok+1))
  done <<EOF_LIST
$SOURCES
EOF_LIST

  if [ "$ok" -eq 0 ]; then
    echo "ERROR: all $total sources failed, old files kept" >&2
    exit 1
  fi
  echo "done: $ok/$total sources refreshed in $OUT"
''
