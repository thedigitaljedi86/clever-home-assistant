"""Tests against a running Home Assistant: smart charging select and total energy."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.clever.const import DOMAIN
from custom_components.clever.exceptions import CleverApiError
from custom_components.clever.models import CleverCharger

CHARGE_BOX = "CB1"
INSTALLATION = {
    "installationId": "1", "chargeBoxId": CHARGE_BOX, "connectorId": 1,
    "chargeBoxType": {"name": "P40"}, "isOnline": True,
}


def transactions(session="s1", wh=0, revision=1, strategy="Smart"):
    """Firestore REST response with one active transaction."""
    return {"documents": [{"fields": {
        "sessionId": {"stringValue": session},
        "chargePointId": {"stringValue": CHARGE_BOX},
        "chargingStart": {"timestampValue": "2026-11-27T18:00:00Z"},
        "consumedWh": {"integerValue": str(wh)},
        "cpmsChargingStatus": {"stringValue": "Charging"},
        "chargingPlan": {"mapValue": {"fields": {
            "revisionNumber": {"integerValue": str(revision)},
            "segments": {"arrayValue": {"values": [{"mapValue": {"fields": {
                "start": {"timestampValue": "2026-11-27T18:00:00Z"},
                "end": {"timestampValue": "2026-11-27T19:00:00Z"},
                "reason": {"mapValue": {"fields": {"strategy": {"stringValue": strategy}}}},
            }}}]}},
        }}},
    }}]}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/clever in every test here."""
    yield


@pytest.fixture
def api():
    """Patch the Clever API client; tests change live_payload to simulate Firestore."""
    state = {"live": transactions()}
    client = "custom_components.clever.api.CleverApiClient"
    with (
        patch(f"{client}.async_get_installations",
              AsyncMock(side_effect=lambda: [CleverCharger.from_api(INSTALLATION)])),
        patch(f"{client}.async_get_charging_profiles", AsyncMock(return_value=[])),
        patch(f"{client}.async_get_active_transactions",
              AsyncMock(side_effect=lambda: state["live"])),
        patch(f"{client}.async_timebox_boost", AsyncMock()) as timebox,
        patch(f"{client}.async_unboost", AsyncMock()) as unboost,
        patch(f"{client}.async_ensure_token", AsyncMock()),
    ):
        yield {"state": state, "timebox": timebox, "unboost": unboost}


async def setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "a@b.dk", "refresh_token": "t"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_id(hass, platform, unique_id):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def refresh_live(hass, entry):
    await hass.data[DOMAIN][f"{entry.entry_id}_live"].async_refresh()
    await hass.async_block_till_done()


async def test_select_shows_command_until_plan_changes(hass, api):
    entry = await setup(hass)
    select = entity_id(hass, "select", f"{CHARGE_BOX}_1_smart_charging")
    assert hass.states.get(select).state == "enable"

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "disable_1h"}, blocking=True
    )
    api["timebox"].assert_awaited_once_with(CHARGE_BOX, 1, 60)
    # Clever has not updated its plan yet: the command is shown, not the old plan
    assert hass.states.get(select).state == "disable_1h"
    await refresh_live(hass, entry)
    assert hass.states.get(select).state == "disable_1h"

    # Clever's plan changes: from now on the live plan is the truth
    api["state"]["live"] = transactions(revision=2, strategy="Smart")
    await refresh_live(hass, entry)
    assert hass.states.get(select).state == "enable"


async def test_select_failure_is_reported(hass, api):
    await setup(hass)
    select = entity_id(hass, "select", f"{CHARGE_BOX}_1_smart_charging")
    api["unboost"].side_effect = CleverApiError("Clever API returned 504")
    with pytest.raises(HomeAssistantError, match="504"):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": select, "option": "enable"}, blocking=True
        )


async def test_total_energy_adds_up_sessions(hass, api):
    api["state"]["live"] = transactions(session="old", wh=9000)
    entry = await setup(hass)
    total = entity_id(hass, "sensor", "clever_energy_total")
    state = hass.states.get(total)
    # a session that was already there at install time is not counted
    assert float(state.state) == 0
    assert state.attributes["state_class"] == "total_increasing"
    assert state.attributes["unit_of_measurement"] == "kWh"

    for session, wh in (("s1", 1500), ("s1", 4000), ("s2", 500), ("s2", 2250)):
        api["state"]["live"] = transactions(session=session, wh=wh)
        await refresh_live(hass, entry)
    assert float(hass.states.get(total).state) == pytest.approx(6.25)

    # between sessions there is no live data, but the total stays available
    api["state"]["live"] = {}
    await refresh_live(hass, entry)
    assert float(hass.states.get(total).state) == pytest.approx(6.25)


async def test_live_entities_link_to_charger_device(hass, api):
    await setup(hass)
    registry = er.async_get(hass)
    total = registry.async_get(entity_id(hass, "sensor", "clever_energy_total"))
    select = registry.async_get(entity_id(hass, "select", f"{CHARGE_BOX}_1_smart_charging"))
    assert total.device_id is not None
    assert total.device_id == select.device_id
