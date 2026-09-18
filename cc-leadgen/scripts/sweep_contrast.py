"""Canonical WCAG AA contrast sweep for Client Compass mockups.

Loads the page via playwright, walks every visible text node, computes effective
background via an alpha-aware bottom-up compositor (handles bg-black/80,
bg-white/10, translucent inline styles), converts oklch/oklab colors via the
CSS-Color-4 matrix to linear sRGB (Chromium's canvas fillStyle does NOT
round-trip these), and reports fg/bg/ratio/threshold per failing element.

Threshold: 4.5 for normal text (< 18.66px bold OR < 24px regular);
           3.0 for large text (>= 24px regular OR >= 18.66px bold).

Usage:
    python3 /app/scripts/sweep_contrast.py <url>
"""
from __future__ import annotations

import sys
from playwright.sync_api import sync_playwright


SWEEP_JS = r"""
() => {
    // ---- color parsing + conversion ----
    function oklabToLinearSRGB(L, a, b) {
        // CSS-Color-4 oklab -> XYZ -> linear sRGB (D65)
        const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
        const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
        const s_ = L - 0.0894841775 * a - 1.2914855480 * b;
        const l = l_ * l_ * l_;
        const m = m_ * m_ * m_;
        const s = s_ * s_ * s_;
        return [
            +4.07674166231455 * l - 3.30771159126301 * m - 0.18096965664556 * s,
            -1.26843800462618 * l + 2.60975740114434 * m - 0.34109239634556 * s,
            -0.00419608654184 * l - 0.70341861467756 * m + 1.70761470107442 * s,
        ];
    }
    function oklchToOklab(str) {
        // oklch(L C H) -> oklab(L a b)
        const m = str.match(/oklch\(\s*([-\d.]+%?)\s+([-\d.]+%?)\s+([-\d.]+(?:deg|rad|turn)?)\s*\)/i);
        if (!m) return null;
        const Lp = m[1].endsWith('%') ? parseFloat(m[1]) / 100 : parseFloat(m[1]);
        let C = parseFloat(m[2]);
        if (m[2].endsWith('%')) C = C / 100 * 0.4;  // oklch C is 0-0.4 approx
        const Hr = m[3].endsWith('deg') ? parseFloat(m[3])
                  : m[3].endsWith('rad') ? parseFloat(m[3]) * 180 / Math.PI
                  : m[3].endsWith('turn') ? parseFloat(m[3]) * 360
                  : parseFloat(m[3]);
        const h = Hr * Math.PI / 180;
        return [Lp, C * Math.cos(h), C * Math.sin(h)];
    }
    function oklabToRGB(str) {
        // oklab(L a b) -> linear sRGB -> gamma-encoded sRGB
        const m = str.match(/oklab\(\s*([-\d.]+%?)\s+([-\d.]+%?)\s+([-\d.]+%?)\s*(?:\/\s*([-\d.]+%?))?\s*\)/i);
        if (!m) return null;
        const Lp = m[1].endsWith('%') ? parseFloat(m[1]) / 100 : parseFloat(m[1]);
        const a = parseFloat(m[2]);
        const b = parseFloat(m[3]);
        const alpha = m[4] === undefined ? 1.0
                    : m[4].endsWith('%') ? parseFloat(m[4]) / 100
                    : parseFloat(m[4]);
        const lin = oklabToLinearSRGB(Lp, a, b);
        const enc = lin.map(c => {
            const v = Math.max(0, Math.min(1, c));
            return v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
        });
        return [Math.round(enc[0] * 255), Math.round(enc[1] * 255), Math.round(enc[2] * 255), alpha];
    }
    function parseColor(s) {
        if (!s) return null;
        s = s.trim();
        if (s === 'transparent' || s === 'none') return [0, 0, 0, 0];
        if (s.startsWith('#')) {
            let h = s.slice(1);
            if (h.length === 3) h = h.split('').map(c => c + c).join('');
            if (h.length === 4) h = h.split('').map(c => c + c).join('');
            if (h.length >= 6) {
                return [
                    parseInt(h.slice(0, 2), 16),
                    parseInt(h.slice(2, 4), 16),
                    parseInt(h.slice(4, 6), 16),
                    h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1.0,
                ];
            }
            return null;
        }
        // oklab / oklch BEFORE rgba? because oklab starts with 'o' not 'r'
        if (s.toLowerCase().startsWith('oklab(')) {
            return oklabToRGB(s);
        }
        if (s.toLowerCase().startsWith('oklch(')) {
            const lab = oklchToOklab(s);
            if (!lab) return null;
            const lin = oklabToLinearSRGB(lab[0], lab[1], lab[2]);
            const enc = lin.map(c => {
                const v = Math.max(0, Math.min(1, c));
                return v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
            });
            const alphaMatch = s.match(/\/\s*([-\d.]+%?)\s*\)$/);
            const alpha = alphaMatch ? (alphaMatch[1].endsWith('%') ? parseFloat(alphaMatch[1]) / 100 : parseFloat(alphaMatch[1])) : 1.0;
            return [Math.round(enc[0] * 255), Math.round(enc[1] * 255), Math.round(enc[2] * 255), alpha];
        }
        let m = s.match(/rgba?\(([^)]+)\)/);
        if (m) {
            const parts = m[1].split(',').map(p => p.trim());
            const nums = parts.map((p, i) => i < 3 ? parseInt(p) : parseFloat(p));
            if (nums.length >= 3 && !isNaN(nums[0])) {
                return [nums[0], nums[1], nums[2], nums.length > 3 && !isNaN(nums[3]) ? nums[3] : 1.0];
            }
        }
        return null;
    }
    function composite(top, bottom) {
        // alpha-aware over-compositing
        const a_out = top[3] + bottom[3] * (1 - top[3]);
        if (a_out === 0) return [0, 0, 0];
        const r = (top[0] * top[3] + bottom[0] * bottom[3] * (1 - top[3])) / a_out;
        const g = (top[1] * top[3] + bottom[1] * bottom[3] * (1 - top[3])) / a_out;
        const b = (top[2] * top[3] + bottom[2] * bottom[3] * (1 - top[3])) / a_out;
        return [Math.round(r), Math.round(g), Math.round(b)];
    }
    function getEffectiveBg(el) {
        const layers = [];
        let cur = el;
        let safety = 50;
        while (cur && safety-- > 0) {
            const cs = getComputedStyle(cur);
            const c = parseColor(cs.backgroundColor);
            if (c && c[3] > 0) layers.unshift(c);  // bottom-up
            cur = cur.parentElement;
            if (!cur || cur === document.documentElement.parentElement) break;
        }
        if (layers.length === 0) layers.push([255, 255, 255, 1.0]);
        let acc = layers[0];
        for (let i = 1; i < layers.length; i++) {
            acc = composite(layers[i], [...acc, 1.0]);
            acc = [acc[0], acc[1], acc[2], 1.0];
        }
        return acc.slice(0, 3);
    }
    function getEffectiveFg(el, bgRgb) {
        const c = parseColor(getComputedStyle(el).color);
        if (!c) return [0, 0, 0];
        if (c[3] < 1) {
            const out = composite(c, [...bgRgb, 1.0]);
            return [out[0], out[1], out[2]];
        }
        return c.slice(0, 3);
    }
    function lin(c) { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
    function lum(rgb) { return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]); }
    function ratio(a, b) { let la = lum(a), lb = lum(b); if (la < lb) [la, lb] = [lb, la]; return (la + 0.05) / (lb + 0.05); }

    const out = [];
    const seen = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    let node;
    while ((node = walker.nextNode())) {
        const raw = node.nodeValue;
        const text = raw.replace(/\s+/g, ' ').trim();
        if (!text || text.length < 2) continue;
        const el = node.parentElement;
        if (!el) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) continue;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) continue;
        const fg = parseColor(cs.color);
        if (!fg || fg[3] === 0) continue;
        const key = text.slice(0, 40) + '|' + Math.round(rect.left) + '|' + Math.round(rect.top);
        if (seen.has(key)) continue;
        seen.add(key);

        const bg = getEffectiveBg(el);
        const effFg = getEffectiveFg(el, bg);
        const r = ratio(effFg, bg);
        const fontSize = parseFloat(cs.fontSize);
        const fontWeight = parseInt(cs.fontWeight) || 400;
        const isLarge = fontSize >= 24 || (fontSize >= 18.66 && fontWeight >= 700);
        const threshold = isLarge ? 3.0 : 4.5;
        if (r < threshold) {
            out.push({
                text: text.slice(0, 70),
                tag: el.tagName,
                cls: (el.className || '').toString().slice(0, 90),
                fg: `rgb(${effFg.join(',')})`,
                bg: `rgb(${bg.join(',')})`,
                ratio: r.toFixed(2),
                threshold,
                fontSize: cs.fontSize,
                fontWeight: cs.fontWeight,
                x: Math.round(rect.left), y: Math.round(rect.top),
            });
        }
    }
    return out;
}
"""


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:4321/"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(800)
        issues = page.evaluate(SWEEP_JS)
        if not issues:
            print("✓ no contrast issues found")
            browser.close()
            return 0
        for i in issues:
            print(
                f"FAIL [{i['ratio']} < {i['threshold']}] @{i['x']},{i['y']} "
                f"{i['tag']}.{i['cls'][:50]}\n"
                f"    text='{i['text']}'\n"
                f"    fg={i['fg']} bg={i['bg']} ({i['fontSize']}/{i['fontWeight']})"
            )
        print(f"\n{len(issues)} contrast issue(s)")
        browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())