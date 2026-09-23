# -*- coding: utf-8 -*-
# 密着シリーズ公開ショート実測レイアウト:
#  L1 y285-372(≈88px) / L2 y395-503(≈109px) / 映像 1080x606@y552 / 赤引用 y1225-1304(≈80px)
#  黄=(250,250,30) 赤=(223,0,13) フォント=ヒラギノ角ゴW9級
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT="/System/Library/Fonts/ヒラギノ角ゴシック W9.ttc"
W,H=1080,1920
YELLOW=(250,250,30,255); WHITE=(255,255,255,255); RED=(223,0,13,255)
MAXW_HEAD=880
MAXW_QUOTE=860

def draw_segments(d,ds,segs,size,cy):
    f=ImageFont.truetype(FONT,size)
    widths=[d.textbbox((0,0),t,font=f)[2]-d.textbbox((0,0),t,font=f)[0] for t,_ in segs]
    total=sum(widths)
    bb=d.textbbox((0,0),"".join(t for t,_ in segs),font=f)
    x=(W-total)//2
    y=cy-(bb[3]+bb[1])//2
    for (t,col),w in zip(segs,widths):
        ds.text((x+3,y+4),t,font=f,fill=(0,0,0,160))
        d.text((x,y),t,font=f,fill=col)
        x+=w
    return total

def fit(d,segs,size0,maxw):
    size=size0
    while size>40:
        f=ImageFont.truetype(FONT,size)
        total=sum(d.textbbox((0,0),t,font=f)[2] for t,_ in segs)
        if total<=maxw: break
        size-=2
    return size

def build(l1,l2,quote,out):
    """l1,l2,quote: [(text,'w'|'y'|'r'),...]"""
    C={'w':WHITE,'y':YELLOW,'r':RED}
    base=Image.new("RGB",(W,H),(0,0,0))
    txt=Image.new("RGBA",(W,H),(0,0,0,0))
    sh=Image.new("RGBA",(W,H),(0,0,0,0))
    d=ImageDraw.Draw(txt); ds=ImageDraw.Draw(sh)
    for segs,size0,cy,maxw in [(l1,88,328,MAXW_HEAD),(l2,109,449,MAXW_HEAD),(quote,80,1264,MAXW_QUOTE)]:
        segs=[(t,C[c]) for t,c in segs]
        size=fit(d,segs,size0,maxw)
        draw_segments(d,ds,segs,size,cy)
    sh=sh.filter(ImageFilter.GaussianBlur(3))
    base=Image.alpha_composite(base.convert("RGBA"),sh)
    base=Image.alpha_composite(base,txt)
    base.convert("RGB").save(out)
