"""
AI Video Generator — Flask Web App
Generates animated MP4 videos with voiceover from a topic + script.
"""

import os, math, random, subprocess, threading, time, uuid
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont
import imageio, numpy as np

app = Flask(__name__)

JOBS = {}
OUTPUT_DIR = Path("videos")
OUTPUT_DIR.mkdir(exist_ok=True)

# ── Colours ──────────────────────────────────────────────────────────────────
BG   = (10, 10, 30)
C1   = (0, 201, 255)
C2   = (146, 254, 157)
C3   = (255, 107, 107)
C4   = (255, 217, 61)
W_C  = (255, 255, 255)
GRAY = (136, 146, 176)
CARD = (26, 26, 46)
SCENE_COLORS = [C1, C4, C2, C3, (179,136,255), (255,138,101), C2, C3]

W, H, FPS = 1280, 720, 24

def font(size, bold=False):
    try:
        r = subprocess.run(
            ["fc-match","DejaVu Sans"+(" Bold" if bold else ""),"--format=%{file}"],
            capture_output=True, text=True).stdout.strip()
        return ImageFont.truetype(r, size)
    except:
        return ImageFont.load_default()

def ease_out(t): return 1-(1-max(0,min(1,t)))**3
def blend(base,col,a): return tuple(int(b*(1-a)+c*a) for b,c in zip(base,col))
def lerp(a,b,t): return a+(b-a)*t
def rrect(draw,box,r=10,fill=None,outline=None,w=2):
    draw.rounded_rectangle(box,radius=r,fill=fill,outline=outline,width=w)

def make_particles():
    random.seed(int(time.time()))
    cols = [C1,C2,C3,C4]
    return [dict(x=random.uniform(0,W),y=random.uniform(0,H),
                 vx=random.uniform(-0.3,0.3),vy=random.uniform(-0.3,0.3),
                 r=random.uniform(1,3),col=random.choice(cols),
                 a=random.uniform(0.1,0.4)) for _ in range(60)]

def draw_particles(img, particles):
    d = ImageDraw.Draw(img)
    for p in particles:
        p['x']=(p['x']+p['vx'])%W; p['y']=(p['y']+p['vy'])%H
        c=blend(BG,p['col'],p['a']); r=int(p['r'])
        d.ellipse([p['x']-r,p['y']-r,p['x']+r,p['y']+r],fill=c)

def draw_character(draw, x, y, t, color=C2, scale=1.0, action="talk"):
    s = scale
    bounce = math.sin(t * math.pi * 2) * 4 * s
    body_top = y - int(60*s) + int(bounce)
    body_bot = y + int(10*s) + int(bounce)
    head_y   = y - int(85*s) + int(bounce)
    head_r   = int(28*s)
    shadow_col = blend(BG,(50,50,80),0.7)
    draw.ellipse([x-int(35*s),y+int(12*s),x+int(35*s),y+int(22*s)],fill=shadow_col)
    rrect(draw,[x-int(22*s),body_top,x+int(22*s),body_bot],r=8,
          fill=color,outline=blend(color,W_C,0.3),w=2)
    head_col=(255,220,177)
    draw.ellipse([x-head_r,head_y-head_r,x+head_r,head_y+head_r],
                 fill=head_col,outline=blend(head_col,W_C,0.2),width=2)
    eye_blink=abs(math.sin(t*math.pi*0.4))>0.95
    eye_h=3 if eye_blink else int(6*s)
    for ex in [x-int(10*s),x+int(10*s)]:
        draw.ellipse([ex-int(5*s),head_y-int(8*s),ex+int(5*s),head_y-int(8*s)+eye_h],fill=(40,40,60))
    if action=="talk":
        mouth_open=abs(math.sin(t*math.pi*4))*int(8*s)
        draw.arc([x-int(12*s),head_y+int(4*s),x+int(12*s),head_y+int(14*s)+mouth_open],
                 0,180,fill=(180,80,80),width=int(3*s))
    else:
        draw.arc([x-int(12*s),head_y+int(4*s),x+int(12*s),head_y+int(16*s)],
                 0,180,fill=(180,80,80),width=int(3*s))
    draw.arc([x-head_r,head_y-head_r,x+head_r,head_y+int(5*s)],200,340,fill=(80,50,20),width=int(8*s))
    arm_angle=math.sin(t*math.pi*1.5)*0.15
    r_arm_end=(x+int(45*s),body_top+int(40*s)+int(bounce))
    draw.line([x+int(20*s),body_top+int(15*s),r_arm_end[0],r_arm_end[1]],fill=head_col,width=int(8*s))
    l_arm_end=(x-int((30+30*math.cos(arm_angle))*s),
               body_top+int(15*s)-int(30*math.sin(arm_angle)*s)+int(bounce))
    draw.line([x-int(20*s),body_top+int(15*s),l_arm_end[0],l_arm_end[1]],fill=head_col,width=int(8*s))
    for sign in [-1,1]:
        lx=x+sign*int(12*s); ly1=body_bot; ly2=y+int(55*s)+int(bounce)
        draw.line([lx,ly1,lx,ly2],fill=blend(color,(30,30,60),0.4),width=int(12*s))
        draw.ellipse([lx-int(12*s),ly2-int(5*s),lx+int(12*s),ly2+int(8*s)],fill=(40,40,60))

def render_scene(img, draw, idx, name, p, t_anim, color, topic, total):
    fade=min(p/0.1,1.0,(1-p)/0.08)
    bar_a=ease_out(min(p/0.15,1))*fade
    rrect(draw,[0,0,W,70],r=0,fill=blend(BG,color,bar_a*0.25))
    draw.text((W//2,35),topic.upper(),font=font(20,True),fill=blend(BG,color,bar_a),anchor="mm")
    char_a=ease_out(min((p-0.05)/0.2,1))*fade
    if char_a>0.05:
        action="talk" if 0.1<p<0.85 else "idle"
        draw_character(draw,200,480,t_anim*2,color=color,scale=1.1*char_a,action=action)
    bubble_a=ease_out(min((p-0.15)/0.2,1))*fade
    if bubble_a>0.05:
        bx1,by1,bx2,by2=320,310,1100,530
        rrect(draw,[bx1,by1,bx2,by2],r=20,fill=blend(BG,CARD,bubble_a*0.95),
              outline=blend(BG,color,bubble_a),w=2)
        tail=[(bx1,by2-40),(bx1-30,by2+20),(bx1+40,by2-10)]
        draw.polygon(tail,fill=blend(BG,CARD,bubble_a*0.95))
        draw.line([(bx1,by2-40),(bx1-30,by2+20)],fill=blend(BG,color,bubble_a),width=2)
        draw.line([(bx1-30,by2+20),(bx1+40,by2-10)],fill=blend(BG,color,bubble_a),width=2)
    a=ease_out(min((p-0.2)/0.25,1))*fade
    draw.text((710,390),name.upper(),font=font(38,True),fill=blend(BG,color,a),anchor="mm")
    draw.text((W//2,H-30),f"Chapter {idx+1}  ·  {name.upper()}",
              font=font(15),fill=blend(BG,GRAY,fade*0.7),anchor="mm")
    prog=(idx+p)/total
    bw=int(prog*W)
    for x in range(bw):
        t_=x/W; c=tuple(int(lerp(ca,cb,t_)) for ca,cb in zip(C1,C2))
        draw.line([x,H-4,x,H],fill=c)

def generate_video(job_id, topic, script):
    try:
        JOBS[job_id].update(status='running', message='Generating voiceover...', progress=5)
        audio_path  = f"/tmp/{job_id}_audio.mp3"
        silent_path = f"/tmp/{job_id}_silent.mp4"
        out_path    = str(OUTPUT_DIR / f"{job_id}.mp4")

        tts = gTTS(" ".join(s['text'] for s in script), lang='en', slow=False)
        tts.save(audio_path)
        JOBS[job_id]['progress'] = 15

        total_sec    = sum(s['duration'] for s in script)
        total_frames = total_sec * FPS
        particles    = make_particles()
        writer = imageio.get_writer(silent_path, fps=FPS, quality=8, macro_block_size=1)
        frame_n = 0; t_abs = 0.0

        for idx, scene in enumerate(script):
            color = SCENE_COLORS[idx % len(SCENE_COLORS)]
            sf    = scene['duration'] * FPS
            for f in range(sf):
                p     = f/sf; t_anim=t_abs+f/FPS
                img   = Image.new("RGB",(W,H),BG)
                draw_particles(img,particles)
                draw  = ImageDraw.Draw(img)
                render_scene(img,draw,idx,scene['name'],p,t_anim,color,topic,len(script))
                writer.append_data(np.array(img))
                frame_n += 1
            t_abs += scene['duration']
            JOBS[job_id].update(
                progress=15+int((frame_n/total_frames)*70),
                message=f"Rendering scene {idx+1}/{len(script)}...")

        writer.close()

        JOBS[job_id].update(message='Merging audio + video...', progress=88)
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg,"-y","-i",silent_path,"-i",audio_path,
                        "-c:v","copy","-c:a","aac","-shortest",out_path],
                       capture_output=True)

        JOBS[job_id].update(status='done', progress=100, message='Video ready!', file=out_path)
        for f in [audio_path, silent_path]:
            try: os.remove(f)
            except: pass

    except Exception as e:
        JOBS[job_id].update(status='error', message=str(e))


# ── HTML ─────────────────────────────────────────────────────────────────────
HTML = open("templates/index.html").read() if os.path.exists("templates/index.html") else ""

@app.route('/')
def index():
    return render_template_string(open(
        os.path.join(os.path.dirname(__file__), 'templates', 'index.html')
    ).read())

@app.route('/generate', methods=['POST'])
def start_generate():
    data   = request.json
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {'status':'pending','progress':0,'message':'Queued...','file':None}
    threading.Thread(target=generate_video,
                     args=(job_id, data.get('topic','Video'), data.get('script',[])),
                     daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def status(job_id):
    return jsonify(JOBS.get(job_id, {'status':'error','message':'Not found','progress':0}))

@app.route('/download/<job_id>')
def download(job_id):
    job = JOBS.get(job_id)
    if not job or not job.get('file'):
        return "Not ready", 404
    return send_file(job['file'], as_attachment=True,
                     download_name='ai_video.mp4', mimetype='video/mp4')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
