# mynix

jx8819 的共享 Nix 库：`pkgs/` 打包可公开复用的软件，`modules/` 定义可复用的 NixOS 选项。部署编排、私密值与站点路径不在本仓库，留给调用方。

## Flake 输出

### nixosModules（11 个）

| 输出 | 说明 |
| --- | --- |
| `nixosModules.default` | maxwork 全力模式扩展（/maxwork） |
| `nixosModules.ompweb` | ompweb Web UI 服务 |
| `nixosModules.omp` | omp 编码代理服务 |
| `nixosModules.fan-control` | Supermicro IPMI 风扇调速 |
| `nixosModules.rules-sync` | ros-rules-generator 规则列表同步（systemd timer） |
| `nixosModules.mesh-guardian` | 小米 Mesh 有线组网恢复看门狗 |
| `nixosModules.hdsky-checkin` | HDSky（hdsky.me）自动签到 |
| `nixosModules.herdr` | Herdr 终端工作区管理器 |
| `nixosModules.home-assistant-plugins` | Home Assistant 自定义组件集合 |
| `nixosModules.miloco-omp-agent` | Miloco agent webhook → OMP 桥（仅米家设备/场景工具） |
| `nixosModules.lume-agent` | Lume VPS 探针监控 Agent |

### overlays.default

加进调用方的 `nixpkgs.overlays` 后直接用 `pkgs.<name>`；包清单在 `flake.nix` 单源声明，overlay 与 packages 同源生成。

### packages.x86_64-linux（18 个）

`nix run github:jx8819/mynix#<name>` 临时使用。

| 包 | 说明 |
| --- | --- |
| `nut-exporter` | Prometheus NUT（UPS）指标导出器 |
| `perftest` | InfiniBand verbs 性能测试工具集 |
| `sas3ircu` | Broadcom SAS3 IR 配置工具（闭源二进制，unfree） |
| `yacd-meta` | Yacd-meta 仪表盘（mihomo external-ui） |
| `ompweb` | OMP 编码代理 Web UI |
| `omp` | omp 终端编码代理（can1357/oh-my-pi 源码打包） |
| `ros-rules-generator` | 米家自动化规则列表同步 CLI（rules-sync 模块用） |
| `mesh-guardian` | 小米 Mesh 有线组网恢复看门狗 |
| `hdsky-checkin` | HDSky（hdsky.me）自动签到 |
| `oh-my-sage` | 米家高级自动化 MCP 服务 |
| `miloco-omp-agent` | Miloco agent webhook → 受限 OMP 桥（仅米家设备/场景工具） |
| `miot-mcp` | 米家设备 MCP 服务 |
| `ddddocr` | ddddocr 验证码识别 python 库（hdsky-checkin 的依赖） |
| `ha-dyson` | HA 自定义组件：Dyson（domain: dyson_local） |
| `ha-cololight` | HA 自定义组件：Cololight（domain: cololight） |
| `ha-xiaomi-home` | HA 官方小米集成（domain: xiaomi_home，unfree） |
| `ha-homeconnect-local` | HA 自定义组件：Home Connect Local（domain: homeconnect_ws） |
| `lume-agent` | Lume VPS 探针监控客户端（vpsmon-agent） |

## 公私边界

- 公开可复用：`pkgs/`（打包配方）与 `modules/`（NixOS 选项）；机制全部公开，可直接被任意 NixOS 配置引用。
- 留给调用方：部署编排（主机与服务组合）、私密值（Cookie、token、API key 等，经 sops 等注入 options）、站点路径与具体参数（IP、目录、阈值等）。
- unfree 许可：`ha-xiaomi-home`、`sas3ircu` 为 unfree，消费侧需要 `nixpkgs.config.allowUnfree`（或等价的 allowUnfreePredicate）。
