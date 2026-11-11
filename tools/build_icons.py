"""ホーム画面用の PNG アイコン (180/192/512px) を作る。icon.svg と同じ図柄を Pillow で描く。

    python tools/build_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

ICON_DIR = Path(__file__).resolve().parent.parent / "packages" / "matsimapp" / "src" / "matsimapp" / "frontend" / "icons"
BLUE, WHITE, YELLOW = (47, 111, 237), (255, 255, 255), (255, 209, 102)
CORNERS = [(150, 170), (362, 170), (150, 342), (362, 342)]
CENTER = (256, 256)
EDGES = [(0, 1), (2, 3), (0, 2), (1, 3)]


def draw(size: int) -> Image.Image:
    scale = 4  # 大きく描いて縮小し、輪郭をなめらかにする
    s = size * scale / 512
    image = Image.new("RGB", (size * scale, size * scale), BLUE)
    pen = ImageDraw.Draw(image)

    def p(point):
        return (point[0] * s, point[1] * s)

    def circle(point, radius, color):
        x, y = p(point)
        r = radius * s
        pen.ellipse((x - r, y - r, x + r, y + r), fill=color)

    for a, b in EDGES:
        pen.line([p(CORNERS[a]), p(CORNERS[b])], fill=WHITE, width=round(18 * s))
    for corner in CORNERS:
        pen.line([p(corner), p(CENTER)], fill=WHITE, width=round(18 * s))
    for corner in CORNERS:
        circle(corner, 40, WHITE)
    circle(CENTER, 50, YELLOW)
    return image.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for size in (180, 192, 512):
        path = ICON_DIR / f"icon-{size}.png"
        draw(size).save(path)
        print(path)
