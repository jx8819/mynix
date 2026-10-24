# mynix

Nix 软件包与 NixOS 模块。

- 查看输出：`nix flake show github:jx8819/mynix`
- 构建软件包：`nix build github:jx8819/mynix#<包名>`（x86_64-linux）
- 在 NixOS 配置中按需导入 `mynix.nixosModules.<名称>`；软件包可通过 `mynix.overlays.default` 使用。

源码见 `pkgs/`、`modules/`；输出清单以 `flake.nix` 为准。部分包需要 `allowUnfree`。

## OMP 与 ompweb

`omp` 固定到上游稳定版 **18.12.6** 的源码提交，Rust 与 Bun 依赖均由固定输出哈希锁定；
`ompweb` 固定到 npm 发布版 **0.5.2**，使用该发布记录 `gitHead` 对应的上游原版
`package-lock.json` 重建生产依赖（npm tarball 不包含 lock 文件）。

OMP 的构建、依赖安装与启动均要求 **Bun >= 1.3.14**，保留上游最低版本校验，不再通过补丁屏蔽。
若消费方 Nixpkgs 的 Bun 已达标则直接复用；否则仅在 OMP 包内复用 Nixpkgs 的 Bun 打包配方，
使用官方 **1.3.14 x86_64-linux** 发布包及固定哈希，不替换系统或其他服务的 Bun，也不更新全局 Nixpkgs。
Bun 1.3.13 存在并发文件流误关 TTY stdin 的缺陷，详见 [Bun 1.3.14 修复说明](https://bun.com/blog/bun-v1.3.14)
和 [上游问题 #30565](https://github.com/oven-sh/bun/issues/30565)。隔离真实 PTY、保持 master 打开的版本对照中，
同一份 OMP `ProcessTerminal` 在 1.3.13 下因假 EOF 自发 SIGHUP、退出码 129；1.3.14 保持 raw 输入可读。
本修复不忽略 EOF 或 SIGHUP；真实终端断开时仍保留上游退出行为。

```sh
nix build .#omp .#ompweb
```

ompweb 0.5.2 不再接受明文 `OMP_WEB_PASSWORD`。`services.ompweb.environmentFile`
必须提供 `OMP_WEB_PASSWORD_HASH`，值由同版上游 `hash-password` 工具生成，格式为
`scrypt$…`。哈希也应由消费仓库的 SOPS 等机制在运行时注入，不写入 Nix store；
从原密码生成哈希无需更改用户的登录密码。

## IPTV 多模式同步与 FCC

`nixosModules.iptv-merge` 的过滤/发布程序位于 `pkgs/iptv-merge`。每个
`services.iptv-merge.outputs` 项独立写入自己的 `outputPath`，基础模式与 service/timer
名称固定对应：`both` → `iptv-sync`、`telecom-only` → `iptv-telecom-sync`、
`mobile-only` → `iptv-mobile-sync`、`multicast` → `iptv-multicast-sync`、
`fcc` → `iptv-fcc-sync`。`sports` 默认关闭，既有五类输出的源、URL 与默认配置不变。

在已启用、已配置运行用户及 `multicast.url` 的实例中，可追加一个独立 FCC 输出
（以下均为文档示例地址；实际站点参数放在消费此模块的配置仓库）：

```nix
services.iptv-merge.outputs = [
  # 原有输出继续保留，(mode, sports) 组合与 outputPath 均须唯一。
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

### 可选体育补充

每个输出都可设置 `sports = true;`，与基础 `mode` 独立组合。原地址不变时，新增一项
不同 `outputPath` 的输出即可发布独立下载地址；同一 `(mode, sports)` 组合仍只能定义一次。
体育源 URL、黑名单与分组规则由站点显式配置，通用模块不内置订阅源或频道偏好：

```nix
services.iptv-merge.sports = {
  url = "https://sports.example/playlist.m3u";
  blacklist = [ "Exclude" "Guide" ];
};
services.iptv-merge.outputs = [
  { mode = "telecom-only"; outputPath = "/srv/iptv/telecom.m3u"; }
  { mode = "telecom-only"; sports = true; outputPath = "/srv/iptv/telecom-sports.m3u"; }
];
```

体育变体的单元名在原名末尾的 `-sync` 前加 `-sports`，例如
`iptv-telecom-sports-sync`、`iptv-fcc-sports-sync`；`both` 对应 `iptv-sports-sync`。
`fcc` 可同时设置 `fcc.includeMobile = true;` 与 `sports = true;`。

`sports` 的 `sourceLabel`、`minChannels`、`requirePattern`、`blacklist`、`dropGroups`
沿用其他源的配置语义。黑名单匹配 `#EXTINF` 逗号后的显示名，而非 `tvg-name`；
体育补充只做结构校验与显式黑名单/分组过滤，不套用基础源的去重、同名/高清优选或
`merge.strategy`。存活频道按体育源原序追加，EXTINF 与 URL/查询参数/fragment 原样保留，
不经过 udpxy 或 FCC 改写，输出仍只有一个 M3U 头。

未开启时不下载体育源；开启后体育下载、校验或过滤失败会保留旧的完整输出，
不会先发布缺少体育频道的基础列表。CLI 使用 `IPTV_SPORTS=1` 开启（省略为 `0`，
只接受 `0` / `1`），`IPTV_SRC_SPORTS` 可覆盖体育订阅 URL。


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
以及体育补充的五模式组合、FCC+移动+体育隔离、共享元数据的不同频道保留、
关闭时不拉取体育源、体育失败原子保旧、非法配置/损坏源与临时文件清理；
不依赖日志或状态注释的具体措辞。

默认软件包没有站点黑名单或剔除组；要用同一真实 CLI 验证这两个过滤项，可构建仅为静态
fixture 配置的实例（仍无外网上游请求）：

```sh
nix build --impure --out-link result-iptv-smoke --expr '
  let flake = builtins.getFlake (toString ./.);
  in flake.packages.x86_64-linux.iptv-merge.override {
    mobileBlacklist = [ "BLOCKED CANDIDATE" ];
    mobileDropGroups = [ "Excluded" ];
    sportsBlacklist = [ "睛彩" "说明" ];
    mergeStrategy = "smart";
  }'
python3 pkgs/iptv-merge/smoke-test.py ./result-iptv-smoke/bin/iptv-merge \
  --mobile-filters --sports-filters --strategy smart
```

fixture 包含大小写混合的移动黑名单候选、命中剔除组的候选，以及显示名含
`睛彩` / `说明`、但 `tvg-name` 与正常体育频道相同的候选。`--mobile-filters` /
`--sports-filters` 声明待测程序已使用对应构建参数，断言被排除频道不进入结果。
再分别把构建参数 `mergeStrategy` 与运行参数 `--strategy` 改为 `exact`、`off`，
即可验证相应去重/追加行为；这些运行参数不会修改程序或模拟其过滤逻辑。
生产实例还应使用实际配置的构建产物与固定订阅快照验证站点规则，并对比原有五类输出。
