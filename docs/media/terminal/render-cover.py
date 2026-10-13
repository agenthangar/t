#!/usr/bin/env python3
"""Draw the editorial video cover; this is a title card, not terminal footage."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

MEDIA = Path(__file__).resolve().parent

def cover():
    image = Image.new('RGB', (1424, 852), '#121414')
    d = ImageDraw.Draw(image)
    def font(size, bold=False, mono=False):
        mac = '/System/Library/Fonts/Menlo.ttc' if mono else '/System/Library/Fonts/Supplemental/Arial' + (' Bold' if bold else '') + '.ttf'
        fallback = '/usr/share/fonts/truetype/dejavu/DejaVuSans' + ('Mono' if mono else '') + ('-Bold' if bold else '') + '.ttf'
        return ImageFont.truetype(mac if Path(mac).exists() else fallback, size)
    for x in range(72,1424,160): d.line((x,0,x,852),fill='#1b1e1d',width=1)
    amber,white,muted = '#f2a93b','#f3f1eb','#a5aaa3'
    d.rounded_rectangle((72,64,136,128),radius=14,fill=amber)
    d.text((93,66),'t',font=font(50,True),fill='#121414')
    d.text((158,82),'AGENTHANGAR',font=font(25,mono=True),fill=white)
    d.rounded_rectangle((1110, 74,1352,122),radius=24,outline='#454b43',width=2)
    d.text((1141,83),'1 MINUTE DEMO',font=font(20,mono=True),fill=muted)
    d.text((68,172),'One task.',font=font(94,True),fill=white)
    d.text((68,274),'Every surface.',font=font(94,True),fill=amber)
    d.text((74,400),'Fix the code. Keep the context.',font=font(35),fill=muted)
    d.rounded_rectangle((985,213,1352,400),radius=20,fill='#20251f',outline='#394936',width=2)
    d.text((1013,237),'REAL BUG FIX',font=font(21,mono=True),fill=muted)
    d.text((1013,279),'2 failed',font=font(38,True),fill=white)
    d.text((1013,329),'5 passed',font=font(38,True),fill='#add59f')
    cards=[('01','Open','Terminal'),('02','Move','Mac mini'),('03','Open in app','Codex desktop'),('04','Resume','Terminal')]
    for i,(number,label,surface) in enumerate(cards):
        x=72+i*326
        d.rounded_rectangle((x,513,x+302,697),radius=18,fill='#1d201f',outline='#40453f',width=2)
        d.text((x+24,533),number,font=font(21,mono=True),fill=amber)
        d.text((x+24,573),label,font=font(35,True),fill=white)
        d.text((x+24,629),surface,font=font(25),fill=muted)
        if i<3:
            cx=x+314
            d.line((cx-6,605,cx+6,605),fill=amber,width=2)
            d.line((cx+1,600,cx+6,605,cx+1,610),fill=amber,width=2)
    return image

if __name__ == '__main__':
    cover().save(MEDIA / 'workflow-poster.png')
