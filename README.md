# WiiM devices

This component allows you to integrate control of WiiM Mini and Pro devices into your [Home Assistant](http://www.home-assistant.io) smart home system. Originally developed for LinkPlay devices by @nicjo814, @limych and @nagyrobi.


## Installation
* Copy all files in `custom_components/wiim_custom` to your `<config directory>/custom_components/wiim_custom/` directory.
* Restart Home-Assistant.
* Add the configuration to your configuration.yaml.
* Restart Home-Assistant again.


### Configuration

It is recommended to create static DHCP leases in your network router to ensure the devices always get the same IP address. Recent models versions allow setting static IP address, if you see that option, use it.

To add WiiM units to your installation, add the following to your `configuration.yaml` file:

```yaml
# Example configuration.yaml entry
media_player:
    - platform: wiim_custom
      host: 192.168.1.11
      name: Sound Room1


    - platform: wiim_custom
      host: 192.168.1.12
      name: Sound Room2
      uuid: 'FF31F09E82A6BBC1A2CB6D80'
      volume_step: 10
```

### Configuration Variables

**host:**  
  *(string)* *(Required)* The IP address of the WiiM unit.

**name:**  
  *(string)* *(Required)* Name that Home Assistant will generate the `entity_id` based on. It is also the base of the friendly name seen in the dashboard, but will be overriden by the device name set in the Android app.

**uuid:**  
  *(string)* *(Optional)* Hardware UUID of the player. Can be read out from the attibutes of the entity. Set it manually to that value to handle double-added entity cases when Home Assistant starts up without the WiiM device being on the network at that moment.
  
**volume_step:**  
  *(integer)* *(Optional)* Step size in percent to change volume when calling `volume_up` or `volume_down` service against the media player. Defaults to `5`, can be a number between `1` and `25`.


## Home Assistant component authors & contributors
    "@nicjo814",
    "@limych",
    "@nagyrobi",
	"@onlyoneme"

## Home Assistant component License

MIT License

- Copyright (c) 2019 Niclas Berglind nicjo814
- Copyright (c) 2019—2020 Andrey "Limych" Khrolenok
- Copyright (c) 2020 nagyrobi Robert Horvath-Arkosi
- Copyright (c) 2022 onlyoneme Mariusz Kopacki

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

