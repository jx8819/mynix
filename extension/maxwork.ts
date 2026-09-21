// maxwork — 「全力模式」开关扩展
// /maxwork          显示状态与可用子命令（零 LLM，秒回）
// /maxwork on       开启：切最高模型（回退链）+ 预检 advisor/codex + 通知（零 LLM）
// /maxwork off      关闭：恢复开启前的模型
// /maxwork setup    交互/直接设置委派模式（omp = 仅 omp 内 API，codex = omp+codex）
// /maxwork status   完整状态与可用性检查
//
// 开启是持久状态（maxwork-state.json）：之后直接输入任务即可，
// 每个普通 prompt 自动注入执行协议（codex 委派或 omp 并行），跨会话生效。
//
// 环境相关值全部来自 <agentDir>/maxwork.config.json（可由 Nix 渲染），
// 本文件零环境硬编码，可公开分发。

import type {
	ExtensionAPI,
	ExtensionCommandContext,
	Model,
} from "@oh-my-pi/pi-coding-agent";
import { homedir } from "node:os";
import { join } from "node:path";

type Mode = "omp" | "codex";

interface MaxworkConfig {
	command: string;
	modelRoles: string[]; // 按优先级回退链
	thinkingLevel: string;
	defaultMode: Mode;
	codex: {
		home: string;
		outDir: string;
		tmpdir: string;
		timeoutSec: number;
	};
}

interface MaxworkState {
	enabled?: boolean;
	mode?: Mode;
	prevModelId?: string;
}

const DEFAULTS: MaxworkConfig = {
	command: "maxwork",
	modelRoles: ["@slow", "@default"],
	thinkingLevel: "max",
	defaultMode: "codex",
	codex: { home: "", outDir: "", tmpdir: "/tmp", timeoutSec: 300 },
};

interface CheckResult {
	ok: boolean;
	detail: string;
}

interface ModelCheck {
	role: string;
	model: Model | null;
	fellBack: boolean;
}

// 多处调用点共用同一基准目录，保留具名函数。
function agentDir(): string {
	return process.env.PI_CODING_AGENT_DIR ?? join(homedir(), ".omp", "agent");
}

function parseConfig(raw: unknown): Partial<MaxworkConfig> {
	if (!raw || typeof raw !== "object") return {};
	const o = raw as Record<string, unknown>;
	const out: Partial<MaxworkConfig> = {};
	if (typeof o.command === "string") out.command = o.command;
	if (Array.isArray(o.modelRoles)) {
		out.modelRoles = o.modelRoles.filter(
			(r): r is string => typeof r === "string",
		);
	}
	if (typeof o.thinkingLevel === "string") out.thinkingLevel = o.thinkingLevel;
	if (o.defaultMode === "omp" || o.defaultMode === "codex") {
		out.defaultMode = o.defaultMode;
	}
	if (o.codex && typeof o.codex === "object") {
		const c = o.codex as Record<string, unknown>;
		out.codex = { ...DEFAULTS.codex };
		if (typeof c.home === "string") out.codex.home = c.home;
		if (typeof c.outDir === "string") out.codex.outDir = c.outDir;
		if (typeof c.tmpdir === "string") out.codex.tmpdir = c.tmpdir;
		if (typeof c.timeoutSec === "number") out.codex.timeoutSec = c.timeoutSec;
	}
	return out;
}

async function loadConfig(): Promise<MaxworkConfig> {
	const raw: unknown = await Bun.file(
		join(agentDir(), "maxwork.config.json"),
	)
		.json()
		.catch(() => null);
	const user = parseConfig(raw);
	return { ...DEFAULTS, ...user, codex: { ...DEFAULTS.codex, ...user.codex } };
}

async function loadState(cfg: MaxworkConfig): Promise<Required<MaxworkState>> {
	const raw: unknown = await Bun.file(join(agentDir(), "maxwork-state.json"))
		.json()
		.catch(() => null);
	const o = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
	const mode = o.mode;
	return {
		enabled: o.enabled === true,
		mode: mode === "omp" || mode === "codex" ? mode : cfg.defaultMode,
		prevModelId: typeof o.prevModelId === "string" ? o.prevModelId : "",
	};
}

async function saveState(patch: MaxworkState): Promise<void> {
	const cfg = await loadConfig();
	const cur = await loadState(cfg);
	await Bun.write(
		join(agentDir(), "maxwork-state.json"),
		JSON.stringify({ ...cur, ...patch }, null, 2) + "\n",
	);
}

function checkModel(
	ctx: ExtensionCommandContext,
	cfg: MaxworkConfig,
): ModelCheck {
	const available = new Set<string>();
	for (const m of ctx.models.list() ?? []) {
		if (typeof m.id === "string") available.add(m.id);
	}
	for (let i = 0; i < cfg.modelRoles.length; i++) {
		const role = cfg.modelRoles[i];
		const m = ctx.models.resolve(role);
		if (m && (available.size === 0 || available.has(m.id))) {
			return { role, model: m, fellBack: i > 0 };
		}
	}
	return { role: "(current)", model: null, fellBack: true };
}

async function checkAdvisor(): Promise<CheckResult> {
	const text = await Bun.file(join(agentDir(), "config.yml"))
		.text()
		.catch(() => "");
	const hasRole = /modelRoles:[\s\S]*?^\s+advisor:/m.test(text);
	const hasEnabled = /^advisor:\s*\n\s+enabled:\s*true/m.test(text);
	if (hasRole && hasEnabled) {
		return { ok: true, detail: "advisor.enabled=true 且 modelRoles.advisor 已配置" };
	}
	return {
		ok: false,
		detail: `config.yml 未完整配置 advisor（role=${hasRole}, enabled=${hasEnabled}）`,
	};
}

async function checkCodex(cfg: MaxworkConfig): Promise<CheckResult> {
	if (!cfg.codex.home) return { ok: false, detail: "codex.home 未配置" };
	try {
		const proc = Bun.spawn({
			cmd: ["codex", "login", "status"],
			env: {
				...process.env,
				CODEX_HOME: cfg.codex.home,
				TMPDIR: cfg.codex.tmpdir,
			},
			stdout: "pipe",
			stderr: "pipe",
		});
		const out =
			(await new Response(proc.stdout).text()) +
			(await new Response(proc.stderr).text());
		const rc = await proc.exited;
		if (rc === 0 && /logged in/i.test(out)) {
			return { ok: true, detail: out.trim() };
		}
		return {
			ok: false,
			detail: `codex login status rc=${rc}: ${out.trim().slice(0, 120)}`,
		};
	} catch (e) {
		return { ok: false, detail: `codex 不可用: ${String(e)}` };
	}
}

function applyModel(pi: ExtensionAPI, mc: ModelCheck, thinkingLevel: string): void {
	if (!mc.model) return;
	pi.setModel(mc.model);
	try {
		pi.setThinkingLevel(thinkingLevel);
	} catch {
		// 模型不支持该 thinking 级别时忽略
	}
}

function protocolFor(mode: Mode): string {
	return mode === "codex"
		? `[maxwork ON] 本任务按全力协议执行：你是 orchestrator——先分解任务；独立、单目录、只读可完成的子任务按 delegate-to-codex 协议委派（委派前先用 codex login status（带 CODEX_HOME）复核可用；失败（401/quota/超时/空产出）立即收回自己做并向用户说明）。关键路径、落盘、部署、验证由你亲自完成。允许多路并行（task subagents + codex）。`
		: `[maxwork ON] 本任务按全力协议执行：充分利用并行 task subagents 分解执行；重要改动先验证再落盘。`;
}

export default function maxwork(pi: ExtensionAPI) {
	pi.setLabel("Maxwork 全力模式");

	pi.on("session_start", async (_event, ctx) => {
		const cfg = await loadConfig();
		const prefix = `/${cfg.command}`;

		// 跨会话持久：开启状态下新会话自动应用最高模型，并预装协议注入
		//（nextTurn 在下一条 prompt 生效；首条任务因此也能拿到协议）。
		const boot = await loadState(cfg);
		if (boot.enabled) {
			const mc = checkModel(ctx, cfg);
			applyModel(pi, mc, cfg.thinkingLevel);
			pi.sendMessage(protocolFor(boot.mode), { deliverAs: "nextTurn" });
			ctx.ui.notify(
				`[maxwork] 开启中（模式: ${boot.mode}，模型: ${mc.role}）——/maxwork off 关闭`,
				"info",
			);
		}

		// 开启状态下，每个普通 prompt 注入执行协议（nextTurn 随下轮生效）。
		pi.on("input", async (event, _ctx) => {
			const st = await loadState(cfg);
			if (!st.enabled) return;
			const text = event.text ?? "";
			if (text.startsWith("/")) return;
			pi.sendMessage(protocolFor(st.mode), { deliverAs: "nextTurn" });
		});

		pi.registerCommand(cfg.command, {
			description: "全力模式开关：on 开启 / off 关闭 / setup 模式 / status 状态",
			handler: async (args, ctx) => {
				const [sub, ...rest] = args.trim().split(/\s+/).filter(Boolean);

				if (sub === "on") {
					const st = await loadState(cfg);
					const prev = ctx.models.current();
					const mc = checkModel(ctx, cfg);
					applyModel(pi, mc, cfg.thinkingLevel);
					await saveState({
						enabled: true,
						prevModelId: prev && typeof prev.id === "string" ? prev.id : st.prevModelId,
					});
					const notes: string[] = [
						mc.model
							? mc.fellBack
								? `⚠️ 首选模型不可用，已回退到 ${mc.role}`
								: `模型已切至 ${mc.role}（thinking=${cfg.thinkingLevel}）`
							: "⚠️ 回退链上无可用模型，保持当前模型",
					];
					const adv = await checkAdvisor();
					if (!adv.ok) notes.push(`⚠️ ${adv.detail}`);
					if (st.mode === "codex") {
						const cx = await checkCodex(cfg);
						if (!cx.ok) {
							notes.push(`⚠️ codex 不可用（${cx.detail}）——委派任务时将全部自行完成`);
						}
					}
					ctx.ui.notify(
						`[maxwork ON] ${notes.join("；")}。模式: ${st.mode}。直接输入任务即可；/${cfg.command} off 关闭`,
						"info",
					);
					return;
				}

				if (sub === "off") {
					const st = await loadState(cfg);
					let restored = "";
					if (st.prevModelId) {
						const m = ctx.models.resolve(st.prevModelId);
						if (m) {
							pi.setModel(m);
							restored = `，模型已恢复到 ${st.prevModelId}`;
						}
					}
					await saveState({ enabled: false });
					ctx.ui.notify(`[maxwork OFF] 已关闭${restored}`, "info");
					return;
				}

				if (sub === "setup") {
					const m = rest[0];
					if (m === "omp" || m === "codex") {
						await saveState({ mode: m });
						ctx.ui.notify(`maxwork 委派模式已设为「${m}」`, "info");
						return;
					}
					const st = await loadState(cfg);
					if (rest.length === 0 && ctx.hasUI) {
						try {
							const choice = await ctx.ui.select(
								`maxwork 委派模式（当前: ${st.mode}）`,
								[
									"omp    — 只用 omp 内可用 API（多模型并行）",
									"codex  — omp + codex CLI 一起上",
								],
							);
							if (choice) {
								await saveState({
									mode: choice.startsWith("codex") ? "codex" : "omp",
								});
								ctx.ui.notify("maxwork 委派模式已保存", "info");
							}
							return;
						} catch {
							// 对话框不可用时落到用法提示
						}
					}
					ctx.ui.notify(
						`当前委派模式: ${st.mode}。用法: /${cfg.command} setup omp|codex`,
						"info",
					);
					return;
				}

				if (sub === "status") {
					const st = await loadState(cfg);
					const mc = checkModel(ctx, cfg);
					const adv = await checkAdvisor();
					const cx =
						st.mode === "codex"
							? await checkCodex(cfg)
							: { ok: true, detail: "(omp 模式，跳过)" };
					ctx.ui.notify(
						[
							`状态: ${st.enabled ? "ON" : "OFF"}（委派模式: ${st.mode}）`,
							`模型: ${mc.role}${mc.fellBack ? "（回退）" : ""} ${mc.model ? "✓" : "✗ 无可用"}`,
							`advisor: ${adv.ok ? "✓" : "✗"} ${adv.detail}`,
							`codex: ${cx.ok ? "✓" : "✗"} ${cx.detail}`,
						].join("\n"),
						"info",
					);
					return;
				}

				// bare /maxwork 与未知子命令：只给选项，不启动任何任务。
				const st = await loadState(cfg);
				ctx.ui.notify(
					[
						`maxwork 当前: ${st.enabled ? "ON" : "OFF"}（委派模式: ${st.mode}）`,
						`/${cfg.command} on       开启（切最高模型 + 预检）`,
						`/${cfg.command} off      关闭（恢复原模型）`,
						`/${cfg.command} setup    设置委派模式（omp|codex）`,
						`/${cfg.command} status   可用性自检`,
						`开启后直接输入任务即可，无需前缀。`,
					].join("\n"),
					"info",
				);
			},
		});
	});
}
