#!/usr/bin/env python2.6
"""
DLFramework is a framework to consolidate methods used throughout all
Indigo plugins with the com.fogbert.indigoPlugin.xxxx bundle identifier.
"""

import indigo
import platform
import sys


__author__ = "DaveL17"
__build__ = ""
__copyright__ = "Copyright 2017 DaveL17"
__license__ = "MIT"
__title__ = "DLFramework"
__version__ = "0.1.01"


class Fogbert(object):

        def __init__(self, plugin):
            self.plugin = plugin
            self.plugin.debugLog(u"Initializing DLFramework...")

        def pluginEnvironment(self):
            """
            The pluginEnvironment method prints selected information about the
            pluginEnvironment that the plugin is running in. It pulls some of this
            information from the calling plugin and some from the server
            pluginEnvironment.
            """
            self.plugin.debugLog(u"DLFramework pluginEnvironment method called.")

            indigo.server.log(u"")
            indigo.server.log(u"{0:=^130}".format(" Initializing New Plugin Session "))
            indigo.server.log(u"{0:<31} {1}".format("Plugin name:", self.plugin.pluginDisplayName))
            indigo.server.log(u"{0:<31} {1}".format("Plugin version:", self.plugin.pluginVersion))
            indigo.server.log(u"{0:<31} {1}".format("Plugin ID:", self.plugin.pluginId))
            indigo.server.log(u"{0:<31} {1}".format("Indigo version:", indigo.server.version))
            indigo.server.log(u"{0:<31} {1}".format("Python version:", sys.version.replace('\n', '')))
            indigo.server.log(u"{0:<31} {1}".format("Mac OS Version:", platform.mac_ver()[0]))
            indigo.server.log(u"{0:=^130}".format(""))

        def convertDebugLevel(self, debugVal):
            """
            docstring placeholder
            """
            self.plugin.debugLog(u"DLFramework somethingElse method called.")

            if debugVal == "High":
                return 3
            elif debugVal == "Medium":
                return 2
            else:
                return 1