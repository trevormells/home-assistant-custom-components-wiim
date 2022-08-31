"""
Support for WiiM Mini devices.

For more details about this platform, please refer to the documentation at
https://github.com/onlyoneme/home-assistant-custom-components-wiim
"""

import asyncio
import async_timeout
import voluptuous as vol

from datetime import timedelta
import logging

import string
import aiohttp
from http import HTTPStatus
from aiohttp.client_exceptions import ClientError

from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.aiohttp import AiohttpRequester
import xml.etree.ElementTree as ET

from homeassistant.util import Throttle
from homeassistant.util.dt import utcnow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from homeassistant.components.media_player import (
    PLATFORM_SCHEMA, 
    MediaPlayerEntity,
    MediaPlayerDeviceClass,
    MediaPlayerEntityFeature,	
)

from homeassistant.components import media_source
from homeassistant.components.media_player.browse_media import (
    async_process_play_media_url,
)

from homeassistant.components.media_player.const import (
    MEDIA_TYPE_MUSIC,
    MEDIA_TYPE_URL,
    REPEAT_MODE_ALL,
    REPEAT_MODE_OFF,
    REPEAT_MODE_ONE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_DEVICE_CLASS,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
)


from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

ICON_DEFAULT = 'mdi:speaker'
ICON_PLAYING = 'mdi:speaker-wireless'
ICON_MUTED = 'mdi:speaker-off'
ICON_PUSHSTREAM = 'mdi:cast-audio'

ATTR_FWVER = 'firmware'
ATTR_TRCNT = 'pl_tracks'
ATTR_TRCRT = 'pl_track_current'
ATTR_TRCRC = 'track_current'
ATTR_STURI = 'stream_uri'
ATTR_UUID = 'uuid'
ATTR_DEBUG = 'debug_info'
ATTR_BITRATE = 'bit_rate'
ATTR_SAMPLERATE = 'sample_rate'
ATTR_DEPTH = 'bit_depth'

CONF_NAME = 'name'
CONF_UUID = 'uuid'

DEBUGSTR_ATTR = True
MAX_VOL = 100

UPNP_TIMEOUT = 2
API_TIMEOUT = 2

UNA_THROTTLE = timedelta(seconds=20)
CONNECT_PAUSED_TIMEOUT = timedelta(seconds=300)
AUTOIDLE_STATE_TIMEOUT = timedelta(seconds=1)

SOURCES_MAP = {'-1': 'Idle', 
               '0': 'Idle', 
               '1': 'Airplay', 
               '2': 'DLNA',
               '3': 'Amazon',
               '10': 'Network',
               '20': 'Network',			   
               '31': 'Spotify',
               '32': 'TIDAL',			   
               '99': 'Idle'}

SOURCES_IDLE = ['-1', '0', '99']
SOURCES_STREAM = ['1', '2', '3', '10', '20']
SOURCES_CONNECT = ['31', '32']

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_UUID, default=''): cv.string,
    }
)




class WiiMData:
    """Storage class for platform global data."""
    def __init__(self):
        """Initialize the data."""
        self.entities = []

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the WiiM platform."""

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = WiiMData()

    name = config.get(CONF_NAME)
    host = config.get(CONF_HOST)
    uuid = config.get(CONF_UUID)

    state = STATE_IDLE

    initurl = "https://{0}/httpapi.asp?command=getStatusEx".format(host)
    
    try:
        websession = async_get_clientsession(hass)
        response = await websession.get(initurl, ssl=False)

        if response.status == HTTPStatus.OK:
            data = await response.json(content_type=None)
            _LOGGER.debug("HOST: %s DATA response: %s", host, data)

            try:
                uuid = data['uuid']
            except KeyError:
                pass

            if name == None:
                try:
                    name = data['DeviceName']
                except KeyError:
                    pass

        else:
            _LOGGER.warning(
                "Get Status UUID failed, response code: %s Full message: %s",
                response.status,
                response,
            )
            state = STATE_UNAVAILABLE

    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        _LOGGER.warning(
            "Failed communicating with WiiM (start) '%s': uuid: %s %s", host, uuid, type(error)
        )
        state = STATE_UNAVAILABLE

    wiim = WiiMDevice(name, 
                            host, 
                            uuid,
                            state,
                            hass)

    async_add_entities([wiim])
		
class WiiMDevice(MediaPlayerEntity):
    """WiiM Player Object."""

    def __init__(self, 
                 name, 
                 host, 
                 uuid,
                 state,
                 hass):
        """Initialize the media player."""
        self._uuid = uuid
        self._fw_ver = '1.0.0'
        requester = AiohttpRequester(UPNP_TIMEOUT)
        self._factory = UpnpFactory(requester, disable_unknown_out_argument_error=True)
        self._upnp_device = None
        self._service = None
        self._features = None
        self._preset_key = 6
        self._name = name
        self._host = host
        self._icon = ICON_DEFAULT
        self._state = state
        self._volume = 0
        self._source = None

        self._muted = False
        self._playhead_position = 0
        self._duration = 0
        self._position_updated_at = None
        self._connect_paused_at = None
        self._idletime_updated_at = None
        self._shuffle = False
        self._repeat = REPEAT_MODE_OFF
        self._media_album = None
        self._media_artist = None
        self._media_prev_artist = None
        self._media_title = None
        self._media_prev_title = None
        self._media_image_url = None
        self._media_uri = None
        self._media_uri_final = None
        self._player_statdata = {}
        self._first_update = True

        self._pl_tracks = None
        self._pl_trackc = None
        self._trackc = None

        self._playing_stream = False
        self._playing_idle = True
        self._playing_connect = False
        self._playing_mediabrowser = False
  
        self._unav_throttle = False
        self._samplerate = None
        self._bitrate = None
        self._bitdepth = None

    async def async_added_to_hass(self):
        """Record entity."""
        self.hass.data[DOMAIN].entities.append(self)

		
    async def call_wiim_httpapi(self, cmd, jsn):
        """Get the latest data from HTTPAPI service."""
        _LOGGER.debug("For: %s  cmd: %s  jsn: %s", self._name, cmd, jsn)
        url = "https://{0}/httpapi.asp?command={1}".format(self._host, cmd)
        
        if self._first_update:
            timeout = 10
        else:
            timeout = API_TIMEOUT
        
        try:
            websession = async_get_clientsession(self.hass)
            async with async_timeout.timeout(timeout):
                response = await websession.get(url, ssl=False)

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.warning(
                "Failed communicating with WiiM (httpapi) '%s': %s", self._name, type(error)
            )
            return False

        if response.status == HTTPStatus.OK:
            if jsn:
                data = await response.json(content_type=None)
            else:
                data = await response.text()
                _LOGGER.debug("For: %s  cmd: %s  resp: %s", self._name, cmd, data)
        else:
            _LOGGER.error(
                "For: %s (%s) Get failed, response code: %s Full message: %s",
                self._name,
                self._host,
                response.status,
                response,
            )
            return False

        return data

		
    @Throttle(UNA_THROTTLE)
    async def async_get_status(self):
        resp = await self.call_wiim_httpapi("getPlayerStatus", True)
        if resp is False:
            _LOGGER.debug('Unable to connect to device: %s, %s', self.entity_id, self._name)
            self._state = STATE_UNAVAILABLE
            self._unav_throttle = True
            self._playhead_position = None
            self._duration = None
            self._position_updated_at = None
            self._media_title = None
            self._media_artist = None
            self._media_album = None
            self._media_image_url = None
            self._media_uri = None
            self._media_uri_final = None
            self._trackc = None
            self._pl_tracks = None
            self._pl_trackc = None
            self._playing_mediabrowser = False
            self._playing_stream = False
            self._playing_idle = True
            self._playing_connect = False
            self._source = None
            self._upnp_device = None
            self._first_update = True
            self._player_statdata = None
            self._service = None
            self._icon = ICON_DEFAULT
            self._samplerate = None
            self._bitrate = None
            self._bitdepth = None
            self._features = None		
            return
        self._player_statdata = resp.copy()
		
    async def async_trigger_schedule_update(self, before):
        await self.async_schedule_update_ha_state(before)	


    async def async_update(self):
        """Update state."""
        #_LOGGER.debug("01 Start update %s, %s", self.entity_id, self._name)


        if self._unav_throttle:
            await self.async_get_status()
        else:
            await self.async_get_status(no_throttle=True)

        if self._player_statdata is None:
            _LOGGER.debug("First update/No response from api: %s, %s", self.entity_id, self._player_statdata)
            return

        if isinstance(self._player_statdata, dict):
            self._unav_throttle = False
            if self._first_update or (self._state == STATE_UNAVAILABLE):
                #_LOGGER.debug("03 Update first time getStatus %s, %s", self.entity_id, self._name)
                device_status = await self.call_wiim_httpapi("getStatusEx", True)
                if device_status is not None:
                    if isinstance(device_status, dict):
                        if self._state == STATE_UNAVAILABLE:
                            self._state = STATE_IDLE
                        
                        try:
                            self._uuid = device_status['uuid']
                        except KeyError:
                            pass

                        try:
                            self._name = device_status['DeviceName']
                        except KeyError:
                            pass

                        try:
                            self._fw_ver = device_status['firmware']
                        except KeyError:
                            self._fw_ver = '1.0.0'							

                        try:
                            self._preset_key = int(device_status['preset_key'])
                        except KeyError:
                            self._preset_key = 6

                        if self._upnp_device is None: # and self._name is not None:
                            url = "http://{0}:49152/description.xml".format(self._host)
                            try:
                                self._upnp_device = await self._factory.async_create_device(url)
                            except:
                                _LOGGER.warning(
                                    "Failed communicating with WiiM (UPnP) '%s': %s", self._name, type(error)
                                )

                        if self._first_update:
                            self._duration = 0
                            self._playhead_position = 0
                            self._idletime_updated_at = utcnow()
                            self._first_update = False

            self._position_updated_at = utcnow()

            self._pl_tracks = self._player_statdata['plicount']
            self._pl_trackc = self._player_statdata['plicurr']

            #_LOGGER.debug("04 Update VOL, Shuffle, Repeat, STATE %s, %s", self.entity_id, self._name)
            self._volume = self._player_statdata['vol']
            self._muted = bool(int(self._player_statdata['mute'])) 

            self._shuffle = {
                '2': True,
                '3': True,
            }.get(self._player_statdata['loop'], False)

            self._repeat = {
                '0': REPEAT_MODE_ALL,
                '1': REPEAT_MODE_ONE,
                '2': REPEAT_MODE_ALL,
            }.get(self._player_statdata['loop'], REPEAT_MODE_OFF)

            
            if self._player_statdata['mode'] in SOURCES_IDLE or self._player_statdata['status'] in ['stop', 'none']: 
                if utcnow() >= (self._idletime_updated_at + AUTOIDLE_STATE_TIMEOUT):
                    self._state = STATE_IDLE
                    #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)
            elif self._player_statdata['status'] in ['play', 'load']:
                self._state = STATE_PLAYING
                #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)
            elif self._player_statdata['status'] == 'pause':
                self._state = STATE_PAUSED
                #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)

            if self._state in [STATE_PLAYING, STATE_PAUSED]:
                self._duration = int(int(self._player_statdata['totlen']) / 1000)
                self._playhead_position = int(int(self._player_statdata['curpos']) / 1000)
                #_LOGGER.debug("04 Update DUR, POS %s, %s, %s, %s, %s", self.entity_id, self._name, self._state, self._duration, self._playhead_position)
            else:
                self._duration = 0
                self._playhead_position = 0

            #_LOGGER.debug("05 Update self._playing_whatever %s, %s", self.entity_id, self._name)
            self._playing_connect = self._player_statdata['mode'] in SOURCES_CONNECT		
            self._playing_idle = self._player_statdata['mode'] in SOURCES_IDLE
            self._playing_stream = self._player_statdata['mode'] in SOURCES_STREAM

            self._playing_mediabrowser = bool(self._player_statdata['mode'] in ['10', '20'])


            self._source = SOURCES_MAP.get(self._player_statdata['mode'], 'Network')			


            if self._source != 'Network' and not (self._playing_stream or self._playing_connect):
                #_LOGGER.debug("08 Line Inputs: %s, %s", self.entity_id, self._name)
                if self._source == 'Idle':
                    self._state = STATE_IDLE
                    self._media_title = None
                else:
                    self._state = STATE_PLAYING
                    self._media_title = self._source

                self._media_artist = None
                self._media_album = None
                self._media_image_url = None

            if self._player_statdata['mode'] in ['1', '2', '3']:
                #_LOGGER.debug("08 Line Inputs name playing: %s, %s", self.entity_id, self._name)
                self._state = STATE_PLAYING
                self._media_title = self._source

            if self._playing_connect and self._state == STATE_IDLE:
                self._source = None

            if self._connect_paused_at != None:
                if utcnow() >= (self._connect_paused_at + CONNECT_PAUSED_TIMEOUT):
                    # Prevent sticking in Pause mode for a long time (Spotify doesn't have a stop button on the app)
                    await self.async_media_stop()
                    return


            if self._playing_connect:
                #_LOGGER.debug("09 it's playing spotifty: %s, %s", self.entity_id, self._name)
                if self._state != STATE_IDLE:
                    await self.async_update_via_upnp()
                if self._state == STATE_PAUSED:
                    if self._connect_paused_at == None:
                        self._connect_paused_at = utcnow()
                else:
                    self._connect_paused_at = None

            elif self._playing_stream:
                if self._state != STATE_IDLE:
                    await self.async_update_via_upnp()
                self._connect_paused_at = None
            else:
                #_LOGGER.debug("09 it's playing something else: %s, %s", self.entity_id, self._name)
                self._connect_paused_at = None
                if self._state not in [STATE_PLAYING, STATE_PAUSED]:
                    self._media_title = None
                    self._media_artist = None
                    self._media_album = None
                    self._media_image_url = None

            self._media_prev_artist = self._media_artist
            self._media_prev_title = self._media_title

        else:
            _LOGGER.error("Erroneous JSON during update and process self._player_statdata: %s, %s", self.entity_id, self._name)



        return True


    @property
    def name(self):
        """Return the name of the device."""

        return self._name		
	

    @property
    def icon(self):
        """Return the icon of the device."""


        if self._state in [STATE_PAUSED, STATE_UNAVAILABLE, STATE_IDLE, STATE_UNKNOWN]:
            return ICON_DEFAULT

        if self._muted:
            return ICON_MUTED

        if self._source == "DLNA" or self._source == "Airplay" or self._source == "Amazon" or self._source == "Spotify" or self._source == "TIDAL":
            return ICON_PUSHSTREAM

        if self._state == STATE_PLAYING:
            return ICON_PLAYING

        return ICON_DEFAULT	


    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return int(self._volume) / MAX_VOL

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        return self._muted

    @property
    def source(self):
        """Return the current input source."""
        if self._source not in ['Idle', 'Network']:
            return self._source
        else:
            return None

    @property
    def supported_features(self):
        """Flag media player features that are supported."""

        if self._playing_connect or self._playing_mediabrowser:
            if self._state in [STATE_PLAYING, STATE_PAUSED]:
                self._features = \
                MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA | \
                MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE | \
                MediaPlayerEntityFeature.NEXT_TRACK | MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.SHUFFLE_SET | MediaPlayerEntityFeature.REPEAT_SET | MediaPlayerEntityFeature.SEEK
            else:
                self._features = \
                MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA | \
                MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE | \
                MediaPlayerEntityFeature.NEXT_TRACK | MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.SHUFFLE_SET | MediaPlayerEntityFeature.REPEAT_SET

        elif self._playing_stream:
            self._features = \
            MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA | \
            MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE | \
            MediaPlayerEntityFeature.NEXT_TRACK | MediaPlayerEntityFeature.PREVIOUS_TRACK

        elif self._playing_idle:
            self._features = \
            MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA | \
            MediaPlayerEntityFeature.STOP

        return self._features	
		
    @property
    def media_position(self):
        """Time in seconds of current playback head position."""
        if (self._playing_connect or self._playing_stream) and self._state != STATE_UNAVAILABLE:
            return self._playhead_position
        else:
            return None

    @property
    def media_duration(self):
        """Time in seconds of current song duration."""
        if (self._playing_connect or self._playing_stream) and self._state != STATE_UNAVAILABLE:
            return self._duration
        else:
            return None

    @property
    def media_position_updated_at(self):
        """When the seek position was last updated."""
        if not self._playing_idle and self._state == STATE_PLAYING:
            return self._position_updated_at
        else:
            return None

    @property
    def shuffle(self):
        """Return True if shuffle mode is enabled."""
        return self._shuffle

    @property
    def repeat(self):
        """Return repeat mode."""
        return self._repeat

    @property
    def media_title(self):
        """Return title of the current track."""
        return self._media_title

    @property
    def media_artist(self):
        """Return name of the current track artist."""
        return self._media_artist

    @property
    def media_album_name(self):
        """Return name of the current track album."""
        return self._media_album

    @property
    def media_image_url(self):
        """Return name the image for the current track."""
        return self._media_image_url

    @property
    def media_content_type(self):
        """Content type of current playing media. Has to be MEDIA_TYPE_MUSIC in order for Lovelace to show both artist and title."""
        return MEDIA_TYPE_MUSIC		

		
    @property
    def device_class(self) -> MediaPlayerDeviceClass:
        return MediaPlayerDeviceClass.SPEAKER

    @property
    def extra_state_attributes(self):
        """List members in group and set master and slave state."""
        attributes = {}

        attributes[ATTR_SAMPLERATE] = ''
        attributes[ATTR_BITRATE] = ''
        attributes[ATTR_DEPTH] = ''

        if self._media_uri:
            attributes[ATTR_STURI] = self._media_uri
        if self._media_uri_final:
            attributes[ATTR_STURI] = self._media_uri_final
        if self._pl_tracks:
            attributes[ATTR_TRCNT] = self._pl_tracks
        if self._pl_trackc:
            attributes[ATTR_TRCRT] = self._pl_trackc
        if self._trackc:
            attributes[ATTR_TRCRC] = self._trackc
        if self._uuid != '':
            attributes[ATTR_UUID] = self._uuid
        if self._samplerate:
            attributes[ATTR_SAMPLERATE] = str(float(self._samplerate) / 1000) + ' kHz'
        if self._bitrate:
            attributes[ATTR_BITRATE] = self._bitrate + ' kbps'
        if self._bitdepth and int(self._bitdepth) > 24:
            attributes[ATTR_DEPTH] = '24'
        elif self._bitdepth:
            attributes[ATTR_DEPTH] = self._bitdepth

        if DEBUGSTR_ATTR:
            atrdbg = ""

            if self._playing_connect:
                atrdbg = atrdbg + " _playing_connect"			

            if self._playing_stream:
                atrdbg = atrdbg + " _playing_stream"

            if self._playing_idle:
                atrdbg = atrdbg + " _playing_idle"
                
            if self._playing_mediabrowser:
                atrdbg = atrdbg + " _playing_mediabrowser"

            attributes[ATTR_DEBUG] = atrdbg

        if self._state != STATE_UNAVAILABLE:
            attributes[ATTR_FWVER] = self._fw_ver
			
        return attributes	

    @property
    def host(self):
        """Self ip."""
        return self._host

    @property
    def track_count(self):
        """List of tracks present on the device."""
        return self._pl_tracks

    @property
    def unique_id(self):
        """Return the unique id."""
        if self._uuid != '':
            return "wiim_media_" + self._uuid

    @property
    def fw_ver(self):
        """Return the firmware version number of the device."""
        return self._fw_ver		

    async def async_media_next_track(self):
        """Send media_next command to media player."""

        value = await self.call_wiim_httpapi("setPlayerCmd:next", None)
        self._playhead_position = 0
        self._duration = 0
        self._position_updated_at = utcnow()
        self._trackc = None
        if value != "OK":
            _LOGGER.warning("Failed skip to next track. Device: %s, Got response: %s", self.entity_id, value)


    async def async_media_previous_track(self):
        """Send media_previous command to media player."""

        value = await self.call_wiim_httpapi("setPlayerCmd:prev", None)
        self._playhead_position = 0
        self._duration = 0
        self._position_updated_at = utcnow()
        self._trackc = None
        if value != "OK":
            _LOGGER.warning("Failed to skip to previous track." " Device: %s, Got response: %s", self.entity_id, value)

    async def async_media_play(self):
        """Send media_play command to media player."""
        if self._state == STATE_PAUSED:
            value = await self.call_wiim_httpapi("setPlayerCmd:resume", None)

        else:
            value = await self.call_wiim_httpapi("setPlayerCmd:play", None)

        if value == "OK":
            self._state = STATE_PLAYING
            self._unav_throttle = False

            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
    
        else:
            _LOGGER.warning("Failed to start or resume playback. Device: %s, Got response: %s", self.entity_id, value)

    async def async_media_pause(self):
        """Send media_pause command to media player."""

        if self._playing_stream and not self._playing_mediabrowser:
            # Pausing a live stream will cause a buffer overrun in hardware. Stop is the correct procedure in this case.
            # If the stream is configured as an input source, when pressing Play after this, it will be started again (using self._prev_source).
            await self.async_media_stop()
            return

        value = await self.call_wiim_httpapi("setPlayerCmd:pause", None)
        if value == "OK":
            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
            if self._playing_connect:
                self._connect_paused_at = utcnow()
            self._state = STATE_PAUSED

        else:
            _LOGGER.warning("Failed to pause playback. Device: %s, Got response: %s", self.entity_id, value)


    async def async_media_stop(self):
        """Send stop command."""
 
        if self._playing_connect or self._playing_idle or self._playing_stream:
            await self.call_wiim_httpapi("setPlayerCmd:pause", None)
            await self.call_wiim_httpapi("setPlayerCmd:switchmode:wifi", None)


        value = await self.call_wiim_httpapi("setPlayerCmd:stop", None)
        if value == "OK":
            self._state = STATE_IDLE
            self._playhead_position = 0
            self._duration = 0
            self._media_title = None
 
            self._source = None
 
            self._media_artist = None
            self._media_album = None

            self._media_uri = None
            self._media_uri_final = None

            self._playing_mediabrowser = False
            self._playing_stream = False
            self._playing_connect = False
            self._trackc = None
            self._media_image_url = None
            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
            self._connect_paused_at = None
            self._samplerate = None
            self._bitrate = None
            self._bitdepth = None

        else:
            _LOGGER.warning("Failed to stop playback. Device: %s, Got response: %s", self.entity_id, value)

    async def async_media_seek(self, position):
        """Send media_seek command to media player."""
        _LOGGER.debug("Seek. Device: %s, DUR: %s POS: %", self.name, self._duration, position)
        if self._duration > 0 and position >= 0 and position <= self._duration:
            value = await self.call_wiim_httpapi("setPlayerCmd:seek:{0}".format(str(position)), None)
            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
            if value != "OK":
                _LOGGER.warning("Failed to seek. Device: %s, Got response: %s", self.entity_id, value)		

    async def async_clear_playlist(self):
        """Clear players playlist."""
        pass

    async def async_play_media(self, media_type, media_id, **kwargs):
        """Play media from a URL or localfile."""
        _LOGGER.debug("Trying to play media. Device: %s, Media_type: %s, Media_id: %s", self.entity_id, media_type, media_id)

        self._playing_mediabrowser = True

        if not (media_type in [MEDIA_TYPE_URL] or media_source.is_media_source_id(media_id)):
            _LOGGER.warning("For: %s Invalid media type %s. Only %s is supported", self._name, media_type, MEDIA_TYPE_URL)
            await self.async_media_stop()
            return False
            
   



        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            if media_id.find('radio_browser') != -1:  # radios are an exception, be treated by server redirect checker
                self._playing_mediabrowser = False

            media_id = play_item.url
            if not play_item.mime_type in ['audio/basic',
                                           'audio/mpeg', 
                                           'audio/mp3', 
                                           'audio/mpeg3', 
                                           'audio/x-mpeg-3',
                                           'audio/x-mpegurl', 
                                           'audio/mp4', 
                                           'audio/aac', 
                                           'audio/x-aac',
                                           'audio/x-hx-aac-adts', 
                                           'audio/x-aiff', 
                                           'audio/ogg', 
                                           'audio/vorbis', 
                                           'application/ogg', 
                                           'audio/opus', 
                                           'audio/webm', 
                                           'audio/wav', 
                                           'audio/x-wav', 
                                           'audio/vnd.wav', 
                                           'audio/flac',
                                           'audio/x-flac', 
                                           'audio/x-ms-wma']:
                _LOGGER.warning("For: %s Invalid media type, %s is not supported", self._name, play_item.mime_type)
                self._playing_mediabrowser = False
                return False
                
            media_id = async_process_play_media_url(self.hass, media_id)
            _LOGGER.debug("Trying to play HA media. Device: %s, Play_Item: %s, Media_id: %s", self._name, play_item, media_id)

        media_id_check = media_id.lower()

        if media_id_check.startswith('http'):
            media_type = MEDIA_TYPE_URL

        if media_type != MEDIA_TYPE_URL:
            _LOGGER.warning("For: %s Invalid media type %s. Only %s is supported", self._name, media_type, MEDIA_TYPE_URL)
            await self.async_media_stop()
            return False


        if self._playing_mediabrowser:
            media_id_final = media_id
        else:
            media_id_final = await self.async_detect_stream_url_redirection(media_id)

        if self._state == STATE_PLAYING:
            await self.call_wiim_httpapi("setPlayerCmd:pause", None)
                
        if self._playing_connect:  # disconnect from Spotify before playing new http source
            await self.call_wiim_httpapi("setPlayerCmd:switchmode:wifi", None)

        if media_id_check.find('.m3u') != -1:
            _LOGGER.debug("For: %s, Detected M3U list: %s, Media_id: %s", self._name, media_id_final, media_id)
            
            if await self.async_parse_m3u_url(media_id_final):
                value = await self.call_wiim_httpapi("setPlayerCmd:playlist:{0}:0".format(media_id_final), None)
            else:
                self._playing_mediabrowser = False
                return False
        else:
            value = await self.call_wiim_httpapi("setPlayerCmd:play:{0}".format(media_id_final), None)
        if value != "OK":
            _LOGGER.warning("Failed to play media type URL. Device: %s, Got response: %s, Media_Id: %s", self.entity_id, value, media_id)
            self._playing_mediabrowser = False
            return False


        self._state = STATE_PLAYING
        if media_id.find('tts_proxy') != -1:
            #_LOGGER.debug("Setting TTS: %s, %s", self.entity_id, self._name)
            self._playing_mediabrowser = False
            self._playing_stream = False

        self._media_title = None
        self._media_artist = None
        self._media_album = None
 
        self._playhead_position = 0
        self._duration = 0
        self._trackc = None
        self._position_updated_at = utcnow()
        self._idletime_updated_at = self._position_updated_at
        self._media_image_url = None
        self._samplerate = None
        self._bitrate = None
        self._bitdepth = None

        self._unav_throttle = False

        self._media_uri = media_id
        self._media_uri_final = media_id_final

        return True



		
    async def async_set_shuffle(self, shuffle):
        """Change the shuffle mode."""

        if shuffle:
            self._shuffle = shuffle
            mode = '3' if self._repeat == REPEAT_MODE_OFF else '2'
        else:
            if self._repeat == REPEAT_MODE_OFF:
                mode = '4'
            elif self._repeat == REPEAT_MODE_ALL:
                mode = '0'
            elif self._repeat == REPEAT_MODE_ONE:
                mode = '1'
        value = await self.call_wiim_httpapi("setPlayerCmd:loopmode:{0}".format(mode), None)
        if value != "OK":
            _LOGGER.warning("Failed to change shuffle mode. Device: %s, Got response: %s", self.entity_id, value)


    async def async_set_repeat(self, repeat):
        """Change the repeat mode."""
        self._repeat = repeat
        if repeat == REPEAT_MODE_OFF:
            mode = '3' if self._shuffle else '4'
        elif repeat == REPEAT_MODE_ALL:
            mode = '2' if self._shuffle else '0'
        elif repeat == REPEAT_MODE_ONE:
            mode = '1'
        value = await self.call_wiim_httpapi("setPlayerCmd:loopmode:{0}".format(mode), None)
        if value != "OK":
            _LOGGER.warning("Failed to change repeat mode. Device: %s, Got response: %s", self.entity_id, value)


    async def async_detect_stream_url_redirection(self, uri):
        if uri.find('tts_proxy') != -1: # skip redirect check for local TTS streams
            return uri
        _LOGGER.debug('For: %s detect URI redirect-from:   %s', self._name, uri)
        redirect_detect = True
        check_uri = uri
        try:
            while redirect_detect:
                response_location = requests.head(check_uri, allow_redirects=False, headers={'User-Agent': 'VLC/3.0.16 LibVLC/3.0.16'})
                #_LOGGER.debug('For: %s detecting URI redirect code: %s', self._name, str(response_location.status_code))
                if response_location.status_code in [301, 302, 303, 307, 308] and 'Location' in response_location.headers:
                    #_LOGGER.debug('For: %s detecting URI redirect location: %s', self._name, response_location.headers['Location'])
                    check_uri = response_location.headers['Location']
                else:
                    #_LOGGER.debug('For: %s detecting URI redirect - result: %s', self._name, check_uri)
                    redirect_detect = False
        except:
            pass

        _LOGGER.debug('For: %s detect URI redirect - to:   %s', self._name, check_uri)
        return check_uri
	
		
    async def async_parse_m3u_url(self, playlist):
        """Parse an M3U playlist URL for actual streams, and return the first one"""
        try:
            websession = async_get_clientsession(self.hass)
            async with async_timeout.timeout(10):
                response = await websession.get(playlist, ssl=False)

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.warning(
                "For: %s unable to get the M3U playlist: %s", self._name, playlist
            )
            return False

        if response.status == HTTPStatus.OK:
            data = await response.text()
            _LOGGER.debug("For: %s M3U playlist: %s  contents: %s", self._name, playlist, data)

            lines = [line.strip("\n\r") for line in data.split("\n") if line.strip("\n\r") != ""]
            if len(lines) > 0:
                _LOGGER.debug("For: %s M3U playlist: %s  lines: %s", self._name, playlist, lines)
                noturls = [u for u in lines if not u.startswith('http')]
                _LOGGER.debug("For: %s M3U playlist: %s  not urls: %s", self._name, playlist, noturls)
                if len(noturls) > 0:
                    return False
                else:
                    return True
            else:
                _LOGGER.error("For: %s M3U playlist: %s No content to parse!!!", self._name, playlist)
                return False
        else:
            _LOGGER.error(
                "For: %s (%s) Get failed, response code: %s Full message: %s",
                self._name,
                self._host,
                response.status,
                response,
            )
            return False

        return False		
		
		
	
		

    async def async_set_media_title(self, title):
        """Set the media title property."""
        self._media_title = title

    async def async_set_media_artist(self, artist):
        """Set the media artist property."""
        self._media_artist = artist

    async def async_set_volume(self, volume):
        """Set the volume property."""
        self._volume = volume

    async def async_set_muted(self, mute):
        """Set the muted property."""
        self._muted = mute

    async def async_set_state(self, state):
        """Set the state property."""
        self._state = state


    async def async_set_playhead_position(self, position):
        """Set the playhead position property."""
        self._playhead_position = position

    async def async_set_duration(self, duration):
        """Set the duration property."""
        self._duration = duration

    async def async_set_position_updated_at(self, time):
        """Set the position updated at property."""
        self._position_updated_at = time

    async def async_set_source(self, source):
        """Set the source property."""
        self._source = source


    async def async_set_media_image_url(self, url):
        """Set the media image URL property."""
        self._media_image_url = url

    async def async_set_media_uri(self, uri):
        """Set the media URL property."""
        self._media_uri = uri		
		
    async def async_set_features(self, features):
        """Set the self features property."""
        self._features = features

    async def async_set_unav_throttle(self, unav_throttle):
        """Set update throttle property."""
        self._unav_throttle = unav_throttle		

    async def async_preset_button(self, preset):
        """Simulate pressing a physical preset button."""
        if self._preset_key != None and preset != None:
            if int(preset) > 0 and int(preset) <= self._preset_key:
                value = await self.call_wiim_httpapi("MCUKeyShortClick:{0}".format(str(preset)), None)

                if value != "OK":
                    _LOGGER.warning("Failed to recall preset %s. " "Device: %s, Got response: %s", self.entity_id, preset, value)
            else:
                _LOGGER.warning("Wrong preset number %s. Device: %s, has to be integer between 1 and %s", self.entity_id, preset, self._preset_key)

		
		
    async def async_execute_command(self, command, notif):
        """Execute desired command against the player using factory API."""
        if command == 'Rescan':
            self._unav_throttle = False
            self._first_update = True
            value = "Scheduled to Rescan"
        elif command == 'reboot':
            value = await self.call_wiim_httpapi("reboot;", None)
        else:
            value = "No such command implemented."
            _LOGGER.warning("Player %s command: %s, result: %s", self.entity_id, command, value)

        _LOGGER.debug("Player %s executed command: %s, result: %s", self.entity_id, command, value)

        if notif:
            self.hass.components.persistent_notification.async_create("<b>Executed command:</b><br>{0}<br><b>Result:</b><br>{1}".format(command, value), title=self.entity_id)
		


		
    async def async_update_via_upnp(self):
        """Update track info via UPNP."""
        import validators

        if self._upnp_device is None:
            return

        _LOGGER.debug("Update via UPnP for: %s", self.entity_id)

        self._service = self._upnp_device.service('urn:schemas-upnp-org:service:AVTransport:1')
        #_LOGGER.debug("GetMediaInfo for: %s, UPNP service:%s", self.entity_id, self._service)
        
        media_info = dict()
        media_metadata = None
        try:
            media_info = await self._service.action("GetMediaInfo").async_call(InstanceID=0)
            self._trackc = media_info.get('CurrentURI')
            self._media_uri_final = media_info.get('TrackSource')
            media_metadata = media_info.get('CurrentURIMetaData')
            #_LOGGER.debug("GetMediaInfo for: %s, UPNP media_metadata:%s", self.entity_id, media_info)
        except:
            _LOGGER.warning("GetMediaInfo/CurrentURIMetaData UPNP error: %s", self.entity_id)

        self._media_title = None
        self._media_album = None
        self._media_artist = None
        self._media_image_url = None
        self._samplerate = None
        self._bitrate = None
        self._bitdepth = None

        if media_metadata is None:
            return


        xml_tree = ET.fromstring(media_metadata)

        xml_path = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}item/"
        title_xml_path = "{http://purl.org/dc/elements/1.1/}title"
        artist_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}artist"
        album_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}album"
        image_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}albumArtURI"
        rate_hz_xml_path = "{www.wiimu.com/song/}rate_hz"
        format_s_xml_path = "{www.wiimu.com/song/}format_s"
        bitrate_xml_path = "{www.wiimu.com/song/}bitrate"

        title_node = xml_tree.find("{0}{1}".format(xml_path, title_xml_path))
        artist_node = xml_tree.find("{0}{1}".format(xml_path, artist_xml_path))
        album_node = xml_tree.find("{0}{1}".format(xml_path, album_xml_path))
        image_url_node = xml_tree.find("{0}{1}".format(xml_path, image_xml_path))
        rate_hz_node = xml_tree.find("{0}{1}".format(xml_path, rate_hz_xml_path))
        format_s_node = xml_tree.find("{0}{1}".format(xml_path, format_s_xml_path))
        bitrate_node = xml_tree.find("{0}{1}".format(xml_path, bitrate_xml_path))

        if title_node is not None:
            self._media_title = title_node.text
        if artist_node is not None:
            self._media_artist = artist_node.text
        if album_node is not None:
            self._media_album = album_node.text
        if image_url_node is not None:
            self._media_image_url = image_url_node.text
        if rate_hz_node is not None:
            self._samplerate = rate_hz_node.text
        if format_s_node is not None:
            self._bitdepth = format_s_node.text
        if bitrate_node is not None:
            self._bitrate = bitrate_node.text

        if self._media_image_url is not None:
            if not validators.url(self._media_image_url):
                self._media_image_url = None    
	
    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Implement the websocket media browsing helper."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )		