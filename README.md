# omp maxwork 全力模式扩展

一个 [omp](https://github.com/oh-my-pi/omp)（Oh My Pi）扩展：在 omp CLI 或 ompweb 里用 `/maxwork on` 一键进入「全力模式」——**开启/关闭/帮助全部零 LLM、秒回**，不浪费 token 在模式切换本身。

开启后：

1. **最高模型**：按角色回退链（默认 `@slow` → `@default`）取第一个已认证可用的模型并拉满 thinking（跨会话自动应用；`/maxwork off` 恢复之前的模型）
2. **可用性预检 + 回退 + 通知**：advisor 配置、codex CLI 登录态逐项检查；不可用明确 `⚠️` 通知，不静默降级
3. **每轮注入执行协议**：之后**直接输入任务**即可——每个普通 prompt 自动带上执行协议（codex 编排委派 或 omp 多模型并行）

## 组成

| 文件 | 作用 |
|---|---|
| `extension/maxwork.ts` | omp 扩展：on/off 状态机、模型切换与恢复、预检通知、per-prompt 协议注入、`/maxwork setup|status` |
| `module.nix` | NixOS module：全部环境相关值抽成 `services.ompMaxwork.*` options |
| `flake.nix` | 输出 `nixosModules.default` |

**公开安全**：扩展零硬编码，所有路径/角色/目录都是 option 或配置 JSON 渲染值。本仓库不包含任何主机路径、provider、密钥信息。

## 前置条件

- omp（extensions 自动发现，`PI_CODING_AGENT_DIR` 下的 `extensions/`）
- advisor 依赖全局配置（扩展只预检不修改）：
  ```yaml
  # <agentDir>/config.yml
  modelRoles:
    advisor: <provider>/<model>:<level>
  advisor:
    enabled: true
  ```
- codex 委派模式需要 [codex CLI](https://github.com/openai/codex) 已登录；不可用时预检回退并通知

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

`nixos-rebuild switch` 后激活（activation 幂等安装 `extensions/maxwork.ts` 与 `maxwork.config.json`；`maxwork-state.json` 是运行时状态，不受管理）。

非 Nix 用户：把 `extension/maxwork.ts` 放到 `<agentDir>/extensions/`，并参考 `module.nix` 的 JSON 结构写 `<agentDir>/maxwork.config.json`。

## 使用

| 输入 | 行为 | 耗时 |
|---|---|---|
| `/maxwork` | 显示当前状态与全部子命令 | 即时，零 token |
| `/maxwork on` | 开启：切最高模型 + 预检 + 通知 | ~1s，零 token |
| `/maxwork off` | 关闭：恢复开启前的模型 | 即时，零 token |
| `/maxwork setup [omp\|codex]` | 设置委派模式（不带参数弹选择框） | 即时，零 token |
| `/maxwork status` | 状态与三项可用性自检 | ~1s，零 token |

两种委派模式：

- **omp**：只用 omp 内可用 API，多模型并行 task subagents
- **codex**：omp 做 orchestrator，独立子任务委派 codex CLI（委派前复核登录态，失败收回自己做）

## 升级兼容性

扩展文件住在 agent 目录（用户数据区），omp 包升级不触碰；单个扩展加载失败有隔离，不影响主程序。扩展 API 无硬性 semver 保证，omp 大版本升级后请用 `/maxwork status` 快速自检一次。

## 许可

MIT
