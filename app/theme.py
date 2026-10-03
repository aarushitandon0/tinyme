"""Design tokens for Tiny Me's window: colour, type, spacing, motion.

One module so that every widget in :mod:`app.ui` and every stroke in
:mod:`app.overlay` reads the same palette. The values come from the design
sheet (forest green chrome, warm cream paper, sage "Show me how", sky "Do it
for me", coral for anything she must not hand to us).

Nothing here imports a widget, so it is safe to import from anywhere, and the
font lookup is memoised because ``QFontDatabase.families()`` is slow enough to
be felt if it runs once per label.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Sequence

from PySide6.QtCore import QEasingCurve
from PySide6.QtGui import QColor, QFont, QFontDatabase

# --- colour ----------------------------------------------------------------
# Hex strings, because most of them are spent in a stylesheet. ``qcolor()``
# turns one into a QColor for the hand-painted widgets.

#: The chrome: sidebar, title bar, primary buttons.
FOREST_DEEP = "#24351F"
FOREST = "#2E4532"
FOREST_SOFT = "#3B5A3E"
FOREST_HOVER = "#45684A"
PRIMARY = "#3F6B41"
PRIMARY_HOVER = "#4A7C4C"
PRIMARY_PRESSED = "#345A36"

#: The paper: everything she reads sits on one of these.
CREAM = "#FAF6EC"
CREAM_DEEP = "#F3ECDC"
PAPER = "#FFFDF7"
LINE = "#E0D6C0"
LINE_SOFT = "#EBE3D2"

#: Ink.
INK = "#243024"
INK_SOFT = "#4A5A4C"
INK_MUTED = "#7B8A7D"
ON_FOREST = "#F2EEE2"
ON_FOREST_MUTED = "#A9BCA9"

#: "Show me how" — sage. The default, so it is the warmer of the two.
SAGE_BG = "#DCE8D2"
SAGE_BG_HOVER = "#D2E2C6"
SAGE_LINE = "#B3CBA3"
SAGE_INK = "#2E4532"

#: "Do it for me" — sky. Cooler, because it is the one that acts for her.
SKY_BG = "#D8E7F1"
SKY_BG_HOVER = "#CBDFEC"
SKY_LINE = "#A5C7DC"
SKY_INK = "#23414F"

#: Accents.
TAN = "#E2C89B"
TAN_LINE = "#CBAE7C"
BROWN = "#6B4A33"
CORAL = "#E0675C"
CORAL_BG = "#F8E2DE"
CORAL_LINE = "#EEBDB5"

#: The overlay ring, and the small marks that echo it in the window.
HIGHLIGHT = "#E8873A"
HIGHLIGHT_WARM = "#F2A65A"

#: Status.
ONLINE_DOT = "#8FC98F"

# --- shape -----------------------------------------------------------------

RADIUS_SM = 8
RADIUS_MD = 12
RADIUS_LG = 16
RADIUS_XL = 22
RADIUS_PILL = 999

#: The window's own rounded corner, painted by :class:`app.ui.PromptWindow`.
WINDOW_RADIUS = 18
#: Space kept clear around the window for its drop shadow.
SHADOW_MARGIN = 24

GAP_XS = 6
GAP_SM = 10
GAP_MD = 16
GAP_LG = 24
GAP_XL = 32

SIDEBAR_WIDTH = 236
PANEL_WIDTH = 348
TITLEBAR_HEIGHT = 46

# --- motion ----------------------------------------------------------------
# Durations in ms. Everything she can hover is FAST; anything that moves a
# whole region of the window is SLOW, so it reads as one object travelling
# rather than a repaint.

FAST = 130
MEDIUM = 220
SLOW = 320
PANEL = 420
#: Gap between items in a staggered entrance.
STAGGER = 55

EASE_OUT = QEasingCurve.Type.OutCubic
EASE_IN_OUT = QEasingCurve.Type.InOutCubic
#: For things that should feel like they land: chips, cards, the send button.
EASE_POP = QEasingCurve.Type.OutBack
EASE_PANEL = QEasingCurve.Type.OutQuint


# --- type ------------------------------------------------------------------

#: Rounded and friendly if the machine has one, Segoe UI if not. Tiny Me ships
#: no font files, so this is a best-effort walk down a list of things Windows
#: and designers' machines tend to have.
DISPLAY_CANDIDATES = (
    "Baloo 2", "Quicksand", "Comfortaa", "Nunito", "Varela Round",
    "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI",
)
TEXT_CANDIDATES = (
    "Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", "Arial",
)
#: The pencilled notes in the margin ("Built for one special person").
HAND_CANDIDATES = (
    "Ink Free", "Segoe Script", "Segoe Print", "Bradley Hand ITC",
    "Comic Sans MS", "Segoe UI",
)


@lru_cache(maxsize=1)
def _installed() -> frozenset[str]:
    return frozenset(QFontDatabase.families())


def pick_family(candidates: Sequence[str]) -> str:
    """First installed family in ``candidates``, else the last one."""
    installed = _installed()
    for name in candidates:
        if name in installed:
            return name
    return candidates[-1]


@lru_cache(maxsize=1)
def display_family() -> str:
    return pick_family(DISPLAY_CANDIDATES)


@lru_cache(maxsize=1)
def text_family() -> str:
    return pick_family(TEXT_CANDIDATES)


@lru_cache(maxsize=1)
def hand_family() -> str:
    return pick_family(HAND_CANDIDATES)


def font(size: int, weight: QFont.Weight = QFont.Weight.Normal, *,
         family: str | None = None, italic: bool = False,
         letter_spacing: float = 0.0) -> QFont:
    """A QFont in points, with the text family by default."""
    f = QFont(family or text_family())
    f.setPointSizeF(size)
    f.setWeight(weight)
    f.setItalic(italic)
    if letter_spacing:
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + letter_spacing)
    return f


def display_font(size: int, weight: QFont.Weight = QFont.Weight.Bold) -> QFont:
    return font(size, weight, family=display_family())


def hand_font(size: int) -> QFont:
    return font(size, QFont.Weight.Normal, family=hand_family())


# --- helpers ---------------------------------------------------------------

def qcolor(hex_or_color: str | QColor, alpha: float = 1.0) -> QColor:
    """``qcolor(SAGE_LINE, 0.4)`` — a token with an opacity applied."""
    colour = QColor(hex_or_color) if isinstance(hex_or_color, str) else QColor(hex_or_color)
    if alpha < 1.0:
        colour.setAlphaF(max(0.0, min(1.0, alpha)))
    return colour


def mix(a: str | QColor, b: str | QColor, t: float) -> QColor:
    """Blend two colours. ``t=0`` is ``a``, ``t=1`` is ``b``."""
    ca, cb = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
        round(ca.alpha() + (cb.alpha() - ca.alpha()) * t),
    )


def stylesheet() -> str:
    """The app-wide sheet.

    Only the things Qt styles well live here — scrollbars, the plain text
    widgets, tooltips. Every card, chip and nav item paints itself in
    :mod:`app.widgets`, because they animate, and animating a stylesheet means
    re-parsing it sixty times a second.
    """
    return f"""
    QWidget {{
        color: {INK};
        font-family: "{text_family()}";
        font-size: 10.5pt;
    }}
    QLabel {{ background: transparent; }}

    QLineEdit {{
        background: transparent;
        border: none;
        color: {INK};
        selection-background-color: {SAGE_LINE};
        selection-color: {INK};
    }}

    QToolTip {{
        background: {PAPER};
        color: {INK};
        border: 1px solid {LINE};
        border-radius: {RADIUS_SM}px;
        padding: 6px 10px;
    }}

    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}

    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 4px 2px 4px 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {LINE};
        border-radius: 4px;
        min-height: 36px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {TAN_LINE}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
    }}
    QScrollBar:horizontal {{ height: 0px; }}
    """
