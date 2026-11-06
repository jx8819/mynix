# mynix
Nix 软件包与 NixOS 模块，源码见 `pkgs/`、`modules/`。
用 `nix flake show github:jx8819/mynix` 查看输出，按需构建软件包或导入模块。

## Home Assistant：Tuya Local

`ha-tuya-local` 通过现有 overlay 与 `packages.x86_64-linux` 导出，复用 Nixpkgs
的 Tuya Local（当前锁定上游版本 `2026.5.2`），追加新风除湿机
PID `gcej0ye8rj60qiee` 的产品描述。DP 定义来自云端产品模型，并以真实设备的
LAN 状态核对；不代表所有控制均已实机验证，DP 2、12 仍未映射。
滤网重置属于维护操作，按钮默认禁用，不自动执行。
主机地址、设备 ID、`local_key` 与配置条目由使用方在运行时提供，不随软件包发布。

软件包同时修复配置选项更新的重载生命周期：经 Home Assistant 的
`config_entries.async_reload` 卸载、加载，并在条目卸载时注销更新监听器，
避免绕过核心状态机或重复注册监听器。

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
  {
    name = "edge";
    mode = "multicast";
    sports = true;
    bbc = true;
    udpxy = "proxy.example:4022";
    outputPath = "/srv/iptv/edge-sports-bbc.m3u";
  }
];
```

上例生成 `iptv-multicast-edge-sync`、`iptv-multicast-edge-sports-sync`、
`iptv-multicast-edge-sports-bbc-sync` 三组 oneshot service 与 timer，共用模块的订阅源、
过滤和定时配置。`sports`/`bbc` 是逐输出的可选补充源开关（订阅地址与过滤规则分别在
`services.iptv-merge.sports` / `services.iptv-merge.bbc` 配置），追加序固定为
基础 → 体育 → BBC，单元名按 `-sports`、`-bbc` 后缀递进；两者失败语义相同——源不可用
或校验不过时保留旧的完整清单，不发布缺少补充频道的半成品。
体育 / BBC 附加源按完整 M3U 记录过滤和追加，保留每频道的 `#EXTVLCOPT`、
`#EXTHTTP` 等播放选项，不把选项丢弃或串到下一频道；文件级前导行不作为频道选项追加。
`name` 默认 `null`，不改变原有五模式及体育变体的十个单元名。
显式标识只允许小写字母、数字和分段用的单连字符；输出路径与最终单元名均须唯一，
完整 service 单元名最长 255 字节。名称碰撞按最终结果检查：
例如普通组播的 `name = "sports"` 会与未命名的体育组播输出冲突。

### 英国频道清单辅助工具

`pkgs.iptv-merge.ukPlaylist` 提供 `iptv-uk-sync`，在 US 上处理源清单，
英国机器只提供出口 HTTP 代理。例如：

```sh
iptv-uk-sync \
  --source https://iptv-org.github.io/iptv/countries/uk.m3u \
  --output-dir /var/lib/iptv-uk \
  --proxy http://uk-proxy.example:3128 \
  --workers 4 --timeout 8 --min-height 720 \
  --bbc-service BBCOne.uk=bbc_one_london \
  --bbc-service BBCNews.uk=bbc_news_channel_hd \
  --exclude-id BBCNewsPashto.uk
```

`--workers`、`--timeout`、`--min-height` 的默认值分别为 4、8 秒、720。
显式代理不受 `NO_PROXY` 绕过。筛选依据是实际解码出的视频帧高度，不是频道名中的
“HD”、容器声明的高度或非空 packet；只有音频帧不能证明视频可播。媒体前缀每路最多
2 MiB，本地解码最长 10 秒；无法解码出视频帧、高度未知或低于门槛的流不入选。
BBC One、BBC Two 各自合并地区和画质变体，只保留一个频道；
BBC Scotland、时移频道仍是独立身份。

最终 UK 频道在选优、去重后按清理后的显示名、不区分大小写排序，不受上游顺序或
BBC API 候选追加位置影响。显示名移除圆括号 / 方括号附注（画质、国家、
`Geo-blocked`、`Not 24/7` 等）并整理空白；`(+1)` 保留为 `+1`，不合并同名的不同身份。
已有 `tvg-name` 与显示名同步，频道 ID、分组、播放 URL 和请求头不变；
`report.json` 保留原始名称及源索引用于追溯。组合订阅只改变追加的 UK 区块，
不会对前面的运营商或体育频道重新排序。

HLS 会追踪变体、音视频 rendition、重定向、密钥与媒体请求的主机；公开可获取的
16 字节 AES-128 密钥允许使用，但拒绝 DRM。明文 DASH 必须实际取得初始化段和
媒体段，并检查所有可选音视频 representation（最多 32 个，超过则拒绝）。
IP 地址形式的播放依赖不支持仅靠 DNS 的分流，HLS Content Steering 也不支持，
因此这两类不会作为已完整解析的可播放候选发布。

可重复的 `--bbc-service TVG_ID=SERVICE` 从 BBC 官方 mediaselector 获取 H.264 HTTPS
HLS 候选，验证后优先使用官方服务及稳定主 CDN；未配置时保留原有上游选择方式。
`--bbc-api-template` 可覆盖带 `{service}` 占位符的 API 地址。
`--exclude-id TVG_ID` 可重复，按不含 `@` 变体后缀的身份、不区分大小写排除，
在请求媒体前生效；上例恢复英语 BBC News，并排除独立身份 BBC News Pashto。

显式播放请求头在探测与导出时保持一致：识别旧 `http-referrer`、
APTV 示例使用的 `http-referer` / `http-header` 以及 `http-user-agent`，
保留原逐频道选项，并导出 Referer / User-Agent 的 APTV 行内字段。
不会把默认探测 User-Agent 伪装成源声明的播放要求。
EXTINF、VLC、EXTHTTP、Kodi 与 URL 尾部若声明相互矛盾的同名头则拒绝该候选，
头名称不区分大小写，相同重复声明允许保留。

输出目录是指向同代产物的原子切换符号链接，内含 `tv.m3u`、
`tv-domains.txt` 与筛选报告 `report.json`；已有的真实目录会被拒绝，不会覆盖。
新一代必须同时含 BBC One、BBC Two、所有 `--bbc-service` 配置的频道身份，
且至少有 10 个符合条件的唯一频道，否则失败并保留全部旧产物。
域名文件只收录最终选中频道的精确播放依赖主机，
不加入台标、源目录网站或宽泛的共享域名后缀。

## RouterOS 电视域名分流

`services.rulesSync.tvDomains` 是静态电视域名列表；可选的
`services.rulesSync.tvDomainsFile`（默认 `null`）从运行时文件逐行读取域名，
例如设置为上述 `/var/lib/iptv-uk/tv-domains.txt`。配置文件输入时，两者取并集；
文件不可读则在发布前失败，不用残缺输入替换旧输出。要移除电视域名，须从所有
已启用的输入中移除，不能只清空静态列表而留下运行时文件中的同名项。

同一并集生成 mihomo 的 `rules/tv.yaml`、独立小文件 `tv.rsc`，并参与完整的
`domain.rsc` 与 DNSmasq 输出。生成阶段即从 GFW 域名集合中排除电视精确域名，
避免仅依赖 RouterOS 导入顺序决定优先级；不扩大为整个共享后缀的优先级。
RouterOS 电视记录使用 `type=FWD`、`forward-to=clash`、`comment=tv`，
不设置 `address-list`。

完整 `domain.rsc` 导入先清理 `gfw`、`tv` 两个托管组，再添加新一代规则。
独立 `tv.rsc` 只替换旧 `tv` 组，并在添加每个新电视精确域名前删除已有的同名
DNS 记录，因此会替换同名的旧 GFW 或手工记录，但保留其他 GFW 和手工记录。
完整 → 电视、电视 → 完整两种导入顺序均可重复，电视优先且不会因重复域名报错；
空电视集合也能安全清理旧 `tv` 组。

电视客户端必须经站点的 DNS 转发拿到 mihomo Fake-IP，再由网关路由到
mihomo；仅通过显式 HTTP/SOCKS 代理访问成功，不能证明这条透明播放链路正常。

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
