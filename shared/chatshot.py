"""Картинка переписки для алерта в VK — как скриншот чата (14.10: «что написал клиент и что ответил бот» с одного
взгляда, вместо простыни текста). Клиент слева, бот и владелец справа, сверху — имя клиента и суть."""
import io
import re
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

W, PAD, GAP = 720, 22, 14
BUBBLE = 520                    # максимальная ширина пузыря
MAX_CHARS = 420                 # длинные сообщения обрезаем — картинка, а не архив
FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf", "C:/Windows/Fonts/arial{}.ttf")
COLORS = {"bg": "#EEF0F3", "head": "#1F3864", "client": "#FFFFFF", "bot": "#D6E6FF", "owner": "#D9F2DC",
          "text": "#1B1B1B", "meta": "#6B7280"}
WHO = {"client": "Клиент", "bot": "Бот", "owner": "Вы"}
# эмодзи и пиктограммы в шрифте нет — квадратики хуже, чем ничего
NO_GLYPH = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")


def _font(size: int, bold: bool = False):
    for pattern in FONTS:
        try:
            return ImageFont.truetype(pattern.format("-Bold" if bold and "dejavu" in pattern else
                                                     "bd" if bold else ""), size)
        except OSError:
            continue
    return None


def _wrap(text: str, font, width: int) -> list[str]:
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            probe = f"{cur} {word}" if cur else word
            if font.getlength(probe) <= width:
                cur = probe
                continue
            if cur:
                lines.append(cur)
            while font.getlength(word) > width:          # слово длиннее строки (ссылка) — режем
                cut = max(1, int(len(word) * width / font.getlength(word)) - 1)
                lines.append(word[:cut])
                word = word[cut:]
            cur = word
        lines.append(cur)
    return lines


def render(header: str, sub: str, rows: list[tuple[str, str, datetime | None]], tz=None) -> bytes | None:
    """rows — (origin client|bot|owner, текст, время). JPEG или None, если нет шрифта с кириллицей."""
    body, small, bold = _font(21), _font(16), _font(23, bold=True)
    if not body:
        return None
    blocks = []
    for who, text, at in rows:
        text = NO_GLYPH.sub("", (text or "").strip()) or "[вложение]"
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS].rstrip() + "…"
        lines = _wrap(text, body, BUBBLE - 2 * PAD)
        w = max(body.getlength(x) for x in lines) + 2 * PAD
        meta = WHO.get(who, who) + (f" · {at.astimezone(tz):%d.%m %H:%M}" if at else "")
        blocks.append((who, lines, w, meta))
    line_h = 28
    head_lines = _wrap(NO_GLYPH.sub("", header), bold, W - 2 * PAD) + _wrap(NO_GLYPH.sub("", sub), small, W - 2 * PAD)
    head_h = PAD + len(head_lines) * 30 + PAD // 2
    height = head_h + PAD + sum(22 + len(b[1]) * line_h + 2 * GAP + GAP for b in blocks) + PAD
    img = Image.new("RGB", (W, height), COLORS["bg"])
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, head_h], fill=COLORS["head"])
    y = PAD
    for i, line in enumerate(head_lines):
        d.text((PAD, y), line, font=bold if i == 0 else small, fill="#FFFFFF")
        y += 30
    y = head_h + PAD
    for who, lines, w, meta in blocks:
        right = who != "client"
        x = W - PAD - w if right else PAD
        d.text((x + (w - small.getlength(meta) if right else 0), y), meta, font=small, fill=COLORS["meta"])
        y += 22
        h = len(lines) * line_h + 2 * GAP
        d.rounded_rectangle([x, y, x + w, y + h], radius=16, fill=COLORS.get(who, COLORS["bot"]))
        for j, line in enumerate(lines):
            d.text((x + PAD, y + GAP + j * line_h), line, font=body, fill=COLORS["text"])
        y += h + GAP
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()
