"""
Parametric slat wall panel from a height map.  Runs inside Blender (headless):

  Blender -b --factory-startup --python build_panel.py -- --input map.png --out DIR [options]

Pipeline: height map -> relief surface -> vertical plywood slats with 45-degree
hooks -> plywood base with 45-degree-bevelled slots and countersunk dowel holes
-> studio scene (wall, floor, lights, camera) -> .blend + renders.

Units: metres.  Wall plane is XZ at y=0, panel grows towards -Y.
"""
import bpy, bmesh, math, sys, os, time, argparse, json
import numpy as np
from mathutils import Vector

# ----------------------------------------------------------------- args
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True, help="height map (PNG/JPG/TIF/EXR), white = closer to viewer")
ap.add_argument("--out", required=True, help="output folder")
ap.add_argument("--name", default="panel")
ap.add_argument("--width", type=float, default=1.0)       # m
ap.add_argument("--height", type=float, default=1.0)      # m
ap.add_argument("--thickness", type=float, default=0.018) # plywood / slat thickness
ap.add_argument("--gap", type=float, default=0.018)       # gap between slats
ap.add_argument("--border", type=float, default=0.02)     # min base reveal left/right
ap.add_argument("--min-depth", type=float, default=0.05)  # slat depth where map is black
ap.add_argument("--amp", type=float, default=0.12)        # extra depth where map is white
ap.add_argument("--end-fade", type=float, default=0.0)    # length over which slat depth eases out at top/bottom (0 = square ends)
ap.add_argument("--end-depth", type=float, default=0.02)  # slat depth right at the top/bottom end when fading
ap.add_argument("--invert", action="store_true")
ap.add_argument("--smooth", type=float, default=0.0, help="gaussian blur sigma in px of the map")
ap.add_argument("--rows", type=int, default=4)
ap.add_argument("--profile-res", type=float, default=240, help="profile points per metre along the slat")
ap.add_argument("--render", default="full", choices=["none", "preview", "full"])
ap.add_argument("--samples-fhd", type=int, default=384)
ap.add_argument("--samples-4k", type=int, default=256)
A = ap.parse_args(argv)
os.makedirs(A.out, exist_ok=True)

W, H, T, GAP = A.width, A.height, A.thickness, A.gap
TAB_H = 0.050            # hook height (tip to top)
WALL_CLR = 0.001         # hook is 1 mm shorter than base thickness
L = T - WALL_CLR         # hook depth; 45 deg bevel -> vertical drop = L
SLOT_W = T + 0.002       # 1 mm clearance each side
SLOT_H = TAB_H + 0.005   # height of straight through-slot
# The 45 deg hook bevel rests on the edge in the MIDDLE of the base thickness:
# a straight through-slot + a pocket milled from the back (base is flipped once).
# Everything is cut with a straight end mill, no V-bit needed.
POCKET_D = T / 2         # back pocket depth (from the wall side)
POCKET_W = T + 0.006     # wider than slot so the 6 mm cutter's corner radii never touch the hook
POCKET_BELOW = POCKET_D + 0.002   # how far the pocket goes below the slot bottom (hook tip + 2 mm)
POCKET_ABOVE = 0.006     # overlap into the slot
HOLE_D, CSK_D = 0.006, 0.012
yF, yB = -T, 0.0         # base front / back
# The base shows a reveal ("border") on the left and right only; slats run the full height.
pitch = T + GAP
n = max(1, int(math.floor((W - 2 * A.border + GAP) / pitch + 1e-9)))
span = n * T + (n - 1) * GAP
BORDER = (W - span) / 2          # actual reveal (>= --border)
HS = H                           # slat height = full base height (user decision)

# ----------------------------------------------------------------- reset
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.unit_settings.system = 'METRIC'
try: sc.unit_settings.length_unit = 'CENTIMETERS'
except Exception: pass

def coll(name, parent=None):
    c = bpy.data.collections.new(name); (parent or sc.collection).children.link(c); return c

# ----------------------------------------------------------------- height map
img = bpy.data.images.load(os.path.abspath(A.input))
img.colorspace_settings.name = 'Non-Color'
iw, ih = img.size
px = np.empty(iw * ih * img.channels, np.float32); img.pixels.foreach_get(px)
px = px.reshape(ih, iw, img.channels)
G = px[:, :, 0] if img.channels < 3 else (0.2126 * px[:, :, 0] + 0.7152 * px[:, :, 1] + 0.0722 * px[:, :, 2])
if A.smooth > 0:
    s = A.smooth; r = int(3 * s) + 1; k = np.exp(-np.arange(-r, r + 1) ** 2 / (2 * s * s)); k /= k.sum()
    G = np.apply_along_axis(lambda m: np.convolve(np.pad(m, r, mode='edge'), k, 'valid'), 1, G)
    G = np.apply_along_axis(lambda m: np.convolve(np.pad(m, r, mode='edge'), k, 'valid'), 0, G)
G = (G - G.min()) / max(G.max() - G.min(), 1e-9)
if A.invert: G = 1 - G
# save the normalised map next to the .blend (16-bit PNG) so the scene is self-contained
import zlib, struct
def write_png16(path, arr):  # arr: rows bottom-up (Blender order), values 0..1
    u = (np.clip(arr[::-1], 0, 1) * 65535 + 0.5).astype('>u2')
    raw = b''.join(b'\x00' + u[i].tobytes() for i in range(u.shape[0]))
    ch = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n' + ch(b'IHDR', struct.pack('>IIBBBBB', u.shape[1], u.shape[0], 16, 0, 0, 0, 0))
                           + ch(b'IDAT', zlib.compress(raw, 9)) + ch(b'IEND', b''))
hpath = os.path.join(A.out, "height_map.png"); write_png16(hpath, G)
norm = bpy.data.images.load(hpath); norm.colorspace_settings.name = 'Non-Color'

def Hm(u, v):  # bilinear, u,v in 0..1, v=0 bottom
    fx = np.clip(u, 0, 1) * (iw - 1); fy = np.clip(v, 0, 1) * (ih - 1)
    x0 = np.floor(fx).astype(int); y0 = np.floor(fy).astype(int)
    x1 = np.minimum(x0 + 1, iw - 1); y1 = np.minimum(y0 + 1, ih - 1); ax = fx - x0; ay = fy - y0
    return (G[y0, x0] * (1 - ax) + G[y0, x1] * ax) * (1 - ay) + (G[y1, x0] * (1 - ax) + G[y1, x1] * ax) * ay

# ----------------------------------------------------------------- materials
def principled(name):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; return m, nt, next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")

def wood(name, dark, light):
    m, nt, b = principled(name)
    tc = nt.nodes.new("ShaderNodeTexCoord"); mp = nt.nodes.new("ShaderNodeMapping")
    mp.inputs["Scale"].default_value = (60, 60, 1.5)      # grain along Z (slat length)
    nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 4
    nz.inputs["Detail"].default_value = 6; nz.inputs["Distortion"].default_value = 0.6
    cr = nt.nodes.new("ShaderNodeValToRGB")
    cr.color_ramp.elements[0].position = 0.35; cr.color_ramp.elements[1].position = 0.7
    cr.color_ramp.elements[0].color = (*dark, 1); cr.color_ramp.elements[1].color = (*light, 1)
    nt.links.new(tc.outputs["Object"], mp.inputs["Vector"]); nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
    nt.links.new(nz.outputs["Fac"], cr.inputs["Fac"]); nt.links.new(cr.outputs["Color"], b.inputs["Base Color"])
    b.inputs["Roughness"].default_value = 0.5
    for k, v in (("Coat Weight", 0.12), ("Coat Roughness", 0.35)):
        if k in b.inputs: b.inputs[k].default_value = v
    m.diffuse_color = (*light, 1); return m

M_SLAT = wood("Фанера_ламелі", (0.040, 0.019, 0.009), (0.105, 0.052, 0.025))
M_BASE = wood("Фанера_база", (0.050, 0.025, 0.012), (0.12, 0.062, 0.03))

# ----------------------------------------------------------------- geometry helpers
def prism(bm, x0, x1, poly):
    a = [bm.verts.new((x0, y, z)) for y, z in poly]; b = [bm.verts.new((x1, y, z)) for y, z in poly]
    bm.faces.new(a[::-1]); bm.faces.new(b); n = len(poly)
    for k in range(n): bm.faces.new((a[k], a[(k + 1) % n], b[(k + 1) % n], b[k]))

def obj_from_bm(bm, name, col, mat):
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new(name); bm.to_mesh(me); bm.free(); me.materials.append(mat)
    o = bpy.data.objects.new(name, me); col.objects.link(o); return o

# ----------------------------------------------------------------- relief (reference only)
cR = coll("Рельєф (джерело)")
bpy.ops.mesh.primitive_grid_add(x_subdivisions=300, y_subdivisions=300, size=1.0)
rel = bpy.context.object; rel.name = "Relief"
for c in rel.users_collection: c.objects.unlink(rel)
cR.objects.link(rel)
rel.scale = (span, HS, 1); rel.rotation_euler = (math.radians(90), 0, 0)
rel.location = (0, yF - A.min_depth, HS / 2)
tex = bpy.data.textures.new("height", 'IMAGE'); tex.image = norm; tex.extension = 'EXTEND'
d = rel.modifiers.new("Displace", 'DISPLACE'); d.texture = tex; d.texture_coords = 'UV'
d.strength = A.amp; d.mid_level = 0.0
cR.hide_render = True
bpy.context.view_layer.layer_collection.children[cR.name].hide_viewport = True

# ----------------------------------------------------------------- slats
xs = [-span / 2 + T / 2 + i * pitch for i in range(n)]
rows = [H * (0.10 + 0.78 * k / (A.rows - 1)) for k in range(A.rows)] if A.rows > 1 else [H * 0.5]
zs = np.linspace(0, HS, max(120, int(HS * A.profile_res)) + 1)
cS = coll("Ламелі")
SLAT_2D = []
for i, cx in enumerate(xs):
    yb = yF
    prof = [(yb, 0.0)]
    for zc in rows:                         # zc: slot bottom = contact edge at mid-thickness
        zf = zc + POCKET_D                  # bevel meets the slat back edge here (base front face)
        ztip = zf - L
        prof += [(yb, zf), (yb + L, ztip), (yb + L, ztip + TAB_H), (yb, ztip + TAB_H)]
    prof.append((yb, HS))
    dep = A.min_depth + A.amp * Hm(np.full_like(zs, (cx + span / 2) / span), zs / HS)
    if A.end_fade > 0:   # smooth ease-out of the depth towards both ends (rounded silhouette, cut as one 2D contour)
        e = np.clip(np.minimum(zs, HS - zs) / A.end_fade, 0, 1)
        e = np.sin(e * np.pi / 2)                       # quarter-circle-like ease: vertical tangent at the end
        dep = A.end_depth + (dep - A.end_depth) * e
    yf = yb - dep
    prof += [(float(yf[k]), float(zs[k])) for k in range(len(zs) - 1, -1, -1)]
    bm = bmesh.new(); prism(bm, cx - T / 2, cx + T / 2, prof)
    obj_from_bm(bm, f"Ламель_{i + 1:02d}", cS, M_SLAT)
    # 2D part for CNC, mm: u = along slat (bottom->top), v = depth from back edge (front > 0, hooks < 0)
    SLAT_2D.append([[round(z * 1000, 3), round((yb - y) * 1000, 3)] for y, z in prof])

# ----------------------------------------------------------------- base with slots + dowel holes
cB = coll("База")
bm = bmesh.new(); prism(bm, -W / 2, W / 2, [(yF, 0), (yB, 0), (yB, H), (yF, H)])
base = obj_from_bm(bm, "База_фанера", cB, M_BASE)
bm = bmesh.new(); e = 0.003
for zc in rows:
    slot = [(yF - e, zc), (yB + e, zc), (yB + e, zc + SLOT_H), (yF - e, zc + SLOT_H)]
    pock = [(yB - POCKET_D, zc - POCKET_BELOW), (yB + e, zc - POCKET_BELOW), (yB + e, zc + POCKET_ABOVE), (yB - POCKET_D, zc + POCKET_ABOVE)]
    for cx in xs:
        prism(bm, cx - SLOT_W / 2, cx + SLOT_W / 2, slot)
        prism(bm, cx - POCKET_W / 2, cx + POCKET_W / 2, pock)
hole_x = [xs[1], xs[n // 2], xs[-2]]
hole_z = [(a + b + SLOT_H) / 2 for a, b in zip(rows, rows[1:])] or [H / 2]
r1, r2 = HOLE_D / 2, CSK_D / 2
hp = [(r1, yB + 0.004), (r1, yF + (r2 - r1)), (r2 + 0.002, yF - 0.002)]   # countersink 90 deg
SEG = 32
for x in hole_x:
    for z in hole_z:
        rings = [[bm.verts.new((x + r * math.cos(2 * math.pi * k / SEG), y, z + r * math.sin(2 * math.pi * k / SEG)))
                  for k in range(SEG)] for r, y in hp]
        for a, b in zip(rings, rings[1:]):
            for k in range(SEG): bm.faces.new((a[k], a[(k + 1) % SEG], b[(k + 1) % SEG], b[k]))
        bm.faces.new(rings[0]); bm.faces.new(rings[-1][::-1])
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
cm = bpy.data.meshes.new("cutter"); bm.to_mesh(cm); bm.free()
cut = bpy.data.objects.new("cutter", cm); sc.collection.objects.link(cut)
mod = base.modifiers.new("Пази", 'BOOLEAN'); mod.operation = 'DIFFERENCE'; mod.object = cut; mod.solver = 'EXACT'; mod.use_self = True
dg = bpy.context.evaluated_depsgraph_get()
nm = bpy.data.meshes.new_from_object(base.evaluated_get(dg)); base.modifiers.clear()
old = base.data; base.data = nm; bpy.data.meshes.remove(old); nm.name = "База_фанера"
bpy.data.objects.remove(cut); bpy.data.meshes.remove(cm)
bmc = bmesh.new(); bmc.from_mesh(nm); nonman = sum(1 for e in bmc.edges if not e.is_manifold); bmc.free()

# ----------------------------------------------------------------- CNC geometry (mm, base seen from the FRONT, origin bottom-left)
mm = lambda v: round(v * 1000, 3)
cnc = dict(
    units="mm", thickness=mm(T),
    slats=[dict(id=i + 1, outline=p) for i, p in enumerate(SLAT_2D)],
    base=dict(
        size=[mm(W), mm(H)],
        slots=[dict(row=r + 1, slat=i + 1, x0=mm(cx - SLOT_W / 2 + W / 2), x1=mm(cx + SLOT_W / 2 + W / 2),
                    z0=mm(zc), z1=mm(zc + SLOT_H)) for r, zc in enumerate(rows) for i, cx in enumerate(xs)],
        back_pockets=[dict(x0=mm(cx - POCKET_W / 2 + W / 2), x1=mm(cx + POCKET_W / 2 + W / 2),
                           z0=mm(zc - POCKET_BELOW), z1=mm(zc + POCKET_ABOVE), depth=mm(POCKET_D))
                      for zc in rows for cx in xs],
        holes=[dict(x=mm(x + W / 2), z=mm(z), d=mm(HOLE_D), countersink_d=mm(CSK_D)) for x in hole_x for z in hole_z]),
    assembly=dict(lift_to_insert_mm=round((L - POCKET_D) * 1000 + 1, 1)))
json.dump(cnc, open(os.path.join(A.out, "cnc_geometry.json"), "w"), indent=0)

# ----------------------------------------------------------------- studio scene
env = coll("Сцена")
FLOOR = -0.75 * H
m, nt, b = principled("Стіна")
b.inputs["Base Color"].default_value = (0.52, 0.50, 0.47, 1); b.inputs["Roughness"].default_value = 0.92
nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 180; nz.inputs["Detail"].default_value = 8
bp = nt.nodes.new("ShaderNodeBump"); bp.inputs["Strength"].default_value = 0.04
nt.links.new(nz.outputs["Fac"], bp.inputs["Height"]); nt.links.new(bp.outputs["Normal"], b.inputs["Normal"])
M_WALL = m
m, nt, b = principled("Підлога")
nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 3; nz.inputs["Detail"].default_value = 10
cr = nt.nodes.new("ShaderNodeValToRGB")
cr.color_ramp.elements[0].color = (0.12, 0.12, 0.115, 1); cr.color_ramp.elements[1].color = (0.24, 0.235, 0.22, 1)
nt.links.new(nz.outputs["Fac"], cr.inputs["Fac"]); nt.links.new(cr.outputs["Color"], b.inputs["Base Color"])
b.inputs["Roughness"].default_value = 0.4
M_FLOOR = m

def plane(name, sx, sy, loc, rot, mat):
    me = bpy.data.meshes.new(name); bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=0.5); bm.to_mesh(me); bm.free()
    me.materials.append(mat); o = bpy.data.objects.new(name, me); env.objects.link(o)
    o.location = loc; o.rotation_euler = rot; o.scale = (sx, sy, 1); return o
S = max(W, H)
plane("Стіна", 8 * S, 5 * S, (0, 0.0005, FLOOR + 2.5 * S), (math.radians(-90), 0, 0), M_WALL)
plane("Підлога", 8 * S, 6 * S, (0, -3 * S, FLOOR), (0, 0, 0), M_FLOOR)

centre = Vector((0, -0.06, H / 2))
def light(name, loc, power, color, size):
    ld = bpy.data.lights.new(name, 'AREA'); ld.energy = power * S * S; ld.color = color
    ld.shape = 'RECTANGLE'; ld.size, ld.size_y = size[0] * S, size[1] * S
    o = bpy.data.objects.new(name, ld); env.objects.link(o); o.location = Vector(loc) * S + Vector((0, 0, 0))
    o.rotation_euler = (centre - o.location).to_track_quat('-Z', 'Y').to_euler(); return o
light("Ключове_зліва_ковзне", (-1.9, -0.45, 1.35), 520, (1.0, 0.84, 0.66), (0.25, 1.3))   # raking light shows relief
light("Заповнення", (1.8, -2.6, 1.0), 30, (0.82, 0.9, 1.0), (2.0, 2.0))
light("Контр_зверху", (0.2, -0.5, 2.3), 45, (1.0, 0.95, 0.9), (1.5, 0.4))
w = bpy.data.worlds.new("World"); w.use_nodes = True; sc.world = w
bg = next(n for n in w.node_tree.nodes if n.type == "BACKGROUND")
bg.inputs[0].default_value = (0.6, 0.62, 0.66, 1); bg.inputs[1].default_value = 0.04

cd = bpy.data.cameras.new("Camera"); cd.lens = 42
cam = bpy.data.objects.new("Camera", cd); env.objects.link(cam); sc.camera = cam
# frame by the panel's real proportions (16:9 frame, 3/4 view from the right)
look_at = Vector((0.0, -0.08, 0.47 * H))
dist = 2.48 * max(W, 1.2 * H)
cam.location = look_at + Vector((1.45, -2.62, 0.08)).normalized() * dist
cam.rotation_euler = (look_at - cam.location).to_track_quat('-Z', 'Y').to_euler()

# ----------------------------------------------------------------- render settings
try: sc.render.engine = 'CYCLES'
except TypeError as err: print("engine:", err)
try:
    pr = bpy.context.preferences.addons['cycles'].preferences
    for t in ('METAL', 'OPTIX', 'CUDA', 'HIP', 'ONEAPI'):
        try: pr.compute_device_type = t; break
        except TypeError: pass
    pr.get_devices()
    for dv in pr.devices: dv.use = dv.type != 'CPU'
    if any(dv.use for dv in pr.devices): sc.cycles.device = 'GPU'
except Exception as err: print("gpu:", err)
sc.cycles.use_adaptive_sampling = True; sc.cycles.adaptive_threshold = 0.01; sc.cycles.use_denoising = True
for prop, val in (("view_transform", "AgX"), ("look", "AgX - Medium High Contrast")):
    try: setattr(sc.view_settings, prop, val)
    except TypeError: pass
sc.view_settings.exposure = 0.0
sc.render.image_settings.file_format = 'PNG'; sc.render.image_settings.color_depth = '8'
sc.render.image_settings.color_mode = 'RGB'

blend = os.path.join(A.out, f"{A.name}.blend")
bpy.ops.wm.save_as_mainfile(filepath=blend, relative_remap=True)

renders = []
def do_render(tag, rx, ry, samples):
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = rx, ry, 100
    sc.cycles.samples = samples
    p = os.path.join(A.out, "renders", f"{A.name}_{tag}.png"); sc.render.filepath = p
    t = time.time(); bpy.ops.render.render(write_still=True); renders.append((p, round(time.time() - t)))
    print(f"RENDER {tag} {rx}x{ry} {round(time.time() - t)}s -> {p}", flush=True)
if A.render == "preview": do_render("preview", 960, 540, 48)
elif A.render == "full":
    do_render("FullHD", 1920, 1080, A.samples_fhd)
    do_render("4K", 3840, 2160, A.samples_4k)
# leave the file at FullHD settings
sc.render.resolution_x, sc.render.resolution_y = 1920, 1080; sc.cycles.samples = A.samples_fhd
bpy.ops.wm.save_mainfile()

report = dict(blend=blend, slats=n, slat_span_cm=round(span * 100, 1), rows_cm=[round(r * 100, 1) for r in rows],
              holes=len(hole_x) * len(hole_z), base_non_manifold_edges=nonman,
              border_cm=round(BORDER * 100, 2), depth_range_cm=[round(A.min_depth * 100, 1), round((A.min_depth + A.amp) * 100, 1)], renders=renders)
json.dump(report, open(os.path.join(A.out, "report.json"), "w"), ensure_ascii=False, indent=1)
print("REPORT " + json.dumps(report, ensure_ascii=False), flush=True)
