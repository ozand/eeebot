from nanobot_ops_dashboard.app import _display_timestamp


def test_display_timestamp_formats_in_msk():
    assert _display_timestamp('2026-04-23T15:00:00Z') == '2026-04-23 18:00:00 MSK'


def test_display_timestamp_handles_empty():
    assert _display_timestamp(None) == 'unknown'
