# -*- coding: utf-8 -*-
"""Geodatabase Loader - QGIS plugin."""


def classFactory(iface):
    from .plugin import GeodatabaseLoaderPlugin
    return GeodatabaseLoaderPlugin(iface)
