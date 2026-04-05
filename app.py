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

W, H, FPS = 640, 360, 12   # 360p @ 12fps — fast render on free tier

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

# ── Font ─────────────────────────────────────────────────────────────────────
def font(size, bold=False):
    try:
        r = subprocess.run(
            ["fc-match", "DejaVu Sans" + (" Bold" if bold else ""), "--format=%{file}"],
            capture_output=True, text=True).stdout.strip()
        return ImageFont.truetype(r, size)
    except:
        return ImageFont.load_default()

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

# ── Fast subtitle (no RGBA overlay — plain text with shadow) ──────────────────
def draw_subtitle(draw, text, p, color):
    words   = text.split()
    visible = ' '.join(words[:max(1, int(len(words)*min(p*1.5,1.0)))])
    lines   = textwrap.wrap(visible, 45) or ['']
    lh = 26
    box_h = len(lines)*lh + 16
    y1 = H - box_h - 6
    # dark box (no RGBA — just draw a filled rect)
    draw.rectangle([0, y1, W, H], fill=(0,0,18))
    draw.rectangle([0, y1, 4, H], fill=color)
    f = font(18)
    for i, ln in enumerate(lines):
        ty = y1 + 8 + i*lh
        # shadow
        draw.text((12, ty+1), ln, font=f, fill=(0,0,0))
        draw.text((11, ty),   ln, font=f, fill=W_C)
    return draw

# ── On-screen content (fast flat layout) ─────────────────────────────────────
def draw_content_panel(draw, idx, name, p, t_anim, color, topic, narration):
    fade  = min(p/0.08, 1.0, (1-p)/0.06)
    bar_a = ease_out(min(p/0.12, 1)) * fade

    # Top bar
    draw.rectangle([0,0,W,44], fill=blend(BG, color, bar_a*0.25))
    draw.text((W//2, 22), topic.upper(),
              font=font(14,True), fill=blend(BG,color,bar_a), anchor="mm")
    draw.text((W-8, 22), f"#{idx+1}",
              font=font(13,True), fill=blend(BG,color,bar_a*0.7), anchor="rm")

    # Character (left)
    char_a = ease_out(min((p-0.04)/0.18,1)) * fade
    if char_a > 0.05:
        action = "talk" if 0.08 < p < 0.88 else "idle"
        draw_character(draw, 90, 270, t_anim*2,
                       color=color, scale=0.75*char_a, action=action)

    # Scene name (right panel)
    bub_a = ease_out(min((p-0.1)/0.2,1)) * fade
    if bub_a > 0.05:
        draw.rectangle([170, 52, W-8, 210],
                       fill=blend(BG, CARD, bub_a*0.9))
        draw.rectangle([170, 52, 174, 210],
                       fill=blend(BG, color, bub_a*0.8))
        draw.text((W//2+50, 100), name.upper(),
                  font=font(20,True), fill=blend(BG,color,bub_a), anchor="mm")
        draw.line([185, 118, W-18, 118],
                  fill=blend(BG,color,bub_a*0.3), width=1)
        # chapter dots
        for si in range(min(8, idx+2)):
            dot_col = color if si == idx else GRAY
            dx = W//2 + 30 + (si - min(8,idx+2)//2)*14
            draw.ellipse([dx-4,185,dx+4,193],
                         fill=blend(BG,dot_col,bub_a))

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

            for f in range(sf):
                p      = f / max(sf-1, 1)
                t_anim = t_abs + f / FPS

                img  = Image.new("RGB", (W, H), BG)
                draw_particles(img, particles)
                draw = ImageDraw.Draw(img)
                draw._image = img   # attach for subtitle overlay

                # Main content
                draw = draw_content_panel(draw, idx, scene['name'], p,
                                          t_anim, color, topic, narration)

                # Subtitle
                sub_p = min(p / 0.15, 1.0)
                draw  = draw_subtitle(draw, narration, sub_p, color)

                # Progress bar (bottom 3px)
                prog = (frame_n) / max(total_frames,1)
                bw   = int(prog * W)
                draw.rectangle([0, H-3, bw, H], fill=C1)

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
