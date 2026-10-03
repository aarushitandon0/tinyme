"""Tests for app.watcher: the five success checks, and the per-step watch.

CLAUDE.md rule 2: success is decided deterministically in code, never by the
model. These tests are where that rule is actually enforced, so they use real
pixel arrays and real window-info values rather than asserting on mocks - a mock
that returns True proves nothing about whether a step worked.

Fixture "images" are small numpy arrays. A screenshot of a real Explorer window
would make these tests depend on one machine's theme and DPI; a 40x60 block of
known pixels tests the same arithmetic and fails for one reason only.
"""
import numpy as np
import pytest

from app import watcher
from app.brain import CheckType, SuccessCheck


def win(class_name="CabinetWClass", title="Downloads", hwnd=101):
    return watcher.WindowInfo(class_name=class_name, title=title, hwnd=hwnd)


def screen(window=None, texts=None, region=None):
    return watcher.Screen(
        window=window if window is not None else win(),
        texts=texts,
        region=region,
    )


def block(value: int, size=(40, 60)) -> np.ndarray:
    return np.full((*size, 3), value, dtype=np.uint8)


class TestWindowClassIs:
    """MASTERSPEC 5.4: GetClassName of the foreground window.

    CLAUDE.md rule 11: Explorer is identified by class, never by title, because
    the title is the folder name ("Home", "Downloads") and never "File Explorer".
    """

    def test_passes_when_the_foreground_class_matches(self):
        check = SuccessCheck(type=CheckType.WINDOW_CLASS_IS, value="CabinetWClass")
        assert watcher.check_passed(check, screen(win(class_name="CabinetWClass")))

    def test_fails_when_a_different_window_is_in_front(self):
        check = SuccessCheck(type=CheckType.WINDOW_CLASS_IS, value="CabinetWClass")
        assert not watcher.check_passed(check, screen(win(class_name="Chrome_WidgetWin_1")))

    def test_ignores_case_because_the_model_writes_the_value(self):
        check = SuccessCheck(type=CheckType.WINDOW_CLASS_IS, value="cabinetwclass")
        assert watcher.check_passed(check, screen(win(class_name="CabinetWClass")))

    def test_does_not_match_a_class_that_merely_contains_it(self):
        """Class is an exact identity, unlike title. Substring matching here
        would pass "CabinetWClassFake" and circle the wrong window."""
        check = SuccessCheck(type=CheckType.WINDOW_CLASS_IS, value="Cabinet")
        assert not watcher.check_passed(check, screen(win(class_name="CabinetWClass")))


class TestWindowTitleContains:
    def test_passes_on_a_substring_case_insensitively(self):
        check = SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS, value="downloads")
        assert watcher.check_passed(check, screen(win(title="Downloads")))

    def test_passes_when_the_title_has_more_around_it(self):
        check = SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS, value="Downloads")
        assert watcher.check_passed(check, screen(win(title="Downloads - File Explorer")))

    def test_fails_when_absent(self):
        check = SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS, value="Downloads")
        assert not watcher.check_passed(check, screen(win(title="Documents")))

    def test_an_empty_value_never_passes(self):
        """Every title contains "", so this would be an instant false success."""
        check = SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS, value="")
        assert not watcher.check_passed(check, screen(win(title="Downloads")))


class TestTextAppears:
    """MASTERSPEC 5.4: fresh OCR, rapidfuzz ratio >= 85.

    Deliberately ``ratio`` per text, not ``partial_ratio``: notes/bench.md
    records a false positive from partial_ratio in the overlay probe, where
    ordinary screen text cleared the 85 threshold.
    """

    def test_passes_on_an_exact_match(self):
        check = SuccessCheck(type=CheckType.TEXT_APPEARS, value="bill.pdf")
        assert watcher.check_passed(check, screen(texts=("Downloads", "bill.pdf")))

    def test_passes_through_one_ocr_slip(self):
        check = SuccessCheck(type=CheckType.TEXT_APPEARS, value="Downloads")
        assert watcher.check_passed(check, screen(texts=("Down1oads",)))

    def test_fails_on_a_different_word(self):
        check = SuccessCheck(type=CheckType.TEXT_APPEARS, value="Downloads")
        assert not watcher.check_passed(check, screen(texts=("Documents", "Desktop")))

    def test_does_not_pass_on_a_long_line_that_merely_contains_it(self):
        """partial_ratio would pass this; ratio must not."""
        check = SuccessCheck(type=CheckType.TEXT_APPEARS, value="bill")
        assert not watcher.check_passed(
            check, screen(texts=("the quick brown bill jumped over the lazy dog",))
        )

    def test_is_pending_not_failed_when_ocr_was_not_run_this_tick(self):
        """Cheap checks run at 1 Hz; OCR does not. No texts means "don't know",
        and "don't know" must not read as success."""
        check = SuccessCheck(type=CheckType.TEXT_APPEARS, value="Downloads")
        assert not watcher.check_passed(check, screen(texts=None))


class TestTextDisappears:
    def test_passes_when_the_text_is_gone(self):
        check = SuccessCheck(type=CheckType.TEXT_DISAPPEARS, value="Downloading…")
        assert watcher.check_passed(check, screen(texts=("Downloads", "bill.pdf")))

    def test_fails_while_the_text_is_still_there(self):
        check = SuccessCheck(type=CheckType.TEXT_DISAPPEARS, value="Downloading…")
        assert not watcher.check_passed(check, screen(texts=("Downloading…",)))

    def test_does_not_pass_when_ocr_was_not_run(self):
        """The dangerous direction: no OCR must not look like "it vanished"."""
        check = SuccessCheck(type=CheckType.TEXT_DISAPPEARS, value="Downloading…")
        assert not watcher.check_passed(check, screen(texts=None))

    def test_does_not_pass_on_an_empty_screen_reading(self):
        """An OCR pass that found nothing at all is a failed read, not proof."""
        check = SuccessCheck(type=CheckType.TEXT_DISAPPEARS, value="Downloading…")
        assert not watcher.check_passed(check, screen(texts=()))


class TestRegionDiff:
    def test_identical_regions_have_no_difference(self):
        assert watcher.region_diff(block(100), block(100)) == 0.0

    def test_mean_absolute_difference_over_the_whole_region(self):
        assert watcher.region_diff(block(100), block(120)) == pytest.approx(20.0)

    def test_a_change_in_part_of_the_region_is_averaged(self):
        before = block(0)
        after = block(0)
        after[:20, :, :] = 255  # half the rows change completely
        assert watcher.region_diff(before, after) == pytest.approx(127.5)

    def test_mismatched_shapes_report_a_change_rather_than_crashing(self):
        """The window moved or resized between captures. That is a change."""
        assert watcher.region_diff(block(100, size=(40, 60)),
                                   block(100, size=(30, 60))) == float("inf")

    def test_does_not_overflow_on_uint8_pixels(self):
        """Subtracting uint8 arrays wraps around: 0 - 255 is 1, not 255."""
        assert watcher.region_diff(block(0), block(255)) == pytest.approx(255.0)


class TestRegionChanged:
    def test_passes_once_the_region_differs_enough(self):
        check = SuccessCheck(type=CheckType.REGION_CHANGED)
        watch = watcher.StepWatch(check, baseline=block(0), region_threshold=10.0)
        assert watch.poll(screen(region=block(50))).outcome is watcher.Outcome.PASSED

    def test_pending_while_the_region_is_unchanged(self):
        check = SuccessCheck(type=CheckType.REGION_CHANGED)
        watch = watcher.StepWatch(check, baseline=block(0), region_threshold=10.0)
        assert watch.poll(screen(region=block(0))).outcome is watcher.Outcome.PENDING

    def test_small_noise_does_not_count_as_a_change(self):
        check = SuccessCheck(type=CheckType.REGION_CHANGED)
        watch = watcher.StepWatch(check, baseline=block(100), region_threshold=10.0)
        assert watch.poll(screen(region=block(103))).outcome is watcher.Outcome.PENDING

    def test_without_a_baseline_it_cannot_pass(self):
        check = SuccessCheck(type=CheckType.REGION_CHANGED)
        watch = watcher.StepWatch(check, baseline=None)
        assert watch.poll(screen(region=block(50))).outcome is watcher.Outcome.PENDING


class TestNeedsOcr:
    """Cheap checks first (MASTERSPEC 5.4). OCR costs 10-20 s on her laptop
    (notes/bench.md), so the loop must know when it can skip it entirely."""

    @pytest.mark.parametrize("check_type", [
        CheckType.WINDOW_CLASS_IS,
        CheckType.WINDOW_TITLE_CONTAINS,
        CheckType.REGION_CHANGED,
    ])
    def test_cheap_checks_never_ask_for_ocr(self, check_type):
        assert not watcher.needs_ocr(SuccessCheck(type=check_type, value="x"))

    @pytest.mark.parametrize("check_type", [
        CheckType.TEXT_APPEARS,
        CheckType.TEXT_DISAPPEARS,
    ])
    def test_text_checks_need_ocr(self, check_type):
        assert watcher.needs_ocr(SuccessCheck(type=check_type, value="x"))


class FakeClock:
    """A clock the test moves by hand, so a 20 s rule takes no real seconds."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def watch(check=None, clock=None, **kwargs):
    # The default check looks for a title no fixture window has, so a watch
    # starts out genuinely pending and a test that wants success says so.
    check = check or SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS,
                                  value="not open yet")
    return watcher.StepWatch(check, clock=clock or FakeClock(), **kwargs)


class TestSuccessBeatsEverythingElse:
    def test_a_step_that_worked_is_not_reported_as_an_unexpected_change(self):
        """Clicking the File Explorer icon changes the foreground window *and*
        satisfies the check. Order matters: this must be PASSED, not REPLAN."""
        check = SuccessCheck(type=CheckType.WINDOW_CLASS_IS, value="CabinetWClass")
        step = watch(check, start_window=win(class_name="Shell_TrayWnd", hwnd=1))
        poll = step.poll(screen(win(class_name="CabinetWClass", hwnd=2)))
        assert poll.outcome is watcher.Outcome.PASSED


class TestReplanOnUnexpectedChange:
    """MASTERSPEC 5.5: never repeat the old step blindly against a new screen."""

    def test_a_different_window_coming_to_the_front_means_replan(self):
        step = watch(start_window=win(title="Home", hwnd=1))
        poll = step.poll(screen(win(title="Settings", hwnd=99)))
        assert poll.outcome is watcher.Outcome.REPLAN
        assert "window" in poll.reason

    def test_the_same_window_still_in_front_is_merely_pending(self):
        step = watch(start_window=win(title="Home", hwnd=1))
        assert step.poll(screen(win(title="Home", hwnd=1))).outcome is watcher.Outcome.PENDING

    def test_the_target_changing_while_the_check_fails_means_replan(self):
        """She clicked something, the thing under the circle moved, but the check
        did not pass: the screen has moved on without us."""
        step = watch(start_window=win(hwnd=1), baseline=block(0), region_threshold=10.0)
        poll = step.poll(screen(win(hwnd=1), region=block(200)))
        assert poll.outcome is watcher.Outcome.REPLAN
        assert "target" in poll.reason

    def test_an_unchanged_target_is_pending(self):
        step = watch(start_window=win(hwnd=1), baseline=block(0), region_threshold=10.0)
        poll = step.poll(screen(win(hwnd=1), region=block(0)))
        assert poll.outcome is watcher.Outcome.PENDING

    def test_a_region_check_does_not_replan_on_its_own_evidence(self):
        """For region_changed, "the target changed" *is* success. Treating it as
        an unexpected change would make this check type impossible to pass."""
        check = SuccessCheck(type=CheckType.REGION_CHANGED)
        step = watch(check, start_window=win(hwnd=1), baseline=block(0),
                     region_threshold=10.0)
        poll = step.poll(screen(win(hwnd=1), region=block(200)))
        assert poll.outcome is watcher.Outcome.PASSED

    def test_without_a_starting_window_no_window_replan_is_claimed(self):
        step = watch(start_window=None)
        assert step.poll(screen(win(hwnd=77))).outcome is watcher.Outcome.PENDING


class TestTheTwentySecondHint:
    def test_no_hint_before_the_timeout(self):
        clock = FakeClock()
        step = watch(clock=clock, hint_after_s=20.0)
        clock.advance(19.0)
        assert step.poll(screen()).show_hint is False

    def test_hint_once_the_step_has_gone_quiet(self):
        clock = FakeClock()
        step = watch(clock=clock, hint_after_s=20.0)
        clock.advance(20.0)
        assert step.poll(screen()).show_hint is True

    def test_the_hint_is_shown_once_and_not_every_tick_after(self):
        """MASTERSPEC 5.5 says rephrase *once*. Nagging her every second while
        she is reading the screen is worse than saying nothing."""
        clock = FakeClock()
        step = watch(clock=clock, hint_after_s=20.0)
        clock.advance(25.0)
        assert step.poll(screen()).show_hint is True
        clock.advance(1.0)
        assert step.poll(screen()).show_hint is False

    def test_a_hint_does_not_end_the_step(self):
        clock = FakeClock()
        step = watch(clock=clock, hint_after_s=20.0)
        clock.advance(30.0)
        assert step.poll(screen()).outcome is watcher.Outcome.PENDING


class TestIDidIt:
    def test_forces_success(self):
        """Always available, and it wins over whatever the check thinks
        (MASTERSPEC 5.5). She can see her own screen; we cannot."""
        step = watch()
        assert step.done_by_her().outcome is watcher.Outcome.MANUAL


class TestCrop:
    """The padded region the watcher diffs against (MASTERSPEC 5.4).

    The padding is the point: a click highlights, underlines or opens a menu
    just *outside* the text box OCR found, so a crop of exactly the bbox would
    often show no change at all.
    """

    def image(self) -> np.ndarray:
        """A 200x300 gradient, so every crop has distinguishable content."""
        rows = np.arange(200, dtype=np.uint8).reshape(200, 1, 1)
        return np.repeat(np.repeat(rows, 300, axis=1), 3, axis=2)

    def test_crops_the_bbox_with_padding_on_every_side(self):
        region = watcher.crop(self.image(), (100.0, 50.0, 140.0, 70.0), pad=10)
        assert region.shape == (40, 60, 3)  # 20 high + 2x10, 40 wide + 2x10

    def test_clamps_to_the_image_rather_than_wrapping(self):
        """A bbox at the screen edge must not produce a negative slice, which
        numpy would happily read from the far side of the image."""
        region = watcher.crop(self.image(), (0.0, 0.0, 30.0, 20.0), pad=20)
        assert region.shape == (40, 50, 3)

    def test_subtracts_the_monitor_origin(self):
        """Bboxes are in screen pixels; the image starts at the monitor origin.
        On a second-position primary monitor those differ (CLAUDE.md rule 9)."""
        image = self.image()
        at_origin = watcher.crop(image, (100.0, 50.0, 140.0, 70.0), (0, 0), pad=0)
        offset = watcher.crop(image, (1100.0, 1050.0, 1140.0, 1070.0),
                              (1000, 1000), pad=0)
        assert np.array_equal(at_origin, offset)

    def test_a_box_entirely_off_the_image_gives_nothing(self):
        assert watcher.crop(self.image(), (900.0, 900.0, 950.0, 950.0), pad=0) is None

    def test_an_empty_box_gives_nothing_rather_than_a_zero_size_array(self):
        assert watcher.crop(self.image(), (50.0, 50.0, 50.0, 50.0), pad=0) is None

    def test_no_bbox_or_no_image_gives_nothing(self):
        assert watcher.crop(self.image(), None) is None
        assert watcher.crop(None, (0.0, 0.0, 10.0, 10.0)) is None

    def test_the_crop_is_a_copy_not_a_view(self):
        """The baseline outlives the screenshot it came from, which is dropped
        after the step (CLAUDE.md rule 6). A view would keep the whole frame
        alive, or worse, change under us."""
        image = self.image()
        region = watcher.crop(image, (100.0, 50.0, 140.0, 70.0), pad=0)
        image[:] = 0
        assert region.any()


class TestReadWindow:
    def test_falls_back_to_blanks_when_win32_is_unavailable(self, monkeypatch):
        """Every external call needs a clear fallback (CLAUDE.md code style). An
        empty WindowInfo fails every cheap check, i.e. "not yet" - never a false
        success."""
        import builtins

        real_import = builtins.__import__

        def no_win32(name, *args, **kwargs):
            if name == "win32gui":
                raise ImportError("no win32gui here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_win32)
        info = watcher.read_window()
        assert info == watcher.WindowInfo()
        assert watcher.check_passed(
            SuccessCheck(type=CheckType.WINDOW_CLASS_IS, value="CabinetWClass"),
            screen(window=info),
        ) is False
