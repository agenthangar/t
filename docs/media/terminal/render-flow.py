#!/usr/bin/env python3
"""Render one task's verified PTY chapters; no invented terminal output.

Input: assets/recordings/workflow.json, with a sanitized cast per chapter.
Chapters can trim idle tails; editorial labels live outside the terminal.
Dependencies: Pillow, pyte, ffmpeg.
"""
import json, subprocess, tempfile
from runpy import run_path
from pathlib import Path
import pyte
from terminal_style import cell_style, BACKGROUND
from PIL import Image, ImageDraw, ImageFont

MEDIA=Path(__file__).resolve().parent
manifest=json.loads((MEDIA/'workflow.json').read_text())
assert manifest['same_thread_verified'] and manifest['desktop_owner_verified']
font=ImageFont.truetype('/System/Library/Fonts/Menlo.ttc' if Path('/System/Library/Fonts/Menlo.ttc').exists() else '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',18)
small=ImageFont.truetype(font.path,14)
bold_font=ImageFont.truetype(font.path,18,index=1) if Path(font.path).name=="Menlo.ttc" else ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",18)
symbol_path=Path('/System/Library/Fonts/Apple Symbols.ttf')
symbol_font=ImageFont.truetype(str(symbol_path),18) if symbol_path.exists() else font
class Replay(pyte.Screen):
    def report_device_status(self,mode,**kwargs):
        if not kwargs.get('private'):super().report_device_status(mode)
width,height,fps=1424,852,8
frame_no=0; chapters=[]
with tempfile.TemporaryDirectory(prefix='t-flow-render-') as tmp:
    tmp=Path(tmp)
    title = run_path(str(Path(__file__).with_name('render-cover.py')))['cover']()
    title.save(MEDIA/'workflow-poster.png')
    chapters.append({'label':'One task. Every surface.','seconds':0})
    for _ in range(round(manifest.get('intro_seconds',1)*fps)):
        title.save(tmp/f'{frame_no:05d}.png');frame_no+=1
    for chapter in manifest['chapters']:
        rows=[json.loads(l) for l in (MEDIA/chapter['source']).read_text().splitlines()]
        header,events=rows[0],rows[1:];screen=Replay(header['width'],header['height']);stream=pyte.Stream(screen)
        index=0;start=chapter.get('start',0);end=chapter.get('end',events[-1][0]+3)
        speed=chapter.get('speed',1);duration=(end-start)/speed
        chapter_time=frame_no/fps
        chapters.append({'label':chapter['label'],'seconds':chapter_time})
        for n in range(round(duration*fps)):
            now=start+n/fps*speed
            while index<len(events) and events[index][0]<=now:stream.feed(events[index][2]);index+=1
            image=Image.new('RGB',(width,height),'#141516');draw=ImageDraw.Draw(image)
            draw.rectangle((0,0,width,52),fill='#242628')
            draw.text((28,17),'t / ONE TASK, EVERY SURFACE',font=small,fill='#f2a93b')
            draw.text((650,17),chapter['label'],font=small,fill='#ebe7df')
            for y in range(header['height']):
                line=''.join(screen.buffer[y][x].data for x in range(header['width']))
                for x in range(header['width']):
                    c=screen.buffer[y][x]
                    color,bg=cell_style(c,line)
                    px,py=28+x*11.4,68+y*24
                    if bg!=BACKGROUND:draw.rectangle((px,py,px+11.4,py+24),fill=bg)
                    if c.data==' ':continue
                    face=symbol_font if c.data in "⬡▣⟳✂" else bold_font if c.bold else font
                    draw.text((px,py),c.data,font=face,fill=color)
                    if c.underscore:draw.line((px,py+23,px+11.4,py+23),fill=color)
            draw.text((28,height-38),(f'{speed}x · ' if speed!=1 else '')+chapter['note'],font=small,fill='#b8b3a8')
            image.save(tmp/f'{frame_no:05d}.png')
            frame_no+=1
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-framerate',str(fps),'-i',str(tmp/'%05d.png'),'-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(MEDIA/'workflow.mp4')],check=True)
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(MEDIA/'workflow.mp4'),'-vf','fps=4,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse','-loop','0',str(MEDIA/'workflow.gif')],check=True)
(MEDIA/'workflow-chapters.json').write_text(json.dumps(chapters,indent=2)+'\n')
print(f'Rendered {frame_no/fps:.1f}s from one task; see workflow.json for capture limitations.')
