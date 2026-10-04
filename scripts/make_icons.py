"""生成 resources/icons 下的图标文件（打包 exe 时需要真实 .ico）。

用法::

    python scripts/make_icons.py

程序运行时并不依赖这些文件：``app/icons.py`` 会用 Qt 直接绘制图标，
这里生成 .ico 只为了给 PyInstaller 提供 exe 图标与托盘图标。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    print("缺少 Pillow，请先执行: pip install -r requirements.txt")
    raise SystemExit(1)

ICON_DIR = ROOT / "resources" / "icons"
ICO_SIZES: List[Tuple[int, int]] = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]


def _lerp(start: Tuple[int, int, int], end: Tuple[int, int, int], ratio: float) -> Tuple[int, int, int]:
    return tuple(int(start[index] + (end[index] - start[index]) * ratio) for index in range(3))  # type: ignore[return-value]


def _gradient(size: int, start: Tuple[int, int, int], end: Tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            ratio = (x + y) / float(max(1, 2 * size - 2))
            pixels[x, y] = _lerp(start, end, ratio) + (255,)
    return image


def draw_app_icon(size: int = 512) -> Image.Image:
    base = _gradient(size, (76, 125, 240), (123, 76, 240))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [size * 0.06, size * 0.06, size * 0.94, size * 0.94], radius=int(size * 0.24), fill=255
    )
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon.paste(base, (0, 0), mask)

    draw = ImageDraw.Draw(icon)
    # 对话气泡
    draw.rounded_rectangle(
        [size * 0.22, size * 0.24, size * 0.78, size * 0.64],
        radius=int(size * 0.10),
        fill=(255, 255, 255, 235),
    )
    draw.polygon(
        [
            (size * 0.34, size * 0.62),
            (size * 0.30, size * 0.78),
            (size * 0.47, size * 0.63),
        ],
        fill=(255, 255, 255, 235),
    )
    for index in range(3):
        center_x = size * (0.33 + index * 0.17)
        radius = size * 0.038
        draw.ellipse(
            [center_x - radius, size * 0.44 - radius, center_x + radius, size * 0.44 + radius],
            fill=(76, 125, 240, 255),
        )
    return icon


def draw_tray_icon(size: int = 128, running: bool = True) -> Image.Image:
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    color = (61, 220, 132, 255) if running else (138, 143, 152, 255)
    glow = (61, 220, 132, 70) if running else (138, 143, 152, 40)
    margin = size * 0.06
    draw.ellipse([margin, margin, size - margin, size - margin], fill=glow)
    inner = size * 0.30
    draw.ellipse([inner, inner, size - inner, size - inner], fill=color)
    for index in (-1, 0, 1):
        center_x = size / 2 + index * size * 0.16
        radius = size * 0.06
        draw.ellipse(
            [center_x - radius, size / 2 - radius, center_x + radius, size / 2 + radius],
            fill=(255, 255, 255, 240),
        )
    return icon


def save_ico(image: Image.Image, path: Path, sizes: List[Tuple[int, int]] = ICO_SIZES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(path), format="ICO", sizes=sizes)
    print("已生成 %s" % path)


def main() -> int:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    app = draw_app_icon(512)
    app.save(str(ICON_DIR / "app.png"))
    save_ico(app, ICON_DIR / "app.ico")
    save_ico(draw_tray_icon(128, True), ICON_DIR / "tray_green.ico", [(64, 64), (48, 48), (32, 32), (16, 16)])
    save_ico(draw_tray_icon(128, False), ICON_DIR / "tray_gray.ico", [(64, 64), (48, 48), (32, 32), (16, 16)])
    print("图标生成完成，输出目录：%s" % ICON_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
