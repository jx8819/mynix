# mynix

Nix 软件包与 NixOS 模块。

- 查看输出：`nix flake show github:jx8819/mynix`
- 构建软件包：`nix build github:jx8819/mynix#<包名>`（x86_64-linux）
- 在 NixOS 配置中按需导入 `mynix.nixosModules.<名称>`；软件包可通过 `mynix.overlays.default` 使用。

源码见 `pkgs/`、`modules/`；输出清单以 `flake.nix` 为准。部分包需要 `allowUnfree`。

## IPTV 多模式同步与 FCC

`nixosModules.iptv-merge` 的过滤/发布程序位于 `pkgs/iptv-merge`。每个
`services.iptv-merge.outputs` 项独立写入自己的 `outputPath`，模式与 service/timer
名称固定对应：`both` → `iptv-sync`、`telecom-only` → `iptv-telecom-sync`、
`mobile-only` → `iptv-mobile-sync`、`multicast` → `iptv-multicast-sync`、
`fcc` → `iptv-fcc-sync`。已有四种模式的源、URL 与默认配置不变。

在已启用、已配置运行用户及 `multicast.url` 的实例中，可追加一个独立 FCC 输出
（以下均为文档示例地址；实际站点参数放在消费此模块的配置仓库）：

```nix
services.iptv-merge.outputs = [
  # 原有输出继续保留，各模式与 outputPath 均须唯一。
  {
    mode = "fcc";
    outputPath = "/srv/iptv/iptv-fcc.m3u";
    fcc = {
      gateway = "192.0.2.10:5140";
      address = "198.51.100.30";
      port = 8027;
      protocol = "huawei";
      includeMobile = true; # 可选；默认 false，仍为纯 FCC 组播输出。
    };
  }
];
```

`fcc.gateway`、`fcc.address`、`fcc.port` 默认均为 `null`，仅 `fcc` 模式必须显式提供；
网关为 `host:port`，FCC 地址为 IPv4 地址或主机名，端口范围为 1–65535。
`fcc.protocol` 仅支持 rtp2httpd 的 `telecom` / `huawei`，默认 `telecom`。
订阅源须支持 `udpxy` 查询参数并返回 HTTP `/udp/<组播IPv4>:<端口>` 或
`/rtp/<组播IPv4>:<端口>` URL。新模式以专属网关作为 `udpxy` 参数请求
`multicast.url`，执行同一套组播校验/过滤后，仅将存活频道 URL 转为：

```text
http://192.0.2.10:5140/rtp/239.1.2.3:1234?fcc=198.51.100.30:8027&fcc-type=huawei
```

主源中的 `fcc` / `fcc-type` 参数全部替换，其他查询参数（含重复项及编码）与 fragment
保留；M3U 头部、EXTINF 元数据和频道顺序不变，仍追加 `# updated` 注释。
FCC 配置缺失、非法端口、未知协议、主源结构损坏或不支持的主源频道 URL 都会拒绝新发布
并保留旧文件；不会回退为无 FCC 的主源 URL，也不会向其他模式注入 FCC 参数。

`fcc.includeMobile` 默认 `false`，保持原有纯 FCC 输出。设为 `true` 时，先完成组播主源
过滤与 FCC 转换，再用同一份 `mobile.url`、`mobile.minChannels`、`mobile.requirePattern`、
`mobile.blacklist`、`mobile.dropGroups` 做移动源净化/校验/过滤，并复用 `merge.strategy`
合并；不需要第二套移动源设置。默认 `smart` 会归一化高清/HD/4K 等标记、跨源去重、
移动内部 first-wins，并识别 CCTV 裸号与主源完整频道名：主源优先且顺序不变，
只在末尾按移动原序补缺。`exact` 只按归一化名称去重、不使用 CCTV 裸号规则，
`off` 则保留过滤后的移动频道、直接追加。移动 HTTP/HLS URL 和 EXTINF 始终原样，
不经过 FCC 转换，也不新增 `fcc` 参数。

移动补充失败时与 `both` 一样发出 stderr 警告，并原子发布本次成功的 FCC 主源，
不保留旧的移动条目；主源失败或合并失败则仍保留旧文件。补充成功的输出注释包含主源与
移动源标签、最终频道数和实际新增数，journal 同时记录两源过滤与去重/合并计数。

直接调用构建后的 `iptv-merge` 时，设置 `IPTV_MODE=fcc`、`IPTV_DST`，
以及 `IPTV_FCC_GATEWAY`、`IPTV_FCC_ADDRESS`、`IPTV_FCC_PORT`；
`IPTV_FCC_PROTOCOL` 可省略（默认 `telecom`），`IPTV_FCC_INCLUDE_MOBILE` 省略时为 `0`；
只有 `0`（不补充）与 `1`（补充）合法，空值或其他值会在任何文件动作前报错。
`IPTV_SRC_MULTICAST` 仍可覆盖组播订阅源，`IPTV_SRC_MOBILE` 可覆盖移动订阅源。
仅 FCC 模式会读取这些 `IPTV_FCC_*` 变量并以其网关代替 `IPTV_UDPXY`；
NixOS 模块也仅为 FCC 单元注入这些变量。

### 离线回归 smoke

没有额外测试框架；`pkgs/iptv-merge/smoke-test.py` 接收**实际构建的可执行文件**，
通过本机 HTTP 订阅源调用真实 CLI，不提取或复制程序的 awk 实现、不连接 IPTV 上游。
使用默认构建参数，环境需有 Python 3、curl、gawk、grep 与 coreutils：

```sh
nix build .#iptv-merge
python3 pkgs/iptv-merge/smoke-test.py ./result/bin/iptv-merge
```

覆盖 UDP/RTP 路径转换、查询参数保留与 FCC 覆盖、协议默认值、共享过滤、
高清/HD 名称重叠、CCTV 裸号/加号去重、CCTV4K 与移动独有频道保留、
移动 URL/元数据/追加顺序不变、显式关闭补充、移动失败降级、旧 multicast 行为不变，
以及非法 FCC 配置/损坏主源的原子保旧和临时文件清理；不依赖日志或状态注释的具体措辞。

默认软件包没有站点黑名单或剔除组；要用同一真实 CLI 验证这两个过滤项，可构建仅为静态
fixture 配置的实例（仍无外网上游请求）：

```sh
nix build --impure --out-link result-iptv-smoke --expr '
  let flake = builtins.getFlake (toString ./.);
  in flake.packages.x86_64-linux.iptv-merge.override {
    mobileBlacklist = [ "BLOCKED CANDIDATE" ];
    mobileDropGroups = [ "Excluded" ];
    mergeStrategy = "smart";
  }'
python3 pkgs/iptv-merge/smoke-test.py ./result-iptv-smoke/bin/iptv-merge \
  --mobile-filters --strategy smart
```

fixture 同时包含大小写混合的黑名单候选与命中剔除组的候选，开启 `--mobile-filters`
后断言二者均不进入结果。再分别把构建参数 `mergeStrategy` 与运行参数 `--strategy`
改为 `exact`、`off`，即可验证相应去重/追加行为；这些运行参数仅声明待测程序的构建配置，
不会修改程序或模拟其过滤逻辑。生产实例还应使用实际配置的构建产物与固定订阅快照验证
站点黑名单/分组规则，并对比原有四个输出。
