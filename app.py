"""
AI Video Generator — Flask Web App
Interactive motion-graphics style: full-screen text, karaoke highlight, no empty space.
"""

import os, math, random, subprocess, threading, uuid, asyncio, re, textwrap, json
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont
import imageio, numpy as np
from eci_scraper import get_tn_results, start_background_fetcher

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["https://tn-election-lyart.vercel.app", "http://localhost:5173"]}})

# Start background ECI scraper once — all user requests are served from cache only
start_background_fetcher()

BASE_DIR   = Path(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = BASE_DIR / "videos"
JOBS_DIR   = BASE_DIR / "jobs"
OUTPUT_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

# ── Persist job state to disk ─────────────────────────────────────────────────
def job_set(job_id, **data):
    path = JOBS_DIR / f"{job_id}.json"
    existing = job_get(job_id) or {}
    existing.update(data)
    path.write_text(json.dumps(existing))

def job_get(job_id):
    path = JOBS_DIR / f"{job_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None

# ── Colours ───────────────────────────────────────────────────────────────────
BG   = (6, 6, 20)
C1   = (0, 210, 255)
C2   = (80, 255, 140)
C3   = (255, 80, 110)
C4   = (255, 205, 50)
C5   = (180, 120, 255)
C6   = (255, 140, 60)
W_C  = (240, 245, 255)
GRAY = (70, 80, 110)
SCENE_COLORS = [C1, C4, C2, C3, C5, C6, C2, C1, C4, C3]

W, H, FPS = 640, 360, 8

# Layout zones
TOP_H  = 30    # top bar
BOT_H  = 38    # subtitle strip
CHAR_W = 148   # left column for character
PANEL_X = CHAR_W + 4

LANGUAGES = {
    "English (US)":  "en-US-JennyNeural",
    "English (UK)":  "en-GB-SoniaNeural",
    "Hindi":         "hi-IN-SwaraNeural",
    "Tamil":         "ta-IN-PallaviNeural",
    "Telugu":        "te-IN-ShrutiNeural",
    "Spanish":       "es-ES-ElviraNeural",
    "French":        "fr-FR-DeniseNeural",
    "German":        "de-DE-KatjaNeural",
    "Arabic":        "ar-EG-SalmaNeural",
    "Japanese":      "ja-JP-NanamiNeural",
    "Chinese":       "zh-CN-XiaoxiaoNeural",
    "Portuguese":    "pt-BR-FranciscaNeural",
}

# ── Font system — per-word script detection, CJK via system font ──────────────
_FONTS_DIR  = BASE_DIR / "fonts"
_FONT_CACHE = {}

def _find_system_font(query):
    try:
        r = subprocess.run(['fc-match', query, '--format=%{file}'],
                           capture_output=True, text=True, timeout=5)
        p = r.stdout.strip()
        if r.returncode == 0 and p and os.path.exists(p):
            return p
    except Exception:
        pass
    return None

# Detect CJK font once at startup
_CJK_FONT = (
    _find_system_font('NotoSansCJK:lang=zh') or
    _find_system_font(':lang=zh') or
    _find_system_font(':lang=ja') or
    ''
)

_SCRIPT_FONTS = [
    (0x0B80, 0x0BFF, 'NotoSansTamil-Regular.ttf',     'NotoSansTamil-Bold.ttf'),
    (0x0900, 0x097F, 'NotoSansDevanagari-Regular.ttf', 'NotoSansDevanagari-Bold.ttf'),
    (0x0C00, 0x0C7F, 'NotoSansTelugu-Regular.ttf',     'NotoSans-Bold.ttf'),
    (0x0600, 0x06FF, 'NotoSansArabic-Regular.ttf',     'NotoSans-Bold.ttf'),
]

def _script_files(text):
    for ch in text:
        cp = ord(ch)
        # CJK (Chinese, Japanese, Korean)
        if (0x3040 <= cp <= 0x30FF or 0x4E00 <= cp <= 0x9FFF or 0xAC00 <= cp <= 0xD7AF):
            if _CJK_FONT:
                return _CJK_FONT, _CJK_FONT
        for lo, hi, reg, bld in _SCRIPT_FONTS:
            if lo <= cp <= hi:
                return reg, bld
    return 'NotoSans-Regular.ttf', 'NotoSans-Bold.ttf'

def font(size, bold=False, text=''):
    reg, bld = _script_files(text) if text else ('NotoSans-Regular.ttf', 'NotoSans-Bold.ttf')
    fname = bld if bold else reg
    fpath = fname if os.path.isabs(fname) else str(_FONTS_DIR / fname)
    key = (fpath, size)
    if key not in _FONT_CACHE:
        try:    _FONT_CACHE[key] = ImageFont.truetype(fpath, size)
        except: _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]

def set_render_lang(_voice):
    global _FONT_CACHE
    _FONT_CACHE = {}

# ── Helpers ───────────────────────────────────────────────────────────────────
def ease_out(t):     return 1 - (1 - max(0.0, min(1.0, t))) ** 3
def ease_in_out(t):  t = max(0.0, min(1.0, t)); return t*t*(3-2*t)
def blend(b, c, a):  return tuple(int(x*(1-a)+y*a) for x, y in zip(b, c))
def rrect(d, box, r=8, fill=None, outline=None, w=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=w)

def _ww(f, word):
    try:    return max(1, int(f.getlength(word)))
    except: bb = f.getbbox(word); return (bb[2]-bb[0]) if bb else 8

def draw_mixed(draw, xy, text, sz, bold=False, fill=W_C, anchor=None,
               shadow=False, max_px=None):
    """Word-by-word rendering — each word uses its correct script font."""
    if not text or not text.strip(): return
    words = text.split()
    if not words: return
    sp = _ww(font(sz, bold), ' ')
    parts = [(w, font(sz, bold, text=w), _ww(font(sz, bold, text=w), w)) for w in words]
    tw = sum(pw for _, _, pw in parts) + sp * max(0, len(parts)-1)
    if max_px and tw > max_px:
        trimmed, acc = [], 0
        for item in parts:
            if acc + item[2] > max_px - 16: break
            trimmed.append(item); acc += item[2] + sp
        parts = trimmed
        tw = sum(pw for _, _, pw in parts) + sp * max(0, len(parts)-1)
    x, y = xy
    if anchor == "mm": x -= tw//2; y -= sz//2
    elif anchor == "rm": x -= tw
    elif anchor == "lm": y -= sz//2
    for i, (w, wf, pw) in enumerate(parts):
        if shadow: draw.text((x+1, y+1), w, font=wf, fill=(0,0,0))
        draw.text((x, y), w, font=wf, fill=fill)
        x += pw + (sp if i < len(parts)-1 else 0)

# ── Background ────────────────────────────────────────────────────────────────
def make_particles():
    random.seed(42)
    return [dict(x=random.uniform(0,W), y=random.uniform(0,H),
                 vx=random.uniform(-0.3,0.3), vy=random.uniform(-0.3,0.3),
                 r=random.uniform(1,3), col=random.choice([C1,C2,C3,C4,C5]),
                 a=random.uniform(0.05,0.18)) for _ in range(35)]

def draw_bg(img, particles, color):
    d = ImageDraw.Draw(img)
    # Grid
    gc = blend(BG, color, 0.07)
    for x in range(0, W+1, 40): d.line([(x,0),(x,H)], fill=gc, width=1)
    for y in range(0, H+1, 40): d.line([(0,y),(W,y)], fill=gc, width=1)
    # Diagonal accent lines (top-left corner decoration)
    ac = blend(BG, color, 0.1)
    for i in range(4):
        off = 30 + i*20
        d.line([(0, off), (off, 0)], fill=ac, width=1)
    # Moving particles
    for p in particles:
        p['x'] = (p['x']+p['vx']) % W
        p['y'] = (p['y']+p['vy']) % H
        c = blend(BG, p['col'], p['a'])
        r = max(1, int(p['r']))
        d.ellipse([p['x']-r, p['y']-r, p['x']+r, p['y']+r], fill=c)

# ── Character ─────────────────────────────────────────────────────────────────
def draw_character(draw, x, y, t, color=C2, scale=1.0, action="talk"):
    s = scale
    bounce  = math.sin(t * math.pi * 2) * 4 * s
    b_top   = y - int(60*s) + int(bounce)
    b_bot   = y + int(10*s) + int(bounce)
    head_y  = y - int(85*s) + int(bounce)
    head_r  = int(28*s)
    # Aura / glow ring
    draw.ellipse([x-int(45*s), b_top-int(10*s), x+int(45*s), b_bot+int(20*s)],
                 fill=blend(BG, color, 0.07))
    # Shadow
    draw.ellipse([x-int(35*s), y+int(12*s), x+int(35*s), y+int(22*s)],
                 fill=blend(BG, (50,50,80), 0.6))
    # Body
    rrect(draw, [x-int(22*s), b_top, x+int(22*s), b_bot], r=8,
          fill=color, outline=blend(color, W_C, 0.3), w=2)
    # Head
    hc = (255, 220, 177)
    draw.ellipse([x-head_r, head_y-head_r, x+head_r, head_y+head_r],
                 fill=hc, outline=blend(hc, W_C, 0.2), width=2)
    # Eyes
    blink = abs(math.sin(t * math.pi * 0.4)) > 0.95
    ey_h = 3 if blink else int(6*s)
    for ex in [x-int(10*s), x+int(10*s)]:
        draw.ellipse([ex-int(5*s), head_y-int(8*s),
                      ex+int(5*s), head_y-int(8*s)+ey_h], fill=(40,40,60))
    # Mouth
    if action == "talk":
        mo = abs(math.sin(t*math.pi*5)) * int(8*s)
        draw.arc([x-int(12*s), head_y+int(4*s), x+int(12*s), head_y+int(14*s)+mo],
                 0, 180, fill=(180,80,80), width=int(3*s))
    else:
        draw.arc([x-int(12*s), head_y+int(4*s), x+int(12*s), head_y+int(16*s)],
                 0, 180, fill=(180,80,80), width=int(3*s))
    # Hair
    draw.arc([x-head_r, head_y-head_r, x+head_r, head_y+int(5*s)],
             200, 340, fill=(80,50,20), width=int(8*s))
    # Arms
    arm = math.sin(t*math.pi*1.5)*0.2
    draw.line([x+int(20*s), b_top+int(15*s),
               x+int(45*s), b_top+int(40*s)+int(bounce)],
              fill=hc, width=int(8*s))
    draw.line([x-int(20*s), b_top+int(15*s),
               x-int((30+30*math.cos(arm))*s),
               b_top+int(15*s)-int(30*math.sin(arm)*s)+int(bounce)],
              fill=hc, width=int(8*s))
    # Legs
    for sign in [-1, 1]:
        lx = x + sign*int(12*s)
        draw.line([lx, b_bot, lx, y+int(55*s)+int(bounce)],
                  fill=blend(color,(30,30,60),0.4), width=int(12*s))
        draw.ellipse([lx-int(12*s), y+int(50*s)+int(bounce),
                      lx+int(12*s), y+int(63*s)+int(bounce)], fill=(40,40,60))

def draw_sound_bars(draw, x, y, t, color, active):
    """Animated equalizer bars — shows voice activity."""
    if not active: return
    for i in range(5):
        bh = int(abs(math.sin(t*math.pi*4 + i*0.9)) * 10 + 3)
        bx = x - 12 + i*6
        draw.rectangle([bx, y-bh, bx+4, y], fill=blend(BG, color, 0.65))

# ── Top bar ───────────────────────────────────────────────────────────────────
def draw_top_bar(draw, topic, idx, total, color, a):
    draw.rectangle([0, 0, W, TOP_H], fill=blend(BG, (15,15,40), 0.98))
    draw.rectangle([0, TOP_H-2, W, TOP_H], fill=blend(BG, color, a*0.7))
    draw_mixed(draw, (W//2, TOP_H//2), topic, 11, bold=True,
               fill=blend(BG, W_C, a*0.92), anchor="mm", max_px=W-90)
    # Scene badge
    btext = f"{idx+1} / {total}"
    bw = _ww(font(9, True), btext) + 14
    bx = W - 5 - bw
    rrect(draw, [bx, 5, W-5, TOP_H-5], r=5,
          fill=blend(BG, color, a*0.22))
    draw_mixed(draw, (W-12, TOP_H//2), btext, 9, bold=True,
               fill=blend(BG, color, a*0.9), anchor="rm")

# ── Left column: scene number + character + sound bars ───────────────────────
def draw_left_column(draw, idx, p, t_anim, color):
    a = ease_out(min(p / 0.15, 1.0))
    if a < 0.02: return

    # Large decorative scene number (fills upper-left)
    cx, cy = CHAR_W // 2, TOP_H + 50
    cr = 38
    # Outer glow ring
    draw.ellipse([cx-cr-4, cy-cr-4, cx+cr+4, cy+cr+4],
                 fill=blend(BG, color, a*0.08))
    # Ring border
    draw.ellipse([cx-cr, cy-cr, cx+cr, cy+cr],
                 fill=blend(BG, color, a*0.15),
                 outline=blend(BG, color, a*0.5), width=2)
    draw_mixed(draw, (cx, cy), str(idx+1), 30, bold=True,
               fill=blend(BG, color, a*0.9), anchor="mm", shadow=False)

    # Character (center-left, bigger scale)
    char_a = ease_out(min((p-0.05)/0.2, 1.0))
    if char_a > 0.04:
        action = "talk" if 0.08 < p < 0.90 else "idle"
        draw_character(draw, CHAR_W//2, 268, t_anim*2,
                       color=color, scale=0.82*char_a, action=action)
        # Sound bars just above subtitle
        if action == "talk":
            draw_sound_bars(draw, CHAR_W//2, H - BOT_H - 6, t_anim*3, color, True)

# ── Right panel: scene name + FULL karaoke text ───────────────────────────────
def draw_narration_panel(draw, idx, name, narration, p, color, total_scenes):
    """Full right panel with scene name + karaoke-style word highlight."""
    a = ease_out(min(p / 0.18, 1.0))
    if a < 0.02: return

    px0 = PANEL_X
    py0 = TOP_H + 4
    px1 = W - 5
    py1 = H - BOT_H - 5

    # Panel card
    rrect(draw, [px0, py0, px1, py1], r=10,
          fill=blend(BG, (16, 16, 44), a*0.97),
          outline=blend(BG, color, a*0.45), w=1)
    # Top color bar
    rrect(draw, [px0, py0, px1, py0+3], r=2,
          fill=blend(BG, color, a*0.85))

    inner_x = px0 + 12
    inner_y = py0 + 10

    # ── Scene name header ──────────────────────────────────────────────────
    # Badge circle
    draw.ellipse([inner_x, inner_y, inner_x+24, inner_y+24],
                 fill=blend(BG, color, a*0.9))
    draw_mixed(draw, (inner_x+12, inner_y+12), str(idx+1), 10, bold=True,
               fill=BG, anchor="mm", shadow=False)

    # Scene name text
    name_lines = textwrap.wrap(name, 22)[:1]
    for nl in name_lines:
        draw_mixed(draw, (inner_x+30, inner_y+2), nl, 15, bold=True,
                   fill=blend(BG, W_C, a),
                   max_px=px1 - inner_x - 38)

    # Thin divider
    div_y = inner_y + 30
    dw = int((px1 - inner_x - 10) * min(a * 3, 1.0))
    draw.rectangle([inner_x, div_y, inner_x + dw, div_y + 1],
                   fill=blend(BG, color, a*0.55))

    # ── Karaoke narration text ─────────────────────────────────────────────
    words = narration.split()
    n_total = len(words)
    if n_total > 0:
        n_shown = int(n_total * min(p * 1.25, 1.0))  # words revealed so far

        TEXT_SZ  = 13
        LINE_H   = 22
        TEXT_Y0  = div_y + 9
        TEXT_W   = px1 - inner_x - 14
        CPL      = max(18, TEXT_W // 7)   # chars per line approx

        lines = textwrap.wrap(narration, CPL)[:5]
        word_cursor = 0

        for li, line in enumerate(lines):
            ty = TEXT_Y0 + li * LINE_H
            if ty + LINE_H > py1 - 22: break
            lwords = line.split()
            xpos = inner_x
            sp_f = font(TEXT_SZ)
            sp_w = _ww(sp_f, ' ')

            for wi, w in enumerate(lwords):
                gidx = word_cursor + wi
                is_current = (gidx == n_shown)
                wf  = font(TEXT_SZ, bold=is_current, text=w)
                wpx = _ww(wf, w)

                if gidx < n_shown:
                    # Already spoken — white
                    wfill = blend(BG, W_C, a * 0.82)
                elif is_current:
                    # Current word — scene color + subtle highlight bg
                    wfill = blend(BG, color, a)
                    rrect(draw, [xpos-2, ty-2, xpos+wpx+3, ty+TEXT_SZ+3], r=3,
                          fill=blend(BG, color, a*0.18))
                else:
                    # Future word — dimmed
                    wfill = blend(BG, GRAY, a*0.55)

                # Shadow + text
                draw.text((xpos+1, ty+1), w, font=wf, fill=(0,0,0))
                draw.text((xpos,   ty),   w, font=wf, fill=wfill)
                xpos += wpx + sp_w

            word_cursor += len(lwords)

    # ── Scene progress dots ────────────────────────────────────────────────
    dot_y = py1 - 13
    for si in range(min(total_scenes, 8)):
        r = 5 if si == idx else 3
        dx = inner_x + si * 14
        draw.ellipse([dx-r, dot_y-r, dx+r, dot_y+r],
                     fill=blend(BG, color if si == idx else GRAY,
                                a*(1.0 if si == idx else 0.4)))

# ── Subtitle bar (small backup) ───────────────────────────────────────────────
def draw_subtitle(draw, text, p, color):
    """Tiny subtitle pill at bottom — 9px, backs up narration panel."""
    words = text.split()
    if not words: return
    n = max(1, int(len(words) * min(p * 1.3, 1.0)))
    visible = ' '.join(words[:n])
    lines = textwrap.wrap(visible, 90)[:1]   # single line only
    if not lines: return

    SZ = 9
    pad = 4
    bh = SZ + pad * 2
    y1 = H - bh - 3
    rrect(draw, [4, y1, W-4, H-3], r=5, fill=(5, 5, 18))
    rrect(draw, [4, y1, 7, H-3], r=3, fill=blend(BG, color, 0.85))

    xpos = 12
    sp_w = _ww(font(SZ), ' ')
    for j, w in enumerate(lines[0].split()):
        wf = font(SZ, text=w)
        draw.text((xpos+1, y1+pad+1), w, font=wf, fill=(0,0,0))
        draw.text((xpos,   y1+pad),   w, font=wf, fill=W_C)
        xpos += _ww(wf, w) + (sp_w if j < len(lines[0].split())-1 else 0)

# ── TTS ───────────────────────────────────────────────────────────────────────
def generate_tts(text, voice, out_file):
    import edge_tts
    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(out_file)
    asyncio.run(_run())

def get_audio_duration(audio_file):
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([ffmpeg, '-i', audio_file], capture_output=True, text=True)
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
    if m:
        h, mn, s = m.groups()
        return int(h)*3600 + int(mn)*60 + float(s)
    return 8.0

def concat_audio(files, out_file):
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    list_file = out_file + ".txt"
    with open(list_file, 'w') as lf:
        for af in files:
            lf.write(f"file '{af}'\n")
    subprocess.run([ffmpeg, '-y', '-f', 'concat', '-safe', '0',
                    '-i', list_file, '-c', 'copy', out_file],
                   capture_output=True)
    os.remove(list_file)

# ── Main video generator ──────────────────────────────────────────────────────
def generate_video(job_id, topic, script, voice):
    try:
        set_render_lang(voice)
        job_set(job_id, status='running', message='Generating voiceover...', progress=5)

        silent_path  = f"/tmp/{job_id}_silent.mp4"
        out_path     = str(OUTPUT_DIR / f"{job_id}.mp4")
        scene_audios = []
        total_scenes = len(script)

        # ── TTS per scene ─────────────────────────────────────────────────
        for i, scene in enumerate(script):
            af = f"/tmp/{job_id}_s{i}.mp3"
            generate_tts(scene['text'], voice, af)
            dur = get_audio_duration(af)
            scene['actual_duration'] = max(dur + 0.5, 3.0)
            scene_audios.append(af)
            pct = 5 + int((i+1)/total_scenes * 20)
            job_set(job_id, progress=pct,
                    message=f'Voice {i+1}/{total_scenes}: {scene["name"]}')

        combined_audio = f"/tmp/{job_id}_audio.mp3"
        concat_audio(scene_audios, combined_audio)

        # ── Render frames ─────────────────────────────────────────────────
        job_set(job_id, message='Rendering animation...', progress=26)
        total_frames = sum(int(s['actual_duration'] * FPS) for s in script)
        particles    = make_particles()

        writer  = imageio.get_writer(silent_path, fps=FPS, quality=8, macro_block_size=1)
        frame_n = 0
        t_abs   = 0.0

        for idx, scene in enumerate(script):
            color     = SCENE_COLORS[idx % len(SCENE_COLORS)]
            duration  = scene['actual_duration']
            narration = scene['text']
            sf        = int(duration * FPS)

            # Static background (particles + grid) — pre-rendered once
            static = Image.new("RGB", (W, H), BG)
            draw_bg(static, particles, color)
            static_arr = np.array(static)

            for fi in range(sf):
                p      = fi / max(sf - 1, 1)
                t_anim = t_abs + fi / FPS

                img  = Image.fromarray(static_arr.copy())
                draw = ImageDraw.Draw(img)

                # UI layers
                bar_a = ease_out(min(p / 0.12, 1.0))
                draw_top_bar(draw, topic, idx, total_scenes, color, bar_a)
                draw_left_column(draw, idx, p, t_anim, color)
                draw_narration_panel(draw, idx, scene['name'], narration,
                                     p, color, total_scenes)
                draw_subtitle(draw, narration, min(p / 0.1, 1.0), color)

                # Scene fade-in from black (first ~5% of scene)
                fade = ease_in_out(min(p / 0.055, 1.0))
                if fade < 0.99:
                    img = Image.blend(Image.new("RGB", (W, H), BG), img, fade)

                # Global progress bar — top 3px, always visible
                fd = ImageDraw.Draw(img)
                bw = int(frame_n / max(total_frames, 1) * W)
                fd.rectangle([0, 0, bw, 3], fill=blend(C1, C2, frame_n/max(total_frames,1)))

                writer.append_data(np.array(img))
                frame_n += 1

            t_abs += duration
            pct = 26 + int(frame_n / total_frames * 60)
            job_set(job_id, progress=pct,
                    message=f'Rendering scene {idx+1}/{total_scenes}')

        writer.close()

        # ── Merge audio ───────────────────────────────────────────────────
        job_set(job_id, message='Merging audio + video...', progress=88)
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg, '-y',
                        '-i', silent_path,
                        '-i', combined_audio,
                        '-c:v', 'copy', '-c:a', 'aac',
                        '-shortest', out_path],
                       capture_output=True)

        job_set(job_id, status='done', progress=100,
                message='Video ready!', file=out_path)

        for f in scene_audios + [combined_audio, silent_path]:
            try: os.remove(f)
            except: pass

    except Exception as e:
        import traceback
        job_set(job_id, status='error', message=str(e))
        print(traceback.format_exc())

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/debug')
def debug():
    files = [f.name for f in _FONTS_DIR.iterdir()] if _FONTS_DIR.exists() else []
    return jsonify({'fonts_dir': str(_FONTS_DIR), 'files': sorted(files),
                    'cjk_font': _CJK_FONT})

@app.route('/')
def index():
    return render_template('index.html', languages=list(LANGUAGES.keys()))

@app.route('/generate', methods=['POST'])
def start_generate():
    data   = request.json
    job_id = str(uuid.uuid4())[:8]
    voice  = LANGUAGES.get(data.get('language', 'English (US)'), 'en-US-JennyNeural')
    job_set(job_id, status='pending', progress=0, message='Queued...', file=None)
    threading.Thread(target=generate_video,
                     args=(job_id, data.get('topic', 'Video'),
                           data.get('script', []), voice),
                     daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def status(job_id):
    job = job_get(job_id)
    return jsonify(job or {'status': 'error', 'message': 'Not found', 'progress': 0})

@app.route('/download/<job_id>')
def download(job_id):
    job = job_get(job_id)
    if not job or not job.get('file'):
        return "Not ready", 404
    fpath = job['file']
    if not os.path.exists(fpath):
        return "File not found", 404
    return send_file(fpath, as_attachment=True,
                     download_name='ai_video.mp4', mimetype='video/mp4')

@app.route('/api/tn-results')
def tn_results():
    """
    Serves TN 2026 results from in-memory cache — never hits ECI per request.
    Background thread refreshes cache every 10 minutes.
    """
    try:
        data, meta = get_tn_results()
        return jsonify({"ok": True, "results": data, **meta})
    except Exception as e:
        return jsonify({"ok": False, "results": [], "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
