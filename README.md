# omp maxwork 全力模式扩展

一个 [omp](https://github.com/oh-my-pi/omp)（Oh My Pi）扩展：在 omp CLI 或 ompweb 里输入 `/maxwork <任务>`，一键进入「全力模式」——

1. **切换最高模型**：按配置的角色回退链（默认 `@slow` → `@default`）取第一个已认证可用的模型，并拉满 thinking 级别
2. **组件可用性预检**：检查 advisor 配置与 codex CLI 登录态；不可用时**回退并明确通知**（不会静默降级）
3. **codex CLI 联动（可选）**：进入 omp 编排 + codex 委派的多路并行执行协议

## 组成

| 文件 | 作用 |
|---|---|
| `extension/maxwork.ts` | omp 扩展：`input` 监听（同步切模型 + 异步预检通知）+ 免 token 管理命令 `/maxwork-setup`、`/maxwork-status` |
| `extension/maxwork.md` | 文件型斜杠命令模板：`/maxwork <任务>` 的 prompt 展开与执行协议（含 setup/status 管理分支） |
| `module.nix` | NixOS module：全部环境相关值抽成 `services.ompMaxwork.*` options |
| `flake.nix` | 输出 `nixosModules.default` |

**公开安全**：扩展与模板零硬编码，所有路径/角色/目录都是 option 或配置 JSON 渲染值。本仓库不包含任何主机路径、provider、密钥信息。

## 前置条件

- omp（extensions 与 commands 自动发现，`PI_CODING_AGENT_DIR` 下的 `extensions/`、`commands/`）
- advisor 依赖全局配置（扩展只预检不修改）：
  ```yaml
  # <agentDir>/config.yml
  modelRoles:
    advisor: <provider>/<model>:<level>
  advisor:
    enabled: true
  ```
- codex 模式需要 [codex CLI](https://github.com/openai/codex) 已登录（`CODEX_HOME` 指向含 auth.json 的目录）；不可用时自动回退 omp-only 并通知

## 安装（NixOS / flakes）

```nix
# flake.nix
inputs.mynix.url = "github:jx8819/mynix";

# 配置
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

`nixos-rebuild switch` 后激活（activation 幂等安装三个文件：`extensions/maxwork.ts`、`commands/maxwork.md`、`maxwork.config.json`）。

非 Nix 用户：手动把 `extension/maxwork.ts` 放到 `<agentDir>/extensions/`、`extension/maxwork.md` 放到 `<agentDir>/commands/maxwork.md`（把 `@agentDir@`、`@command@` 替换为实际值），并参考 `module.nix` 的 JSON 结构写 `<agentDir>/maxwork.config.json`。

## 使用

| 输入 | 行为 |
|---|---|
| `/maxwork <任务>` | 切模型 + 预检通知 + 按当前模式执行任务 |
| `/maxwork-setup` | 交互选择模式（omp 内多模型 / omp+codex） |
| `/maxwork-setup codex` | 直接设置模式（持久化到 `maxwork-state.json`，免 token） |
| `/maxwork-status` | 显示模式与模型/advisor/codex 可用性（免 token） |
| `/maxwork setup mode omp` | （模板分支，经 agent 写状态文件；推荐用免 token 的 `/maxwork-setup`） |

两种模式：

- **omp**：只用 omp 内可用 API，多模型并行 task subagents
- **codex**：omp 做 orchestrator，独立子任务委派 codex CLI（委派前复核登录态，失败收回自己做）

## 升级兼容性

扩展文件住在 agent 目录（用户数据区），omp 包升级不触碰；omp 对单个扩展加载失败做了隔离（不影响主程序）。扩展 API 无硬性 semver 保证，omp 大版本升级后请用 `/maxwork-status` 快速自检一次。

## 许可

MIT
