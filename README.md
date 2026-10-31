# mynix
Nix 软件包与 NixOS 模块，源码见 `pkgs/`、`modules/`。
用 `nix flake show github:jx8819/mynix` 查看输出，按需构建软件包或导入模块。

## IPTV 输出多实例

导入 `nixosModules.iptv-merge` 后，在既有订阅源、过滤规则和运行身份配置下，
可用 `services.iptv-merge.outputs` 同时发布同一模式的多个实例：

```nix
services.iptv-merge.outputs = [
  {
    name = "edge";
    mode = "multicast";
    udpxy = "proxy.example:4022";
    outputPath = "/srv/iptv/edge.m3u";
  }
  {
    name = "edge";
    mode = "multicast";
    sports = true;
    udpxy = "proxy.example:4022";
    outputPath = "/srv/iptv/edge-sports.m3u";
  }
];
```

上例生成 `iptv-multicast-edge-sync`、`iptv-multicast-edge-sports-sync`
两组 oneshot service 与 timer，共用模块的订阅源、过滤和定时配置。
`name` 默认 `null`，不改变原有五模式及体育变体的十个单元名。
显式标识只允许小写字母、数字和分段用的单连字符；输出路径与最终单元名均须唯一，
完整 service 单元名最长 255 字节。名称碰撞按最终结果检查：
例如普通组播的 `name = "sports"` 会与未命名的体育组播输出冲突。

## rtp2httpd 软件包

`packages.x86_64-linux.rtp2httpd` 与 overlay 的 `pkgs.rtp2httpd` 从上游
`v3.17.2` 源码构建，使用发布标签中已生成的内嵌 Web UI，不需要 Node.js、
FFmpeg 或容器运行时。`nix run github:jx8819/mynix#rtp2httpd -- --help`
可查看运行参数；监听地址、组播接口、防火墙和 systemd 实例属于部署方配置，
本仓库不另设包装 NixOS 模块。

## jellyfin-pinned 冻结构建与声明式配置

`pkgs.jellyfin-pinned` 从 jx8819/jellyfin `release-10.11.z` 的冻结 commit 构建
Jellyfin 10.11；jellyfin-ffmpeg、jellyfin-web、dotnet 工具链全部取自同一个按
rev + hash 冻结的 nixpkgs（当前 `a033e66…`）。上游 NixOS / nixpkgs 升级不改变
该闭包。升级到 fork 的新 commit 时，同步修改 `pkgs/jellyfin-pinned/default.nix`
里的 `forkRev` 与源码 hash；若 csproj 的 NuGet 依赖有变化，需按 `jellyfin-pin`
对应的 nixpkgs 中 `pkgs/by-name/je/jellyfin/update.sh` 的流程重建 nuget-deps.json
（当前直接复用冻结 nixpkgs 里的那份）。

导入 `nixosModules.jellyfin` 后，在上游 `services.jellyfin` 之上多出三组托管字段
（`encoding` 对应 encoding.xml、`server` 对应 system.xml、`network` 对应
network.xml，`null` = 不托管），默认包为 `pkgs.jellyfin-pinned`：

```nix
services.jellyfin = {
  enable = true;
  package = pkgs.jellyfin-pinned;
  user = "media";
  dataDir = "/mnt/data/jellyfin";
  cacheDir = "/mnt/cache/jellyfin";
  encoding = {
    transcodingTempPath = "/mnt/cache/jellyfin/transcodes";
    hardwareAccelerationType = "vaapi";
    vaapiDevice = "/dev/dri/renderD128";
    enableTonemapping = true;
    enableDecodingColorDepth10HevcRext = true;
    enableDecodingColorDepth12HevcRext = true;
    hardwareDecodingCodecs = [ "h264" "hevc" "vc1" ];
  };
  server = {
    uiCulture = "zh-CN";
    preferredMetadataLanguage = "zh";
    metadataCountryCode = "CN";
    libraryScanFanoutConcurrency = 2;
  };
  network = {
    enableRemoteAccess = false;
    autoDiscovery = false;
  };
};
```

托管字段在服务启动前写入（存在则更新、缺失则创建最小配置文件），未托管字段继续
由 Web UI 管理。媒体库、用户、插件、元数据、播放记录等运行状态在 `dataDir`，
不由 Nix 重建；新主机复现的是同一服务与同一份托管设置。
