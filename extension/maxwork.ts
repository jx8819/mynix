// maxwork — 「全力模式」companion 扩展
// 职责：
//   1. 监听 input：用户输入 /maxwork ... 时，同步切换模型（回退链）+ 异步预检
//      advisor/codex 可用性并通知（不满足「check 可用性，不能用时回退并通知」）。
//   2. 提供免 token 管理命令：/maxwork-setup [mode omp|codex]、/maxwork-status。
// /maxwork 本身是 commands/maxwork.md 的文件命令（负责把任务展开进 prompt 流），
// 与本扩展配套；两者共用 maxwork.config.json / maxwork-state.json。
//
// 所有环境相关值来自 <agentDir>/maxwork.config.json（可由 Nix 渲染），
// 本文件不含任何环境硬编码，可公开分发。

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

// 4 处以上调用点需要同一基准目录，保留具名函数。
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

async function loadMode(cfg: MaxworkConfig): Promise<Mode> {
	const raw: unknown = await Bun.file(join(agentDir(), "maxwork-state.json"))
		.json()
		.catch(() => null);
	const mode =
		raw && typeof raw === "object"
			? (raw as Record<string, unknown>).mode
			: undefined;
	return mode === "omp" || mode === "codex" ? mode : cfg.defaultMode;
}

async function saveMode(mode: Mode): Promise<void> {
	await Bun.write(
		join(agentDir(), "maxwork-state.json"),
		JSON.stringify({ mode }, null, 2) + "\n",
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

export default function maxwork(pi: ExtensionAPI) {
	pi.setLabel("Maxwork 全力模式");

	pi.on("session_start", async () => {
		const cfg = await loadConfig();
		const prefix = `/${cfg.command}`;

		pi.on("input", async (event, ctx) => {
			const text = event.text ?? "";
			if (!text.startsWith(prefix + " ") && text !== prefix) return;
			// setup/status 管理输入不触发激活检查（由模板或管理命令处理）。
			const rest = text.slice(prefix.length).trim();
			if (rest === "" || rest === "status" || rest.startsWith("setup")) return;

			// 同步部分：立即切模型（回退链），保证本轮请求就用新模型。
			const mc = checkModel(ctx, cfg);
			if (mc.model) {
				pi.setModel(mc.model);
				try {
					pi.setThinkingLevel(cfg.thinkingLevel);
				} catch {
					// 模型不支持该 thinking 级别时忽略
				}
			}

			// 异步部分：advisor/codex 预检，只影响提示，不阻塞本轮。
			const notes: string[] = [
				mc.model
					? mc.fellBack
						? `⚠️ 首选模型不可用，已回退到 ${mc.role}`
						: `模型已切至 ${mc.role}（thinking=${cfg.thinkingLevel}）`
					: "⚠️ 回退链上无可用模型，保持当前模型",
			];
			const adv = await checkAdvisor();
			if (!adv.ok) notes.push(`⚠️ ${adv.detail}`);
			const mode = await loadMode(cfg);
			if (mode === "codex") {
				const cx = await checkCodex(cfg);
				if (!cx.ok) {
					notes.push(
						`⚠️ codex 预检不可用（${cx.detail}）——agent 委派前会复核，若仍不可用将自行完成全部工作`,
					);
				}
			}
			ctx.ui.notify(`[maxwork] ${notes.join("；")}（模式: ${mode}）`, "info");
		});

		pi.registerCommand(`${cfg.command}-setup`, {
			description: "设置 maxwork 模式（omp = 仅 omp 内 API，codex = omp+codex CLI）",
			handler: async (args, ctx) => {
				const [, m] = args.trim().split(/\s+/);
				if (m === "omp" || m === "codex") {
					await saveMode(m);
					ctx.ui.notify(
						`maxwork 模式已设为「${m}」（持久化到 maxwork-state.json）`,
						"info",
					);
					return;
				}
				const current = await loadMode(cfg);
				if (args.trim() === "" && ctx.hasUI) {
					try {
						const choice = await ctx.ui.select(`maxwork 模式（当前: ${current}）`, [
							"omp    — 只用 omp 内可用 API（多模型并行）",
							"codex  — omp + codex CLI 一起上",
						]);
						if (choice) {
							await saveMode(choice.startsWith("codex") ? "codex" : "omp");
							ctx.ui.notify("maxwork 模式已保存", "info");
						}
						return;
					} catch {
						// 对话框不可用时落到用法提示
					}
				}
				ctx.ui.notify(
					`当前模式: ${current}。用法: /${cfg.command}-setup omp|codex`,
					"info",
				);
			},
		});

		pi.registerCommand(`${cfg.command}-status`, {
			description: "显示 maxwork 当前模式与模型/advisor/codex 可用性",
			handler: async (_args, ctx) => {
				const mode = await loadMode(cfg);
				const mc = checkModel(ctx, cfg);
				const adv = await checkAdvisor();
				const cx =
					mode === "codex"
						? await checkCodex(cfg)
						: { ok: true, detail: "(omp 模式，跳过)" };
				ctx.ui.notify(
					[
						`模式: ${mode}`,
						`模型: ${mc.role}${mc.fellBack ? "（回退）" : ""} ${mc.model ? "✓" : "✗ 无可用"}`,
						`advisor: ${adv.ok ? "✓" : "✗"} ${adv.detail}`,
						`codex: ${cx.ok ? "✓" : "✗"} ${cx.detail}`,
					].join("\n"),
					"info",
				);
			},
		});
	});
}
