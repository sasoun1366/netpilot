"""The Telegram release announcer.

`.github/telegram/announce.py` is a standalone script (it has to be: the workflow runs
it from a checkout of the tag being released), so these tests load it by path and drive
it the way GitHub Actions would.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ANNOUNCE_PATH = ROOT / ".github" / "telegram" / "announce.py"
SELFTEST = ROOT / ".github" / "telegram" / "telegram-test.py"


def _load():
    spec = importlib.util.spec_from_file_location("netpilot_announce", ANNOUNCE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


announce = _load()


def payload(**release_overrides) -> dict:
    release = {
        "tag_name": "v1.2.3",
        "html_url": "https://github.com/sasoun1366/netpilot/releases/tag/v1.2.3",
        "body": "",
        "assets": [],
    }
    release.update(release_overrides)
    return {
        "repository": {"name": "netpilot", "full_name": "sasoun1366/netpilot"},
        "release": release,
    }


# ── the shape of the post ───────────────────────────────────────────────────────────


def test_a_release_post_names_the_project_and_version():
    text = announce.build_message(payload(), "en")
    assert "<b>netpilot</b>" in text
    assert "<b>1.2.3</b>" in text
    assert "releases/tag/v1.2.3" in text


def test_assets_are_listed_with_sizes_and_links():
    text = announce.build_message(
        payload(
            assets=[
                {
                    "name": "netpilot-1.2.3-windows-desktop.exe",
                    "size": 51_700_000,
                    "browser_download_url": "https://github.com/dl/desktop.exe",
                }
            ]
        ),
        "fa",
    )
    assert "netpilot-1.2.3-windows-desktop.exe" in text
    assert "49.3 MB" in text
    assert 'href="https://github.com/dl/desktop.exe"' in text


def test_the_first_line_of_the_release_notes_becomes_the_summary():
    text = announce.build_message(
        payload(body="Fixes the freeze on Windows.\n\n## Details\n\nmore"), "en"
    )
    assert "Fixes the freeze on Windows." in text
    assert "more" not in text


def test_the_auto_generated_changelog_line_is_not_the_summary():
    """GitHub writes "Full Changelog: …" itself; the post links there anyway."""
    assert announce.summarise("**Full Changelog**: https://github.com/a/b/compare/v1...v2") == ""


def test_change_bullets_are_used_when_there_are_no_notes():
    text = announce.build_message(payload(), "fa", ["Fix the freeze", "Add tests"])
    assert "🛠 تغییرات:" in text
    assert "• Fix the freeze" in text


def test_html_in_the_release_notes_is_escaped():
    """Telegram's HTML mode is not forgiving: a stray < breaks the whole post."""
    text = announce.build_message(
        payload(body="Use <b>carefully</b> when latency > 100 ms & rising", assets=[]), "en"
    )
    assert "&lt;b&gt;carefully&lt;/b&gt;" in text
    assert "&gt; 100 ms &amp; rising" in text
    assert text.count("<b>") == 2, "only our own two tags may survive"


def test_both_languages_are_separated():
    text = announce.build_message(payload(body="Notes"), "both")
    assert "منتشر شد" in text and "is out" in text
    assert text.index("منتشر شد") < text.index("is out")


def test_a_missing_body_does_not_break_the_post():
    text = announce.build_message(payload(body=None), "fa")
    assert "🚀" in text and "دانلود" not in text


def test_versions_render_without_a_leading_v():
    assert "<b>0.1.1</b>" in announce.build_message(payload(tag_name="v0.1.1"), "en")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 B"), (999, "999 B"), (1024, "1.0 KB"), (51_700_000, "49.3 MB"), (None, "")],
)
def test_human_size(value, expected):
    assert announce.human_size(value) == expected


def test_the_test_message_carries_the_text_it_was_given(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "sasoun1366/netpilot")
    assert announce.build_test_message("hi", "en") == "✅ <b>netpilot</b> — hi"
    assert announce.build_test_message("سلام", "fa") == "✅ <b>netpilot</b> — سلام"


def test_a_bilingual_test_message_splits_on_the_separator(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "sasoun1366/netpilot")
    text = announce.build_test_message("فارسی || English", "both")
    assert "✅ <b>netpilot</b> — فارسی" in text
    assert "✅ <b>netpilot</b> — English" in text
    assert text.index("فارسی") < text.index("English")


def test_a_single_test_message_is_reused_for_both_languages(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "sasoun1366/netpilot")
    text = announce.build_test_message("hello", "both")
    assert text.count("hello") == 2, "with no separator the same text serves both blocks"


def test_an_empty_test_message_still_says_something(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "sasoun1366/netpilot")
    assert "آزمایشی" in announce.build_test_message("", "fa")


# ── driven the way Actions drives it ────────────────────────────────────────────────


def test_missing_secrets_skip_the_announcement_without_failing(tmp_path, monkeypatch, capsys):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload()), encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    code = announce.main(["--lang", "fa", "--event", str(event)])
    assert code == 0, "an unconfigured repository must not break its release"
    assert "::warning::" in capsys.readouterr().out


def test_a_manual_run_shows_the_latest_release(tmp_path, monkeypatch, capsys):
    """Pressing "Run workflow" with no test message should still show a real post."""
    event = tmp_path / "event.json"
    event.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GITHUB_REPOSITORY", "sasoun1366/netpilot")
    monkeypatch.setattr(announce, "latest_release", lambda *a, **k: payload(body="Notes"))
    assert announce.main(["--event", str(event), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "showing the latest release" in out
    assert "🚀" in out, "the post itself must be printed"


def test_no_release_anywhere_is_a_quiet_no_op(tmp_path, monkeypatch, capsys):
    event = tmp_path / "event.json"
    event.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert announce.main(["--event", str(event)]) == 0
    assert "nothing to announce" in capsys.readouterr().out


def test_no_release_in_the_event_is_a_quiet_no_op(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    event = tmp_path / "event.json"
    event.write_text("{}", encoding="utf-8")
    assert announce.main(["--event", str(event)]) == 0
    assert "nothing to announce" in capsys.readouterr().out


def test_the_end_to_end_self_test_passes():
    """The same script the workflow runs, against a local stand-in for the Bot API."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SELFTEST)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
