# Обводка элементов на скриншотах инструкции.
# Использование: python _annotate.py  (читает marks.json рядом с собой)
#
# marks.json:
# { "01.png": [ {"box":[x,y,w,h], "label":"1"}, {"box":[...], "style":"arrow"} ] }
#
# Координаты — в пикселях исходного скриншота (raw/NN.png), результат — img/NN.png

import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
RAW = HERE / "raw"
OUT = HERE / "img"
RED = (227, 30, 36)

def font(size):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()

def draw_mark(d, mark):
    x, y, w, h = mark["box"]
    for i in range(3):  # рамка 3 px без width= — ровные углы
        d.rectangle([x - i, y - i, x + w + i, y + h + i], outline=RED)
    label = mark.get("label")
    if not label:
        return
    f = font(20)
    tw = d.textlength(label, font=f)
    r = 14
    # кружок ставим СНАРУЖИ рамки, иначе он закрывает подпись элемента
    cx, cy = x - r - 6, y + h / 2
    if cx - r < 2:                      # у левого края - выносим вправо
        cx = x + w + r + 6
    if cy - r < 2:                      # у верхнего края - опускаем
        cy = r + 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=RED)
    d.text((cx - tw / 2, cy - 12), label, font=f, fill="white")

def main():
    marks = json.loads((HERE / "marks.json").read_text(encoding="utf-8"))
    OUT.mkdir(exist_ok=True)
    # снимки без обводок тоже нужны в img/ - копируем все, отмеченные рисуем
    for src in sorted(RAW.glob("*.png")):
        name = src.name
        if name.startswith("_"):
            continue
        img = Image.open(src).convert("RGB")
        items = marks.get(name, [])
        if items:
            d = ImageDraw.Draw(img)
            for m in items:
                draw_mark(d, m)
        img.save(OUT / name)
        print(name, len(items), "обводок")

if __name__ == "__main__":
    main()
