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
 *   mockup_screenshot       — full-page screenshot of a live URL for visual (vision) review
 *   mockup_fetch_image      — download an image URL found via Playwright browsing into the asset pool
 *   mockup_search_stock_images — search Pexels for stock photo candidates when the lead's own
 *                                site (scraped + Playwright fallback) doesn't have enough real
 *                                imagery; returns thumbnails for visual review, same as mockup_screenshot
 *   mockup_write_approval   — insert approval row in prod admin DB
 *
 * Usage:
 *   pi -p "use the mockup-builder skill to generate a mockup for lead_id=<UUID>" \
 *      --extension ~/.pi/agent/extensions/mockup-builder.ts
 *
 * Iteration contract:
 *   - You have up to 3 build attempts. After 3 failed verify cycles, ship whatever
 *     you have and log the reason.
 *   - MANDATORY: call mockup_screenshot on the deployed mockup URL and visually
 *     review the returned image (logo placement/cropping, image quality, layout,
 *     colour usage) BEFORE calling mockup_write_approval. This is enforced below —
 *     mockup_write_approval is hard-blocked until a successful mockup_screenshot
 *     call exists earlier in the session (see the tool_call gate at the bottom of
 *     this file). mockup_verify gives you programmatic checks; mockup_screenshot
 *     gives you eyes.
 *   - You may also use Playwright MCP (browser_navigate) to browse the prospect's
 *     original site for aesthetic reference, but mockup_screenshot — not
 *     Playwright's own screenshot tool — is what satisfies the vision gate, since
 *     it's the one guaranteed to return the image as in-context model input.
 *   - Image sourcing fallback ladder (Phase P, docs/PHASE_P_PLAN.md): (1) <site_snapshot>
 *     static scrape, (2) Playwright + mockup_fetch_image for JS-rendered photos, (3)
 *     mockup_search_stock_images (Pexels) when the lead's own site genuinely lacks enough
 *     real imagery, (4) Pillow gradient/solid-colour placeholder as the true last resort.
 *     If mockup_screenshot's mandatory visual review flags a bad image (wrong crop,
 *     irrelevant photo, duplicate gallery tile) and it's fixable by swapping the image,
 *     call mockup_search_stock_images and rebuild rather than shipping it — you have
 *     iteration budget for this.
 *   - If you need to modify config between iterations, use pi's read/edit tools
 *     on /tmp/cc_mockups/<slug>/src/config/{client,brand}.ts, then call
 *     mockup_build + mockup_deploy + mockup_verify again.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn } from "node:child_process";
import { writeFileSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const SCREENSHOT_TOOL_NAME = "mockup_screenshot";
const APPROVAL_TOOL_NAME = "mockup_write_approval";

// ─── Configuration ─────────────────────────────────────────────────────────

const PYTHON_BIN = process.env.MOCKUP_PYTHON_BIN ?? "/app/.venv/bin/python";
const PROJECT_ROOT = process.env.MOCKUP_PROJECT_ROOT ?? "/app";
const HELPER_MODULE = "app.workers.mockup_helpers.cli";
const DEFAULT_TIMEOUT_S = 300;
const BUILD_TIMEOUT_S = 240;
const DEPLOY_TIMEOUT_S = 120;
// Guardrail on context size — a Pexels search can return up to 10 candidates,
// but we only need enough for Pi to visually pick a winner from.
const MAX_STOCK_CANDIDATES_RETURNED = 6;

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

function toolResultWithImage(
	text: string,
	imageBase64: string,
	mimeType: string,
	details: Record<string, unknown> = {},
) {
	return {
		content: [
			{ type: "text" as const, text },
			{ type: "image" as const, data: imageBase64, mimeType },
		],
		details,
	};
}

function toolResultWithImages(
	text: string,
	images: { data: string; mimeType: string }[],
	details: Record<string, unknown> = {},
) {
	return {
		content: [
			{ type: "text" as const, text },
			...images.map((img) => ({ type: "image" as const, data: img.data, mimeType: img.mimeType })),
		],
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
			"Copy scraped images (logo, hero, gallery) from the audit into the template's public/images/ dir. Falls back to Pillow-generated placeholders if any source is missing or zero-byte. Always produces logo.jpg, hero.jpg, about.jpg, gallery/1-4.jpg. " +
			"IMPORTANT: always pass candidate_pool with EVERY real image path you've gathered for this lead " +
			"(all <site_snapshot> local paths, every mockup_fetch_image download, every " +
			"mockup_search_stock_images pick) — not just your per-slot hints. logo_path/hero_path/" +
			"gallery_paths are hints (which slot you'd like each one in), but candidate_pool lets the " +
			"scorer independently pick the best-fit, DISTINCT image per slot instead of reusing your " +
			"hero pick for about (or gallery) too. Omitting candidate_pool silently falls back to legacy " +
			"behaviour, which WILL duplicate the hero image into the about slot whenever about has no " +
			"pick of its own — a known bug users have flagged before.",
		parameters: Type.Object({
			slug: Type.String(),
			logo_path: Type.Optional(Type.String({ description: "Absolute path to scraped logo image" })),
			hero_path: Type.Optional(Type.String({ description: "Absolute path to scraped hero image" })),
			gallery_paths: Type.Optional(Type.Array(Type.String(), {
				description: "Array of absolute paths to scraped gallery images (max 4 used)",
			})),
			candidate_pool: Type.Optional(Type.Array(Type.String(), {
				description: "ALL real image paths gathered for this lead (site_snapshot images, mockup_fetch_image downloads, mockup_search_stock_images picks). Strongly recommended on every call — enables distinct-per-slot scoring instead of hero/about duplication.",
			})),
			business_name: Type.String({ description: "Used for text-logo fallback" }),
			accent_color: Type.String({ description: "Hex color like #1a4d5c, used for placeholders" }),
		}),
		async execute(_id, params) {
			const galleryCsv = (params.gallery_paths ?? []).slice(0, 4).join(",");
			const poolCsv = (params.candidate_pool ?? []).join(",");
			const args = [
				params.slug,
				`--business-name=${params.business_name}`,
				`--accent-color=${params.accent_color}`,
			];
			if (params.logo_path) args.push(`--logo=${params.logo_path}`);
			if (params.hero_path) args.push(`--hero=${params.hero_path}`);
			if (galleryCsv) args.push(`--gallery=${galleryCsv}`);
			if (poolCsv) args.push(`--pool=${poolCsv}`);
			const result = await callHelper("copy_assets", args);
			const text = result.ok
				? `Assets copied: logo ${result.logo_bytes}B, hero ${result.hero_bytes}B, about ${result.about_bytes}B, ${result.gallery_count} gallery images` +
					(poolCsv ? "" : " [WARNING: no candidate_pool passed — about/gallery may duplicate hero]")
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

	// ─── mockup_screenshot ──────────────────────────────────────────────
	pi.registerTool({
		name: SCREENSHOT_TOOL_NAME,
		label: "Screenshot Mockup",
		description:
			"Capture a full-page screenshot of a URL (the deployed mockup, or the prospect's " +
			"original site for comparison) and return it as an image for visual review. " +
			"MANDATORY: call this on the deployed mockup URL after mockup_deploy succeeds, " +
			"BEFORE calling mockup_write_approval — mockup_write_approval is blocked until this " +
			"has succeeded at least once in this session. Look at colors, logo placement/cropping, " +
			"image quality, layout, and whether anything looks broken or unprofessional.",
		parameters: Type.Object({
			url: Type.String({ description: "URL to screenshot" }),
			viewport: Type.Optional(
				Type.Union([Type.Literal("desktop"), Type.Literal("mobile")], {
					description: "desktop (1440x900) or mobile (390x844). Defaults to desktop.",
				}),
			),
		}),
		async execute(_id, params) {
			const viewport = params.viewport ?? "desktop";
			const result = await callHelper("screenshot", [params.url, `--viewport=${viewport}`], DEFAULT_TIMEOUT_S);
			if (!result.ok || typeof result.png_base64 !== "string") {
				return toolResult(`Error: ${result.error ?? "screenshot did not return image data"}`, result);
			}
			const text = `Screenshot of ${params.url} (${viewport}, ${result.width}x${result.height})`;
			return toolResultWithImage(text, result.png_base64 as string, "image/png", {
				ok: true,
				url: params.url,
				viewport,
				width: result.width,
				height: result.height,
			});
		},
	});

	// ─── mockup_fetch_image ─────────────────────────────────────────────
	// site_snapshot() (run before this Pi session starts) only sees a static
	// HTML fetch of the lead's homepage — no JS execution — so sites with
	// JS-rendered sliders/galleries (e.g. WordPress "revslider") often only
	// yield placeholder images to it even though real photos are visible to
	// a real browser. This tool closes that gap: after using Playwright MCP
	// to browse the live site (browser_navigate to any /gallery, /portfolio,
	// /work page found in the nav; browser_evaluate running
	// `[...document.querySelectorAll('img')].map(i => i.src)` or
	// browser_network_requests to find real image URLs a static fetch
	// missed), call this with the actual image URL to download it into the
	// local asset pool. The returned local_path can then be passed into
	// mockup_copy_assets' hero_path/gallery_paths.
	pi.registerTool({
		name: "mockup_fetch_image",
		label: "Fetch Extra Image",
		description:
			"Download a single image URL you found by browsing the lead's live site with " +
			"Playwright MCP (e.g. via browser_evaluate reading <img> src attributes, or " +
			"browser_network_requests) into the local asset cache, for use as a hero/gallery " +
			"image. Use this when <site_snapshot> came up short on real photos (e.g. only " +
			"placeholder/dummy images were found) — that usually means the real photos are " +
			"loaded by JavaScript (sliders, carousels) that the static pre-scrape can't see, " +
			"but Playwright can. Rejects the download if it's too small or looks like a solid-" +
			"colour placeholder rather than a real photo — try a different URL if so.",
		parameters: Type.Object({
			url: Type.String({ description: "Absolute image URL found while browsing with Playwright" }),
			slug: Type.String(),
			index: Type.Optional(Type.Integer({ description: "Distinguishes multiple fetched images, default 1" })),
		}),
		async execute(_id, params) {
			const args = [params.url, params.slug];
			if (params.index !== undefined) args.push(`--index=${params.index}`);
			const result = await callHelper("fetch_image", args, DEFAULT_TIMEOUT_S);
			const text = result.ok
				? `Fetched ${params.url} → ${result.local_path} (${result.width}x${result.height}, ${result.bytes} bytes)`
				: `Error: ${result.error}`;
			return toolResult(text, result);
		},
	});

	// ─── mockup_search_stock_images ─────────────────────────────────────
	// Phase P (docs/PHASE_P_PLAN.md): closes the gap between "Playwright +
	// mockup_fetch_image found nothing" and "settle for a Pillow gradient".
	// Searches Pexels (free, watermark-free, no attribution required) and
	// returns the downloaded candidates as images in this tool result — the
	// same mechanism mockup_screenshot uses — so Pi's own vision (MiniMax M3
	// is natively multimodal) judges relevance/aesthetic fit directly, no
	// separate model call needed. The winning candidate's local_path is then
	// just an ordinary hint into mockup_copy_assets, same as an LLM-nominated
	// scraped image.
	pi.registerTool({
		name: "mockup_search_stock_images",
		label: "Search Stock Images",
		description:
			"Search Pexels for stock photo candidates and return them as images for visual " +
			"review, the same way mockup_screenshot returns a screenshot. Use this when " +
			"<site_snapshot> AND the Playwright + mockup_fetch_image fallback (see the image " +
			"sourcing steps earlier in this prompt) both come up short on real, on-topic photos " +
			"for the hero, about, or gallery slots — i.e. the lead's own site genuinely doesn't " +
			"have enough usable imagery. NOT for the logo slot — there is no such thing as a " +
			"stock logo; use the text-logo fallback (omit logo_path in mockup_copy_assets) " +
			"instead. Build the query from the business type plus vertical style words (e.g. " +
			"'plumber south africa professional photo', or 'elegant wedding marquee evening " +
			"lights' for a creative/event vertical). Look at the returned thumbnails and pick " +
			"AT MOST ONE winner — reject anything that isn't a real, on-topic, unwatermarked " +
			"photo. If nothing fits, call again with a refined query before falling back to a " +
			"gradient/solid-colour placeholder.",
		parameters: Type.Object({
			query: Type.String({ description: "Search query, e.g. 'plumber south africa professional photo'" }),
			slug: Type.String(),
			slot_hint: Type.Union(
				[Type.Literal("hero"), Type.Literal("about"), Type.Literal("gallery")],
				{ description: "Which slot this search is for. Logo is not supported." },
			),
			orientation: Type.Optional(
				Type.Union([Type.Literal("landscape"), Type.Literal("portrait"), Type.Literal("square")], {
					description: "Defaults to landscape. Use 'square' for gallery tiles if you want a tighter grid fit.",
				}),
			),
		}),
		async execute(_id, params) {
			if ((params.slot_hint as string) === "logo") {
				return toolResult(
					"Error: mockup_search_stock_images does not support the logo slot — a stock " +
						"photo is never an appropriate logo. Omit logo_path in mockup_copy_assets to " +
						"use the text-logo fallback instead.",
					{ ok: false, error: "logo_slot_not_supported" },
				);
			}
			const args = [params.query, params.slug];
			if (params.orientation) args.push(`--orientation=${params.orientation}`);
			const result = await callHelper("search_stock", args, DEFAULT_TIMEOUT_S);
			if (!result.ok || !Array.isArray(result.candidates)) {
				return toolResult(`Error: ${result.error ?? "stock search returned no candidates"}`, result);
			}
			const candidates = (result.candidates as any[]).slice(0, MAX_STOCK_CANDIDATES_RETURNED);
			const images = candidates.map((c) => ({
				data: readFileSync(c.local_path).toString("base64"),
				mimeType: "image/jpeg",
			}));
			const text =
				`Found ${candidates.length} Pexels candidate(s) for "${params.query}" (${params.slot_hint} slot). ` +
				"Review the images above and pick at most one winner's local_path to use as a hint " +
				"in mockup_copy_assets, or call again with a refined query if none fit. Candidates:\n" +
				candidates
					.map((c, i) => `  [${i}] ${c.local_path} (${c.width}x${c.height}, ${c.bytes}B, by ${c.photographer ?? "unknown"})`)
					.join("\n");
			return toolResultWithImages(text, images, result);
		},
	});

	// ─── Hard gate: mockup_write_approval requires a prior successful
	// mockup_screenshot call in this session. Prompt-only instructions have
	// repeatedly failed to make the LLM actually look before approving
	// (see docs/MOCKUP_VISION_IMPLEMENTATION_PLAN.md §2.2) — this makes it
	// mechanically impossible to skip instead of just discouraged.
	pi.on("tool_call", (event, ctx) => {
		if (event.toolName !== APPROVAL_TOOL_NAME) return;
		const sawScreenshot = ctx.sessionManager.getEntries().some(
			(entry: any) =>
				entry.type === "message" &&
				entry.message?.role === "toolResult" &&
				entry.message?.toolName === SCREENSHOT_TOOL_NAME &&
				!entry.message?.isError,
		);
		if (!sawScreenshot) {
			return {
				block: true,
				reason:
					`${APPROVAL_TOOL_NAME} blocked: call ${SCREENSHOT_TOOL_NAME} on the deployed ` +
					"mockup URL first and visually review the returned image before approving.",
			};
		}
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
				image_audit: Type.Optional(Type.Object({
					hero_source: Type.Optional(Type.String()),
					logo_source: Type.Optional(Type.String()),
					gallery_sources: Type.Optional(Type.Array(Type.String())),
				}, { additionalProperties: true, description: "From the last mockup_copy_assets call — each source is 'scraped', 'stock', or 'placeholder'. Surfaced in the admin approval UI." })),
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
