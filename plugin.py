# -*- coding: utf-8 -*-
"""Geodatabase Loader - plugin class (menu + toolbar registration)."""

import os

from qgis.PyQt.QtGui import QIcon
try:
    from qgis.PyQt.QtGui import QAction  # Qt6
except ImportError:
    from qgis.PyQt.QtWidgets import QAction  # Qt5


class GeodatabaseLoaderPlugin:
    MENU_NAME = "&Geodatabase Loader"

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.toolbar = None
        self.dlg = None

    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, "icon.svg"))
        self.action = QAction(icon, "Geodatabase Loader", self.iface.mainWindow())
        self.action.setObjectName("GeodatabaseLoaderAction")
        self.action.setToolTip(
            "Load an Esri File Geodatabase (.gdb) preserving Feature Dataset groups"
        )
        self.action.triggered.connect(self.run)
        # Dedicated toolbar so the button doesn't mix with the shared Plugins toolbar
        self.toolbar = self.iface.addToolBar("Geodatabase Loader")
        self.toolbar.setObjectName("GeodatabaseLoaderToolbar")
        self.toolbar.addAction(self.action)
        self.iface.addPluginToMenu(self.MENU_NAME, self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(self.MENU_NAME, self.action)
            self.action.deleteLater()
            self.action = None
        if self.toolbar is not None:
            self.toolbar.deleteLater()
            self.toolbar = None
        if self.dlg is not None:
            self.dlg.close()
            self.dlg = None

    def run(self):
        from .gdb_loader_dialog import GeodatabaseLoaderDialog

        try:
            self.dlg = GeodatabaseLoaderDialog(self.iface.mainWindow())
            self.dlg.exec()
        except Exception as exc:
            import traceback
            self.iface.messageBar().pushCritical(
                "Geodatabase Loader", f"Failed to launch: {exc}"
            )
            traceback.print_exc()
