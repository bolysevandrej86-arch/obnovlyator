# -*- coding: utf-8 -*-
"""Генерирует app.ico для сборки .exe (та же стрелка-обновление, что в трее)."""
from pathlib import Path

from PIL import Image, ImageDraw


def make_icon() -> Image.Image:
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([8, 8, size - 8, size - 8], radius=56,
                           fill=(27, 27, 32, 255))
    draw.arc([64, 64, 192, 192], start=300, end=200,
             fill=(59, 130, 246, 255), width=28)
    draw.polygon([(176, 46), (234, 96), (152, 112)], fill=(59, 130, 246, 255))
    return img


if __name__ == "__main__":
    icon = make_icon()
    out = Path(__file__).resolve().parent / "app.ico"
    icon.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"OK: {out}")
