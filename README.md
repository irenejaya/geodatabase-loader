# Geodatabase Loader

A QGIS plugin for loading selected layers and tables from Esri File Geodatabases (`.gdb`). It preserves Feature Dataset structure by placing those layers into subgroups under a group named after the geodatabase.

## Use

1. Open **Geodatabase Loader** from the Plugins menu or toolbar.
2. Browse to a `.gdb` folder and scan it.
3. Select the layers or tables you want, then load them into QGIS.

The plugin uses GDAL’s OpenFileGDB driver and supports QGIS 3.22 and later. It is designed to work with both light and dark QGIS themes.

## License

MIT
