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

源中的 `fcc` / `fcc-type` 参数全部替换，其他查询参数（含重复项及编码）与 fragment
保留；M3U 头部、EXTINF 元数据和频道顺序不变，仍追加原有格式的 `# updated` 注释。
配置缺失、非法端口、未知协议、结构损坏或不支持的频道 URL 都会拒绝新发布并保留旧文件；
不会回退为无 FCC 的 URL，也不会向其他模式注入 FCC 参数。

直接调用构建后的 `iptv-merge` 时，设置 `IPTV_MODE=fcc`、`IPTV_DST`，
以及 `IPTV_FCC_GATEWAY`、`IPTV_FCC_ADDRESS`、`IPTV_FCC_PORT`；
`IPTV_FCC_PROTOCOL` 可省略（默认 `telecom`）。
`IPTV_SRC_MULTICAST` 仍可覆盖组播订阅源。仅 FCC 模式会读取这些 `IPTV_FCC_*`
变量并以其网关代替 `IPTV_UDPXY`；NixOS 模块也仅为 FCC 单元注入这些变量。

### 离线回归 smoke

没有额外测试框架；`pkgs/iptv-merge/smoke-test.py` 接收**实际构建的可执行文件**，
通过本机 HTTP 订阅源调用真实 CLI，不提取或复制程序的 awk 实现、不连接 IPTV 上游。
使用默认构建参数，环境需有 Python 3、curl、gawk、grep 与 coreutils：

```sh
nix build .#iptv-merge
python3 pkgs/iptv-merge/smoke-test.py ./result/bin/iptv-merge
```

覆盖 UDP/RTP 路径转换、查询参数保留与 FCC 覆盖、协议默认值、共享过滤、
旧 multicast 行为不变，以及非法 FCC 配置/损坏订阅源的原子保旧和临时文件清理。
