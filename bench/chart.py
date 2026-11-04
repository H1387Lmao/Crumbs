#!/usr/bin/env python3
"""Draw bench/results.svg from bench/results.json: run time of every language relative to C (1.0 = as fast as C)."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run import LANGS, TITLES, ORDER

COLORS = {"crumbs": "#d9a400", "crumbs-unchecked": "#f2cf5b", "crumbs-nogc": "#e8832a", "crumbs-own": "#c43e8a", "c": "#5b7bd6", "cpp": "#7f9ae0", "rust": "#c4623a",
          "go": "#2fa6b8", "java": "#a0674b", "node": "#6a9a47"}
CAP = 4.0          # bars are cut at 4x C; the label still shows the real figure

def main():
    res = json.load(open(os.path.join(HERE, "results.json")))
    langs = [l for l in LANGS if any(l in r for r in res["programs"].values())]
    progs = [p for p in ORDER if p in res["programs"]]
    left, bar_h, gap, group_gap, width = 250, 11, 2, 18, 900
    plot_w = width - left - 70
    group_h = len(langs) * (bar_h + gap)
    height = 86 + len(progs) * (group_h + group_gap) + 40
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" font-family="DM Sans, Helvetica, Arial, sans-serif" font-size="11">',
           '<style>.t{fill:#272820}.m{fill:#606157}@media (prefers-color-scheme: dark){.t{fill:#f0f0e9}.m{fill:#b0b0a5}}</style>',
           f'<text x="{left}" y="24" class="t" font-size="15" font-weight="700">Run time relative to C (lower is better)</text>']
    for i, l in enumerate(langs):
        lx, ly = left + (i % 5) * 150, 36 + (i // 4) * 16
        out.append(f'<rect x="{lx}" y="{ly}" width="10" height="10" fill="{COLORS[l]}"/><text x="{lx + 14}" y="{ly + 9}" class="m">{LANGS[l][0]}</text>')
    y = 86
    for p in progs:
        row = res["programs"][p]
        base = (row.get("c") or row.get("cpp") or row["crumbs"])["best"]
        out.append(f'<text x="{left - 10}" y="{y + group_h / 2 + 4}" text-anchor="end" class="t" font-weight="600">{TITLES[p].split(":")[0]}</text>')
        out.append(f'<line x1="{left}" y1="{y - 4}" x2="{left}" y2="{y + group_h}" stroke="#888" stroke-opacity=".4"/>')
        out.append(f'<line x1="{left + plot_w / CAP}" y1="{y - 4}" x2="{left + plot_w / CAP}" y2="{y + group_h}" stroke="#888" stroke-opacity=".25" stroke-dasharray="3 3"/>')
        for l in langs:
            if l in row:
                rel = row[l]["best"] / base
                w = max(1.5, min(rel, CAP) / CAP * plot_w)
                out.append(f'<rect x="{left}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="2" fill="{COLORS[l]}"/>')
                label = f"{rel:.1f}x" + (" ▸" if rel > CAP else "")
                out.append(f'<text x="{left + w + 5:.1f}" y="{y + 9}" class="m" font-size="10">{label}</text>')
            y += bar_h + gap
        y += group_gap
    out.append(f'<text x="{left + plot_w / CAP}" y="{y + 4}" text-anchor="middle" class="m" font-size="10">1x = C</text>')
    out.append("</svg>")
    open(os.path.join(HERE, "results.svg"), "w").write("\n".join(out))
    print("wrote bench/results.svg")

if __name__ == "__main__":
    main()
