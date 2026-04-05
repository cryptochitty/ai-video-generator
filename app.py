"""
AI Video Generator — Flask Web App
Features: edge-tts voice, multilingual, on-screen text, auto timing sync
"""

import os, math, random, subprocess, threading, time, uuid, asyncio, re, textwrap, json
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
from PIL import Image, ImageDraw, ImageFont
import imageio, numpy as np

app = Flask(__name__)

BASE_DIR   = Path(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = BASE_DIR / "videos"
JOBS_DIR   = BASE_DIR / "jobs"
OUTPUT_DIR.mkdir(exist_ok=True)
JOBS_DIR.mkdir(exist_ok=True)

# ── Persist job state to disk (survives Render restarts) ──────────────────────
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
BG   = (10, 10, 30)
C1   = (0, 201, 255)
C2   = (146, 254, 157)
C3   = (255, 107, 107)
C4   = (255, 217, 61)
W_C  = (255, 255, 255)
GRAY = (136, 146, 176)
CARD = (26, 26, 46)
SCENE_COLORS = [C1, C4, C2, C3, (179,136,255), (255,138,101), C2, C3, C1, C4]

W, H, FPS = 640, 360, 8    # 360p @ 8fps — fastest render on free tier

LANGUAGES = {
    "English (US)":     "en-US-JennyNeural",
    "English (UK)":     "en-GB-SoniaNeural",
    "Hindi":            "hi-IN-SwaraNeural",
    "Tamil":            "ta-IN-PallaviNeural",
    "Telugu":           "te-IN-ShrutiNeural",
    "Spanish":          "es-ES-ElviraNeural",
    "French":           "fr-FR-DeniseNeural",
    "German":           "de-DE-KatjaNeural",
    "Arabic":           "ar-EG-SalmaNeural",
    "Japanese":         "ja-JP-NanamiNeural",
    "Chinese":          "zh-CN-XiaoxiaoNeural",
    "Portuguese":       "pt-BR-FranciscaNeural",
}

# ── Bundled fonts — per-character script detection ────────────────────────────
_FONTS_DIR  = BASE_DIR / "fonts"
_FONT_CACHE = {}
_ACTIVE_FONT = ('NotoSans-Regular.ttf', 'NotoSans-Bold.ttf')

# Unicode range → (regular, bold)
_SCRIPT_FONTS = [
    (0x0B80, 0x0BFF, 'NotoSansTamil-Regular.ttf',     'NotoSansTamil-Bold.ttf'),
    (0x0900, 0x097F, 'NotoSansDevanagari-Regular.ttf', 'NotoSansDevanagari-Bold.ttf'),
    (0x0C00, 0x0C7F, 'NotoSansTelugu-Regular.ttf',     'NotoSans-Bold.ttf'),
    (0x0600, 0x06FF, 'NotoSansArabic-Regular.ttf',     'NotoSans-Bold.ttf'),
]

def _script_files(text):
    """Return (regular, bold) font filenames for the dominant script in text."""
    for ch in text:
        cp = ord(ch)
        for lo, hi, reg, bld in _SCRIPT_FONTS:
            if lo <= cp <= hi:
                return reg, bld
    return 'NotoSans-Regular.ttf', 'NotoSans-Bold.ttf'

def font(size, bold=False, text=''):
    """Return cached font — auto-picks script from text characters."""
    reg, bld = _script_files(text) if text else ('NotoSans-Regular.ttf','NotoSans-Bold.ttf')
    fname = bld if bold else reg
    key   = (fname, size)
    if key not in _FONT_CACHE:
        try:    _FONT_CACHE[key] = ImageFont.truetype(str(_FONTS_DIR / fname), size)
        except: _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]

def set_render_lang(voice):
    global _ACTIVE_FONT, _FONT_CACHE
    _FONT_CACHE = {}   # clear cache between videos

def ease_out(t): return 1 - (1 - max(0, min(1, t))) ** 3
def blend(base, col, a): return tuple(int(b*(1-a)+c*a) for b,c in zip(base, col))
def lerp(a, b, t): return a + (b-a)*t
def rrect(draw, box, r=10, fill=None, outline=None, w=2):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=w)

def make_particles():
    random.seed(42)
    return [dict(x=random.uniform(0,W), y=random.uniform(0,H),
                 vx=random.uniform(-0.25,0.25), vy=random.uniform(-0.25,0.25),
                 r=random.uniform(1,2), col=random.choice([C1,C2,C3,C4]),
                 a=random.uniform(0.08,0.25)) for _ in range(22)]  # fewer = faster

def draw_particles(img, particles):
    d = ImageDraw.Draw(img)
    for p in particles:
        p['x'] = (p['x']+p['vx']) % W
        p['y'] = (p['y']+p['vy']) % H
        c = blend(BG, p['col'], p['a'])
        r = max(1, int(p['r']))
        d.ellipse([p['x']-r, p['y']-r, p['x']+r, p['y']+r], fill=c)

# ── Character ─────────────────────────────────────────────────────────────────
def draw_character(draw, x, y, t, color=C2, scale=1.0, action="talk"):
    s = scale
    bounce = math.sin(t * math.pi * 2) * 4 * s
    body_top = y - int(60*s) + int(bounce)
    body_bot = y + int(10*s) + int(bounce)
    head_y   = y - int(85*s) + int(bounce)
    head_r   = int(28*s)
    draw.ellipse([x-int(35*s), y+int(12*s), x+int(35*s), y+int(22*s)],
                 fill=blend(BG,(50,50,80),0.7))
    rrect(draw, [x-int(22*s), body_top, x+int(22*s), body_bot], r=8,
          fill=color, outline=blend(color, W_C, 0.3), w=2)
    head_col = (255, 220, 177)
    draw.ellipse([x-head_r, head_y-head_r, x+head_r, head_y+head_r],
                 fill=head_col, outline=blend(head_col,W_C,0.2), width=2)
    eye_blink = abs(math.sin(t * math.pi * 0.4)) > 0.95
    eye_h = 3 if eye_blink else int(6*s)
    for ex in [x-int(10*s), x+int(10*s)]:
        draw.ellipse([ex-int(5*s), head_y-int(8*s),
                      ex+int(5*s), head_y-int(8*s)+eye_h], fill=(40,40,60))
    if action == "talk":
        mo = abs(math.sin(t * math.pi * 5)) * int(9*s)
        draw.arc([x-int(12*s), head_y+int(4*s),
                  x+int(12*s), head_y+int(14*s)+mo],
                 0, 180, fill=(180,80,80), width=int(3*s))
    else:
        draw.arc([x-int(12*s), head_y+int(4*s),
                  x+int(12*s), head_y+int(16*s)],
                 0, 180, fill=(180,80,80), width=int(3*s))
    draw.arc([x-head_r, head_y-head_r, x+head_r, head_y+int(5*s)],
             200, 340, fill=(80,50,20), width=int(8*s))
    arm_angle = math.sin(t * math.pi * 1.5) * 0.2
    r_arm = (x+int(45*s), body_top+int(40*s)+int(bounce))
    draw.line([x+int(20*s), body_top+int(15*s), r_arm[0], r_arm[1]],
              fill=head_col, width=int(8*s))
    l_arm = (x-int((30+30*math.cos(arm_angle))*s),
             body_top+int(15*s)-int(30*math.sin(arm_angle)*s)+int(bounce))
    draw.line([x-int(20*s), body_top+int(15*s), l_arm[0], l_arm[1]],
              fill=head_col, width=int(8*s))
    for sign in [-1, 1]:
        lx = x + sign*int(12*s)
        draw.line([lx, body_bot, lx, y+int(55*s)+int(bounce)],
                  fill=blend(color,(30,30,60),0.4), width=int(12*s))
        draw.ellipse([lx-int(12*s), y+int(50*s)+int(bounce),
                      lx+int(12*s), y+int(63*s)+int(bounce)], fill=(40,40,60))

# ── Subtitle — slim transparent bar, wraps properly ──────────────────────────
def draw_subtitle(draw, text, p, color):
    words   = text.split()
    visible = ' '.join(words[:max(1, int(len(words) * min(p * 1.5, 1.0)))])
    lines   = textwrap.wrap(visible, 60)[:2]
    if not lines: return draw
    f     = font(13, text=text)
    lh    = 18
    pad   = 6
    box_h = len(lines) * lh + pad * 2
    y1    = H - box_h
    draw.rectangle([0, y1, W, H], fill=(0, 0, 14))
    draw.rectangle([0, y1, 3, H], fill=color)
    for i, ln in enumerate(lines):
        ty = y1 + pad + i * lh
        draw.text((8, ty + 1), ln, font=f, fill=(0, 0, 0))
        draw.text((7, ty),     ln, font=f, fill=W_C)
    return draw

# ── On-screen content ─────────────────────────────────────────────────────────
def draw_content_panel(draw, idx, name, p, t_anim, color, topic, narration):
    fade  = min(p / 0.08, 1.0, (1 - p) / 0.06)
    bar_a = ease_out(min(p / 0.12, 1)) * fade

    # Slim top bar (30px)
    draw.rectangle([0, 0, W, 30], fill=blend(BG, color, bar_a * 0.3))
    draw.text((W // 2, 15), topic,
              font=font(11, True, text=topic), fill=blend(BG, color, bar_a), anchor="mm")
    draw.text((W - 6, 15), f"#{idx+1}",
              font=font(10, True), fill=blend(BG, color, bar_a * 0.6), anchor="rm")

    # Character (left side, smaller)
    char_a = ease_out(min((p - 0.04) / 0.18, 1)) * fade
    if char_a > 0.05:
        action = "talk" if 0.08 < p < 0.88 else "idle"
        draw_character(draw, 75, 240, t_anim * 2,
                       color=color, scale=0.62 * char_a, action=action)

    # Scene name — right side, no big box, just styled text
    bub_a = ease_out(min((p - 0.1) / 0.2, 1)) * fade
    if bub_a > 0.05:
        # thin accent line left edge
        draw.rectangle([160, 38, 163, 200], fill=blend(BG, color, bub_a * 0.7))
        # scene name (smaller font, wraps if long)
        sname_lines = textwrap.wrap(name, 28)[:2]
        for li, sl in enumerate(sname_lines):
            draw.text((175, 60 + li * 22), sl,
                      font=font(14, True, text=sl), fill=blend(BG, color, bub_a))
        # chapter indicator dots
        for si in range(min(idx + 2, 8)):
            col_d = color if si == idx else GRAY
            draw.ellipse([175 + si * 12, 170, 183 + si * 12, 178],
                         fill=blend(BG, col_d, bub_a * 0.8))

    return draw


# ── TTS via edge-tts ──────────────────────────────────────────────────────────
def generate_tts(text, voice, out_file):
    import edge_tts

    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(out_file)

    asyncio.run(_run())


def get_audio_duration(audio_file):
    """Return duration in seconds using bundled ffmpeg."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([ffmpeg, '-i', audio_file],
                       capture_output=True, text=True)
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
    if m:
        h, mn, s = m.groups()
        return int(h)*3600 + int(mn)*60 + float(s)
    return 8.0  # fallback


def concat_audio(files, out_file):
    """Concatenate multiple MP3 files using ffmpeg."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    list_file = out_file + ".txt"
    with open(list_file, 'w') as f:
        for af in files:
            f.write(f"file '{af}'\n")
    subprocess.run([ffmpeg, '-y', '-f', 'concat', '-safe', '0',
                    '-i', list_file, '-c', 'copy', out_file],
                   capture_output=True)
    os.remove(list_file)


# ── Main video generator ──────────────────────────────────────────────────────
def generate_video(job_id, topic, script, voice):
    try:
        set_render_lang(voice)   # set language-aware font before rendering
        job_set(job_id, status='running', message='Generating voiceover...', progress=5)

        silent_path = f"/tmp/{job_id}_silent.mp4"
        out_path    = str(OUTPUT_DIR / f"{job_id}.mp4")
        scene_audios = []

        # ── Step 1: Generate per-scene audio & measure duration ──
        total_scenes = len(script)
        for i, scene in enumerate(script):
            af = f"/tmp/{job_id}_s{i}.mp3"
            generate_tts(scene['text'], voice, af)
            dur = get_audio_duration(af)
            scene['actual_duration'] = max(dur + 0.5, 3.0)  # +0.5s padding
            scene_audios.append(af)
            pct = 5 + int((i+1)/total_scenes * 20)
            job_set(job_id, progress=pct,
                                message=f'Voice {i+1}/{total_scenes}: {scene["name"]}')

        # Concatenate all scene audios
        combined_audio = f"/tmp/{job_id}_audio.mp3"
        concat_audio(scene_audios, combined_audio)

        # ── Step 2: Render video frames ──
        job_set(job_id, message='Rendering animation...', progress=26)
        total_frames = sum(int(s['actual_duration'] * FPS) for s in script)
        particles    = make_particles()

        writer = imageio.get_writer(silent_path, fps=FPS, quality=8, macro_block_size=1)
        frame_n = 0
        t_abs   = 0.0

        for idx, scene in enumerate(script):
            color    = SCENE_COLORS[idx % len(SCENE_COLORS)]
            duration = scene['actual_duration']
            narration= scene['text']
            sf       = int(duration * FPS)

            # ── Pre-render static background once per scene ──────────────────
            static = Image.new("RGB", (W, H), BG)
            draw_particles(static, particles)
            sd = ImageDraw.Draw(static)
            # slim top bar
            sd.rectangle([0, 0, W, 30], fill=blend(BG, color, 0.3))
            sd.text((W // 2, 15), topic,
                    font=font(11, True, text=topic), fill=color, anchor="mm")
            sd.text((W - 6, 15), f"#{idx+1}",
                    font=font(10, True), fill=blend(BG, color, 0.6), anchor="rm")
            # accent line + scene name
            sd.rectangle([160, 38, 163, 200], fill=blend(BG, color, 0.7))
            for li, sl in enumerate(textwrap.wrap(scene['name'], 28)[:2]):
                sd.text((175, 60 + li * 22), sl,
                        font=font(14, True, text=sl), fill=color)
            static_arr = np.array(static)

            for f in range(sf):
                p      = f / max(sf-1, 1)
                t_anim = t_abs + f / FPS

                # Start from pre-rendered static (fast copy)
                img  = Image.fromarray(static_arr.copy())
                draw = ImageDraw.Draw(img)

                # Dynamic: character only
                action = "talk" if 0.08 < p < 0.88 else "idle"
                draw_character(draw, 90, 270, t_anim*2,
                               color=color, scale=0.75, action=action)

                # Subtitle (compact strip)
                draw_subtitle(draw, narration, min(p/0.12, 1.0), color)

                # Progress bar (2px, above subtitle)
                bw = int(frame_n / max(total_frames,1) * W)
                draw.rectangle([0, H-2, bw, H], fill=C1)

                writer.append_data(np.array(img))
                frame_n += 1

            t_abs += duration
            pct = 26 + int(frame_n / total_frames * 60)
            job_set(job_id, progress=pct,
                    message=f'Rendering scene {idx+1}/{total_scenes}')

        writer.close()

        # ── Step 3: Merge audio + video ──
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

        # Cleanup
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
                    'active_font': list(_ACTIVE_FONT)})

@app.route('/')
def index():
    return render_template('index.html', languages=list(LANGUAGES.keys()))

@app.route('/generate', methods=['POST'])
def start_generate():
    data   = request.json
    job_id = str(uuid.uuid4())[:8]
    voice  = LANGUAGES.get(data.get('language','English (US)'), 'en-US-JennyNeural')
    job_set(job_id, status='pending', progress=0, message='Queued...', file=None)
    threading.Thread(target=generate_video,
                     args=(job_id, data.get('topic','Video'),
                           data.get('script',[]), voice),
                     daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def status(job_id):
    job = job_get(job_id)
    return jsonify(job or {'status':'error','message':'Not found','progress':0})

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
