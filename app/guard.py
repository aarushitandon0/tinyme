"""The guard: what Tiny Me must not look at and must not do (MASTERSPEC 4, 6).

CLAUDE.md rule 3: this runs *before* every model call and before every
automated action. Rule 4: the "do it for me" permissions live here, in code,
not in the prompt -- a model can be talked out of a rule in its system prompt,
and the whole promise of this app is that it cannot be talked into typing her
password.

Two independent questions live here, deliberately in one file so there is one
place to read the policy:

* :func:`is_sensitive` -- is this *screen* private? Nothing is sent to the model
  and nothing is clicked while it says yes.
* :func:`can_do_it` -- may "Do it for me" perform this *kind of task* at all?

The matching rule (the decision P7 asks us to make and document)
---------------------------------------------------------------
MASTERSPEC 6 lists the keywords and asks for case-insensitive, fuzzy matching.
Applied naively that pauses constantly: "bank" sits in "Bank holiday
list.docx" in her Downloads folder, and fuzzy-matching a 4-letter word puts
"Back" one slip away from "bank" on every browser toolbar. So:

1. Keywords are **tiered**. A *strong* keyword (password, OTP, CVV, card
   number, UPI PIN, net banking, passcode) means private on its own. A *weak*
   one (bank, sign in, login, expiry, card) only counts with a **second,
   different** weak keyword somewhere on screen -- which is MASTERSPEC 6's
   own "sign in (when combined with a password field)", generalised.
2. Fuzziness applies **only to keywords of 6 characters or more**, where OCR
   slips are the real risk and the words are distinctive enough to survive it.
   Short keywords ("otp", "cvv", "bank") are matched exactly on a word
   boundary, so "Back" is not "bank" and "Options" is not "OTP".

The asymmetry is intentional. Missing a password screen is a broken promise;
pausing on an ordinary one costs her a tap on "I did it". When in doubt this
module pauses.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Sequence

from app.brain import CheckType, StepPlan, SuccessCheck
from app.elements import Element

log = logging.getLogger(__name__)

#: What the overlay says when the guard stops us (MASTERSPEC 3B, 6).
HANDOVER_MESSAGE = "This part is yours. I'll wait."

#: One of these on screen is enough to stop everything.
STRONG_KEYWORDS: tuple[str, ...] = (
    "password",
    "passcode",
    "otp",
    "one time",
    "one time code",
    "one time password",
    "cvv",
    "cvc",
    "card number",
    "upi pin",
    "net banking",
    "netbanking",
    "security code",
    "verification code",
)

#: These mean something only in company: two *different* ones on the same
#: screen. See the module docstring for why.
WEAK_KEYWORDS: tuple[str, ...] = (
    "bank",
    "sign in",
    "signin",
    "log in",
    "login",
    "expiry",
    "expiration",
    "card",
    "credit card",
    "debit card",
    "payment",
    "pay now",
    "username",
    "account number",
)

#: Keywords at least this long are matched fuzzily; shorter ones exactly.
MIN_FUZZY_LEN = 6
#: Fuzzy threshold for those longer keywords. High: this absorbs a misread
#: character, not a different word.
FUZZY_RATIO = 85.0

_WORDS = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Lower-case, strip punctuation to single spaces.

    So "Card-Number:" and "CARD  NUMBER" both become "card number" and a
    multi-word keyword can be found by plain substring search.
    """
    return _WORDS.sub(" ", (text or "").casefold()).strip()


def _contains_keyword(haystack: str, keyword: str) -> bool:
    """Is this one keyword present in one already-normalised string?

    Multi-word keywords are a substring check on the normalised text. Single
    words are checked token by token, exactly when short and fuzzily when long
    (see the module docstring).
    """
    if not haystack:
        return False

    if " " in keyword:
        return keyword in haystack

    tokens = haystack.split()
    if keyword in tokens:
        return True
    if len(keyword) < MIN_FUZZY_LEN:
        return False

    from rapidfuzz import fuzz

    # Only compare against tokens of a plausible length, so "pass" does not
    # score highly against "password" by being a prefix of it.
    return any(
        abs(len(token) - len(keyword)) <= 2
        and fuzz.ratio(keyword, token) >= FUZZY_RATIO
        for token in tokens
    )


def _found(haystacks: Sequence[str], keywords: Iterable[str]) -> list[str]:
    """Which of these keywords appear anywhere in these strings."""
    return [keyword for keyword in keywords
            if any(_contains_keyword(text, keyword) for text in haystacks)]


@dataclass(frozen=True)
class Verdict:
    """The guard's answer about one screen.

    ``reason`` is for the log and for the Sentry span, and names the keywords
    that fired -- those are our own constants, never her screen text, so this
    is safe to log (CLAUDE.md rule 7).
    """

    sensitive: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.sensitive


CLEAR = Verdict(sensitive=False)


def is_sensitive(
    elements: Sequence[Element],
    window_title: str = "",
) -> Verdict:
    """Is this screen private? Called before every model call (CLAUDE.md rule 3).

    Reads only text: the element list we already built plus the foreground
    window title. No screenshot is inspected and nothing leaves the process.
    """
    haystacks = [normalise(window_title)]
    haystacks += [normalise(element.text) for element in elements]
    haystacks = [text for text in haystacks if text]

    strong = _found(haystacks, STRONG_KEYWORDS)
    if strong:
        reason = "strong:" + ",".join(sorted(strong))
        log.info("guard: screen held back (%s)", reason)
        return Verdict(sensitive=True, reason=reason)

    weak = _found(haystacks, WEAK_KEYWORDS)
    if len(weak) >= 2:
        reason = "weak:" + ",".join(sorted(weak))
        log.info("guard: screen held back (%s)", reason)
        return Verdict(sensitive=True, reason=reason)

    return CLEAR


# --- what "Do it for me" may do (MASTERSPEC 4) -----------------------------


class TaskType(Enum):
    """The kinds of task the permission table has an opinion about."""

    FIND_FILE = "find_file"
    OPEN_FOLDER = "open_folder"
    BROWSE_AND_FILL = "browse_and_fill"
    CREDENTIALS = "credentials"
    PAYMENT = "payment"
    DELETE = "delete"
    SEND = "send"
    #: Anything we did not recognise. MASTERSPEC 4: falls back to guide mode.
    UNKNOWN = "unknown"


#: The table from MASTERSPEC 4, as an allow-list. Anything absent is refused,
#: so adding a TaskType cannot accidentally grant it permission.
_ALLOWED: frozenset[TaskType] = frozenset({
    TaskType.FIND_FILE,
    TaskType.OPEN_FOLDER,
    TaskType.BROWSE_AND_FILL,
})


def can_do_it(task_type: Any) -> bool:
    """May "Do it for me" act on this task type? Default no.

    Accepts anything, including ``None`` and strings from outside, because the
    caller may be parsing her words. Anything that is not an allowed
    :class:`TaskType` is refused and the task falls back to guide mode.
    """
    allowed = isinstance(task_type, TaskType) and task_type in _ALLOWED
    if not allowed:
        log.info("guard: do-it refused for %r", getattr(task_type, "value", task_type))
    return allowed


def handover_plan(reason: str = "") -> StepPlan:
    """The "step" we show instead of calling the model on a private screen.

    No circle (``cannot_see_it``), not a finished goal: she still has a task in
    progress, and the loop keeps waiting until the screen is ordinary again.
    The success check is a placeholder that the paused path never runs -- the
    loop re-reads the screen and asks the guard again instead.
    """
    log.info("guard: handing over (%s)", reason or "unspecified")
    return StepPlan(
        instruction=HANDOVER_MESSAGE,
        target_id=0,
        success_check=SuccessCheck(type=CheckType.REGION_CHANGED, value=""),
        goal_reached=False,
        cannot_see_it=True,
        hint_if_missing=HANDOVER_MESSAGE,
    )
