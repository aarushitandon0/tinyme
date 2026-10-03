"""The pieces the window is built from, and the way they move.

Everything here paints itself. That is a deliberate choice rather than a
stylistic one: these widgets animate on hover, focus and press, and a Qt
stylesheet has to be re-parsed every time it changes, so animating one means
re-parsing it sixty times a second. Painting also lets a card lift its icon,
title and border together as one object, which is what makes the motion read as
a card moving rather than a repaint.

The motion vocabulary, used consistently so the whole window feels like one
thing:

* **hover** — 0 to 1 over ``theme.FAST``, out-cubic. Lifts a surface, deepens
  its shadow, warms its fill.
* **press** — 0 to 1 over ~90 ms. Pushes the surface back down, slightly
  smaller. Release springs back with out-back.
* **enter** — 0 to 1 over ``theme.MEDIUM``, out-back, staggered across a row by
  ``theme.STAGGER``. Fades a thing in while it rises 10 px and grows from 0.96.
* **glow** — the focus ring on the prompt bar, and the breathing of anything
  that means "Tiny Me is working".

CLAUDE.md rule 8 applies to every class here: Qt widgets, main thread only.
"""

from __future__ import annotations

import math
from typing import Callable, Sequence

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QGridLayout,
    QLayout,
    QLineEdit,
    QStackedWidget,
    QWidget,
)

from app import icons, theme


# --- painting helpers ------------------------------------------------------


def soft_shadow(painter: QPainter, rect: QRectF, radius: float, spread: float,
                alpha: float, colour: QColor | None = None) -> None:
    """A cheap stacked-ring shadow.

    Qt's drop-shadow effect would be smoother, but it lives outside the
    paintEvent and cannot be animated in step with the fill, which is exactly
    what hover needs. Eight rings is enough to read as a blur at this size.
    """
    if spread <= 0 or alpha <= 0:
        return
    base = QColor(colour) if colour is not None else QColor(36, 48, 36)
    painter.setPen(Qt.PenStyle.NoPen)
    rings = 8
    for i in range(rings, 0, -1):
        t = i / rings
        ring = rect.adjusted(-spread * t, -spread * t * 0.55,
                             spread * t, spread * t * 1.25)
        shade = QColor(base)
        shade.setAlphaF(alpha * (1.0 - t) ** 2 * 0.5)
        painter.setBrush(shade)
        painter.drawRoundedRect(ring, radius + spread * t, radius + spread * t)


def draw_text(painter: QPainter, rect: QRectF, text: str, font: QFont,
              colour: QColor, flags: int | Qt.AlignmentFlag, *,
              opacity: float = 1.0) -> QRectF:
    """Draw text and return the rectangle it actually occupied."""
    painter.save()
    if opacity < 1.0:
        painter.setOpacity(painter.opacity() * opacity)
    painter.setFont(font)
    painter.setPen(QPen(colour))
    painter.drawText(rect, int(flags), text)
    bounds = painter.boundingRect(rect, int(flags), text)
    painter.restore()
    return QRectF(bounds)


def elide(text: str, font: QFont, width: float) -> str:
    metrics = QFontMetricsF(font)
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)


def text_height(text: str, font: QFont, width: float) -> float:
    """How tall ``text`` will be when wrapped into ``width``."""
    metrics = QFontMetricsF(font)
    rect = metrics.boundingRect(
        QRectF(0, 0, width, 10_000),
        int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop),
        text,
    )
    return rect.height()


def animate(target, name: bytes, to: float, duration: int,
            curve: QEasingCurve.Type = theme.EASE_OUT,
            store: str | None = None) -> QPropertyAnimation:
    """Start a property animation, keeping a reference so Qt does not bin it.

    ``store`` names the attribute the animation is parked on; reusing the same
    name on the same object replaces the running animation, which is what makes
    a fast hover-in/hover-out never fight itself.
    """
    key = store or f"_anim_{name.decode()}"
    previous = getattr(target, key, None)
    if previous is not None:
        previous.stop()
    anim = QPropertyAnimation(target, name, target)
    anim.setDuration(duration)
    anim.setEasingCurve(curve)
    anim.setStartValue(float(getattr(target, name.decode())))
    anim.setEndValue(float(to))
    setattr(target, key, anim)
    anim.start()
    return anim


# --- the base surface ------------------------------------------------------


class Surface(QWidget):
    """A self-painting widget with hover, press and entrance animation.

    Subclasses override :meth:`paint_content` and get the motion for free. The
    three floats are real Qt properties so ``QPropertyAnimation`` can drive
    them; each setter calls ``update()``, so a running animation repaints.
    """

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None, *, clickable: bool = True,
                 lift: float = 4.0) -> None:
        super().__init__(parent)
        self._hover = 0.0
        self._press = 0.0
        self._enter = 1.0
        self._lift_px = lift
        self._clickable = clickable
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)

    # -- animated properties --

    def _get_hover(self) -> float:
        return self._hover

    def _set_hover(self, value: float) -> None:
        self._hover = value
        self.update()

    hover = Property(float, _get_hover, _set_hover)

    def _get_press(self) -> float:
        return self._press

    def _set_press(self, value: float) -> None:
        self._press = value
        self.update()

    press = Property(float, _get_press, _set_press)

    def _get_enter(self) -> float:
        return self._enter

    def _set_enter(self, value: float) -> None:
        self._enter = value
        self.update()

    enter = Property(float, _get_enter, _set_enter)

    # -- entrance --

    def play_entrance(self, delay_ms: int = 0) -> None:
        """Fade up from 10 px below at 0.96 scale. Call once, on show."""
        self._enter = 0.0
        self.update()
        QTimer.singleShot(
            max(0, delay_ms),
            lambda: animate(self, b"enter", 1.0, theme.MEDIUM + 140, theme.EASE_POP),
        )

    # -- interaction --

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.isEnabled():
            animate(self, b"hover", 1.0, theme.FAST)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        animate(self, b"hover", 0.0, theme.MEDIUM)
        animate(self, b"press", 0.0, theme.FAST)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            animate(self, b"press", 1.0, 90)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            animate(self, b"press", 0.0, theme.MEDIUM, theme.EASE_POP)
            if self.rect().contains(event.position().toPoint()) and self.isEnabled():
                self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Space and Enter activate a focused card, so this is keyboard-usable."""
        if self._clickable and event.key() in (
            Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter
        ):
            animate(self, b"press", 1.0, 80)
            QTimer.singleShot(
                90, lambda: animate(self, b"press", 0.0, theme.MEDIUM, theme.EASE_POP)
            )
            self.clicked.emit()
            return
        super().keyPressEvent(event)

    # -- painting --

    @property
    def lift(self) -> float:
        """Pixels the surface floats by right now: up on hover, back on press."""
        return self._lift_px * self._hover - self._lift_px * 0.9 * self._press

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if self._enter < 1.0:
            # Fade in while rising and growing: one gesture, three channels.
            painter.setOpacity(max(0.0, self._enter))
            scale = 0.96 + 0.04 * self._enter
            centre = QPointF(self.width() / 2.0, self.height() / 2.0)
            painter.translate(centre)
            painter.scale(scale, scale)
            painter.translate(-centre)
            painter.translate(0.0, (1.0 - self._enter) * 10.0)

        painter.translate(0.0, -self.lift)
        self.paint_content(painter, QRectF(self.rect()))
        painter.end()

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        """Subclasses draw here, in widget coordinates."""


# --- cards -----------------------------------------------------------------


class Card(Surface):
    """A rounded panel: fill, hairline border, shadow that deepens on hover."""

    def __init__(self, parent: QWidget | None = None, *,
                 fill: str = theme.PAPER,
                 fill_hover: str | None = None,
                 border: str = theme.LINE,
                 border_hover: str | None = None,
                 radius: int = theme.RADIUS_LG,
                 clickable: bool = True,
                 lift: float = 4.0,
                 shadow: float = 1.0) -> None:
        super().__init__(parent, clickable=clickable, lift=lift)
        self._fill = fill
        self._fill_hover = fill_hover or fill
        self._border = border
        self._border_hover = border_hover or border
        self._radius = radius
        self._shadow = shadow

    def body_rect(self, rect: QRectF) -> QRectF:
        """The panel itself, inset so the border lands on a whole pixel."""
        return rect.adjusted(0.5, 0.5, -0.5, -0.5)

    def paint_panel(self, painter: QPainter, rect: QRectF) -> QRectF:
        body = self.body_rect(rect)
        h = self._hover
        if self._shadow:
            soft_shadow(painter, body, self._radius,
                        spread=(3.0 + 7.0 * h) * self._shadow,
                        alpha=(0.16 + 0.22 * h) * self._shadow)
        painter.setPen(QPen(theme.mix(self._border, self._border_hover, h), 1.0))
        painter.setBrush(theme.mix(self._fill, self._fill_hover, h))
        painter.drawRoundedRect(body, self._radius, self._radius)
        if self.hasFocus():
            ring = body.adjusted(-2.5, -2.5, 2.5, 2.5)
            painter.setPen(QPen(theme.qcolor(theme.HIGHLIGHT, 0.75), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(ring, self._radius + 3, self._radius + 3)
        return body

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        self.paint_panel(painter, rect)


class ActionCard(Card):
    """The two big choices: "Show me how" and "Do it for me".

    Sized by its own content so the pair always match height, and the arrow
    slides right on hover — the one bit of motion that says "this goes
    somewhere" without any words.
    """

    def __init__(self, title: str, body: str, icon: str, *,
                 fill: str, fill_hover: str, border: str, ink: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent, fill=fill, fill_hover=fill_hover,
                         border=border, border_hover=theme.mix(border, ink, 0.3).name(),
                         radius=theme.RADIUS_LG, lift=5.0)
        self._title = title
        self._body = body
        self._icon = icon
        self._ink = ink
        self.setMinimumHeight(132)
        self.setToolTip(f"{title} — {body}")

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Grow when the body wraps, so the arrow never lands on the text."""
        inner_width = max(60.0, self.width() - 40)
        text_left = 54.0 if inner_width >= 250 else 0.0
        body_width = max(60.0, inner_width - text_left - 44)
        body_h = text_height(self._body, theme.font(10.5), body_width)
        self.setMinimumHeight(round(max(132.0, 18 + 28 + 4 + body_h + 44)))
        super().resizeEvent(event)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        body = self.paint_panel(painter, rect)
        inner = body.adjusted(20, 18, -20, -18)
        ink = QColor(self._ink)

        # Below this the icon is costing more room than it earns, so the words
        # get it back. These two cards are the whole choice on this screen;
        # a clipped "Show me ho" would be the worst thing on it.
        roomy = inner.width() >= 250
        if roomy:
            icon_box = QRectF(inner.left(), inner.top() + 2, 38, 38)
            icons.paint(painter, self._icon, icon_box, ink, width=1.9)
            text_left = icon_box.right() + 16
        else:
            text_left = inner.left()

        # The title spans the full width: the arrow sits on the bottom row and
        # never shares a line with it.
        title_width = inner.right() - text_left
        title_font = theme.display_font(16.5, QFont.Weight.DemiBold)
        if QFontMetricsF(title_font).horizontalAdvance(self._title) > title_width:
            title_font = theme.display_font(14, QFont.Weight.DemiBold)
        title_rect = QRectF(text_left, inner.top(), title_width, 28)
        draw_text(painter, title_rect, elide(self._title, title_font, title_width),
                  title_font, ink,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        body_font = theme.font(10.5)
        # The body does make room for the arrow, which sits beside its last line.
        body_rect = QRectF(text_left, title_rect.bottom() + 4,
                           max(60.0, title_width - 44),
                           inner.bottom() - title_rect.bottom() - 4)
        draw_text(painter, body_rect, self._body, body_font,
                  theme.mix(ink, theme.CREAM, 0.32),
                  Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop)

        # The arrow: a filled disc that travels 5 px right on hover.
        travel = 5.0 * self._hover
        disc = QRectF(inner.right() - 34 + travel, inner.bottom() - 34, 34, 34)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.mix(theme.qcolor(ink, 0.10), ink, self._hover))
        painter.drawEllipse(disc)
        icons.paint(painter, "arrow_right", disc.adjusted(8, 8, -8, -8),
                    theme.mix(ink, theme.CREAM, self._hover), width=2.0)


class ExampleCard(Card):
    """One of the "Try these examples" tiles: an icon and a line of text."""

    def __init__(self, text: str, icon: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, fill=theme.PAPER, fill_hover="#FFFFFF",
                         border=theme.LINE, border_hover=theme.SAGE_LINE,
                         radius=theme.RADIUS_MD, lift=3.0)
        self._text = text
        self._icon = icon
        self.setMinimumHeight(60)
        self.setToolTip(text)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        body = self.paint_panel(painter, rect)
        inner = body.adjusted(14, 10, -12, -10)

        icon_box = QRectF(inner.left(), inner.center().y() - 11, 22, 22)
        icons.paint(painter, self._icon, icon_box,
                    theme.mix(theme.INK_SOFT, theme.PRIMARY, self._hover), width=1.7)

        font = theme.font(10.5)
        text_rect = QRectF(icon_box.right() + 12, inner.top(),
                           inner.right() - icon_box.right() - 12, inner.height())
        draw_text(painter, text_rect, self._text, font, theme.qcolor(theme.INK),
                  Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignVCenter)


class TrustBadge(Card):
    """A promise, not a control: "Works offline", "Safe by design"."""

    def __init__(self, title: str, body: str, icon: str, tint: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent, fill=theme.PAPER, border=theme.LINE_SOFT,
                         radius=theme.RADIUS_MD, clickable=False, lift=0.0,
                         shadow=0.55)
        self._title = title
        self._body = body
        self._icon = icon
        self._tint = tint
        self.setMinimumHeight(72)

    def _text_width(self) -> float:
        return max(80.0, self.width() - 24 - 32 - 11 - 12)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # These sit three-across and their text is the first thing to run out
        # of room, so the badge grows rather than truncating a promise.
        width = self._text_width()
        title_h = text_height(self._title, theme.font(10, QFont.Weight.DemiBold), width)
        body_h = text_height(self._body, theme.font(8.8), width)
        self.setMinimumHeight(round(max(72.0, title_h + body_h + 24)))
        super().resizeEvent(event)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        body = self.paint_panel(painter, rect)
        inner = body.adjusted(12, 10, -12, -10)

        chip = QRectF(inner.left(), inner.center().y() - 16, 32, 32)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(self._tint, 0.22))
        painter.drawRoundedRect(chip, 10, 10)
        icons.paint(painter, self._icon, chip.adjusted(7, 7, -7, -7),
                    QColor(self._tint), width=1.8)

        left = chip.right() + 11
        width = inner.right() - left
        title_font = theme.font(10, QFont.Weight.DemiBold)
        body_font = theme.font(8.8)
        title_h = text_height(self._title, title_font, width)
        body_h = text_height(self._body, body_font, width)
        top = inner.center().y() - (title_h + body_h) / 2.0
        draw_text(painter, QRectF(left, top, width, title_h + 1),
                  self._title, title_font, theme.qcolor(theme.INK),
                  Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop)
        draw_text(painter, QRectF(left, top + title_h, width, body_h + 1),
                  self._body, body_font, theme.qcolor(theme.INK_MUTED),
                  Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop)


# --- small controls --------------------------------------------------------


class Chip(Surface):
    """A suggestion under the prompt bar. Click fills the box."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, lift=2.0)
        self._text = text
        font = theme.font(9.8)
        width = QFontMetricsF(font).horizontalAdvance(text) + 28
        self.setFixedSize(QSize(round(width), 30))

    @property
    def text(self) -> str:
        return self._text

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        body = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        h = self._hover
        soft_shadow(painter, body, 15, spread=1.5 + 3.0 * h, alpha=0.10 + 0.12 * h)
        painter.setPen(QPen(theme.mix(theme.LINE, theme.SAGE_LINE, h), 1.0))
        painter.setBrush(theme.mix(theme.PAPER, "#FFFFFF", h))
        painter.drawRoundedRect(body, body.height() / 2, body.height() / 2)
        draw_text(painter, body, self._text, theme.font(9.8),
                  theme.mix(theme.INK_SOFT, theme.PRIMARY, h),
                  Qt.AlignmentFlag.AlignCenter)


class Button(Surface):
    """Primary, secondary and ghost, from the design sheet's button row."""

    PRIMARY = "primary"
    SECONDARY = "secondary"
    GHOST = "ghost"
    DANGER = "danger"

    def __init__(self, text: str = "", *, kind: str = PRIMARY,
                 icon: str | None = None, parent: QWidget | None = None,
                 width: int | None = None, height: int = 38) -> None:
        super().__init__(parent, lift=2.0)
        self._text = text
        self._kind = kind
        self._icon = icon
        font = theme.font(10.2, QFont.Weight.DemiBold)
        content = QFontMetricsF(font).horizontalAdvance(text) if text else 0
        if icon:
            content += 22 if text else 10
        self.setFixedHeight(height)
        self.setMinimumWidth(width or round(content + 34))
        if width:
            self.setFixedWidth(width)

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def _colours(self) -> tuple[QColor, QColor, QColor]:
        """fill, border, ink — already blended for the current hover/press."""
        h, p = self._hover, self._press
        if self._kind == self.PRIMARY:
            fill = theme.mix(theme.PRIMARY, theme.PRIMARY_HOVER, h)
            fill = theme.mix(fill, theme.PRIMARY_PRESSED, p)
            return fill, fill, theme.qcolor(theme.CREAM)
        if self._kind == self.DANGER:
            fill = theme.mix(theme.CORAL_BG, "#F3D2CC", h)
            return fill, theme.qcolor(theme.CORAL_LINE), theme.qcolor("#A8382E")
        if self._kind == self.SECONDARY:
            fill = theme.mix(theme.PAPER, "#FFFFFF", h)
            return fill, theme.mix(theme.LINE, theme.TAN_LINE, h), theme.qcolor(theme.INK)
        fill = theme.mix(theme.qcolor(theme.LINE_SOFT, 0.0), theme.LINE_SOFT, h)
        return fill, theme.qcolor(theme.LINE, 0.55 + 0.45 * h), theme.qcolor(theme.INK_SOFT)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        body = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        fill, border, ink = self._colours()
        if not self.isEnabled():
            fill.setAlphaF(fill.alphaF() * 0.45)
            ink.setAlphaF(0.45)
            border.setAlphaF(border.alphaF() * 0.45)
        radius = min(theme.RADIUS_MD, body.height() / 2)
        if self._kind in (self.PRIMARY, self.DANGER) and self.isEnabled():
            soft_shadow(painter, body, radius,
                        spread=2.0 + 4.0 * self._hover,
                        alpha=0.18 + 0.14 * self._hover,
                        colour=QColor(theme.FOREST_DEEP))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(body, radius, radius)
        if self.hasFocus():
            painter.setPen(QPen(theme.qcolor(theme.HIGHLIGHT, 0.8), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            ring = body.adjusted(-2.5, -2.5, 2.5, 2.5)
            painter.drawRoundedRect(ring, radius + 3, radius + 3)

        font = theme.font(10.2, QFont.Weight.DemiBold)
        if self._icon and not self._text:
            icons.paint(painter, self._icon, body.adjusted(9, 9, -9, -9), ink, width=2.0)
            return
        if self._icon:
            metrics = QFontMetricsF(font)
            text_w = metrics.horizontalAdvance(self._text)
            total = text_w + 22
            left = body.center().x() - total / 2
            icons.paint(painter, self._icon,
                        QRectF(left, body.center().y() - 9, 18, 18), ink, width=1.9)
            draw_text(painter, QRectF(left + 22, body.top(), text_w + 2, body.height()),
                      self._text, font, ink,
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return
        draw_text(painter, body, self._text, font, ink, Qt.AlignmentFlag.AlignCenter)


class SendButton(Surface):
    """The round primary arrow at the end of the prompt bar.

    It carries the busy state too: while Tiny Me is thinking it spins a short
    arc instead of showing the arrow, so the one control she just pressed is
    also the thing that tells her it was heard.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, lift=1.5)
        self.setFixedSize(QSize(46, 46))
        self._busy = False
        self._spin = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._advance)
        self.setToolTip("Send")

    def set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _advance(self) -> None:
        self._spin = (self._spin + 4.6) % 360.0
        self.update()

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        body = rect.adjusted(2, 2, -2, -2)
        h, p = self._hover, self._press
        fill = theme.mix(theme.PRIMARY, theme.PRIMARY_HOVER, h)
        fill = theme.mix(fill, theme.PRIMARY_PRESSED, p)
        if not self.isEnabled():
            fill.setAlphaF(0.4)
        soft_shadow(painter, body, body.height() / 2,
                    spread=2.5 + 5.0 * h, alpha=0.22 + 0.16 * h,
                    colour=QColor(theme.FOREST_DEEP))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(body)

        ink = theme.qcolor(theme.CREAM, 1.0 if self.isEnabled() else 0.5)
        if self._busy:
            arc = body.adjusted(12, 12, -12, -12)
            pen = QPen(ink, 2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(arc, round(-self._spin * 16), 110 * 16)
            return
        icons.paint(painter, "arrow_right", body.adjusted(12, 12, -12, -12),
                    ink, width=2.2)


class Toggle(Surface):
    """The settings switch. The knob travels; the track crossfades."""

    toggled = Signal(bool)

    def __init__(self, on: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent, lift=0.0)
        self.setFixedSize(QSize(46, 26))
        self._on = on
        self._pos = 1.0 if on else 0.0
        self.clicked.connect(self._flip)

    def _get_pos(self) -> float:
        return self._pos

    def _set_pos(self, value: float) -> None:
        self._pos = value
        self.update()

    pos = Property(float, _get_pos, _set_pos)

    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool) -> None:
        if on == self._on:
            return
        self._on = on
        animate(self, b"pos", 1.0 if on else 0.0, theme.MEDIUM, theme.EASE_POP)

    def _flip(self) -> None:
        self.set_on(not self._on)
        self.toggled.emit(self._on)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        track = rect.adjusted(1, 3, -1, -3)
        if not self.isEnabled():
            # A read-only switch still has to say which way it is set, so it
            # fades rather than greying out into something unreadable.
            painter.setOpacity(0.55)
        painter.setPen(QPen(theme.mix(theme.LINE, theme.PRIMARY, self._pos), 1.0))
        painter.setBrush(theme.mix(theme.CREAM_DEEP, theme.PRIMARY, self._pos))
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)

        travel = track.width() - track.height()
        knob_d = track.height() - 5
        knob = QRectF(track.left() + 2.5 + travel * self._pos,
                      track.top() + 2.5, knob_d, knob_d)
        soft_shadow(painter, knob, knob_d / 2, spread=2.0, alpha=0.25)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor("#FFFFFF"))
        painter.drawEllipse(knob)


# --- the prompt bar --------------------------------------------------------


class PromptBar(QWidget):
    """Search icon, her text box, and the send arrow.

    The only widget here with a real child control, because a hand-painted text
    field would mean reimplementing selection, IME and accessibility. The
    painting is the frame; the ``QLineEdit`` sits inside it with no chrome of
    its own, and the focus ring is animated by this widget.
    """

    submitted = Signal(str)
    attach_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._glow = 0.0
        self.setFixedHeight(62)

        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("What would you like to do?")
        self.edit.setFont(theme.font(12))
        self.edit.setFrame(False)
        self.edit.returnPressed.connect(self._submit)
        self.edit.installEventFilter(self)

        self.send = SendButton(self)
        self.send.clicked.connect(self._submit)

    # -- animated focus ring --

    def _get_glow(self) -> float:
        return self._glow

    def _set_glow(self, value: float) -> None:
        self._glow = value
        self.update()

    glow = Property(float, _get_glow, _set_glow)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
        if obj is self.edit:
            if event.type() == event.Type.FocusIn:
                animate(self, b"glow", 1.0, theme.MEDIUM)
            elif event.type() == event.Type.FocusOut:
                animate(self, b"glow", 0.0, theme.MEDIUM)
        return super().eventFilter(obj, event)

    # -- api --

    def text(self) -> str:
        return self.edit.text().strip()

    def set_text(self, text: str) -> None:
        self.edit.setText(text)
        self.edit.setFocus()
        self.edit.selectAll()

    def clear(self) -> None:
        self.edit.clear()

    def set_busy(self, busy: bool) -> None:
        self.edit.setEnabled(not busy)
        self.send.setEnabled(not busy)
        self.send.set_busy(busy)

    def focus(self) -> None:
        self.edit.setFocus()
        self.edit.selectAll()

    def _submit(self) -> None:
        text = self.text()
        if text:
            self.submitted.emit(text)

    # -- layout and paint --

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        h = self.height()
        self.send.move(self.width() - h + 6, 8)
        self.edit.setGeometry(52, 14, self.width() - h - 60, h - 28)
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = theme.RADIUS_LG

        soft_shadow(painter, body, radius, spread=3.0 + 5.0 * self._glow,
                    alpha=0.14 + 0.14 * self._glow)
        painter.setPen(QPen(theme.mix(theme.LINE, theme.PRIMARY, self._glow),
                            1.0 + 0.8 * self._glow))
        painter.setBrush(theme.qcolor("#FFFFFF"))
        painter.drawRoundedRect(body, radius, radius)

        if self._glow > 0.01:
            ring = body.adjusted(-3, -3, 3, 3)
            painter.setPen(QPen(theme.qcolor(theme.SAGE_LINE, 0.55 * self._glow), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(ring, radius + 3, radius + 3)

        icons.paint(painter, "search", QRectF(20, body.center().y() - 11, 22, 22),
                    theme.mix(theme.INK_MUTED, theme.PRIMARY, self._glow), width=1.8)
        painter.end()


# --- sidebar ---------------------------------------------------------------


class NavItem(Surface):
    """One sidebar row. The selected pill is drawn by :class:`Sidebar`."""

    def __init__(self, text: str, icon: str, parent: QWidget | None = None) -> None:
        super().__init__(parent, lift=0.0)
        self._text = text
        self._icon = icon
        self._selected = 0.0
        self.setFixedHeight(42)

    @property
    def label(self) -> str:
        return self._text

    def _get_selected(self) -> float:
        return self._selected

    def _set_selected(self, value: float) -> None:
        self._selected = value
        self.update()

    selected = Property(float, _get_selected, _set_selected)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        s, h = self._selected, self._hover
        if h > 0.01 and s < 0.99:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor("#FFFFFF", 0.07 * h * (1 - s)))
            painter.drawRoundedRect(rect.adjusted(0, 1, 0, -1),
                                    theme.RADIUS_MD, theme.RADIUS_MD)

        ink = theme.mix(theme.ON_FOREST_MUTED, theme.ON_FOREST, max(s, h * 0.6))
        icon_box = QRectF(rect.left() + 14, rect.center().y() - 11, 22, 22)
        icons.paint(painter, self._icon, icon_box, ink, width=1.8 + 0.3 * s)
        weight = QFont.Weight.DemiBold if s > 0.5 else QFont.Weight.Normal
        draw_text(painter, QRectF(icon_box.right() + 13, rect.top(),
                                  rect.width() - 60, rect.height()),
                  self._text, theme.font(10.8, weight), ink,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


class StatusPill(QWidget):
    """"Works offline" — with a dot that breathes, so it reads as live."""

    def __init__(self, title: str, body: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._body = body
        self._phase = 0.0
        self.setFixedHeight(58)
        timer = QTimer(self)
        timer.setInterval(40)
        timer.timeout.connect(self._tick)
        timer.start()
        self._timer = timer

    def _tick(self) -> None:
        self._phase = (self._phase + 0.045) % (2 * math.pi)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(theme.qcolor("#FFFFFF", 0.12), 1.0))
        painter.setBrush(theme.qcolor("#FFFFFF", 0.06))
        painter.drawRoundedRect(body, theme.RADIUS_MD, theme.RADIUS_MD)

        pulse = 0.5 + 0.5 * math.sin(self._phase)
        centre = QPointF(body.left() + 20, body.center().y() - 7)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(theme.ONLINE_DOT, 0.18 + 0.22 * pulse))
        painter.drawEllipse(centre, 8.5 + 2.5 * pulse, 8.5 + 2.5 * pulse)
        painter.setBrush(theme.qcolor(theme.ONLINE_DOT))
        painter.drawEllipse(centre, 4.2, 4.2)

        left = body.left() + 34
        width = body.width() - 44
        draw_text(painter, QRectF(left, body.top() + 10, width, 16), self._title,
                  theme.font(10, QFont.Weight.DemiBold),
                  theme.qcolor(theme.ON_FOREST),
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        draw_text(painter, QRectF(left, body.top() + 26, width, 26),
                  self._body, theme.font(8.6),
                  theme.qcolor(theme.ON_FOREST_MUTED),
                  Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop)
        painter.end()


# --- feedback --------------------------------------------------------------


class ThinkingDots(QWidget):
    """Three dots breathing in sequence: "Tiny Me is looking"."""

    def __init__(self, parent: QWidget | None = None,
                 colour: str = theme.PRIMARY) -> None:
        super().__init__(parent)
        self._phase = 0.0
        self._colour = colour
        self.setFixedSize(QSize(38, 12))
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.show()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.11) % (2 * math.pi)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            wave = 0.5 + 0.5 * math.sin(self._phase - i * 0.7)
            painter.setBrush(theme.qcolor(self._colour, 0.3 + 0.7 * wave))
            centre = QPointF(6 + i * 13, self.height() / 2 - 2.0 * wave)
            painter.drawEllipse(centre, 3.4 + 0.8 * wave, 3.4 + 0.8 * wave)
        painter.end()


class Banner(Surface):
    """The states from the design sheet: error, privacy stop, plain note.

    Collapses to zero height when there is nothing to say, animating its own
    ``reveal`` so the layout below slides rather than jumps.
    """

    ERROR = "error"
    PRIVACY = "privacy"
    NOTE = "note"
    GOOD = "good"

    _LOOK = {
        ERROR: ("alert", theme.CORAL_BG, theme.CORAL_LINE, "#A8382E"),
        PRIVACY: ("lock", theme.CORAL_BG, theme.CORAL_LINE, "#A8382E"),
        NOTE: ("info", theme.CREAM_DEEP, theme.LINE, theme.INK_SOFT),
        GOOD: ("check", theme.SAGE_BG, theme.SAGE_LINE, theme.SAGE_INK),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, clickable=False, lift=0.0)
        self._text = ""
        self._kind = self.NOTE
        self._reveal = 0.0
        self._full_height = 0
        self.setFixedHeight(0)

    def _get_reveal(self) -> float:
        return self._reveal

    def _set_reveal(self, value: float) -> None:
        self._reveal = value
        self.setFixedHeight(round(self._full_height * value))
        self.update()

    reveal = Property(float, _get_reveal, _set_reveal)

    def show_message(self, text: str, kind: str = NOTE) -> None:
        self._text = text
        self._kind = kind
        font = theme.font(10)
        width = max(160, self.width() - 86)
        self._full_height = round(max(52.0, text_height(text, font, width) + 30))
        animate(self, b"reveal", 1.0, theme.MEDIUM, theme.EASE_OUT)

    def dismiss(self) -> None:
        if self._reveal > 0:
            animate(self, b"reveal", 0.0, theme.MEDIUM, theme.EASE_IN_OUT)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        if self._reveal <= 0.01 or not self._text:
            return
        icon, fill, border, ink = self._LOOK.get(self._kind, self._LOOK[self.NOTE])
        body = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setOpacity(min(1.0, self._reveal * 1.4))
        painter.setPen(QPen(QColor(border), 1.0))
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(body, theme.RADIUS_MD, theme.RADIUS_MD)

        chip = QRectF(body.left() + 14, body.center().y() - 13, 26, 26)
        icons.paint(painter, icon, chip, QColor(ink), width=1.9)
        draw_text(painter, QRectF(chip.right() + 12, body.top() + 8,
                                  body.width() - 66, body.height() - 16),
                  self._text, theme.font(10), QColor(ink),
                  Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignVCenter)


class FadeLabel(QWidget):
    """A line of text that cross-fades when it changes.

    The status line changes often and abruptly — "Looking at your screen" to a
    real instruction — and a hard swap reads as a glitch. This fades the old
    words out and the new ones in, which also draws her eye to the change.
    """

    def __init__(self, text: str = "", *, font: QFont | None = None,
                 colour: str = theme.INK, align: Qt.AlignmentFlag | int =
                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._old = ""
        self._fade = 1.0
        self._font = font or theme.font(10.5)
        self._colour = colour
        self._align = int(align) | int(Qt.TextFlag.TextWordWrap)

    def _get_fade(self) -> float:
        return self._fade

    def _set_fade(self, value: float) -> None:
        self._fade = value
        self.update()

    fade = Property(float, _get_fade, _set_fade)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._old = self._text
        self._text = text
        self._fade = 0.0
        animate(self, b"fade", 1.0, theme.MEDIUM + 60, theme.EASE_OUT)

    def set_colour(self, colour: str) -> None:
        self._colour = colour
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        width = max(120, self.width())
        return QSize(width, round(text_height(self._text, self._font, width)) + 2)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        rect = QRectF(self.rect())
        if self._fade < 1.0 and self._old:
            # The old words leave upward as the new ones arrive from below.
            painter.save()
            painter.translate(0.0, -6.0 * self._fade)
            draw_text(painter, rect, self._old, self._font,
                      theme.qcolor(self._colour), self._align,
                      opacity=1.0 - self._fade)
            painter.restore()
        painter.save()
        painter.translate(0.0, 6.0 * (1.0 - self._fade))
        draw_text(painter, rect, self._text, self._font,
                  theme.qcolor(self._colour), self._align, opacity=self._fade)
        painter.restore()
        painter.end()


# --- the walkthrough -------------------------------------------------------


class StepRow(Surface):
    """One row of the walkthrough panel: pending, active, or done.

    State is a float, not an enum, because the change is animated: the badge
    fills in, the number cross-fades to a tick, and the body text opens. The
    active row also breathes, which is the panel's answer to "is it still
    doing something?".
    """

    PENDING, ACTIVE, DONE = 0, 1, 2

    def __init__(self, index: int, title: str, body: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent, clickable=False, lift=0.0)
        self._index = index
        self._title = title
        self._body = body
        self._state = self.PENDING
        self._active = 0.0
        self._done = 0.0
        self._phase = 0.0
        self._pulse = QTimer(self)
        self._pulse.setInterval(45)
        self._pulse.timeout.connect(self._tick)
        self.setMinimumHeight(self._wanted_height())

    # -- animated state --

    def _get_active(self) -> float:
        return self._active

    def _set_active(self, value: float) -> None:
        self._active = value
        self.setMinimumHeight(self._wanted_height())
        self.updateGeometry()
        self.update()

    active = Property(float, _get_active, _set_active)

    def _get_done(self) -> float:
        return self._done

    def _set_done(self, value: float) -> None:
        self._done = value
        self.update()

    done = Property(float, _get_done, _set_done)

    def _tick(self) -> None:
        self._phase = (self._phase + 0.07) % (2 * math.pi)
        self.update()

    def _wanted_height(self) -> float:
        base = 46.0
        if self._body:
            body_h = text_height(self._body, theme.font(9.8),
                                 max(140, self.width() - 60))
            return base + body_h * self._active + 8 * self._active
        return base

    def set_body(self, body: str) -> None:
        self._body = body
        self.setMinimumHeight(self._wanted_height())
        self.updateGeometry()
        self.update()

    def set_state(self, state: int) -> None:
        if state == self._state:
            return
        self._state = state
        animate(self, b"active", 1.0 if state == self.ACTIVE else 0.0,
                theme.MEDIUM + 80, theme.EASE_OUT, store="_anim_state_a")
        animate(self, b"done", 1.0 if state == self.DONE else 0.0,
                theme.MEDIUM + 80, theme.EASE_OUT, store="_anim_state_d")
        if state == self.ACTIVE:
            self._pulse.start()
        else:
            self._pulse.stop()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.setMinimumHeight(self._wanted_height())
        super().resizeEvent(event)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        a, d = self._active, self._done
        body = rect.adjusted(0.5, 0.5, -0.5, -0.5)

        if a > 0.01:
            painter.setPen(QPen(theme.qcolor(theme.SAGE_LINE, a), 1.0))
            painter.setBrush(theme.qcolor(theme.SAGE_BG, 0.55 * a))
            painter.drawRoundedRect(body, theme.RADIUS_MD, theme.RADIUS_MD)
        elif d > 0.01:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor(theme.SAGE_BG, 0.3 * d))
            painter.drawRoundedRect(body, theme.RADIUS_MD, theme.RADIUS_MD)

        # The badge: a hollow circle that fills as the step becomes real.
        badge = QRectF(body.left() + 12, body.top() + 11, 28, 28)
        filled = max(a, d)
        if a > 0.01:
            pulse = 0.5 + 0.5 * math.sin(self._phase)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.qcolor(theme.PRIMARY, 0.14 * a * pulse))
            grow = 4.0 * a * pulse
            painter.drawEllipse(badge.adjusted(-grow, -grow, grow, grow))

        painter.setPen(QPen(theme.mix(theme.LINE, theme.PRIMARY, filled), 1.4))
        painter.setBrush(theme.mix(theme.qcolor(theme.PAPER), theme.qcolor(theme.PRIMARY), filled))
        painter.drawEllipse(badge)

        number_ink = theme.mix(theme.INK_MUTED, theme.CREAM, filled)
        if d < 0.5:
            draw_text(painter, badge, str(self._index),
                      theme.font(10.2, QFont.Weight.DemiBold), number_ink,
                      Qt.AlignmentFlag.AlignCenter, opacity=1.0 - d * 2)
        if d > 0.0:
            icons.paint(painter, "check", badge.adjusted(7, 7, -7, -7),
                        theme.qcolor(theme.CREAM), width=2.2, opacity=d)

        left = badge.right() + 13
        width = body.right() - left - 12
        title_font = theme.font(10.6, QFont.Weight.DemiBold if filled > 0.4
                                else QFont.Weight.Normal)
        title_ink = theme.mix(theme.INK_MUTED, theme.INK, max(filled, 0.35))
        draw_text(painter, QRectF(left, body.top() + 12, width, 22),
                  elide(self._title, title_font, width), title_font, title_ink,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        if self._body and a > 0.02:
            draw_text(painter, QRectF(left, body.top() + 34, width,
                                      body.height() - 42),
                      self._body, theme.font(9.8),
                      theme.qcolor(theme.INK_SOFT),
                      Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop,
                      opacity=min(1.0, a * 1.6))


class ProgressTrack(QWidget):
    """The 1 — 2 — 3 rail at the top of the panel.

    The filled part of the rail animates between stops rather than snapping, so
    finishing a step reads as forward movement.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = 3
        self._progress = 0.0
        self.setFixedHeight(34)

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = value
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def set_count(self, count: int) -> None:
        self._count = max(1, count)
        self.update()

    def set_current(self, index: int) -> None:
        """``index`` is 1-based; 0 means nothing started yet."""
        target = 0.0 if self._count <= 1 else max(0.0, index - 1) / (self._count - 1)
        animate(self, b"progress", min(1.0, target), theme.SLOW, theme.EASE_OUT)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = 11.0
        y = self.height() / 2
        left = radius + 2
        right = self.width() - radius - 2
        if right <= left:
            painter.end()
            return

        pen = QPen(theme.qcolor(theme.LINE), 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(left, y), QPointF(right, y))

        filled_to = left + (right - left) * self._progress
        if filled_to > left + 0.5:
            pen = QPen(theme.qcolor(theme.PRIMARY), 2.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(left, y), QPointF(filled_to, y))

        for i in range(self._count):
            t = 0.0 if self._count == 1 else i / (self._count - 1)
            cx = left + (right - left) * t
            reached = self._progress >= t - 0.001
            centre = QPointF(cx, y)
            painter.setPen(QPen(theme.qcolor(theme.PRIMARY if reached else theme.LINE), 1.5))
            painter.setBrush(theme.qcolor(theme.PRIMARY if reached else theme.PAPER))
            painter.drawEllipse(centre, radius, radius)
            draw_text(painter,
                      QRectF(cx - radius, y - radius, radius * 2, radius * 2),
                      str(i + 1), theme.font(9, QFont.Weight.DemiBold),
                      theme.qcolor(theme.CREAM if reached else theme.INK_MUTED),
                      Qt.AlignmentFlag.AlignCenter)
        painter.end()


# --- page transitions ------------------------------------------------------


class FadeStack(QStackedWidget):
    """A stack whose pages cross-fade and slide a little as they swap."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._running: list[QPropertyAnimation] = []

    def go_to(self, index: int, *, forward: bool = True) -> None:
        """Swap to ``index``, fading and sliding the arriving page in.

        The index is set *first* and the new page animated afterwards. Trying
        to animate a page that the stack still considers hidden means fighting
        the stack's own layout, which shows up as a one-frame flash; doing it
        this way the stack has already placed the page and the animation only
        decorates it.
        """
        if index == self.currentIndex() or not (0 <= index < self.count()):
            return
        self.setCurrentIndex(index)
        page = self.currentWidget()
        if page is None:
            return

        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        effect.setOpacity(0.0)

        fade = QPropertyAnimation(effect, b"opacity", self)
        fade.setDuration(theme.SLOW)
        fade.setEasingCurve(theme.EASE_OUT)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)

        home = page.pos()
        shift = 22 if forward else -22
        slide = QPropertyAnimation(page, b"pos", self)
        slide.setDuration(theme.SLOW)
        slide.setEasingCurve(theme.EASE_PANEL)
        slide.setStartValue(QPoint(home.x() + shift, home.y()))
        slide.setEndValue(home)

        def finished() -> None:
            # Dropping the effect matters: a live QGraphicsOpacityEffect makes
            # the page render through an offscreen pixmap for the rest of its
            # life, which costs a repaint on every hover animation underneath.
            page.setGraphicsEffect(None)
            page.move(home)
            self._running.clear()

        fade.finished.connect(finished)
        self._running = [fade, slide]
        fade.start()
        slide.start()


# --- toasts and empty states -----------------------------------------------


class Toast(Surface):
    """A short-lived note in the corner: done, failed, waiting, offline.

    Slides up and fades in, waits, then leaves the same way. Dismissable,
    because the one thing worse than a missed message is one that will not go
    away while she is trying to read the screen behind it.
    """

    SUCCESS = "success"
    ERROR = "error"
    WARN = "warn"
    INFO = "info"

    _LOOK = {
        SUCCESS: ("check_fill", theme.SAGE_BG, theme.SAGE_LINE, "#3C7A41", theme.SAGE_INK),
        ERROR: ("alert", theme.CORAL_BG, theme.CORAL_LINE, "#D1504A", "#8E2F27"),
        WARN: ("alert", "#FBEBD2", "#EDCF9E", "#D08A2C", "#7A5116"),
        INFO: ("info", "#DCEAF4", "#AFCEE2", "#3C7FB1", "#20455C"),
    }

    closed = Signal()

    def __init__(self, title: str, body: str = "", kind: str = INFO,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent, clickable=False, lift=0.0)
        self._title = title
        self._body = body
        self._kind = kind
        self._slide = 0.0
        self.setFixedHeight(58 if body else 44)
        self.setMinimumWidth(300)
        self.setMouseTracking(True)
        self._life = QTimer(self)
        self._life.setSingleShot(True)
        self._life.timeout.connect(self.dismiss)

    def _get_slide(self) -> float:
        return self._slide

    def _set_slide(self, value: float) -> None:
        self._slide = value
        self.update()

    slide = Property(float, _get_slide, _set_slide)

    def present(self, hold_ms: int = 4200) -> None:
        self.show()
        self._slide = 0.0
        animate(self, b"slide", 1.0, theme.SLOW, theme.EASE_POP, store="_anim_slide")
        if hold_ms:
            self._life.start(hold_ms)

    def dismiss(self) -> None:
        self._life.stop()
        anim = animate(self, b"slide", 0.0, theme.MEDIUM, theme.EASE_IN_OUT,
                       store="_anim_slide")
        anim.finished.connect(self._gone)

    def _gone(self) -> None:
        if self._slide <= 0.01:
            self.hide()
            self.closed.emit()

    def _close_rect(self, body: QRectF) -> QRectF:
        return QRectF(body.right() - 32, body.center().y() - 11, 22, 22)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._close_rect(body).adjusted(-6, -6, 6, 6).contains(
            event.position()
        ):
            self.dismiss()
        super().mouseReleaseEvent(event)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        if self._slide <= 0.005:
            return
        icon, fill, border, accent, ink = self._LOOK.get(self._kind, self._LOOK[self.INFO])
        painter.setOpacity(min(1.0, self._slide * 1.3))
        painter.translate(0.0, (1.0 - self._slide) * 16.0)

        body = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        soft_shadow(painter, body, theme.RADIUS_MD, spread=7.0, alpha=0.26)
        painter.setPen(QPen(QColor(border), 1.0))
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(body, theme.RADIUS_MD, theme.RADIUS_MD)

        disc = QRectF(body.left() + 12, body.center().y() - 11, 22, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(accent))
        painter.drawEllipse(disc)
        glyph = "check" if icon == "check_fill" else icon
        icons.paint(painter, glyph, disc.adjusted(5, 5, -5, -5),
                    theme.qcolor("#FFFFFF"), width=2.1)

        left = disc.right() + 12
        width = body.right() - left - 40
        title_font = theme.font(10, QFont.Weight.DemiBold)
        if self._body:
            draw_text(painter, QRectF(left, body.top() + 10, width, 17),
                      elide(self._title, title_font, width), title_font,
                      QColor(ink),
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            draw_text(painter, QRectF(left, body.top() + 28, width, 18),
                      elide(self._body, theme.font(9.4), width), theme.font(9.4),
                      theme.mix(ink, fill, 0.35),
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        else:
            draw_text(painter, QRectF(left, body.top(), width, body.height()),
                      elide(self._title, title_font, width), title_font,
                      QColor(ink),
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        icons.paint(painter, "close", self._close_rect(body),
                    theme.mix(ink, fill, 0.45), width=1.8)


class ToastHost(QWidget):
    """Stacks toasts bottom-up in a corner of the window and tidies up after.

    It is a transparent, click-through-ish layer sized by its parent; only the
    toasts themselves take the mouse.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        # The host covers the whole window, so it must not take the mouse or it
        # would swallow every click on the page beneath. The attribute is
        # per-widget: the toasts themselves are still hit-tested normally.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._toasts: list[Toast] = []

    def post(self, title: str, body: str = "", kind: str = Toast.INFO,
             hold_ms: int = 4200) -> Toast:
        toast = Toast(title, body, kind, self)
        toast.closed.connect(lambda t=toast: self._remove(t))
        self._toasts.append(toast)
        # Three at a time is plenty; older ones leave to make room.
        for old in self._toasts[:-3]:
            old.dismiss()
        self._relayout()
        toast.present(hold_ms)
        return toast

    def _remove(self, toast: Toast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        toast.deleteLater()
        self._relayout()

    def _relayout(self) -> None:
        width = min(360, max(260, self.width() - 32))
        y = self.height() - 16
        for toast in reversed(self._toasts):
            y -= toast.height()
            toast.setFixedWidth(width)
            toast.move(self.width() - width - 16, y)
            toast.raise_()
            y -= 10

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._relayout()
        super().resizeEvent(event)


class EmptyState(QWidget):
    """"Nothing here yet", "I couldn't find it", "You're offline".

    An icon, two lines, and at most one button — the design sheet's version of
    a dead end that still offers a way forward.
    """

    acted = Signal()

    def __init__(self, icon: str, title: str, body: str = "",
                 action: str | None = None, *, tint: str = theme.INK_MUTED,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._body = body
        self._tint = tint
        self._button: Button | None = None
        if action:
            self._button = Button(action, kind=Button.SECONDARY, parent=self)
            self._button.clicked.connect(self.acted.emit)

    def set_message(self, title: str, body: str = "", icon: str | None = None) -> None:
        self._title = title
        self._body = body
        if icon:
            self._icon = icon
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._button is not None:
            self._button.move(
                round((self.width() - self._button.width()) / 2),
                round(self.height() / 2 + 44),
            )
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        centre_y = rect.height() / 2

        icons.paint(painter, self._icon,
                    QRectF(rect.center().x() - 24, centre_y - 92, 48, 48),
                    theme.qcolor(self._tint, 0.65), width=1.6)
        draw_text(painter, QRectF(rect.left() + 24, centre_y - 34,
                                  rect.width() - 48, 26),
                  self._title, theme.display_font(13, QFont.Weight.DemiBold),
                  theme.qcolor(theme.INK), Qt.AlignmentFlag.AlignCenter)
        if self._body:
            draw_text(painter, QRectF(rect.left() + 40, centre_y - 4,
                                      rect.width() - 80, 44),
                      self._body, theme.font(10), theme.qcolor(theme.INK_MUTED),
                      Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter
                      | Qt.AlignmentFlag.AlignTop)
        painter.end()


# --- responsive layout -----------------------------------------------------


class FlowLayout(QLayout):
    """Lays items out in a row, wrapping to the next line when they run out.

    Qt ships no flow layout, and the suggestion chips need one: when the
    walkthrough drawer opens, the page loses ~350 px and a fixed row of chips
    would set a minimum width the page can never go below, which is what turns
    a narrow page into a clipped one rather than a reflowed one.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int = 8,
                 margin: int = 0) -> None:
        super().__init__(parent)
        self._items: list = []
        self._space = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item) -> None:  # noqa: N802 - Qt naming
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 - Qt naming
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802 - Qt naming
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 - Qt naming
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._lay(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:  # noqa: N802 - Qt naming
        super().setGeometry(rect)
        self._lay(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt naming
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _lay(self, rect, *, apply: bool) -> int:
        margins = self.contentsMargins()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        right = rect.right() - margins.right()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > right and line_height > 0:
                x = rect.x() + margins.left()
                y += line_height + self._space
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._space
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()


class CardGrid(QWidget):
    """A grid of equal cards that drops to fewer columns as it narrows.

    Re-gridding only happens when the column count actually changes, so a
    slow drag of the drawer does not relayout on every frame.
    """

    def __init__(self, cards: Sequence[QWidget], *, min_card: int = 200,
                 max_columns: int = 3, spacing: int = 12,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards = list(cards)
        self._min_card = min_card
        self._max_columns = max_columns
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(spacing)
        self._grid.setVerticalSpacing(spacing)
        for card in self._cards:
            card.setParent(self)
        self._regrid(max_columns)

    def _wanted_columns(self, width: int) -> int:
        if width <= 0:
            return self._max_columns
        fits = max(1, (width + 12) // (self._min_card + 12))
        return int(max(1, min(self._max_columns, fits)))

    def _regrid(self, columns: int) -> None:
        if columns == self._columns:
            return
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // columns, i % columns)
        for column in range(self._max_columns):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._regrid(self._wanted_columns(self.width()))
        super().resizeEvent(event)


# --- layout helper ---------------------------------------------------------


def stagger(widgets: Sequence[Surface], *, start_ms: int = 60,
            step_ms: int = theme.STAGGER) -> None:
    """Play each widget's entrance one after another, left to right."""
    for i, widget in enumerate(widgets):
        widget.play_entrance(start_ms + i * step_ms)


def on_click(widget: Surface, handler: Callable[[], None]) -> None:
    widget.clicked.connect(handler)
