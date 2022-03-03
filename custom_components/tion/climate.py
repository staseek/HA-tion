"""Adds support for generic thermostat units."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import TEMP_CELSIUS
import voluptuous as vol

from homeassistant.components.climate import ClimateEntity

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TionInstance
from .const import *

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Required(CONF_MAC): cv.string,
        vol.Optional(CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_KEEP_ALIVE, default=30): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(
            [HVAC_MODE_FAN_ONLY, HVAC_MODE_HEAT, HVAC_MODE_OFF]
        ),
        vol.Optional(CONF_AWAY_TEMP): vol.Coerce(float),
    }
)

devices = []


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """Setup entry"""
    tion_instance: TionInstance = hass.data[DOMAIN][config_entry.unique_id]
    unique_id = tion_instance.unique_id

    if unique_id not in devices:
        devices.append(unique_id)
        async_add_entities([TionClimateEntity(hass, tion_instance)])
    else:
        _LOGGER.warning(f"Device {unique_id} is already configured! ")
    return True


class TionClimateEntity(ClimateEntity, CoordinatorEntity):
    """Representation of a Tion device."""

    _attr_hvac_modes = [HVAC_MODE_HEAT, HVAC_MODE_FAN_ONLY, HVAC_MODE_OFF]
    _attr_min_temp = 0
    _attr_max_temp = 30
    _attr_fan_modes = [1, 2, 3, 4, 5, 6]
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 1
    _attr_temperature_unit = TEMP_CELSIUS
    _attr_preset_modes = [PRESET_NONE, PRESET_BOOST, PRESET_SLEEP]
    _attr_preset_mode = PRESET_NONE
    _attr_supported_features = SUPPORT_FLAGS | SUPPORT_PRESET_MODE
    _attr_icon = 'mdi:air-purifier'
    coordinator: TionInstance

    def __init__(self, hass: HomeAssistant, instance: TionInstance):
        CoordinatorEntity.__init__(
            self=self,
            coordinator=instance,
        )
        self.hass: HomeAssistant = hass
        self._away_temp = self.coordinator.away_temp

        # saved states
        self._last_mode = None
        self._saved_target_temp = None
        self._saved_fan_mode = None

        # current state
        self._target_temp = None
        self._is_boost: bool = False
        self._fan_speed = 1

        if self._away_temp:
            self._attr_preset_modes.append(PRESET_AWAY)

        self._attr_device_info = self.coordinator.device_info
        self._attr_name = self.coordinator.name
        self._attr_unique_id = self.coordinator.unique_id

        self._get_current_state()
        ClimateEntity.__init__(self)

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.
        Need to be one of CURRENT_HVAC_*.
        """

        self.coordinator.logger.debug(f"state is {self.coordinator.data.get('is_on')} "
                                      f"heating is {self.coordinator.data.get('is_heating')}")
        if self.coordinator.data.get("is_on"):
            if self.coordinator.data.get("is_heating"):
                current_hvac_operation = CURRENT_HVAC_HEAT
            else:
                current_hvac_operation = CURRENT_HVAC_FAN
        else:
            current_hvac_operation = CURRENT_HVAC_OFF
        return current_hvac_operation

    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        _LOGGER.info("Need to set mode to %s, current mode is %s", hvac_mode, self.hvac_mode)
        if self.hvac_mode == hvac_mode:
            # Do nothing if mode is same
            _LOGGER.debug(f"{self.name} is asked for mode {hvac_mode}, but it is already in {self.hvac_mode}. Do "
                          f"nothing.")
            pass
        elif hvac_mode == HVAC_MODE_OFF:
            # Keep last mode while turning off. May be used while calling climate turn_on service
            self._last_mode = self.hvac_mode
            await self._async_set_state(is_on=False)

        elif hvac_mode == HVAC_MODE_HEAT:
            saved_target_temp = self.target_temperature
            try:
                await self.coordinator.connect()
                await self._async_set_state(heater=True, is_on=True)
                if self.hvac_mode == HVAC_MODE_FAN_ONLY:
                    await self.async_set_temperature(**{ATTR_TEMPERATURE: saved_target_temp})
            finally:
                await self.coordinator.disconnect()
        elif hvac_mode == HVAC_MODE_FAN_ONLY:
            await self._async_set_state(heater=False, is_on=True)

        else:
            _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
            return
        # Ensure we update the current operation after changing the mode
        await self._handle_coordinator_update()

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        actions = []
        _LOGGER.debug("Going to change preset mode from %s to %s", self.preset_mode, preset_mode)
        if preset_mode == PRESET_AWAY and self.preset_mode != PRESET_AWAY:
            _LOGGER.info("Going to AWAY mode. Will save target temperature %s", self.target_temperature)
            self._saved_target_temp = self.target_temperature
            actions.append([self._async_set_state, {'heater_temp': self._away_temp}])

        if preset_mode != PRESET_AWAY and self.preset_mode == PRESET_AWAY and self._saved_target_temp:
            # retuning from away mode
            _LOGGER.info("Returning from AWAY mode: will set saved temperature %s", self._saved_target_temp)
            actions.append([self._async_set_state, {'heater_temp': self._saved_target_temp}])
            self._saved_target_temp = None

        if preset_mode == PRESET_SLEEP and self.preset_mode != PRESET_SLEEP:
            _LOGGER.info("Going to night mode: will save fan_speed: %s", self.fan_mode)
            if self._saved_fan_mode is None:
                self._saved_fan_mode = int(self.fan_mode)
            actions.append([self.async_set_fan_mode, {'fan_mode': min(int(self.fan_mode), self.sleep_max_fan_mode)}])

        if preset_mode == PRESET_BOOST and not self._is_boost:
            self._is_boost = True
            if self._saved_fan_mode is None:
                self._saved_fan_mode = int(self.fan_mode)
            actions.append([self.async_set_fan_mode, {'fan_mode': self.boost_fan_mode}])

        if self.preset_mode in [PRESET_BOOST, PRESET_SLEEP] and preset_mode not in [PRESET_BOOST, PRESET_SLEEP]:
            # returning from boost or sleep mode
            _LOGGER.info("Returning from %s mode. Going to set fan speed %d", self.preset_mode, self._saved_fan_mode)
            if self.preset_mode == PRESET_BOOST:
                self._is_boost = False

            if self._saved_fan_mode is not None:
                actions.append([self.async_set_fan_mode, {'fan_mode': self._saved_fan_mode}])
                self._saved_fan_mode = None

        self._attr_preset_mode = preset_mode
        try:
            await self.coordinator.connect()
            for a in actions:
                await a[0](**a[1])
            self._attr_preset_mode = preset_mode
            await self._handle_coordinator_update()
        finally:
            await self.coordinator.disconnect()

        await self._handle_coordinator_update()

    @property
    def boost_fan_mode(self) -> int:
        """Fan speed for boost mode

        :return: maximum of supported fan_modes
        """
        return max([int(x) for x in self.fan_modes])

    @property
    def sleep_max_fan_mode(self) -> int:
        """Maximum fan speed for sleep mode"""
        return 2

    async def async_set_fan_mode(self, fan_mode):
        if self.preset_mode == PRESET_SLEEP:
            if int(fan_mode) > self.sleep_max_fan_mode:
                _LOGGER.info("Fan speed %s was required, but I'm in SLEEP mode, so it should not be greater than %d",
                             self.sleep_max_fan_mode)
                fan_mode = self.sleep_max_fan_mode

        if (self.preset_mode == PRESET_BOOST and self._is_boost) and fan_mode != self.boost_fan_mode:
            _LOGGER.debug("I'm in boost mode. Will ignore requested fan speed %s" % fan_mode)
            fan_mode = self.boost_fan_mode
        if fan_mode != self.fan_mode or not self.coordinator.data.get("is_on"):
            self._fan_speed = fan_mode
            await self._async_set_state(fan_speed=fan_mode, is_on=True)

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._target_temp = temperature
        await self._async_set_state(heater_temp=temperature)

    async def async_turn_on(self):
        """
        Turn breezer on. Tries to restore last state. Use HEAT as backup
        """
        _LOGGER.debug(f"Turning on from {self.hvac_mode} to {self._last_mode}")
        if self.hvac_mode != HVAC_MODE_OFF:
            # do nothing if we already working
            pass
        elif self._last_mode is None:
            await self.async_set_hvac_mode(HVAC_MODE_HEAT)
        else:
            await self.async_set_hvac_mode(self._last_mode)

    async def async_turn_off(self):
        _LOGGER.debug(f"Turning off from {self.hvac_mode}")
        await self.async_set_hvac_mode(HVAC_MODE_OFF)

    async def _async_set_state(self, **kwargs):
        await self.coordinator.set(**kwargs)
        await self._handle_coordinator_update()

    async def _handle_coordinator_update(self) -> None:
        self._get_current_state()
        if self.fan_mode != self.boost_fan_mode and (self._is_boost or self.preset_mode == PRESET_BOOST):
            _LOGGER.warning(f"I'm in boost mode, but current speed {self.fan_mode} is not equal boost speed "
                            f"{self.boost_fan_mode}. Dropping boost mode")
            self._is_boost = False
            self._attr_preset_mode = PRESET_NONE

        self.async_write_ha_state()

    def _get_current_state(self):
        self._attr_target_temperature = self.coordinator.data.get("heater_temp")
        self._attr_current_temperature = self.coordinator.data.get("out_temp")
        self._attr_fan_mode = self.coordinator.data.get("fan_speed")
        self._attr_assumed_state = False if self.coordinator.last_update_success else True
        self._attr_extra_state_attributes = {
            'air_mode': self.coordinator.data.get("air_mode"),
            'in_temp': self.coordinator.data.get("in_temp")
        }
        self._attr_hvac_mode = HVAC_MODE_OFF if not self.coordinator.data.get("is_on") else \
            HVAC_MODE_HEAT if self.coordinator.data.get("heater") else HVAC_MODE_FAN_ONLY

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True
