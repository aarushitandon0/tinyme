"""Tests for app.uia: filtering, name cleanup, the time budget, the fallback.

Nothing here touches real UI Automation. The COM walk is injected, so these run
on any machine and in CI, and the rules MASTERSPEC 5.2 states ("named,
on-screen, enabled controls", "< 300 ms", "depth-limited") are tested as rules
rather than eyeballed on screen.
"""
import sys

import pytest

from app import uia
from app.elements import Monitor

MON = Monitor(left=0, top=0, width=1920, height=1080)
# Where the taskbar sits on a 1920x1080 screen: the bottom 60 px.
TASKBAR_Y = (1020, 1080)


def info(name="Thing", role="ButtonControl", bbox=(100, 1030, 160, 1070),
         enabled=True, offscreen=False):
    return uia.ControlInfo(name=name, role=role, bbox_px=bbox,
                           enabled=enabled, offscreen=offscreen)


class TestCleanName:
    """Taskbar names carry state suffixes that are noise to her and to Gemma."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("File Explorer pinned", "File Explorer"),
            ("Microsoft Store pinned", "Microsoft Store"),
            ("Microsoft Edge - 1 running window pinned", "Microsoft Edge"),
            ("Visual Studio Code - 1 running window", "Visual Studio Code"),
            ("Spotify - 3 running windows", "Spotify"),
            ("Downloads", "Downloads"),
        ],
    )
    def test_strips_taskbar_state_suffixes(self, raw, expected):
        assert uia.clean_name(raw) == expected

    def test_keeps_only_the_first_line(self):
        # Tray buttons pack a whole status report into Name.
        assert uia.clean_name("Network PiyaliDen\nInternet access") == "Network PiyaliDen"
        assert uia.clean_name(" OneDrive\r\nThere isn't enough space") == "OneDrive"

    def test_collapses_whitespace(self):
        assert uia.clean_name("  Task   View  ") == "Task View"

    def test_running_window_suffix_alone_is_not_a_name(self):
        assert uia.clean_name("- 1 running window") == ""


class TestIsUsefulText:
    def test_rejects_empty(self):
        assert not uia.is_useful_text("")
        assert not uia.is_useful_text("   ")

    def test_rejects_icon_font_glyphs(self):
        # Tray icons put private-use codepoints in Name; OCR cannot read them
        # and neither can she, so they must never become an element.
        assert not uia.is_useful_text("")
        assert not uia.is_useful_text("")

    def test_keeps_real_words_even_beside_a_glyph(self):
        assert uia.is_useful_text("ENG IN")
        assert uia.is_useful_text(" Hidden icons")


class TestElementsFromInfos:
    def test_maps_name_role_and_source(self):
        [element] = uia.elements_from_infos(
            [info(name="File Explorer pinned", role="ButtonControl")], MON
        )
        assert element.text == "File Explorer"
        assert element.source == "uia"
        # MASTERSPEC 5.2 renders `uia:ListItem`, not `uia:ListItemControl`.
        assert element.role == "Button"
        assert element.region == "bottom-left"
        assert element.id == 0  # numbering is elements.number_elements' job

    def test_drops_unnamed_disabled_and_offscreen_controls(self):
        infos = [
            info(name=""),
            info(name="Greyed out", enabled=False),
            info(name="Scrolled away", offscreen=True),
            info(name="Keep me"),
        ]
        kept = uia.elements_from_infos(infos, MON)
        assert [element.text for element in kept] == ["Keep me"]

    def test_drops_zero_area_and_off_monitor_boxes(self):
        infos = [
            info(name="Collapsed", bbox=(0, 0, 0, 0)),
            info(name="Other screen", bbox=(-1900, 100, -1800, 140)),
            info(name="On screen"),
        ]
        kept = uia.elements_from_infos(infos, MON)
        assert [element.text for element in kept] == ["On screen"]

    def test_drops_a_nested_duplicate_of_its_parent(self):
        # A taskbar Search button contains a Text child also called "Search".
        # Pre-order walk means the parent comes first, and the parent is the
        # thing she can actually click.
        infos = [
            info(name="Search", role="ButtonControl", bbox=(659, 1030, 933, 1070)),
            info(name="Search", role="TextControl", bbox=(705, 1037, 757, 1061)),
        ]
        [element] = uia.elements_from_infos(infos, MON)
        assert element.role == "Button"

    def test_keeps_same_text_in_a_different_place(self):
        infos = [
            info(name="Open", bbox=(100, 100, 160, 130)),
            info(name="Open", bbox=(900, 500, 960, 530)),
        ]
        assert len(uia.elements_from_infos(infos, MON)) == 2

    def test_drops_a_text_label_inside_a_control_it_already_kept(self):
        """The clock and weather widget contribute several unclickable labels.

        They would hold slots in the 80-element cap ahead of real foreground
        elements, because cap_elements ranks taskbar elements first. The text
        differs from the container's name, so the same-name rule misses them.
        """
        infos = [
            info(name="Widgets 22C Mostly sunny", role="ButtonControl",
                 bbox=(8, 1020, 198, 1080)),
            info(name="22C", role="TextControl", bbox=(21, 1025, 100, 1075)),
            info(name="Mostly sunny", role="TextControl", bbox=(100, 1025, 190, 1075)),
        ]
        kept = uia.elements_from_infos(infos, MON)
        assert [element.text for element in kept] == ["Widgets 22C Mostly sunny"]

    def test_keeps_a_text_element_that_stands_on_its_own(self):
        infos = [
            info(name="Open", role="ButtonControl", bbox=(100, 100, 160, 130)),
            info(name="Downloads", role="TextControl", bbox=(400, 300, 500, 330)),
        ]
        kept = uia.elements_from_infos(infos, MON)
        assert [element.text for element in kept] == ["Open", "Downloads"]

    def test_skips_container_roles_nobody_clicks(self):
        infos = [
            info(name="Taskbar", role="PaneControl", bbox=(0, 1020, 1920, 1080)),
            info(name="Downloads", role="ListItemControl", bbox=(40, 300, 200, 330)),
        ]
        [element] = uia.elements_from_infos(infos, MON)
        assert element.text == "Downloads"


class TestTakeWithinBudget:
    def test_stops_when_the_budget_expires_and_keeps_what_it_has(self):
        clock = Clock(start=1.0, step=1.0)  # each reading "costs" a second
        pulled = []

        def source():
            for index in range(10):
                pulled.append(index)
                yield info(name=f"Item {index}")

        taken = uia.take_within_budget(source(), deadline=3.0, clock=clock)
        assert len(taken) == 2
        # The generator was not drained past the deadline: the walk really stops.
        assert pulled == [0, 1]

    def test_returns_nothing_if_already_out_of_time(self):
        def source():
            raise AssertionError("should not be walked at all")
            yield  # pragma: no cover

        assert uia.take_within_budget(source(), deadline=0.0, clock=lambda: 5.0) == []

    def test_caps_the_number_of_controls(self):
        source = (info(name=f"Item {index}") for index in range(500))
        taken = uia.take_within_budget(source, deadline=1.0, clock=lambda: 0.0, limit=5)
        assert len(taken) == 5


class Clock:
    """A fake clock that moves on every reading, or only when told to.

    ``step=0`` freezes it so a test can control time with ``advance``. Unlike an
    exhausted ``iter([...]).__next__`` it never raises, so a refactor that adds a
    clock reading fails an assertion instead of a StopIteration from nowhere.
    """

    def __init__(self, start: float = 0.0, step: float = 0.0) -> None:
        self.now = start
        self.step = step
        self.readings = 0

    def __call__(self) -> float:
        self.readings += 1
        value = self.now
        self.now += self.step
        return value

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRect:
    def __init__(self, box) -> None:
        self.left, self.top, self.right, self.bottom = box


class FakeControl:
    """Enough of a uiautomation Control for _walk, counting what it costs.

    Each property read on the real thing is a cross-process COM call, so the
    walk's whole job is to touch as few as possible. ``touched`` records every
    control whose ``Name`` was read, which is what the budget is spent on.
    """

    def __init__(self, name, role="ButtonControl", box=(0, 1030, 50, 1070),
                 touched=None, clock=None, cost=0.0) -> None:
        self._name = name
        self.ControlTypeName = role
        self.BoundingRectangle = FakeRect(box)
        self.IsEnabled = True
        self.IsOffscreen = False
        self._touched = touched if touched is not None else []
        self._clock = clock
        self._cost = cost

    @property
    def Name(self):
        self._touched.append(self._name)
        if self._clock is not None:
            self._clock.advance(self._cost)
        return self._name


class FakeUia:
    """Stands in for the lazily imported ``uiautomation`` module."""

    def __init__(self, controls) -> None:
        self.controls = controls
        self.walked_depths = []

    def ControlFromHandle(self, handle):
        return object()

    def WalkControl(self, root, includeTop=False, maxDepth=0):
        self.walked_depths.append(maxDepth)
        for control in self.controls:
            yield control, 1


class TestWalkHonoursTheDeadline:
    """The budget has to be hard on controls the walk *discards*, not just the
    ones it yields. Unnamed controls never reach ``take_within_budget``, so
    without a check inside the walk a long unnamed stretch - or the tail of the
    tree after the last named control - runs past the budget unmeasured."""

    def fake_module(self, controls):
        return FakeUia(controls)

    def test_stops_touching_discarded_controls_once_time_is_up(self, monkeypatch):
        clock = Clock(step=0.0)
        touched: list[str] = []
        # Unnamed controls: _snapshot drops every one, so nothing is ever yielded
        # and the consumer-side check never gets a chance to fire.
        controls = [FakeControl("", touched=touched, clock=clock, cost=0.1)
                    for _ in range(50)]
        monkeypatch.setitem(sys.modules, "uiautomation", self.fake_module(controls))

        taken = list(uia._walk(1, max_depth=6, deadline=0.25, clock=clock))

        assert taken == []
        # 0.1 s per control: read 3 (clock 0.0, 0.1, 0.2) then stop at 0.3.
        assert len(touched) == 3

    def test_yields_everything_when_there_is_time(self, monkeypatch):
        clock = Clock(step=0.0)
        controls = [FakeControl("File Explorer pinned", clock=clock, cost=0.001),
                   FakeControl("", clock=clock, cost=0.001),
                   FakeControl("Start", clock=clock, cost=0.001)]
        monkeypatch.setitem(sys.modules, "uiautomation", self.fake_module(controls))

        taken = list(uia._walk(1, max_depth=6, deadline=10.0, clock=clock))

        # Names are cleaned on the way out, and the unnamed one is dropped.
        assert [item.name for item in taken] == ["File Explorer", "Start"]

    def test_passes_the_depth_limit_through(self, monkeypatch):
        fake = self.fake_module([])
        monkeypatch.setitem(sys.modules, "uiautomation", fake)
        list(uia._walk(1, max_depth=7, deadline=10.0, clock=Clock()))
        assert fake.walked_depths == [7]

    def test_a_dead_window_handle_yields_nothing(self, monkeypatch):
        fake = self.fake_module([FakeControl("Start")])
        fake.ControlFromHandle = lambda handle: None
        monkeypatch.setitem(sys.modules, "uiautomation", fake)
        assert list(uia._walk(1, 6, deadline=10.0, clock=Clock())) == []

    def test_skip_roles_cost_one_extra_read_not_four(self, monkeypatch):
        """A named container is dropped before its rect is read.

        Containers are nearly all named, so the Name gate does not catch them;
        without the role check they would spend three more COM calls each.
        """
        clock = Clock()
        pane = FakeControl("Taskbar", role="PaneControl", clock=clock)
        monkeypatch.setitem(sys.modules, "uiautomation", self.fake_module([pane]))
        assert list(uia._walk(1, 6, deadline=10.0, clock=clock)) == []


class TestCollectInfos:
    """The budget split, which is where the acceptance element can be lost."""

    def walk_recorder(self, yields_per_root=0, cost=0.0, clock=None):
        """Replace _walk, recording the deadline each root was given."""
        calls: list[dict] = []

        def fake_walk(hwnd, max_depth, deadline, clock_arg=None, label="walk"):
            calls.append({"hwnd": hwnd, "max_depth": max_depth,
                          "deadline": deadline, "label": label})
            for index in range(yields_per_root):
                if clock is not None:
                    clock.advance(cost)
                yield info(name=f"{hwnd}-{index}")

        return fake_walk, calls

    def test_the_last_root_gets_the_rest_and_the_others_keep_a_reserve(self, monkeypatch):
        clock = Clock(step=0.0)
        fake_walk, calls = self.walk_recorder()
        monkeypatch.setattr(uia, "_ensure_com", lambda: None)
        monkeypatch.setattr(uia, "_walk", fake_walk)

        uia._collect_infos(300.0, clock=clock,
                           roots=[("taskbar", 11, 7), ("foreground", 22, 6)])

        # Taskbar may spend everything except the foreground's reserve; the
        # foreground runs to the hard deadline.
        assert calls[0]["deadline"] == pytest.approx(0.300 - uia.FOREGROUND_RESERVE_MS / 1000)
        assert calls[1]["deadline"] == pytest.approx(0.300)
        assert [call["max_depth"] for call in calls] == [7, 6]
        # The label reaches the walk, which is how the logs name the root that
        # ran out of time.
        assert [call["label"] for call in calls] == ["taskbar", "foreground"]

    def test_a_single_root_gets_the_whole_budget(self, monkeypatch):
        clock = Clock(step=0.0)
        fake_walk, calls = self.walk_recorder()
        monkeypatch.setattr(uia, "_ensure_com", lambda: None)
        monkeypatch.setattr(uia, "_walk", fake_walk)

        uia._collect_infos(300.0, clock=clock, roots=[("taskbar", 11, 7)])

        assert calls[0]["deadline"] == pytest.approx(0.300)

    def test_an_overrunning_taskbar_leaves_the_foreground_empty_without_raising(
            self, monkeypatch):
        """What a loaded machine does. Scene A's icons matter more than the
        foreground window's caption buttons, so this is the trade we want."""
        clock = Clock(step=0.0)
        fake_walk, calls = self.walk_recorder(yields_per_root=10, cost=0.05, clock=clock)
        monkeypatch.setattr(uia, "_ensure_com", lambda: None)
        monkeypatch.setattr(uia, "_walk", fake_walk)

        infos = uia._collect_infos(300.0, clock=clock,
                                   roots=[("taskbar", 11, 7), ("foreground", 22, 6)])

        # The taskbar kept everything it had reached by its deadline, and the
        # foreground got only the sliver of reserve that was left - not the 40%
        # share it used to be handed before the taskbar had run.
        from_taskbar = [item for item in infos if item.name.startswith("11-")]
        from_foreground = [item for item in infos if item.name.startswith("22-")]
        assert len(from_taskbar) >= 4
        assert len(from_foreground) <= 1
        # And the whole pass still respected the hard deadline.
        assert clock.now <= 0.300 + 0.05

    def test_no_roots_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(uia, "_ensure_com", lambda: None)
        assert uia._collect_infos(300.0, clock=Clock(), roots=[]) == []

    def test_the_control_limit_is_shared_across_roots(self, monkeypatch):
        clock = Clock(step=0.0)
        fake_walk, calls = self.walk_recorder(yields_per_root=500)
        monkeypatch.setattr(uia, "_ensure_com", lambda: None)
        monkeypatch.setattr(uia, "_walk", fake_walk)

        infos = uia._collect_infos(300.0, clock=clock,
                                   roots=[("taskbar", 11, 7), ("foreground", 22, 6)])

        assert len(infos) == uia.MAX_CONTROLS


class TestCollectFallback:
    """CLAUDE.md: every external call has a timeout and a clear fallback."""

    def test_returns_no_elements_when_uia_raises(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("COM said no")

        monkeypatch.setattr(uia, "_collect_infos", boom)
        elements, uia_ms = uia.collect(MON)
        assert elements == []
        assert uia_ms >= 0.0

    def test_reports_elapsed_milliseconds(self, monkeypatch):
        monkeypatch.setattr(uia, "_collect_infos",
                            lambda *_a, **_k: [info(name="File Explorer pinned")])
        elements, uia_ms = uia.collect(MON)
        assert [element.text for element in elements] == ["File Explorer"]
        assert isinstance(uia_ms, float)
