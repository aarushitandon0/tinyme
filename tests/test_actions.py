"""Tests for app.actions: the two things Tiny Me does *for* her.

Split in three, because the three risks are different:

* **Picking the wrong file** is the quiet failure. She asked for one thing and
  Explorer highlights another, and the only way she finds out is by opening it.
  So the matcher is tested on the filler words she actually types.
* **The ``explorer /select`` quoting** is the loud failure, and the one that is
  invisible on a path with no spaces -- which is every path in a test folder
  unless the test puts one there on purpose. See ``TestSelectCommand``.
* **The booking DOM guard** is the unacceptable failure: typing into her
  password box. Those tests run a real headless Chromium over the real
  ``demo_site/`` pages, because the guard reads a live DOM and a hand-written
  dictionary of what we *think* the page contains would prove nothing about the
  page we ship.

Nothing here writes outside ``tmp_path`` and nothing launches Explorer: the one
test that covers the launch path asserts on the command string instead
(``reveal_in_explorer`` is the only function that could open a window, and it
is given a path that does not exist).
"""
from pathlib import Path

import pytest

from app import actions
from app.actions import (
    FileHit,
    find_file,
    find_for_her,
    name_hints,
    reveal_in_explorer,
)
from app.guard import TaskType


def touch(folder: Path, name: str, mtime: float) -> Path:
    """A file with a known modification time, so "newest" is not a race."""
    path = folder / name
    path.write_text("x", encoding="utf-8")
    import os

    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    return folder


class TestNameHints:
    """What is left of her goal once the words every goal contains are gone."""

    def test_a_goal_with_no_filename_leaves_nothing(self):
        assert name_hints("I can't find the file I downloaded") == []

    def test_newest_phrasing_leaves_nothing(self):
        assert name_hints("open my newest download please") == []

    def test_a_name_survives(self):
        assert name_hints("where is the electricity bill pdf") == [
            "electricity", "bill", "pdf",
        ]

    def test_single_letters_are_dropped(self):
        # OCR-free, but her typing is not: "a" and "i" carry nothing.
        assert "a" not in name_hints("find a bill")

    def test_empty_goal_is_fine(self):
        assert name_hints("") == []


class TestFindFile:
    """Name match when her words say which file, newest when they do not."""

    def test_newest_when_the_goal_names_nothing(self, downloads: Path):
        touch(downloads, "old.pdf", 1_000.0)
        newest = touch(downloads, "statement.pdf", 9_000.0)

        hit = find_file("I can't find the file I downloaded", downloads)

        assert hit is not None
        assert hit.path == newest
        assert hit.because == "newest"

    def test_a_named_file_beats_the_newest(self, downloads: Path):
        wanted = touch(downloads, "Electricity bill September.pdf", 1_000.0)
        touch(downloads, "holiday-photos.zip", 9_000.0)

        hit = find_file("where is the electricity bill", downloads)

        assert hit is not None
        assert hit.path == wanted
        assert hit.because == "name"

    def test_a_weak_name_match_falls_back_to_newest(self, downloads: Path):
        """Her words name something that simply is not there.

        The fallback matters more than the match: "here is your newest
        download" is visibly an answer to a different question, and she can
        see that. A confident circle on an unrelated file cannot be seen.
        """
        touch(downloads, "holiday-photos.zip", 1_000.0)
        newest = touch(downloads, "receipt.pdf", 9_000.0)

        hit = find_file("find the aadhaar card scan", downloads)

        assert hit is not None
        assert hit.path == newest
        assert hit.because == "newest"

    def test_an_empty_folder_finds_nothing(self, downloads: Path):
        assert find_file("anything", downloads) is None

    def test_folders_are_not_offered(self, downloads: Path):
        (downloads / "old stuff").mkdir()
        assert find_file("", downloads) is None

    def test_partial_downloads_are_skipped(self, downloads: Path):
        """A half-downloaded file is the newest thing there and is useless."""
        done = touch(downloads, "report.pdf", 1_000.0)
        touch(downloads, "report.pdf.crdownload", 9_000.0)

        hit = find_file("", downloads)

        assert hit is not None
        assert hit.path == done

    def test_windows_clutter_is_skipped(self, downloads: Path):
        real = touch(downloads, "invoice.pdf", 1_000.0)
        touch(downloads, "desktop.ini", 9_000.0)

        hit = find_file("", downloads)

        assert hit is not None
        assert hit.path == real

    def test_a_missing_folder_finds_nothing(self, tmp_path: Path):
        assert find_file("", tmp_path / "nope") is None


class TestSelectCommand:
    """The quoting. This is the bug that only shows up on her laptop.

    Her Downloads has "Bank holiday list.docx" in it; a test folder usually
    does not, so the space is put there deliberately.
    """

    def test_the_path_is_quoted_and_the_switch_is_not(self):
        command = actions._select_command(Path(r"C:\Users\Her\Downloads\bill.pdf"))
        assert command == r'explorer /select,"C:\Users\Her\Downloads\bill.pdf"'

    def test_a_path_with_spaces_keeps_the_switch_outside_the_quotes(self):
        command = actions._select_command(Path(r"C:\My Files\Bank holiday list.docx"))
        # The failure mode being guarded against is `"/select,C:\My Files\..."`,
        # which Explorer answers by opening Documents and selecting nothing.
        assert command.startswith('explorer /select,"')
        assert '"/select' not in command


class TestRevealIsReadOnly:
    """MASTERSPEC 4: finding and opening, never deleting or moving."""

    def test_a_missing_file_launches_nothing(self, tmp_path: Path, monkeypatch):
        launched: list[str] = []
        monkeypatch.setattr(actions.subprocess, "Popen",
                            lambda command, **kwargs: launched.append(command))

        assert reveal_in_explorer(tmp_path / "gone.pdf") is False
        assert launched == []

    def test_a_real_file_is_revealed_with_select(self, downloads: Path, monkeypatch):
        launched: list[str] = []
        monkeypatch.setattr(actions.subprocess, "Popen",
                            lambda command, **kwargs: launched.append(command))
        path = touch(downloads, "invoice.pdf", 1_000.0)

        assert reveal_in_explorer(path) is True
        assert len(launched) == 1
        assert launched[0] == f'explorer /select,"{path}"'

    def test_the_permission_table_can_switch_it_off(self, downloads: Path, monkeypatch):
        """CLAUDE.md rule 4: the policy lives in guard.py, not in a second copy.

        If ``can_do_it`` stops allowing FIND_FILE, this function must stop
        acting -- so the test drives the real permission check rather than
        asserting that someone remembered to call it.
        """
        launched: list[str] = []
        monkeypatch.setattr(actions.subprocess, "Popen",
                            lambda command, **kwargs: launched.append(command))
        monkeypatch.setattr(actions.guard_mod, "can_do_it",
                            lambda task_type: task_type is not TaskType.FIND_FILE)
        path = touch(downloads, "invoice.pdf", 1_000.0)

        assert reveal_in_explorer(path) is False
        assert launched == []

    def test_the_module_has_no_way_to_delete_or_move(self):
        """A grep, deliberately: the promise is about what the code *cannot* do.

        "Read-only" in a docstring is a hope. This fails the moment someone
        adds the convenient one-liner that moves her file "somewhere tidier".
        """
        source = Path(actions.__file__).read_text(encoding="utf-8")
        for forbidden in ("os.remove", "os.unlink", "shutil.move", "shutil.rmtree",
                          ".unlink(", ".rmdir(", ".rename(", ".replace(",
                          ".write_text(", ".write_bytes("):
            assert forbidden not in source, f"actions.py must not {forbidden}"


class TestFindForHer:
    """The whole action, including the words she reads afterwards."""

    def test_the_message_says_when_it_is_only_the_newest(self, downloads: Path, monkeypatch):
        monkeypatch.setattr(actions.subprocess, "Popen", lambda command, **kwargs: None)
        touch(downloads, "receipt.pdf", 9_000.0)

        result = find_for_her("I can't find my file", downloads)

        assert result.opened is True
        assert "newest" in result.message
        assert "receipt.pdf" in result.message

    def test_the_message_is_plain_when_her_words_matched(self, downloads: Path, monkeypatch):
        monkeypatch.setattr(actions.subprocess, "Popen", lambda command, **kwargs: None)
        touch(downloads, "Electricity bill.pdf", 9_000.0)

        result = find_for_her("find the electricity bill", downloads)

        assert result.hit is not None and result.hit.because == "name"
        assert "Electricity bill.pdf" in result.message
        assert "newest" not in result.message

    def test_an_empty_folder_says_so_without_jargon(self, downloads: Path):
        result = find_for_her("anything", downloads)

        assert result.hit is None
        assert result.opened is False
        assert result.message == actions.NOTHING_FOUND

    def test_a_failed_launch_is_not_reported_as_success(self, downloads: Path, monkeypatch):
        monkeypatch.setattr(actions, "reveal_in_explorer", lambda path: False)
        touch(downloads, "receipt.pdf", 9_000.0)

        result = find_for_her("", downloads)

        assert result.opened is False
        assert "could not open" in result.message


class TestFileHit:
    def test_name_is_the_filename(self, tmp_path: Path):
        hit = FileHit(path=tmp_path / "a b.pdf", because="newest")
        assert hit.name == "a b.pdf"


# ===========================================================================
# The booking flow (MASTERSPEC 3B, P9)
# ===========================================================================

from datetime import date  # noqa: E402  (grouped with the booking tests)

from app import guard as guard_mod  # noqa: E402
from app.actions import (  # noqa: E402
    BookingFlow,
    HandoverRefused,
    field_bbox_px,
    parse_booking,
    parse_date,
    read_fields,
)

#: Sat 3 Oct 2026, so "tomorrow" and the year rollover are fixed facts rather
#: than whatever the day of the run happens to be.
TODAY = date(2026, 10, 3)

DEMO = Path(__file__).resolve().parents[1] / "demo_site"


class TestParseDate:
    def test_day_then_month(self):
        assert parse_date("on 12 October", TODAY) == "2026-10-12"

    def test_month_then_day(self):
        assert parse_date("on October 12", TODAY) == "2026-10-12"

    def test_ordinals(self):
        assert parse_date("on the 12th of October", TODAY) == "2026-10-12"

    def test_iso(self):
        assert parse_date("on 2026-11-02", TODAY) == "2026-11-02"

    def test_day_first_not_month_first(self):
        """She writes 12/11 meaning 12 November. Documented, not guessed."""
        assert parse_date("on 12/11/2026", TODAY) == "2026-11-12"

    def test_tomorrow(self):
        assert parse_date("book a train tomorrow", TODAY) == "2026-10-04"

    def test_today(self):
        assert parse_date("a train today", TODAY) == "2026-10-03"

    def test_a_past_month_means_next_year(self):
        """Nobody books a train into the past."""
        assert parse_date("on 3 January", TODAY) == "2027-01-03"

    def test_an_impossible_date_is_no_date(self):
        assert parse_date("on 31 February", TODAY) == ""

    def test_no_date_at_all(self):
        assert parse_date("book me a ticket to Pune") == ""


class TestParseBooking:
    def test_from_and_to(self):
        request = parse_booking("book a ticket from Pune to Mumbai on 12 October", TODAY)
        assert (request.origin, request.destination, request.date) == (
            "Pune", "Mumbai", "2026-10-12")
        assert request.has_route is True

    def test_destination_only(self):
        request = parse_booking("I want to book a ticket to Delhi tomorrow", TODAY)
        assert request.destination == "Delhi"
        assert request.origin == ""
        assert request.has_route is False

    def test_multi_word_stations(self):
        request = parse_booking("train from New Delhi to Agra Cantt on 2026-11-02", TODAY)
        assert request.origin == "New Delhi"
        assert request.destination == "Agra Cantt"

    def test_a_leading_article_is_dropped(self):
        request = parse_booking("book me a train from the airport to Pune", TODAY)
        assert request.origin == "airport"

    def test_nothing_understood_is_left_empty(self):
        """The honest failure: an empty box she fills herself."""
        request = parse_booking("book a ticket", TODAY)
        assert (request.origin, request.destination, request.date) == ("", "", "")


# --- against the real pages, in a real browser -----------------------------

playwright_api = pytest.importorskip("playwright.sync_api",
                                     reason="playwright is not installed")


@pytest.fixture(scope="module")
def browser():
    """One Chromium for the whole module.

    Headless *here* only. The flow she uses is headed (MASTERSPEC 3B: she
    watches); these tests are about the DOM guard's decisions, which do not
    depend on whether a window is on screen, and a visible browser per test
    would make the suite unrunnable.
    """
    with playwright_api.sync_playwright() as api:
        try:
            launched = api.chromium.launch(headless=True)
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium is not installed: {exc}")
        yield launched
        launched.close()


@pytest.fixture
def page(browser):
    opened = browser.new_page()
    yield opened
    opened.close()


def demo_url(name: str) -> str:
    """A ``file://`` URL for one demo page.

    The app serves the site over ``python -m http.server`` (MASTERSPEC 3B);
    the tests load the same files directly, because what is being tested is the
    DOM of these pages and a local HTTP server would add a port, a process and
    a race to every run without changing a single field.
    """
    return (DEMO / name).as_uri()


class TestDemoSitePages:
    """The pages themselves. If these change shape, the guard tests lie."""

    def test_every_page_says_it_is_a_demo(self):
        """MASTERSPEC 3B: say openly that this is not a real railway."""
        for name in ("index.html", "results.html", "login.html", "otp.html",
                     "payment.html", "done.html"):
            text = (DEMO / name).read_text(encoding="utf-8")
            assert "Demo site" in text, name

    def test_the_search_page_has_the_three_fields(self, page):
        page.goto(demo_url("index.html"))
        ids = {entry.id for entry in read_fields(page) if entry.visible}
        assert {"from", "to", "date"} <= ids


class TestDomGuardOnTheRealPages:
    """The P9 acceptance check, in a browser, on the pages we ship.

    A hand-written dictionary of fields would prove the rule; only this proves
    that the rule and the page agree.
    """

    def test_the_search_page_is_clear(self, page):
        page.goto(demo_url("index.html"))
        assert guard_mod.first_sensitive_field(read_fields(page)) is None

    def test_the_results_page_is_clear(self, page):
        page.goto(demo_url("results.html"))
        assert guard_mod.first_sensitive_field(read_fields(page)) is None

    def test_the_login_page_hands_over_the_password_not_the_username(self, page):
        page.goto(demo_url("login.html"))
        found = guard_mod.first_sensitive_field(read_fields(page))
        assert found is not None
        entry, reason = found
        assert entry.id == "password"
        assert reason == "password"

    def test_the_otp_page_hands_over(self, page):
        page.goto(demo_url("otp.html"))
        found = guard_mod.first_sensitive_field(read_fields(page))
        assert found is not None
        entry, reason = found
        assert entry.id == "otp"
        assert "one-time-code" in reason

    def test_the_payment_page_hands_over(self, page):
        page.goto(demo_url("payment.html"))
        found = guard_mod.first_sensitive_field(read_fields(page))
        assert found is not None
        assert found[0].id == "cardname"

    def test_every_card_field_is_refused_including_the_bare_cvv(self, page):
        """The CVV on that page has no autocomplete attribute at all."""
        page.goto(demo_url("payment.html"))
        reasons = {
            entry.id: guard_mod.sensitive_dom_field(entry)
            for entry in read_fields(page)
        }
        for name in ("cardname", "cardnumber", "expiry", "cvv"):
            assert reasons[name], f"{name} must be hers"
        assert reasons["cvv"].startswith(("label:", "name:"))

    def test_the_hidden_route_inputs_are_not_handed_over(self, page):
        """login.html carries from/to/date as hidden inputs. Not her secrets."""
        page.goto(demo_url("login.html") + "?from=Pune&to=Mumbai&date=2026-10-12")
        hidden = [entry for entry in read_fields(page) if not entry.visible]
        assert hidden, "the hidden inputs should be there to test"
        assert all(guard_mod.sensitive_dom_field(entry) == "" for entry in hidden)


class TestFieldBboxOnScreen:
    """Turning a field's place in the page into a circle on the screen."""

    def test_a_visible_field_gets_a_physical_pixel_box(self, page):
        page.goto(demo_url("login.html"))
        found = guard_mod.first_sensitive_field(read_fields(page))
        assert found is not None
        bbox = field_bbox_px(page, found[0])
        assert bbox is not None
        left, top, right, bottom = bbox
        assert right > left and bottom > top
        # A password box is wider than it is tall, in any browser chrome.
        assert (right - left) > (bottom - top)

    def test_a_zero_sized_field_gets_no_circle(self, page):
        """No circle beats a circle around nothing."""
        page.goto(demo_url("login.html"))
        nowhere = guard_mod.DomField(id="password", rect=(0.0, 0.0, 0.0, 0.0))
        assert field_bbox_px(page, nowhere) is None

    def test_the_screen_scale_is_applied_and_the_pages_own_is_ignored(self, page):
        """The bug this exists for: using window.devicePixelRatio.

        Playwright pins the page's ``devicePixelRatio`` to 1 whatever the
        display is doing, so a scale taken from the page silently drew the
        circle one field too high -- around the username box, on this very
        page. Scaling must come from the screen, which this proves by handing
        in two different scales and requiring the answer to follow them.
        """
        page.goto(demo_url("login.html"))
        found = guard_mod.first_sensitive_field(read_fields(page))
        assert found is not None
        at_one = field_bbox_px(page, found[0], screen_scale=1.0)
        at_two = field_bbox_px(page, found[0], screen_scale=2.0)

        assert at_one is not None and at_two is not None
        for single, double in zip(at_one, at_two):
            assert double == pytest.approx(single * 2.0)

    def test_the_measured_screen_scale_is_plausible(self):
        """``capture.screen_scale`` is the fallback when there is no Qt to ask.

        Not asserted to be 1.25: that is this laptop. Asserted to be a real
        scaling factor, because the failure mode it replaced was a confident
        1.0 on a 125% display.
        """
        from app import capture

        scale = capture.screen_scale()
        assert 1.0 <= scale <= 4.0


class Her:
    """Stands in for her, in the tests that need her to do her part.

    Passed as the flow's ``sleep``, so it acts on the same beat the flow polls
    on. Every few polls it does what she would do: fill the box it was handed
    and submit, which navigates the page and ends the handover.
    """

    #: What she does, in the order she meets it. Note the values: a fake
    #: password and 123456, because this is the demo site and there is nothing
    #: real to type (demo_site/login.html says so on the page).
    STEPS = (
        ("#password", "not-a-real-password", "#signin"),
        ("#otp", "123456", "#verify"),
        ("#cvv", "123", "#pay"),
    )

    def __init__(self, page, after: int = 2, up_to: tuple[str, ...] | None = None) -> None:
        self.page = page
        self.after = after
        #: Which handovers she takes. Limiting this is how a test says "and
        #: then she walked away", which is the case that must leave her form
        #: untouched rather than quietly filled in.
        self.up_to = up_to
        self.polls = 0
        self.filled: list[str] = []

    def __call__(self, seconds: float) -> None:
        self.polls += 1
        if self.polls % self.after:
            return
        for selector, value, submit in self.STEPS:
            if self.up_to is not None and selector not in self.up_to:
                continue
            if self.page.query_selector(selector) is None:
                continue
            self.page.fill(selector, value)
            self.filled.append(selector)
            self.page.click(submit)
            self.page.wait_for_load_state("load")
            return


class TestBookingFlow:
    """The acceptance check from PROMPTS.md P9, end to end on the demo site."""

    def flow(self, page, **kwargs) -> BookingFlow:
        handovers: list[tuple] = []
        resumes: list[int] = []
        built = BookingFlow(
            on_handover=lambda bbox, message: handovers.append((bbox, message)),
            on_resume=lambda: resumes.append(1),
            url=demo_url("index.html"),
            poll_ms=10,
            handover_timeout_s=kwargs.pop("handover_timeout_s", 2.0),
            **kwargs,
        )
        built.seen_handovers = handovers  # type: ignore[attr-defined]
        built.seen_resumes = resumes  # type: ignore[attr-defined]
        return built

    def test_it_fills_the_route_and_date_and_picks_the_first_train(self, page):
        her = Her(page)
        flow = self.flow(page, sleep=her, handover_timeout_s=5.0)

        outcome = flow.run("book a ticket from Pune to Mumbai on 2026-10-12", page)

        assert outcome.filled == {"from": "Pune", "to": "Mumbai",
                                  "date": "2026-10-12"}
        assert outcome.chose_train is True

    def test_it_stops_at_the_password_with_a_circle_and_the_right_words(self, page):
        """The demo moment: it circles her password box and waits."""
        flow = self.flow(page)

        outcome = flow.run("from Pune to Mumbai on 2026-10-12", page)

        assert outcome.ended == "handed_over"
        assert outcome.handovers[0] == "password"
        bbox, message = flow.seen_handovers[0]
        assert message == "You type your own password, I'll wait."
        assert bbox is not None and bbox[2] > bbox[0]

    def test_it_never_types_into_the_password_box(self, page):
        """The promise, checked against the page rather than the code."""
        flow = self.flow(page)
        flow.run("from Pune to Mumbai on 2026-10-12", page)

        assert page.input_value("#password") == ""

    def test_it_resumes_after_she_signs_in_and_stops_again_at_the_otp(self, page):
        her = Her(page)
        flow = self.flow(page, sleep=her, handover_timeout_s=5.0)

        outcome = flow.run("from Pune to Mumbai on 2026-10-12", page)

        assert "#password" in her.filled, "she should have signed in"
        assert outcome.handovers[:2] == ["password", "autocomplete:one-time-code"]
        assert flow.seen_resumes, "the circle should have come down"

    def test_it_stops_again_at_the_card_fields(self, page):
        """She signs in and enters the code, then stops. The card is hers.

        She deliberately does *not* fill the payment page here, so the run ends
        while that page is still open and the test can look at the card boxes
        it left behind. Letting her submit it would navigate to done.html and
        there would be nothing to check.
        """
        her = Her(page, up_to=("#password", "#otp"))
        flow = self.flow(page, sleep=her, handover_timeout_s=1.0)

        outcome = flow.run("from Pune to Mumbai on 2026-10-12", page)

        assert len(outcome.handovers) >= 3
        assert any("cc-" in reason or "card" in reason
                   for reason in outcome.handovers[2:])
        assert page.url.endswith("payment.html") or "payment.html" in page.url
        for box in ("#cardname", "#cardnumber", "#expiry", "#cvv"):
            assert page.input_value(box) == "", box

    def test_a_handover_she_never_takes_stops_the_flow(self, page):
        """She walked away. Tiny Me stops touching the form."""
        flow = self.flow(page, handover_timeout_s=0.2)

        outcome = flow.run("from Pune to Mumbai on 2026-10-12", page)

        assert outcome.ended == "handed_over"
        assert "yours" in outcome.message

    def test_stopping_ends_it_promptly(self, page):
        """Ctrl+Alt+P during a handover."""
        flow = self.flow(page, is_stopped=lambda: True)

        outcome = flow.run("from Pune to Mumbai on 2026-10-12", page)

        assert outcome.ended == "stopped"

    def test_an_ununderstood_goal_leaves_the_form_to_her(self, page):
        flow = self.flow(page)

        outcome = flow.run("book me something nice", page)

        assert outcome.filled == {}
        assert outcome.ended == "handed_over"
        assert "where you want to go" in outcome.message

    def test_fill_refuses_a_sensitive_field_by_name(self, page):
        """The second refusal: the one that survives a forgotten page check."""
        page.goto(demo_url("login.html"))
        flow = self.flow(page, handover_timeout_s=0.05)

        with pytest.raises(HandoverRefused):
            flow._fill(page, "#password", "nope")

        assert page.input_value("#password") == ""
