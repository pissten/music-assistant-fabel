"""Setup flow for the Fabel provider — SMS OTP (same UX as Fabel-appen / Min Konto)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.models.setup_flow import SetupFlowError

from .api import FabelApi
from .constants import (
    CONF_ACCESS_TOKEN,
    CONF_OTP_CODE,
    CONF_OTP_STATE,
    CONF_PHONE_NUMBER,
    CONF_PROFILE_ID,
    CONF_REFRESH_TOKEN,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


_PHONE_ENTRY = ConfigEntry(
    key=CONF_PHONE_NUMBER,
    type=ConfigEntryType.STRING,
    required=True,
    label="Mobilnummer",
    description="Norsk mobilnummer brukt i Fabel-appen (8 siffer eller +47…).",
)

_OTP_ENTRY = ConfigEntry(
    key=CONF_OTP_CODE,
    type=ConfigEntryType.STRING,
    required=True,
    label="SMS-kode",
    description="Engangskoden du får på SMS.",
)


async def run_setup(session: SetupSession) -> None:  # noqa: C901, PLR0915
    """Phone → SMS OTP → Eventyr tokens → pick profile."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)

    # --- Step 1: phone number ---
    while True:
        phone_entry = replace(
            _PHONE_ENTRY, value=setup_data.get(CONF_PHONE_NUMBER, _PHONE_ENTRY.value)
        )
        submitted = await session.form(
            [phone_entry], step_id="phone", errors=errors, last_step=False
        )
        setup_data.update(submitted)
        phone = str(setup_data.get(CONF_PHONE_NUMBER) or "").strip()
        if not phone:
            errors = {"base": "oppgi_mobilnummer"}
            continue

        api = FabelApi(session.mass.http_session, access_token="pending")  # noqa: S106
        try:
            state = await api.request_sms_otp(phone)
            setup_data[CONF_PHONE_NUMBER] = api.normalize_phone(phone)
            setup_data[CONF_OTP_STATE] = state
            setup_data.pop(CONF_OTP_CODE, None)
            # Keep state on the session context so retries/UI remounts still have it
            session.context.setup_data[CONF_PHONE_NUMBER] = setup_data[CONF_PHONE_NUMBER]
            session.context.setup_data[CONF_OTP_STATE] = state
            errors = None
            break
        except LoginFailed as err:
            errors = {"base": str(err)}
        except Exception as err:  # noqa: BLE001
            errors = {"base": str(err)}

    # --- Step 2: SMS code ---
    profiles: list[dict[str, Any]] = []
    while True:
        otp_entry = replace(_OTP_ENTRY, value="")
        submitted = await session.form([otp_entry], step_id="otp", errors=errors, last_step=False)
        setup_data.update(submitted)
        raw_code = str(submitted.get(CONF_OTP_CODE, setup_data.get(CONF_OTP_CODE)) or "")
        phone = str(
            setup_data.get(CONF_PHONE_NUMBER)
            or session.context.setup_data.get(CONF_PHONE_NUMBER)
            or ""
        )
        state = str(
            setup_data.get(CONF_OTP_STATE) or session.context.setup_data.get(CONF_OTP_STATE) or ""
        )

        api = FabelApi(session.mass.http_session, access_token="pending")  # noqa: S106
        try:
            tokens = await api.complete_sms_otp(phone, raw_code, state)
            setup_data[CONF_ACCESS_TOKEN] = tokens["access_token"]
            if tokens.get("refresh_token"):
                setup_data[CONF_REFRESH_TOKEN] = tokens["refresh_token"]
            # Validate against Eventyr streaming API
            await api.get_me()
            profiles = await api.get_profiles()
            errors = None
            break
        except LoginFailed as err:
            errors = {"base": str(err)}
        except Exception as err:  # noqa: BLE001
            errors = {"base": str(err)}

    if not profiles:
        raise SetupFlowError("ingen_profiler")

    options = [
        ConfigValueOption(
            f"{p.get('name') or 'Profil'} ({p.get('type') or '?'})",
            str(p.get("id")),
        )
        for p in profiles
        if p.get("id")
    ]
    preferred = next(
        (
            str(p["id"])
            for p in profiles
            if str(p.get("type") or "").lower() == "adult"
            or str(p.get("name") or "").lower() == "default"
        ),
        options[0].value if options else "",
    )
    existing = str(setup_data.get(CONF_PROFILE_ID) or preferred)

    profile_entry = ConfigEntry(
        key=CONF_PROFILE_ID,
        type=ConfigEntryType.STRING,
        required=True,
        label="Lytteprofil",
        description="Brukes i header x-profile-id på stream og bibliotek.",
        options=options,
        default_value=preferred,
        value=existing if existing in {o.value for o in options} else preferred,
    )

    while True:
        try:
            profile_values = await session.form(
                [profile_entry], step_id="profile", errors=errors, last_step=True
            )
            setup_data.update(profile_values)
            # Drop ephemeral OTP fields from persisted setup
            setup_data.pop(CONF_OTP_CODE, None)
            setup_data.pop(CONF_OTP_STATE, None)
            await session.finish(setup_data)
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}
            profile_entry = replace(profile_entry, value=setup_data.get(CONF_PROFILE_ID))
        else:
            return
