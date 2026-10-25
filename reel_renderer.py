import json, math, os, subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def font_path():
    for path in ['/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf', 'C:/Windows/Fonts/arial.ttf']:
        if Path(path).exists():
            return path
    raise RuntimeError('No usable font installed')


def frame(text, index, total, size=(1080,1920)):
    w,h=size
    im=Image.new('RGB',(w,h))
    d=ImageDraw.Draw(im)
    for y in range(h):
        shade=int(10+12*(1-y/h))
        d.line((0,y,w,y), fill=(shade,shade+2,shade+6))
    gold=(216,184,118)
    d.line((90,420,220,420),fill=gold,width=5)
    lines=text.splitlines()
    for sz in range(74,35,-2):
        f=ImageFont.truetype(font_path(),sz)
        if max(d.textlength(line,font=f) for line in lines)<=880:
            break
    else:
        raise RuntimeError('Reel text exceeds safe width')
    box=d.multiline_textbbox((0,0),text,font=f,spacing=24)
    y=(h-(box[3]-box[1]))/2-box[1]-60
    d.multiline_text((90,y),text,font=f,fill='white',spacing=24)
    small=ImageFont.truetype(font_path(),25)
    d.text((90,1510),'BRIGHT MIND SHORTS',font=small,fill=gold)
    for i in range(total):
        x=90+i*230
        d.rounded_rectangle((x,1580,x+205,1584),radius=2,fill=gold if i<=index else (55,58,65))
    return im


def render_reel(item, folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    scenes=item['scenes']
    if len(scenes)!=4 or not all(isinstance(x,str) and x.strip() for x in scenes):
        raise RuntimeError('Reels require four nonempty scenes')
    frames=[]
    for i,text in enumerate(scenes):
        image=frame(text,i,4)
        path=folder/f'scene-{i}.png';image.save(path);frames.append(path)
        if i==0:image.save(folder/'cover.jpg',quality=95)
    frame(item.get('story_text',item['text']),0,1).save(folder/'story.jpg',quality=95)
    cmd=['ffmpeg','-y','-hide_banner','-loglevel','error']
    for path in frames:
        cmd+=['-loop','1','-framerate','30','-t','4','-i',str(path)]
    cmd+=['-f','lavfi','-i','aevalsrc=0.025*sin(2*PI*174.61*t)+0.018*sin(2*PI*220*t)+0.012*sin(2*PI*261.63*t):s=48000:d=16']
    filters=[]
    for i in range(4):
        filters.append(f"[{i}:v]zoompan=z='1+0.00012*on':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=1080x1920:fps=30,fade=t=in:st=0:d=0.25,fade=t=out:st=3.75:d=0.25,setsar=1[v{i}]")
    filters.append('[v0][v1][v2][v3]concat=n=4:v=1:a=0[v]')
    filters.append('[4:a]afade=t=in:st=0:d=1,afade=t=out:st=14.5:d=1.5[a]')
    cmd+=['-filter_complex_threads','1','-filter_complex',';'.join(filters),'-map','[v]','-map','[a]','-t','16','-c:v','libx264','-preset','fast','-crf','23','-pix_fmt','yuv420p','-threads','2','-r','30','-c:a','aac','-b:a','96k','-ar','48000','-ac','2','-movflags','+faststart',str(folder/'reel.mp4')]
    subprocess.run(cmd,check=True,timeout=600)
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(folder/'reel.mp4')],text=True))
    video=next(s for s in probe['streams'] if s['codec_type']=='video')
    assert (video['codec_name'],video['width'],video['height'],video['pix_fmt'])==('h264',1080,1920,'yuv420p')
    assert 15.8<=float(probe['format']['duration'])<=16.2
    if (folder/'reel.mp4').stat().st_size>20_000_000:raise RuntimeError('Video unexpectedly large')
    (folder/'caption.txt').write_text(item['caption'],encoding='utf-8')
    (folder/'media-check.json').write_text(json.dumps(probe,indent=2),encoding='utf-8')
    print('Reel validated: H.264, AAC, 1080x1920, 30fps, 16 seconds',flush=True)
