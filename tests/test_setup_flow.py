"""Tests for the setup flow of the Fabel provider."""

from fabel.setup_flow import profile_options


def test_profile_options_use_id_as_value_and_name_as_title() -> None:
    """The picker stores the profile id and shows the name and type."""
    options = profile_options(
        [
            {"id": "prof-adult", "name": "Default", "type": "adult"},
            {"id": "prof-child", "name": "Kari", "type": "child"},
            {"id": "prof-unnamed", "type": "child"},
            {"name": "No id", "type": "child"},
        ]
    )

    assert [(o.value, o.title) for o in options] == [
        ("prof-adult", "Default (adult)"),
        ("prof-child", "Kari (child)"),
        ("prof-unnamed", "Profile (child)"),
    ]
