"""Her way in: the Tiny Me window (MASTERSPEC 5.6).

A separate, normal, *clickable* window — the overlay is the click-through one.
What is here is the home screen from the design sheet: forest-green chrome, a
cream page, one prompt bar, the two choices the spec locks us to ("Show me
how", "Do it for me"), examples she can press instead of typing, and a
walkthrough drawer that opens from the right while a task is running.

The contract :mod:`app.main` depends on is small and unchanged:

* signals ``submitted``, ``find_requested``, ``do_requested``, ``did_it``,
  ``stopped``
* methods ``ask``, ``set_status``, ``set_busy``, ``set_task_active``

Everything else on :class:`PromptWindow` (``begin_task``, ``push_step``,
``finish_task``, ``toast``) is optional polish that the loop calls when it has
something to say; none of it changes what the loop does.

CLAUDE.md rule 8: every line in this file runs on the Qt main thread.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
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
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app import config, icons, theme
from app.widgets import (
    ActionCard,
    Banner,
    Button,
    Card,
    CardGrid,
    Chip,
    EmptyState,
    ExampleCard,
    FadeLabel,
    FadeStack,
    FlowLayout,
    NavItem,
    ProgressTrack,
    PromptBar,
    StatusPill,
    StepRow,
    Surface,
    ThinkingDots,
    Toast,
    ToastHost,
    Toggle,
    TrustBadge,
    animate,
    draw_text,
    soft_shadow,
    stagger,
    text_height,
)

log = logging.getLogger(__name__)

#: The examples on the home page. Each is (what she sees, icon, which button
#: it should press). "find" goes straight to "Find it for me" because that one
#: needs no guiding; everything else is a guided task.
EXAMPLES: list[tuple[str, str, str]] = [
    ("Find the file I downloaded", "folder", "find"),
    ("Book a train ticket", "train", "do"),
    ("Open my college portal", "globe", "guide"),
    ("Check my email", "mail", "guide"),
    ("Open YouTube", "video", "guide"),
    ("Make this text bigger", "doc", "guide"),
]

#: The chips under the prompt bar. Pressing one fills the box; it does not
#: submit, so she always sees what she is about to ask for.
SUGGESTIONS = [
    "I can't find the file I downloaded",
    "Book a train ticket",
    "Open my college portal",
    "Check my email",
]

TRUST = [
    ("Your data stays on your laptop", "Works offline", "lock", theme.BROWN),
    ("Safe by design", "I stop for passwords, OTPs and payments.", "shield", theme.PRIMARY),
    ("Made for one special person", "(my mom)", "heart", theme.CORAL),
]


# --- chrome ----------------------------------------------------------------


class WindowButton(Surface):
    """Minimise, maximise, close. Close goes red on hover, the rest go pale."""

    def __init__(self, glyph: str, *, danger: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent, lift=0.0)
        self._glyph = glyph
        self._danger = danger
        self.setFixedSize(QSize(42, theme.TITLEBAR_HEIGHT))

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        h = self._hover
        if h > 0.01:
            painter.setPen(Qt.PenStyle.NoPen)
            fill = theme.qcolor(theme.CORAL if self._danger else "#FFFFFF",
                                (0.85 if self._danger else 0.12) * h)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect.adjusted(5, 8, -5, -8), 8, 8)
        ink = theme.mix(theme.ON_FOREST_MUTED,
                        "#FFFFFF" if self._danger else theme.ON_FOREST, h)
        icons.paint(painter, self._glyph, rect.adjusted(14, 15, -14, -15),
                    ink, width=1.7)


class TitleBar(QWidget):
    """The green strip: Tiny Me's mark and name, and the window buttons.

    Also the drag handle. Qt gives a frameless window no way to be moved, so
    the press/move pair here is the whole of it; double-click maximises, which
    is the one habit people bring from every other window.
    """

    minimise = Signal()
    maximise = Signal()
    close_window = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(theme.TITLEBAR_HEIGHT)
        self._drag_from: QPoint | None = None

        self._min = WindowButton("minimise", parent=self)
        self._max = WindowButton("maximise", parent=self)
        self._close = WindowButton("close", danger=True, parent=self)
        self._min.clicked.connect(self.minimise.emit)
        self._max.clicked.connect(self.maximise.emit)
        self._close.clicked.connect(self.close_window.emit)
        self._min.setToolTip("Minimise")
        self._max.setToolTip("Maximise")
        self._close.setToolTip("Close (Tiny Me stays on your hotkey)")

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addStretch(1)
        row.addWidget(self._min)
        row.addWidget(self._max)
        row.addWidget(self._close)

    # -- dragging --

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            self._drag_from = (
                event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_from is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_from)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._drag_from = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximise.emit()
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())

        mark = QRectF(16, rect.center().y() - 15, 30, 30)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor("#FFFFFF", 0.10))
        painter.drawRoundedRect(mark, 9, 9)
        icons.paint(painter, "leaf", mark.adjusted(5, 5, -5, -5),
                    theme.qcolor(theme.ONLINE_DOT), width=1.6)

        draw_text(painter, QRectF(mark.right() + 11, rect.top() + 7, 240, 18),
                  "Tiny Me", theme.display_font(12, QFont.Weight.DemiBold),
                  theme.qcolor(theme.ON_FOREST),
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        draw_text(painter, QRectF(mark.right() + 11, rect.top() + 24, 300, 15),
                  "Your personal computer sidekick", theme.font(8.6),
                  theme.qcolor(theme.ON_FOREST_MUTED),
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.end()


class Sidebar(QWidget):
    """Navigation, and the two promises that should never leave the screen.

    The selection is one pill that *travels* between rows rather than four that
    switch on and off; moving a single object is what makes the change
    legible rather than a flicker.
    """

    navigated = Signal(int)

    ITEMS = [
        ("Home", "home"),
        ("History", "clock"),
        ("Examples", "grid"),
        ("Settings", "gear"),
        ("About", "info"),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(theme.SIDEBAR_WIDTH)
        self._pill_y = 0.0
        self._current = 0

        self._items: list[NavItem] = []
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 14, 12, 16)
        column.setSpacing(2)

        for index, (label, icon) in enumerate(self.ITEMS):
            item = NavItem(label, icon, self)
            item.clicked.connect(lambda i=index: self.select(i))
            column.addWidget(item)
            self._items.append(item)

        column.addStretch(1)
        self._note = QLabel(
            "A calmer computer\nexperience for the\npeople we love.  ♡", self
        )
        self._note.setFont(theme.hand_font(11))
        self._note.setStyleSheet(f"color: {theme.ON_FOREST_MUTED};")
        self._note.setAlignment(Qt.AlignmentFlag.AlignLeft)
        column.addWidget(self._note)
        column.addSpacing(14)

        self._status = StatusPill("Works offline",
                                  "Everything stays on your laptop.", self)
        column.addWidget(self._status)

        self._items[0]._set_selected(1.0)

    # -- the travelling pill --

    def _get_pill_y(self) -> float:
        return self._pill_y

    def _set_pill_y(self, value: float) -> None:
        self._pill_y = value
        self.update()

    pill_y = Property(float, _get_pill_y, _set_pill_y)

    @property
    def current(self) -> int:
        return self._current

    def select(self, index: int, *, announce: bool = True) -> None:
        if not (0 <= index < len(self._items)):
            return
        previous = self._current
        self._current = index
        for i, item in enumerate(self._items):
            animate(item, b"selected", 1.0 if i == index else 0.0,
                    theme.MEDIUM, theme.EASE_OUT, store="_anim_selected")
        target = float(self._items[index].y())
        if previous == index or self._pill_y == 0.0:
            self._set_pill_y(target)
        else:
            animate(self, b"pill_y", target, theme.MEDIUM + 60, theme.EASE_PANEL)
        if announce:
            self.navigated.emit(index)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Geometry is only real once laid out, so the pill takes its first
        # position here rather than in __init__.
        self._set_pill_y(float(self._items[self._current].y()))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Only the pill.

        The green itself is painted by :class:`PromptWindow`, inside the
        window's rounded clip path. Filling it here instead would square off
        the bottom-left corner, because a child widget is not clipped by its
        parent's path.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self._items:
            item = self._items[self._current]
            pill = QRectF(item.x(), self._pill_y + 1,
                          item.width(), item.height() - 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.PRIMARY))
            painter.drawRoundedRect(pill, theme.RADIUS_MD, theme.RADIUS_MD)
            # A warm edge on the left, the same orange as the overlay ring, so
            # "where you are" and "where to look" speak the same language.
            marker = QRectF(pill.left() + 3, pill.center().y() - 9, 3, 18)
            painter.setBrush(QColor(theme.HIGHLIGHT))
            painter.drawRoundedRect(marker, 1.5, 1.5)
        painter.end()


# --- home page -------------------------------------------------------------


class Hero(QWidget):
    """"Hi! I'm Tiny Me." — with the underline drawing itself in.

    The underline is a hand-drawn swash clipped from the left as ``sweep`` runs
    0 to 1, which reads as a pen stroke rather than a bar appearing.
    """

    #: The widest the subtitle is allowed to get. Past this it reads as a
    #: paragraph rather than a greeting.
    SUB_WIDTH = 440.0
    SUB_TEXT = ("Tell me what you want to do, and I'll show you how to do it "
                "or do it for you.")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sweep = 0.0
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _get_sweep(self) -> float:
        return self._sweep

    def _set_sweep(self, value: float) -> None:
        self._sweep = value
        self.update()

    sweep = Property(float, _get_sweep, _set_sweep)

    def play(self) -> None:
        self._set_sweep(0.0)
        QTimer.singleShot(
            260, lambda: animate(self, b"sweep", 1.0, 620, QEasingCurve.Type.OutQuart)
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        rect = QRectF(self.rect())

        # The decorative sprout sits on the right and is the first thing to be
        # given up when the window is narrow.
        art_width = 150.0
        has_art = rect.width() > 620
        text_width = rect.width() - (art_width if has_art else 0) - 8

        hi = theme.display_font(30, QFont.Weight.Bold)
        hi_rect = draw_text(painter, QRectF(0, 4, text_width, 42), "Hi!", hi,
                            theme.qcolor(theme.INK),
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        name = theme.display_font(30, QFont.Weight.Bold)
        name_rect = draw_text(painter, QRectF(0, hi_rect.bottom() + 2, text_width, 46),
                              "I'm Tiny Me.", name, theme.qcolor(theme.INK),
                              Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._draw_underline(painter, name_rect)

        # Measure before drawing. A hard-coded height here silently clips the
        # second line at 125% and 150% display scaling, where a line of 11.5pt
        # text is a third taller than it is at 100%.
        sub = theme.font(11.5)
        sub_width = min(self.SUB_WIDTH, max(220.0, text_width))
        sub_top = name_rect.bottom() + 14
        sub_height = text_height(self.SUB_TEXT, sub, sub_width) + 4
        draw_text(painter, QRectF(0, sub_top, sub_width, sub_height),
                  self.SUB_TEXT, sub, theme.qcolor(theme.INK_SOFT),
                  Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop)

        wanted = round(sub_top + sub_height + 6)
        if wanted > self.minimumHeight():
            self.setMinimumHeight(wanted)

        if has_art:
            self._draw_art(painter, QRectF(rect.right() - art_width, 6,
                                           art_width, rect.height() - 12))
        painter.end()

    def _draw_underline(self, painter: QPainter, under: QRectF) -> None:
        """A two-stroke swash under "Tiny Me", plus the little flick marks."""
        if self._sweep <= 0.0:
            return
        left = under.left() + under.width() * 0.30
        right = under.right() - 4
        y = under.bottom() - 3

        painter.save()
        painter.setClipRect(QRectF(left - 6, y - 24,
                                   (right - left + 30) * self._sweep, 48))

        stroke = QPainterPath()
        stroke.moveTo(left, y)
        stroke.cubicTo(left + (right - left) * 0.35, y + 6,
                       left + (right - left) * 0.7, y - 4, right, y + 2)
        pen = QPen(theme.qcolor(theme.HIGHLIGHT), 3.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(stroke)

        # Two quick flicks off the end, the way someone underlines by hand.
        pen.setWidthF(2.6)
        pen.setColor(theme.qcolor(theme.HIGHLIGHT_WARM))
        painter.setPen(pen)
        painter.drawLine(QPointF(right + 6, y - 9), QPointF(right + 17, y - 14))
        painter.drawLine(QPointF(right + 7, y - 1), QPointF(right + 19, y - 2))
        painter.restore()

    def _draw_art(self, painter: QPainter, box: QRectF) -> None:
        """A sage blob with a sprout in it, and a pencilled aside."""
        blob = QRectF(box.left() + 10, box.top() + 6, 118, 96)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(theme.SAGE_BG, 0.75))
        painter.drawRoundedRect(blob, 34, 30)
        painter.setBrush(theme.qcolor(theme.TAN, 0.35))
        painter.drawRoundedRect(blob.adjusted(16, 54, -18, -8), 14, 12)
        icons.paint(painter, "leaf",
                    QRectF(blob.center().x() - 24, blob.top() + 12, 48, 48),
                    theme.qcolor(theme.PRIMARY), width=1.7)
        icons.paint(painter, "sparkle",
                    QRectF(blob.right() - 20, blob.top() + 4, 16, 16),
                    theme.qcolor(theme.HIGHLIGHT_WARM), width=1.4)

        draw_text(painter, QRectF(box.left() - 4, blob.bottom() + 6,
                                  box.width(), 34),
                  "same laptop,\nnew confidence ♡", theme.hand_font(10.5),
                  theme.qcolor(theme.INK_MUTED),
                  Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)


class SectionTitle(QWidget):
    """A small label with a hairline running off to the right."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self.setFixedHeight(26)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        font = theme.font(10.5, QFont.Weight.DemiBold)
        bounds = draw_text(painter, rect, self._text, font,
                           theme.qcolor(theme.INK_SOFT),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.setPen(QPen(theme.qcolor(theme.LINE), 1.0))
        y = rect.center().y()
        painter.drawLine(QPointF(bounds.right() + 12, y),
                         QPointF(rect.right(), y))
        painter.end()


class HomePage(QWidget):
    """Everything she sees before she has asked for anything."""

    guide_requested = Signal(str)
    do_requested = Signal(str)
    find_requested = Signal(str)
    examples_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.hero = Hero(self)
        self.prompt = PromptBar(self)
        self.prompt.submitted.connect(self.guide_requested.emit)

        self.banner = Banner(self)

        # A flow layout, not a row: when the walkthrough drawer opens the page
        # loses ~350 px, and a fixed row would pin the page to a minimum width
        # it cannot go below -- which clips the examples instead of reflowing
        # them.
        self.chips: list[Chip] = []
        self._chip_host = QWidget(self)
        chip_flow = FlowLayout(self._chip_host, spacing=8)
        for text in SUGGESTIONS:
            chip = Chip(f"“{text}”", self._chip_host)
            chip.clicked.connect(lambda t=text: self.prompt.set_text(t))
            chip_flow.addWidget(chip)
            self.chips.append(chip)

        self.show_card = ActionCard(
            "Show me how", "I'll guide you step by step with highlights.",
            "cursor", fill=theme.SAGE_BG, fill_hover=theme.SAGE_BG_HOVER,
            border=theme.SAGE_LINE, ink=theme.SAGE_INK, parent=self,
        )
        self.do_card = ActionCard(
            "Do it for me", "I'll do it for you while you watch.",
            "play", fill=theme.SKY_BG, fill_hover=theme.SKY_BG_HOVER,
            border=theme.SKY_LINE, ink=theme.SKY_INK, parent=self,
        )
        self.show_card.clicked.connect(self._guide)
        self.do_card.clicked.connect(self._do)

        cards = QHBoxLayout()
        cards.setSpacing(16)
        cards.addWidget(self.show_card, 1)
        cards.addWidget(self.do_card, 1)

        self.examples: list[ExampleCard] = []
        for text, icon, mode in EXAMPLES:
            card = ExampleCard(text, icon, self)
            card.clicked.connect(lambda t=text, m=mode: self._example(t, m))
            self.examples.append(card)
        self.example_grid = CardGrid(self.examples, min_card=190, max_columns=3,
                                     parent=self)

        self.badges: list[TrustBadge] = []
        for title, body, icon, tint in TRUST:
            self.badges.append(TrustBadge(title, body, icon, tint, self))
        self.badge_grid = CardGrid(self.badges, min_card=215, max_columns=3,
                                   parent=self)

        # The rhythm is tuned so the whole page -- down to the three promises
        # at the bottom -- fits without scrolling at the default window size.
        # It still scrolls on a shorter screen; it just should not have to.
        column = QVBoxLayout(self)
        column.setContentsMargins(theme.GAP_XL, 20, theme.GAP_XL, 18)
        column.setSpacing(0)
        column.addWidget(self.hero)
        column.addSpacing(8)
        column.addWidget(self.prompt)
        column.addSpacing(10)
        column.addWidget(self._chip_host)
        column.addSpacing(4)
        column.addWidget(self.banner)
        column.addSpacing(10)
        column.addLayout(cards)
        column.addSpacing(16)
        column.addWidget(SectionTitle("Try these examples", self))
        column.addSpacing(6)
        column.addWidget(self.example_grid)
        column.addSpacing(14)
        column.addWidget(self.badge_grid)
        column.addStretch(1)

    # -- what the controls mean --

    def _guide(self) -> None:
        text = self.prompt.text()
        if text:
            self.guide_requested.emit(text)
        else:
            self.nudge("Tell me what you'd like to do, then press this.")

    def _do(self) -> None:
        text = self.prompt.text()
        if text:
            self.do_requested.emit(text)
        else:
            self.nudge("Type what you'd like me to do first, like "
                       "“book a train ticket”.")

    def _example(self, text: str, mode: str) -> None:
        self.prompt.set_text(text)
        if mode == "find":
            self.find_requested.emit(text)
        elif mode == "do":
            self.do_requested.emit(text)
        else:
            self.guide_requested.emit(text)

    def nudge(self, message: str) -> None:
        """Say what is missing, and point at the box by focusing it."""
        self.banner.show_message(message, Banner.NOTE)
        self.prompt.focus()
        QTimer.singleShot(4200, self.banner.dismiss)

    def play_entrance(self) -> None:
        self.hero.play()
        stagger(self.chips, start_ms=180, step_ms=45)
        stagger([self.show_card, self.do_card], start_ms=120, step_ms=80)
        stagger(self.examples, start_ms=280, step_ms=40)
        stagger(self.badges, start_ms=460, step_ms=55)


# --- the other pages -------------------------------------------------------


class HistoryPage(QWidget):
    """What she has asked for this session. Empty until she asks something."""

    repeat_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[ExampleCard] = []

        self.empty = EmptyState(
            "clock", "Nothing here yet",
            "The things you ask me to do will show up here, "
            "so you can run one again without typing it.",
            parent=self,
        )

        self._list_host = QWidget(self)
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(10)
        self._list.addStretch(1)
        self._list_host.hide()

        column = QVBoxLayout(self)
        column.setContentsMargins(theme.GAP_XL, theme.GAP_LG, theme.GAP_XL, theme.GAP_LG)
        column.setSpacing(12)
        column.addWidget(SectionTitle("Recent", self))
        column.addWidget(self.empty, 1)
        column.addWidget(self._list_host, 1)

    def add(self, goal: str) -> None:
        if not goal.strip():
            return
        for row in self._rows:
            if row.toolTip() == goal:
                return
        self.empty.hide()
        self._list_host.show()
        card = ExampleCard(goal, "clock", self._list_host)
        card.clicked.connect(lambda g=goal: self.repeat_requested.emit(g))
        self._list.insertWidget(0, card)
        self._rows.insert(0, card)
        card.play_entrance(0)
        # Keep it short: this is a memory aid, not an archive.
        while len(self._rows) > 12:
            old = self._rows.pop()
            self._list.removeWidget(old)
            old.deleteLater()


class ExamplesPage(QWidget):
    """The full list, grouped, for when she wants to browse rather than type."""

    chosen = Signal(str, str)

    GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
        ("Files on your laptop", [
            ("Find the file I downloaded", "folder", "find"),
            ("Open my Documents folder", "folder", "guide"),
            ("Make this text bigger", "doc", "guide"),
        ]),
        ("Out on the web", [
            ("Book a train ticket", "train", "do"),
            ("Open my college portal", "globe", "guide"),
            ("Open YouTube", "video", "guide"),
            ("Check my email", "mail", "guide"),
        ]),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cards: list[ExampleCard] = []

        column = QVBoxLayout(self)
        column.setContentsMargins(theme.GAP_XL, theme.GAP_LG, theme.GAP_XL, theme.GAP_LG)
        column.setSpacing(10)

        for title, items in self.GROUPS:
            column.addWidget(SectionTitle(title, self))
            group: list[ExampleCard] = []
            for text, icon, mode in items:
                card = ExampleCard(text, icon, self)
                card.clicked.connect(lambda t=text, m=mode: self.chosen.emit(t, m))
                group.append(card)
                self.cards.append(card)
            column.addWidget(CardGrid(group, min_card=190, max_columns=3,
                                      parent=self))
            column.addSpacing(10)
        column.addStretch(1)

    def play_entrance(self) -> None:
        stagger(self.cards, start_ms=60, step_ms=38)


class SettingRow(Card):
    """One line of Settings: icon, label, and whatever sits on the right."""

    def __init__(self, icon: str, title: str, body: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent, fill=theme.PAPER, border=theme.LINE_SOFT,
                         radius=theme.RADIUS_MD, clickable=False, lift=0.0,
                         shadow=0.4)
        self._icon = icon
        self._title = title
        self._body = body
        self.setFixedHeight(58)
        self._right: QWidget | None = None

    def set_right(self, widget: QWidget) -> None:
        self._right = widget
        widget.setParent(self)
        widget.show()
        self._place()

    def _place(self) -> None:
        if self._right is not None:
            self._right.move(self.width() - self._right.width() - 16,
                             round((self.height() - self._right.height()) / 2))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._place()
        super().resizeEvent(event)

    def paint_content(self, painter: QPainter, rect: QRectF) -> None:
        body = self.paint_panel(painter, rect)
        icon_box = QRectF(body.left() + 14, body.center().y() - 11, 22, 22)
        icons.paint(painter, self._icon, icon_box, theme.qcolor(theme.INK_SOFT),
                    width=1.7)
        left = icon_box.right() + 12
        width = body.width() - (left - body.left()) - 150
        if self._body:
            draw_text(painter, QRectF(left, body.top() + 10, width, 18),
                      self._title, theme.font(10.4, QFont.Weight.DemiBold),
                      theme.qcolor(theme.INK),
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            draw_text(painter, QRectF(left, body.top() + 28, width, 18),
                      self._body, theme.font(9.2),
                      theme.qcolor(theme.INK_MUTED),
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        else:
            draw_text(painter, QRectF(left, body.top(), width, body.height()),
                      self._title, theme.font(10.4),
                      theme.qcolor(theme.INK),
                      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


class ValueTag(QWidget):
    """A read-only value on the right of a settings row (a hotkey, a model)."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        font = theme.font(9.6, QFont.Weight.DemiBold)
        self.setFixedSize(QSize(round(QFontMetricsF(font).horizontalAdvance(text)) + 26,
                                28))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(theme.qcolor(theme.LINE), 1.0))
        painter.setBrush(theme.qcolor(theme.CREAM_DEEP))
        painter.drawRoundedRect(body, 8, 8)
        draw_text(painter, body, self._text,
                  theme.font(9.6, QFont.Weight.DemiBold),
                  theme.qcolor(theme.INK_SOFT), Qt.AlignmentFlag.AlignCenter)
        painter.end()


class SettingsPage(QWidget):
    """What Tiny Me is actually set to right now.

    Everything on the left of a row is read out of :mod:`app.config`, so this
    page cannot drift from the running app. The two switches are the only
    controls, and they do what they say.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(theme.GAP_XL, theme.GAP_LG, theme.GAP_XL, theme.GAP_LG)
        column.setSpacing(10)

        column.addWidget(SectionTitle("Shortcuts", self))
        open_row = SettingRow("sparkle", "Open Tiny Me",
                              "Works from any app, any time.", self)
        open_row.set_right(ValueTag("Ctrl + Alt + H", open_row))
        column.addWidget(open_row)

        stop_row = SettingRow("alert", "Stop everything",
                              "Takes the circle down and stops looking.", self)
        stop_row.set_right(ValueTag("Ctrl + Alt + P", stop_row))
        column.addWidget(stop_row)

        column.addSpacing(10)
        column.addWidget(SectionTitle("Privacy", self))

        offline_row = SettingRow("wifi_off", "Runs on your laptop",
                                 "No screenshot or word ever leaves it.", self)
        offline_row.set_right(ValueTag("Always on", offline_row))
        column.addWidget(offline_row)

        # Shown, not offered. Telemetry is read from TINYME_TELEMETRY at
        # import (CLAUDE.md rule 7), so a switch she could flip here would be
        # a switch wired to nothing -- it reports the real state and is
        # deliberately not clickable.
        self.telemetry = Toggle(config.TELEMETRY_ENABLED, self)
        self.telemetry.setEnabled(False)
        self.telemetry.setToolTip("Set with the TINYME_TELEMETRY environment "
                                  "variable before Tiny Me starts.")
        telemetry_row = SettingRow(
            "shield", "Developer diagnostics",
            "Numbers only, never text or pictures. Set by TINYME_TELEMETRY.",
            self)
        telemetry_row.set_right(self.telemetry)
        column.addWidget(telemetry_row)

        column.addSpacing(10)
        column.addWidget(SectionTitle("The brain", self))
        model_row = SettingRow("sparkle", "Model",
                               "Runs locally through Ollama.", self)
        model_row.set_right(ValueTag(config.MODEL, model_row))
        column.addWidget(model_row)

        language_row = SettingRow("doc", "I explain things in", "", self)
        language_row.set_right(ValueTag(config.LANGUAGE, language_row))
        column.addWidget(language_row)

        column.addStretch(1)


class AboutPage(QWidget):
    """Who this is for, and what it will not do."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(theme.GAP_XL, theme.GAP_LG, theme.GAP_XL, theme.GAP_LG)
        column.setSpacing(12)

        column.addWidget(SectionTitle("About Tiny Me", self))

        blurb = QLabel(
            "Tiny Me watches your screen only while you have asked it to, "
            "draws a circle around the one thing to click, and waits for you. "
            "It never sends anything anywhere.",
            self,
        )
        blurb.setWordWrap(True)
        blurb.setFont(theme.font(11))
        blurb.setStyleSheet(f"color: {theme.INK_SOFT};")
        column.addWidget(blurb)

        column.addSpacing(6)
        column.addWidget(SectionTitle("Where I always stop", self))
        for text in (
            "Passwords and PINs — you type them, I look away.",
            "One-time codes and CVVs — the same.",
            "Paying, deleting, and sending — I hand the mouse back.",
        ):
            row = SettingRow("lock", text, "", self)
            column.addWidget(row)

        column.addSpacing(10)
        note = QLabel("Built for one special person.  (my mom)  ♡", self)
        note.setFont(theme.hand_font(13))
        note.setStyleSheet(f"color: {theme.INK_MUTED};")
        column.addWidget(note)
        column.addStretch(1)


# --- the walkthrough drawer ------------------------------------------------


class StepPanel(QWidget):
    """The right-hand drawer: what Tiny Me is doing, step by step.

    It opens when a task starts and closes when one ends. The steps arrive one
    at a time from the loop, so this never pretends to know how many there will
    be: the rail re-scales as they come.
    """

    did_it = Signal()
    stopped = Signal()
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(theme.PANEL_WIDTH)

        self._rows: list[StepRow] = []
        self._goal = ""

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel("Tiny Me is helping you…", self)
        self._title.setFont(theme.font(11, QFont.Weight.DemiBold))
        self._title.setStyleSheet(f"color: {theme.INK};")
        self.dots = ThinkingDots(self)
        self.dots.hide()
        close = Button(kind=Button.GHOST, icon="close", parent=self,
                       width=32, height=32)
        close.clicked.connect(self.closed.emit)
        close.setToolTip("Hide this panel")
        header.addWidget(self._title)
        header.addSpacing(6)
        header.addWidget(self.dots)
        header.addStretch(1)
        header.addWidget(close)

        self.goal_card = Card(self, fill=theme.CREAM_DEEP, border=theme.LINE,
                              radius=theme.RADIUS_MD, clickable=False,
                              lift=0.0, shadow=0.0)
        self.goal_card.setFixedHeight(46)
        self._goal_label = FadeLabel(
            "", font=theme.font(10.4, QFont.Weight.DemiBold),
            colour=theme.INK_SOFT,
            align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            parent=self.goal_card,
        )

        self.track = ProgressTrack(self)

        self._steps_host = QWidget(self)
        self._steps = QVBoxLayout(self._steps_host)
        self._steps.setContentsMargins(0, 0, 0, 0)
        self._steps.setSpacing(6)
        self._steps.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._steps_host)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll = scroll

        self.note = Banner(self)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.stop_button = Button("Stop", kind=Button.DANGER, parent=self)
        self.done_button = Button("I did it", kind=Button.PRIMARY,
                                  icon="check", parent=self)
        self.stop_button.clicked.connect(self.stopped.emit)
        self.done_button.clicked.connect(self.did_it.emit)
        self.done_button.setToolTip(
            "Already done it? Press this and I'll move on."
        )
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        buttons.addWidget(self.done_button)

        self.safety = Card(self, fill=theme.CREAM_DEEP, border=theme.LINE_SOFT,
                           radius=theme.RADIUS_MD, clickable=False, lift=0.0,
                           shadow=0.0)
        self.safety.setMinimumHeight(60)
        self._safety_text = QLabel(
            "For passwords, OTPs, payments, deleting or sending, "
            "I'll stop and let you do it.", self.safety)
        self._safety_text.setWordWrap(True)
        self._safety_text.setFont(theme.font(9.2))
        self._safety_text.setStyleSheet(f"color: {theme.INK_MUTED};")

        column = QVBoxLayout(self)
        column.setContentsMargins(18, 16, 18, 16)
        column.setSpacing(10)
        column.addLayout(header)
        column.addWidget(self.goal_card)
        column.addWidget(self.track)
        column.addWidget(scroll, 1)
        column.addWidget(self.note)
        column.addLayout(buttons)
        column.addWidget(self.safety)

    # -- layout for the hand-placed children --

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._goal_label.setGeometry(14, 0, self.goal_card.width() - 28,
                                     self.goal_card.height())
        self._safety_text.setGeometry(40, 10, self.safety.width() - 54,
                                      self.safety.height() - 20)
        super().resizeEvent(event)

    # -- the task --

    def begin(self, goal: str) -> None:
        self.clear()
        self._goal = goal
        self._goal_label.set_text(f"“{goal}”" if goal else "Working…")
        self.track.set_count(3)
        self.track.set_current(0)
        self.dots.start()
        self.stop_button.setEnabled(True)
        self.done_button.setEnabled(True)

    def clear(self) -> None:
        for row in self._rows:
            self._steps.removeWidget(row)
            row.deleteLater()
        self._rows = []
        self.note.dismiss()

    def push_step(self, index: int, title: str, body: str = "") -> None:
        """A new step arrived. Everything before it is, by definition, done."""
        for row in self._rows:
            row.set_state(StepRow.DONE)

        existing = next((r for r in self._rows if r._index == index), None)
        if existing is not None:
            existing._title = title
            existing.set_body(body)
            existing.set_state(StepRow.ACTIVE)
        else:
            row = StepRow(index, title, body, self._steps_host)
            self._steps.insertWidget(len(self._rows), row)
            self._rows.append(row)
            row.play_entrance(40)
            row.set_state(StepRow.ACTIVE)

        self.track.set_count(max(3, len(self._rows)))
        self.track.set_current(index)
        self.dots.start()
        QTimer.singleShot(60, self._scroll_to_bottom)

    def set_detail(self, text: str) -> None:
        """Extra words for the step she is on (the 20 s rephrase, mostly)."""
        if self._rows:
            self._rows[-1].set_body(text)

    def say(self, text: str, kind: str = Banner.NOTE) -> None:
        self.note.show_message(text, kind)

    def finish(self, message: str, ok: bool = True) -> None:
        for row in self._rows:
            row.set_state(StepRow.DONE)
        self.track.set_current(max(len(self._rows), 3))
        self.dots.stop()
        self.stop_button.setEnabled(False)
        self.done_button.setEnabled(False)
        if message:
            self.note.show_message(message, Banner.GOOD if ok else Banner.ERROR)

    def set_busy(self, busy: bool) -> None:
        if busy:
            self.dots.start()
        else:
            self.dots.stop()

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        anim = QPropertyAnimation(bar, b"value", self)
        anim.setDuration(theme.SLOW)
        anim.setEasingCurve(theme.EASE_OUT)
        anim.setStartValue(bar.value())
        anim.setEndValue(bar.maximum())
        self._scroll_anim = anim
        anim.start()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        # Deliberately no fill: the window's cream is already underneath, and
        # painting it here would square off the bottom-right corner.
        painter.setPen(QPen(theme.qcolor(theme.LINE), 1.0))
        painter.drawLine(QPointF(0.5, 0), QPointF(0.5, rect.height()))
        icons.paint(painter, "shield",
                    QRectF(self.safety.x() + 12,
                           self.safety.y() + self.safety.height() / 2 - 11, 22, 22),
                    theme.qcolor(theme.PRIMARY, 0.8), width=1.7)
        painter.end()


def scrollable(page: QWidget, parent: QWidget | None = None) -> QScrollArea:
    """Wrap a page so a short screen scrolls rather than clipping.

    Her laptop is the small one in this story — the home page is designed for
    790 px of height and has to survive 650.
    """
    area = QScrollArea(parent)
    area.setWidget(page)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.viewport().setAutoFillBackground(False)
    return area


class Drawer(QWidget):
    """Clips the panel so it can slide in from the right edge.

    The width is animated rather than the position, so the page beside it
    reflows as the drawer opens instead of being covered — on a 1180 px window
    covering the examples would hide the thing she was about to click.
    """

    #: Emitted on every frame of the open/close animation, so whatever is
    #: anchored to the page beside it can keep up.
    width_changed = Signal(int)

    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._content = content
        content.setParent(self)
        content.move(0, 0)
        self.setFixedWidth(0)
        self.hide()
        self._open = False
        self._anim: QPropertyAnimation | None = None

    @property
    def is_open(self) -> bool:
        return self._open

    def set_open(self, opened: bool) -> None:
        if opened == self._open:
            return
        self._open = opened
        if opened:
            self.show()
        anim = QPropertyAnimation(self, b"maximumWidth", self)
        anim.setDuration(theme.PANEL)
        anim.setEasingCurve(theme.EASE_PANEL)
        anim.setStartValue(self.width())
        anim.setEndValue(theme.PANEL_WIDTH if opened else 0)
        anim.valueChanged.connect(self._width_changed)
        if not opened:
            anim.finished.connect(self.hide)
        self._anim = anim
        anim.start()

    def _width_changed(self, value) -> None:
        self.setMinimumWidth(int(value))
        self.width_changed.emit(int(value))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._content.setFixedHeight(self.height())
        self._content.move(self.width() - theme.PANEL_WIDTH, 0)
        super().resizeEvent(event)


# --- the window ------------------------------------------------------------


class PromptWindow(QWidget):
    """Her way in: one window, five ways out.

    A separate, normal, clickable window — MASTERSPEC 5.6 is explicit that the
    overlay is click-through and this is not.

    "I did it" is always available during a task (MASTERSPEC 5.5). It is the
    escape hatch for every way a success check can be wrong: she clicked the
    right thing, the check disagreed, and she should not have to argue with a
    circle. "Stop" is the same thing as Ctrl+Alt+P for someone who would rather
    press a button than remember a chord. Both live in the walkthrough drawer,
    which is open for exactly as long as there is a task to stop.
    """

    submitted = Signal(str)
    #: "Find it for me": the one thing Tiny Me does *instead* of guiding
    #: (MASTERSPEC 3A, 4). A separate signal rather than a mode flag, so the
    #: guide path cannot accidentally start doing things for her.
    find_requested = Signal(str)
    #: "Do it for me": the booking flow (MASTERSPEC 3B). Its own signal for the
    #: same reason as ``find_requested`` -- what Tiny Me is allowed to *do* is
    #: decided by which button she pressed plus ``guard.can_do_it``, never by
    #: the model.
    do_requested = Signal(str)
    did_it = Signal()
    stopped = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setWindowTitle("Tiny Me")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(theme.stylesheet())
        self._maximised = False
        self._normal_geometry = None
        self._task_active = False

        # -- chrome --
        self.titlebar = TitleBar(self)
        self.titlebar.minimise.connect(self.showMinimized)
        self.titlebar.maximise.connect(self.toggle_maximised)
        self.titlebar.close_window.connect(self.hide)

        self.sidebar = Sidebar(self)
        self.sidebar.navigated.connect(self._navigate)

        # -- pages --
        self.home = HomePage(self)
        self.history = HistoryPage(self)
        self.examples = ExamplesPage(self)
        self.settings = SettingsPage(self)
        self.about = AboutPage(self)

        self.pages = FadeStack(self)
        for page in (self.home, self.history, self.examples, self.settings,
                     self.about):
            self.pages.addWidget(scrollable(page, self.pages))

        self.home.guide_requested.connect(self._guide)
        self.home.do_requested.connect(self._do)
        self.home.find_requested.connect(self._find)
        self.history.repeat_requested.connect(self._guide)
        self.examples.chosen.connect(self._from_example)

        # -- the drawer --
        self.panel = StepPanel(self)
        self.drawer = Drawer(self.panel, self)
        self.panel.did_it.connect(self.did_it.emit)
        self.panel.stopped.connect(self.stopped.emit)
        self.panel.closed.connect(lambda: self.drawer.set_open(False))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.sidebar)
        body.addWidget(self.pages, 1)
        body.addWidget(self.drawer)

        column = QVBoxLayout(self)
        margin = theme.SHADOW_MARGIN
        column.setContentsMargins(margin, margin, margin, margin)
        column.setSpacing(0)
        column.addWidget(self.titlebar)
        column.addLayout(body, 1)

        self.toasts = ToastHost(self)
        self.drawer.width_changed.connect(lambda _: self._place_toasts())
        self._size_to_screen()

    # -- geometry --

    def _size_to_screen(self) -> None:
        """Pick a size that fits, and sit a little above centre."""
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        wanted_w, wanted_h = 1180, 790
        if available is not None:
            wanted_w = min(wanted_w, int(available.width() * 0.92))
            wanted_h = min(wanted_h, int(available.height() * 0.92))
        margin = theme.SHADOW_MARGIN * 2
        self.resize(wanted_w + margin, wanted_h + margin)
        if available is not None:
            self.move(
                available.center().x() - self.width() // 2,
                max(available.top(),
                    available.center().y() - int(self.height() * 0.54)),
            )

    def toggle_maximised(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        if self._maximised:
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)
            self._maximised = False
        else:
            self._normal_geometry = self.geometry()
            self.setGeometry(screen.availableGeometry())
            self._maximised = True
        self.update()

    def _place_toasts(self) -> None:
        """Keep toasts over the page, never over the walkthrough drawer.

        The drawer's bottom-right corner is where "Stop" and "I did it" live,
        and a toast landing on the button she is reaching for is the one place
        a notification must never go.
        """
        right = self.width() - theme.SHADOW_MARGIN - self.drawer.width()
        left = theme.SHADOW_MARGIN + theme.SIDEBAR_WIDTH
        self.toasts.setGeometry(
            left, theme.SHADOW_MARGIN + theme.TITLEBAR_HEIGHT,
            max(280, right - left),
            max(120, self.height() - 2 * theme.SHADOW_MARGIN - theme.TITLEBAR_HEIGHT),
        )
        self.toasts.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._place_toasts()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """The window itself: a rounded card floating on a soft shadow."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        margin = 0 if self._maximised else theme.SHADOW_MARGIN
        radius = 0 if self._maximised else theme.WINDOW_RADIUS
        body = QRectF(self.rect()).adjusted(margin, margin, -margin, -margin)

        if not self._maximised:
            soft_shadow(painter, body, radius, spread=16.0, alpha=0.30,
                        colour=QColor(theme.FOREST_DEEP))

        path = QPainterPath()
        path.addRoundedRect(body, radius, radius)
        painter.setClipPath(path)
        painter.fillRect(body, QColor(theme.CREAM))
        # The green chrome runs behind the title bar and down the sidebar, as
        # one shape, so the corner radius cuts both at once.
        painter.fillRect(
            QRectF(body.left(), body.top(), body.width(), theme.TITLEBAR_HEIGHT),
            QColor(theme.FOREST),
        )
        painter.fillRect(
            QRectF(body.left(), body.top(), theme.SIDEBAR_WIDTH, body.height()),
            QColor(theme.FOREST),
        )
        painter.setClipping(False)
        painter.setPen(QPen(theme.qcolor(theme.FOREST_DEEP, 0.35), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.end()

    # -- navigation --

    def _navigate(self, index: int) -> None:
        forward = index >= self.pages.currentIndex()
        self.pages.go_to(index, forward=forward)
        if index == 2:
            QTimer.singleShot(120, self.examples.play_entrance)

    def go_home(self) -> None:
        self.sidebar.select(0)

    # -- what the controls mean --

    def _guide(self, text: str) -> None:
        self.history.add(text)
        self.submitted.emit(text)

    def _do(self, text: str) -> None:
        self.history.add(text)
        self.do_requested.emit(text)

    def _find(self, text: str) -> None:
        self.history.add(text or "Find my newest download")
        self.find_requested.emit(text)

    def _from_example(self, text: str, mode: str) -> None:
        self.go_home()
        self.home.prompt.set_text(text)
        if mode == "find":
            self._find(text)
        elif mode == "do":
            self._do(text)
        else:
            self._guide(text)

    # --- the API app.main uses --------------------------------------------

    def ask(self) -> None:
        """Show, focus and raise. Called on the main thread only."""
        first_show = not self.isVisible()
        self.show()
        self.raise_()
        self.activateWindow()
        self.home.prompt.focus()
        if first_show:
            self.go_home()
            self.home.play_entrance()
            self._play_window_entrance()

    def _play_window_entrance(self) -> None:
        """Rise 18 px into place. Short, because she pressed a hotkey to get here."""
        end = self.pos()
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(theme.SLOW)
        anim.setEasingCurve(theme.EASE_PANEL)
        anim.setStartValue(QPoint(end.x(), end.y() + 18))
        anim.setEndValue(end)
        self._entrance = anim
        anim.start()

    def set_status(self, text: str) -> None:
        """The one line that says what is happening.

        It goes to whichever surface she is looking at: the drawer while a task
        is running, and the home page's banner when there is no task — a
        finished message, or something that went wrong.
        """
        text = (text or "").strip()
        if not text:
            return
        if self._task_active and self.drawer.is_open:
            self.panel.set_detail(text)
        else:
            kind = Banner.ERROR if text.lower().startswith("something went wrong") \
                else Banner.NOTE
            self.home.banner.show_message(text, kind)

    def set_busy(self, busy: bool) -> None:
        self.home.prompt.set_busy(busy)
        self.home.show_card.setEnabled(not busy)
        self.home.do_card.setEnabled(not busy)
        self.panel.set_busy(busy)

    def set_task_active(self, active: bool) -> None:
        """Only offer "I did it" and "Stop" while there is a task to do it to."""
        self._task_active = active
        self.panel.stop_button.setEnabled(active)
        self.panel.done_button.setEnabled(active)
        self.drawer.set_open(active)
        if active:
            self.home.banner.dismiss()

    # --- optional extras the loop calls when it has something to say -------

    def begin_task(self, goal: str) -> None:
        """A task is starting: open the drawer on a clean slate."""
        self.panel.begin(goal)
        self.drawer.set_open(True)

    def push_step(self, index: int, title: str, body: str = "") -> None:
        """Step ``index`` is now the one she is on."""
        self.panel.push_step(index, title, body)

    def finish_task(self, message: str, ok: bool = True) -> None:
        self.panel.finish(message, ok)
        self.toast("All done" if ok else "That one stopped early",
                   message, Toast.SUCCESS if ok else Toast.WARN)

    def toast(self, title: str, body: str = "", kind: str = Toast.INFO) -> None:
        self.toasts.post(title, body, kind)

    def privacy_hold(self, message: str) -> None:
        """The guard stopped us. Say so in the drawer, in her words.

        Deliberately loud: this is the one moment where Tiny Me doing nothing
        is the feature, and she needs to know the screen is hers again.
        """
        self.panel.say(message, Banner.PRIVACY)

    def handover_note(self, message: str) -> None:
        """A field in the browser is hers to fill (MASTERSPEC 3B)."""
        self.panel.say(message, Banner.PRIVACY)
        self.toast("This part is yours", message, Toast.WARN)

    # -- keyboard --

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Escape hides the window; it never stops a task by accident."""
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
