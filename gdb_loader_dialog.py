"""
Geodatabase Loader
============================================
Load File Geodatabases (.gdb) into QGIS while preserving the
Feature Dataset hierarchy as layer-tree groups.

Features:
- Browse for a .gdb folder directly
- Enumerate layers using the OpenFileGDB driver (GDAL ≥ 3.4
  Group API for Feature Dataset detection, with fallback)
- Display layers in a tree grouped by Feature Dataset
- Select / deselect / toggle individual layers to load
- Filter the layer list by name
- Creates a group per Feature Dataset in the QGIS layer tree
- Layers outside any Feature Dataset go under the .gdb root group
- Background loading with progress reporting
- Detailed logging
"""

import os
import re
from typing import List, Dict, Optional, Tuple
from datetime import datetime

from qgis.PyQt.QtCore import Qt, QEvent
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QAbstractItemView,
    QGroupBox, QCheckBox, QLineEdit, QTabWidget,
    QWidget, QScrollArea, QFileDialog, QApplication, QMessageBox,
    QHeaderView, QFrame, QTreeWidget, QTreeWidgetItem, QSizePolicy,
)
from qgis.PyQt.QtGui import QFont, QIcon

from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer,
    QgsMessageLog, Qgis, QgsLayerTreeGroup,
)
from qgis.gui import QgsFilterLineEdit


LOG_TAG = "GDB Loader"
ICON_PATH = os.path.join(os.path.dirname(__file__), "icon.svg")
# Scoped enum (3.30+/QGIS 4); legacy flat name is looked up dynamically for 3.22-3.28
try:
    LOG_WARNING = Qgis.MessageLevel.Warning
except AttributeError:
    LOG_WARNING = getattr(Qgis, "Warning")


def is_dark_palette(widget: QWidget) -> bool:
    """True when the widget's current palette is a dark theme."""
    return widget.palette().window().color().lightness() < 128


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def format_timestamp() -> str:
    """Return formatted timestamp for logging."""
    return datetime.now().strftime("%H:%M:%S")


_DIGITS_RE = re.compile(r"[0-9]+")


def natural_sort_key(text: str) -> Tuple:
    """Sort key that orders names the way Windows Explorer does.

    - case-insensitive ("uncategorised" sorts with "UG_..." / "WATERWAYS")
    - runs of digits compare by value ("2PCT" < "10PCT")
    - punctuation such as "_" or "-" sorts before digits, digits before letters
    """
    tokens = []
    pos = 0
    for m in _DIGITS_RE.finditer(text):
        for ch in text[pos:m.start()]:
            tokens.append((2, ch.casefold()) if ch.isalpha() else (0, ord(ch)))
        tokens.append((1, int(m.group()), len(m.group())))
        pos = m.end()
    for ch in text[pos:]:
        tokens.append((2, ch.casefold()) if ch.isalpha() else (0, ord(ch)))
    # Original text as tie-breaker keeps the order stable for e.g. "A1" / "a1"
    return tuple(tokens), text


def enumerate_gdb_layers(
    gdb_path: str,
    include_non_spatial: bool = False,
) -> List[Dict]:
    """
    Enumerate layers inside a File Geodatabase using the OpenFileGDB driver.

    Each returned dict has:
        name        – layer name (str)
        dataset     – Feature Dataset name or None
        geom_type   – geometry type string (Point, Line, Polygon, …)
                      or 'Table' for non-spatial
        geom_base   – base geometry family: 'point', 'line', 'polygon',
                      'table', or 'unknown' (for icon selection)
        crs         – CRS auth id string (e.g. 'EPSG:7855') or ''
        feat_count  – feature / row count (-1 if unknown)
        uri         – full URI for loading ('path.gdb|layername=Name')

    Strategy:
        1. GDAL GDALGroup API (≥ 3.4) – preserves Feature Dataset hierarchy.
        2. osgeo.ogr flat enumeration – no hierarchy info.
        3. QgsVectorLayer.dataProvider().subLayers() – last-resort fallback.
    """
    results: List[Dict] = []
    norm_path = os.path.normpath(gdb_path)

    # Base OGR geometry type names (after flattening Z/M/ZM flags)
    _OGR_BASE_MAP = {
        0: ("Unknown", "unknown"),
        1: ("Point", "point"),
        2: ("LineString", "line"),
        3: ("Polygon", "polygon"),
        4: ("MultiPoint", "point"),
        5: ("MultiLineString", "line"),
        6: ("MultiPolygon", "polygon"),
        7: ("GeometryCollection", "unknown"),
        100: ("None", "table"),
    }

    def _geom_label(ogr_type: int) -> Tuple[str, str]:
        """Return (human_label, base_family) from an OGR geometry type.

        Handles Z, M, and ZM variants by flattening with ogr.GT_Flatten
        and appending the appropriate suffix.
        """
        try:
            from osgeo import ogr as _ogr
            flat = _ogr.GT_Flatten(ogr_type)
        except Exception:
            flat = ogr_type & 0x0FFFFFFF  # manual mask as fallback

        label, family = _OGR_BASE_MAP.get(flat, (f"Type({flat})", "unknown"))

        # Detect Z / M / ZM suffix
        try:
            from osgeo import ogr as _ogr
            has_z = _ogr.GT_HasZ(ogr_type)
            has_m = _ogr.GT_HasM(ogr_type)
        except Exception:
            has_z = bool(ogr_type & 0x80000000)
            has_m = bool(ogr_type & 0x40000000)

        if has_z and has_m:
            label += " ZM"
        elif has_z:
            label += " Z"
        elif has_m:
            label += " M"

        return label, family

    def _get_crs_string(ogr_layer) -> str:
        """Extract a CRS auth id (e.g. 'EPSG:7855') from an OGR layer."""
        try:
            srs = ogr_layer.GetSpatialRef()
            if srs is None:
                return ""
            auth_name = srs.GetAuthorityName(None) or ""
            auth_code = srs.GetAuthorityCode(None) or ""
            if auth_name and auth_code:
                return f"{auth_name}:{auth_code}"
            return srs.GetName() or ""
        except Exception:
            return ""

    # ---- Strategy 1: GDAL Group API (preserves Feature Datasets) --------
    try:
        from osgeo import gdal, ogr

        ds = gdal.OpenEx(
            norm_path, gdal.OF_VECTOR,
            allowed_drivers=["OpenFileGDB"],
        )
        if ds is None:
            # Retry without explicit driver hint
            ds = gdal.OpenEx(norm_path, gdal.OF_VECTOR)

        if ds is not None:
            root = ds.GetRootGroup() if hasattr(ds, "GetRootGroup") else None

            if root is not None:
                # --- Top-level layers (no Feature Dataset) ---
                top_names = (
                    root.GetVectorLayerNames()
                    if hasattr(root, "GetVectorLayerNames")
                    else []
                ) or []

                for name in top_names:
                    lyr = ds.GetLayerByName(name)
                    if lyr is None:
                        continue
                    gt = lyr.GetGeomType()
                    if gt == ogr.wkbNone and not include_non_spatial:
                        continue
                    if gt == ogr.wkbNone:
                        gl, gf = "Table", "table"
                    else:
                        gl, gf = _geom_label(gt)
                    results.append({
                        "name": name,
                        "dataset": None,
                        "geom_type": gl,
                        "geom_base": gf,
                        "crs": _get_crs_string(lyr),
                        "feat_count": lyr.GetFeatureCount(),
                        "uri": f"{norm_path}|layername={name}",
                    })

                # --- Feature Datasets (sub-groups) ---
                group_names = (
                    root.GetGroupNames()
                    if hasattr(root, "GetGroupNames")
                    else []
                ) or []

                for gname in group_names:
                    grp = root.OpenGroup(gname)
                    if grp is None:
                        continue
                    child_names = (
                        grp.GetVectorLayerNames()
                        if hasattr(grp, "GetVectorLayerNames")
                        else []
                    ) or []
                    for name in child_names:
                        lyr = ds.GetLayerByName(name)
                        if lyr is None:
                            continue
                        gt = lyr.GetGeomType()
                        if gt == ogr.wkbNone and not include_non_spatial:
                            continue
                        if gt == ogr.wkbNone:
                            gl, gf = "Table", "table"
                        else:
                            gl, gf = _geom_label(gt)
                        results.append({
                            "name": name,
                            "dataset": gname,
                            "geom_type": gl,
                            "geom_base": gf,
                            "crs": _get_crs_string(lyr),
                            "feat_count": lyr.GetFeatureCount(),
                            "uri": f"{norm_path}|layername={name}",
                        })

                ds = None
                if results:
                    return sorted(
                        results,
                        key=lambda r: (
                            natural_sort_key(r["dataset"] or ""),
                            natural_sort_key(r["name"]),
                        ),
                    )

            # --- Group API unavailable – flat enumeration ---
            for i in range(ds.GetLayerCount() if ds else 0):
                lyr = ds.GetLayerByIndex(i)
                if lyr is None:
                    continue
                gt = lyr.GetGeomType()
                if gt == ogr.wkbNone and not include_non_spatial:
                    continue
                if gt == ogr.wkbNone:
                    gl, gf = "Table", "table"
                else:
                    gl, gf = _geom_label(gt)
                results.append({
                    "name": lyr.GetName(),
                    "dataset": None,
                    "geom_type": gl,
                    "geom_base": gf,
                    "crs": _get_crs_string(lyr),
                    "feat_count": lyr.GetFeatureCount(),
                    "uri": f"{norm_path}|layername={lyr.GetName()}",
                })
            if ds:
                ds = None
            if results:
                return sorted(results, key=lambda r: natural_sort_key(r["name"]))

    except Exception as exc:
        QgsMessageLog.logMessage(
            f"GDAL enumerate failed for '{gdb_path}': {exc}",
            LOG_TAG, LOG_WARNING,
        )

    # ---- Strategy 2: QgsVectorLayer.dataProvider().subLayers() ----------
    try:
        tmp = QgsVectorLayer(norm_path, "gdb_probe", "ogr")
        sub = tmp.dataProvider().subLayers() if tmp.dataProvider() else []
        for entry in sub:
            parts = entry.split("!!::!!")
            if len(parts) >= 4:
                lyr_name = parts[1]
                feat_cnt = int(parts[2]) if parts[2].isdigit() else -1
                geom_str = parts[3]
                if geom_str.lower() in ("aspatial", "none", "") and not include_non_spatial:
                    continue
                # Derive base geometry family from the subLayers string
                gs = geom_str.lower()
                if "point" in gs:
                    gf = "point"
                elif "line" in gs:
                    gf = "line"
                elif "polygon" in gs:
                    gf = "polygon"
                elif gs in ("aspatial", "none", ""):
                    gf = "table"
                else:
                    gf = "unknown"
                results.append({
                    "name": lyr_name,
                    "dataset": None,
                    "geom_type": geom_str if geom_str else "Table",
                    "geom_base": gf,
                    "crs": "",  # subLayers fallback – CRS unavailable
                    "feat_count": feat_cnt,
                    "uri": f"{norm_path}|layername={lyr_name}",
                })
        del tmp
    except Exception as exc:
        QgsMessageLog.logMessage(
            f"subLayers probe failed for '{gdb_path}': {exc}",
            LOG_TAG, LOG_WARNING,
        )

    return sorted(results, key=lambda r: natural_sort_key(r["name"]))


def build_gdb_tree(layers: List[Dict]) -> Dict:
    """
    Build a nested tree dict from the enumerated layer list.

    Structure:
        { 'FeatureDatasetName': { '_layers': [layer_dict, …] },
          '_layers': [layer_dict, …]   # layers not in any dataset
        }
    """
    tree: Dict = {"_layers": []}
    for lyr in layers:
        ds = lyr.get("dataset")
        if ds:
            tree.setdefault(ds, {"_layers": []})
            tree[ds]["_layers"].append(lyr)
        else:
            tree["_layers"].append(lyr)
    return tree


# ============================================================================
# MAIN DIALOG CLASS
# ============================================================================

class GeodatabaseLoaderDialog(QDialog):
    """Main dialog for loading a File Geodatabase into QGIS."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Geodatabase Loader")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(1100, 650)
        self.setMinimumWidth(900)

        # State
        self.gdb_path: Optional[str] = None
        self.layer_list: List[Dict] = []  # result of enumerate_gdb_layers
        self._summary_state = "idle"  # idle | found | empty
        self._bulk_update = False  # suppress per-item refresh during bulk checks

        self._build_ui()
        self._wire_signals()
        self._validate()

    # ------------------------------------------------------------------
    # THEME (light / dark) HELPERS
    # ------------------------------------------------------------------
    def _muted_color(self) -> str:
        return "#9aa0a6" if is_dark_palette(self) else "#6b7280"

    def _warn_color(self) -> str:
        return "#f0a030" if is_dark_palette(self) else "#b45f06"

    def _ok_color(self) -> str:
        return "#3fb950" if is_dark_palette(self) else "#1a7f37"

    def _info_color(self) -> str:
        return "#58a6ff" if is_dark_palette(self) else "#0969da"

    def _apply_theme(self):
        """(Re)apply palette-derived colours; safe to call repeatedly."""
        if hasattr(self, "summary_label"):
            if self._summary_state == "idle":
                self.summary_label.setStyleSheet(
                    f"color: {self._muted_color()}; font-style: italic;"
                )
            elif self._summary_state == "empty":
                self.summary_label.setStyleSheet(f"color: {self._warn_color()};")
            else:
                self.summary_label.setStyleSheet("font-weight: bold;")
        if hasattr(self, "count_label"):
            self._update_count()

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            # Defer-safe: avoid re-entrancy when the stylesheet itself changes
            if not getattr(self, "_theming", False):
                self._theming = True
                try:
                    self._apply_theme()
                finally:
                    self._theming = False
        super().changeEvent(event)

    # ==================================================================
    # UI CONSTRUCTION
    # ==================================================================
    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        # --- Parameters tab ---
        self.parameters_tab = QWidget()
        self._setup_parameters_tab()
        self.tab_widget.addTab(self.parameters_tab, "Parameters")

        # --- Log tab ---
        self.log_tab = QWidget()
        self._setup_log_tab()
        self.tab_widget.addTab(self.log_tab, "Log")

        # --- Bottom bar ---
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(3, 3, 3, 3)
        bottom_layout.setSpacing(3)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setValue(0)
        bottom_layout.addWidget(self.progress_bar)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(3)
        btn_layout.addStretch()

        self.load_btn = QPushButton("Load")
        self.load_btn.setDefault(True)
        self.load_btn.setEnabled(False)
        btn_layout.addWidget(self.load_btn)

        self.close_btn = QPushButton("Close")
        btn_layout.addWidget(self.close_btn)

        bottom_layout.addLayout(btn_layout)
        main_layout.addWidget(bottom)

    # ------------------------------------------------------------------
    def _setup_parameters_tab(self):
        main_h = QHBoxLayout(self.parameters_tab)
        main_h.setContentsMargins(0, 0, 0, 0)
        main_h.setSpacing(0)

        # LEFT – scrollable parameters
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.StyledPanel)
        scroll.setStyleSheet(
            "QScrollArea { background-color: palette(base); border: none; }"
        )

        scroll_content = QWidget()
        scroll_content.setStyleSheet("""
            QWidget#scrollContent { background-color: palette(base); }
            QGroupBox { background-color: palette(base); }
        """)
        scroll_content.setObjectName("scrollContent")
        scroll_content.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(6, 6, 6, 6)
        scroll_layout.setSpacing(4)

        scroll_layout.addWidget(self._create_input_group())
        scroll_layout.addWidget(self._create_options_group())
        scroll_layout.addStretch()

        scroll.setWidget(scroll_content)
        main_h.addWidget(scroll, 70)

        # RIGHT – help panel
        help_panel = QFrame()
        help_panel.setObjectName("helpPanel")
        help_panel.setStyleSheet(
            "QFrame#helpPanel { background-color: palette(alternate-base); }"
            "QFrame#helpPanel QLabel { background: transparent; }"
        )
        help_layout = QVBoxLayout(help_panel)
        help_layout.setContentsMargins(6, 6, 6, 6)

        help_title = QLabel("<b>Geodatabase Loader</b>")
        help_title.setWordWrap(True)
        help_layout.addWidget(help_title)

        help_text = QLabel(
            "Load an Esri File Geodatabase (.gdb) into QGIS while "
            "preserving the Feature Dataset folder structure as "
            "layer-tree groups.<br><br>"

            "<b>Workflow:</b><br>"
            "1. Browse to a .gdb folder<br>"
            "2. Click <i>Scan</i> to enumerate layers<br>"
            "3. Check / uncheck the layers you want to load<br>"
            "4. Adjust options as needed<br>"
            "5. Click <i>Load</i><br><br>"

            "<b>Driver:</b><br>"
            "Uses the GDAL <code>OpenFileGDB</code> driver to read "
            "the geodatabase (read-only, no licence required).<br><br>"

            "<b>Hierarchy:</b><br>"
            "• A top-level group is created for the .gdb<br>"
            "• Sub-groups are created for each Feature Dataset<br>"
            "• Layers not in a Feature Dataset sit directly under "
            "the .gdb group<br><br>"

            "<b>Note:</b><br>"
            "Feature Dataset detection relies on the GDAL Group API "
            "(GDAL ≥ 3.4). On older GDAL versions layers load "
            "without dataset grouping."
        )
        help_text.setWordWrap(True)
        help_text.setTextFormat(Qt.TextFormat.RichText)
        help_layout.addWidget(help_text)
        help_layout.addStretch()

        main_h.addWidget(help_panel, 30)

    # ------------------------------------------------------------------
    def _create_input_group(self) -> QGroupBox:
        group = QGroupBox("Geodatabase")
        layout = QVBoxLayout()
        layout.setSpacing(6)

        # --- GDB path ---
        lbl = QLabel("File Geodatabase (.gdb) path:")
        layout.addWidget(lbl)

        path_row = QHBoxLayout()
        path_row.setSpacing(4)

        self.gdb_edit = QLineEdit()
        self.gdb_edit.setPlaceholderText("Select a .gdb folder…")
        path_row.addWidget(self.gdb_edit)

        self.browse_btn = QPushButton()
        self.browse_btn.setIcon(
            QgsApplication.getThemeIcon("/mActionFileOpen.svg")
        )
        self.browse_btn.setToolTip("Browse for .gdb folder")
        self.browse_btn.setFixedSize(24, 24)
        path_row.addWidget(self.browse_btn)

        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setMaximumWidth(80)
        self.scan_btn.setEnabled(False)
        path_row.addWidget(self.scan_btn)

        layout.addLayout(path_row)

        # Summary label
        self.summary_label = QLabel(
            "No geodatabase selected. Browse to a .gdb folder and click Scan."
        )
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        # Name filter
        self.filter_edit = QgsFilterLineEdit()
        self.filter_edit.setShowSearchIcon(True)
        self.filter_edit.setPlaceholderText("Search feature class name…")
        self.filter_edit.setToolTip(
            "Show only layers whose name (or Feature Dataset name) "
            "contains this text. Case-insensitive."
        )
        self.filter_edit.setEnabled(False)
        layout.addWidget(self.filter_edit)

        # Layer tree
        self.layer_tree = QTreeWidget()
        self.layer_tree.setHeaderLabels(["Layer", "Type", "Features", "CRS"])
        self.layer_tree.setColumnCount(4)
        self.layer_tree.setAlternatingRowColors(True)
        self.layer_tree.setMinimumHeight(300)
        self.layer_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.layer_tree.setToolTip(
            "Check/uncheck layers to select them for loading"
        )

        # Layer column takes all remaining space; Type / Features / CRS are
        # kept compact (Features and CRS share the same width).
        header = self.layer_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        self._resize_columns()

        layout.addWidget(self.layer_tree)

        # Select all / none + count
        sel_row = QHBoxLayout()
        sel_row.setSpacing(4)

        self.select_all_btn = QPushButton("Check All")
        self.select_all_btn.setEnabled(False)
        sel_row.addWidget(self.select_all_btn)

        self.select_none_btn = QPushButton("Uncheck All")
        self.select_none_btn.setEnabled(False)
        sel_row.addWidget(self.select_none_btn)

        self.toggle_btn = QPushButton("Toggle Selection")
        self.toggle_btn.setToolTip(
            "Tick the highlighted rows, or untick them if they are all "
            "ticked (Ctrl/Shift-click to highlight several rows)"
        )
        self.toggle_btn.setEnabled(False)
        sel_row.addWidget(self.toggle_btn)

        sel_row.addStretch()

        self.count_label = QLabel("")
        sel_row.addWidget(self.count_label)

        layout.addLayout(sel_row)

        group.setLayout(layout)
        return group

    # ------------------------------------------------------------------
    def _create_options_group(self) -> QGroupBox:
        group = QGroupBox("Options")
        layout = QHBoxLayout()
        layout.setSpacing(16)

        self.non_spatial_check = QCheckBox("Include non-spatial tables")
        self.non_spatial_check.setChecked(True)
        self.non_spatial_check.setToolTip(
            "When checked, non-spatial (attribute-only) tables are "
            "also listed and can be loaded."
        )
        layout.addWidget(self.non_spatial_check)

        self.group_check = QCheckBox("Create Feature Dataset groups")
        self.group_check.setChecked(True)
        self.group_check.setToolTip(
            "When checked, a group hierarchy matching the geodatabase "
            "Feature Dataset structure is created in the layer tree."
        )
        layout.addWidget(self.group_check)

        self.expand_check = QCheckBox(
            "Expand groups after loading"
        )
        self.expand_check.setChecked(True)
        self.expand_check.setToolTip(
            "Expand the created layer-tree groups after loading."
        )
        layout.addWidget(self.expand_check)
        layout.addStretch()

        group.setLayout(layout)
        return group

    # ------------------------------------------------------------------
    def _setup_log_tab(self):
        layout = QVBoxLayout()

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Courier New", 9))
        layout.addWidget(self.log_text)

        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self.log_text.clear)
        layout.addWidget(clear_btn)

        self.log_tab.setLayout(layout)

    # ==================================================================
    # SIGNAL WIRING
    # ==================================================================
    def _wire_signals(self):
        self.browse_btn.clicked.connect(self._browse_gdb)
        self.scan_btn.clicked.connect(self._scan_gdb)
        self.gdb_edit.textChanged.connect(self._on_path_text_changed)

        self.select_all_btn.clicked.connect(self._check_all)
        self.select_none_btn.clicked.connect(self._uncheck_all)
        self.toggle_btn.clicked.connect(self._toggle_selected)
        self.layer_tree.itemSelectionChanged.connect(self._update_toggle_enabled)
        self.layer_tree.itemChanged.connect(self._on_tree_changed)
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.non_spatial_check.stateChanged.connect(
            lambda _: self._rescan_if_loaded()
        )

        self.load_btn.clicked.connect(self._run_load)
        self.close_btn.clicked.connect(self.close)

    # ==================================================================
    # BROWSING & SCANNING
    # ==================================================================
    def _browse_gdb(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select File Geodatabase (.gdb)",
            "",
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
        )
        if folder:
            self.gdb_edit.setText(folder)
            self.gdb_path = folder
            self.scan_btn.setEnabled(True)
            self._scan_gdb()

    def _on_path_text_changed(self, text: str):
        text = text.strip()
        if text and os.path.isdir(text):
            self.gdb_path = text
            self.scan_btn.setEnabled(True)
        else:
            self.gdb_path = None
            self.scan_btn.setEnabled(bool(text))
        self._validate()

    def _rescan_if_loaded(self):
        """Re-scan when 'include non-spatial' changes and a GDB is loaded."""
        if self.gdb_path and os.path.isdir(self.gdb_path):
            self._scan_gdb()

    def _scan_gdb(self):
        if not self.gdb_path:
            return

        self._log(f"🔍 Scanning geodatabase: {self.gdb_path}")

        include_tables = self.non_spatial_check.isChecked()
        self.layer_list = enumerate_gdb_layers(
            self.gdb_path, include_non_spatial=include_tables
        )

        self.layer_tree.clear()

        if not self.layer_list:
            self.summary_label.setText(
                "No layers found in the selected geodatabase."
            )
            self._summary_state = "empty"
            self._apply_theme()
            self._set_tree_controls_enabled(False)
            self._log("⚠️ No layers found in geodatabase")
            self._update_count()
            self._validate()
            return

        # Summarise
        ds_names = {info["dataset"] for info in self.layer_list if info["dataset"]}
        spatial = sum(1 for info in self.layer_list if info["geom_type"] != "Table")
        tables = sum(1 for info in self.layer_list if info["geom_type"] == "Table")
        summary_parts = [f"{len(self.layer_list)} layer(s)"]
        if ds_names:
            summary_parts.append(f"{len(ds_names)} Feature Dataset(s)")
        if spatial:
            summary_parts.append(f"{spatial} spatial")
        if tables:
            summary_parts.append(f"{tables} table(s)")

        self.summary_label.setText(
            f"{os.path.basename(self.gdb_path)}  —  {', '.join(summary_parts)}"
        )
        self._summary_state = "found"
        self._apply_theme()

        # Build tree
        tree = build_gdb_tree(self.layer_list)
        self._populate_tree(None, tree)
        self.layer_tree.expandAll()
        self._resize_columns()
        self._apply_filter(self.filter_edit.text())

        self._set_tree_controls_enabled(True)

        self._log(f"✅ Found {len(self.layer_list)} layer(s)")
        if ds_names:
            self._log(f"   Feature Datasets: {', '.join(sorted(ds_names, key=natural_sort_key))}")
        self._update_count()
        self._validate()

    # ==================================================================
    # TREE HELPERS
    # ==================================================================
    def _populate_tree(self, parent, tree_dict: Dict):
        """Recursively populate the QTreeWidget from the nested dict."""
        datasets = sorted(
            (k for k in tree_dict if k != "_layers"), key=natural_sort_key
        )
        for key in datasets:
            subtree = tree_dict[key]
            folder_item = self._make_folder_item(key)
            if parent is None:
                self.layer_tree.addTopLevelItem(folder_item)
            else:
                parent.addChild(folder_item)
            self._populate_tree(folder_item, subtree)

        for lyr in tree_dict.get("_layers", []):
            item = self._make_layer_item(lyr)
            if parent is None:
                self.layer_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)

    def _make_folder_item(self, name: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([name, "Feature Dataset", "", ""])
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsAutoTristate
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(0, Qt.CheckState.Checked)
        item.setIcon(0, QgsApplication.getThemeIcon("/mIconFolder.svg"))
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)
        return item

    def _make_layer_item(self, lyr: Dict) -> QTreeWidgetItem:
        fc = lyr["feat_count"]
        fc_str = f"{fc:,}" if fc >= 0 else "?"
        crs_str = lyr.get("crs", "")
        item = QTreeWidgetItem(
            [lyr["name"], lyr["geom_type"], fc_str, crs_str]
        )
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Checked)
        item.setData(0, Qt.ItemDataRole.UserRole, lyr)  # store full dict
        item.setToolTip(0, lyr["name"])
        if crs_str:
            item.setToolTip(3, crs_str)

        # QGIS native icon based on base geometry family
        family = lyr.get("geom_base", "unknown")
        _ICON_MAP = {
            "point":   "/mIconPointLayer.svg",
            "line":    "/mIconLineLayer.svg",
            "polygon": "/mIconPolygonLayer.svg",
            "table":   "/mIconTableLayer.svg",
            "unknown": "/mIconVector.svg",
        }
        icon = _ICON_MAP.get(family, "/mIconVector.svg")
        item.setIcon(0, QgsApplication.getThemeIcon(icon))

        return item

    def _get_checked_layers(self) -> List[Dict]:
        """Return list of layer dicts for every checked leaf item."""
        checked: List[Dict] = []

        def _walk(item):
            if item.childCount() == 0:
                if item.checkState(0) == Qt.CheckState.Checked:
                    d = item.data(0, Qt.ItemDataRole.UserRole)
                    if d:
                        checked.append(d)
            else:
                for i in range(item.childCount()):
                    _walk(item.child(i))

        for i in range(self.layer_tree.topLevelItemCount()):
            _walk(self.layer_tree.topLevelItem(i))
        return checked

    def _iter_layer_items(self, visible_only: bool = False):
        """Yield every layer (leaf) item, optionally skipping filtered-out ones."""
        def _walk(item):
            if visible_only and item.isHidden():
                return
            if item.childCount() == 0:
                if item.data(0, Qt.ItemDataRole.UserRole):
                    yield item
            else:
                for i in range(item.childCount()):
                    yield from _walk(item.child(i))

        for i in range(self.layer_tree.topLevelItemCount()):
            yield from _walk(self.layer_tree.topLevelItem(i))

    def _selected_layer_items(self) -> List[QTreeWidgetItem]:
        """Shown layer items in the highlighted rows; a highlighted Feature
        Dataset row contributes its shown layers."""
        items: List[QTreeWidgetItem] = []
        seen = set()

        def _add(item):
            if item.isHidden():
                return
            if item.childCount() == 0:
                if item.data(0, Qt.ItemDataRole.UserRole) and id(item) not in seen:
                    seen.add(id(item))
                    items.append(item)
            else:
                for i in range(item.childCount()):
                    _add(item.child(i))

        for item in self.layer_tree.selectedItems():
            _add(item)
        return items

    def _set_check_states(self, state, items=None):
        """Set ``state`` on ``items`` (default: every shown layer) in one pass."""
        if items is None:
            items = list(self._iter_layer_items(visible_only=True))
        self._bulk_update = True
        try:
            for item in items:
                item.setCheckState(0, state)
        finally:
            self._bulk_update = False
        self._update_count()

    def _check_all(self):
        self._set_check_states(Qt.CheckState.Checked)

    def _uncheck_all(self):
        self._set_check_states(Qt.CheckState.Unchecked)

    def _toggle_selected(self):
        """Tick the highlighted rows, or untick them if they are all ticked."""
        items = self._selected_layer_items()
        if not items:
            return
        all_checked = all(
            item.checkState(0) == Qt.CheckState.Checked for item in items
        )
        self._set_check_states(
            Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked,
            items,
        )

    def _update_toggle_enabled(self):
        self.toggle_btn.setEnabled(bool(self._selected_layer_items()))

    def _set_tree_controls_enabled(self, enabled: bool):
        self.select_all_btn.setEnabled(enabled)
        self.select_none_btn.setEnabled(enabled)
        self.filter_edit.setEnabled(enabled)
        self._update_toggle_enabled()

    def _apply_filter(self, text: str):
        """Hide layers whose name doesn't contain ``text`` (case-insensitive).

        A Feature Dataset whose own name matches keeps all of its layers.
        """
        needle = text.strip().lower()

        def _filter(item, parent_match: bool) -> bool:
            name_match = not needle or needle in item.text(0).lower()
            if item.childCount() == 0:
                visible = parent_match or name_match
            else:
                visible = False
                for i in range(item.childCount()):
                    if _filter(item.child(i), parent_match or name_match):
                        visible = True
            item.setHidden(not visible)
            return visible

        for i in range(self.layer_tree.topLevelItemCount()):
            _filter(self.layer_tree.topLevelItem(i), False)
        self._update_count()
        self._update_toggle_enabled()

    def _resize_columns(self):
        """Keep Type / Features / CRS compact so the Layer column gets the rest.

        Features and CRS share one width that fits their widest value.
        """
        fm = self.layer_tree.fontMetrics()
        pad = 24

        def _w(texts) -> int:
            return max(fm.horizontalAdvance(t) for t in texts) + pad

        types = ["Type", "Feature Dataset", "MultiPolygon Z"]
        numeric = ["Features", "CRS", "99,999,999", "EPSG:7855"]
        for info in self.layer_list:
            types.append(info["geom_type"])
            fc = info["feat_count"]
            numeric.append(f"{fc:,}" if fc >= 0 else "?")
            numeric.append(info.get("crs", ""))

        num_w = min(_w(numeric), _w(["EPSG:999999"]) * 2)
        self.layer_tree.setColumnWidth(1, _w(types))
        self.layer_tree.setColumnWidth(2, num_w)
        self.layer_tree.setColumnWidth(3, num_w)

    def _on_tree_changed(self, _item, _col):
        if self._bulk_update:
            return
        self._update_count()

    def _update_count(self):
        total = len(self.layer_list)
        checked = len(self._get_checked_layers())
        if total > 0:
            text = f"{checked} of {total} selected"
            if self.filter_edit.text().strip():
                shown = sum(1 for _ in self._iter_layer_items(visible_only=True))
                text = f"{shown} shown  ·  {text}"
            self.count_label.setText(text)
            if checked == 0:
                color = self._warn_color()
            elif checked == total:
                color = self._ok_color()
            else:
                color = self._info_color()
            self.count_label.setStyleSheet(f"color: {color};")
        else:
            self.count_label.setText("")
        self._validate()

    # ==================================================================
    # VALIDATION
    # ==================================================================
    def _validate(self):
        ok = (
            self.gdb_path is not None
            and os.path.isdir(self.gdb_path or "")
            and len(self._get_checked_layers()) > 0
        )
        self.load_btn.setEnabled(ok)

    # ==================================================================
    # LOADING
    # ==================================================================
    def _run_load(self):
        selected = self._get_checked_layers()
        if not selected:
            QMessageBox.warning(self, "Nothing selected", "Select at least one layer.")
            return

        self.tab_widget.setCurrentIndex(1)  # switch to log tab
        self.load_btn.setEnabled(False)
        self.load_btn.setText("Loading…")
        self.progress_bar.setValue(0)
        QApplication.processEvents()

        gdb_name = os.path.basename(self.gdb_path)
        use_groups = self.group_check.isChecked()
        expand = self.expand_check.isChecked()

        self._log(f"\n{'=' * 60}")
        self._log(f"🚀 Loading {len(selected)} layer(s) from {gdb_name}")
        self._log(f"{'=' * 60}\n")

        root = QgsProject.instance().layerTreeRoot()

        # Create top-level group for the GDB
        gdb_group: QgsLayerTreeGroup = root.insertGroup(0, gdb_name) if use_groups else root

        # Pre-build sub-groups for Feature Datasets
        ds_groups: Dict[str, QgsLayerTreeGroup] = {}

        loaded = 0
        failed = 0
        total = len(selected)

        for idx, lyr_info in enumerate(selected):
            name = lyr_info["name"]
            uri = lyr_info["uri"]
            dataset = lyr_info["dataset"]
            geom = lyr_info["geom_type"]

            pct = int(((idx) / total) * 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"{pct}%  –  {name}")
            QApplication.processEvents()

            self._log(f"   📄 Loading: {name}  ({geom})")

            # Load layer using OpenFileGDB provider
            layer = QgsVectorLayer(uri, name, "ogr")

            if not layer.isValid():
                self._log(f"      ❌ Failed to load '{name}'")
                failed += 1
                continue

            # Add to project (without putting it in the tree yet)
            QgsProject.instance().addMapLayer(layer, False)

            # Determine target group
            if use_groups and dataset:
                if dataset not in ds_groups:
                    ds_groups[dataset] = gdb_group.addGroup(dataset)
                    self._log(f"      📁 Created group: {dataset}")
                target = ds_groups[dataset]
            else:
                target = gdb_group

            target.addLayer(layer)
            loaded += 1
            self._log(f"      ✅ Loaded ({layer.featureCount():,} features, CRS: {layer.crs().authid()})")

        # Expand / collapse
        if use_groups and expand:
            gdb_group.setExpanded(True)
            for g in ds_groups.values():
                g.setExpanded(True)
        elif use_groups and not expand:
            gdb_group.setExpanded(False)

        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("100%")

        self._log(f"\n{'=' * 60}")
        self._log(f"✅ LOADING COMPLETE")
        self._log(f"   Loaded: {loaded}   Failed: {failed}   Total: {total}")
        self._log(f"{'=' * 60}")

        # Re-enable
        self.load_btn.setEnabled(True)
        self.load_btn.setText("Load")

        # Summary message
        if failed:
            QMessageBox.warning(
                self, "Loading Complete (with errors)",
                f"Loaded {loaded} of {total} layer(s).\n"
                f"{failed} layer(s) failed — see log for details.",
            )
        else:
            QMessageBox.information(
                self, "Loading Complete",
                f"Successfully loaded {loaded} layer(s) from\n{gdb_name}",
            )

        self.progress_bar.setValue(0)

    # ==================================================================
    # LOGGING
    # ==================================================================
    def _log(self, message: str):
        ts = format_timestamp()
        self.log_text.append(f"[{ts}] {message}")
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())
