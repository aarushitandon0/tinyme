"""Tests for app.guard: the rules that keep her secrets off the wire.

CLAUDE.md rule 3 says the guard runs before every model call and every
automated action, so these are the tests that have to be right even when the
rest of the app is half-built. Two kinds of mistake matter, and they are not
symmetric:

* A **false negative** sends a password screen to the model. Unacceptable.
* A **false positive** pauses on an ordinary screen and says "this part is
  yours". Annoying, recoverable, and the side we lean to on purpose.

So the negative tests below are about the *common* annoyances (a file called
"Bank holiday list.docx"), not about squeezing out every possible pause.
"""
from app.elements import Element
from app.guard import (
    HANDOVER_MESSAGE,
    DomField,
    TaskType,
    can_do_it,
    first_sensitive_field,
    handover_message,
    handover_plan,
    is_sensitive,
    sensitive_dom_field,
)


def els(*texts: str) -> list[Element]:
    """A numbered element list, positions irrelevant: the guard reads text only."""
    return [
        Element(id=index, text=text, source="ocr", role="text",
                region="center", bbox_px=(100.0 + index, 100.0, 300.0, 124.0))
        for index, text in enumerate(texts, start=1)
    ]


class TestStrongKeywords:
    """One of these on screen is enough. No second signal required."""

    def test_a_password_label_is_sensitive(self):
        verdict = is_sensitive(els("Username", "Password", "Sign in"))
        assert verdict.sensitive is True
        assert "password" in verdict.reason

    def test_otp_is_sensitive_on_its_own(self):
        assert is_sensitive(els("Enter OTP")).sensitive is True

    def test_one_time_code_is_sensitive(self):
        assert is_sensitive(els("Enter the one-time code we sent you")).sensitive is True

    def test_cvv_is_sensitive(self):
        assert is_sensitive(els("CVV")).sensitive is True

    def test_card_number_is_sensitive(self):
        assert is_sensitive(els("Card number")).sensitive is True

    def test_upi_pin_is_sensitive(self):
        assert is_sensitive(els("Enter UPI PIN")).sensitive is True

    def test_net_banking_is_sensitive(self):
        assert is_sensitive(els("Net Banking")).sensitive is True

    def test_netbanking_written_as_one_word_is_sensitive(self):
        assert is_sensitive(els("Netbanking login")).sensitive is True

    def test_passcode_is_sensitive(self):
        assert is_sensitive(els("Device passcode")).sensitive is True

    def test_the_window_title_alone_can_trigger_it(self):
        verdict = is_sensitive(els("Continue"), window_title="Sign in - Password")
        assert verdict.sensitive is True

    def test_case_and_punctuation_do_not_matter(self):
        assert is_sensitive(els("PASSWORD:")).sensitive is True
        assert is_sensitive(els("card-number")).sensitive is True

    def test_an_ocr_slip_in_a_long_keyword_still_triggers(self):
        """MASTERSPEC 6 asks for fuzzy matching, and OCR does misread letters."""
        assert is_sensitive(els("Passwcrd")).sensitive is True


class TestWeakKeywords:
    """Words that mean trouble together and nothing on their own."""

    def test_a_bank_filename_in_explorer_does_not_trigger(self):
        """The documented rule: one weak word is not evidence. Explorer full of
        her own files would otherwise pause on every step."""
        verdict = is_sensitive(
            els("Bank holiday list.docx", "Recipes.docx", "photo.jpg"),
            window_title="Downloads",
        )
        assert verdict.sensitive is False
        assert verdict.reason == ""

    def test_sign_in_alone_does_not_trigger(self):
        assert is_sensitive(els("Sign in", "Create account")).sensitive is False

    def test_expiry_alone_does_not_trigger(self):
        assert is_sensitive(els("Expiry")).sensitive is False

    def test_two_weak_words_together_do_trigger(self):
        verdict = is_sensitive(els("Bank of somewhere", "Sign in to continue"))
        assert verdict.sensitive is True
        assert "bank" in verdict.reason and "sign in" in verdict.reason

    def test_the_same_weak_word_twice_is_still_one_signal(self):
        verdict = is_sensitive(els("Bank holiday list.docx", "Bank holidays 2026.pdf"))
        assert verdict.sensitive is False

    def test_a_payment_page_with_expiry_and_card_triggers(self):
        assert is_sensitive(els("Credit card", "Expiry", "Pay now")).sensitive is True


class TestOrdinaryScreens:
    def test_an_empty_screen_is_not_sensitive(self):
        assert is_sensitive([]).sensitive is False

    def test_explorer_showing_downloads_is_not_sensitive(self):
        verdict = is_sensitive(
            els("Downloads", "bill.pdf", "holiday photos", "Quick access"),
            window_title="Downloads",
        )
        assert verdict.sensitive is False

    def test_a_short_word_that_merely_resembles_a_keyword_does_not_trigger(self):
        """"back" is one letter from "bank" and sits on half the screens she
        uses, so short keywords are matched exactly, never fuzzily."""
        assert is_sensitive(els("Back", "Forward", "Refresh")).sensitive is False

    def test_options_does_not_read_as_otp(self):
        assert is_sensitive(els("Options", "Tools", "Help")).sensitive is False


class TestPermissions:
    """MASTERSPEC 4, in code rather than in the prompt (CLAUDE.md rule 4)."""

    def test_finding_and_opening_files_is_allowed(self):
        assert can_do_it(TaskType.FIND_FILE) is True
        assert can_do_it(TaskType.OPEN_FOLDER) is True

    def test_filling_non_sensitive_web_fields_is_allowed(self):
        assert can_do_it(TaskType.BROWSE_AND_FILL) is True

    def test_credentials_payment_delete_and_send_are_refused(self):
        for task in (TaskType.CREDENTIALS, TaskType.PAYMENT,
                     TaskType.DELETE, TaskType.SEND):
            assert can_do_it(task) is False, task

    def test_an_unknown_task_type_is_refused(self):
        assert can_do_it(TaskType.UNKNOWN) is False

    def test_a_task_type_we_have_never_heard_of_is_refused(self):
        """Unknown means guide mode, not "probably fine" (MASTERSPEC 4)."""
        assert can_do_it("transfer money") is False
        assert can_do_it(None) is False

    def test_every_task_type_has_an_explicit_answer(self):
        """A new TaskType must be decided here, not defaulted to allowed."""
        for task in TaskType:
            assert can_do_it(task) in (True, False)


class TestHandover:
    def test_the_handover_plan_circles_nothing_and_says_so(self):
        plan = handover_plan("password")
        assert plan.target_id == 0
        assert plan.cannot_see_it is True
        assert plan.instruction == HANDOVER_MESSAGE
        assert plan.hint_if_missing == HANDOVER_MESSAGE

    def test_the_handover_plan_is_not_a_finished_goal(self):
        """She still has a task in progress; the loop must keep waiting."""
        assert handover_plan("otp").goal_reached is False


# --- the browser guard (CLAUDE.md rule 5, P9) ------------------------------


def dom(**overrides) -> DomField:
    """An ordinary, visible text input unless a test says otherwise."""
    base = dict(tag="input", type="text", autocomplete="", name="", id="",
                placeholder="", label="", visible=True,
                rect=(100.0, 200.0, 240.0, 36.0))
    base.update(overrides)
    return DomField(**base)


class TestDomGuardFires:
    """The fields Tiny Me must never type into (CLAUDE.md rule 5).

    Each test names the *signal* it relies on, because real pages give you
    different ones: a password box always has the type, an OTP box usually
    only has the autocomplete, and a CVV box is often identified by nothing
    but its label.
    """

    def test_a_password_input_by_its_type(self):
        reason = sensitive_dom_field(dom(type="password", id="password"))
        assert reason == "password"

    def test_a_password_input_with_a_misleading_label(self):
        """The type is a fact about the field; the label is only a claim."""
        assert sensitive_dom_field(
            dom(type="password", label="Nickname", name="nickname")
        ) == "password"

    def test_a_card_number_by_its_autocomplete(self):
        reason = sensitive_dom_field(dom(autocomplete="cc-number", id="cardnumber"))
        assert reason.startswith("autocomplete:cc-")

    def test_every_cc_autocomplete_value_counts(self):
        for value in ("cc-name", "cc-number", "cc-exp", "cc-exp-month", "cc-csc"):
            assert sensitive_dom_field(dom(autocomplete=value)), value

    def test_a_one_time_code_by_its_autocomplete(self):
        """An OTP box is an ordinary text input. The attribute is the signal."""
        reason = sensitive_dom_field(dom(autocomplete="one-time-code", id="otp"))
        assert reason == "autocomplete:one-time-code"

    def test_a_cvv_by_its_label_alone(self):
        """demo_site/payment.html ships one of these on purpose.

        No autocomplete attribute, type="text", nothing but a label saying
        CVV. A guard that needed the attribute would type into it.
        """
        assert sensitive_dom_field(dom(label="CVV", id="cvv")) == "label:cvv"

    def test_an_otp_by_its_label_alone(self):
        assert "otp" in sensitive_dom_field(dom(label="One-time password (OTP)"))

    def test_a_upi_pin_by_its_label(self):
        assert sensitive_dom_field(dom(label="Enter UPI PIN"))

    def test_the_field_name_is_enough_when_there_is_no_label(self):
        assert sensitive_dom_field(dom(name="cvv")) == "name:cvv"

    def test_the_placeholder_is_enough(self):
        assert sensitive_dom_field(dom(placeholder="Card number"))

    def test_the_id_is_enough(self):
        """Written as one word, the way pages really write it."""
        assert sensitive_dom_field(dom(id="cardnumber")) == "name:card"

    def test_a_camel_case_id_is_enough(self):
        assert sensitive_dom_field(dom(id="userCardNumber")) == "name:card"

    def test_a_snake_case_name_is_enough(self):
        assert sensitive_dom_field(dom(name="otp_input")) == "name:otp"


class TestDomGuardStaysQuiet:
    """The fields it may fill. A guard that stops everywhere stops nothing."""

    def test_a_station_name_is_fillable(self):
        assert sensitive_dom_field(
            dom(id="from", label="From", placeholder="Station you are leaving from")
        ) == ""

    def test_a_date_of_travel_is_fillable(self):
        assert sensitive_dom_field(dom(type="date", id="date", label="Date of travel")) == ""

    def test_a_username_is_fillable(self):
        """MASTERSPEC 6 pauses on "sign in" plus a password *on screen*.

        Per-field, the username box itself is not hers to keep -- but note that
        the whole-page rule in actions.BookingFlow still stops the flow on a
        login page, because the password box next to it is sensitive. This test
        is about the field rule, not about the page.
        """
        assert sensitive_dom_field(
            dom(type="text", id="username", autocomplete="username", label="Username")
        ) == ""

    def test_discard_is_not_card(self):
        """The label is prose and matched whole-word; the name by prefix.

        "discard" *contains* "card" and must stay quiet, which is why
        identifiers are matched by prefix rather than by substring.
        """
        assert sensitive_dom_field(dom(label="Discard this draft", name="discard")) == ""

    def test_a_postcard_field_is_not_a_card_field(self):
        assert sensitive_dom_field(dom(name="postcard", label="Postcard message")) == ""

    def test_an_invisible_password_field_is_not_handed_over(self):
        """There is nothing to type into and nothing to draw a circle around.

        The demo site's hidden inputs carrying the route between pages are
        this case; so is every framework's off-screen template field.
        """
        assert sensitive_dom_field(dom(type="password", visible=False)) == ""

    def test_a_plain_search_box_is_fillable(self):
        assert sensitive_dom_field(dom(name="q", placeholder="Search")) == ""


class TestFirstSensitiveField:
    def test_returns_the_first_in_document_order(self):
        """On a login page that is the password box, not the username above it."""
        fields = [
            dom(id="username", label="Username"),
            dom(id="password", type="password", label="Password"),
            dom(id="otp", autocomplete="one-time-code"),
        ]
        found = first_sensitive_field(fields)
        assert found is not None
        chosen, reason = found
        assert chosen.id == "password"
        assert reason == "password"

    def test_none_when_the_page_is_ordinary(self):
        assert first_sensitive_field([dom(id="from"), dom(id="to")]) is None

    def test_none_for_an_empty_page(self):
        assert first_sensitive_field([]) is None


class TestHandoverWords:
    """What she reads. MASTERSPEC 3B quotes the password line exactly."""

    def test_the_password_line_is_the_one_in_the_spec(self):
        assert handover_message("password") == "You type your own password, I'll wait."

    def test_an_otp_is_not_called_a_password(self):
        message = handover_message("autocomplete:one-time-code")
        assert "phone" in message
        assert "password" not in message.casefold()

    def test_card_fields_are_called_card_details(self):
        assert "card" in handover_message("autocomplete:cc-number").casefold()

    def test_a_cvv_found_by_its_label_still_says_card(self):
        assert "card" in handover_message("label:cvv").casefold()

    def test_every_message_promises_to_wait(self):
        for reason in ("password", "autocomplete:one-time-code",
                       "autocomplete:cc-number", "label:cvv", "label:upi"):
            assert "wait" in handover_message(reason).casefold(), reason
