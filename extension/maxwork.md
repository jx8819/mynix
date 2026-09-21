---
description: 全力模式：最高强度执行任务（maxwork 扩展已自动切模型并做可用性检查）
---

[maxwork 模式] 以最高强度执行（模型档位已由 maxwork 扩展切换并通知，advisor 全局在岗）。

先读模式状态文件 `@agentDir@/maxwork-state.json`（不存在则读 `@agentDir@/maxwork.config.json` 的 defaultMode）：

- **mode=codex**：你是 orchestrator——先分解任务；独立、单目录、只读可完成的子任务按 delegate-to-codex 协议委派（委派前必须先用 `codex login status`（带 CODEX_HOME）复核可用；不可用（401/quota/超时/空产出）则全部收回自己做并向用户说明）。关键路径、落盘、部署、验证由你亲自完成。允许多路并行（task subagents + codex）。
- **mode=omp**：不使用 codex。充分利用并行 task subagents 分解执行；重要改动先验证再落盘。

例外：若任务文本精确匹配 `setup mode omp|codex`，忽略上述协议——改为把 `{"mode":"<值>"}` 写入 `@agentDir@/maxwork-state.json` 并简短确认；若为 `status`，改为读取 state/config 文件并运行 codex 可用性检查后简短汇报（这些管理动作也可由免 token 的 /@command@-setup、/@command@-status 扩展命令完成）。

任务：$ARGUMENTS
