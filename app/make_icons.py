"""Draw the two app icons and save them as multi-resolution .ico files.

Run this again if you want to tweak the artwork:
    python app\\make_icons.py
"""
import os
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
S = 512                     # draw big, then let the .ico sizes downsample
SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]


def rounded(draw, box, r, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def backrooms_icon():
    """A yellow room with Diamond standing in it, eyes lit."""
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    wall = (201, 185, 122)
    floor = (150, 136, 84)
    ceil = (214, 206, 180)

    d.rectangle([0, 0, S, S], fill=wall)                     # back wall
    d.polygon([(0, 0), (S, 0), (S, S * 0.30), (0, S * 0.30)], fill=ceil)
    d.polygon([(0, S), (S, S), (S, S * 0.66), (0, S * 0.66)], fill=floor)

    # side walls angling in, so it reads as a corridor even when tiny
    d.polygon([(0, 0), (S * 0.17, S * 0.26), (S * 0.17, S * 0.72), (0, S)],
              fill=(176, 161, 105))
    d.polygon([(S, 0), (S * 0.83, S * 0.26), (S * 0.83, S * 0.72), (S, S)],
              fill=(176, 161, 105))

    # a doorway of darkness behind it
    d.rectangle([S * 0.36, S * 0.28, S * 0.64, S * 0.70], fill=(84, 76, 48))
    d.rectangle([S * 0.41, S * 0.31, S * 0.59, S * 0.70], fill=(38, 34, 22))

    # ceiling troffer
    d.rectangle([S * 0.40, S * 0.10, S * 0.60, S * 0.15], fill=(255, 250, 214))
    glow = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([S * 0.30, S * 0.03, S * 0.70, S * 0.24],
                                 fill=(255, 248, 200, 90))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.03)))

    # --- Diamond: a black cutout, bold enough to survive 16px ---
    ink = (9, 9, 12, 255)
    cx = S * 0.5
    d.polygon([(cx, S * 0.20), (cx + S * 0.085, S * 0.33),        # head
               (cx, S * 0.46), (cx - S * 0.085, S * 0.33)], fill=ink)
    d.rectangle([cx - S * 0.018, S * 0.44, cx + S * 0.018, S * 0.52], fill=ink)
    d.polygon([(cx - S * 0.075, S * 0.51), (cx + S * 0.075, S * 0.51),
               (cx + S * 0.050, S * 0.75), (cx - S * 0.050, S * 0.75)], fill=ink)
    for sgn in (-1, 1):                                            # long arms
        x = cx + sgn * S * 0.072
        d.polygon([(x - S * 0.020, S * 0.52), (x + S * 0.020, S * 0.52),
                   (x + sgn * S * 0.030 + S * 0.016, S * 0.80),
                   (x + sgn * S * 0.030 - S * 0.016, S * 0.80)], fill=ink)
    for sgn in (-1, 1):                                            # legs
        x = cx + sgn * S * 0.030
        d.rectangle([x - S * 0.020, S * 0.74, x + S * 0.020, S * 0.93], fill=ink)

    eye = (255, 246, 214, 255)
    for sgn in (-1, 1):
        ex = cx + sgn * S * 0.030
        d.ellipse([ex - S * 0.012, S * 0.305, ex + S * 0.012, S * 0.329], fill=eye)

    # corner vignette
    vig = Image.new('L', (S, S), 0)
    ImageDraw.Draw(vig).ellipse([-S * 0.25, -S * 0.25, S * 1.25, S * 1.25], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(S * 0.09))
    dark = Image.new('RGBA', (S, S), (0, 0, 0, 255))
    dark.putalpha(Image.eval(vig, lambda v: int((255 - v) * 0.75)))
    img.alpha_composite(dark)
    return img


def toggleboard_icon():
    """A dark control panel: three switches, the middle one thrown and live."""
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    rounded(d, [S * 0.06, S * 0.06, S * 0.94, S * 0.94], S * 0.10,
            fill=(24, 24, 28, 255), outline=(78, 78, 88, 255), width=int(S * 0.016))
    rounded(d, [S * 0.06, S * 0.06, S * 0.94, S * 0.22], S * 0.10, fill=(44, 44, 52, 255))
    d.rectangle([S * 0.06, S * 0.17, S * 0.94, S * 0.22], fill=(44, 44, 52, 255))
    for i in range(3):                                   # title-bar dots
        d.ellipse([S * (0.12 + i * 0.075), S * 0.12, S * (0.155 + i * 0.075), S * 0.155],
                  fill=(120, 120, 132, 255))

    rows = [(0.34, False), (0.55, True), (0.76, False)]
    for y, on in rows:
        left, right = S * 0.16, S * 0.60
        pill = [left, S * (y - 0.065), right, S * (y + 0.065)]
        rounded(d, pill, S * 0.065,
                fill=(172, 34, 34, 255) if on else (66, 66, 76, 255))
        kx = right - S * 0.065 if on else left + S * 0.065
        d.ellipse([kx - S * 0.052, S * (y - 0.052), kx + S * 0.052, S * (y + 0.052)],
                  fill=(240, 226, 210, 255) if on else (150, 150, 162, 255))
        # a status lamp beside each switch
        d.ellipse([S * 0.70, S * (y - 0.045), S * 0.79, S * (y + 0.045)],
                  fill=(255, 96, 78, 255) if on else (58, 58, 66, 255))

    glow = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([S * 0.60, S * 0.42, S * 0.90, S * 0.68],
                                 fill=(255, 60, 40, 150))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.05)))
    return img


def save(img, name):
    path = os.path.join(HERE, name + '.ico')
    img.save(path, sizes=SIZES)
    print('wrote', path)
    return path


if __name__ == '__main__':
    a, b = backrooms_icon(), toggleboard_icon()
    save(a, 'HollowFloor')
    save(b, 'ToggleBoard')
    # a preview sheet, so the icons can be checked at the sizes windows uses
    sheet = Image.new('RGB', (760, 420), (30, 30, 34))
    x = 20
    for img in (a, b):
        y = 20
        for px in (256, 128, 48, 32, 16):
            sheet.paste(img.resize((px, px), Image.LANCZOS).convert('RGB'), (x, y))
            y += px + 12
        x += 300
    sheet.save(os.path.join(HERE, 'icon_preview.png'))
    print('wrote icon_preview.png')
