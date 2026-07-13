/**
 * Mockup Builder Extension for pi.
 *
 * Registers tools that orchestrate the mockup generation pipeline.
 * Each tool wraps a Python helper in app.workers.mockup_helpers.* so the
 * LLM gets structured JSON output (no raw build/deploy log dumps in context).
 *
 * Tools:
 *   mockup_load_lead        — pull lead context from DB
 *   mockup_clone_template   — git clone one of 3 template repos
 *   mockup_write_config     — write client.ts + brand.ts into template
 *   mockup_copy_assets      — copy scraped images, generate placeholders if missing
 *   mockup_build            — pnpm install + pnpm build
 *   mockup_deploy           — wrangler pages deploy + DNS A record
 *   mockup_verify           — programmatic checks (HTTP, images, trade-copy regression)
 *   mockup_write_approval   — insert approval row in prod admin DB
 *
 * Usage:
 *   pi -p "use the mockup-builder skill to generate a mockup for lead_id=<UUID>" \
 *      --extension ~/.pi/agent/extensions/mockup-builder.ts
 *
 * Iteration contract:
 *   - You have up to 3 build attempts. After 3 failed verify cycles, ship whatever
 *     you have and log the reason.
 *   - Use Playwright MCP (browser_navigate + browser_take_screenshot) to visually
 *     review each built mockup. mockup_verify gives you programmatic checks;
 *     your eyes give you visual fidelity.
 *   - If you need to modify config between iterations, use pi's read/edit tools
 *     on /tmp/cc_mockups/<slug>/src/config/{client,brand}.ts, then call
 *     mockup_build + mockup_deploy + mockup_verify again.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn } from "node:child_process";
import { writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// ─── Configuration ─────────────────────────────────────────────────────────

const PYTHON_BIN = process.env.MOCKUP_PYTHON_BIN ?? "/app/.venv/bin/python";
const PROJECT_ROOT = process.env.MOCKUP_PROJECT_ROOT ?? "/app";
const HELPER_MODULE = "app.workers.mockup_helpers.cli";
const DEFAULT_TIMEOUT_S = 300;
const BUILD_TIMEOUT_S = 240;
const DEPLOY_TIMEOUT_S = 120;

// ─── Helpers ───────────────────────────────────────────────────────────────

interface PythonResult {
	ok: boolean;
	error?: string;
	[key: string]: unknown;
}

async function callHelper(
	subcommand: string,
	args: string[],
	timeoutS: number = DEFAULT_TIMEOUT_S,
): Promise<PythonResult> {
	return new Promise((resolve, reject) => {
		const proc = spawn(PYTHON_BIN, ["-m", HELPER_MODULE, subcommand, ...args], {
			cwd: PROJECT_ROOT,
			timeout: timeoutS * 1000,
			env: { ...process.env, PYTHONPATH: PROJECT_ROOT },
		});

		let stdout = "";
		let stderr = "";
		proc.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
		proc.stderr.on("data", (d: Buffer) => (stderr += d.toString()));

		proc.on("error", (err) => reject(err));
		proc.on("close", (code) => {
			if (code !== 0) {
				resolve({
					ok: false,
					error: `helper ${subcommand} exited ${code}: ${stderr.slice(-400)}`,
					_stderr: stderr.slice(-400),
				});
				return;
			}
			try {
				const parsed = JSON.parse(stdout);
				resolve(parsed);
			} catch (e) {
				resolve({
					ok: false,
					error: `helper ${subcommand} returned non-JSON: ${stdout.slice(0, 200)}`,
					_raw_stdout: stdout.slice(0, 500),
				});
			}
		});
	});
}

function toolResult(text: string, details: Record<string, unknown> = {}) {
	return {
		content: [{ type: "text" as const, text }],
		details,
	};
}

// ─── Extension ─────────────────────────────────────────────────────────────

export default function mockupBuilderExtension(pi: ExtensionAPI) {
	// ─── mockup_load_lead ───────────────────────────────────────────────
	pi.registerTool({
		name: "mockup_load_lead",
		label: "Load Lead",
		description:
			"Load lead context (business info, scraped assets, audit results) from the leadgen DB. Call this FIRST before generating a mockup. Returns a JSON object with everything you need to design the config files.",
		parameters: Type.Object({
			lead_id: Type.String({ description: "Lead UUID" }),
		}),
		async execute(_id, params) {
			const result = await callHelper("load_lead", [params.lead_id]);
			const text = result.ok
				? `Lead loaded: ${(result.lead as any)?.business_name} (${(result.lead as any)?.business_type}, ${(result.lead as any)?.city})`
				: `Error: ${result.error}`;
			return toolResult(text, result);
		},
	});

	// ─── mockup_clone_template ──────────────────────────────────────────
	pi.registerTool({
		name: "mockup_clone_template",
		label: "Clone Template",
		description:
			"Clone one of 3 Astro template repos into the build directory. Templates: 'trades' (plumbers, electricians, construction), 'creative' (photographers, event planners), 'general' (balanced default).",
		parameters: Type.Object({
			template: Type.Union([
				Type.Literal("trades"),
				Type.Literal("creative"),
				Type.Literal("general"),
			]),
			slug: Type.String({ description: "URL-safe identifier (e.g. 'limelight-event-hire')" }),
		}),
		async execute(_id, params) {
			const result = await callHelper("clone_template", [params.template, params.slug]);
			const text = result.ok
				? `Cloned ${params.template} template to ${result.path} (${result.clone_ms}ms)`
				: `Error: ${result.error}`;
			return toolResult(text, result);
		},
	});

	// ─── mockup_write_config ────────────────────────────────────────────
	pi.registerTool({
		name: "mockup_write_config",
		label: "Write Config",
		description:
			"Write the client.ts and brand.ts files into the cloned template. Pass the FULL TypeScript source code as strings. The helper validates that 'export const client' and 'export const brand' are present, and rejects unquoted logo paths (Session 10 regression).",
		parameters: Type.Object({
			slug: Type.String(),
			client_ts: Type.String({
				description: "Full content of client.ts (must contain `export const client`)",
			}),
			brand_ts: Type.String({
				description: "Full content of brand.ts (must contain `export const brand`)",
			}),
		}),
		async execute(_id, params) {
			// Stage the content to temp files, then pass paths to helper
			const tmpDir = join(tmpdir(), `mockup-${params.slug}-${Date.now()}`);
			mkdirSync(tmpDir, { recursive: true });
			const clientPath = join(tmpDir, "client.ts");
			const brandPath = join(tmpDir, "brand.ts");
			writeFileSync(clientPath, params.client_ts);
			writeFileSync(brandPath, params.brand_ts);

			const result = await callHelper("write_config", [
				params.slug,
				`--client-ts-file=${clientPath}`,
				`--brand-ts-file=${brandPath}`,
			]);
			const text = result.ok
				? `Wrote client.ts (${result.client_ts_bytes} bytes) + brand.ts (${result.brand_ts_bytes} bytes)`
				: `Error: ${result.error}`;
			return toolResult(text, result);
		},
	});

	// ─── mockup_copy_assets ─────────────────────────────────────────────
	pi.registerTool({
		name: "mockup_copy_assets",
		label: "Copy Assets",
		description:
			"Copy scraped images (logo, hero, gallery) from the audit into the template's public/images/ dir. Falls back to Pillow-generated placeholders if any source is missing or zero-byte. Always produces logo.jpg, hero.jpg, about.jpg, gallery/1-4.jpg.",
		parameters: Type.Object({
			slug: Type.String(),
			logo_path: Type.Optional(Type.String({ description: "Absolute path to scraped logo image" })),
			hero_path: Type.Optional(Type.String({ description: "Absolute path to scraped hero image" })),
			gallery_paths: Type.Optional(Type.Array(Type.String(), {
				description: "Array of absolute paths to scraped gallery images (max 4 used)",
			})),
			business_name: Type.String({ description: "Used for text-logo fallback" }),
			accent_color: Type.String({ description: "Hex color like #1a4d5c, used for placeholders" }),
		}),
		async execute(_id, params) {
			const galleryCsv = (params.gallery_paths ?? []).slice(0, 4).join(",");
			const args = [
				params.slug,
				`--business-name=${params.business_name}`,
				`--accent-color=${params.accent_color}`,
			];
			if (params.logo_path) args.push(`--logo=${params.logo_path}`);
			if (params.hero_path) args.push(`--hero=${params.hero_path}`);
			if (galleryCsv) args.push(`--gallery=${galleryCsv}`);
			const result = await callHelper("copy_assets", args);
			const text = result.ok
				? `Assets copied: logo ${result.logo_bytes}B, hero ${result.hero_bytes}B, about ${result.about_bytes}B, ${result.gallery_count} gallery images`
				: `Error: ${result.error}`;
			return toolResult(text, result);
		},
	});

	// ─── mockup_build ───────────────────────────────────────────────────
	pi.registerTool({
		name: "mockup_build",
		label: "Build Mockup",
		description:
			"Run `pnpm install` and `pnpm build` in the cloned template. Returns build timings + dist/ size. Fails loudly if dist/ has zero-byte images (no silent broken deploys).",
		parameters: Type.Object({
			slug: Type.String(),
		}),
		async execute(_id, params) {
			const result = await callHelper("build", [params.slug], BUILD_TIMEOUT_S);
			const text = result.ok
				? `Built in ${result.total_ms}ms (install ${result.install_ms}ms + build ${result.build_ms}ms), dist=${result.dist_size_kb}KB`
				: `Build error: ${result.error}`;
			return toolResult(text, result);
		},
	});

	// ─── mockup_deploy ──────────────────────────────────────────────────
	pi.registerTool({
		name: "mockup_deploy",
		label: "Deploy Mockup",
		description:
			"Deploy the built dist/ to Cloudflare Pages (<slug>-demo project) and create the demo-<slug>.clientcompass.co.za DNS A record. Returns the live demo URL.",
		parameters: Type.Object({
			slug: Type.String(),
		}),
		async execute(_id, params) {
			const result = await callHelper("deploy", [params.slug], DEPLOY_TIMEOUT_S);
			const text = result.ok
				? `Deployed: ${result.demo_url} (pages ${result.pages_ms}ms + dns ${result.dns_ms}ms)`
				: `Deploy error: ${result.error}`;
			return toolResult(text, result);
		},
	});

	// ─── mockup_verify ──────────────────────────────────────────────────
	pi.registerTool({
		name: "mockup_verify",
		label: "Verify Mockup",
		description:
			"Programmatic checks on the live mockup URL: HTTP 200, all images resolve and are >1KB, no trade-copy phrases on non-trades verticals (regression check), has title and h1. Returns {ok, checks, issues, recommendation: 'ship'|'iterate'}. Use Playwright MCP (browser_navigate + browser_take_screenshot) for visual review on top of this.",
		parameters: Type.Object({
			url: Type.String({ description: "Live demo URL" }),
			vertical: Type.String({ description: "Business vertical (plumber, event_planner, photographer, etc.)" }),
		}),
		async execute(_id, params) {
			const result = await callHelper("verify", [params.url, params.vertical]);
			const text = result.ok
				? `✓ All checks passed. Title: "${result.title}", H1: "${result.h1}". Recommend: ${result.recommendation}`
				: `✗ Issues found: ${(result.issues as any[]).map((i) => i.name).join(", ")}. Recommend: ${result.recommendation}`;
			return toolResult(text, result);
		},
	});

	// ─── mockup_write_approval ──────────────────────────────────────────
	pi.registerTool({
		name: "mockup_write_approval",
		label: "Write Approval",
		description:
			"Insert a pending approval row in admin_crm.mockup_approvals on the prod admin DB, and update the leadgen lead's mockup_status to 'pending_approval'. Call this LAST after verification passes. Returns the approval_id.",
		parameters: Type.Object({
			lead_id: Type.String(),
			mockup_url: Type.String(),
			recommendation: Type.Object({
				template: Type.String(),
				rationale: Type.String(),
				brand_colors: Type.Optional(Type.Object({
					primary: Type.String(),
					accent: Type.String(),
				})),
			}, { additionalProperties: true }),
		}),
		async execute(_id, params) {
			const result = await callHelper("write_approval", [
				params.lead_id,
				params.mockup_url,
				JSON.stringify(params.recommendation),
			]);
			const text = result.ok
				? `Approval written: ${result.approval_id} (status: pending)`
				: `Error: ${result.error}`;
			return toolResult(text, result);
		},
	});
}
