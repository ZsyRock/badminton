from pathlib import Path

from dotenv import dotenv_values

import booking_admin


def test_update_dotenv_is_atomic_and_preserves_unrelated_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP='yes'\nGYM_PASSWORD='old'\n", encoding="utf-8")
    env_file.chmod(0o600)

    booking_admin.update_dotenv(
        {
            "GYM_PASSWORD": "new'password\\part",
            "PRIMARY_BOOKING_ENABLED": "false",
        },
        env_file,
    )

    values = dotenv_values(env_file)
    assert values["KEEP"] == "yes"
    assert values["GYM_PASSWORD"] == "new'password\\part"
    assert values["PRIMARY_BOOKING_ENABLED"] == "false"
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_disabled_account_does_not_require_saved_credentials():
    slot = booking_admin.ACCOUNT_SLOT_BY_KEY["account_a"]

    assert not booking_admin.account_is_enabled(
        slot,
        {
            "PRIMARY_BOOKING_ENABLED": "false",
            "GYM_USERNAME": "",
            "GYM_PASSWORD": "",
        },
    )


def test_render_page_never_displays_password():
    page = booking_admin.render_page(
        {
            "GYM_USERNAME": "owner@example.com",
            "GYM_PASSWORD": "top-secret-password",
            "PRIMARY_BOOKING_ENABLED": "true",
        },
        "csrf-token",
    )

    assert "owner@example.com" in page
    assert "top-secret-password" not in page
    assert "3 active accounts" not in page
    assert "1 active account" in page


def test_empty_fallback_selection_is_preserved():
    assert booking_admin.parse_time_order("", booking_admin.FALLBACK_TIMES) == ()


def test_render_page_prefixes_public_form_actions():
    page = booking_admin.render_page(
        {
            "GYM_USERNAME": "owner@example.com",
            "GYM_PASSWORD": "secret",
            "PRIMARY_BOOKING_ENABLED": "true",
        },
        "csrf-token",
        base_path="/badmintonsoton",
    )

    assert 'action="/badmintonsoton/account/logout"' in page
    assert 'action="/badmintonsoton/rules"' in page


def test_admin_password_record_round_trip():
    record = booking_admin.build_admin_password_record(
        "correct-horse-battery-staple",
        salt=b"fixed-test-salt",
    )

    assert booking_admin.verify_admin_password(
        "correct-horse-battery-staple",
        record,
    )
    assert not booking_admin.verify_admin_password("incorrect-password", record)


def test_login_page_uses_short_root_action_without_exposing_hash():
    password_hash = booking_admin.build_admin_password_record(
        "correct-horse-battery-staple",
        salt=b"fixed-test-salt",
    )
    page = booking_admin.render_login_page()

    assert 'action="/login"' in page
    assert "Admin password" in page
    assert password_hash not in page


def test_control_page_includes_cancellation_notice_and_schedule(monkeypatch):
    monkeypatch.setattr(
        booking_admin,
        "render_playable_slots_summary",
        lambda _values: "<h2>Playable Slots Summary</h2><table></table>",
    )

    page = booking_admin.render_page({}, "csrf-token")

    assert "booking badminton slots eight days ahead" in page
    assert "retry at 08:00 and 16:00" in page
    assert (
        "<strong>If you cannot attend, please cancel at least four hours "
        "before the slot starts.</strong>"
    ) in page
    assert "<h2>Playable Slots Summary</h2><table></table>" in page
