import io

from PIL import Image, ImageDraw, ImageFont

from .tree import INTERACTIVE, Element

PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#9a6324", "#800000"]


def prepare(png: bytes, width: float, height: float, elements: list[Element] | None = None,
            quality: int = 80) -> bytes:
    # Resized to the screen size in points, so a pixel position is a tap position.
    image = Image.open(io.BytesIO(png)).convert("RGB").resize((round(width), round(height)), Image.LANCZOS)
    if elements:
        _annotate(image, elements)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _annotate(image: Image.Image, elements: list[Element]) -> None:
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=11)
    for element in elements:
        if element.type not in INTERACTIVE:
            continue
        color = PALETTE[element.index % len(PALETTE)]
        box = (element.x, element.y, element.x + element.width, element.y + element.height)
        draw.rectangle(box, outline=color, width=1)
        tag = str(element.index)
        left, top, right, bottom = draw.textbbox((0, 0), tag, font=font)
        tag_w, tag_h = right - left + 4, bottom - top + 2
        x, y = max(element.x, 0), max(element.y, 0)
        draw.rectangle((x, y, x + tag_w, y + tag_h), fill=color)
        draw.text((x + 2, y - top + 1), tag, fill="white", font=font)
