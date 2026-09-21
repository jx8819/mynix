# mynix — 共享 Nix 库

公开仓库，收录可复用的 Nix 资产；**机制公开，私密值全部抽成 options**（仓库内零主机路径拓扑、零 provider、零密钥）。

## 内容

| 资产 | 说明 |
|---|---|
| `extension/maxwork.ts` + `module.nix` | omp maxwork 全力模式扩展（`/maxwork on` 一键最高模型 + advisor 预检 + codex 联动，零 LLM 秒切） |
| `modules/ompweb.nix` | [ompweb](https://github.com/kahme247/ompweb)（OMP coding agent 的 Web UI）NixOS module |
| `pkgs/mk-exporter` | [mktxp](https://github.com/akpw/mktxp) 1.2.9 — MikroTik RouterOS Prometheus exporter |
| `pkgs/nut-exporter` | [prometheus-nut-exporter](https://github.com/HON95/prometheus-nut-exporter) 1.2.1 — NUT UPS exporter |
| `pkgs/perftest` | [linux-rdma/perftest](https://github.com/linux-rdma/perftest) 26.04.17 — RDMA 性能测试工具集（nixpkgs 无此包） |
| `pkgs/sas3ircu` | Broadcom SAS3IRCU P16 — SAS HBA 管理工具（nixpkgs 无此包） |
| `pkgs/yacd-meta` | [Yacd-meta](https://github.com/MetaCubeX/Yacd-meta) **0.3.8（钉住勿升：新版有 bug）** — mihomo external-ui |
| `pkgs/ompweb` | @kahme247/ompweb 0.5.0（npm registry tarball，免 vendor） |

## 使用

```nix
inputs.mynix.url = "github:jx8819/mynix";

# 1) overlay 消费包：
nixpkgs.overlays = [ inputs.mynix.overlays.default ];
# 然后 pkgs.perftest / pkgs.yacd-meta / ...

# 2) NixOS modules：
imports = [
  inputs.mynix.nixosModules.default  # services.ompMaxwork（maxwork 扩展）
  inputs.mynix.nixosModules.ompweb   # services.ompweb
];

# 3) 临时跑：
# nix run github:jx8819/mynix#perftest
```

## ompweb module 前置条件

- `services.ompweb.sopsFile`：**必填**（密码属调用方私有，本仓库不提供默认值）
- `services.ompweb.nodeModulesDir`（默认 `/opt/ompweb/lib/node_modules`）：需预先 `npm install` 出 `@kahme247/ompweb` 的完整依赖树（Next 依赖不随 npm 包发布）
- 生产部署应把它放在带 TLS+认证的反代（caddy/nginx）之后；`hostname` 默认已收紧为 `127.0.0.1`

## maxwork 扩展

`/maxwork on` 一键进入全力模式（omp CLI / ompweb 通用）：切最高模型（角色回退链）+ advisor/codex 预检（不可用回退并通知）+ 之后直接输入任务、每轮自动注入执行协议（codex 编排委派 或 omp 多模型并行）。

| 输入 | 行为 | 耗时 |
|---|---|---|
| `/maxwork` | 显示当前状态与全部子命令 | 即时，零 token |
| `/maxwork on` | 开启：切最高模型 + 预检 + 通知 | ~1s，零 token |
| `/maxwork off` | 关闭：恢复开启前的模型 | 即时，零 token |
| `/maxwork setup [omp\|codex]` | 设置委派模式（不带参数弹选择框） | 即时，零 token |
| `/maxwork status` | 状态与三项可用性自检 | ~1s，零 token |

安装：

```nix
imports = [ inputs.mynix.nixosModules.default ];

services.ompMaxwork = {
  enable = true;
  agentDir = "/path/to/omp/agent";   # 你的 PI_CODING_AGENT_DIR
  owner = "youruser";                # 运行 omp 的用户
  group = "youruser";
  codex = {
    home = "/path/to/codex/home";
    outDir = "/path/to/codex-out";
    tmpdir = "/path/to/writable-tmp";
  };
};
```

`nixos-rebuild switch` 后激活（activation 幂等安装 `extensions/maxwork.ts` 与 `maxwork.config.json`；`maxwork-state.json` 是运行时状态，不受管理）。

非 Nix 用户：把 `extension/maxwork.ts` 放到 `<agentDir>/extensions/`，并参考 `module.nix` 的 JSON 结构写 `<agentDir>/maxwork.config.json`。

advisor 依赖全局配置（扩展只预检不修改）：

```yaml
# <agentDir>/config.yml
modelRoles:
  advisor: <provider>/<model>:<level>
advisor:
  enabled: true
```

codex 委派模式需要 [codex CLI](https://github.com/openai/codex) 已登录（`CODEX_HOME` 指向含 auth.json 的目录）；不可用时自动回退 omp-only 并通知。

## 升级兼容性

扩展文件住在 agent 目录（用户数据区），omp 包升级不触碰；单个扩展加载失败有隔离，不影响主程序。扩展 API 无硬性 semver 保证，omp 大版本升级后用 `/maxwork status` 自检。

## 许可

MIT
