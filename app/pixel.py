"""Pixel-art sprites: Tiny Me herself, and her cat.

:mod:`app.icons` covers the UI glyphs — thin, smooth, drawn with
``QPainterPath``. This module is the other half of the design sheet: the two
little blocky characters, drawn the way Minecraft draws things, in hard square
pixels with a small palette and no anti-aliasing.

They are kept apart from the icons on purpose. An icon wants to be smooth at
any size; a sprite wants the opposite, and the two needs fight each other in
one painter. Here every sprite is a grid of characters with a palette lookup,
which is also the only sane way to edit pixel art in a text file:

    "..hhhhhhhhhhhh.."
    "..hhhssssssHhh.."

Scaling is deliberately integer-only. A sprite drawn at 3.7 device pixels per
art pixel has rows of uneven thickness and stops reading as pixel art at all,
so :func:`paint` floors the scale, then centres the smaller result inside the
rect it was given. Nothing here ships an image file (CLAUDE.md: no new
dependencies, and a PNG would need redrawing for 125% and 150% scaling).
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter

#: Character -> colour. ``.`` is transparent and never drawn. The hues are the
#: design sheet's: her hair is the same brown as ``theme.BROWN``, her shirt the
#: same green as ``theme.PRIMARY``, so the characters sit in the window's
#: palette rather than next to it.
PALETTE: dict[str, str] = {
    "k": "#2B2016",   # outline / eyes, the darkest thing in the set
    "h": "#6B4A33",   # hair
    "H": "#8A6244",   # hair, lit side
    "s": "#F2C9A0",   # skin
    "S": "#E0A878",   # skin, in shadow (under the chin)
    "b": "#E09A8E",   # blush
    "m": "#B9574E",   # mouth
    "g": "#3F6B41",   # shirt
    "G": "#5C8A5E",   # shirt, lit side
    "c": "#FAF6EC",   # collar
    "o": "#E09A5B",   # cat
    "O": "#C27A3C",   # cat, stripes
    "w": "#FFFDF7",   # muzzle, paws, highlights
    "p": "#E0675C",   # nose
    "r": "#E0675C",   # heart
    "R": "#C24F45",   # heart, lower edge
}

#: Her: head and shoulders, facing front. 16x18. Drawn as a bust rather than a
#: full figure because every place she appears is small — beside a callout, in
#: the corner of a card — and a full figure at that size is a smudge.
GIRL = (
    "......hhhh......",
    "....hhhhhhhh....",
    "...hhhhhhhhhh...",
    "..hhhhhhhhhhhh..",
    "..hhhssssssHhh..",
    "..hhsssssssshh..",
    "..hhsssssssshh..",
    "..hhskksskkshh..",
    "..hhsssssssshh..",
    "..hhsbssssbshh..",
    "..hhsssmmssshh..",
    "..hhhssssssHhh..",
    "...hhhsssshhh...",
    "..hh..SssS..hh..",
    "..hhccggggcchh..",
    ".hhhcggggggchhh.",
    ".hhgggggggggghh.",
    ".ggggggGGgggggg.",
)

#: The cat: sitting, front on, with her tail curled up the right-hand side.
#: 16x16. The tail joins the body at the bottom rather than floating beside it,
#: which at this size is the difference between a cat and two orange blobs.
CAT = (
    "..oo......oo....",
    ".oooo....oooo...",
    ".oOoooooooooO...",
    ".oooooooooooo...",
    ".ookkooookkoo...",
    ".oooooooooooo...",
    ".ooooowpwoooo...",
    ".ooooowwwoooo...",
    "..oooooooooo....",
    "...oooooooo.....",
    "..oooooooooo.OO.",
    "..oOooooooOo.oo.",
    "..oooooooooo.oo.",
    "..ooooooooooooo.",
    "..owwoooooowwo..",
    "..wwwoooooowww..",
)

#: 12x10. The window's "done" and "thank you" moments.
HEART = (
    "..rr....rr..",
    ".rrrr..rrrr.",
    "rrrrrrrrrrrr",
    "rrrrrrrrrrrr",
    "rrrrrrrrrrrr",
    ".RrrrrrrrrR.",
    "..RrrrrrrR..",
    "...RrrrrR...",
    "....RrrR....",
    ".....RR.....",
)


SPRITES: dict[str, tuple[str, ...]] = {
    "girl": GIRL,
    "cat": CAT,
    "heart": HEART,
}


def size(name: str) -> tuple[int, int]:
    """``(columns, rows)`` of ``name``, or ``(0, 0)`` if there is no such sprite."""
    rows = SPRITES.get(name)
    if not rows:
        return (0, 0)
    return (len(rows[0]), len(rows))


def fit(name: str, rect: QRectF) -> QRectF:
    """The rect ``paint`` would actually fill — integer-scaled and centred.

    Callers that need to lay something out beside a sprite (the overlay puts a
    callout next to her) ask for this rather than guessing, because the drawn
    size is almost always a little smaller than the rect offered.
    """
    cols, rows = size(name)
    if not cols or rect.width() <= 0 or rect.height() <= 0:
        return QRectF(rect.center(), rect.center())
    scale = max(1, int(min(rect.width() / cols, rect.height() / rows)))
    width, height = cols * scale, rows * scale
    return QRectF(rect.center().x() - width / 2, rect.center().y() - height / 2,
                  width, height)


def paint(painter: QPainter, name: str, rect: QRectF, *,
          opacity: float = 1.0, flip: bool = False) -> QRectF:
    """Draw ``name`` centred in ``rect`` and return the rect it filled.

    ``flip`` mirrors it horizontally, which is how the cat ends up facing her
    when the two sit side by side. An unknown name draws nothing and returns an
    empty rect, matching :func:`app.icons.paint`: a decoration is never worth
    an exception.
    """
    rows = SPRITES.get(name)
    if not rows:
        return QRectF()

    box = fit(name, rect)
    if box.isEmpty():
        return QRectF()
    scale = box.width() / len(rows[0])

    painter.save()
    # The whole point: no anti-aliasing, so a pixel stays a pixel.
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    if opacity < 1.0:
        painter.setOpacity(painter.opacity() * opacity)
    painter.setPen(Qt.PenStyle.NoPen)

    for y, row in enumerate(rows):
        if flip:
            row = row[::-1]
        x = 0
        width = len(row)
        while x < width:
            char = row[x]
            run = 1
            while x + run < width and row[x + run] == char:
                run += 1
            colour = PALETTE.get(char)
            if colour is not None:
                painter.setBrush(QColor(colour))
                painter.drawRect(QRectF(box.left() + x * scale,
                                        box.top() + y * scale,
                                        run * scale, scale))
            x += run

    painter.restore()
    return box
