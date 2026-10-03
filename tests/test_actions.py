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
