#!/usr/bin/env python3
"""Focused validation for saved ICC behavior and independent monitor edits."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import monitor_colors as m

REAL_CONFIG = '''hl.monitor({
    output = "DP-4",
    mode = "3840x2160@160",
    position = "0x0",
    scale = 1.5,
})
hl.monitor({
    output = "DP-2",
    mode = "3840x2160@60",
    position = "2560x0",
    scale = 1.5,
})
'''
MONITORS = [
    {"name": "DP-4", "model": "VF V1-32", "width": 3840, "height": 2160,
     "refreshRate": 60.0, "x": 0, "y": 0, "scale": 1.5, "transform": 0, "disabled": False},
    {"name": "DP-2", "model": "U28D590", "width": 3840, "height": 2160,
     "refreshRate": 59.997, "x": 2560, "y": 0, "scale": 1.5, "transform": 0, "disabled": False},
]

class Transactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "monitors.lua"
        self.config.write_text(REAL_CONFIG)
        self.patches = [
            patch.object(m, "MODELS", {"DP-4": "VF V1-32", "DP-2": "U28D590"}),
            patch.object(m, "HOME", self.root),
            patch.object(m, "CONFIG", self.config),
            patch.object(m, "PROFILE_DIR", self.root / "icc"),
            patch.object(m.time, "sleep", lambda _: None),
        ]
        for p in self.patches:
            p.start()
        self.calls = []
        def ipc(*args):
            self.calls.append(args)
            return json.dumps(MONITORS) if args == ("-j", "monitors") else ""
        self.ipc = ipc

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_first_monitor_change_preserves_second_and_reset_restores_exact_file(self):
        before = m.target_block(REAL_CONFIG, "DP-2").group()
        requested = m.Settings(saturation=115, contrast=105, gamma=110)
        with patch.object(m, "hypr", self.ipc):
            m.apply_settings("DP-4", requested, expected=m.Settings())
            saved = self.config.read_text()
            self.assertEqual(m.read_settings(saved, "DP-4"), requested)
            self.assertEqual(m.target_block(saved, "DP-2").group(), before)
            m.apply_settings("DP-4", m.Settings(), expected=requested)
        self.assertEqual(self.config.read_text(), REAL_CONFIG)
        self.assertEqual(self.config.with_name("monitors.lua.before-monitor-colors").read_text(), REAL_CONFIG)

    def test_migrate_second_monitor_keeps_existing_saturation(self):
        old_settings = m.read_settings(REAL_CONFIG, "DP-2")
        with patch.object(m, "hypr", self.ipc):
            m.apply_settings("DP-2", old_settings, expected=old_settings)
        saved = self.config.read_text()
        self.assertEqual(m.read_settings(saved, "DP-2"), old_settings)
        self.assertEqual(m.target_block(saved, "DP-4").group(), m.target_block(REAL_CONFIG, "DP-4").group())

    def test_reload_error_restores_previous_file(self):
        def ipc(*args):
            if args == ("configerrors",):
                return "Simulated configuration rejection"
            return self.ipc(*args)
        with patch.object(m, "hypr", ipc):
            with self.assertRaisesRegex(RuntimeError, "restored"):
                m.apply_settings("DP-4", m.Settings(saturation=120))
        self.assertEqual(self.config.read_text(), REAL_CONFIG)

    def test_layout_change_restores_previous_file(self):
        reads = 0
        def ipc(*args):
            nonlocal reads
            if args == ("-j", "monitors"):
                reads += 1
                altered = [dict(item) for item in MONITORS]
                if reads > 1:
                    altered[0]["scale"] = 2
                return json.dumps(altered)
            return ""
        with patch.object(m, "hypr", ipc):
            with self.assertRaisesRegex(RuntimeError, "layout"):
                m.apply_settings("DP-4", m.Settings(saturation=120))
        self.assertEqual(self.config.read_text(), REAL_CONFIG)

    def test_existing_unmanaged_profile_is_protected(self):
        block = m.target_block(REAL_CONFIG, "DP-4")
        external = block.group()[:-2] + '    icc = "/tmp/independent-profile.icc",\n})'
        altered = REAL_CONFIG[:block.start()] + external + REAL_CONFIG[block.end():]
        self.config.write_text(altered)
        with patch.object(m, "hypr", self.ipc):
            with self.assertRaisesRegex(RuntimeError, "separately"):
                m.apply_settings("DP-4", m.Settings(saturation=120))
        self.assertEqual(self.config.read_text(), altered)

    def test_stale_window_cannot_overwrite_newer_adjustments(self):
        with patch.object(m, "hypr", self.ipc):
            m.apply_settings("DP-4", m.Settings(saturation=110))
            newer = self.config.read_text()
            with self.assertRaisesRegex(RuntimeError, "outside this window"):
                m.apply_settings("DP-4", m.Settings(saturation=125), expected=m.Settings())
        self.assertEqual(self.config.read_text(), newer)

class ColorBehavior(unittest.TestCase):
    def test_neutrals_and_color_effects(self):
        cms = m.CMS()
        gray = np.repeat(np.linspace(0, 1, 65)[:, None], 3, axis=1)
        for settings in [m.Settings(), m.Settings(saturation=180), m.Settings(hue=45)]:
            output = cms.evaluate(cms.build(settings), gray, flags=0x2400)
            self.assertLess(float(np.abs(output-gray).max()), .002)
        patches = np.array([[.7,.4,.3], [.2,.6,.5], [.3,.2,.7]])
        output = cms.evaluate(cms.build(m.Settings(saturation=0)), patches, flags=0x2400)
        self.assertLess(float(np.ptp(output, axis=1).max()), .001)
        # Confirm direction of gamma, contrast, and color temperature.
        points = np.repeat(np.array([.25,.5,.75])[:, None], 3, axis=1)
        gamma = cms.evaluate(cms.build(m.Settings(gamma=140)), points, flags=0x2400)
        self.assertTrue(np.all(gamma > points))
        contrast = cms.evaluate(cms.build(m.Settings(contrast=120)), points, flags=0x2400)
        self.assertLess(contrast[0,0], .25)
        self.assertGreater(contrast[2,0], .75)
        self.assertAlmostEqual(contrast[1,0], .5, places=3)
        warm = cms.evaluate(cms.build(m.Settings(temperature=4000)), points, flags=0x2400)
        self.assertTrue(np.all(warm[:,0] > warm[:,2]))


    def test_invalid_values_are_rejected(self):
        for kwargs in [{"gamma":0}, {"hue":181}, {"saturation":float("nan")}, {"red":True}]:
            with self.assertRaises(ValueError):
                m.Settings(**kwargs)


if __name__ == "__main__":
    unittest.main()
