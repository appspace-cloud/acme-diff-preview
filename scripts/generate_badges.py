#!/usr/bin/env python3
"""Generate badges/coverage.svg and badges/version.svg.

Self-contained shields-style SVGs committed to the repo -- no third-party
badge service, because this is a PRIVATE repo: shields.io's dynamic-badge
pattern cannot fetch a private repo's raw files without credentials it does
not have, so it would render "invalid" for every viewer. A static SVG in the
repo renders for any authenticated viewer like any other repo image.

Inputs:
- coverage.json  (pytest --cov-report=json)  -> coverage percentage
- the latest v* git tag                      -> service version

Same visual shape and color tiers as the acme-mcp badge generator, so the
two repos look like siblings.
"""
import json
import os
import subprocess
import sys


def color_for(pct: float) -> str:
    if pct >= 90:
        return "#4c1"
    if pct >= 80:
        return "#97CA00"
    if pct >= 70:
        return "#a4a61d"
    if pct >= 60:
        return "#dfb317"
    if pct >= 50:
        return "#fe7d37"
    return "#e05d44"


def text_width(s: str) -> int:
    # Rough Verdana-11px estimate, same metric shields.io uses.
    return round(len(s) * 6.5 + 10)


def write_badge(out_path: str, label: str, value: str, color: str) -> None:
    lw, vw = text_width(label), text_width(value)
    total = lw + vw
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{label}: {value}">
  <title>{label}: {value}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r">
    <rect width="{total}" height="20" rx="3" fill="#fff"/>
  </clipPath>
  <g clip-path="url(#r)">
    <rect width="{lw}" height="20" fill="#555"/>
    <rect x="{lw}" width="{vw}" height="20" fill="{color}"/>
    <rect width="{total}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="{lw / 2}" y="14">{label}</text>
    <text x="{lw + vw / 2}" y="14">{value}</text>
  </g>
</svg>
"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(svg)
    print(f"Wrote {out_path}: {label} {value} ({color})")


def main() -> int:
    try:
        with open("coverage.json") as f:
            pct = round(json.load(f)["totals"]["percent_covered"], 2)
    except (OSError, KeyError, json.JSONDecodeError) as e:
        print(f"Could not read coverage.json: {e}", file=sys.stderr)
        return 1
    write_badge("badges/coverage.svg", "coverage", f"{pct}%", color_for(pct))

    r = subprocess.run(["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
                       capture_output=True, text=True)
    version = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"
    write_badge("badges/version.svg", "version", version, "#007ec6")
    return 0


if __name__ == "__main__":
    sys.exit(main())
