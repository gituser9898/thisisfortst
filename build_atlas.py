#!/usr/bin/env python3
"""
build_atlas.py — packs the game's animated spritesheets into Phaser-compatible
texture atlases, then assembles a dist/ folder with every asset as a real file
(no base64 embedding). Drop the contents of dist/ onto Vercel as-is.

Usage:
    python3 build_atlas.py [--padding N] [--extrude N] [--max N]

Outputs (inside dist/):
    index.html          ← the game HTML (references assets by filename)
    atlas0.png          ← packed spritesheet atlas page(s)
    atlas0_lo.png       ← lower-res tiers for phones
    atlas0_md.png
    ancient.ttf         ← font (copied from source folder)
    loading.png         ← standalone loading screen image
    intro.mp4           ← video files (if present)
    intro2.mp4
    menu.mp4
    menu_music.mp3      ← audio files (if present)
    … etc …
    manifest.json       ← PWA manifest
    sw.js               ← service worker (works offline after first load)
    icon-192.png        ← PWA icons
    icon-512.png
    vercel.json         ← Vercel routing / cache headers config
"""

import base64
import io
import json
import mimetypes
import math
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "index.html")
DIST_DIR = os.path.join(HERE, "dist")
OUTPUT = os.path.join(DIST_DIR, "index.html")
ATLAS_DIR = os.path.join(DIST_DIR, "_atlas_tmp")   # temp; cleaned up after
PHASER_CDN = "https://cdn.jsdelivr.net/npm/phaser@3.90.0/dist/phaser.min.js"
DEFAULT_MAX = 1280

# Atlas packing config
PADDING = 2
EXTRUDE = 1
PAGE_SIZES = [256, 512, 1024, 2048, 4096, 8192]

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".MP4", ".WEBM", ".MOV")
AUDIO_EXT = (".mp3", ".ogg", ".wav", ".m4a", ".MP3", ".OGG", ".WAV", ".M4A")
FONT_EXT  = (".ttf", ".otf", ".woff", ".woff2", ".TTF", ".OTF", ".WOFF", ".WOFF2")
FONT_MIMES = {".ttf": "font/ttf", ".otf": "font/otf",
              ".woff": "font/woff", ".woff2": "font/woff2"}

# Resolution tiers for the atlas PNG(s).
# "hi" is the full size; "md" / "lo" are scaled-down copies for phones.
ATLAS_TIERS = [("lo", 2.0 / 3.0), ("md", 5.0 / 6.0), ("hi", 1.0)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def flag_value(name, fallback, cast=int):
    if name not in sys.argv:
        return fallback
    spot = sys.argv.index(name)
    if spot + 1 >= len(sys.argv):
        sys.exit("%s needs a value after it" % name)
    try:
        return cast(sys.argv[spot + 1])
    except ValueError:
        sys.exit("%s got a bad value: %r" % (name, sys.argv[spot + 1]))


def next_pow2(n):
    p = 1
    while p < n:
        p *= 2
    return max(p, 1)


def load_pillow():
    try:
        from PIL import Image
        return Image
    except ImportError:
        sys.exit("Pillow is required: pip install pillow --break-system-packages")


def read_recipe(html):
    hit = re.search(r'<script[^>]+id=["\']pack-config["\'][^>]*>(.*?)</script>',
                    html, re.DOTALL | re.IGNORECASE)
    if not hit:
        sys.exit('index.html has no <script id="pack-config"> block')
    try:
        blob = json.loads(hit.group(1))
    except ValueError as bad:
        sys.exit("pack-config in index.html is not valid JSON: %s" % bad)
    entries = blob.get("assets", {})
    tidy = {}
    for key, val in entries.items():
        if isinstance(val, str):
            val = {"file": val}
        tidy[key] = val
    cap = blob.get("max", DEFAULT_MAX)
    base_url = str(blob.get("baseUrl", "") or "")
    return tidy, cap, base_url


def find_asset(ref):
    """Locate an asset file relative to HERE, trying extension variants."""
    direct = os.path.join(HERE, ref)
    if os.path.exists(direct):
        return direct
    stem, ref_ext = os.path.splitext(ref)
    if ref_ext in AUDIO_EXT:
        candidates = AUDIO_EXT
    elif ref_ext in VIDEO_EXT:
        candidates = VIDEO_EXT
    elif ref_ext in FONT_EXT:
        candidates = FONT_EXT
    elif ref_ext in IMAGE_EXT:
        candidates = IMAGE_EXT
    else:
        candidates = IMAGE_EXT + VIDEO_EXT + AUDIO_EXT + FONT_EXT
    for ext in candidates:
        alt = os.path.join(HERE, stem + ext)
        if os.path.exists(alt):
            return alt
    return None


def is_video(path): return os.path.splitext(path)[1].lower() in VIDEO_EXT
def is_audio(path): return os.path.splitext(path)[1].lower() in AUDIO_EXT
def is_font(path):  return os.path.splitext(path)[1].lower() in FONT_EXT
def is_image(path): return os.path.splitext(path)[1].lower() in IMAGE_EXT


def copy_to_dist(src_path, dist_dir=None):
    """Copy a file into dist/ under its basename; return the basename."""
    if dist_dir is None:
        dist_dir = DIST_DIR
    dest = os.path.join(dist_dir, os.path.basename(src_path))
    if os.path.abspath(dest) != os.path.abspath(src_path):
        shutil.copy2(src_path, dest)
    return os.path.basename(src_path)


# ---------------------------------------------------------------------------
# 1. Extract frames from spritesheets
# ---------------------------------------------------------------------------

def resize_sheet_if_needed(Image, img, cols, rows, cap):
    w, h = img.size
    longest = max(w, h)
    if longest <= cap:
        return img
    ratio = cap / float(longest)
    fw = max(1, round(w / cols * ratio))
    fh = max(1, round(h / rows * ratio))
    target = (fw * cols, fh * rows)
    return img.resize(target, Image.LANCZOS)


def slice_frames(Image, path, cols, rows, frame_count, cap):
    cols = cols or 1
    rows = rows or 1
    frame_count = frame_count or 1
    img = Image.open(path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img = resize_sheet_if_needed(Image, img, cols, rows, cap)
    fw = img.width // cols
    fh = img.height // rows
    frames = []
    for i in range(frame_count):
        col, row = i % cols, i // cols
        cell = img.crop((col * fw, row * fh, (col + 1) * fw, (row + 1) * fh))
        frames.append(cell)
    return frames, fw, fh


# ---------------------------------------------------------------------------
# 2. Pack frames into power-of-two atlas page(s)
# ---------------------------------------------------------------------------

def squared(Image, frame_img, min_side=8):
    side = max(min_side, frame_img.width, frame_img.height)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(frame_img, (0, 0))
    return sq


def shelf_pack(items, size, padding, extrude):
    shelves = []
    placements = {}
    leftover = []
    y_cursor = padding
    for name, w, h in items:
        aw, ah = w + 2 * extrude, h + 2 * extrude
        best_idx, best_waste = None, None
        for idx, shelf in enumerate(shelves):
            if shelf["x"] + aw + padding <= size and shelf["h"] >= ah:
                waste = shelf["h"] - ah
                if best_waste is None or waste < best_waste:
                    best_idx, best_waste = idx, waste
        if best_idx is not None:
            shelf = shelves[best_idx]
            placements[name] = {"x": shelf["x"] + extrude, "y": shelf["y"] + extrude,
                                 "w": w, "h": h}
            shelf["x"] += aw + padding
            continue
        new_y = y_cursor
        if new_y + ah + padding > size:
            leftover.append((name, w, h))
            continue
        shelves.append({"y": new_y, "h": ah, "x": padding + aw + padding})
        placements[name] = {"x": padding + extrude, "y": new_y + extrude, "w": w, "h": h}
        y_cursor = new_y + ah + padding
    return placements, leftover


def pack_page(items, padding, extrude):
    items = sorted(items, key=lambda t: -t[2])
    for size in PAGE_SIZES:
        placements, leftover = shelf_pack(items, size, padding, extrude)
        if not leftover:
            return size, placements, []
    size = PAGE_SIZES[-1]
    placements, leftover = shelf_pack(items, size, padding, extrude)
    return size, placements, leftover


def render_page(Image, size, placements, squared_images):
    atlas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for name, rect in placements.items():
        frame = squared_images[name]
        fx, fy, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
        atlas.paste(frame, (fx, fy))
        if EXTRUDE > 0:
            left   = frame.crop((0, 0, 1, h))
            right  = frame.crop((w - 1, 0, w, h))
            top    = frame.crop((0, 0, w, 1))
            bottom = frame.crop((0, h - 1, w, h))
            for e in range(1, EXTRUDE + 1):
                atlas.paste(left,   (fx - e, fy))
                atlas.paste(right,  (fx + w + e - 1, fy))
                atlas.paste(top,    (fx, fy - e))
                atlas.paste(bottom, (fx, fy + h + e - 1))
            tl = frame.crop((0, 0, 1, 1))
            tr = frame.crop((w - 1, 0, w, 1))
            bl = frame.crop((0, h - 1, 1, h))
            br = frame.crop((w - 1, h - 1, w, h))
            atlas.paste(tl, (fx - 1, fy - 1))
            atlas.paste(tr, (fx + w, fy - 1))
            atlas.paste(bl, (fx - 1, fy + h))
            atlas.paste(br, (fx + w, fy + h))
    return atlas


def texturepacker_json(image_name, size, placements, content_sizes):
    frames = []
    for name, rect in placements.items():
        cw, ch = content_sizes[name]
        frames.append({
            "filename": name,
            "frame": {"x": rect["x"], "y": rect["y"], "w": rect["w"], "h": rect["h"]},
            "rotated": False, "trimmed": False,
            "spriteSourceSize": {"x": 0, "y": 0, "w": rect["w"], "h": rect["h"]},
            "sourceSize": {"w": rect["w"], "h": rect["h"]},
            "content": {"w": cw, "h": ch},
        })
    frames.sort(key=lambda f: f["filename"])
    return {
        "frames": frames,
        "meta": {
            "app": "build_atlas.py", "version": "1.0",
            "image": image_name, "format": "RGBA8888",
            "size": {"w": size, "h": size}, "scale": 1,
        },
    }


# ---------------------------------------------------------------------------
# 3. Verification
# ---------------------------------------------------------------------------

def verify(Image, pages_meta, frame_images, report_lines):
    ok = True
    seen = set()
    for page_index, (png_path, json_path, size) in enumerate(pages_meta):
        if size not in PAGE_SIZES:
            ok = False
            report_lines.append("  FAIL page %d: size %d not power-of-two" % (page_index, size))
        atlas_img = Image.open(png_path).convert("RGBA")
        data = json.load(open(json_path))
        rects = [f["frame"] for f in data["frames"]]
        for a in range(len(rects)):
            for b in range(a + 1, len(rects)):
                ra, rb = rects[a], rects[b]
                overlap = not (ra["x"] + ra["w"] <= rb["x"] or rb["x"] + rb["w"] <= ra["x"]
                               or ra["y"] + ra["h"] <= rb["y"] or rb["y"] + rb["h"] <= ra["y"])
                if overlap:
                    ok = False
                    report_lines.append("  FAIL page %d: overlap: %s / %s" % (
                        page_index, data["frames"][a]["filename"], data["frames"][b]["filename"]))
        for f in data["frames"]:
            name = f["filename"]
            seen.add(name)
            r = f["frame"]
            c = f.get("content", {"w": r["w"], "h": r["h"]})
            got  = atlas_img.crop((r["x"], r["y"], r["x"] + c["w"], r["y"] + c["h"]))
            want = frame_images.get(name)
            if want is None:
                ok = False
                report_lines.append("  FAIL: %s missing from source" % name)
                continue
            if got.size != want.size:
                ok = False
                report_lines.append("  FAIL: %s size mismatch atlas=%s original=%s" % (
                    name, got.size, want.size))
                continue
            if list(got.getdata()) != list(want.getdata()):
                ok = False
                report_lines.append("  FAIL: %s pixel mismatch" % name)
    missing = set(frame_images.keys()) - seen
    if missing:
        ok = False
        report_lines.append("  FAIL: %d frame(s) not packed: %s" % (len(missing), sorted(missing)[:10]))
    report_lines.append("  %s — %d frame(s) verified across %d page(s)" % (
        "PASS" if ok else "FAIL", len(seen), len(pages_meta)))
    return ok


# ---------------------------------------------------------------------------
# 4. Patch CSS @font-face to plain filenames (fonts live next to index.html)
# ---------------------------------------------------------------------------

def patch_css_fonts(html):
    """Replace url('ancient.ttf') style references — already plain filenames
    in loading mode, so this is mostly a no-op / sanity check.
    Fonts are copied to dist/ by copy_non_atlas_assets()."""
    return html   # filenames stay as-is; copy_non_atlas_assets handles the copy


# ---------------------------------------------------------------------------
# 5. Copy every non-atlas asset (videos, audio, fonts, standalone images)
#    into dist/ and make sure the JS const points at just the filename.
# ---------------------------------------------------------------------------

def copy_non_atlas_assets(html, recipe):
    """Walk the recipe. For each asset that is NOT an atlas-managed spritesheet,
    copy the real file to dist/ and ensure the JS `const X_SRC = "filename"`
    line is pointing at the plain filename (not a data URI or old path)."""
    for const, entry in recipe.items():
        ref = entry.get("file")
        if not ref:
            continue

        asset = find_asset(ref)

        # Determine if this is atlas-managed (a regular image spritesheet /
        # UI image, not marked standalone). If so, skip — the atlas handles it.
        if asset and is_image(asset) and not entry.get("standalone"):
            continue

        # Pattern to find the JS const declaration
        pattern = re.compile(r'(const\s+%s\s*=\s*)"([^"]*)"' % re.escape(const))

        if asset is None:
            if entry.get("optional"):
                html = pattern.sub(lambda m: m.group(1) + '"MISSING"', html, count=1)
                print("  %-16s not found — marked MISSING (optional)" % const)
            else:
                print("  !! %-14s %s — file not found" % (const, ref))
            continue

        # Copy the file into dist/
        dest_name = os.path.basename(asset)
        dest_path = os.path.join(DIST_DIR, dest_name)
        if os.path.abspath(dest_path) != os.path.abspath(asset):
            shutil.copy2(asset, dest_path)

        size_str = human(os.path.getsize(asset))
        kind = ("video" if is_video(asset) else
                "audio" if is_audio(asset) else
                "font"  if is_font(asset)  else "image")
        print("  %-16s %-24s %9s  [%s] → dist/%s" % (
            const, os.path.basename(asset), size_str, kind, dest_name))

        # Patch the HTML: ensure the const points at just the filename
        html = pattern.sub(lambda m, n=dest_name: m.group(1) + '"%s"' % n, html, count=1)

    # Also copy any font referenced in CSS @font-face url('ancient.ttf') etc.
    css_font_re = re.compile(
        r"url\([\"']?([^\"')]+\.(?:ttf|otf|woff2?))[\"']?\)", re.IGNORECASE)
    for m in css_font_re.finditer(html):
        ref = m.group(1)
        if ref.startswith("data:"):
            continue
        asset = find_asset(ref)
        if asset:
            dest_path = os.path.join(DIST_DIR, os.path.basename(asset))
            if os.path.abspath(dest_path) != os.path.abspath(asset):
                shutil.copy2(asset, dest_path)
            print("  %-16s %-24s %9s  [font/css] → dist/%s" % (
                "CSS @font-face", os.path.basename(asset),
                human(os.path.getsize(asset)), os.path.basename(asset)))

    return html


# ---------------------------------------------------------------------------
# 6. Build atlas tiers (lo / md / hi) and write them to dist/
# ---------------------------------------------------------------------------

def scale_meta(meta, scale):
    out = {"frames": [], "meta": dict(meta.get("meta", {}))}
    for f in meta.get("frames", []):
        r, c = f["frame"], f.get("content", {"w": f["frame"]["w"], "h": f["frame"]["h"]})
        x = int(math.floor(r["x"] * scale))
        y = int(math.floor(r["y"] * scale))
        out["frames"].append({
            "filename": f["filename"],
            "frame": {"x": x, "y": y,
                      "w": max(1, int(math.ceil(r["w"] * scale))),
                      "h": max(1, int(math.ceil(r["h"] * scale)))},
            "rotated": False, "trimmed": False,
            "spriteSourceSize": {"x": 0, "y": 0,
                                 "w": max(1, int(math.ceil(r["w"] * scale))),
                                 "h": max(1, int(math.ceil(r["h"] * scale)))},
            "sourceSize": {"w": max(1, int(math.ceil(r["w"] * scale))),
                           "h": max(1, int(math.ceil(r["h"] * scale)))},
            "content": {"w": max(1, int(math.ceil(c["w"] * scale))),
                        "h": max(1, int(math.ceil(c["h"] * scale)))}
        })
    return out


def build_atlas_tiers(Image, pages_meta):
    """Write all tier PNGs to dist/ and return the JS ATLAS_TIERS array."""
    tiers_js = []
    for tier_name, scale in ATLAS_TIERS:
        srcs  = []
        metas = []
        for idx, (png_path, json_path, size) in enumerate(pages_meta):
            base_meta = json.load(open(json_path))
            if scale >= 0.999:
                # "hi" tier — use the original atlas PNG directly
                out_name = "atlas%d.png" % idx
                out_path = os.path.join(DIST_DIR, out_name)
                if os.path.abspath(out_path) != os.path.abspath(png_path):
                    shutil.copy2(png_path, out_path)
                scaled_meta = base_meta
                scaled_meta["meta"]["image"] = out_name
            else:
                out_name = "atlas%d_%s.png" % (idx, tier_name)
                out_path = os.path.join(DIST_DIR, out_name)
                side = max(1, int(round(size * scale)))
                Image.open(png_path).resize((side, side), Image.LANCZOS).save(
                    out_path, format="PNG", optimize=True)
                scaled_meta = scale_meta(base_meta, scale)
                scaled_meta["meta"]["size"] = {"w": side, "h": side}
                scaled_meta["meta"]["image"] = out_name
            srcs.append(out_name)
            metas.append(scaled_meta)
            sz = human(os.path.getsize(out_path))
            print("  %-16s → dist/%-24s %s" % ("atlas [%s]" % tier_name, out_name, sz))
        tiers_js.append({"name": tier_name, "scale": scale, "srcs": srcs, "meta": metas})
    return tiers_js


# ---------------------------------------------------------------------------
# 7. Patch the atlas JS constants in the HTML
# ---------------------------------------------------------------------------

def patch_atlas_in_html(html, tiers_js):
    """Update ATLAS_SRCS (hi-tier filenames) and ATLAS_TIERS in the HTML."""
    hi_tier = next(t for t in tiers_js if t["name"] == "hi")
    srcs_literal = "[" + ", ".join(json.dumps(s) for s in hi_tier["srcs"]) + "]"

    html = re.sub(
        r'const\s+ATLAS_SRCS\s*=\s*\[[^\]]*\]\s*;',
        "const ATLAS_SRCS = %s;" % srcs_literal,
        html, count=1,
    )
    html = re.sub(
        r'const\s+ATLAS_TIERS\s*=\s*\[[^\]]*\]\s*;',
        "const ATLAS_TIERS = %s;" % json.dumps(tiers_js),
        html, count=1,
    )
    # Inline the atlas JSON metadata (tiny — a few KB) directly in the HTML
    # so the browser never needs a separate request for the frame layout.
    all_metas = [t["meta"] for t in tiers_js if t["name"] == "hi"][0]
    html = re.sub(
        r'(<script id="atlas-data" type="application/json">)(.*?)(</script>)',
        lambda m: m.group(1) + json.dumps(all_metas) + m.group(3),
        html, count=1, flags=re.DOTALL,
    )
    # Clear ASSET_BASE_URL (assets are always relative to index.html on Vercel)
    html = re.sub(
        r'const\s+ASSET_BASE_URL\s*=\s*"[^"]*"\s*;',
        'const ASSET_BASE_URL = "";',
        html, count=1,
    )
    return html


# ---------------------------------------------------------------------------
# 8. PWA support files
# ---------------------------------------------------------------------------

def write_pwa_files(html):
    source_icon = find_asset("icon.png")
    icons = []
    for size in (192, 512, 180):
        name = "icon-%d.png" % size
        path = os.path.join(DIST_DIR, name)
        try:
            from PIL import Image, ImageDraw
            if source_icon:
                im = Image.open(source_icon).convert("RGBA")
                side = max(im.width, im.height)
                sq = Image.new("RGBA", (side, side), (6, 18, 28, 255))
                sq.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
                sq.resize((size, size), Image.LANCZOS).save(path)
            elif not os.path.exists(path):
                im = Image.new("RGBA", (size, size), (6, 18, 28, 255))
                d  = ImageDraw.Draw(im)
                m  = size * 0.5
                d.polygon([(m, size * 0.16), (size * 0.80, size * 0.74),
                            (m, size * 0.60), (size * 0.20, size * 0.74)],
                           fill=(255, 233, 168, 255))
                im.save(path)
        except Exception as e:
            print("  ! could not generate %s (%s)" % (name, e))
            continue
        if size != 180:
            icons.append({"src": name, "sizes": "%dx%d" % (size, size),
                          "type": "image/png", "purpose": "any maskable"})

    manifest = {
        "name": "Arash", "short_name": "Arash",
        "start_url": "./index.html",
        "scope": "./",
        "display": "fullscreen",
        "orientation": "landscape",
        "background_color": "#06121c",
        "theme_color": "#06121c",
        "icons": icons,
    }
    with open(os.path.join(DIST_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    # Collect everything in dist/ that the service worker should pre-cache
    precache = ["./", "./index.html", "./manifest.json"]
    for name in sorted(os.listdir(DIST_DIR)):
        lname = name.lower()
        if lname.endswith((".png", ".mp4", ".webm", ".mp3", ".ogg", ".wav",
                           ".m4a", ".ttf", ".otf", ".woff", ".woff2")):
            precache.append("./" + name)

    sw = """// Generated by build_atlas.py — do not edit by hand.
const CACHE = 'arash-v' + %s;
const ASSETS = %s;

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS).catch(() => {})));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

// Cache-first: assets never change without a rebuild (which bumps the cache name).
self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
      if (res && res.status === 200 && res.type === 'basic') {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return res;
    }).catch(() => caches.match('./index.html')))
  );
});
""" % (json.dumps(str(int(time.time()))), json.dumps(precache, indent=2))

    with open(os.path.join(DIST_DIR, "sw.js"), "w", encoding="utf-8") as fh:
        fh.write(sw)

    origin = "from icon.png" if source_icon else "placeholder — add icon.png to replace"
    print("  %-16s manifest.json, sw.js, PWA icons (%s)" % ("pwa", origin))


# ---------------------------------------------------------------------------
# 9. vercel.json — proper cache headers and SPA routing
# ---------------------------------------------------------------------------

def write_vercel_json():
    config = {
        "headers": [
            {
                # Immutable long-lived cache for atlas + media (hash in filename via tiers)
                "source": "/(atlas.*\\.png|.*\\.mp4|.*\\.webm|.*\\.mp3|.*\\.ogg|.*\\.wav|.*\\.m4a|.*\\.ttf|.*\\.otf|.*\\.woff2?)",
                "headers": [
                    {"key": "Cache-Control",
                     "value": "public, max-age=31536000, immutable"}
                ]
            },
            {
                # index.html — always revalidate so players get new builds
                "source": "/index.html",
                "headers": [
                    {"key": "Cache-Control",
                     "value": "public, max-age=0, must-revalidate"}
                ]
            },
            {
                # Service worker must never be cached long
                "source": "/sw.js",
                "headers": [
                    {"key": "Cache-Control",
                     "value": "public, max-age=0, must-revalidate"}
                ]
            }
        ]
    }
    path = os.path.join(DIST_DIR, "vercel.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    print("  %-16s vercel.json" % "vercel config")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(SOURCE):
        sys.exit("index.html not found next to build_atlas.py")

    Image = load_pillow()
    global EXTRUDE
    padding = flag_value("--padding", PADDING)
    extrude = flag_value("--extrude", EXTRUDE)
    EXTRUDE = extrude

    # Clean and recreate dist/
    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)
    os.makedirs(DIST_DIR)
    os.makedirs(ATLAS_DIR)

    with open(SOURCE, "r", encoding="utf-8") as fh:
        html = fh.read()

    recipe, cap, base_url = read_recipe(html)
    cap = flag_value("--max", cap)

    print("Building dist/ folder for Vercel deployment")
    print("=" * 60)

    # ── Step 1: extract frames ─────────────────────────────────────────────
    print("\n[1/5] Extracting frames from image assets ...")
    frame_images  = {}   # name -> real unpadded PIL image
    squared_images = {}  # name -> square-padded PIL image
    content_sizes  = {}  # name -> (w, h) unpadded
    frame_sizes    = []  # (name, side, side)
    atlas_assets   = []

    for const, entry in recipe.items():
        ref = entry.get("file")
        if not ref:
            continue
        asset = find_asset(ref)
        if (asset is None or is_video(asset) or is_audio(asset)
                or is_font(asset) or entry.get("standalone")):
            continue
        atlas_assets.append(const)
        cols, rows, count = entry.get("cols"), entry.get("rows"), entry.get("frames")
        stem = const[:-4]   # strip _SRC
        frames, fw, fh = slice_frames(Image, asset, cols, rows, count, cap)
        for i, fr in enumerate(frames):
            name = "%s_%d" % (stem, i)
            frame_images[name] = fr
            sq = squared(Image, fr)
            squared_images[name] = sq
            content_sizes[name] = (fr.width, fr.height)
            frame_sizes.append((name, sq.width, sq.height))
        print("  %-12s %-26s %d frame(s) @ %dx%d" % (
            stem, os.path.basename(asset), len(frames), fw, fh))

    if not atlas_assets:
        sys.exit("No image assets found to pack — check pack-config in index.html")

    # ── Step 2: pack atlas pages ───────────────────────────────────────────
    print("\n[2/5] Packing atlas page(s) (padding=%d extrude=%d) ..." % (padding, extrude))
    pages_meta = []
    remaining  = frame_sizes
    page_index = 0
    while remaining:
        size, placements, leftover = pack_page(remaining, padding, extrude)
        png_path  = os.path.join(ATLAS_DIR, "atlas%d.png" % page_index)
        json_path = os.path.join(ATLAS_DIR, "atlas%d.json" % page_index)
        atlas_img = render_page(Image, size, placements, squared_images)
        atlas_img.save(png_path, format="PNG", optimize=True)
        meta = texturepacker_json("atlas%d.png" % page_index, size, placements, content_sizes)
        with open(json_path, "w") as fh:
            json.dump(meta, fh, indent=2)
        used_px = sum(r["w"] * r["h"] for r in placements.values())
        print("  atlas%-2d  %4dx%-4d  %3d frame(s)  %5.1f%% fill  %s" % (
            page_index, size, size, len(placements),
            100.0 * used_px / (size * size), human(os.path.getsize(png_path))))
        pages_meta.append((png_path, json_path, size))
        remaining = leftover
        page_index += 1
        if leftover:
            print("  (page full — %d frame(s) → next page)" % len(leftover))

    # ── Step 3: verify ────────────────────────────────────────────────────
    print("\n[3/5] Verifying atlas ...")
    report_lines = []
    ok = verify(Image, pages_meta, frame_images, report_lines)
    for line in report_lines:
        print(line)
    if not ok:
        sys.exit("\nAtlas verification FAILED — dist/ was NOT written.")

    # ── Step 4: build tiers and copy all assets to dist/ ──────────────────
    print("\n[4/5] Writing assets to dist/ ...")
    tiers_js = build_atlas_tiers(Image, pages_meta)
    html = copy_non_atlas_assets(html, recipe)
    html = patch_css_fonts(html)
    html = patch_atlas_in_html(html, tiers_js)

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    print("  %-16s → dist/index.html  (%s)" % ("index.html", human(os.path.getsize(OUTPUT))))

    # ── Step 5: PWA + Vercel config ───────────────────────────────────────
    print("\n[5/5] Writing PWA and Vercel config ...")
    write_pwa_files(html)
    write_vercel_json()

    # Clean up temp atlas directory
    shutil.rmtree(ATLAS_DIR, ignore_errors=True)

    # Summary
    total = sum(os.path.getsize(os.path.join(DIST_DIR, f))
                for f in os.listdir(DIST_DIR)
                if os.path.isfile(os.path.join(DIST_DIR, f)))
    files = sorted(os.listdir(DIST_DIR))

    print("\n" + "=" * 60)
    print("dist/ is ready for Vercel!  Total: %s  (%d files)" % (human(total), len(files)))
    print()
    print("Files in dist/:")
    for f in files:
        sz = os.path.getsize(os.path.join(DIST_DIR, f))
        print("  %9s  %s" % (human(sz), f))
    print()
    print("Deploy to Vercel:")
    print("  Option A (CLI):  cd dist && vercel --prod")
    print("  Option B (GUI):  drag the dist/ folder onto vercel.com/new")
    print()
    print("The game will be live at your Vercel URL straight away.")


if __name__ == "__main__":
    main()
