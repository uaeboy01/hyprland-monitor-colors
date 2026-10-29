import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor_colors as m
from test_monitor_colors import MONITORS, REAL_CONFIG


class Configuration(unittest.TestCase):
    def test_mapping_validation_and_custom_connectors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitors.json"
            for invalid in [{}, [], {"DP-1": 123}, {"": "Panel"}]:
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    m.load_monitor_map(path)
            mapping = {"HDMI-A-1": "Example Panel", "eDP-1": "Laptop Panel"}
            path.write_text(json.dumps(mapping))
            self.assertEqual(m.load_monitor_map(path), mapping)
            with self.assertRaisesRegex(ValueError, "--list-monitors"):
                m.load_monitor_map(path.with_name("missing.json"))

    def test_listing_only_queries_monitors(self):
        with patch.object(m, "hypr", return_value=json.dumps(MONITORS)) as ipc:
            with patch("sys.argv", ["monitor-colors", "--list-monitors"]):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(m.main(), 0)
        ipc.assert_called_once_with("-j", "monitors")
        self.assertEqual(json.loads(output.getvalue()), {
            "DP-4": "VF V1-32", "DP-2": "U28D590"
        })

    def test_show_uses_explicit_mapping_and_config_without_ipc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, mapping = root / "monitors.lua", root / "monitors.json"
            config.write_text(REAL_CONFIG)
            mapping.write_text(json.dumps({"DP-4": "VF V1-32"}))
            args = ["monitor-colors", "--show", "--config", str(config),
                    "--monitor-map", str(mapping)]
            with patch.object(m, "MODELS", {}), patch.object(m, "CONFIG", config):
                with patch.object(m, "hypr") as ipc, patch("sys.argv", args):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(m.main(), 0)
                ipc.assert_not_called()
            self.assertEqual(json.loads(output.getvalue()), {"DP-4": m.asdict(m.Settings())})
            self.assertEqual(config.read_text(), REAL_CONFIG)


class UserInterface(unittest.TestCase):
    def test_offscreen_drafts_without_apply_or_display_ipc(self):
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "monitors.lua"
            config.write_text(REAL_CONFIG)
            with patch.object(m, "CONFIG", config), patch.object(m, "MODELS", {
                "DP-4": "VF V1-32", "DP-2": "U28D590"
            }), patch.object(m, "hypr") as ipc:
                window = m.create_window(MONITORS)
                self.assertEqual(window.current, "DP-4")
                window.controls["saturation"][0].setValue(115)
                window.selector.setCurrentIndex(1)
                self.assertEqual(window.current, "DP-2")
                self.assertEqual(window.values().saturation, 100)
                window.selector.setCurrentIndex(0)
                self.assertEqual(window.values().saturation, 115)
                window.revert_selected()
                self.assertEqual(window.values(), m.Settings())
                window.close()
                app.processEvents()
                ipc.assert_not_called()
            self.assertEqual(config.read_text(), REAL_CONFIG)
