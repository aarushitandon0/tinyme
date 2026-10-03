"""The icon set, drawn with QPainterPath rather than shipped as files.

Every icon is described on a 24x24 grid and scaled at paint time, so one
definition stays crisp at any DPI and recolours for free (CLAUDE.md asks for no
new dependencies; an icon font would be one, and PNGs would have to be redrawn
for 125% and 150% scaling).

Two kinds:

* *stroked* icons — a list of sub-paths painted with a round-capped pen. These
  are the navigation and UI glyphs, and they match the design sheet's thin,
  friendly line weight.
* *filled* icons — ``heart``, ``leaf-fill``, the dots — painted solid, used
  where the design sheet shows a solid badge.

:func:`paint` is the only entry point the widgets use.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

Builder = Callable[[], tuple[QPainterPath, bool]]

#: Icons whose shape needs filling rather than stroking.
_FILLED = {"heart", "leaf_fill", "dot", "play", "send", "check_fill", "sparkle"}


def _path(*commands) -> QPainterPath:
    """Tiny path builder: ``("m", x, y)``, ``("l", x, y)``, ``("c", ...)``, ``("z",)``."""
    path = QPainterPath()
    for command in commands:
        kind = command[0]
        if kind == "m":
            path.moveTo(command[1], command[2])
        elif kind == "l":
            path.lineTo(command[1], command[2])
        elif kind == "c":
            path.cubicTo(command[1], command[2], command[3], command[4],
                         command[5], command[6])
        elif kind == "q":
            path.quadTo(command[1], command[2], command[3], command[4])
        elif kind == "z":
            path.closeSubpath()
    return path


def _rounded(x: float, y: float, w: float, h: float, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    return path


def _circle(cx: float, cy: float, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(QPointF(cx, cy), r, r)
    return path


def _home() -> QPainterPath:
    p = _path(("m", 4, 10.5), ("l", 12, 4), ("l", 20, 10.5))
    p.moveTo(6.2, 9.4)
    p.lineTo(6.2, 19.2)
    p.lineTo(17.8, 19.2)
    p.lineTo(17.8, 9.4)
    p.moveTo(10, 19.2)
    p.lineTo(10, 14)
    p.lineTo(14, 14)
    p.lineTo(14, 19.2)
    return p


def _clock() -> QPainterPath:
    p = _circle(12, 12, 8)
    p.moveTo(12, 7.2)
    p.lineTo(12, 12.3)
    p.lineTo(15.6, 14.4)
    return p


def _grid() -> QPainterPath:
    p = QPainterPath()
    for x in (4.5, 13.5):
        for y in (4.5, 13.5):
            p.addRoundedRect(QRectF(x, y, 6, 6), 1.8, 1.8)
    return p


def _gear() -> QPainterPath:
    p = _circle(12, 12, 3.1)
    p.addPath(_path(
        ("m", 12, 3.4), ("l", 13.5, 5.6), ("l", 16.1, 5.1), ("l", 16.4, 7.7),
        ("l", 18.8, 8.8), ("l", 17.7, 11.2), ("l", 19.2, 13.4), ("l", 17.1, 15),
        ("l", 17.3, 17.6), ("l", 14.7, 18), ("l", 13.3, 20.2), ("l", 11, 19),
        ("l", 8.7, 20), ("l", 7.5, 17.7), ("l", 4.9, 17.2), ("l", 5, 14.6),
        ("l", 3, 13), ("l", 4.4, 10.8), ("l", 3.5, 8.3), ("l", 5.9, 7.3),
        ("l", 6.4, 4.7), ("l", 9, 5.2), ("z",),
    ))
    return p


def _info() -> QPainterPath:
    p = _circle(12, 12, 8)
    p.moveTo(12, 10.8)
    p.lineTo(12, 16.4)
    p.moveTo(12, 7.6)
    p.lineTo(12, 8.2)
    return p


def _search() -> QPainterPath:
    p = _circle(10.8, 10.8, 5.6)
    p.moveTo(14.9, 14.9)
    p.lineTo(19.4, 19.4)
    return p


def _arrow_right() -> QPainterPath:
    return _path(("m", 5.5, 12), ("l", 17.5, 12), ("m", 12.4, 6.8),
                 ("l", 17.6, 12), ("l", 12.4, 17.2))


def _arrow_left() -> QPainterPath:
    return _path(("m", 18.5, 12), ("l", 6.5, 12), ("m", 11.6, 6.8),
                 ("l", 6.4, 12), ("l", 11.6, 17.2))


def _cursor() -> QPainterPath:
    """The pointer on the "Show me how" card, with its little spark marks."""
    p = _path(("m", 8.4, 5.2), ("l", 8.4, 17.4), ("l", 11.3, 14.6),
              ("l", 13.2, 19.1), ("l", 15.4, 18.1), ("l", 13.6, 13.7),
              ("l", 17.5, 13.3), ("z",))
    p.moveTo(4.6, 7.0)
    p.lineTo(3.0, 5.6)
    p.moveTo(5.4, 4.0)
    p.lineTo(4.6, 2.2)
    p.moveTo(3.4, 10.4)
    p.lineTo(1.5, 10.8)
    return p


def _play() -> QPainterPath:
    """Filled triangle in a soft square — the "Do it for me" mark."""
    return _path(("m", 9, 6.4), ("l", 18.2, 12), ("l", 9, 17.6), ("z",))


def _send() -> QPainterPath:
    return _path(("m", 3.4, 12), ("l", 20.4, 4.2), ("l", 12.8, 20.4),
                 ("l", 11, 13.6), ("z",))


def _paperclip() -> QPainterPath:
    return _path(
        ("m", 16.6, 11.1), ("l", 10.6, 17.1),
        ("c", 8.9, 18.8, 6.3, 18.8, 4.7, 17.1),
        ("c", 3.1, 15.5, 3.1, 12.9, 4.7, 11.2),
        ("l", 12.2, 3.8),
        ("c", 13.3, 2.7, 15.0, 2.7, 16.1, 3.8),
        ("c", 17.2, 4.9, 17.2, 6.6, 16.1, 7.7),
        ("l", 8.7, 15.2),
        ("c", 8.1, 15.7, 7.3, 15.7, 6.8, 15.2),
        ("c", 6.2, 14.6, 6.2, 13.8, 6.8, 13.3),
        ("l", 12.5, 7.6),
    )


def _folder() -> QPainterPath:
    p = _path(("m", 3.4, 7.4), ("l", 9.4, 7.4), ("l", 11.2, 9.6),
              ("l", 20.6, 9.6))
    p.addRoundedRect(QRectF(3.4, 7.4, 17.2, 11.4), 2.2, 2.2)
    return p


def _mail() -> QPainterPath:
    p = _rounded(3.2, 5.6, 17.6, 12.8, 2.4)
    p.moveTo(3.8, 7.2)
    p.lineTo(12, 13.2)
    p.lineTo(20.2, 7.2)
    return p


def _train() -> QPainterPath:
    p = _rounded(5.4, 3.6, 13.2, 13.4, 3.2)
    p.addRect(QRectF(7.6, 6.4, 8.8, 4.6))
    p.moveTo(9.4, 14.2)
    p.lineTo(9.6, 14.2)
    p.moveTo(14.4, 14.2)
    p.lineTo(14.6, 14.2)
    p.moveTo(8.2, 17.2)
    p.lineTo(5.8, 20.6)
    p.moveTo(15.8, 17.2)
    p.lineTo(18.2, 20.6)
    return p


def _globe() -> QPainterPath:
    p = _circle(12, 12, 8.2)
    p.addEllipse(QRectF(7.6, 3.8, 8.8, 16.4))
    p.moveTo(4.1, 9.2)
    p.lineTo(19.9, 9.2)
    p.moveTo(4.1, 14.8)
    p.lineTo(19.9, 14.8)
    return p


def _video() -> QPainterPath:
    p = _rounded(3.2, 6.2, 18, 11.6, 3.2)
    p.moveTo(10.2, 9.4)
    p.lineTo(14.8, 12)
    p.lineTo(10.2, 14.6)
    p.closeSubpath()
    return p


def _doc() -> QPainterPath:
    p = _path(("m", 6, 3.4), ("l", 14, 3.4), ("l", 18.4, 7.8), ("l", 18.4, 20.6),
              ("l", 6, 20.6), ("z",), ("m", 13.8, 3.6), ("l", 13.8, 8.2),
              ("l", 18.2, 8.2))
    p.moveTo(9, 12.4)
    p.lineTo(15.4, 12.4)
    p.moveTo(9, 15.8)
    p.lineTo(15.4, 15.8)
    return p


def _lock() -> QPainterPath:
    p = _rounded(5, 10.4, 14, 9.6, 2.6)
    p.moveTo(8.2, 10.4)
    p.lineTo(8.2, 7.8)
    p.cubicTo(8.2, 5.7, 9.9, 4.0, 12.0, 4.0)
    p.cubicTo(14.1, 4.0, 15.8, 5.7, 15.8, 7.8)
    p.lineTo(15.8, 10.4)
    p.moveTo(12, 14.0)
    p.lineTo(12, 16.4)
    return p


def _shield() -> QPainterPath:
    p = _path(("m", 12, 3.2), ("l", 19.4, 6.2), ("l", 19.4, 12),
              ("c", 19.4, 16.4, 16.2, 19.6, 12, 21),
              ("c", 7.8, 19.6, 4.6, 16.4, 4.6, 12),
              ("l", 4.6, 6.2), ("z",))
    p.moveTo(8.8, 12.2)
    p.lineTo(11.2, 14.6)
    p.lineTo(15.4, 9.8)
    return p


def _heart() -> QPainterPath:
    return _path(
        ("m", 12, 20.2),
        ("c", 4.4, 15.4, 2.6, 11.0, 4.6, 7.6),
        ("c", 6.2, 4.9, 10.0, 4.8, 12.0, 7.8),
        ("c", 14.0, 4.8, 17.8, 4.9, 19.4, 7.6),
        ("c", 21.4, 11.0, 19.6, 15.4, 12.0, 20.2),
        ("z",),
    )


def _leaf() -> QPainterPath:
    """Tiny Me's own mark: a sprout, two leaves on a stem."""
    p = _path(
        ("m", 12, 20.4), ("l", 12, 11.2),
        ("m", 12, 12.6),
        ("c", 8.0, 12.9, 5.0, 10.6, 4.4, 6.4),
        ("c", 8.6, 5.8, 11.6, 8.2, 12.0, 12.6),
        ("z",),
        ("m", 12, 11.0),
        ("c", 12.6, 7.2, 15.4, 4.6, 19.6, 4.2),
        ("c", 19.6, 8.6, 16.6, 11.0, 12.0, 11.0),
        ("z",),
    )
    return p


def _leaf_fill() -> QPainterPath:
    return _leaf()


def _check() -> QPainterPath:
    return _path(("m", 5.6, 12.4), ("l", 10.2, 17), ("l", 18.4, 7.4))


def _check_fill() -> QPainterPath:
    p = _circle(12, 12, 9)
    return p


def _close() -> QPainterPath:
    return _path(("m", 6.6, 6.6), ("l", 17.4, 17.4), ("m", 17.4, 6.6),
                 ("l", 6.6, 17.4))


def _minimise() -> QPainterPath:
    return _path(("m", 6.4, 12), ("l", 17.6, 12))


def _maximise() -> QPainterPath:
    return _rounded(6.6, 6.6, 10.8, 10.8, 2.0)


def _alert() -> QPainterPath:
    p = _circle(12, 12, 8.4)
    p.moveTo(12, 7.4)
    p.lineTo(12, 13)
    p.moveTo(12, 16)
    p.lineTo(12, 16.4)
    return p


def _wifi_off() -> QPainterPath:
    p = _path(("m", 4.0, 9.4), ("c", 8.6, 5.4, 15.4, 5.4, 20.0, 9.4))
    p.moveTo(7.2, 13.0)
    p.cubicTo(9.9, 10.7, 14.1, 10.7, 16.8, 13.0)
    p.moveTo(10.2, 16.4)
    p.cubicTo(11.3, 15.5, 12.7, 15.5, 13.8, 16.4)
    p.moveTo(12, 19.4)
    p.lineTo(12, 19.8)
    p.moveTo(4.6, 19.6)
    p.lineTo(19.4, 4.8)
    return p


def _sparkle() -> QPainterPath:
    return _path(
        ("m", 12, 3.4), ("c", 13.0, 9.2, 14.8, 11.0, 20.6, 12.0),
        ("c", 14.8, 13.0, 13.0, 14.8, 12.0, 20.6),
        ("c", 11.0, 14.8, 9.2, 13.0, 3.4, 12.0),
        ("c", 9.2, 11.0, 11.0, 9.2, 12.0, 3.4), ("z",),
    )


def _dot() -> QPainterPath:
    return _circle(12, 12, 4.2)


def _chevron_down() -> QPainterPath:
    return _path(("m", 7.4, 10), ("l", 12, 14.6), ("l", 16.6, 10))


def _keyboard() -> QPainterPath:
    """Settings > Shortcuts. Three rows of keys, the bottom one a space bar."""
    p = _rounded(2.6, 6.2, 18.8, 11.6, 2.4)
    for x in (5.6, 9.0, 12.4, 15.8):
        p.moveTo(x, 9.6)
        p.lineTo(x + 0.2, 9.6)
    for x in (6.6, 10.0, 13.4):
        p.moveTo(x, 12.4)
        p.lineTo(x + 0.2, 12.4)
    p.moveTo(8.2, 15.2)
    p.lineTo(15.8, 15.2)
    return p


def _palette() -> QPainterPath:
    """Settings > Appearance. A paint palette with its thumb hole and wells."""
    p = _path(
        ("m", 12, 3.4),
        ("c", 16.9, 3.4, 20.6, 7.0, 20.6, 11.6),
        ("c", 20.6, 14.4, 18.6, 15.6, 16.6, 15.6),
        ("l", 14.8, 15.6),
        ("c", 13.6, 15.6, 13.0, 16.4, 13.0, 17.3),
        ("c", 13.0, 19.1, 14.4, 19.2, 14.0, 20.2),
        ("c", 13.7, 20.9, 12.9, 20.6, 12.0, 20.6),
        ("c", 7.1, 20.6, 3.4, 16.8, 3.4, 12.0),
        ("c", 3.4, 7.2, 7.1, 3.4, 12.0, 3.4),
        ("z",),
    )
    for cx, cy in ((8.0, 8.4), (12.4, 7.2), (16.0, 10.0), (7.4, 13.0)):
        p.addEllipse(QPointF(cx, cy), 1.0, 1.0)
    return p


def _trash() -> QPainterPath:
    """"Deleting files" on the safety list."""
    p = _path(("m", 4.2, 6.6), ("l", 19.8, 6.6))
    p.addPath(_path(
        ("m", 6.4, 6.6), ("l", 7.4, 19.4),
        ("c", 7.5, 20.2, 8.1, 20.6, 8.8, 20.6),
        ("l", 15.2, 20.6),
        ("c", 15.9, 20.6, 16.5, 20.2, 16.6, 19.4),
        ("l", 17.6, 6.6),
    ))
    p.moveTo(9.2, 6.6)
    p.lineTo(9.6, 4.2)
    p.lineTo(14.4, 4.2)
    p.lineTo(14.8, 6.6)
    p.moveTo(10.4, 10.2)
    p.lineTo(10.8, 17.0)
    p.moveTo(13.6, 10.2)
    p.lineTo(13.2, 17.0)
    return p


def _card() -> QPainterPath:
    """"Payments" on the safety list: a bank card with its magnetic stripe."""
    p = _rounded(2.8, 5.4, 18.4, 13.2, 2.6)
    p.moveTo(2.8, 9.8)
    p.lineTo(21.2, 9.8)
    p.moveTo(6.2, 14.4)
    p.lineTo(10.4, 14.4)
    return p


ICONS: dict[str, Callable[[], QPainterPath]] = {
    "home": _home,
    "clock": _clock,
    "grid": _grid,
    "gear": _gear,
    "info": _info,
    "search": _search,
    "arrow_right": _arrow_right,
    "arrow_left": _arrow_left,
    "cursor": _cursor,
    "play": _play,
    "send": _send,
    "paperclip": _paperclip,
    "folder": _folder,
    "mail": _mail,
    "train": _train,
    "globe": _globe,
    "video": _video,
    "doc": _doc,
    "lock": _lock,
    "shield": _shield,
    "heart": _heart,
    "leaf": _leaf,
    "leaf_fill": _leaf_fill,
    "check": _check,
    "check_fill": _check_fill,
    "close": _close,
    "minimise": _minimise,
    "maximise": _maximise,
    "alert": _alert,
    "wifi_off": _wifi_off,
    "sparkle": _sparkle,
    "dot": _dot,
    "chevron_down": _chevron_down,
    "keyboard": _keyboard,
    "palette": _palette,
    "trash": _trash,
    "card": _card,
}


def paint(painter: QPainter, name: str, rect: QRectF, colour: QColor,
          width: float = 1.8, opacity: float = 1.0) -> None:
    """Draw ``name`` to fit ``rect``.

    ``width`` is the stroke weight on the 24-unit grid, so it scales with the
    icon and keeps the same visual weight at every size. Unknown names draw
    nothing rather than raising: an icon is never the point of the screen.
    """
    builder = ICONS.get(name)
    if builder is None:
        return

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if opacity < 1.0:
        painter.setOpacity(painter.opacity() * opacity)

    scale = min(rect.width(), rect.height()) / 24.0
    painter.translate(rect.center())
    painter.scale(scale, scale)
    painter.translate(-12.0, -12.0)

    path = builder()
    if name in _FILLED:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawPath(path)
    else:
        pen = QPen(colour, width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    painter.restore()
