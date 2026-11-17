---
name: slat-panel-from-heightmap
description: Turns a height / depth map image into a parametric plywood slat wall panel in Blender — relief, vertical slats with 45° hooks, a plywood base with slots, back pockets and dowel holes, a lit studio scene, a .blend file, FullHD + 4K renders, and on request Mach3 G-code + DXF sheet nesting for CNC production. Use when the user drops a height map, depth map, "карта висот", "карта глибини" or a grayscale relief image and wants a 3D panel, slat/parametric panel, renders or CNC/Mach3 production files for it ("зроби панель з цієї карти", "розклади на лист", "G-код для стрия", "прожени через панель", "ось нова карта висот").
---

# Slat panel from a height map

One script does the whole pipeline: `build_panel.py` next to this file. It runs headless Blender, so it works whether or not the Blender MCP addon is connected.

## Steps

1. **Get the image.** The user sends a path, often in `~/Downloads`. If it is HEIC, WEBP or another format Blender can't read, convert it with `sips -s format png <in> --out <scratchpad>/<name>.png`.
2. **Look at the map** with Read before building. White means closer to the viewer. If the subject is obviously inverted (a face that would come out pressed in), pass `--invert` and tell the user. If the map is noisy or blocky (JPEG artefacts, an AI depth map), pass `--smooth 2`–`4`.
3. **Choose the output folder:** `~/Documents/3D панель/<NN> <short Ukrainian name>/`, where NN is the next free number in `~/Documents/3D панель/`. Never overwrite an existing folder.
4. **Run a preview first.** It takes about 20 s:
   ```bash
   /Applications/Blender.app/Contents/MacOS/Blender -b --factory-startup \
     --python ~/.claude/skills/slat-panel-from-heightmap/build_panel.py -- \
     --input "<map>" --out "<folder>" --name <latin_name> --render preview
   ```
   Then Read `<folder>/renders/<name>_preview.png`. Check that the relief reads well and is not inverted, too flat or too spiky. If it is, adjust `--amp`, `--min-depth`, `--invert` or `--smooth` and run the preview again.
5. **Run the full render** in the background (`run_in_background: true`). The command is the same with `--render full`. It produces FullHD (384 samples) and 4K (256 samples) on the M1 Pro GPU, which takes a few minutes. Grep the output for `RENDER|REPORT|Error|Traceback`.
6. **Report in Ukrainian:**
   - the folder path;
   - the number of slats and the depth range from `report.json`;
   - the render paths;
   - any parameter you changed and why.

   If `base_non_manifold_edges` is above 0, say so: the base mesh is unfit for CNC export until it is fixed.
7. If the user has the file open in Blender and the MCP addon is connected, offer to open the new `.blend` there.
8. **CNC (Mach3)**, when asked for production files or a sheet layout:
   ```bash
   cd <scratchpad> && uv run --no-project --with shapely --with ezdxf --with matplotlib python \
     ~/.claude/skills/slat-panel-from-heightmap/export_cnc.py --geom "<folder>/cnc_geometry.json" --out "<folder>/CNC"
   ```
   - **Defaults** (agreed with the user's uncle): sheet 2500×1250 (`--sheet 1500x1500` is the alternative), 6 mm end mill, step-down 1.5 mm (he allows at most 1–2 mm), final depth 18.5 mm, feed 1500, plunge 300, S18000, Z0 = top of material, conventional milling, tabs 8×3 mm, part spacing and sheet margin 10 mm.
   - **Outputs:** `sheetN_front.tap` (holes → slots → outer contours with tabs, base last), `base_back.tap` (the base flipped left↔right, X0Y0 at its bottom-left corner, 9 mm pockets), DXF files, `sheetN_layout.png` with the real toolpath overlaid, and `cnc_report.json` with minutes per program.
   - **Before reporting:**
     - Read every layout PNG.
     - Check that the G-code is ASCII-only, Z min is −18.5 (front) / −9 (back) and X/Y stay inside the sheet.
     - Confirm tabs exist: grep `Z-15.500`.
   - **Then** write `CNC/README_для_ЧПК.txt` in Ukrainian for the uncle. Use `templates/README_для_ЧПК.txt` next to this file as the template and update the counts and times from `cnc_report.json`.
   - **Countersinks** are done by hand with a countersink bit, because there is no V-bit on the machine.

## Parameters (metres) and current defaults

These defaults are the user's decisions so far. Keep them unless the user asks otherwise.

| flag | default | meaning |
|---|---|---|
| `--width` / `--height` | 1.0 / 1.0 | panel and base size (100×100 cm) |
| `--thickness` | 0.018 | plywood: slats and base (18 mm) |
| `--gap` | 0.018 | gap between slats (18 mm; the user once wrote "18 cm", which meant mm) |
| `--border` | 0.02 | minimum base reveal left/right, which sets the slat count (27 at 1 m) |
| `--min-depth` | 0.05 | slat depth where the map is black |
| `--amp` | 0.12 | extra depth where the map is white (total 5–17 cm) |
| `--rows` | 4 | rows of hooks/slots, at 10 %…88 % of the height |
| `--invert`, `--smooth σpx` | off, 0 | map preprocessing |
| `--profile-res` | 240 | profile points per metre along the slat; use 1000 for production |
| `--samples-fhd` / `--samples-4k` | 384 / 256 | Cycles samples |

**User preference (2026-11-06, panel 03 "хвилі 120х60 v2"):** the user found 3–10 cm relief far too deep and saw steps on the slat edges.
They called the result "в рази краще" with `--min-depth 0.02 --amp 0.03 --end-fade 0.04 --end-depth 0.015 --smooth 10 --profile-res 1000` (2–5 cm).
Start new panels from these values. 8-bit, low-contrast maps get stretched by normalisation, which creates visible steps, so always smooth them.

## Fixed design (agreed with the user, don't change silently)

- **Hook** (4 per slat): a flat tab on the slat's back edge, 17 mm deep (base thickness minus 1 mm, so it never touches the wall) and 50 mm tall. Its bottom is cut at 45°, with the sharp point downward and towards the wall. This follows the user's sketch.
- **Slot** (4 rows × N): a straight through-slot, 20×55 mm. From the back there is also a pocket under each slot: 24×17 mm, 9 mm deep. The hook bevel rests on the edge in the middle of the base thickness ("pointed in the middle").
  - Everything is cut with a straight 6 mm end mill. A V-bit was rejected: its cone would eat the 16 mm bridges between slots.
  - Mounting: lift the slat about 1 cm, push it in, drop it. `clash.py`-style checks confirmed zero overlap when mounted, a free path with a 10 mm lift and a locked slat without the lift.
- **Width:** the base is exactly `--width`. Slats are centred with an equal reveal of base material left and right (≥ `--border` 2 cm, 2.3 cm at 1 m with 27 slats) and run the full height. The user tried flush edges and a top reveal, then explicitly chose side reveals only.
- **No battens.** The base is screwed directly to the wall through 9 holes (3×3) between the slot rows. Each hole is Ø6 mm with a 90° countersink Ø12 mm, for a 5 mm screw and an 8 mm plug. The holes sit on the slat lines so they are hidden after assembly.
- **Scene:** a matte plaster wall and a dark concrete floor. Lighting is a warm raking area light from the left (this is what shows the relief), a weak cool fill and a top rim. The material is dark walnut. Cycles with AgX Medium High Contrast. The camera is at 3/4 from the right.

## Output

`<folder>/` contains:
- `cnc_geometry.json` (2D parts in mm, the input for `export_cnc.py`);
- `<name>.blend`;
- `height_map.png` (the normalised 16-bit map, linked relatively);
- `report.json`;
- `renders/<name>_FullHD.png` and `renders/<name>_4K.png`.

In the `.blend` file, the collection `Рельєф (джерело)` is the hidden source surface, `Ламелі` holds `Ламель_01…`, `База` holds `База_фанера`, and `Сцена` holds the wall, floor, lights and camera.
