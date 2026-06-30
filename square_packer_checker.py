"""
Usage:
    python square_packer_checker.py <svg_file> [--scale N] [--sim-tol T] [--annotate <out.svg>] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

SVG_NS = "http://www.w3.org/2000/svg"


Vec2 = Tuple[float, float]


@dataclass
class Square:
    idx: int
    x: float
    y: float
    w: float
    h: float
    angle_deg: float
    center: Vec2
    corners: Tuple[Vec2, Vec2, Vec2, Vec2] = field(repr=False)

    @property
    def normalized_angle_deg(self) -> float:
        return self.angle_deg % 90.0


@dataclass
class CheckResult:
    svg_path: Path
    container_w: float
    container_h: float
    num_shapes: int
    shape_size: Tuple[float, float]
    scale: float
    out_of_bounds: List[int]
    overlaps: List[Tuple[int, int]]
    pairwise_min_distance: float
    inside_min_margin: float

    @property
    def is_valid(self) -> bool:
        return not self.out_of_bounds and not self.overlaps

    @property
    def sim_pairwise_min_distance(self) -> float:
        return self.pairwise_min_distance / self.scale if self.scale else self.pairwise_min_distance

    @property
    def sim_inside_min_margin(self) -> float:
        return self.inside_min_margin / self.scale if self.scale else self.inside_min_margin


_ROT_RE = re.compile(
    r"rotate\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)"
)
_ROT_RE_NO_CENTER = re.compile(r"rotate\(\s*(-?\d+(?:\.\d+)?)\s*\)")


def parse_rotation(transform: str) -> Tuple[float, Vec2]:
    m = _ROT_RE.search(transform)
    if m:
        return float(m.group(1)), (float(m.group(2)), float(m.group(3)))
    m = _ROT_RE_NO_CENTER.search(transform)
    if m:
        return float(m.group(1)), (float("nan"), float("nan"))
    raise ValueError(f"Cannot parse transform: {transform!r}")


def rotated_corners(x: float, y: float, w: float, h: float,
                    angle_deg: float, cx: float, cy: float) -> Tuple[Vec2, ...]:
    hw, hh = w / 2.0, h / 2.0
    local = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    theta = math.radians(angle_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return tuple((cx + lx * cos_t - ly * sin_t,
                  cy + lx * sin_t + ly * cos_t) for lx, ly in local)


_META_RE = re.compile(r"<!--\s*packing-meta:\s*(\{.*?\})\s*-->", re.DOTALL)


def parse_packing_meta(svg_path: Path) -> dict:
    text = svg_path.read_text(encoding="utf-8")
    m = _META_RE.search(text)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def parse_svg(svg_path: Path) -> Tuple[float, float, List[Square], dict]:
    tree = ET.parse(svg_path)
    root = tree.getroot()

    view_box = root.get("viewBox")
    if view_box:
        parts = view_box.split()
        container_w, container_h = float(parts[2]), float(parts[3])
    else:
        container_w = float(root.get("width"))
        container_h = float(root.get("height"))

    squares: List[Square] = []
    idx = 0
    for rect in root.iter(f"{{{SVG_NS}}}rect"):
        if rect.get("data-container") == "true":
            continue
        x = float(rect.get("x"))
        y = float(rect.get("y"))
        w = float(rect.get("width"))
        h = float(rect.get("height"))
        transform = rect.get("transform", "") or ""
        angle_deg, (rcx, rcy) = parse_rotation(transform)
        cx = rcx if not math.isnan(rcx) else x + w / 2.0
        cy = rcy if not math.isnan(rcy) else y + h / 2.0
        corners = rotated_corners(x, y, w, h, angle_deg, cx, cy)
        squares.append(Square(idx, x, y, w, h, angle_deg, (cx, cy), corners))
        idx += 1

    meta = parse_packing_meta(svg_path)
    return container_w, container_h, squares, meta



def _project(poly: Tuple[Vec2, ...], ax: float, ay: float) -> Tuple[float, float]:
    dots = [px * ax + py * ay for px, py in poly]
    return min(dots), max(dots)


def sat_overlap(a: Tuple[Vec2, ...], b: Tuple[Vec2, ...], tol: float) -> bool:
    for poly in (a, b):
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            ex, ey = x2 - x1, y2 - y1
            ax, ay = -ey, ex
            length = math.hypot(ax, ay)
            if length == 0.0:
                continue
            ax, ay = ax / length, ay / length
            min_a, max_a = _project(a, ax, ay)
            min_b, max_b = _project(b, ax, ay)
            if max_a - tol <= min_b or max_b - tol <= min_a:
                return False
    return True


def sat_signed_min_gap(a: Tuple[Vec2, ...], b: Tuple[Vec2, ...]) -> float:
    best_sep = -math.inf
    for poly in (a, b):
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            ex, ey = x2 - x1, y2 - y1
            ax, ay = -ey, ex
            length = math.hypot(ax, ay)
            if length == 0.0:
                continue
            ax, ay = ax / length, ay / length
            min_a, max_a = _project(a, ax, ay)
            min_b, max_b = _project(b, ax, ay)
            sep = max(min_b - max_a, min_a - max_b)
            if sep > best_sep:
                best_sep = sep
    return best_sep


def inside_margin(corners: Tuple[Vec2, ...], W: float, H: float) -> float:
    m = math.inf
    for cx, cy in corners:
        m = min(m, cx, W - cx, cy, H - cy)
    return m



def auto_detect_scale(meta: dict, squares: List[Square]) -> float:
    ss = meta.get("shape_size")
    if isinstance(ss, list) and ss:
        for v in ss:
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    if squares and squares[0].w > 0:
        return float(squares[0].w)
    return 1.0


def check(svg_path: Path, sim_tol: float = 1e-9, scale: float | None = None) -> CheckResult:
    W, H, squares, meta = parse_svg(svg_path)
    n = len(squares)
    if scale is None:
        scale = auto_detect_scale(meta, squares)
    tol_display = sim_tol * scale

    out_of_bounds: List[int] = []
    inside_min = math.inf
    for s in squares:
        m = inside_margin(s.corners, W, H)
        if m < inside_min:
            inside_min = m
        if m < -tol_display:
            out_of_bounds.append(s.idx)

    overlaps: List[Tuple[int, int]] = []
    pair_min_gap = math.inf
    for i in range(n):
        for j in range(i + 1, n):
            gap = sat_signed_min_gap(squares[i].corners, squares[j].corners)
            if gap < pair_min_gap:
                pair_min_gap = gap
            if sat_overlap(squares[i].corners, squares[j].corners, tol_display):
                overlaps.append((squares[i].idx, squares[j].idx))

    return CheckResult(
        svg_path=svg_path,
        container_w=W,
        container_h=H,
        num_shapes=n,
        shape_size=(squares[0].w, squares[0].h) if squares else (0.0, 0.0),
        scale=scale,
        out_of_bounds=out_of_bounds,
        overlaps=overlaps,
        pairwise_min_distance=pair_min_gap,
        inside_min_margin=inside_min,
    )



def report(res: CheckResult, sim_tol: float = 1e-9, out=sys.stdout) -> None:
    p = res.svg_path.name
    sc = res.scale
    print(f"\n{'=' * 72}", file=out)
    print(f"File: {p}", file=out)
    print(f"Scale (sim -> display):  {sc}", file=out)
    print(f"Container (display):     {res.container_w:.6f} x {res.container_h:.6f}", file=out)
    print(f"Container (sim):         {res.container_w / sc:.6f} x {res.container_h / sc:.6f}", file=out)
    print(f"Squares (display):       {res.shape_size[0]:.4f} x {res.shape_size[1]:.4f}  ({res.num_shapes} squares)", file=out)
    print(f"Squares (sim):           {res.shape_size[0] / sc:.4f} x {res.shape_size[1] / sc:.4f}", file=out)
    print("-" * 72, file=out)
    print("Min square-to-square gap:", file=out)
    print(f"  display: {res.pairwise_min_distance:+.6e}", file=out)
    print(f"  sim:     {res.sim_pairwise_min_distance:+.6e}", file=out)
    print(f"  (positive = separated; negative = overlapping by |value|)", file=out)
    print("Min margin to container wall:", file=out)
    print(f"  display: {res.inside_min_margin:+.6e}", file=out)
    print(f"  sim:     {res.sim_inside_min_margin:+.6e}", file=out)
    print(f"  (positive = strictly inside; negative = outside by |value|)", file=out)
    print("-" * 72, file=out)
    print(f"Squares outside container (at sim-tol={sim_tol:.0e}): {len(res.out_of_bounds)}", file=out)
    if res.out_of_bounds:
        print(f"  indices: {res.out_of_bounds}", file=out)
    print(f"Overlapping pairs (at sim-tol={sim_tol:.0e}): {len(res.overlaps)}", file=out)
    if res.overlaps:
        preview = res.overlaps[:20]
        more = "" if len(res.overlaps) <= 20 else f"  ... (+{len(res.overlaps) - 20} more)"
        print(f"  pairs:   {preview}{more}", file=out)
    print("-" * 72, file=out)
    print("Verdict under different SIMULATION tolerances:", file=out)
    for tol in (1e-12, 1e-9, 1e-6, 1e-4, 1e-3):
        bad_in = res.sim_inside_min_margin < -tol
        bad_ov = res.sim_pairwise_min_distance < -tol
        ok = not bad_in and not bad_ov
        tag = "ALLOWED" if ok else "NOT ALLOWED"
        print(f"  sim-tol={tol:.0e}: {tag}", file=out)
    print("-" * 72, file=out)
    if res.is_valid:
        print(f"VERDICT (sim-tol={sim_tol:.0e}):  ALLOWED", file=out)
    else:
        print(f"VERDICT (sim-tol={sim_tol:.0e}):  NOT ALLOWED", file=out)
    print(f"{'=' * 72}", file=out)



def annotate(svg_path: Path, out_path: Path, res: CheckResult) -> None:
    W, H, squares, _meta = parse_svg(svg_path)
    bad_set = set(res.out_of_bounds)
    overlap_set = set()
    for i, j in res.overlaps:
        overlap_set.add(i)
        overlap_set.add(j)

    lines = []
    lines.append(f'<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W:.4f}" height="{H:.4f}" '
        f'viewBox="0 0 {W:.4f} {H:.4f}">'
    )
    lines.append(
        f'<rect x="0" y="0" width="{W:.4f}" height="{H:.4f}" '
        f'fill="none" stroke="#3c3c50" stroke-width="2"/>'
    )

    for s in squares:
        if s.idx in overlap_set:
            fill = "#ff5555"
            stroke = "#7a0000"
            sw = 1.5
        elif s.idx in bad_set:
            fill = "#ffaa00"
            stroke = "#7a4500"
            sw = 1.5
        else:
            fill = "#9696c8"
            stroke = "#ffffff"
            sw = 1
        lines.append(
            f'<rect x="{s.x:.4f}" y="{s.y:.4f}" '
            f'width="{s.w:.4f}" height="{s.h:.4f}" '
            f'transform="rotate({s.angle_deg:.6f} {s.center[0]:.4f} {s.center[1]:.4f})" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
        )

    for i, j in res.overlaps[:200]:
        a = squares[i].center
        b = squares[j].center
        lines.append(
            f'<line x1="{a[0]:.4f}" y1="{a[1]:.4f}" '
            f'x2="{b[0]:.4f}" y2="{b[1]:.4f}" '
            f'stroke="#ff0000" stroke-width="0.8" stroke-dasharray="2,2" '
            f'opacity="0.7"/>'
        )

    lx = 4
    ly = H - 20
    lines.append(
        f'<rect x="{lx}" y="{ly}" width="14" height="14" '
        f'fill="#9696c8" stroke="#fff" stroke-width="1"/>'
    )
    lines.append(
        f'<text x="{lx + 18}" y="{ly + 11}" font-size="9" '
        f'font-family="sans-serif" fill="#222">OK</text>'
    )
    lines.append(
        f'<rect x="{lx + 50}" y="{ly}" width="14" height="14" '
        f'fill="#ffaa00" stroke="#7a4500" stroke-width="1"/>'
    )
    lines.append(
        f'<text x="{lx + 68}" y="{ly + 11}" font-size="9" '
        f'font-family="sans-serif" fill="#222">Out of container</text>'
    )
    lines.append(
        f'<rect x="{lx + 170}" y="{ly}" width="14" height="14" '
        f'fill="#ff5555" stroke="#7a0000" stroke-width="1"/>'
    )
    lines.append(
        f'<text x="{lx + 188}" y="{ly + 11}" font-size="9" '
        f'font-family="sans-serif" fill="#222">Overlapping</text>'
    )

    verdict = "ALLOWED" if res.is_valid else "NOT ALLOWED"
    vcolor = "#0a7a0a" if res.is_valid else "#a00000"
    lines.append(
        f'<text x="{W - 4}" y="{ly + 11}" text-anchor="end" '
        f'font-size="12" font-family="sans-serif" '
        f'font-weight="bold" fill="{vcolor}">Verdict: {verdict}</text>'
    )

    lines.append("</svg>")
    out_path.write_text("\n".join(lines), encoding="utf-8")



def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Square packer checker for SVG files")
    ap.add_argument("svg", type=Path, help="input SVG file")
    ap.add_argument("--scale", type=float, default=None,
                    help="simulation->display scale (default: auto-detect from "
                         "packing-meta shape_size, or first square width)")
    ap.add_argument("--sim-tol", type=float, default=1e-9,
                    help="tolerance in SIMULATION units (default 1e-9). "
                         "display_tol = sim_tol * scale")
    ap.add_argument("--annotate", type=Path, default=None,
                    help="if given, write an annotated SVG to this path")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable JSON summary on stdout")
    args = ap.parse_args(argv)

    res = check(args.svg, sim_tol=args.sim_tol, scale=args.scale)
    if not args.json:
        report(res, sim_tol=args.sim_tol)
    else:
        print(json.dumps({
            "file": str(args.svg),
            "scale": res.scale,
            "container_display": [res.container_w, res.container_h],
            "container_sim": [res.container_w / res.scale, res.container_h / res.scale],
            "num_shapes": res.num_shapes,
            "out_of_bounds": res.out_of_bounds,
            "overlaps": res.overlaps,
            "pairwise_min_distance_display": res.pairwise_min_distance,
            "pairwise_min_distance_sim": res.sim_pairwise_min_distance,
            "inside_min_margin_display": res.inside_min_margin,
            "inside_min_margin_sim": res.sim_inside_min_margin,
            "sim_tol": args.sim_tol,
            "valid": res.is_valid,
        }, indent=2))

    if args.annotate:
        annotate(args.svg, args.annotate, res)
        if not args.json:
            print(f"Annotated SVG written to: {args.annotate}")

    return 0 if res.is_valid else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
