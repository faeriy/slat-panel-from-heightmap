"""
Nest slat-panel parts on plywood sheets and write Mach3 G-code + DXF + previews.

  uv run --no-project --with shapely --with ezdxf --with matplotlib python export_cnc.py \
      --geom <folder>/cnc_geometry.json --out <folder>/CNC [options]

Machine conventions (agreed with the user's uncle):
  * Mach3, metric, one tool: 6 mm flat end mill, Z0 = TOP of the sheet, X0Y0 = sheet bottom-left corner.
  * Step-down 1.5 mm (max 2 mm), parts held by tabs, cut 0.5 mm into the spoil board.
Programs per sheet: holes (peck) -> inner contours (slots) -> outer contours with tabs.
Base back side: separate program after flipping the cut-out base left<->right.
"""
import json, math, os, argparse
from shapely.geometry import Polygon, LineString, Point, box
from shapely.geometry.polygon import orient
from shapely import affinity
from shapely.ops import unary_union

ap = argparse.ArgumentParser()
ap.add_argument("--geom", required=True); ap.add_argument("--out", required=True)
ap.add_argument("--sheet", default="2500x1250", help="LxW mm, X along L")
ap.add_argument("--margin", type=float, default=10)     # sheet edge margin
ap.add_argument("--spacing", type=float, default=10)    # gap between parts; < 2x tool so kerfs overlap and no thin slivers are left
ap.add_argument("--tool", type=float, default=6.0)
ap.add_argument("--stepdown", type=float, default=1.5)
ap.add_argument("--through", type=float, default=18.5, help="final depth (sheet + 0.5 into spoil board)")
ap.add_argument("--feed", type=float, default=1500); ap.add_argument("--plunge", type=float, default=300)
ap.add_argument("--rpm", type=int, default=18000); ap.add_argument("--safe", type=float, default=10)
ap.add_argument("--tab-len", type=float, default=8); ap.add_argument("--tab-h", type=float, default=3)
ap.add_argument("--climb", action="store_true", help="climb milling (default: conventional)")
A = ap.parse_args()
os.makedirs(A.out, exist_ok=True)
G = json.load(open(A.geom))
SL, SW = map(float, A.sheet.lower().split("x"))
R = A.tool / 2
T = G["thickness"]
BW, BH = G["base"]["size"]

# ------------------------------------------------------------------ nesting
class Part:
    def __init__(s, name, poly, kind, inner=(), holes=(), tabs=4):
        s.name, s.poly, s.kind, s.inner, s.holes, s.tabs = name, poly, kind, list(inner), list(holes), tabs

def slat_variants(outline):
    p = Polygon(outline).buffer(0)
    out = []
    for fx in (1, -1):
        for fy in (1, -1):
            q = affinity.scale(p, fx, fy, origin=(0, 0))
            minx, miny, _, _ = q.bounds
            out.append(affinity.translate(q, -minx, -miny))
    return out

def make_regions(first):
    """Rectangles where slats are stacked: (x0, y0, x1, y1, along_x)."""
    m, sp = A.margin, A.spacing
    regs = []
    slat_len = max(Polygon(s["outline"]).bounds[2] for s in G["slats"])
    if first:   # base sits in the bottom-left corner of sheet 1
        bx1, by1 = m + BW, m + BH
        if SW - m - (by1 + sp) > 60: regs.append((m, by1 + sp, bx1, SW - m, True))
        x = bx1 + sp
    else:
        x = m
    # horizontal columns (slat length along X), as many as fit
    k = int((SL - m - x + sp) // (slat_len + sp))
    if k > 0:
        x1 = x + k * slat_len + (k - 1) * sp
        regs.append((x, m, x1, SW - m, True)); x = x1 + sp
    # leftover strip: slats standing vertically (length along Y)
    if SW - 2 * m >= slat_len and SL - m - x > 60:
        regs.append((x, m, SL - m, SW - m, False))
    return regs

def place_in_region(reg, placed, variants):
    x0, y0, x1, y1, along_x = reg
    best = None
    for v in variants:
        q = v if along_x else affinity.rotate(v, 90, origin=(0, 0))
        mnx, mny, mxx, mxy = q.bounds; q = affinity.translate(q, -mnx, -mny)
        w, h = mxx - mnx, mxy - mny
        if (w > x1 - x0 + 1e-6) or (h > y1 - y0 + 1e-6): continue
        # try every column start along the region's length axis (pack 2 slats end to end if they fit)
        starts = [0.0]
        L = w if along_x else h; span = (x1 - x0) if along_x else (y1 - y0)
        k = 1
        while (k + 1) * L + k * A.spacing <= span + 1e-6: starts.append(k * (L + A.spacing)); k += 1
        for st in starts:
            def at(off):
                return affinity.translate(q, x0 + st, y0 + off) if along_x else affinity.translate(q, x0 + off, y0 + st)
            span2 = (y1 - y0 - h) if along_x else (x1 - x0 - w)
            if span2 < 0: continue
            obst = unary_union([p.buffer(A.spacing - 0.01) for p in placed]) if placed else None
            ok = lambda off: obst is None or not at(off).intersects(obst)
            # first free offset by coarse scan then refine (scan avoids non-monotonic traps)
            off = None
            for o in [i * 2.0 for i in range(int(span2 // 2) + 1)]:
                if ok(o): off = o; break
            if off is None: continue
            lo, hi = max(0, off - 2.0), off
            for _ in range(12):
                mid = (lo + hi) / 2
                if ok(mid): hi = mid
                else: lo = mid
            cand = at(hi)
            key = (cand.bounds[3] if along_x else cand.bounds[2], st)
            if best is None or key < best[0]: best = (key, cand)
    return best[1] if best else None

sheets = []
queue = [(s["id"], slat_variants(s["outline"])) for s in G["slats"]]
sheet_no = 0
while queue or sheet_no == 0:
    sheet_no += 1
    parts = []
    if sheet_no == 1:
        bp = box(A.margin, A.margin, A.margin + BW, A.margin + BH)
        ox, oy = A.margin, A.margin
        slots = [box(ox + s["x0"], oy + s["z0"], ox + s["x1"], oy + s["z1"]) for s in G["base"]["slots"]]
        edge = [q for q in slots if not bp.contains(q)]      # outer slats: open notches at the base edge
        inner = [q for q in slots if bp.contains(q)]
        if edge: bp = bp.difference(unary_union(edge))
        holes = [(ox + h["x"], oy + h["z"], h["d"]) for h in G["base"]["holes"]]
        parts.append(Part("BASE", bp, "base", inner, holes, tabs=8))
    for reg in make_regions(sheet_no == 1):
        while queue:   # best-fit: of all remaining slats take the one that ends lowest in the stack
            placed = [p.poly for p in parts]
            best = None
            for qi, (sid, var) in enumerate(queue):
                q = place_in_region(reg, placed, var)
                if q is None: continue
                top = q.bounds[3] if reg[4] else q.bounds[2]
                if best is None or top < best[0] - 1e-6: best = (top, qi, q)
            if best is None: break
            sid, _ = queue.pop(best[1])
            parts.append(Part(f"S{sid:02d}", best[2], "slat"))
    if len(parts) == 0: raise SystemExit("part does not fit on the sheet")
    sheets.append(parts)

# ------------------------------------------------------------------ toolpaths
def depths(final):
    d, out = 0.0, []
    while d < final - 1e-6:
        d = min(final, d + A.stepdown); out.append(round(d, 3))
    return out

def ring_coords(poly, cw):
    p = orient(poly, sign=-1.0 if cw else 1.0)
    return list(p.exterior.coords)

class GW:
    def __init__(s, title):
        s.l = []; s.cut = 0.0; s.plunge_len = 0.0; s.rapid = 0.0; s.pos = (0.0, 0.0, A.safe)
        s.l += [f"({title})", "(Tool: 6mm flat end mill. Z0 = top of material. X0 Y0 = see README)",
                f"(Feed {A.feed:.0f} mm/min, plunge {A.plunge:.0f}, stepdown {A.stepdown} mm, final depth {A.through} mm)",
                "G21 G90 G17 G94 G40 G49 G80", f"G0 Z{A.safe:.3f}", f"M3 S{A.rpm}", "G4 P3"]
    def _d(s, a, b): return math.dist(a, b)
    def rapid_xy(s, x, y):
        if s.pos[2] < A.safe - 1e-6: s.l.append(f"G0 Z{A.safe:.3f}"); s.pos = (s.pos[0], s.pos[1], A.safe)
        s.rapid += s._d(s.pos[:2], (x, y)); s.l.append(f"G0 X{x:.3f} Y{y:.3f}"); s.pos = (x, y, A.safe)
    def z(s, z, feed=None):
        if abs(z - s.pos[2]) < 1e-6: return
        if z < s.pos[2]:
            s.l.append(f"G1 Z{z:.3f} F{(feed or A.plunge):.0f}"); s.plunge_len += s.pos[2] - z
        else: s.l.append(f"G0 Z{z:.3f}" if z >= 0 else f"G1 Z{z:.3f} F{A.plunge:.0f}")
        s.pos = (s.pos[0], s.pos[1], z)
    def to(s, x, y):
        s.cut += s._d(s.pos[:2], (x, y)); s.l.append(f"G1 X{x:.3f} Y{y:.3f} F{A.feed:.0f}"); s.pos = (x, y, s.pos[2])
    def comment(s, t): s.l.append(f"({t})")
    def end(s):
        s.l += [f"G0 Z{A.safe:.3f}", "M5", "G0 X0 Y0", "M30"]
    def minutes(s): return s.cut / A.feed + s.plunge_len / A.plunge + s.rapid / 3000

def contour(gw, coords, final, tabs_s=(), tab_top=None):
    """coords closed ring (first==last). tabs_s: list of (s0,s1) arc-length intervals to bridge."""
    ln = LineString(coords); total = ln.length
    # split points at tab boundaries
    cuts = sorted({0.0, total} | {max(0, min(total, v)) for iv in tabs_s for v in iv})
    def in_tab(s): return any(a <= s <= b for a, b in tabs_s)
    gw.rapid_xy(*coords[0][:2])
    for d in depths(final):
        zc = -d; use_tabs = tab_top is not None and d > tab_top + 1e-6
        gw.z(zc)
        # walk the ring
        pts = []
        dist = [0.0]
        for a, b in zip(coords, coords[1:]): dist.append(dist[-1] + math.dist(a, b))
        events = sorted(set(dist) | (set(cuts) if use_tabs else set()))
        for s in events[1:]:
            p = ln.interpolate(s); mid = (s + prev) / 2 if (prev := events[events.index(s) - 1]) is not None else s
            target_z = -tab_top if (use_tabs and in_tab(mid)) else zc
            if target_z > gw.pos[2] + 1e-6: gw.z(target_z)
            gw.to(p.x, p.y)
            if target_z < zc + 1e-6 and False: pass
            nxt_mid = None
            # drop back after the tab
            if use_tabs and not in_tab(min(total, s + 1e-3)) and gw.pos[2] > zc + 1e-6: gw.z(zc)
    gw.z(A.safe)

def tab_intervals(path_ring, part, n):
    ln = LineString(path_ring); total = ln.length
    if part.kind == "base":
        mnx, mny, mxx, mxy = part.poly.bounds
        targets = [(mnx + (mxx - mnx) * f, mny) for f in (0.25, 0.75)] + [(mnx + (mxx - mnx) * f, mxy) for f in (0.25, 0.75)] \
                + [(mnx, mny + (mxy - mny) * f) for f in (0.25, 0.75)] + [(mxx, mny + (mxy - mny) * f) for f in (0.25, 0.75)]
    else:  # two tabs on each long side, away from hooks (at 25% / 75% of the length)
        mnx, mny, mxx, mxy = part.poly.bounds
        along_x = (mxx - mnx) > (mxy - mny)
        targets = []
        for f in (0.25, 0.75):
            if along_x: targets += [(mnx + (mxx - mnx) * f, mny - 50), (mnx + (mxx - mnx) * f, mxy + 50)]
            else: targets += [(mnx - 50, mny + (mxy - mny) * f), (mxx + 50, mny + (mxy - mny) * f)]
    half = A.tab_len / 2 + R
    out = []
    for t in targets:
        s = ln.project(Point(t)); out.append((max(0, s - half), min(total, s + half)))
    return out

def write_front(sheet_i, parts):
    gw = GW(f"Slat panel - sheet {sheet_i} of {len(sheets)} - FRONT side - X0Y0 = sheet bottom-left")
    # 1 holes (peck drill with the 6 mm end mill)
    hs = [h for p in parts for h in p.holes]
    if hs: gw.comment("Dowel holes 6mm - peck")
    for x, y, d in hs:
        gw.rapid_xy(x, y)
        for dd in depths(A.through): gw.z(-dd); gw.z(min(-dd + 1.0, 0.0) if dd < A.through else -dd)
        gw.z(A.safe)
    # 2 inner contours (slots)
    ins = [(p, q) for p in parts for q in p.inner]
    if ins: gw.comment("Slots - inner contours")
    cur = (0, 0)
    while ins:
        k = min(range(len(ins)), key=lambda i: math.dist(cur, ins[i][1].centroid.coords[0]))
        p, q = ins.pop(k)
        path = q.buffer(-R, join_style=2)
        if path.is_empty: continue
        ring = ring_coords(path, cw=not A.climb)
        contour(gw, ring, A.through); cur = ring[0][:2]
    # 3 outer contours with tabs, slats first, base last (largest part, keeps sheet rigid)
    outs = sorted(parts, key=lambda p: (p.kind == "base",))
    gw.comment("Outer contours with tabs")
    for p in outs:
        gw.comment(f"Part {p.name}")
        path = p.poly.buffer(R, join_style=1, quad_segs=8).simplify(0.02)
        ring = ring_coords(path, cw=A.climb)
        contour(gw, ring, A.through, tab_intervals(ring, p, p.tabs), A.through - A.tab_h)
    gw.end()
    fn = os.path.join(A.out, f"sheet{sheet_i}_front.tap")
    open(fn, "w").write("\n".join(gw.l) + "\n")
    return fn, gw

def write_base_back():
    """Base flipped LEFT<->RIGHT (top stays top). X0Y0 = bottom-left corner of the flipped base."""
    gw = GW("Slat panel - BASE BACK side - base flipped left-right, X0Y0 = its bottom-left corner")
    gw.comment("Back pockets 9mm deep - hook seats")
    pk = G["base"]["back_pockets"]; cur = (0, 0)
    pk = sorted(pk, key=lambda r: (round(r["z0"]), (BW - r["x1"]) if int(round(r["z0"])) % 2 == 0 else -(BW - r["x1"])))
    for r in pk:
        x0, x1 = BW - r["x1"], BW - r["x0"]       # mirror X
        rect = box(x0, r["z0"], x1, r["z1"])
        loops = []; cur_p = rect.buffer(-R, join_style=2)
        while not cur_p.is_empty and cur_p.area > 0.01:
            loops.append(cur_p); cur_p = cur_p.buffer(-(A.tool * 0.42), join_style=2)
        loops.reverse()                                     # inside -> out
        cx, cy = rect.centroid.x, rect.centroid.y
        gw.rapid_xy(cx, cy)
        for d in depths(r["depth"]):
            gw.z(-d)
            mnx, mny, mxx, mxy = loops[0].bounds
            if (mxx - mnx) > 0.1 or (mxy - mny) > 0.1:            # centre line
                if mxx - mnx > mxy - mny: gw.to(mnx, cy); gw.to(mxx, cy); gw.to(cx, cy)
                else: gw.to(cx, mny); gw.to(cx, mxy); gw.to(cx, cy)
            for lp in loops:
                ring = ring_coords(lp, cw=not A.climb)
                gw.to(*ring[0][:2])
                for c in ring[1:]: gw.to(*c[:2])
            gw.to(cx, cy)
        gw.z(A.safe)
    gw.end()
    fn = os.path.join(A.out, "base_back.tap"); open(fn, "w").write("\n".join(gw.l) + "\n")
    return fn, gw

# ------------------------------------------------------------------ DXF + previews
import ezdxf
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

def write_dxf(sheet_i, parts):
    doc = ezdxf.new("R2010"); msp = doc.modelspace()
    for n, c in (("SHEET", 8), ("OUTER", 1), ("INNER", 5), ("DRILL_6", 3), ("LABEL", 7)): doc.layers.add(n, color=c)
    msp.add_lwpolyline([(0, 0), (SL, 0), (SL, SW), (0, SW)], close=True, dxfattribs={"layer": "SHEET"})
    for p in parts:
        msp.add_lwpolyline(list(p.poly.exterior.coords), close=True, dxfattribs={"layer": "OUTER"})
        for q in p.inner: msp.add_lwpolyline(list(q.exterior.coords), close=True, dxfattribs={"layer": "INNER"})
        for x, y, d in p.holes: msp.add_circle((x, y), d / 2, dxfattribs={"layer": "DRILL_6"})
        c = p.poly.representative_point()
        msp.add_text(p.name, height=20, dxfattribs={"layer": "LABEL"}).set_placement((c.x, c.y))
    fn = os.path.join(A.out, f"sheet{sheet_i}.dxf"); doc.saveas(fn); return fn

def write_base_back_dxf():
    doc = ezdxf.new("R2010"); msp = doc.modelspace()
    doc.layers.add("OUTLINE", color=8); doc.layers.add("POCKET_9mm", color=4)
    msp.add_lwpolyline([(0, 0), (BW, 0), (BW, BH), (0, BH)], close=True, dxfattribs={"layer": "OUTLINE"})
    for r in G["base"]["back_pockets"]:
        x0, x1 = BW - r["x1"], BW - r["x0"]
        msp.add_lwpolyline([(x0, r["z0"]), (x1, r["z0"]), (x1, r["z1"]), (x0, r["z1"])], close=True, dxfattribs={"layer": "POCKET_9mm"})
    fn = os.path.join(A.out, "base_back.dxf"); doc.saveas(fn); return fn

def preview(fn_png, title, parts=None, gcode=None, size=(SL, SW)):
    fig, ax = plt.subplots(figsize=(16, 16 * size[1] / size[0] + 0.6), dpi=110)
    ax.add_patch(plt.Rectangle((0, 0), size[0], size[1], fill=True, fc="#e9dcc6", ec="#555"))
    if parts:
        for p in parts:
            x, y = p.poly.exterior.xy; ax.fill(x, y, fc="#b88a5a", ec="#3b2614", lw=0.8)
            for q in p.inner: x, y = q.exterior.xy; ax.fill(x, y, fc="#e9dcc6", ec="#3b2614", lw=0.5)
            for hx, hy, d in p.holes: ax.add_patch(plt.Circle((hx, hy), 4, color="#c00"))
            c = p.poly.representative_point(); ax.text(c.x, c.y, p.name, ha="center", va="center", fontsize=9, color="white", weight="bold")
    if gcode:   # overlay the real toolpath parsed back from the .tap file
        xs, ys, X, Y = [], [], 0.0, 0.0
        for line in open(gcode):
            if line.startswith("G1") and ("X" in line or "Y" in line):
                for tok in line.split():
                    if tok[0] == "X": X = float(tok[1:])
                    if tok[0] == "Y": Y = float(tok[1:])
                xs.append(X); ys.append(Y)
            elif line.startswith("G0") and ("X" in line):
                for tok in line.split():
                    if tok[0] == "X": X = float(tok[1:])
                    if tok[0] == "Y": Y = float(tok[1:])
                xs.append(float("nan")); ys.append(float("nan")); xs.append(X); ys.append(Y)
        ax.plot(xs, ys, color="#1f5fbf", lw=0.4)
    ax.set_xlim(-20, size[0] + 20); ax.set_ylim(-20, size[1] + 20); ax.set_aspect("equal")
    ax.set_title(title, fontsize=13); ax.set_xlabel("X, мм"); ax.set_ylabel("Y, мм")
    fig.tight_layout(); fig.savefig(fn_png); plt.close(fig)

summary = []
for i, parts in enumerate(sheets, 1):
    tap, gw = write_front(i, parts); dxf = write_dxf(i, parts)
    png = os.path.join(A.out, f"sheet{i}_layout.png")
    preview(png, f"Лист {i}/{len(sheets)} ({SL:.0f}×{SW:.0f}) — деталі: {', '.join(p.name for p in parts)}", parts, tap)
    summary.append(dict(sheet=i, parts=[p.name for p in parts], tap=tap, dxf=dxf, png=png,
                        minutes=round(gw.minutes()), lines=len(gw.l)))
tap, gw = write_base_back(); dxf = write_base_back_dxf()
png = os.path.join(A.out, "base_back_layout.png")
preview(png, "База, ТИЛЬНИЙ бік (перевернута ліво↔право) — кишені 9 мм", None, tap, size=(BW, BH))
summary.append(dict(sheet="base_back", tap=tap, dxf=dxf, png=png, minutes=round(gw.minutes()), lines=len(gw.l)))
json.dump(dict(params=vars(A), sheets=summary), open(os.path.join(A.out, "cnc_report.json"), "w"), ensure_ascii=False, indent=1)
print("CNC " + json.dumps(summary, ensure_ascii=False))
