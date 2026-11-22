"""Tests of the provider, loaded by a real server."""

from music_assistant_models.enums import ProviderTier

from music_assistant.mass import MusicAssistant
from tests import DOMAIN


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY
