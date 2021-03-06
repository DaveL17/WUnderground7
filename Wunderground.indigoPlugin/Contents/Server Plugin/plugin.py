#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-

"""
WUnderground7 Plugin
plugin.py
Author: DaveL17
Credits:
Update Checker by: berkinet (with additional features by Travis Cook)
Regression Testing by: Monstergerm

The WUnderground plugin downloads JSON data from Weather Underground and parses
it into custom device states. Theoretically, the user can create an unlimited
number of devices representing individual observation locations. The
WUnderground plugin will update each custom device found in the device
dictionary incrementally. The user can have independent settings for each
weather location.

The base Weather Underground developer plan allows for 10 calls per minute and
a total of 500 per day. Setting the plugin for 5 minute refreshes results in
288 calls per device per day. In other words, two devices (with different
location settings) at 5 minutes will be an overage. The plugin makes only one
call per location per cycle. See Weather Underground for more information on
API call limitations.

The plugin tries to leave WU data unchanged. But in order to be useful, some
changes need to be made. The plugin adjusts the raw JSON data in the following
ways:
- The barometric pressure symbol is changed to something more human
  friendly: (+ -> ^, 0 -> -, - -> v). Now with more options!
- Takes numerics and converts them to strings for Indigo compatibility
  where necessary.
- Strips non-numeric values from numeric values for device states where
  appropriate (but retains them for ui.Value)
- Weather Underground is inconsistent in the data it provides as
  strings and numerics. Sometimes a numeric value is provided as a
  string and we convert it to a float where useful.
- Sometimes, WU provides a forecast value that has a level of precision greater
  than expected. For example, a forecast high of 72.1º. It is unlikely that WU
  would predict with such precision intentionally, so we round these values to
  the nearest integer.
- Sometimes, WU provides a value that would break Indigo logic.
  Conversions made:
  - Replaces anything that is not a rational value (i.e., "--" with "0"
    for precipitation since precipitation can only be zero or a
    positive value) and replaces "-999.0" with a value of -99.0 and a UI value
    of "--" since the actual value could be positive or negative.

 Not all values are available in all API calls.  The plugin makes these units
 available::

   distance       w    -    -    -
   percentage     w    t    h    -
   pressure       w    -    -    -
   rainfall       w    t    h    -
   snow           -    t    h    -
   temperature    w    t    h    a
   wind           w    t    h    -

 (above: _w_eather, _t_en day, _h_ourly, _a_lmanac)

Weather data copyright Weather Underground and Weather Channel, LLC., (and its
subsidiaries), or respective data providers. This plugin and its author are in
no way affiliated with Weather Underground, LLC. For more information about
data provided see Weather Underground Terms of Service located at:
http://www.wunderground.com/weather/api/d/terms.html.

For information regarding the use of this plugin, see the license located in
the plugin package or located on GitHub:
https://github.com/DaveL17/WUnderground7/blob/master/LICENSE
"""

# =================================== TO DO ===================================

# ================================== IMPORTS ==================================

# Built-in modules
import cgi
import datetime as dt
import logging
import re
import requests
import simplejson
import socket
import sys
import time
import traceback
import urllib   # (satellite imagery fallback)
import urllib2  # (weather data fallback)

# Third-party modules
# from DLFramework import indigoPluginUpdateChecker
try:
    import indigo
except ImportError:
    pass
try:
    import pydevd
except ImportError:
    pass

# My modules
import DLFramework.DLFramework as Dave

# =================================== HEADER ==================================

__author__    = Dave.__author__
__copyright__ = Dave.__copyright__
__license__   = Dave.__license__
__build__     = Dave.__build__
__title__     = "WUnderground7 Plugin for Indigo Home Control"
__version__   = "7.0.17"

# =============================================================================

kDefaultPluginPrefs = {
    u'alertLogging': "false",           # Write severe weather alerts to the log?
    u'apiKey': "",                      # WU requires the api key.
    u'callCounter': "500",              # WU call limit based on UW plan.
    u'dailyCallCounter': "0",           # Number of API calls today.
    u'dailyCallDay': "1970-01-01",      # API call counter date.
    u'dailyCallLimitReached': "false",  # Has the daily call limit been reached?
    u'downloadInterval': "900",         # Frequency of weather updates.
    u'ignoreEstimated' : False,         # Accept estimated conditions, or not
    u'itemListTempDecimal': "1",        # Precision for Indigo Item List.
    u'language': "EN",                  # Language for WU text.
    u'lastSuccessfulPoll': "1970-01-01 00:00:00",  # Last successful plugin cycle
    u'launchWUparameters' : "https://www.wunderground.com/api/",  # url for launch API button
    u'nextPoll': "",                    # Last successful plugin cycle
    u'noAlertLogging': "false",         # Suppresses "no active alerts" logging.
    u'showDebugLevel': "30",            # Logger level.
    u'uiDateFormat': "DD-MM-YYYY",     # Preferred date format string.
    u'uiHumidityDecimal': "1",          # Precision for Indigo UI display (humidity).
    u'uiPressureTrend': "text",         # Pressure trend symbology
    u'uiTempDecimal': "1",              # Precision for Indigo UI display (temperature).
    u'uiTimeFormat': "military",       # Preferred time format string.
    u'uiWindDecimal': "1",              # Precision for Indigo UI display (wind).
    u'updaterEmail': "",                # Email to notify of plugin updates.
    u'updaterEmailsEnabled': "false"  # Notification of plugin updates wanted.
}


# Indigo Methods ==============================================================
class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        indigo.PluginBase.__init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        self.pluginIsInitializing = True
        self.pluginIsShuttingDown = False

        self.download_interval = dt.timedelta(seconds=int(self.pluginPrefs.get('downloadInterval', '900')))
        self.masterWeatherDict = {}
        self.masterTriggerDict = {}
        self.wuOnline = True
        self.pluginPrefs['dailyCallLimitReached'] = False

        # ========================== API Poll Values ==========================
        last_poll = self.pluginPrefs.get('lastSuccessfulPoll', "1970-01-01 00:00:00")
        try:
            self.last_poll_attempt = dt.datetime.strptime(last_poll, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            self.last_poll_attempt = dt.datetime.strptime(last_poll, '%Y-%m-%d %H:%M:%S.%f')

        next_poll = self.pluginPrefs.get('lastSuccessfulPoll', "1970-01-01 00:00:00")
        try:
            self.next_poll_attempt = dt.datetime.strptime(next_poll, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            self.next_poll_attempt = dt.datetime.strptime(next_poll, '%Y-%m-%d %H:%M:%S.%f')

        # ====================== Initialize DLFramework =======================

        self.Fogbert   = Dave.Fogbert(self)
        self.Formatter = Dave.Formatter(self)

        self.date_format = self.Formatter.dateFormat()
        self.time_format = self.Formatter.timeFormat()

        # Weather Underground Attribution and disclaimer.
        indigo.server.log(u"{0:*^130}".format(""))
        indigo.server.log(u"{0:*^130}".format("  Data are provided by Weather Underground, LLC. This plugin and its author are in no way affiliated with Weather Underground.  "))
        indigo.server.log(u"{0:*^130}".format(""))

        # Log pluginEnvironment information when plugin is first started
        self.Fogbert.pluginEnvironment()

        # ================== Initialize Debugging Protocols ===================

        # Get current debug level. This value could be left over from prior
        # versions of the plugin.
        debug_level = self.pluginPrefs.get('showDebugLevel', '30')

        # Convert old debugLevel scale (low, medium, high or 1, 2, 3) to new
        # scale (logger).  If it's the old [low, medium, high], convert it.
        debug_level = self.Fogbert.convertDebugLevel(debug_level)

        if 0 < debug_level <= 3:
            if self.pluginPrefs.get('showDebugInfo', True):
                self.indigo_log_handler.setLevel(10)
            else:
                self.pluginPrefs['showDebugLevel'] = '20'  # informational messages

        # Set the format and level handlers for the logger
        log_format = '%(asctime)s.%(msecs)03d\t%(levelname)-10s\t%(name)s.%(funcName)-28s %(msg)s'
        self.plugin_file_handler.setFormatter(logging.Formatter(log_format, datefmt='%Y-%m-%d %H:%M:%S'))
        self.indigo_log_handler.setLevel(int(self.pluginPrefs['showDebugLevel']))

        # =====================================================================

        # try:
        #     pydevd.settrace('localhost', port=5678, stdoutToServer=True, stderrToServer=True, suspend=False)
        # except:
        #     pass

        self.pluginIsInitializing = False

    def __del__(self):
        indigo.PluginBase.__del__(self)

    def closedPrefsConfigUi(self, values_dict, user_cancelled):

        self.logger.debug(u"closedPrefsConfigUi called.")

        if user_cancelled:
            self.logger.debug(u"User prefs dialog cancelled.")

        if not user_cancelled:
            self.indigo_log_handler.setLevel(int(values_dict['showDebugLevel']))

            # ============================= Update Poll Time ==============================
            self.download_interval = dt.timedelta(seconds=int(self.pluginPrefs.get('downloadInterval', '900')))
            last_poll              = self.pluginPrefs.get('lastSuccessfulPoll', "1970-01-01 00:00:00")

            try:
                next_poll = dt.datetime.strptime(last_poll, '%Y-%m-%d %H:%M:%S') + self.download_interval
            except ValueError:
                next_poll = dt.datetime.strptime(last_poll, '%Y-%m-%d %H:%M:%S.%f') + self.download_interval

            self.pluginPrefs['nextPoll'] = dt.datetime.strftime(next_poll, '%Y-%m-%d %H:%M:%S')

            # =================== Update Item List Temperature Precision ==================
            # For devices that display the temperature as their main UI state, try to set
            # them to their (potentially changed) ui format.
            for dev in indigo.devices.itervalues('self'):

                # For weather device types
                if dev.model in ['WUnderground Device', 'WUnderground Weather', 'WUnderground Weather Device', 'Weather Underground', 'Weather']:

                    current_on_off_state = dev.states.get('onOffState', True)
                    current_on_off_state_ui = dev.states.get('onOffState.ui', "")

                    # If the device is currently displaying its temperature value, update it to
                    # reflect its new format
                    if current_on_off_state_ui not in ['Disabled', 'Enabled', '']:
                        try:
                            units_dict = {'S': 'F', 'M': 'C', 'SM': 'F', 'MS': 'C', 'SN': '', 'MN': ''}
                            units = units_dict[dev.pluginProps.get('itemListUiUnits', 'SN')]
                            display_value = u"{0:.{1}f} {2}{3}".format(dev.states['temp'], int(self.pluginPrefs['itemListTempDecimal']), dev.pluginProps['temperatureUnits'], units)

                        except KeyError:
                            display_value = u""

                        dev.updateStateOnServer('onOffState', value=current_on_off_state, uiValue=display_value)

            self.logger.debug(u"User prefs saved.")
            # indigo.server.log(unicode(values_dict))

    def deviceStartComm(self, dev):

        self.logger.debug(u"Starting Device: {0}".format(dev.name))

        # Check to see if the device profile has changed.
        dev.stateListOrDisplayStateIdChanged()

        # ========================= Update Temperature Display ========================
        # For devices that display the temperature as their UI state, try to set them
        # to a value we already have.
        try:
            units_dict = {'S': 'F', 'M': 'C', 'SM': 'F', 'MS': 'C', 'SN': '', 'MN': ''}
            units = units_dict[dev.pluginProps.get('itemListUiUnits', 'SN')]
            display_value = u"{0:.{1}f} {2}{3}".format(dev.states['temp'], int(self.pluginPrefs['itemListTempDecimal']), dev.pluginProps['temperatureUnits'], units)

        except KeyError:
            display_value = u"Enabled"

        # =========================== Set Device Icon to Off ==========================
        if dev.model in ['WUnderground Device', 'WUnderground Weather', 'WUnderground Weather Device', 'Weather Underground', 'Weather']:
            dev.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
        else:
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

        dev.updateStateOnServer('onOffState', value=True, uiValue=display_value)

    def deviceStopComm(self, dev):

        self.logger.debug(u"Stopping Device: {0}".format(dev.name))

        # =========================== Set Device Icon to Off ==========================
        if dev.model in ['WUnderground Device', 'WUnderground Weather', 'WUnderground Weather Device', 'Weather Underground', 'Weather']:
            dev.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
        else:
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

        dev.updateStateOnServer('onOffState', value=False, uiValue=u"Disabled")

    def getDeviceConfigUiValues(self, values_dict, type_id, dev_id):

        self.logger.debug(u"getDeviceConfigUiValues called.")

        # =========================== Populate Default Value ==========================
        # weatherSummaryEmailTime is set by a generator. We need this bit to pre-
        # populate the control with the default value when a new device is created.
        if type_id == 'wunderground' and 'weatherSummaryEmailTime' not in values_dict.keys():
            values_dict['weatherSummaryEmailTime'] = "01:00"

        return values_dict

    def getPrefsConfigUiValues(self):

        return self.pluginPrefs

    def runConcurrentThread(self):

        self.logger.debug(u"Starting main thread.")

        self.sleep(5)

        try:
            while True:

                # Load the download interval in case it's changed
                self.download_interval = dt.timedelta(seconds=int(self.pluginPrefs.get('downloadInterval', '900')))

                # If the next poll attempt hasn't been changed to tomorrow, let's update it
                if self.next_poll_attempt == "1970-01-01 00:00:00" or not self.next_poll_attempt.day > dt.datetime.now().day:
                    self.next_poll_attempt = self.last_poll_attempt + self.download_interval
                    self.pluginPrefs['nextPoll'] = dt.datetime.strftime(self.next_poll_attempt, '%Y-%m-%d %H:%M:%S')

                # If we have reached the time for the next scheduled poll
                if dt.datetime.now() > self.next_poll_attempt:

                    self.last_poll_attempt = dt.datetime.now()
                    self.pluginPrefs['lastSuccessfulPoll'] = dt.datetime.strftime(self.last_poll_attempt, '%Y-%m-%d %H:%M:%S')

                    self.refreshWeatherData()
                    self.triggerProcessing()

                    # Report results of download timer.
                    plugin_cycle_time = (dt.datetime.now() - self.last_poll_attempt)
                    plugin_cycle_time = (dt.datetime.min + plugin_cycle_time).time()

                    self.logger.debug(u"[  Plugin execution time: {0} seconds  ]".format(plugin_cycle_time.strftime('%S.%f')))
                    self.logger.debug(u"{0:{1}^40}".format(' Plugin Cycle Complete ', '='))

                # Wait 60 seconds before trying again.
                self.sleep(30)

        except self.StopThread:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.debug(u"Stopping WUnderground Plugin thread.")

    def shutdown(self):

        self.pluginIsShuttingDown = True

    def startup(self):

        # =========================== Version Check ===========================
        self.Fogbert.audit_server_version(min_ver=7)

        for dev in indigo.devices.itervalues("self"):
            props = dev.pluginProps

            # Update legacy devices to support the 'isWeatherDevice' prop.
            props_dict = {'wundergroundAlmanac': True,
                          'wundergroundAstronomy': True,
                          'wundergroundHourly': True,
                          'satelliteImageDownloader': False,
                          'wundergroundRadar': False,
                          'wundergroundTenDay': True,
                          'wundergroundTides': True,
                          'wunderground': True,
                          }
            props['isWeatherDevice'] = props_dict[dev.deviceTypeId]

            dev.replacePluginPropsOnServer(props)
            
        return

    def triggerStartProcessing(self, trigger):

        self.logger.debug(u"Starting Trigger: {0}".format(trigger.name))

        dev_id = trigger.pluginProps['listOfDevices']
        timer  = trigger.pluginProps.get('offlineTimer', '60')

        # ============================= masterTriggerDict =============================
        # masterTriggerDict contains information on Weather Location Offline triggers.
        # {dev.id: (timer, trigger.id)}
        if trigger.configured and trigger.pluginTypeId == 'weatherSiteOffline':
            self.masterTriggerDict[dev_id] = (timer, trigger.id)

    def triggerStopProcessing(self, trigger):

        self.logger.debug(u"Stopping {0} trigger.".format(trigger.name))

    def validateDeviceConfigUi(self, values_dict, type_id, dev_id):

        self.logger.debug(u"validateDeviceConfigUi called.")

        error_msg_dict = indigo.Dict()

        # WUnderground Radar Devices
        if type_id == 'wundergroundRadar':

            if values_dict['imagename'] == "" or values_dict['imagename'].isspace():
                error_msg_dict['imagename'] = u"You must enter a valid image name."

            try:
                height = int(values_dict['height'])
                if not height >= 100:
                    error_msg_dict['height'] = u"The image height must be at least 100 pixels."
            except ValueError:
                error_msg_dict['height'] = u"Image Size Error.\n\nImage size values must be real numbers greater than zero."

            try:
                width = int(values_dict['width'])
                if not width >= 100:
                    error_msg_dict['width'] = u"The image width must be at least 100 pixels."
            except ValueError:
                error_msg_dict['width'] = u"Image Size Error.\n\nImage size values must be real numbers greater than zero."

            try:
                num = int(values_dict['num'])
                if not 0 < num <= 15:
                    error_msg_dict['num'] = u"The number of frames must be between 1 - 15."
            except ValueError:
                error_msg_dict['num'] = u"The number of frames must be between 1 - 15."

            try:
                time_label_x = int(values_dict['timelabelx'])
                time_label_y = int(values_dict['timelabely'])

                if not time_label_x >= 0:
                    error_msg_dict['timelabelx'] = u"The time stamp location setting must be a value greater than or equal to zero."

                if not time_label_y >= 0:
                    error_msg_dict['timelabely'] = u"The time stamp location setting must be a value greater than or equal to zero."

            except ValueError:
                error_msg_dict['timelabelx'] = u"Must be values greater than or equal to zero."
                error_msg_dict['timelabely'] = u"Must be values greater than or equal to zero."

            # Image Type: Bounding Box
            if values_dict['imagetype'] == 'boundingbox':

                try:
                    maxlat = float(values_dict['maxlat'])
                    maxlon = float(values_dict['maxlon'])
                    minlat = float(values_dict['minlat'])
                    minlon = float(values_dict['minlon'])

                    if not -90.0 <= minlat <= 90.0:
                        error_msg_dict['minlat'] = u"The Min Lat must be between -90.0 and 90.0."

                    if not -90.0 <= maxlat <= 90.0:
                        error_msg_dict['maxlat'] = u"The Max Lat must be between -90.0 and 90.0."

                    if not -180.0 <= minlon <= 180.0:
                        error_msg_dict['minlon'] = u"The Min Long must be between -180.0 and 180.0."

                    if not -180.0 <= maxlon <= 180.0:
                        error_msg_dict['maxlon'] = u"The Max Long must be between -180.0 and 180.0."

                    if abs(minlat) > abs(maxlat):
                        error_msg_dict['minlat'] = u"The Max Lat must be greater than the Min Lat."
                        error_msg_dict['maxlat'] = u"The Max Lat must be greater than the Min Lat."

                    if abs(minlon) > abs(maxlon):
                        error_msg_dict['minlon'] = u"The Max Long must be greater than the Min Long."
                        error_msg_dict['maxlon'] = u"The Max Long must be greater than the Min Long."
                except ValueError:
                    for _ in ('maxlat', 'maxlon', 'minlat', 'minlon'):
                        error_msg_dict[_] = u"Latitude and Longitude values must be expressed as real numbers."

            elif values_dict['imagetype'] == 'radius':
                try:
                    centerlat = float(values_dict['centerlat'])
                    centerlon = float(values_dict['centerlon'])
                except ValueError:
                    error_msg_dict['centerlat'] = u"Latitude and Longitude values must be expressed as real numbers."
                    error_msg_dict['centerlon'] = u"Latitude and Longitude values must be expressed as real numbers."

                try:
                    radius = float(values_dict['radius'])
                except ValueError:
                    error_msg_dict['radius'] = u"Radius Value Error.\n\nThe radius value must be a real number greater than zero"

                if not -90.0 <= centerlat <= 90.0:
                    error_msg_dict['centerlat'] = u"Center Lat must be between -90.0 and 90.0."

                if not -180.0 <= centerlon <= 180.0:
                    error_msg_dict['centerlon'] = u"Center Long must be between -180.0 and 180.0."

                if not radius > 0:
                    error_msg_dict['radius'] = u"Radius must be greater than zero."

            elif values_dict['imagetype'] == 'locationbox':
                if values_dict['location'].isspace():
                    error_msg_dict['location'] = u"You must specify a valid location. Please see the plugin wiki for examples."

        if values_dict['isWeatherDevice']:

            # Test location setting for devices that must specify one.
            location_config = values_dict['location']
            if not location_config:
                error_msg_dict['location'] = u"Please specify a weather location."

            if " " in location_config:
                error_msg_dict['location'] = u"The location value can't contain spaces."

            if "\\" in location_config:
                error_msg_dict['location'] = u"The location value can't contain a \\ character. Replace it with a / character."

            if location_config.isspace():
                error_msg_dict['location'] = u"Please enter a valid location value."

        if len(error_msg_dict) > 0:
            error_msg_dict['showAlertText'] = u"Configuration Errors\n\nThere are one or more settings that need to be corrected. Fields requiring attention will be highlighted."
            return False, values_dict, error_msg_dict

        return True, values_dict

    def validateEventConfigUi(self, values_dict, type_id, event_id):

        self.logger.debug(u"validateEventConfigUi called.")

        dev_id         = values_dict['listOfDevices']
        error_msg_dict = indigo.Dict()

        # Weather Site Offline trigger
        if type_id == 'weatherSiteOffline':

            self.masterTriggerDict = {trigger.pluginProps['listOfDevices']: (trigger.pluginProps['offlineTimer'], trigger.id) for trigger in indigo.triggers.iter(filter="self.weatherSiteOffline")}

            # ======================== Validate Trigger Unique ========================
            # Limit weather location offline triggers to one per device
            if dev_id in self.masterTriggerDict.keys() and event_id != self.masterTriggerDict[dev_id][1]:
                existing_trigger_id = int(self.masterTriggerDict[dev_id][1])
                values_dict['listOfDevices'] = ''
                error_msg_dict['listOfDevices'] = u"Please select a weather device without an existing offline trigger."

            # ============================ Validate Timer =============================
            try:
                if int(values_dict['offlineTimer']) <= 0:
                    raise ValueError

            except ValueError:
                values_dict['offlineTimer'] = ''
                error_msg_dict['offlineTimer'] = u"You must enter a valid time value in minutes (positive integer " \
                                                 u"greater than zero)."

        if len(error_msg_dict) > 0:
            error_msg_dict['showAlertText'] = u"Configuration Errors\n\nThere are one or more settings that need " \
                                              u"to be corrected. Fields requiring attention will be highlighted."
            return False, values_dict, error_msg_dict

        return True, values_dict

    def validatePrefsConfigUi(self, values_dict):

        self.logger.debug(u"validatePrefsConfigUi called.")

        api_key_config      = values_dict['apiKey']
        call_counter_config = values_dict['callCounter']
        error_msg_dict      = indigo.Dict()
        update_email        = values_dict['updaterEmail']
        update_wanted       = values_dict['updaterEmailsEnabled']

        # Test api_keyconfig setting.
        if len(api_key_config) == 0:
            error_msg_dict['apiKey'] = u"The plugin requires an API key to function. See help for details."

        elif " " in api_key_config:
            error_msg_dict['apiKey'] = u"The API key can't contain a space."

        # Test call limit config setting.
        elif not int(call_counter_config):
            error_msg_dict['callCounter'] = u"The call counter can only contain integers."

        elif call_counter_config < 0:
            error_msg_dict['callCounter'] = u"The call counter value must be a positive integer."

        # Test plugin update notification settings.
        elif update_wanted and update_email == "":
            error_msg_dict['updaterEmail'] = u"If you want to be notified of updates, you must supply an email address."

        elif update_wanted and "@" not in update_email:
            error_msg_dict['updaterEmail'] = u"Valid email addresses have at least one @ symbol in them (foo@bar.com)."

        if len(error_msg_dict) > 0:
            error_msg_dict['showAlertText'] = u"Configuration Errors\n\nThere are one or more settings that need to be corrected. Fields requiring attention will be highlighted."
            return False, values_dict, error_msg_dict

        return True, values_dict

# WUnderground7 Methods =======================================================
    def actionRefreshWeather(self, values_dict):
        """
        Refresh all weather as a result of an action call

        The actionRefreshWeather() method calls the refreshWeatherData() method to
        request a complete refresh of all weather data (Actions.XML call.)

        -----

        :param indigo.Dict values_dict:
        """

        self.logger.debug(u"Processing Action: refresh all weather data.")

        self.refreshWeatherData()

    def callCount(self):
        """
        Maintain count of calls made to the WU API

        Maintains a count of daily calls to Weather Underground to help ensure that the
        plugin doesn't go over a user-defined limit. The limit is set within the plugin
        config dialog.

        -----
        """

        calls_made             = int(self.pluginPrefs.get('dailyCallCounter', '0'))  # Calls today so far
        calls_max              = int(self.pluginPrefs.get('callCounter', '500'))  # Max calls allowed per day

        # See if we have exceeded the daily call limit.  If we have, set the "dailyCallLimitReached" flag to be true.
        if calls_made >= calls_max:
            self.logger.info(u"Daily call limit ({0}) reached. Taking the rest of the day off.".format(calls_max))
            self.logger.debug(u"Set call limiter to: True")

            self.pluginPrefs['dailyCallLimitReached'] = True

            mark_delta = dt.datetime.now() + dt.timedelta(days=1)
            new_mark = mark_delta.replace(hour=0, minute=0, second=0, microsecond=0)
            self.next_poll_attempt = new_mark
            self.pluginPrefs['nextPoll'] = dt.datetime.strftime(self.next_poll_attempt, '%Y-%m-%d %H:%M:%S')
            self.logger.debug(u"Next Poll Time Updated: {0} (max calls exceeded)".format(self.next_poll_attempt))

        # Daily call limit has not been reached. Increment the call counter (and ensure that call limit flag is set to False.
        else:
            # Increment call counter and write it out to the preferences dict.
            self.pluginPrefs['dailyCallLimitReached'] = False
            self.pluginPrefs['dailyCallCounter'] += 1

            # Calculate how many calls are left for debugging purposes.
            calls_left = calls_max - calls_made
            self.logger.debug(u"API calls left: {0}".format(calls_left))

    def callDay(self):
        """
        Track day for call counter reset and forecast email

        Manages the day for the purposes of maintaining the call counter and the flag
        for the daily forecast email message.

        -----
        """

        call_day               = self.pluginPrefs.get('dailyCallDay', '1970-01-01')  # The date that the plugin thinks it is
        call_limit_reached     = self.pluginPrefs.get('dailyCallLimitReached', False)  # Has the daily call limit been reached?
        todays_date            = dt.datetime.today().date()  # Obtain today's date from OS as datetime object
        today_str              = u"{0}".format(todays_date)  # Convert today's date to a string object
        today_date             = dt.datetime.strptime(call_day, "%Y-%m-%d").date()  # Convert call_day to date object

        self.logger.debug(u"Processing API: Call counter")
        self.logger.debug(u"Daily call limit reached: {0}".format(call_limit_reached))

        # Check if callDay is a default value and set to today if it is.
        if call_day in ["", "1970-01-01"]:
            self.logger.debug(u"Initializing variable dailyCallDay: {0}".format(today_str))
            self.pluginPrefs['dailyCallDay'] = today_str

        # Reset call counter and call day because it's a new day.
        if todays_date > today_date:
            self.pluginPrefs['dailyCallCounter'] = 0
            self.pluginPrefs['dailyCallLimitReached'] = False
            self.pluginPrefs['dailyCallDay'] = today_str

            # If it's a new day, reset the forecast email sent flags.
            for dev in indigo.devices.itervalues('self'):
                try:
                    if 'weatherSummaryEmailSent' in dev.states:
                        dev.updateStateOnServer('weatherSummaryEmailSent', value=False)

                except Exception:
                    self.Fogbert.pluginErrorHandler(traceback.format_exc())
                    self.logger.error(u"Error setting email sent value.")

            self.logger.debug(u"Reset call limit, call counter and call day.")

        self.logger.debug(u"New call day: {0}".format(todays_date > today_date))

        if call_limit_reached:
            self.logger.info(u"Daily call limit reached. Taking the rest of the day off.")

    def commsKillAll(self):
        """
        Disable all plugin devices

        commsKillAll() sets the enabled status of all plugin devices to false.

        -----
        """

        for dev in indigo.devices.itervalues("self"):
            try:
                indigo.device.enable(dev, value=False)

            except Exception:
                self.Fogbert.pluginErrorHandler(traceback.format_exc())
                self.logger.error(u"Exception when trying to kill all comms.")

    def commsUnkillAll(self):
        """
        Enable all plugin devices

        commsUnkillAll() sets the enabled status of all plugin devices to true.

        -----
        """

        for dev in indigo.devices.itervalues("self"):
            try:
                indigo.device.enable(dev, value=True)

            except Exception:
                self.Fogbert.pluginErrorHandler(traceback.format_exc())
                self.logger.error(u"Exception when trying to unkill all comms.")

    def dumpTheJSON(self):
        """
        Dump copy of weather JSON to file

        The dumpTheJSON() method reaches out to Weather Underground, grabs a copy of
        the configured JSON data and saves it out to a file placed in the Indigo Logs
        folder. If a weather data log exists for that day, it will be replaced. With a
        new day, a new log file will be created (file name contains the date.)

        -----
        """

        file_name = '{0}/{1} Wunderground.txt'.format(indigo.server.getLogsFolderPath(), dt.datetime.today().date())

        try:

            with open(file_name, 'w') as logfile:

                # This works, but PyCharm doesn't like it as Unicode.  Encoding clears the inspection error.
                logfile.write(u"Weather Underground JSON Data\n".encode('utf-8'))
                logfile.write(u"Written at: {0}\n".format(dt.datetime.today().strftime('%Y-%m-%d %H:%M')).encode('utf-8'))
                logfile.write(u"{0}{1}".format("=" * 72, '\n').encode('utf-8'))

                for key in self.masterWeatherDict.keys():
                    logfile.write(u"Location Specified: {0}\n".format(key).encode('utf-8'))
                    logfile.write(u"{0}\n\n".format(self.masterWeatherDict[key]).encode('utf-8'))

            indigo.server.log(u"Weather data written to: {0}".format(file_name))

        except IOError:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.info(u"Unable to write to Indigo Log folder.")

    def emailForecast(self, dev):
        """
        Email forecast information

        The emailForecast() method will construct and send a summary of select weather
        information to the user based on the email address specified for plugin update
        notifications.

        -----

        :param indigo.Device dev:
        """

        try:
            summary_wanted = dev.pluginProps.get('weatherSummaryEmail', '')
            summary_sent   = dev.states.get('weatherSummaryEmailSent', False)

            # Get the desired summary email time and convert it for test.
            summary_time = dev.pluginProps.get('weatherSummaryEmailTime', '01:00')
            summary_time = dt.datetime.strptime(summary_time, '%H:%M')

            # Legacy devices had this setting improperly established as a string rather than a bool.
            if isinstance(summary_wanted, basestring):
                if summary_wanted.lower() == "false":
                    summary_wanted = False
                elif summary_wanted.lower() == "true":
                    summary_wanted = True

            if isinstance(summary_sent, basestring):
                if summary_sent.lower() == "false":
                    summary_sent = False
                elif summary_sent.lower() == "true":
                    summary_sent = True

            # If an email summary is wanted but not yet sent and we have reached the desired time of day.
            if summary_wanted and not summary_sent and dt.datetime.now().hour >= summary_time.hour:

                config_menu_units = dev.pluginProps.get('configMenuUnits', '')
                email_body        = u""
                email_list        = []
                location          = dev.pluginProps['location']

                weather_data = self.masterWeatherDict[location]

                temp_high_record_year        = int(self.nestedLookup(weather_data, keys=('almanac', 'temp_high', 'recordyear')))
                temp_low_record_year         = int(self.nestedLookup(weather_data, keys=('almanac', 'temp_low', 'recordyear')))
                today_record_high_metric     = self.nestedLookup(weather_data, keys=('almanac', 'temp_high', 'record', 'C'))
                today_record_high_standard   = self.nestedLookup(weather_data, keys=('almanac', 'temp_high', 'record', 'F'))
                today_record_low_metric      = self.nestedLookup(weather_data, keys=('almanac', 'temp_low', 'record', 'C'))
                today_record_low_standard    = self.nestedLookup(weather_data, keys=('almanac', 'temp_low', 'record', 'F'))

                forecast_today_metric        = self.nestedLookup(weather_data, keys=('forecast', 'txt_forecast', 'forecastday'))[0]['fcttext_metric']
                forecast_today_standard      = self.nestedLookup(weather_data, keys=('forecast', 'txt_forecast', 'forecastday'))[0]['fcttext']
                forecast_today_title         = self.nestedLookup(weather_data, keys=('forecast', 'txt_forecast', 'forecastday'))[0]['title']
                forecast_tomorrow_metric     = self.nestedLookup(weather_data, keys=('forecast', 'txt_forecast', 'forecastday'))[1]['fcttext_metric']
                forecast_tomorrow_standard   = self.nestedLookup(weather_data, keys=('forecast', 'txt_forecast', 'forecastday'))[1]['fcttext']
                forecast_tomorrow_title      = self.nestedLookup(weather_data, keys=('forecast', 'txt_forecast', 'forecastday'))[1]['title']
                max_humidity                 = self.nestedLookup(weather_data, keys=('forecast', 'simpleforecast', 'forecastday', 'maxhumidity'))
                today_high_metric            = self.nestedLookup(weather_data, keys=('forecast', 'simpleforecast', 'forecastday', 'high', 'celsius'))
                today_high_standard          = self.nestedLookup(weather_data, keys=('forecast', 'simpleforecast', 'forecastday', 'high', 'fahrenheit'))
                today_low_metric             = self.nestedLookup(weather_data, keys=('forecast', 'simpleforecast', 'forecastday', 'low', 'celsius'))
                today_low_standard           = self.nestedLookup(weather_data, keys=('forecast', 'simpleforecast', 'forecastday', 'low', 'fahrenheit'))
                today_qpf_metric             = self.nestedLookup(weather_data, keys=('forecast', 'simpleforecast', 'forecastday', 'qpf_allday', 'mm'))
                today_qpf_standard           = self.nestedLookup(weather_data, keys=('forecast', 'simpleforecast', 'forecastday', 'qpf_allday', 'in'))

                yesterday_high_temp_metric   = self.nestedLookup(weather_data, keys=('history', 'dailysummary', 'maxtempm'))
                yesterday_high_temp_standard = self.nestedLookup(weather_data, keys=('history', 'dailysummary', 'maxtempi'))
                yesterday_low_temp_metric    = self.nestedLookup(weather_data, keys=('history', 'dailysummary', 'mintempm'))
                yesterday_low_temp_standard  = self.nestedLookup(weather_data, keys=('history', 'dailysummary', 'mintempi'))
                yesterday_total_qpf_metric   = self.nestedLookup(weather_data, keys=('history', 'dailysummary', 'precipm'))
                yesterday_total_qpf_standard = self.nestedLookup(weather_data, keys=('history', 'dailysummary', 'precipi'))

                max_humidity                 = u"{0}".format(self.floatEverything(state_name="sendMailMaxHumidity", val=max_humidity))
                today_high_metric            = u"{0:.0f}C".format(self.floatEverything(state_name="sendMailHighC", val=today_high_metric))
                today_high_standard          = u"{0:.0f}F".format(self.floatEverything(state_name="sendMailHighF", val=today_high_standard))
                today_low_metric             = u"{0:.0f}C".format(self.floatEverything(state_name="sendMailLowC", val=today_low_metric))
                today_low_standard           = u"{0:.0f}F".format(self.floatEverything(state_name="sendMailLowF", val=today_low_standard))
                today_qpf_metric             = u"{0} mm.".format(self.floatEverything(state_name="sendMailQPF", val=today_qpf_metric))
                today_qpf_standard           = u"{0} in.".format(self.floatEverything(state_name="sendMailQPF", val=today_qpf_standard))
                today_record_high_metric     = u"{0:.0f}C".format(self.floatEverything(state_name="sendMailRecordHighC", val=today_record_high_metric))
                today_record_high_standard   = u"{0:.0f}F".format(self.floatEverything(state_name="sendMailRecordHighF", val=today_record_high_standard))
                today_record_low_metric      = u"{0:.0f}C".format(self.floatEverything(state_name="sendMailRecordLowC", val=today_record_low_metric))
                today_record_low_standard    = u"{0:.0f}F".format(self.floatEverything(state_name="sendMailRecordLowF", val=today_record_low_standard))
                yesterday_high_temp_metric   = u"{0:.0f}C".format(self.floatEverything(state_name="sendMailMaxTempM", val=yesterday_high_temp_metric))
                yesterday_high_temp_standard = u"{0:.0f}F".format(self.floatEverything(state_name="sendMailMaxTempI", val=yesterday_high_temp_standard))
                yesterday_low_temp_metric    = u"{0:.0f}C".format(self.floatEverything(state_name="sendMailMinTempM", val=yesterday_low_temp_metric))
                yesterday_low_temp_standard  = u"{0:.0f}F".format(self.floatEverything(state_name="sendMailMinTempI", val=yesterday_low_temp_standard))
                yesterday_total_qpf_metric   = u"{0} mm.".format(self.floatEverything(state_name="sendMailPrecipM", val=yesterday_total_qpf_metric))
                yesterday_total_qpf_standard = u"{0} in.".format(self.floatEverything(state_name="sendMailPrecipM", val=yesterday_total_qpf_standard))

                email_list.append(u"{0}".format(dev.name))

                if config_menu_units in ['M', 'MS']:
                    for element in [forecast_today_title, forecast_today_metric, forecast_tomorrow_title, forecast_tomorrow_metric, today_high_metric, today_low_metric, max_humidity,
                                    today_qpf_metric, today_record_high_metric, temp_high_record_year, today_record_low_metric, temp_low_record_year, yesterday_high_temp_metric,
                                    yesterday_low_temp_metric, yesterday_total_qpf_metric]:
                        try:
                            email_list.append(element)
                        except KeyError:
                            email_list.append(u"Not provided")

                elif config_menu_units in 'I':
                    for element in [forecast_today_title, forecast_today_metric, forecast_tomorrow_title, forecast_tomorrow_metric, today_high_metric, today_low_metric, max_humidity,
                                    today_qpf_standard, today_record_high_metric, temp_high_record_year, today_record_low_metric, temp_low_record_year, yesterday_high_temp_metric,
                                    yesterday_low_temp_metric, yesterday_total_qpf_standard]:
                        try:
                            email_list.append(element)
                        except KeyError:
                            email_list.append(u"Not provided")

                elif config_menu_units in 'S':
                    for element in [forecast_today_title, forecast_today_standard, forecast_tomorrow_title, forecast_tomorrow_standard, today_high_standard, today_low_standard,
                                    max_humidity, today_qpf_standard, today_record_high_standard, temp_high_record_year, today_record_low_standard, temp_low_record_year,
                                    yesterday_high_temp_standard, yesterday_low_temp_standard, yesterday_total_qpf_standard]:
                        try:
                            email_list.append(element)
                        except KeyError:
                            email_list.append(u"Not provided")

                email_list = tuple([u"--" if x == "" else x for x in email_list])  # Set value to u"--" if an empty string.

                email_body += u"{d[0]}\n" \
                              u"-------------------------------------------\n\n" \
                              u"{d[1]}:\n" \
                              u"{d[2]}\n\n" \
                              u"{d[3]}:\n" \
                              u"{d[4]}\n\n" \
                              u"Today:\n" \
                              u"-------------------------\n" \
                              u"High: {d[5]}\n" \
                              u"Low: {d[6]}\n" \
                              u"Humidity: {d[7]}%\n" \
                              u"Precipitation total: {d[8]}\n\n" \
                              u"Record:\n" \
                              u"-------------------------\n" \
                              u"High: {d[9]} ({d[10]})\n" \
                              u"Low: {d[11]} ({d[12]})\n\n" \
                              u"Yesterday:\n" \
                              u"-------------------------\n" \
                              u"High: {d[13]}\n" \
                              u"Low: {d[14]}\n" \
                              u"Precipitation: {d[15]}\n\n".format(d=email_list)

                indigo.server.sendEmailTo(self.pluginPrefs['updaterEmail'], subject=u"Daily Weather Summary", body=email_body)
                dev.updateStateOnServer('weatherSummaryEmailSent', value=True)
            else:
                pass

        except (KeyError, IndexError):
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            dev.updateStateOnServer('weatherSummaryEmailSent', value=True, uiValue=u"Err")
            self.logger.debug(u"Unable to compile forecast data for {0}.".format(dev.name))

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.warning(u"Unable to send forecast email message. Will keep trying.")

    def fixCorruptedData(self, state_name, val):
        """
        Format corrupted and missing data

        Sometimes WU receives corrupted data from personal weather stations. Could be
        zero, positive value or "--" or "-999.0" or "-9999.0". This method tries to
        "fix" these values for proper display.

        -----

        :param str state_name:
        :param str or float val:
        """

        try:
            val = float(val)

            if val < -55.728:  # -99 F = -55.728 C
                self.logger.debug(u"Formatted {0} data. Got: {1} Returning: (-99.0, --)".format(state_name, val))
                return -99.0, u"--"

            else:
                return val, str(val)

        except (ValueError, TypeError):
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.debug(u"Imputing {0} data. Got: {1} Returning: (-99.0, --)".format(state_name, val))
            return -99.0, u"--"

    def floatEverything(self, state_name, val):
        """
        Take value and return float

        This doesn't actually float everything. Select values are sent here to see if
        they float. If they do, a float is returned. Otherwise, a Unicode string is
        returned. This is necessary because Weather Underground will send values that
        won't float even when they're supposed to.

        -----

        :param str state_name:
        :param val:
        """

        try:
            return float(val)

        except (ValueError, TypeError):
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.debug(u"Error floating {0} (val = {1})".format(state_name, val))
            return -99.0

    def generatorTime(self, filter="", values_dict=None, type_id="", target_id=0):
        """
        List of hours generator

        Creates a list of times for use in setting the desired time for weather
        forecast emails to be sent.

        -----
        :param str filter:
        :param indigo.Dict values_dict:
        :param str type_id:
        :param int target_id:
        """

        return [(u"{0:02.0f}:00".format(hour), u"{0:02.0f}:00".format(hour)) for hour in range(0, 24)]

    def getLatLong(self, values_dict, type_id, dev_id):
        """
        Get server latitude and longitude

        Called when a device configuration dialog is opened. Returns the current
        latitude and longitude from the Indigo server.

        -----

        :param indigo.Dict values_dict:
        :param str type_id:
        :param int dev_id:
        """

        latitude, longitude = indigo.server.getLatitudeAndLongitude()
        values_dict['centerlat'] = latitude
        values_dict['centerlon'] = longitude

        return values_dict

    def getSatelliteImage(self, dev):
        """
        Download satellite image and save to file

        The getSatelliteImage() method will download a file from a user- specified
        location and save it to a user-specified folder on the local server. This
        method is used by the Satellite Image Downloader device type.

        -----

        :param indigo.Device dev:
        """

        destination = unicode(dev.pluginProps['imageDestinationLocation'])
        source      = unicode(dev.pluginProps['imageSourceLocation'])

        try:
            if destination.endswith((".gif", ".jpg", ".jpeg", ".png")):

                get_data_time = dt.datetime.now()

                # If requests doesn't work for some reason, revert to urllib.
                try:
                    self.logger.debug(u"Source: {0}".format(source))
                    self.logger.debug(u"Destination: {0}".format(destination))
                    r = requests.get(source, stream=True, timeout=20)

                    with open(destination, 'wb') as img:
                        for chunk in r.iter_content(2000):
                            img.write(chunk)

                except requests.exceptions.ConnectionError:
                    self.Fogbert.pluginErrorHandler(traceback.format_exc())
                    self.logger.warning(u"Error downloading satellite image. (No comm.)")
                    dev.updateStateOnServer('onOffState', value=False, uiValue=u"No comm")
                    dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
                    return

                # Requests not installed
                except NameError:
                    self.Fogbert.pluginErrorHandler(traceback.format_exc())
                    urllib.urlretrieve(source, destination)

                dev.updateStateOnServer('onOffState', value=True, uiValue=u" ")
                dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

                # Report results of download timer.
                data_cycle_time = (dt.datetime.now() - get_data_time)
                data_cycle_time = (dt.datetime.min + data_cycle_time).time()

                self.logger.debug(u"[  {0} download: {1} seconds  ]".format(dev.name, data_cycle_time.strftime('%S.%f')))

                return

            else:
                self.logger.error(u"The image destination must include one of the approved types (.gif, .jpg, .jpeg, .png)")
                dev.updateStateOnServer('onOffState', value=False, uiValue=u"Bad Type")
                dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
                return False

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"[{0}] Error downloading satellite image.")
            dev.updateStateOnServer('onOffState', value=False, uiValue=u"No comm")
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def getWUradar(self, dev):
        """
        Get radar image through WU API

        The getWUradar() method will download a satellite image from Weather
        Underground. The construction of the image is based upon user preferences
        defined in the WUnderground Radar device type.

        -----

        :param indigo.Device dev:
        """

        location    = u''
        name        = unicode(dev.pluginProps['imagename'])
        parms       = u''
        parms_dict = {
            'apiref': '97986dc4c4b7e764',
            'centerlat': float(dev.pluginProps.get('centerlat', 41.25)),
            'centerlon': float(dev.pluginProps.get('centerlon', -87.65)),
            'delay': int(dev.pluginProps.get('delay', 25)),
            'feature': dev.pluginProps.get('feature', True),
            'height': int(dev.pluginProps.get('height', 500)),
            'imagetype': dev.pluginProps.get('imagetype', 'radius'),
            'maxlat': float(dev.pluginProps.get('maxlat', 43.0)),
            'maxlon': float(dev.pluginProps.get('maxlon', -90.5)),
            'minlat': float(dev.pluginProps.get('minlat', 39.0)),
            'minlon': float(dev.pluginProps.get('minlon', -86.5)),
            'newmaps': dev.pluginProps.get('newmaps', False),
            'noclutter': dev.pluginProps.get('noclutter', True),
            'num': int(dev.pluginProps.get('num', 10)),
            'radius': float(dev.pluginProps.get('radius', 150)),
            'radunits': dev.pluginProps.get('radunits', 'nm'),
            'rainsnow': dev.pluginProps.get('rainsnow', True),
            'reproj.automerc': dev.pluginProps.get('Mercator', False),
            'smooth': dev.pluginProps.get('smooth', 1),
            'timelabel.x': int(dev.pluginProps.get('timelabelx', 10)),
            'timelabel.y': int(dev.pluginProps.get('timelabely', 20)),
            'timelabel': dev.pluginProps.get('timelabel', True),
            'width': int(dev.pluginProps.get('width', 500)),
        }

        try:

            # Type of image
            if parms_dict['feature']:
                radartype = 'animatedradar'
            else:
                radartype = 'radar'

            # Type of boundary
            if parms_dict['imagetype'] == 'radius':
                for key in ('minlat', 'minlon', 'maxlat', 'maxlon', 'imagetype',):
                    del parms_dict[key]

            elif parms_dict['imagetype'] == 'boundingbox':
                for key in ('centerlat', 'centerlon', 'radius', 'imagetype',):
                    del parms_dict[key]

            else:
                for key in ('minlat', 'minlon', 'maxlat', 'maxlon', 'imagetype', 'centerlat', 'centerlon', 'radius',):
                    location = u"q/{0}".format(dev.pluginProps['location'])
                    name = ''
                    del parms_dict[key]

            # If Mercator is 0, del the key
            if not parms_dict['reproj.automerc']:
                del parms_dict['reproj.automerc']

            for k, v in parms_dict.iteritems():

                # Convert boolean props to 0/1 for URL encode.
                if str(v) == 'False':
                    v = 0

                elif str(v) == 'True':
                    v = 1

                # Create string of parms for URL encode.
                if len(parms) < 1:
                    parms += "{0}={1}".format(k, v)

                else:
                    parms += "&{0}={1}".format(k, v)

            source = 'http://api.wunderground.com/api/{0}/{1}/{2}{3}{4}?{5}'.format(self.pluginPrefs['apiKey'], radartype, location, name, '.gif', parms)
            destination = "{0}/IndigoWebServer/images/controls/static/{1}.gif".format(indigo.server.getInstallFolderPath(), dev.pluginProps['imagename'])

            # If requests doesn't work for some reason, revert to urllib.
            try:

                get_data_time = dt.datetime.now()

                self.logger.debug(u"URL: {0}".format(source))
                r = requests.get(source, stream=True, timeout=20)
                self.logger.debug(u"Status code: {0}".format(r.status_code))

                if r.status_code == 200:
                    with open(destination, 'wb') as img:

                        for chunk in r.iter_content(1024):
                            img.write(chunk)

                    dev.updateStateOnServer('onOffState', value=True, uiValue=u" ")
                    dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

                    # Report results of download timer.
                    data_cycle_time = (dt.datetime.now() - get_data_time)
                    data_cycle_time = (dt.datetime.min + data_cycle_time).time()

                    self.logger.debug(u"[  {0} download: {1} seconds  ]".format(dev.name, data_cycle_time.strftime('%S.%f')))

                else:
                    self.logger.error(u"Error downloading image file: {0}".format(r.status_code))
                    raise NameError

            except requests.exceptions.ConnectionError:
                self.Fogbert.pluginErrorHandler(traceback.format_exc())
                self.logger.warning(u"Error downloading satellite image. (No comm.)")
                dev.updateStateOnServer('onOffState', value=False, uiValue=u"No comm")
                dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
                return

            # Requests not installed
            except NameError:
                self.Fogbert.pluginErrorHandler(traceback.format_exc())
                urllib.urlretrieve(source, destination)

            # Since this uses the API, go increment the call counter.
            self.callCount()

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Error downloading satellite image.")
            dev.updateStateOnServer('onOffState', value=False, uiValue=u"No comm")
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def getWeatherData(self, dev):
        """
        Reach out to Weather Underground and download data for this location

        Grab the JSON return for the device. A separate call must be made for each
        weather device because the data are location specific.

        -----

        :param indigo.Device dev:
        """

        try:

            location = dev.pluginProps.get('location', 'autoip')

            if location == 'autoip':
                self.logger.warning(u"[{0}]. Automatically determining your location using 'autoip'.".format(dev.name))

            if location in self.masterWeatherDict.keys():
                # We already have the data; no need to get it again.
                self.logger.debug(u"Location [{0}] already in master weather dictionary.".format(location))

            else:
                # Get the data and add it to the masterWeatherDict.
                url = (u"http://api.wunderground.com/api/{0}/geolookup/alerts_v11/almanac_v11/astronomy_v11/conditions_v11/forecast10day_v11/hourly_v11/lang:{1}/"
                       u"yesterday_v11/tide_v11/q/{2}.json?apiref=97986dc4c4b7e764".format(self.pluginPrefs['apiKey'], self.pluginPrefs['language'], location))

                self.logger.debug(u"URL for {0}: {1}".format(location, url))

                # Start download timer.
                get_data_time = dt.datetime.now()

                try:
                    f = requests.get(url, timeout=20)
                    simplejson_string = f.text  # We convert the file to a json object below, so we don't use requests' built-in decoder.

                # If requests is not installed, try urllib2 instead.
                except NameError:
                    self.Fogbert.pluginErrorHandler(traceback.format_exc())
                    try:
                        # Connect to Weather Underground and retrieve data.
                        socket.setdefaulttimeout(20)
                        f = urllib2.urlopen(url)
                        simplejson_string = f.read()

                    except Exception:
                        self.Fogbert.pluginErrorHandler(traceback.format_exc())
                        self.logger.warning(u"Unable to reach Weather Underground. Sleeping until next scheduled poll.")
                        self.logger.debug(u"Unable to reach Weather Underground after 20 seconds.")
                        for dev in indigo.devices.itervalues("self"):
                            dev.updateStateOnServer("onOffState", value=False, uiValue=u" ")
                            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
                        return

                # Report results of download timer.
                data_cycle_time = (dt.datetime.now() - get_data_time)
                data_cycle_time = (dt.datetime.min + data_cycle_time).time()

                if simplejson_string != "":
                    self.logger.debug(u"[  {0} download: {1} seconds  ]".format(location, data_cycle_time.strftime('%S.%f')))

                # Load the JSON data from the file.
                try:
                    parsed_simplejson = simplejson.loads(simplejson_string, encoding="utf-8")
                except Exception:
                    self.Fogbert.pluginErrorHandler(traceback.format_exc())
                    self.logger.error(u"Unable to decode data.")
                    parsed_simplejson = {}

                # Add location JSON to master weather dictionary.
                self.logger.debug(u"Adding weather data for {0} to Master Weather Dictionary.".format(location))
                self.masterWeatherDict[location] = parsed_simplejson

                # Increment (or reset) the call counter.
                self.callCount()

                # We've been successful, mark device online
                dev.updateStateOnServer('onOffState', value=True)

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.warning(u"Unable to reach Weather Underground. Sleeping until next scheduled poll.")
            self.logger.debug(u"Unable to reach Weather Underground after 20 seconds.")

            # Unable to fetch the JSON. Mark all devices as 'false'.
            for dev in indigo.devices.itervalues("self"):
                if dev.enabled:
                    # Mark device as off and dim the icon.
                    dev.updateStateOnServer('onOffState', value=False, uiValue=u"No comm")
                    dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

            self.wuOnline = False

        # We could have come here from several different places. Return to whence we came to further process the weather data.
        self.wuOnline = True
        return self.masterWeatherDict

    def listOfDevices(self, filter, values_dict, target_id, trigger_id):
        """
        Generate list of devices for offline trigger

        listOfDevices returns a list of plugin devices limited to weather
        devices only (not forecast devices, etc.) when the Weather Location Offline
        trigger is fired.

        -----

        :param str filter:
        :param indigo.Dict values_dict:
        :param str target_id:
        :param int trigger_id:
        """

        return self.Fogbert.deviceList()

    def listOfWeatherDevices(self, filter, values_dict, target_id, trigger_id):
        """
        Generate list of devices for severe weather alert trigger

        listOfDevices returns a list of plugin devices limited to weather devices only
        (not forecast devices, etc.) when severe weather alert trigger is fired.

        -----

        :param str filter:
        :param indigo.Dict values_dict:
        :param str target_id:
        :param int trigger_id:
        """

        return self.Fogbert.deviceList(filter='self.wunderground')

    def nestedLookup(self, obj, keys, default=u"Not available"):
        """
        Do a nested lookup of the WU JSON

        The nestedLookup() method is used to extract the relevant data from the Weather
        Underground JSON return. The JSON is known to be inconsistent in the form of
        sometimes missing keys. This method allows for a default value to be used in
        instances where a key is missing. The method call can rely on the default
        return, or send an optional 'default=some_value' parameter.

        Credit: Jared Goguen at StackOverflow for initial implementation.

        -----

        :param obj:
        :param keys:
        :param default:
        """

        current = obj

        for key in keys:
            current = current if isinstance(current, list) else [current]

            try:
                current = next(sub[key] for sub in current if key in sub)

            except StopIteration:
                self.Fogbert.pluginErrorHandler(traceback.format_exc())
                return default

        return current

    def parseAlmanacData(self, dev):
        """
        Parse almanac data to devices

        The parseAlmanacData() method takes selected almanac data and parses it
        to device states.

        -----

        :param indigo.Device dev:
        """

        try:
            almanac_states_list  = []
            location             = dev.pluginProps['location']
            weather_data         = self.masterWeatherDict[location]

            airport_code              = self.nestedLookup(weather_data, keys=('almanac', 'airport_code'))
            current_observation       = self.nestedLookup(weather_data, keys=('current_observation', 'observation_time'))
            current_observation_epoch = self.nestedLookup(weather_data, keys=('current_observation', 'observation_epoch'))
            station_id                = self.nestedLookup(weather_data, keys=('current_observation', 'station_id'))

            no_ui_format = {'tempHighRecordYear': self.nestedLookup(weather_data, keys=('almanac', 'temp_high', 'recordyear')),
                            'tempLowRecordYear':  self.nestedLookup(weather_data, keys=('almanac', 'temp_low', 'recordyear'))
                            }

            ui_format_temp = {'tempHighNormalC': self.nestedLookup(weather_data, keys=('almanac', 'temp_high', 'normal', 'C')),
                              'tempHighNormalF': self.nestedLookup(weather_data, keys=('almanac', 'temp_high', 'normal', 'F')),
                              'tempHighRecordC': self.nestedLookup(weather_data, keys=('almanac', 'temp_high', 'record', 'C')),
                              'tempHighRecordF': self.nestedLookup(weather_data, keys=('almanac', 'temp_high', 'record', 'F')),
                              'tempLowNormalC':  self.nestedLookup(weather_data, keys=('almanac', 'temp_low', 'normal', 'C')),
                              'tempLowNormalF':  self.nestedLookup(weather_data, keys=('almanac', 'temp_low', 'normal', 'F')),
                              'tempLowRecordC':  self.nestedLookup(weather_data, keys=('almanac', 'temp_low', 'record', 'C')),
                              'tempLowRecordF':  self.nestedLookup(weather_data, keys=('almanac', 'temp_low', 'record', 'F'))
                              }

            almanac_states_list.append({'key': 'airportCode', 'value': airport_code, 'uiValue': airport_code})
            almanac_states_list.append({'key': 'currentObservation', 'value': current_observation, 'uiValue': current_observation})
            almanac_states_list.append({'key': 'currentObservationEpoch', 'value': current_observation_epoch, 'uiValue': current_observation_epoch})

            # Current Observation Time 24 Hour (string)
            current_observation_24hr = time.strftime("{0} {1}".format(self.date_format, self.time_format), time.localtime(int(current_observation_epoch)))
            almanac_states_list.append({'key': 'currentObservation24hr', 'value': current_observation_24hr, 'uiValue': current_observation_24hr})

            for key, value in no_ui_format.iteritems():
                value, ui_value = self.fixCorruptedData(state_name=key, val=value)  # fixCorruptedData() returns float, unicode string
                almanac_states_list.append({'key': key, 'value': int(value), 'uiValue': ui_value})

            for key, value in ui_format_temp.iteritems():
                value, ui_value = self.fixCorruptedData(state_name=key, val=value)
                ui_value = self.uiFormatTemperature(dev=dev, state_name=key, val=ui_value)  # uiFormatTemperature() returns unicode string
                almanac_states_list.append({'key': key, 'value': value, 'uiValue': ui_value})

            new_props = dev.pluginProps
            new_props['address'] = station_id
            dev.replacePluginPropsOnServer(new_props)
            almanac_states_list.append({'key': 'onOffState', 'value': True, 'uiValue': u" "})
            dev.updateStatesOnServer(almanac_states_list)
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        except (KeyError, ValueError):
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing almanac data.")
            dev.updateStateOnServer('onOffState', value=False, uiValue=u" ")
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def parseAlertsData(self, dev):
        """
        Parse alerts data to devices

        The parseAlertsData() method takes weather alert data and parses it to device
        states.

        -----

        :param indigo.Device dev:
        """

        attribution        = u""
        alerts_states_list = []

        alerts_suppressed = dev.pluginProps.get('suppressWeatherAlerts', False)
        location          = dev.pluginProps['location']
        weather_data      = self.masterWeatherDict[location]

        alert_logging    = self.pluginPrefs.get('alertLogging', True)
        no_alert_logging = self.pluginPrefs.get('noAlertLogging', False)

        alerts_data   = self.nestedLookup(weather_data, keys=('alerts',))
        location_city = self.nestedLookup(weather_data, keys=('location', 'city'))

        current_observation       = self.nestedLookup(weather_data, keys=('current_observation', 'observation_time'))
        current_observation_epoch = self.nestedLookup(weather_data, keys=('current_observation', 'observation_epoch'))

        try:
            alerts_states_list.append({'key': 'currentObservation', 'value': current_observation, 'uiValue': current_observation})
            alerts_states_list.append({'key': 'currentObservationEpoch', 'value': current_observation_epoch, 'uiValue': current_observation_epoch})

            # Current Observation Time 24 Hour (string)
            current_observation_24hr = time.strftime("{0} {1}".format(self.date_format, self.time_format), time.localtime(int(current_observation_epoch)))
            alerts_states_list.append({'key': 'currentObservation24hr', 'value': current_observation_24hr})

            # Alerts: This segment iterates through all available alert information. It retains only the first five alerts. We set all alerts to an empty string each time, and then
            # repopulate (this clears out alerts that may have expired.) If there are no alerts, set alert status to false.

            # Reset alert states (1-5).
            for alert_counter in range(1, 6):
                alerts_states_list.append({'key': 'alertDescription{0}'.format(alert_counter), 'value': u" ", 'uiValue': u" "})
                alerts_states_list.append({'key': 'alertExpires{0}'.format(alert_counter), 'value': u" ", 'uiValue': u" "})
                alerts_states_list.append({'key': 'alertMessage{0}'.format(alert_counter), 'value': u" ", 'uiValue': u" "})
                alerts_states_list.append({'key': 'alertType{0}'.format(alert_counter), 'value': u" ", 'uiValue': u" "})

            # If there are no alerts (the list is empty):
            if not alerts_data:
                alerts_states_list.append({'key': 'alertStatus', 'value': "false", 'uiValue': u"False"})

                if alert_logging and not no_alert_logging and not alerts_suppressed:
                    self.logger.info(u"There are no severe weather alerts for the {0} location.".format(location_city))

            # If there is at least one alert (the list is not empty):
            else:
                alert_array = []
                alerts_states_list.append({'key': 'alertStatus', 'value': "true", 'uiValue': u"True"})

                for item in alerts_data:

                    # Strip whitespace from the ends.
                    alert_text = u"{0}".format(item['message'].strip())

                    # Create a tuple of each alert within the master dict and add it to the array. alert_tuple = (type, description, alert text, expires)
                    alert_tuple = (u"{0}".format(item['type']),
                                   u"{0}".format(item['description']),
                                   u"{0}".format(alert_text),
                                   u"{0}".format(item['expires'])
                                   )

                    alert_array.append(alert_tuple)

                    # Per Weather Underground TOS, attribution must be provided for European weather alert source. If appropriate, write it to the log.
                    try:
                        # Attempt to clean out HTML tags.
                        tag_re      = re.compile(r'(<!--.*?-->|<[^>]*>)')
                        no_tags     = tag_re.sub('', item['attribution'])  # Remove well-formed tags
                        clean       = cgi.escape(no_tags)  # Clean up anything else by escaping
                        attribution = u"European weather alert {0}".format(clean)

                    except (KeyError, Exception):
                        self.Fogbert.pluginErrorHandler(traceback.format_exc())
                        attribution = u""

                if len(alert_array) == 1:
                    # If user has enabled alert logging, write alert message to the Indigo log.
                    if alert_logging and not alerts_suppressed:
                        self.logger.info(u"There is 1 severe weather alert for the {0} location:".format(location_city))
                else:
                    # If user has enabled alert logging, write alert message to the Indigo log.
                    if alert_logging and not alerts_suppressed:
                        self.logger.info(u"There are {0} severe weather alerts for the {1} location:".format(len(alert_array), u"{0}".format(location_city)))

                    # If user has enabled alert logging, write alert message to the Indigo log.
                    if alert_logging and not alerts_suppressed and len(alert_array) > 4:
                        self.logger.info(u"The plugin only retains information for the first 5 alerts.")

                # Debug output can contain sensitive data.
                self.logger.debug(u"{0}".format(alert_array))

                alert_counter = 1
                for alert in range(len(alert_array)):
                    if alert_counter < 6:
                        alerts_states_list.append({'key': u"alertType{0}".format(alert_counter), 'value': u"{0}".format(alert_array[alert][0])})
                        alerts_states_list.append({'key': u"alertDescription{0}".format(alert_counter), 'value': u"{0}".format(alert_array[alert][1])})
                        alerts_states_list.append({'key': u"alertMessage{0}".format(alert_counter), 'value': u"{0}".format(alert_array[alert][2])})
                        alerts_states_list.append({'key': u"alertExpires{0}".format(alert_counter), 'value': u"{0}".format(alert_array[alert][3])})
                        alert_counter += 1

                    if alert_logging and not alerts_suppressed:
                        self.logger.info(u"\n{0}".format(alert_array[alert][2]))

            if attribution != u"":
                self.logger.info(attribution)

            dev.updateStatesOnServer(alerts_states_list)

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing weather alert data:")
            alerts_states_list.append({'key': 'onOffState', 'value': False, 'uiValue': u" "})
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def parseAstronomyData(self, dev):
        """
        Parse astronomy data to devices

        The parseAstronomyData() method takes astronomy data and parses it to device
        states::

            Age of Moon (Integer: 0 - 31, units: days)
            Current Time Hour (Integer: 0 - 23, units: hours)
            Current Time Minute (Integer: 0 - 59, units: minutes)
            Hemisphere (String: North, South)
            Percent Illuminated (Integer: 0 - 100, units: percentage)
            Phase of Moon (String: Full, Waning Crescent...)

        Phase of Moon Icon (String, no spaces) 8 principal and intermediate phases::

            =========================================================
            1. New Moon (P): + New_Moon
            2. Waxing Crescent (I): + Waxing_Crescent
            3. First Quarter (P): + First_Quarter
            4. Waxing Gibbous (I): + Waxing_Gibbous
            5. Full Moon (P): + Full_Moon
            6. Waning Gibbous (I): + Waning_Gibbous
            7. Last Quarter (P): + Last_Quarter
            8. Waning Crescent (I): + Waning_Crescent

        -----

        :param indigo.Device dev:
        """

        astronomy_states_list = []
        location              = dev.pluginProps['location']

        weather_data = self.masterWeatherDict[location]

        current_observation       = self.nestedLookup(weather_data, keys=('current_observation', 'observation_time'))
        current_observation_epoch = self.nestedLookup(weather_data, keys=('current_observation', 'observation_epoch'))
        percent_illuminated       = self.nestedLookup(weather_data, keys=('moon_phase', 'percentIlluminated'))
        station_id                = self.nestedLookup(weather_data, keys=('current_observation', 'station_id'))

        astronomy_dict = {'ageOfMoon':              self.nestedLookup(weather_data, keys=('moon_phase', 'ageOfMoon')),
                          'currentTimeHour':        self.nestedLookup(weather_data, keys=('moon_phase', 'current_time', 'hour')),
                          'currentTimeMinute':      self.nestedLookup(weather_data, keys=('moon_phase', 'current_time', 'minute')),
                          'hemisphere':             self.nestedLookup(weather_data, keys=('moon_phase', 'hemisphere')),
                          'phaseOfMoon':            self.nestedLookup(weather_data, keys=('moon_phase', 'phaseofMoon')),
                          'sunriseHourMoonphase':   self.nestedLookup(weather_data, keys=('moon_phase', 'sunrise', 'hour')),
                          'sunriseHourSunphase':    self.nestedLookup(weather_data, keys=('sun_phase', 'sunrise', 'hour')),
                          'sunriseMinuteMoonphase': self.nestedLookup(weather_data, keys=('moon_phase', 'sunrise', 'minute')),
                          'sunriseMinuteSunphase':  self.nestedLookup(weather_data, keys=('sun_phase', 'sunrise', 'minute')),
                          'sunsetHourMoonphase':    self.nestedLookup(weather_data, keys=('moon_phase', 'sunset', 'hour')),
                          'sunsetHourSunphase':     self.nestedLookup(weather_data, keys=('sun_phase', 'sunset', 'hour')),
                          'sunsetMinuteMoonphase':  self.nestedLookup(weather_data, keys=('moon_phase', 'sunset', 'minute')),
                          'sunsetMinuteSunphase':   self.nestedLookup(weather_data, keys=('sun_phase', 'sunset', 'minute'))
                          }

        try:
            astronomy_states_list.append({'key': 'currentObservation', 'value': current_observation, 'uiValue': current_observation})
            astronomy_states_list.append({'key': 'currentObservationEpoch', 'value': current_observation_epoch, 'uiValue': current_observation_epoch})

            # Current Observation Time 24 Hour (string)
            current_observation_24hr = time.strftime("{0} {1}".format(self.date_format, self.time_format), time.localtime(int(current_observation_epoch)))
            astronomy_states_list.append({'key': 'currentObservation24hr', 'value': current_observation_24hr, 'uiValue': current_observation_24hr})

            for key, value in astronomy_dict.iteritems():
                astronomy_states_list.append({'key': key, 'value': value, 'uiValue': value})

            phase_of_moon = astronomy_dict['phaseOfMoon'].replace(' ', '_')
            astronomy_states_list.append({'key': 'phaseOfMoonIcon', 'value': phase_of_moon, 'uiValue': phase_of_moon})

            # Percent illuminated is excluded from the astronomy dict for further processing.
            percent_illuminated = self.floatEverything(state_name="Percent Illuminated", val=percent_illuminated)
            astronomy_states_list.append({'key': 'percentIlluminated', 'value': percent_illuminated, 'uiValue': u"{0}".format(percent_illuminated)})

            # ========================= NEW =========================
            # Sunrise and Sunset states

            # Get today's date
            year = dt.datetime.today().year
            month = dt.datetime.today().month
            day = dt.datetime.today().day
            datetime_formatter = "{0} {1}".format(self.date_format, self.time_format)  # Get the latest format preferences

            sunrise = dt.datetime(year, month, day, int(astronomy_dict['sunriseHourMoonphase']), int(astronomy_dict['sunriseMinuteMoonphase']))
            sunset = dt.datetime(year, month, day, int(astronomy_dict['sunsetHourMoonphase']), int(astronomy_dict['sunsetMinuteMoonphase']))

            sunrise_string = dt.datetime.strftime(sunrise, datetime_formatter)
            astronomy_states_list.append({'key': 'sunriseString', 'value': sunrise_string})

            sunset_string = dt.datetime.strftime(sunset, datetime_formatter)
            astronomy_states_list.append({'key': 'sunsetString', 'value': sunset_string})

            sunrise_epoch = int(time.mktime(sunrise.timetuple()))
            astronomy_states_list.append({'key': 'sunriseEpoch', 'value': sunrise_epoch})

            sunset_epoch = int(time.mktime(sunset.timetuple()))
            astronomy_states_list.append({'key': 'sunsetEpoch', 'value': sunset_epoch})

            new_props = dev.pluginProps
            new_props['address'] = station_id
            dev.replacePluginPropsOnServer(new_props)
            astronomy_states_list.append({'key': 'onOffState', 'value': True, 'uiValue': u" "})
            dev.updateStatesOnServer(astronomy_states_list)
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing astronomy data.")
            dev.updateStateOnServer('onOffState', value=False, uiValue=u" ")
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def parseForecastData(self, dev):
        """
        Parse forecast data to devices

        The parseForecastData() method takes weather forecast data and parses it to
        device states. (Note that this is only for the weather device and not for the
        hourly or 10 day forecast devices which have their own methods.) Note that we
        round most of the values because some PWSs report decimal precision (even
        though that doesn't make sense since it's unlikely that a site would forecast
        with that level of precision.

        -----

        :param indigo.Device dev:
        """

        forecast_states_list = []
        config_menu_units    = dev.pluginProps.get('configMenuUnits', '')
        location             = dev.pluginProps['location']
        wind_units           = dev.pluginProps.get('windUnits', '')

        weather_data = self.masterWeatherDict[location]

        forecast_data_text   = self.nestedLookup(weather_data, keys=('forecast', 'txt_forecast', 'forecastday'))
        forecast_data_simple = self.nestedLookup(weather_data, keys=('forecast', 'simpleforecast', 'forecastday'))

        try:
            # Metric:
            if config_menu_units in ['M', 'MS']:

                fore_counter = 1
                for day in forecast_data_text:

                    if fore_counter <= 8:
                        fore_text = self.nestedLookup(day, keys=('fcttext_metric',)).lstrip('\n')
                        icon      = self.nestedLookup(day, keys=('icon',))
                        title     = self.nestedLookup(day, keys=('title',))

                        forecast_states_list.append({'key': u"foreText{0}".format(fore_counter), 'value': fore_text, 'uiValue': fore_text})
                        forecast_states_list.append({'key': u"icon{0}".format(fore_counter), 'value': icon, 'uiValue': icon})
                        forecast_states_list.append({'key': u"foreTitle{0}".format(fore_counter), 'value': title, 'uiValue': title})
                        fore_counter += 1

                fore_counter = 1
                for day in forecast_data_simple:

                    if fore_counter <= 4:
                        average_wind = self.nestedLookup(day, keys=('avewind', 'kph'))
                        conditions   = self.nestedLookup(day, keys=('conditions',))
                        fore_day     = self.nestedLookup(day, keys=('date', 'weekday'))
                        fore_high    = self.nestedLookup(day, keys=('high', 'celsius'))
                        fore_low     = self.nestedLookup(day, keys=('low', 'celsius'))
                        icon         = self.nestedLookup(day, keys=('icon',))
                        max_humidity = self.nestedLookup(day, keys=('maxhumidity',))
                        pop          = self.nestedLookup(day, keys=('pop',))

                        # Wind in KPH or MPS?
                        value, ui_value = self.fixCorruptedData(state_name="foreWind{0}".format(fore_counter), val=average_wind)  # fixCorruptedData() returns float, unicode string
                        if config_menu_units == 'MS':
                            value = value / 3.6
                            ui_value = self.uiFormatWind(dev=dev, state_name="foreWind{0}".format(fore_counter), val=value)
                            forecast_states_list.append({'key': u"foreWind{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        else:
                            ui_value = self.uiFormatWind(dev=dev, state_name="foreWind{0}".format(fore_counter), val=ui_value)
                            forecast_states_list.append({'key': u"foreWind{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        forecast_states_list.append({'key': u"conditions{0}".format(fore_counter), 'value': conditions, 'uiValue': conditions})
                        forecast_states_list.append({'key': u"foreDay{0}".format(fore_counter), 'value': fore_day, 'uiValue': fore_day})

                        value, ui_value = self.fixCorruptedData(state_name="foreHigh{0}".format(fore_counter), val=fore_high)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="foreHigh{0}".format(fore_counter), val=ui_value)  # uiFormatTemperature() returns unicode string
                        forecast_states_list.append({'key': u"foreHigh{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="foreLow{0}".format(fore_counter), val=fore_low)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="foreLow{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreLow{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="foreHum{0}".format(fore_counter), val=max_humidity)
                        ui_value = self.uiFormatPercentage(dev=dev, state_name="foreHum{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreHum{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        forecast_states_list.append({'key': u"foreIcon{0}".format(fore_counter), 'value': icon, 'uiValue': icon})

                        value, ui_value = self.fixCorruptedData(state_name="forePop{0}".format(fore_counter), val=pop)
                        ui_value = self.uiFormatPercentage(dev=dev, state_name="forePop{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"forePop{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        fore_counter += 1

            # Mixed:
            elif config_menu_units == 'I':

                fore_counter = 1
                for day in forecast_data_text:

                    if fore_counter <= 8:
                        fore_text = self.nestedLookup(day, keys=('fcttext_metric',)).lstrip('\n')
                        icon      = self.nestedLookup(day, keys=('icon',))
                        title     = self.nestedLookup(day, keys=('title',))

                        forecast_states_list.append({'key': u"foreText{0}".format(fore_counter), 'value': fore_text, 'uiValue': fore_text})
                        forecast_states_list.append({'key': u"icon{0}".format(fore_counter), 'value': icon, 'uiValue': icon})
                        forecast_states_list.append({'key': u"foreTitle{0}".format(fore_counter), 'value': title, 'uiValue': title})
                        fore_counter += 1

                fore_counter = 1
                for day in forecast_data_simple:

                    if fore_counter <= 4:

                        average_wind = self.nestedLookup(day, keys=('avewind', 'mph'))
                        conditions   = self.nestedLookup(day, keys=('conditions',))
                        fore_day     = self.nestedLookup(day, keys=('date', 'weekday'))
                        fore_high    = self.nestedLookup(day, keys=('high', 'celsius'))
                        fore_low     = self.nestedLookup(day, keys=('low', 'celsius'))
                        icon         = self.nestedLookup(day, keys=('icon',))
                        max_humidity = self.nestedLookup(day, keys=('maxhumidity',))
                        pop          = self.nestedLookup(day, keys=('pop',))

                        value, ui_value = self.fixCorruptedData(state_name="foreWind{0}".format(fore_counter), val=average_wind)
                        ui_value = self.uiFormatWind(dev=dev, state_name="foreWind{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreWind{0}".format(fore_counter), 'value': round(value), 'uiValue': u"{0}".format(ui_value, wind_units)})

                        forecast_states_list.append({'key': u"conditions{0}".format(fore_counter), 'value': conditions, 'uiValue': conditions})
                        forecast_states_list.append({'key': u"foreDay{0}".format(fore_counter), 'value': fore_day, 'uiValue': fore_day})

                        value, ui_value = self.fixCorruptedData(state_name="foreHigh{0}".format(fore_counter), val=fore_high)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="foreHigh{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreHigh{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="foreLow{0}".format(fore_counter), val=fore_low)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="foreLow{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreLow{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="foreHum{0}".format(fore_counter), val=max_humidity)
                        ui_value = self.uiFormatPercentage(dev=dev, state_name="foreHum{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreHum{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        forecast_states_list.append({'key': u"foreIcon{0}".format(fore_counter), 'value': icon, 'uiValue': icon})

                        value, ui_value = self.fixCorruptedData(state_name="forePop{0}".format(fore_counter), val=pop)
                        ui_value = self.uiFormatPercentage(dev=dev, state_name="forePop{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"forePop{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        fore_counter += 1

            # Standard:
            else:

                fore_counter = 1
                for day in forecast_data_text:

                    if fore_counter <= 8:
                        fore_text = self.nestedLookup(day, keys=('fcttext',)).lstrip('\n')
                        icon      = self.nestedLookup(day, keys=('icon',))
                        title     = self.nestedLookup(day, keys=('title',))

                        forecast_states_list.append({'key': u"foreText{0}".format(fore_counter), 'value': fore_text, 'uiValue': fore_text})
                        forecast_states_list.append({'key': u"icon{0}".format(fore_counter), 'value': icon, 'uiValue': icon})
                        forecast_states_list.append({'key': u"foreTitle{0}".format(fore_counter), 'value': title, 'uiValue': title})
                        fore_counter += 1

                fore_counter = 1
                for day in forecast_data_simple:

                    if fore_counter <= 4:
                        average_wind = self.nestedLookup(day, keys=('avewind', 'mph'))
                        conditions   = self.nestedLookup(day, keys=('conditions',))
                        fore_day     = self.nestedLookup(day, keys=('date', 'weekday'))
                        fore_high    = self.nestedLookup(day, keys=('high', 'fahrenheit'))
                        fore_low     = self.nestedLookup(day, keys=('low', 'fahrenheit'))
                        max_humidity = self.nestedLookup(day, keys=('maxhumidity',))
                        icon         = self.nestedLookup(day, keys=('icon',))
                        pop          = self.nestedLookup(day, keys=('pop',))

                        value, ui_value = self.fixCorruptedData(state_name="foreWind{0}".format(fore_counter), val=average_wind)
                        ui_value = self.uiFormatWind(dev=dev, state_name="foreWind{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreWind{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        forecast_states_list.append({'key': u"conditions{0}".format(fore_counter), 'value': conditions, 'uiValue': conditions})
                        forecast_states_list.append({'key': u"foreDay{0}".format(fore_counter), 'value': fore_day, 'uiValue': fore_day})

                        value, ui_value = self.fixCorruptedData(state_name="foreHigh{0}".format(fore_counter), val=fore_high)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="foreHigh{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreHigh{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="foreLow{0}".format(fore_counter), val=fore_low)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="foreLow{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreLow{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="foreHum{0}".format(fore_counter), val=max_humidity)
                        ui_value = self.uiFormatPercentage(dev=dev, state_name="foreHum{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"foreHum{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        forecast_states_list.append({'key': u"foreIcon{0}".format(fore_counter), 'value': icon, 'uiValue': icon})

                        value, ui_value = self.fixCorruptedData(state_name="forePop{0}".format(fore_counter), val=pop)
                        ui_value = self.uiFormatPercentage(dev=dev, state_name="forePop{0}".format(fore_counter), val=ui_value)
                        forecast_states_list.append({'key': u"forePop{0}".format(fore_counter), 'value': round(value), 'uiValue': ui_value})

                        fore_counter += 1

        except (KeyError, Exception):
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing weather forecast data.")

        # Determine how today's forecast compares to yesterday.
        try:
            diff_text = u""

            try:
                difference = float(dev.states['foreHigh1']) - float(dev.states['historyHigh'])

            except ValueError:
                difference = -99

            if difference == -99:
                diff_text = u"unknown"

            elif difference <= -5:
                diff_text = u"much colder"

            elif -5 < difference <= -1:
                diff_text = u"colder"

            elif -1 < difference <= 1:
                diff_text = u"about the same"

            elif 1 < difference <= 5:
                diff_text = u"warmer"

            elif 5 < difference:
                diff_text = u"much warmer"

            forecast_states_list.append({'key': 'foreTextShort', 'value': diff_text, 'uiValue': diff_text})

            if diff_text != u"unknown":
                forecast_states_list.append({'key': 'foreTextLong', 'value': u"Today is forecast to be {0} than yesterday.".format(diff_text)})

            else:
                forecast_states_list.append({'key': 'foreTextLong', 'value': u"Unable to compare today's forecast with yesterday's high temperature."})

            dev.updateStatesOnServer(forecast_states_list)

        except (KeyError, Exception):
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem comparing forecast and history data.")

            for state in ['foreTextShort', 'foreTextLong']:
                forecast_states_list.append({'key': state, 'value': u"Unknown", 'uiValue': u"Unknown"})

            dev.updateStatesOnServer(forecast_states_list)

    def parseHourlyData(self, dev):
        """
        Parse hourly forecast data to devices

        The parseHourlyData() method takes hourly weather forecast data and parses it
        to device states.

        -----

        :param indigo.Device dev:
        """

        hourly_forecast_states_list = []
        config_menu_units           = dev.pluginProps.get('configMenuUnits', '')
        location                    = dev.pluginProps['location']

        weather_data  = self.masterWeatherDict[location]
        forecast_data = self.nestedLookup(weather_data, keys=('hourly_forecast',))

        current_observation_epoch = self.nestedLookup(weather_data, keys=('current_observation', 'observation_epoch'))
        current_observation_time  = self.nestedLookup(weather_data, keys=('current_observation', 'observation_time'))
        station_id                = self.nestedLookup(weather_data, keys=('current_observation', 'station_id'))

        try:
            hourly_forecast_states_list.append({'key': 'currentObservation', 'value': current_observation_time, 'uiValue': current_observation_time})
            hourly_forecast_states_list.append({'key': 'currentObservationEpoch', 'value': current_observation_epoch, 'uiValue': current_observation_epoch})

            current_observation_24hr = time.strftime("{0} {1}".format(self.date_format, self.time_format), time.localtime(int(current_observation_epoch)))
            hourly_forecast_states_list.append({'key': 'currentObservation24hr', 'value': u"{0}".format(current_observation_24hr)})

            fore_counter = 1
            for observation in forecast_data:

                if fore_counter <= 24:

                    civil_time          = self.nestedLookup(observation, keys=('FCTTIME', 'civil'))
                    condition           = self.nestedLookup(observation, keys=('condition',))
                    day                 = self.nestedLookup(observation, keys=('FCTTIME', 'mday_padded'))
                    fore_humidity       = self.nestedLookup(observation, keys=('humidity',))
                    fore_pop            = self.nestedLookup(observation, keys=('pop',))
                    fore_qpf_metric     = self.nestedLookup(observation, keys=('qpf', 'metric'))
                    fore_qpf_standard   = self.nestedLookup(observation, keys=('qpf', 'english'))
                    fore_snow_metric    = self.nestedLookup(observation, keys=('snow', 'metric'))
                    fore_snow_standard  = self.nestedLookup(observation, keys=('snow', 'english'))
                    fore_temp_metric    = self.nestedLookup(observation, keys=('temp', 'metric'))
                    fore_temp_standard  = self.nestedLookup(observation, keys=('temp', 'english'))
                    hour                = self.nestedLookup(observation, keys=('FCTTIME', 'hour_padded'))
                    icon                = self.nestedLookup(observation, keys=('icon',))
                    minute              = self.nestedLookup(observation, keys=('FCTTIME', 'min'))
                    month               = self.nestedLookup(observation, keys=('FCTTIME', 'mon_padded'))
                    wind_degrees        = self.nestedLookup(observation, keys=('wdir', 'degrees'))
                    wind_dir            = self.nestedLookup(observation, keys=('wdir', 'dir'))
                    wind_speed_metric   = self.nestedLookup(observation, keys=('wspd', 'metric'))
                    wind_speed_standard = self.nestedLookup(observation, keys=('wspd', 'english'))
                    year                = self.nestedLookup(observation, keys=('FCTTIME', 'year'))

                    wind_speed_mps = "{0}".format(float(wind_speed_metric) * 0.277778)

                    # Add leading zero to counter value for device state names 1-9.
                    if fore_counter < 10:
                        fore_counter_text = u"0{0}".format(fore_counter)
                    else:
                        fore_counter_text = fore_counter

                    # Values that are set regardless of unit setting:
                    hourly_forecast_states_list.append({'key': u"h{0}_cond".format(fore_counter_text), 'value': condition, 'uiValue': condition})
                    hourly_forecast_states_list.append({'key': u"h{0}_icon".format(fore_counter_text), 'value': icon, 'uiValue': icon})
                    hourly_forecast_states_list.append({'key': u"h{0}_proper_icon".format(fore_counter_text), 'value': icon, 'uiValue': icon})
                    hourly_forecast_states_list.append({'key': u"h{0}_time".format(fore_counter_text), 'value': civil_time, 'uiValue': civil_time})
                    hourly_forecast_states_list.append({'key': u"h{0}_windDirLong".format(fore_counter_text),
                                                        'value': self.verboseWindNames("h{0}_windDirLong".format(fore_counter_text), wind_dir)})
                    hourly_forecast_states_list.append({'key': u"h{0}_windDegrees".format(fore_counter_text), 'value': int(wind_degrees), 'uiValue': str(int(wind_degrees))})

                    time_long = u"{0}-{1}-{2} {3}:{4}".format(year, month, day, hour, minute)
                    hourly_forecast_states_list.append({'key': u"h{0}_timeLong".format(fore_counter_text), 'value': time_long, 'uiValue': time_long})

                    value, ui_value = self.fixCorruptedData(state_name="h{0}_humidity".format(fore_counter_text), val=fore_humidity)
                    ui_value = self.uiFormatPercentage(dev=dev, state_name="h{0}_humidity".format(fore_counter_text), val=ui_value)
                    hourly_forecast_states_list.append({'key': u"h{0}_humidity".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    value, ui_value = self.fixCorruptedData(state_name="h{0}_precip".format(fore_counter_text), val=fore_pop)
                    ui_value = self.uiFormatPercentage(dev=dev, state_name="h{0}_precip".format(fore_counter_text), val=ui_value)
                    hourly_forecast_states_list.append({'key': u"h{0}_precip".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    # Metric temperature (°C)
                    if config_menu_units in ("M", "MS", "I"):
                        value, ui_value = self.fixCorruptedData(state_name="h{0}_temp".format(fore_counter_text), val=fore_temp_metric)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="h{0}_temp".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_temp".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    # Standard temperature (°F):
                    if config_menu_units == "S":
                        value, ui_value = self.fixCorruptedData(state_name="h{0}_temp".format(fore_counter_text), val=fore_temp_standard)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="h{0}_temp".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_temp".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    # KPH Wind:
                    if config_menu_units == "M":

                        value, ui_value = self.fixCorruptedData(state_name="h{0}_windSpeed".format(fore_counter_text), val=wind_speed_metric)
                        ui_value = self.uiFormatWind(dev=dev, state_name="h{0}_windSpeed".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_windSpeed".format(fore_counter_text), 'value': value, 'uiValue': ui_value})
                        hourly_forecast_states_list.append({'key': u"h{0}_windSpeedIcon".format(fore_counter_text), 'value': "{0}".format(wind_speed_metric).replace('.', '')})

                    # MPS Wind:
                    if config_menu_units == "MS":

                        value, ui_value = self.fixCorruptedData(state_name="h{0}_windSpeed".format(fore_counter_text), val=wind_speed_mps)
                        ui_value = self.uiFormatWind(dev=dev, state_name="h{0}_windSpeed".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_windSpeed".format(fore_counter_text), 'value': value, 'uiValue': ui_value})
                        hourly_forecast_states_list.append({'key': u"h{0}_windSpeedIcon".format(fore_counter_text), 'value': u"{0}".format(wind_speed_mps).replace('.', '')})

                    # Metric QPF and Snow:
                    if config_menu_units in ("M", "MS"):
                        value, ui_value = self.fixCorruptedData(state_name="h{0}_qpf".format(fore_counter_text), val=fore_qpf_metric)
                        ui_value = self.uiFormatRain(dev=dev, state_name="h{0}_qpf".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_qpf".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="h{0}_snow".format(fore_counter_text), val=fore_snow_metric)
                        ui_value = self.uiFormatSnow(dev=dev, state_name="h{0}_snow".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_snow".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    # Standard QPF, Snow and Wind:
                    if config_menu_units in ("I", "S"):

                        value, ui_value = self.fixCorruptedData(state_name="h{0}_qpf".format(fore_counter_text), val=fore_qpf_standard)
                        ui_value = self.uiFormatRain(dev=dev, state_name="h{0}_qpf".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_qpf".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="h{0}_snow".format(fore_counter_text), val=fore_snow_standard)
                        ui_value = self.uiFormatSnow(dev=dev, state_name="h{0}_snow".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_snow".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        value, ui_value = self.fixCorruptedData(state_name="h{0}_windSpeed".format(fore_counter_text), val=wind_speed_standard)
                        ui_value = self.uiFormatWind(dev=dev, state_name="h{0}_windSpeed".format(fore_counter_text), val=ui_value)
                        hourly_forecast_states_list.append({'key': u"h{0}_windSpeed".format(fore_counter_text), 'value': value, 'uiValue': ui_value})
                        hourly_forecast_states_list.append({'key': u"h{0}_windSpeedIcon".format(fore_counter_text), 'value': u"{0}".format(wind_speed_standard).replace('.', '')})

                    if dev.pluginProps.get('configWindDirUnits', '') == "DIR":
                        hourly_forecast_states_list.append({'key': u"h{0}_windDir".format(fore_counter_text), 'value': wind_dir, 'uiValue': wind_dir})

                    else:
                        hourly_forecast_states_list.append({'key': u"h{0}_windDir".format(fore_counter_text), 'value': wind_degrees, 'uiValue': wind_degrees})

                    fore_counter += 1

            new_props = dev.pluginProps
            new_props['address'] = station_id
            dev.replacePluginPropsOnServer(new_props)
            hourly_forecast_states_list.append({'key': 'onOffState', 'value': True, 'uiValue': u" "})
            dev.updateStatesOnServer(hourly_forecast_states_list)
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing hourly forecast data.")
            hourly_forecast_states_list.append({'key': 'onOffState', 'value': False, 'uiValue': u" "})
            dev.updateStatesOnServer(hourly_forecast_states_list)
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def parseTenDayData(self, dev):
        """
        Parse ten day forecase data to devices

        The parseTenDayData() method takes 10 day forecast data and parses it to device
        states.

        -----

        :param indigo.Device dev:
        """

        ten_day_forecast_states_list = []
        config_menu_units           = dev.pluginProps.get('configMenuUnits', '')
        location                    = dev.pluginProps['location']
        wind_speed_units            = dev.pluginProps.get('configWindSpdUnits', '')

        weather_data = self.masterWeatherDict[location]
        forecast_day = self.masterWeatherDict[location].get('forecast', {}).get('simpleforecast', {}).get('forecastday', {})

        current_observation_epoch = self.nestedLookup(weather_data, keys=('current_observation', 'observation_epoch'))
        current_observation_time  = self.nestedLookup(weather_data, keys=('current_observation', 'observation_time'))
        station_id                = self.nestedLookup(weather_data, keys=('current_observation', 'station_id'))

        try:

            ten_day_forecast_states_list.append({'key': 'currentObservation', 'value': current_observation_time, 'uiValue': current_observation_time})
            ten_day_forecast_states_list.append({'key': 'currentObservationEpoch', 'value': current_observation_epoch, 'uiValue': current_observation_epoch})

            # Current Observation Time 24 Hour (string)
            current_observation_24hr = time.strftime("{0} {1}".format(self.date_format, self.time_format), time.localtime(float(current_observation_epoch)))
            ten_day_forecast_states_list.append({'key': 'currentObservation24hr', 'value': current_observation_24hr})

            fore_counter = 1

            for observation in forecast_day:

                conditions         = self.nestedLookup(observation, keys=('conditions',))
                forecast_date      = self.nestedLookup(observation, keys=('date', 'epoch'))
                fore_pop           = self.nestedLookup(observation, keys=('pop',))
                fore_qpf_metric    = self.nestedLookup(observation, keys=('qpf_allday', 'mm'))
                fore_qpf_standard  = self.nestedLookup(observation, keys=('qpf_allday', 'in'))
                fore_snow_metric   = self.nestedLookup(observation, keys=('snow_allday', 'cm'))
                fore_snow_standard = self.nestedLookup(observation, keys=('snow_allday', 'in'))
                high_temp_metric   = self.nestedLookup(observation, keys=('high', 'celsius'))
                high_temp_standard = self.nestedLookup(observation, keys=('high', 'fahrenheit'))
                icon               = self.nestedLookup(observation, keys=('icon',))
                low_temp_metric    = self.nestedLookup(observation, keys=('low', 'celsius'))
                low_temp_standard  = self.nestedLookup(observation, keys=('low', 'fahrenheit'))
                max_humidity       = self.nestedLookup(observation, keys=('maxhumidity',))
                weekday            = self.nestedLookup(observation, keys=('date', 'weekday'))
                wind_avg_degrees   = self.nestedLookup(observation, keys=('avewind', 'degrees'))
                wind_avg_dir       = self.nestedLookup(observation, keys=('avewind', 'dir'))
                wind_avg_metric    = self.nestedLookup(observation, keys=('avewind', 'kph'))
                wind_avg_standard  = self.nestedLookup(observation, keys=('avewind', 'mph'))
                wind_max_degrees   = self.nestedLookup(observation, keys=('maxwind', 'degrees'))
                wind_max_dir       = self.nestedLookup(observation, keys=('maxwind', 'dir'))
                wind_max_metric    = self.nestedLookup(observation, keys=('maxwind', 'kph'))
                wind_max_standard  = self.nestedLookup(observation, keys=('maxwind', 'mph'))

                if fore_counter <= 10:

                    # Add leading zero to counter value for device state names 1-9.
                    if fore_counter < 10:
                        fore_counter_text = "0{0}".format(fore_counter)
                    else:
                        fore_counter_text = fore_counter

                    ten_day_forecast_states_list.append({'key': u"d{0}_conditions".format(fore_counter_text), 'value': conditions, 'uiValue': conditions})
                    ten_day_forecast_states_list.append({'key': u"d{0}_day".format(fore_counter_text), 'value': weekday, 'uiValue': weekday})

                    # Forecast day
                    forecast_date = time.strftime('%Y-%m-%d', time.localtime(float(forecast_date)))
                    ten_day_forecast_states_list.append({'key': u"d{0}_date".format(fore_counter_text), 'value': forecast_date, 'uiValue': forecast_date})

                    # Pop
                    value, ui_value = self.fixCorruptedData(state_name="d{0}_pop".format(fore_counter_text), val=fore_pop)
                    ui_value = self.uiFormatPercentage(dev=dev, state_name="d{0}_pop".format(fore_counter_text), val=ui_value)
                    ten_day_forecast_states_list.append({'key': u"d{0}_pop".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    # Forecast humidity (all day).
                    value, ui_value = self.fixCorruptedData(state_name="d{0}_humidity".format(fore_counter_text), val=max_humidity)
                    ui_value = self.uiFormatPercentage(dev=dev, state_name="d{0}_humidity".format(fore_counter_text), val=ui_value)
                    ten_day_forecast_states_list.append({'key': u"d{0}_humidity".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    # Forecast icon (all day).
                    ten_day_forecast_states_list.append({'key': u"d{0}_icon".format(fore_counter_text), 'value': u"{0}".format(icon)})

                    # Wind. This can be impacted by whether the user wants average wind or max wind.
                    # Three states are affected by this setting: _windDegrees, _windDir, and _windDirLong.
                    if wind_speed_units == "AVG":
                        wind_degrees = wind_avg_degrees
                        wind_dir = wind_avg_dir
                    else:
                        wind_degrees = wind_max_degrees
                        wind_dir = wind_max_dir

                    value, ui_value = self.fixCorruptedData(state_name="d{0}_windDegrees".format(fore_counter_text), val=wind_degrees)
                    ten_day_forecast_states_list.append({'key': u"d{0}_windDegrees".format(fore_counter_text), 'value': int(value), 'uiValue': str(int(value))})

                    ten_day_forecast_states_list.append({'key': u"d{0}_windDir".format(fore_counter_text), 'value': wind_dir, 'uiValue': wind_dir})

                    wind_long_name = self.verboseWindNames(state_name="d{0}_windDirLong".format(fore_counter_text), val=wind_dir)
                    ten_day_forecast_states_list.append({'key': u"d{0}_windDirLong".format(fore_counter_text), 'value': wind_long_name, 'uiValue': wind_long_name})

                    if config_menu_units in ["I", "M", "MS"]:

                        # High Temperature (Metric)
                        value, ui_value = self.fixCorruptedData(state_name="d{0}_high".format(fore_counter_text), val=high_temp_metric)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="d{0}_high".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_high".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        # Low Temperature (Metric)
                        value, ui_value = self.fixCorruptedData(state_name="d{0}_low".format(fore_counter_text), val=low_temp_metric)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="d{0}_low".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_low".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    # User preference is Metric.
                    if config_menu_units in ["M", "MS"]:

                        # QPF Amount
                        value, ui_value = self.fixCorruptedData(state_name="d{0}_qpf".format(fore_counter_text), val=fore_qpf_metric)
                        ui_value = self.uiFormatRain(dev=dev, state_name="d{0}_qpf".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_qpf".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        # Snow Value
                        value, ui_value = self.fixCorruptedData(state_name="d{0}_snow".format(fore_counter_text), val=fore_snow_metric)
                        ui_value = self.uiFormatSnow(dev=dev, state_name="d{0}_snow".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_snow".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        # Wind speed
                        if wind_speed_units == "AVG":
                            wind_value = wind_avg_metric
                        else:
                            wind_value = wind_max_metric

                        if config_menu_units == 'MS':
                            wind_value *= 0.277778

                        value, ui_value = self.fixCorruptedData(state_name="d{0}_windSpeed".format(fore_counter_text), val=wind_value)
                        ui_value = self.uiFormatWind(dev=dev, state_name="d{0}_windSpeed".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_windSpeed".format(fore_counter_text), 'value': value, 'uiValue': ui_value})
                        ten_day_forecast_states_list.append({'key': u"d{0}_windSpeedIcon".format(fore_counter_text), 'value': unicode(wind_value).replace('.', '')})

                    # User preference is Mixed.
                    if config_menu_units in ["I", "S"]:

                        # QPF Amount
                        value, ui_value = self.fixCorruptedData(state_name="d{0}_qpf".format(fore_counter_text), val=fore_qpf_standard)
                        ui_value = self.uiFormatRain(dev=dev, state_name="d{0}_qpf".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_qpf".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        # Snow Value
                        value, ui_value = self.fixCorruptedData(state_name="d{0}_snow".format(fore_counter_text), val=fore_snow_standard)
                        ui_value = self.uiFormatSnow(dev=dev, state_name="d{0}_snow".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_snow".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        # Wind speed
                        if wind_speed_units == "AVG":
                            wind_value = wind_avg_standard
                        else:
                            wind_value = wind_max_standard

                        value, ui_value = self.fixCorruptedData(state_name="d{0}_windSpeed".format(fore_counter_text), val=wind_value)
                        ui_value = self.uiFormatWind(dev=dev, state_name="d{0}_windSpeed".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_windSpeed".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        ten_day_forecast_states_list.append({'key': u"d{0}_windSpeedIcon".format(fore_counter_text), 'value': unicode(wind_value).replace('.', '')})

                    # User preference is Standard.
                    if config_menu_units == "S":

                        # High Temperature (Standard)
                        value, ui_value = self.fixCorruptedData(state_name="d{0}_high".format(fore_counter_text), val=high_temp_standard)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="d{0}_high".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_high".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                        # Low Temperature Standard
                        value, ui_value = self.fixCorruptedData(state_name="d{0}_low".format(fore_counter_text), val=low_temp_standard)
                        ui_value = self.uiFormatTemperature(dev=dev, state_name="d{0}_low".format(fore_counter_text), val=ui_value)
                        ten_day_forecast_states_list.append({'key': u"d{0}_low".format(fore_counter_text), 'value': value, 'uiValue': ui_value})

                    fore_counter += 1

            new_props = dev.pluginProps
            new_props['address'] = station_id
            dev.replacePluginPropsOnServer(new_props)
            ten_day_forecast_states_list.append({'key': 'onOffState', 'value': True, 'uiValue': u" "})
            dev.updateStatesOnServer(ten_day_forecast_states_list)
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing 10-day forecast data.")
            ten_day_forecast_states_list.append({'key': 'onOffState', 'value': False, 'uiValue': u" "})
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
            dev.updateStatesOnServer(ten_day_forecast_states_list)

    def parseTidesData(self, dev):
        """
        Parse tides data to devices

        The parseTidesData() method takes tide data and parses it to device states.

        -----

        :param indigo.Device dev:
        """

        tide_states_list = []
        location         = dev.pluginProps['location']

        weather_data = self.masterWeatherDict[location]

        current_observation_epoch = self.nestedLookup(weather_data, keys=('current_observation', 'observation_epoch'))
        current_observation_time  = self.nestedLookup(weather_data, keys=('current_observation', 'observation_time'))
        station_id                = self.nestedLookup(weather_data, keys=('current_observation', 'station_id'))
        tide_min_height           = self.nestedLookup(weather_data, keys=('tide', 'tideSummaryStats', 'minheight'))
        tide_max_height           = self.nestedLookup(weather_data, keys=('tide', 'tideSummaryStats', 'maxheight'))
        tide_site                 = self.nestedLookup(weather_data, keys=('tide', 'tideInfo', 'tideSite'))
        tide_summary              = self.nestedLookup(weather_data, keys=('tide', 'tideSummary'))

        try:

            tide_states_list.append({'key': 'currentObservation', 'value': current_observation_time, 'uiValue': current_observation_time})
            tide_states_list.append({'key': 'currentObservationEpoch', 'value': current_observation_epoch, 'uiValue': current_observation_epoch})

            # Current Observation Time 24 Hour (string)
            current_observation_24hr = time.strftime("{0} {1}".format(self.date_format, self.time_format), time.localtime(float(current_observation_epoch)))
            tide_states_list.append({'key': 'currentObservation24hr', 'value': current_observation_24hr})

            # Tide location information. This is only appropriate for some locations.
            if tide_site in [u"", u" "]:
                tide_states_list.append({'key': 'tideSite', 'value': u"No tide info."})

            else:
                tide_states_list.append({'key': 'tideSite', 'value': tide_site, 'uiValue': tide_site})

            # Minimum and maximum tide levels.
            if tide_min_height == 99:
                tide_states_list.append({'key': 'minHeight', 'value': tide_min_height, 'uiValue': u"--"})

            else:
                tide_states_list.append({'key': 'minHeight', 'value': tide_min_height, 'uiValue': tide_min_height})

            if tide_max_height == -99:
                tide_states_list.append({'key': 'maxHeight', 'value': tide_max_height, 'uiValue': u"--"})

            else:
                tide_states_list.append({'key': 'maxHeight', 'value': tide_max_height})

            # Observations
            tide_counter = 1
            if len(tide_summary):

                for observation in tide_summary:

                    if tide_counter < 32:

                        pretty      = self.nestedLookup(observation, keys=('date', 'pretty'))
                        tide_height = self.nestedLookup(observation, keys=('data', 'height'))
                        tide_type   = self.nestedLookup(observation, keys=('data', 'type'))

                        tide_states_list.append({'key': u"p{0}_height".format(tide_counter), 'value': tide_height, 'uiValue': tide_height})
                        tide_states_list.append({'key': u"p{0}_pretty".format(tide_counter), 'value': pretty, 'uiValue': pretty})
                        tide_states_list.append({'key': u"p{0}_type".format(tide_counter), 'value': tide_type, 'uiValue': tide_type})

                        tide_counter += 1

            new_props = dev.pluginProps
            new_props['address'] = station_id
            dev.replacePluginPropsOnServer(new_props)

            tide_states_list.append({'key': 'onOffState', 'value': True, 'uiValue': u" "})
            dev.updateStatesOnServer(tide_states_list)
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn)

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing tide data.")
            self.logger.error(u"Note: Tide information may not be available in your area. Check Weather Underground for more information.")

            tide_states_list.append({'key': 'onOffState', 'value': False, 'uiValue': u" "})
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)
            dev.updateStatesOnServer(tide_states_list)

    def parseWeatherData(self, dev):
        """
        Parse weather data to devices

        The parseWeatherData() method takes weather data and parses it to Weather
        Device states.

        -----

        :param indigo.Device dev:
        """

        # Reload the date and time preferences in case they've changed.

        self.date_format = self.Formatter.dateFormat()
        self.time_format = self.Formatter.timeFormat()

        weather_states_list = []

        try:

            config_itemlist_ui_units = dev.pluginProps.get('itemListUiUnits', '')
            config_menu_units        = dev.pluginProps.get('configMenuUnits', '')
            config_distance_units    = dev.pluginProps.get('distanceUnits', '')
            location                 = dev.pluginProps['location']
            pressure_units           = dev.pluginProps.get('pressureUnits', '')

            weather_data = self.masterWeatherDict[location]
            history_data = self.nestedLookup(weather_data, keys=('history', 'dailysummary'))

            current_observation_epoch = self.nestedLookup(weather_data, keys=('current_observation', 'observation_epoch'))
            current_observation_time  = self.nestedLookup(weather_data, keys=('current_observation', 'observation_time'))
            current_temp_c            = self.nestedLookup(weather_data, keys=('current_observation', 'temp_c',))
            current_temp_f            = self.nestedLookup(weather_data, keys=('current_observation', 'temp_f',))
            current_weather           = self.nestedLookup(weather_data, keys=('current_observation', 'weather',))
            dew_point_c               = self.nestedLookup(weather_data, keys=('current_observation', 'dewpoint_c',))
            dew_point_f               = self.nestedLookup(weather_data, keys=('current_observation', 'dewpoint_f',))
            feels_like_c              = self.nestedLookup(weather_data, keys=('current_observation', 'feelslike_c',))
            feels_like_f              = self.nestedLookup(weather_data, keys=('current_observation', 'feelslike_f',))
            heat_index_c              = self.nestedLookup(weather_data, keys=('current_observation', 'heat_index_c',))
            heat_index_f              = self.nestedLookup(weather_data, keys=('current_observation', 'heat_index_f',))
            icon                      = self.nestedLookup(weather_data, keys=('current_observation', 'icon',))
            location_city             = self.nestedLookup(weather_data, keys=('location', 'city',))
            nearby_stations           = self.nestedLookup(weather_data, keys=('location', 'nearby_weather_stations', 'pws', 'station'))
            precip_1hr_m              = self.nestedLookup(weather_data, keys=('current_observation', 'precip_1hr_metric',))
            precip_1hr_in             = self.nestedLookup(weather_data, keys=('current_observation', 'precip_1hr_in',))
            precip_today_m            = self.nestedLookup(weather_data, keys=('current_observation', 'precip_today_metric',))
            precip_today_in           = self.nestedLookup(weather_data, keys=('current_observation', 'precip_today_in',))
            pressure_mb               = self.nestedLookup(weather_data, keys=('current_observation', 'pressure_mb',))
            pressure_in               = self.nestedLookup(weather_data, keys=('current_observation', 'pressure_in',))
            pressure_trend            = self.nestedLookup(weather_data, keys=('current_observation', 'pressure_trend',))
            relative_humidity         = self.nestedLookup(weather_data, keys=('current_observation', 'relative_humidity',))
            solar_radiation           = self.nestedLookup(weather_data, keys=('current_observation', 'solarradiation',))
            station_id                = self.nestedLookup(weather_data, keys=('current_observation', 'station_id',))
            uv_index                  = self.nestedLookup(weather_data, keys=('current_observation', 'UV',))
            visibility_km             = self.nestedLookup(weather_data, keys=('current_observation', 'visibility_km',))
            visibility_mi             = self.nestedLookup(weather_data, keys=('current_observation', 'visibility_mi',))
            wind_chill_c              = self.nestedLookup(weather_data, keys=('current_observation', 'windchill_c',))
            wind_chill_f              = self.nestedLookup(weather_data, keys=('current_observation', 'windchill_f',))
            wind_degrees              = self.nestedLookup(weather_data, keys=('current_observation', 'wind_degrees',))
            wind_dir                  = self.nestedLookup(weather_data, keys=('current_observation', 'wind_dir',))
            wind_gust_kph             = self.nestedLookup(weather_data, keys=('current_observation', 'wind_gust_kph',))
            wind_gust_mph             = self.nestedLookup(weather_data, keys=('current_observation', 'wind_gust_mph',))
            wind_speed_kph            = self.nestedLookup(weather_data, keys=('current_observation', 'wind_kph',))
            wind_speed_mph            = self.nestedLookup(weather_data, keys=('current_observation', 'wind_mph',))

            temp_c, temp_c_ui = self.fixCorruptedData(state_name="temp_c", val=current_temp_c)
            temp_c_ui = self.uiFormatTemperature(dev=dev, state_name="tempC (M, MS, I)", val=temp_c_ui)

            temp_f, temp_f_ui = self.fixCorruptedData(state_name="temp_f", val=current_temp_f)
            temp_f_ui = self.uiFormatTemperature(dev=dev, state_name="tempF (S)", val=temp_f_ui)

            # We want these written to the server right away so we use the legacy method.
            if config_menu_units in ['M', 'MS', 'I']:
                dev.updateStateOnServer('temp', value=temp_c, uiValue=temp_c_ui)
                icon_value = u"{0}".format(str(round(temp_c, 0)).replace('.', ''))
                dev.updateStateOnServer('tempIcon', value=icon_value)

            else:
                dev.updateStateOnServer('temp', value=temp_f, uiValue=temp_f_ui)
                icon_value = u"{0}".format(str(round(temp_f, 0)).replace('.', ''))
                dev.updateStateOnServer('tempIcon', value=icon_value)

            # Set the display of temperature in the Indigo Item List display, and set the value of onOffState to true since we were able to get the data.
            # This only affects what is displayed in the Indigo UI.
            if config_itemlist_ui_units == "M":  # Displays °C
                display_value = u"{0} \N{DEGREE SIGN}C".format(self.uiFormatItemListTemperature(val=temp_c))

            elif config_itemlist_ui_units == "S":  # Displays °F
                display_value = u"{0} \N{DEGREE SIGN}F".format(self.uiFormatItemListTemperature(val=temp_f))

            elif config_itemlist_ui_units == "SM":  # Displays °F (°C)
                display_value = u"{0} \N{DEGREE SIGN}F ({1} \N{DEGREE SIGN}C)".format(self.uiFormatItemListTemperature(val=temp_f), self.uiFormatItemListTemperature(val=temp_c))

            elif config_itemlist_ui_units == "MS":  # Displays °C (°F)
                display_value = u"{0} \N{DEGREE SIGN}C ({1} \N{DEGREE SIGN}F)".format(self.uiFormatItemListTemperature(val=temp_c), self.uiFormatItemListTemperature(val=temp_f))

            elif config_itemlist_ui_units == "MN":  # Displays C no units
                display_value = self.uiFormatItemListTemperature(temp_c)

            else:  # Displays F no units
                display_value = self.uiFormatItemListTemperature(temp_f)

            dev.updateStateOnServer('onOffState', value=True, uiValue=display_value)

            weather_states_list.append({'key': 'locationCity', 'value': location_city, 'uiValue': location_city})
            weather_states_list.append({'key': 'stationID', 'value': station_id, 'uiValue': station_id})

            # Neighborhood for this weather location (string: "Neighborhood Name")
            neighborhood = u"Location not found."
            for key in nearby_stations:
                # if key['id'] == unicode(station_id):
                if key['id'] == station_id:
                    neighborhood = key['neighborhood']
                    break

            weather_states_list.append({'key': 'neighborhood', 'value': neighborhood, 'uiValue': neighborhood})

            # Functional icon name:
            # Weather Underground's icon value does not account for day and night icon
            # names (although the iconURL value does). This segment produces a functional
            # icon name to allow for the proper display of daytime and nighttime condition
            # icons. It also provides a separate value for icon names that do not change
            # for day/night. Note that this segment of code is dependent on the Indigo
            # read-only variable 'isDayLight'.

            # Icon Name (string: "clear", "cloudy"...) Moving to the v11 version of the plugin may make the icon name adjustments unnecessary.
            weather_states_list.append({'key': 'properIconNameAllDay', 'value': icon, 'uiValue': icon})
            weather_states_list.append({'key': 'properIconName', 'value': icon, 'uiValue': icon})

            # Current Observation Time (string: "Last Updated on MONTH DD, HH:MM AM/PM TZ")
            weather_states_list.append({'key': 'currentObservation', 'value': current_observation_time, 'uiValue': current_observation_time})

            # Current Observation Time 24 Hour (string)
            current_observation_24hr = time.strftime("{0} {1}".format(self.date_format, self.time_format), time.localtime(float(current_observation_epoch)))
            weather_states_list.append({'key': 'currentObservation24hr', 'value': current_observation_24hr})

            # Current Observation Time Epoch (string)
            weather_states_list.append({'key': 'currentObservationEpoch', 'value': current_observation_epoch, 'uiValue': current_observation_epoch})

            # Current Weather (string: "Clear", "Cloudy"...)
            weather_states_list.append({'key': 'currentWeather', 'value': current_weather, 'uiValue': current_weather})

            # Barometric pressure trend (string: "+", "0", "-")
            pressure_trend = self.uiFormatPressureSymbol(state_name="Pressure Trend", val=pressure_trend)
            weather_states_list.append({'key': 'pressureTrend', 'value': pressure_trend, 'uiValue': pressure_trend})

            # Solar Radiation (string: "0" or greater. Not always provided as a value that can float (sometimes = "").
            # Some sites don't report it.)
            s_rad, s_rad_ui = self.fixCorruptedData(state_name="Solar Radiation", val=solar_radiation)
            weather_states_list.append({'key': 'solarradiation', 'value': s_rad, 'uiValue': s_rad_ui})

            # Ultraviolet light (string: 0 or greater. Not always provided as a value that can float (sometimes = "").
            # Some sites don't report it.)
            uv, uv_ui = self.fixCorruptedData(state_name="Solar Radiation", val=uv_index)
            weather_states_list.append({'key': 'uv', 'value': uv, 'uiValue': uv_ui})

            # Short Wind direction in alpha (string: N, NNE, NE, ENE...)
            weather_states_list.append({'key': 'windDIR', 'value': wind_dir, 'uiValue': wind_dir})

            # Long Wind direction in alpha (string: North, North Northeast, Northeast, East Northeast...)
            wind_dir_long = self.verboseWindNames(state_name="windDIRlong", val=wind_dir)
            weather_states_list.append({'key': 'windDIRlong', 'value': wind_dir_long, 'uiValue': wind_dir_long})

            # Wind direction (integer: 0 - 359 -- units: degrees)
            wind_degrees, wind_degrees_ui = self.fixCorruptedData(state_name="windDegrees", val=wind_degrees)
            weather_states_list.append({'key': 'windDegrees', 'value': int(wind_degrees), 'uiValue': str(int(wind_degrees))})

            # Relative Humidity (string: "80%")
            relative_humidity, relative_humidity_ui = self.fixCorruptedData(state_name="relativeHumidity", val=str(relative_humidity).strip('%'))
            relative_humidity_ui = self.uiFormatPercentage(dev=dev, state_name="relativeHumidity", val=relative_humidity_ui)
            weather_states_list.append({'key': 'relativeHumidity', 'value': relative_humidity, 'uiValue': relative_humidity_ui})

            # Wind Gust (string: "19.3" -- units: kph)
            wind_gust_kph, wind_gust_kph_ui = self.fixCorruptedData(state_name="windGust (KPH)", val=wind_gust_kph)
            wind_gust_mph, wind_gust_mph_ui = self.fixCorruptedData(state_name="windGust (MPH)", val=wind_gust_mph)
            wind_gust_mps, wind_gust_mps_ui = self.fixCorruptedData(state_name="windGust (MPS)", val=int(wind_gust_kph * 0.277778))

            # Wind Gust (string: "19.3" -- units: kph)
            wind_speed_kph, wind_speed_kph_ui = self.fixCorruptedData(state_name="windGust (KPH)", val=wind_speed_kph)
            wind_speed_mph, wind_speed_mph_ui = self.fixCorruptedData(state_name="windGust (MPH)", val=wind_speed_mph)
            wind_speed_mps, wind_speed_mps_ui = self.fixCorruptedData(state_name="windGust (MPS)", val=int(wind_speed_kph * 0.277778))

            # History (yesterday's weather).  This code needs its own try/except block because not all possible
            # weather locations support history.
            try:

                history_max_temp_m  = self.nestedLookup(history_data, keys=('maxtempm',))
                history_max_temp_i  = self.nestedLookup(history_data, keys=('maxtempi',))
                history_min_temp_m  = self.nestedLookup(history_data, keys=('mintempm',))
                history_min_temp_i  = self.nestedLookup(history_data, keys=('mintempi',))
                history_precip_m    = self.nestedLookup(history_data, keys=('precipm',))
                history_precip_i    = self.nestedLookup(history_data, keys=('precipi',))
                history_pretty_date = self.nestedLookup(history_data, keys=('date', 'pretty'))

                weather_states_list.append({'key': 'historyDate', 'value': history_pretty_date})

                if config_menu_units in ['M', 'MS', 'I']:

                    history_high, history_high_ui = self.fixCorruptedData(state_name="historyHigh (M)", val=history_max_temp_m)
                    history_high_ui = self.uiFormatTemperature(dev=dev, state_name="historyHigh (M)", val=history_high_ui)
                    weather_states_list.append({'key': 'historyHigh', 'value': history_high, 'uiValue': history_high_ui})

                    history_low, history_low_ui = self.fixCorruptedData(state_name="historyLow (M)", val=history_min_temp_m)
                    history_low_ui = self.uiFormatTemperature(dev=dev, state_name="historyLow (M)", val=history_low_ui)
                    weather_states_list.append({'key': 'historyLow', 'value': history_low, 'uiValue': history_low_ui})

                if config_menu_units in ['M', 'MS']:

                    history_pop, history_pop_ui = self.fixCorruptedData(state_name="historyPop (M)", val=history_precip_m)
                    history_pop_ui = self.uiFormatRain(dev=dev, state_name="historyPop (M)", val=history_pop_ui)
                    weather_states_list.append({'key': 'historyPop', 'value': history_pop, 'uiValue': history_pop_ui})

                if config_menu_units in ['I', 'S']:

                    history_pop, history_pop_ui = self.fixCorruptedData(state_name="historyPop (I)", val=history_precip_i)
                    history_pop_ui = self.uiFormatRain(dev=dev, state_name="historyPop (I)", val=history_pop_ui)
                    weather_states_list.append({'key': 'historyPop', 'value': history_pop, 'uiValue': history_pop_ui})

                if config_menu_units in ['S']:
                    history_high, history_high_ui = self.fixCorruptedData(state_name="historyHigh (S)", val=history_max_temp_i)
                    history_high_ui = self.uiFormatTemperature(dev=dev, state_name="historyHigh (S)", val=history_high_ui)
                    weather_states_list.append({'key': 'historyHigh', 'value': history_high, 'uiValue': history_high_ui})

                    history_low, history_low_ui = self.fixCorruptedData(state_name="historyLow (S)", val=history_min_temp_i)
                    history_low_ui = self.uiFormatTemperature(dev=dev, state_name="historyLow (S)", val=history_low_ui)
                    weather_states_list.append({'key': 'historyLow', 'value': history_low, 'uiValue': history_low_ui})

            except IndexError:
                self.Fogbert.pluginErrorHandler(traceback.format_exc())
                self.logger.info(u"History data not supported for {0}".format(dev.name))

            # Metric (M), Mixed SI (MS), Mixed (I):
            if config_menu_units in ['M', 'MS', 'I']:

                # Dew Point (integer: -20 -- units: Centigrade)
                dewpoint, dewpoint_ui = self.fixCorruptedData(state_name="dewpointC (M, MS)", val=dew_point_c)
                dewpoint_ui = self.uiFormatTemperature(dev=dev, state_name="dewpointC (M, MS)", val=dewpoint_ui)
                weather_states_list.append({'key': 'dewpoint', 'value': dewpoint, 'uiValue': dewpoint_ui})

                # Feels Like (string: "-20" -- units: Centigrade)
                feelslike, feelslike_ui = self.fixCorruptedData(state_name="feelsLikeC (M, MS)", val=feels_like_c)
                feelslike_ui = self.uiFormatTemperature(dev=dev, state_name="feelsLikeC (M, MS)", val=feelslike_ui)
                weather_states_list.append({'key': 'feelslike', 'value': feelslike, 'uiValue': feelslike_ui})

                # Heat Index (string: "20", "NA" -- units: Centigrade)
                heat_index, heat_index_ui = self.fixCorruptedData(state_name="heatIndexC (M, MS)", val=heat_index_c)
                heat_index_ui = self.uiFormatTemperature(dev=dev, state_name="heatIndexC (M, MS)", val=heat_index_ui)
                weather_states_list.append({'key': 'heatIndex', 'value': heat_index, 'uiValue': heat_index_ui})

                # Wind Chill (string: "17" -- units: Centigrade)
                windchill, windchill_ui = self.fixCorruptedData(state_name="windChillC (M, MS)", val=wind_chill_c)
                windchill_ui = self.uiFormatTemperature(dev=dev, state_name="windChillC (M, MS)", val=windchill_ui)
                weather_states_list.append({'key': 'windchill', 'value': windchill, 'uiValue': windchill_ui})

                # Visibility (string: "16.1" -- units: km)
                visibility, visibility_ui = self.fixCorruptedData(state_name="visibility (M, MS)", val=visibility_km)
                weather_states_list.append({'key': 'visibility', 'value': visibility, 'uiValue': u"{0}{1}".format(int(round(visibility)), config_distance_units)})

                # Barometric Pressure (string: "1039" -- units: mb)
                pressure, pressure_ui = self.fixCorruptedData(state_name="pressureMB (M, MS)", val=pressure_mb)
                weather_states_list.append({'key': 'pressure', 'value': pressure, 'uiValue': u"{0}{1}".format(pressure_ui, pressure_units)})
                weather_states_list.append({'key': 'pressureIcon', 'value': u"{0}".format(int(round(pressure, 0)))})

            # Metric (M), Mixed SI (MS):
            if config_menu_units in ['M', 'MS']:

                # Precipitation Today (string: "0", "2" -- units: mm)
                precip_today, precip_today_ui = self.fixCorruptedData(state_name="precipMM (M, MS)", val=precip_today_m)
                precip_today_ui = self.uiFormatRain(dev=dev, state_name="precipToday (M, MS)", val=precip_today_ui)
                weather_states_list.append({'key': 'precip_today', 'value': precip_today, 'uiValue': precip_today_ui})

                # Precipitation Last Hour (string: "0", "2" -- units: mm)
                precip_1hr, precip_1hr_ui = self.fixCorruptedData(state_name="precipOneHourMM (M, MS)", val=precip_1hr_m)
                precip_1hr_ui = self.uiFormatRain(dev=dev, state_name="precipOneHour (M, MS)", val=precip_1hr_ui)
                weather_states_list.append({'key': 'precip_1hr', 'value': precip_1hr, 'uiValue': precip_1hr_ui})

                # Report winds in KPH or MPS depending on user prefs. 1 KPH = 0.277778 MPS

                if config_menu_units == 'M':

                    weather_states_list.append({'key': 'windGust', 'value': wind_gust_kph, 'uiValue': self.uiFormatWind(dev=dev, state_name="windGust", val=wind_gust_kph_ui)})
                    weather_states_list.append({'key': 'windSpeed', 'value': wind_speed_kph, 'uiValue': self.uiFormatWind(dev=dev, state_name="windSpeed", val=wind_speed_kph_ui)})
                    weather_states_list.append({'key': 'windGustIcon', 'value': unicode(round(wind_gust_kph, 1)).replace('.', '')})
                    weather_states_list.append({'key': 'windSpeedIcon', 'value': unicode(round(wind_speed_kph, 1)).replace('.', '')})
                    weather_states_list.append({'key': 'windString', 'value': u"From the {0} at {1} KPH Gusting to {2} KPH".format(wind_dir, wind_speed_kph, wind_gust_kph)})
                    weather_states_list.append({'key': 'windShortString', 'value': u"{0} at {1}".format(wind_dir, wind_speed_kph)})
                    weather_states_list.append({'key': 'windStringMetric', 'value': u"From the {0} at {1} KPH Gusting to {2} KPH".format(wind_dir, wind_speed_kph, wind_gust_kph)})

                if config_menu_units == 'MS':

                    weather_states_list.append({'key': 'windGust', 'value': wind_gust_mps, 'uiValue': self.uiFormatWind(dev=dev, state_name="windGust", val=wind_gust_mps_ui)})
                    weather_states_list.append({'key': 'windSpeed', 'value': wind_speed_mps, 'uiValue': self.uiFormatWind(dev=dev, state_name="windSpeed", val=wind_speed_mps_ui)})
                    weather_states_list.append({'key': 'windGustIcon', 'value': unicode(round(wind_gust_mps, 1)).replace('.', '')})
                    weather_states_list.append({'key': 'windSpeedIcon', 'value': unicode(round(wind_speed_mps, 1)).replace('.', '')})
                    weather_states_list.append({'key': 'windString', 'value': u"From the {0} at {1} MPS Gusting to {2} MPS".format(wind_dir, wind_speed_mps, wind_gust_mps)})
                    weather_states_list.append({'key': 'windShortString', 'value': u"{0} at {1}".format(wind_dir, wind_speed_mps)})
                    weather_states_list.append({'key': 'windStringMetric', 'value': u"From the {0} at {1} KPH Gusting to {2} KPH".format(wind_dir, wind_speed_mps, wind_gust_mps)})

            # Mixed (I), Standard (S):
            if config_menu_units in ['I', 'S']:

                # Precipitation Today (string: "0", "0.5" -- units: inches)
                precip_today, precip_today_ui = self.fixCorruptedData(state_name="precipToday (I)", val=precip_today_in)
                precip_today_ui = self.uiFormatRain(dev=dev, state_name="precipToday (I)", val=precip_today_ui)
                weather_states_list.append({'key': 'precip_today', 'value': precip_today, 'uiValue': precip_today_ui})

                # Precipitation Last Hour (string: "0", "0.5" -- units: inches)
                precip_1hr, precip_1hr_ui = self.fixCorruptedData(state_name="precipOneHour (I)", val=precip_1hr_in)
                precip_1hr_ui = self.uiFormatRain(dev=dev, state_name="precipOneHour (I)", val=precip_1hr_ui)
                weather_states_list.append({'key': 'precip_1hr', 'value': precip_1hr, 'uiValue': precip_1hr_ui})

                weather_states_list.append({'key': 'windGust', 'value': wind_gust_mph, 'uiValue': self.uiFormatWind(dev=dev, state_name="windGust", val=wind_gust_mph_ui)})
                weather_states_list.append({'key': 'windSpeed', 'value': wind_speed_mph, 'uiValue': self.uiFormatWind(dev=dev, state_name="windSpeed", val=wind_speed_mph_ui)})
                weather_states_list.append({'key': 'windGustIcon', 'value': unicode(round(wind_gust_mph, 1)).replace('.', '')})
                weather_states_list.append({'key': 'windSpeedIcon', 'value': unicode(round(wind_speed_mph, 1)).replace('.', '')})
                weather_states_list.append({'key': 'windString', 'value': u"From the {0} at {1} MPH Gusting to {2} MPH".format(wind_dir, wind_speed_mph, wind_gust_mph)})
                weather_states_list.append({'key': 'windShortString', 'value': u"{0} at {1}".format(wind_dir, wind_speed_mph)})
                weather_states_list.append({'key': 'windStringMetric', 'value': " "})

            # Standard (S):
            if config_menu_units in ['S']:
                # Dew Point (integer: -20 -- units: Fahrenheit)
                dewpoint, dewpoint_ui = self.fixCorruptedData(state_name="dewpointF (S)", val=dew_point_f)
                dewpoint_ui = self.uiFormatTemperature(dev=dev, state_name="dewpointF (S)", val=dewpoint_ui)
                weather_states_list.append({'key': 'dewpoint', 'value': dewpoint, 'uiValue': dewpoint_ui})

                # Feels Like (string: "-20" -- units: Fahrenheit)
                feelslike, feelslike_ui = self.fixCorruptedData(state_name="feelsLikeF (S)", val=feels_like_f)
                feelslike_ui = self.uiFormatTemperature(dev=dev, state_name="feelsLikeF (S)", val=feelslike_ui)
                weather_states_list.append({'key': 'feelslike', 'value': feelslike, 'uiValue': feelslike_ui})

                # Heat Index (string: "20", "NA" -- units: Fahrenheit)
                heat_index, heat_index_ui = self.fixCorruptedData(state_name="heatIndexF (S)", val=heat_index_f)
                heat_index_ui = self.uiFormatTemperature(dev=dev, state_name="heatIndexF (S)", val=heat_index_ui)
                weather_states_list.append({'key': 'heatIndex', 'value': heat_index, 'uiValue': heat_index_ui})

                # Wind Chill (string: "17" -- units: Fahrenheit)
                windchill, windchill_ui = self.fixCorruptedData(state_name="windChillF (S)", val=wind_chill_f)
                windchill_ui = self.uiFormatTemperature(dev=dev, state_name="windChillF (S)", val=windchill_ui)
                weather_states_list.append({'key': 'windchill', 'value': windchill, 'uiValue': windchill_ui})

                # Barometric Pressure (string: "30.25" -- units: inches of mercury)
                pressure, pressure_ui = self.fixCorruptedData(state_name="pressure (S)", val=pressure_in)
                weather_states_list.append({'key': 'pressure', 'value': pressure, 'uiValue': u"{0}{1}".format(pressure_ui, pressure_units)})
                weather_states_list.append({'key': 'pressureIcon', 'value': pressure_ui.replace('.', '')})

                # Visibility (string: "16.1" -- units: miles)
                visibility, visibility_ui = self.fixCorruptedData(state_name="visibility (S)", val=visibility_mi)
                weather_states_list.append({'key': 'visibility', 'value': visibility, 'uiValue': u"{0}{1}".format(int(round(visibility)), config_distance_units)})

            new_props = dev.pluginProps
            new_props['address'] = station_id
            dev.replacePluginPropsOnServer(new_props)

            dev.updateStatesOnServer(weather_states_list)
            dev.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensorOn)

        except IndexError:
            self.logger.warning(u"Note: List index out of range. This is likely normal.")

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing weather device data.")
            dev.updateStateOnServer('onOffState', value=False, uiValue=u" ")
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

    def refreshWeatherData(self):
        """
        Refresh data for plugin devices

        This method refreshes weather data for all devices based on a WUnderground
        general cycle, Action Item or Plugin Menu call.

        -----
        """

        api_key = self.pluginPrefs['apiKey']
        daily_call_limit_reached = self.pluginPrefs.get('dailyCallLimitReached', False)
        self.download_interval   = dt.timedelta(seconds=int(self.pluginPrefs.get('downloadInterval', '900')))
        self.wuOnline = True

        # Check to see if the daily call limit has been reached.
        try:

            if daily_call_limit_reached:
                self.callDay()

            else:
                self.callDay()

                self.masterWeatherDict = {}

                for dev in indigo.devices.itervalues("self"):

                    if not self.wuOnline:
                        break

                    if not dev:
                        # There are no WUnderground devices, so go to sleep.
                        self.logger.info(u"There aren't any devices to poll yet. Sleeping.")

                    elif not dev.configured:
                        # A device has been created, but hasn't been fully configured yet.
                        self.logger.info(u"A device has been created, but is not fully configured. Sleeping for a minute while you finish.")

                    if api_key in ["", "API Key"]:
                        self.logger.error(u"The plugin requires an API Key. See help for details.")
                        dev.updateStateOnServer('onOffState', value=False, uiValue=u"{0}".format("No key."))
                        dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

                    elif not dev.enabled:
                        self.logger.debug(u"{0}: device communication is disabled. Skipping.".format(dev.name))
                        dev.updateStateOnServer('onOffState', value=False, uiValue=u"{0}".format("Disabled"))
                        dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

                    elif dev.enabled:
                        self.logger.debug(u"Processing device: {0}".format(dev.name))

                        dev.updateStateOnServer('onOffState', value=True, uiValue=u" ")

                        if dev.pluginProps['isWeatherDevice']:

                            location = dev.pluginProps['location']

                            self.getWeatherData(dev)

                            # If we've successfully downloaded data from Weather Underground, let's unpack it and
                            # assign it to the relevant device.
                            try:
                                # If a site location query returns a site unknown (in other words 'querynotfound'
                                # result, notify the user). Note that if the query is good, the error key won't exist
                                # in the dict.
                                response = self.masterWeatherDict[location]['response']['error']['type']
                                if response == 'querynotfound':
                                    self.logger.error(u"Location query for {0} not found. Please ensure that device location follows examples precisely.".format(dev.name))
                                    dev.updateStateOnServer('onOffState', value=False, uiValue=u"Bad Loc")
                                    dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOff)

                            except (KeyError, Exception) as error:
                                # Weather device types. There are multiples of these because the names of the device
                                # models evolved over time.
                                # If the error key is not present, that's good. Continue.
                                error = u"{0}".format(error)
                                if error == "'error'":
                                    pass
                                else:
                                    self.Fogbert.pluginErrorHandler(traceback.format_exc())

                                # Estimated Weather Data (integer: 1 if estimated weather), not present if false.
                                ignore_estimated = False
                                try:
                                    estimated = self.masterWeatherDict[location]['current_observation']['estimated']['estimated']
                                    if estimated == 1:
                                        self.logger.error(u"These are estimated conditions. There may be other functioning weather stations nearby. ({0})".format(dev.name))
                                        dev.updateStateOnServer('estimated', value="true", uiValue=u"True")

                                    # If the user wants to skip updates when weather data are estimated.
                                    if self.pluginPrefs.get('ignoreEstimated', False):
                                        ignore_estimated = True

                                except KeyError as error:
                                    error = u"{0}".format(error)
                                    if error == "'estimated'":
                                        # The estimated key must not be present. Therefore, we assumed the conditions
                                        # are not estimated.
                                        dev.updateStateOnServer('estimated', value="false", uiValue=u"False")
                                        ignore_estimated = False
                                    else:
                                        self.Fogbert.pluginErrorHandler(traceback.format_exc())

                                except Exception:
                                    self.Fogbert.pluginErrorHandler(traceback.format_exc())
                                    ignore_estimated = False

                                # Compare last data epoch to the one we just downloaded. Proceed if the data are newer.
                                # Note: WUnderground have been known to send data that are 5-6 months old. This flag
                                # helps ensure that known data are retained if the new data is not actually newer that
                                # what we already have.
                                try:
                                    # New devices may not have an epoch value yet.
                                    device_epoch = dev.states['currentObservationEpoch']
                                    try:
                                        device_epoch = int(device_epoch)
                                    except ValueError:
                                        device_epoch = 0

                                    # If we don't know the age of the data, we don't update.
                                    try:
                                        weather_data_epoch = int(self.masterWeatherDict[location]['current_observation']['observation_epoch'])
                                    except ValueError:
                                        weather_data_epoch = 0

                                    good_time = device_epoch <= weather_data_epoch
                                    if not good_time:
                                        self.logger.info(u"Latest data are older than data we already have. Skipping "
                                                         u"{0} update.".format(dev.name))

                                except KeyError:
                                    self.Fogbert.pluginErrorHandler(traceback.format_exc())
                                    self.logger.info(u"{0} cannot determine age of data. Skipping until next "
                                                     u"scheduled poll.".format(dev.name))
                                    good_time = False

                                # If the weather dict is not empty, the data are newer than the data we already have, an
                                # the user doesn't want to ignore estimated weather conditions, let's update the
                                # devices.
                                if self.masterWeatherDict != {} and good_time and not ignore_estimated:

                                    # Almanac devices.
                                    if dev.model in ['Almanac', 'WUnderground Almanac']:
                                        self.parseAlmanacData(dev)

                                    # Astronomy devices.
                                    elif dev.model in ['Astronomy', 'WUnderground Astronomy']:
                                        self.parseAstronomyData(dev)

                                    # Hourly Forecast devices.
                                    elif dev.model in ['WUnderground Hourly Forecast', 'Hourly Forecast']:
                                        self.parseHourlyData(dev)

                                    # Ten Day Forecast devices.
                                    elif dev.model in ['Ten Day Forecast', 'WUnderground Ten Day Forecast']:
                                        self.parseTenDayData(dev)

                                    # Tide devices.
                                    elif dev.model in ['WUnderground Tides', 'Tides']:
                                        self.parseTidesData(dev)

                                    # Weather devices.
                                    elif dev.model in ['WUnderground Device', 'WUnderground Weather', 'WUnderground Weather Device', 'Weather Underground', 'Weather']:
                                        self.parseWeatherData(dev)
                                        self.parseAlertsData(dev)
                                        self.parseForecastData(dev)

                                        if self.pluginPrefs.get('updaterEmailsEnabled', False):
                                            self.emailForecast(dev)

                        # Image Downloader devices.
                        elif dev.model in ['Satellite Image Downloader', 'WUnderground Satellite Image Downloader']:
                            self.getSatelliteImage(dev)

                        # WUnderground Radar devices.
                        elif dev.model in ['WUnderground Radar']:
                            self.getWUradar(dev)

                self.logger.debug(u"{0} locations polled: {1}".format(len(self.masterWeatherDict.keys()), self.masterWeatherDict.keys()))

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.error(u"Problem parsing Weather data. Dev: {0}".format(dev.name))

    def triggerProcessing(self):
        """
        Fire various triggers for plugin devices

        Weather Location Offline:
        The triggerProcessing method will examine the time of the last weather location
        update and, if the update exceeds the time delta specified in a WUnderground
        Plugin Weather Location Offline trigger, the trigger will be fired. The plugin
        examines the value of the latest "currentObservationEpoch" and *not* the Indigo
        Last Update value.

        An additional event that will cause a trigger to be fired is if the weather
        location temperature is less than -55 (Weather Underground will often set a
        value to a variation of -99 (-55 C) to indicate that a data value is invalid.

        Severe Weather Alerts:
        This trigger will fire if a weather location has at least one severe weather
        alert.

        Note that trigger processing will only occur during routine weather update
        cycles and will not be triggered when a data refresh is called from the Indigo
        Plugins menu.

        -----
        """

        time_format = '%Y-%m-%d %H:%M:%S'

        # Reconstruct the masterTriggerDict in case it has changed.
        self.masterTriggerDict = {unicode(trigger.pluginProps['listOfDevices']): (trigger.pluginProps['offlineTimer'], trigger.id) for trigger in indigo.triggers.iter(filter="self.weatherSiteOffline")}
        self.logger.debug(u"Rebuild Master Trigger Dict: {0}".format(self.masterTriggerDict))

        try:

            # Iterate through all the plugin devices to see if a related trigger should be fired
            for dev in indigo.devices.itervalues(filter='self'):

                # ========================== Weather Location Offline ==========================
                # If the device is in the masterTriggerDict, it has an offline trigger
                if str(dev.id) in self.masterTriggerDict.keys():

                    # Process the trigger only if the device is enabled
                    if dev.enabled:

                        trigger_id = self.masterTriggerDict[str(dev.id)][1]  # Indigo trigger ID

                        if indigo.triggers[trigger_id].pluginTypeId == 'weatherSiteOffline':

                            offline_delta = dt.timedelta(minutes=int(self.masterTriggerDict.get(unicode(dev.id), ('60', ''))[0]))
                            self.logger.debug(u"Offline weather location delta: {0}".format(offline_delta))

                            # Convert currentObservationEpoch to a localized datetime object
                            current_observation_epoch = float(dev.states['currentObservationEpoch'])

                            current_observation = time.strftime(time_format, time.localtime(current_observation_epoch))
                            current_observation = dt.datetime.strptime(current_observation, time_format)

                            # Time elapsed since last observation
                            diff = indigo.server.getTime() - current_observation

                            # If the observation is older than offline_delta
                            if diff >= offline_delta:
                                total_seconds = int(diff.total_seconds())
                                days, remainder = divmod(total_seconds, 60 * 60 * 24)
                                hours, remainder = divmod(remainder, 60 * 60)
                                minutes, seconds = divmod(remainder, 60)

                                # Note that we leave seconds off, but it could easily be added if needed.
                                diff_msg = u'{} days, {} hrs, {} mins'.format(days, hours, minutes)

                                dev.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
                                dev.updateStateOnServer('onOffState', value='offline')

                                if indigo.triggers[trigger_id].enabled:
                                    self.logger.warning(u"{0} location appears to be offline for {1}".format(dev.name, diff_msg))
                                    indigo.trigger.execute(trigger_id)

                            # If the temperature observation is lower than -55
                            elif dev.states['temp'] <= -55.0:
                                dev.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
                                dev.updateStateOnServer('onOffState', value='offline')

                                if indigo.triggers[trigger_id].enabled:
                                    self.logger.warning(u"{0} location appears to be offline (ambient temperature lower than -55).".format(dev.name))
                                    indigo.trigger.execute(trigger_id)

                # ============================ Severe Weather Alert ============================
                for trigger in indigo.triggers.itervalues('self.weatherAlert'):

                    if int(trigger.pluginProps['listOfDevices']) == dev.id and dev.states['alertStatus'] == 'true' and trigger.enabled:

                        self.logger.warning(u"{0} location has at least one severe weather alert.".format(dev.name))
                        indigo.trigger.execute(trigger.id)

        except KeyError:
            pass

    def uiFormatItemListTemperature(self, val):
        """
        Format temperature values for Indigo UI

        Adjusts the decimal precision of the temperature value for the Indigo Item
        List. Note: this method needs to return a string rather than a Unicode string
        (for now.)

        -----

        :param val:
        """

        try:
            if int(self.pluginPrefs.get('itemListTempDecimal', '1')) == 0:
                val = float(val)
                return u"{0:0.0f}".format(val)
            else:
                return u"{0}".format(val)

        except ValueError:
            return u"{0}".format(val)

    def uiFormatPercentage(self, dev, state_name, val):
        """
        Format percentage data for Indigo UI

        Adjusts the decimal precision of percentage values for display in control
        pages, etc.

        -----

        :param indigo.Device dev:
        :param str state_name:
        :param str val:
        """

        humidity_decimal = int(self.pluginPrefs.get('uiHumidityDecimal', '1'))
        percentage_units = unicode(dev.pluginProps.get('percentageUnits', ''))

        try:
            return u"{0:0.{precision}f}{1}".format(float(val), percentage_units, precision=humidity_decimal)

        except ValueError:
            return u"{0}{1}".format(val, percentage_units)

    def uiFormatPressureSymbol(self, state_name, val):
        """
        Convert pressure trend symbol

        Converts the barometric pressure trend symbol to something more human friendly.

        -----

        :param str state_name:
        :param val:
        """

        pref       = self.pluginPrefs['uiPressureTrend']
        translator = {'graphic': {'+': u'\u2B06'.encode('utf-8'), '-': u'\u2B07'.encode('utf-8'), '0': u'\u27A1'.encode('utf-8')},
                      'lower_letters': {'+': 'r', '-': 'f', '0': 's'},
                      'lower_words': {'+': 'rising', '-': 'falling', '0': 'steady'},
                      'native': {'+': '+', '-': '-', '0': '0'},
                      'text': {'+': '^', '-': 'v', '0': '-'},
                      'upper_letters': {'+': 'R', '-': 'F', '0': 'S'},
                      'upper_words': {'+': 'Rising', '-': 'Falling', '0': 'Steady'},
                      }

        try:
            return translator[pref][val]

        except Exception:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.debug(u"Error setting {0} pressure.")
            return val

    def uiFormatRain(self, dev, state_name, val):
        """
        Format rain data for Indigo UI

        Adds rain units to rain values for display in control pages, etc.

        -----

        :param indigo.Devices dev:
        :param str state_name:
        :param val:
        """

        # Some devices use the prop 'rainUnits' and some use the prop
        # 'rainAmountUnits'.  So if we fail on the first, try the second and--if still
        # not successful, return and empty string.
        try:
            rain_units = dev.pluginProps['rainUnits']
        except KeyError:
            rain_units = dev.pluginProps.get('rainAmountUnits', '')

        if val in ["NA", "N/A", "--", ""]:
            return val

        try:
            return u"{0:0.2f}{1}".format(float(val), rain_units)

        except ValueError:
            return u"{0}".format(val)

    def uiFormatSnow(self, dev, state_name, val):
        """
        Format snow data for Indigo UI

        Adjusts the display format of snow values for display in control pages, etc.

        -----

        :param indigo.Device dev:
        :param str state_name:
        :param val:
        """

        if val in ["NA", "N/A", "--", ""]:
            return val

        try:
            return u"{0}{1}".format(val, dev.pluginProps.get('snowAmountUnits', ''))

        except ValueError:
            return u"{0}".format(val)

    def uiFormatTemperature(self, dev, state_name, val):
        """
        Format temperature data for Indigo UI

        Adjusts the decimal precision of certain temperature values and appends the
        desired units string for display in control pages, etc.

        -----

        :param indigo.Device dev:
        :param str state_name:
        :param val:
        """

        temp_decimal      = int(self.pluginPrefs.get('uiTempDecimal', '1'))
        temperature_units = unicode(dev.pluginProps.get('temperatureUnits', ''))

        try:
            return u"{0:0.{precision}f}{1}".format(float(val), temperature_units, precision=temp_decimal)

        except ValueError:
            return u"--"

    def uiFormatWind(self, dev, state_name, val):
        """
        Format wind data for Indigo UI

        Adjusts the decimal precision of certain wind values for display in control
        pages, etc.

        -----

        :param indigo.Device dev:
        :param str state_name:
        :param val:
        """

        wind_decimal = int(self.pluginPrefs.get('uiWindDecimal', '1'))
        wind_units   = unicode(dev.pluginProps.get('windUnits', ''))

        try:
            return u"{0:0.{precision}f}{1}".format(float(val), wind_units, precision=wind_decimal)

        except ValueError:
            return u"{0}".format(val)

    def verboseWindNames(self, state_name, val):
        """
        Format wind data for Indigo UI

        The verboseWindNames() method takes possible wind direction values and
        standardizes them across all device types and all reporting stations to ensure
        that we wind up with values that we can recognize.

        -----

        :param str state_name:
        :param val:
        """

        wind_dict = {'N': 'north',
                     'North': 'north',
                     'NNE': 'north northeast',
                     'NE': 'northeast',
                     'ENE': 'east northeast',
                     'E': 'east',
                     'East': 'east',
                     'ESE': 'east southeast',
                     'SE': 'southeast',
                     'SSE': 'south southeast',
                     'S': 'south',
                     'South': 'south',
                     'SSW': 'south southwest',
                     'SW': 'southwest',
                     'WSW': 'west southwest',
                     'W': 'west',
                     'West': 'west',
                     'WNW': 'west northwest',
                     'NW': 'northwest',
                     'NNW': 'north northwest'
                     }

        try:
            return wind_dict[val]

        except KeyError:
            self.Fogbert.pluginErrorHandler(traceback.format_exc())
            self.logger.debug(u"Error formatting {0} verbose wind names: {1}".format(state_name, val))
            return val

    def wundergroundSite(self, values_dict):
        """
        Launch a web browser to register for API

        Launch a web browser session with the values_dict parm containing the target
        URL.

        -----

        :param indigo.Dict values_dict:
        """

        self.Fogbert.launchWebPage(values_dict['launchWUparameters'])

