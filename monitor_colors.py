#!/usr/bin/env python3
"""Monitor Colors: independent creative color adjustments for Hyprland."""
from __future__ import annotations
import argparse
import ctypes as C
import ctypes.util
from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import math
import sys
import fcntl
import os
import re
import shutil
import subprocess
import tempfile
import time

import numpy as np

HOME = Path.home()
PROFILE_DIR = HOME / ".local/share/color/icc/monitor-colors"
CONFIG = HOME / ".config/hypr/config/monitors.lua"
MARKER = "-- monitor-colors managed"
LEGACY_MARKER = "-- monitor-saturation managed"
MONITOR_MAP = HOME / ".config/monitor-colors/monitors.json"
MODELS = {}

def load_monitor_map(path):
    """Read an explicit connector-to-model mapping without changing displays."""
    try:
        models = json.loads(Path(path).read_text())
    except FileNotFoundError as error:
        raise ValueError(
            f"Create {path} with connector-to-model pairs first. "
            "Use --list-monitors to inspect connected displays."
        ) from error
    if not isinstance(models, dict) or not models:
        raise ValueError("Monitor mapping must be a nonempty JSON object")
    if any(not isinstance(k, str) or not k.strip() or
           not isinstance(v, str) or not v.strip() for k, v in models.items()):
        raise ValueError("Each monitor connector and model must be a nonempty string")
    return models

# Internal slider units are integer percentages, degrees, or kelvin.
RANGES = {
    "saturation": (0, 200, 100), "contrast": (50, 150, 100),
    "gamma": (50, 200, 100), "hue": (-180, 180, 0),
    "temperature": (2500, 10000, 6500),
    "red": (50, 150, 100), "green": (50, 150, 100), "blue": (50, 150, 100),
}
ALGORITHM = 1

@dataclass(frozen=True)
class Settings:
    saturation: int = 100
    contrast: int = 100
    gamma: int = 100
    hue: int = 0
    temperature: int = 6500
    red: int = 100
    green: int = 100
    blue: int = 100

    def __post_init__(self):
        for name, value in asdict(self).items():
            lo, hi, _ = RANGES[name]
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f"{name} must be an integer from {lo} to {hi}")

    def is_default(self):
        return self == Settings()

def sig(value):
    return int.from_bytes(value.encode("ascii"), "big")

class XYZ(C.Structure):
    _fields_ = [("X", C.c_double), ("Y", C.c_double), ("Z", C.c_double)]

def decode(rgb):
    rgb = np.clip(rgb, 0, 1)
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055)**2.4)

def encode(linear):
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear**(1/2.4) - 0.055)

def blackbody(kelvin):
    t = kelvin / 100.0
    r = 255 if t <= 66 else 329.698727446 * (t - 60)**-0.1332047592
    g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * (t - 60)**-0.0755148492
    b = 255 if t >= 66 else (0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307)
    return np.clip(np.array([r, g, b]) / 255, 0, 1)

def adjustment_matrix(settings):
    weights = np.array([0.2126390059, 0.7151686788, 0.0721923154])
    weights /= weights.sum()
    saturation = settings.saturation/100*np.eye(3) + (1-settings.saturation/100)*np.outer(np.ones(3), weights)
    # Rotate the luminance-free opponent plane, preserving the neutral axis.
    basis = np.array([[1, 1, 0], [1, -weights[0]/weights[1], -weights[2]/weights[1]], [1, 0, 1]], dtype=float)
    a = math.radians(settings.hue)
    rotation = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    hue = basis @ rotation @ np.linalg.inv(basis)
    temperature = blackbody(settings.temperature) / blackbody(6500)
    temperature /= temperature.max()
    gains = decode(temperature) * np.array([settings.red, settings.green, settings.blue])/100
    return np.diag(gains) @ hue @ saturation

def tone(linear, settings):
    encoded = encode(linear)
    encoded = encoded**(100/settings.gamma)
    return np.clip((encoded - 0.5)*(settings.contrast/100) + 0.5, 0, 1)

def expected_transform(rgb, settings):
    return tone(decode(rgb) @ adjustment_matrix(settings).T, settings)

class CMS:
    """Minimal typed binding to the already-installed Little CMS library."""
    def __init__(self):
        library = ctypes.util.find_library("lcms2")
        if not library:
            raise RuntimeError("Little CMS is not available")
        self.lib = C.CDLL(library)
        def bind(name, result, *arguments):
            function = getattr(self.lib, name)
            function.restype, function.argtypes = result, list(arguments)
            return function
        P, U, D, I = C.c_void_p, C.c_uint32, C.c_double, C.c_int
        self.srgb = bind("cmsCreate_sRGBProfile", P)
        self.read = bind("cmsReadTag", P, P, U)
        self.close = bind("cmsCloseProfile", I, P)
        self.save = bind("cmsSaveProfileToMem", I, P, P, C.POINTER(U))
        self.open = bind("cmsOpenProfileFromMem", P, P, U)
        self.version = bind("cmsSetProfileVersion", None, P, D)
        self.intent = bind("cmsSetHeaderRenderingIntent", None, P, U)
        self.transform = bind("cmsCreateTransform", P, P, U, P, U, U, U)
        self.run = bind("cmsDoTransform", None, P, P, P, U)
        self.delete = bind("cmsDeleteTransform", None, P)
        self.mlu = bind("cmsMLUalloc", P, P, U)
        self.text = bind("cmsMLUsetASCII", I, P, C.c_char_p, C.c_char_p, C.c_char_p)
        self.write = bind("cmsWriteTag", I, P, U, P)
        self.free_mlu = bind("cmsMLUfree", None, P)
        self.pipeline = bind("cmsPipelineAlloc", P, P, U, U)
        self.free_pipeline = bind("cmsPipelineFree", None, P)
        self.insert = bind("cmsPipelineInsertStage", I, P, I, P)
        self.matrix_stage = bind("cmsStageAllocMatrix", P, P, U, U, C.POINTER(D), C.POINTER(D))
        self.curve_stage = bind("cmsStageAllocToneCurves", P, P, U, C.POINTER(P))
        self.gamma_curve = bind("cmsBuildGamma", P, P, D)
        self.table_curve = bind("cmsBuildTabulatedToneCurveFloat", P, P, U, C.POINTER(C.c_float))
        self.free_curve = bind("cmsFreeToneCurve", None, P)
        self.free_stage = bind("cmsStageFree", None, P)

    def add(self, pipeline, stage):
        if not stage:
            raise RuntimeError("Cannot allocate ICC stage")
        if not self.insert(pipeline, 1, stage):
            self.free_stage(stage)
            raise RuntimeError("Cannot append ICC stage")

    def describe(self, profile, text):
        mlu = self.mlu(None, 1)
        if not mlu:
            raise RuntimeError("Cannot allocate ICC description")
        try:
            if not self.text(mlu, b"en", b"US", text.encode("ascii")) or not self.write(profile, sig("desc"), mlu):
                raise RuntimeError("Cannot write ICC description")
        finally:
            self.free_mlu(mlu)

    def build(self, settings):
        # Use a standard sRGB forward characterization and a custom floating-point B2D output
        # transform. Hyprland converts linear sRGB to this display via B2D1.
        profile = self.srgb()
        if not profile:
            raise RuntimeError("Cannot create sRGB profile")
        pipeline = None
        try:
            colorants = []
            for tag in ("rXYZ", "gXYZ", "bXYZ"):
                pointer = self.read(profile, sig(tag))
                if not pointer:
                    raise RuntimeError("Missing sRGB matrix")
                value = C.cast(pointer, C.POINTER(XYZ)).contents
                colorants.append([value.X, value.Y, value.Z])
            matrix = np.array(colorants).T
            # ICC floating-point B2D pipelines use actual XYZ PCS units.
            output_matrix = adjustment_matrix(settings) @ np.linalg.inv(matrix)
            output_matrix = np.ascontiguousarray(output_matrix, dtype=np.float64)
            pipeline = self.pipeline(None, 3, 3)
            if not pipeline:
                raise RuntimeError("Cannot allocate ICC pipeline")
            identity = self.table_curve(None, 2, (C.c_float*2)(0, 1))
            if not identity:
                raise RuntimeError("Cannot allocate identity tone curve")
            try:
                self.add(pipeline, self.curve_stage(None, 3, (C.c_void_p*3)(identity, identity, identity)))
            finally:
                self.free_curve(identity)
            self.add(pipeline, self.matrix_stage(None, 3, 3, output_matrix.ctypes.data_as(C.POINTER(C.c_double)), None))
            samples = np.linspace(0, 1, 16385)
            values = np.ascontiguousarray(tone(samples, settings), dtype=np.float32)
            curve = self.table_curve(None, len(values), values.ctypes.data_as(C.POINTER(C.c_float)))
            if not curve:
                raise RuntimeError("Cannot allocate output tone curve")
            try:
                self.add(pipeline, self.curve_stage(None, 3, (C.c_void_p*3)(curve, curve, curve)))
            finally:
                self.free_curve(curve)
            self.version(profile, 4.3)
            self.intent(profile, 1)
            for tag in ("B2D0", "B2D1", "B2D2", "B2D3"):
                if not self.write(profile, sig(tag), pipeline):
                    raise RuntimeError("Cannot write ICC output transform")
            self.describe(profile, "Monitor Colors creative adjustment (not measured calibration)")
            size = C.c_uint32()
            if not self.save(profile, None, C.byref(size)):
                raise RuntimeError("Cannot size ICC profile")
            data = C.create_string_buffer(size.value)
            if not self.save(profile, data, C.byref(size)):
                raise RuntimeError("Cannot serialize ICC profile")
            return data.raw[:size.value]
        finally:
            if pipeline:
                self.free_pipeline(pipeline)
            self.close(profile)

    def evaluate(self, data, inputs, flags=0x2140, linear=False):
        memory = C.create_string_buffer(data)
        target = self.open(memory, len(data))
        source = self.srgb()
        transform = None
        if not target or not source:
            if target: self.close(target)
            if source: self.close(source)
            raise RuntimeError("Cannot reopen ICC profile")
        try:
            if linear:
                # Match Hyprland's linear-sRGB source profile.
                gamma = self.gamma_curve(None, 1)
                try:
                    for tag in ("rTRC", "gTRC", "bTRC"):
                        if not self.write(source, sig(tag), gamma):
                            raise RuntimeError("Cannot create linear reference")
                finally:
                    self.free_curve(gamma)
            fmt = (1 << 22) | (4 << 16) | (3 << 3)
            transform = self.transform(source, fmt, target, fmt, 1, flags)
            if not transform:
                raise RuntimeError("Cannot construct ICC transform")
            inputs = np.ascontiguousarray(inputs, dtype=np.float64)
            output = np.empty_like(inputs)
            self.run(transform, inputs.ctypes.data, output.ctypes.data, len(inputs))
            if not np.isfinite(output).all():
                raise RuntimeError("ICC transform returned invalid colors")
            return output
        finally:
            if transform: self.delete(transform)
            self.close(source)
            self.close(target)

    def checked_profile(self, settings):
        data = self.build(settings)
        gray = np.repeat(np.linspace(0, 1, 65)[:, None], 3, axis=1)
        patches = np.random.default_rng(41).uniform(0.05, 0.95, (512, 3))
        inputs = np.vstack([gray, patches])
        expected = expected_transform(inputs, settings)
        output = self.evaluate(data, inputs)
        error = float(np.max(np.abs(output-expected)))
        # Float ICC stages preserve color and avoid BPC fitting creative effects.
        if error > 0.006:
            raise RuntimeError(f"ICC accuracy check failed (maximum error {error:.5f})")
        # Also exercise the exact source colorspace and LCMS flags Hyprland uses.
        runtime = self.evaluate(data, decode(inputs), flags=0x2400, linear=True)
        runtime_error = float(np.max(np.abs(runtime-expected)))
        if runtime_error > 0.006:
            raise RuntimeError(f"Hyprland ICC transform check failed ({runtime_error:.5f})")
        return data, max(error, runtime_error)

def self_test():
    cms = CMS()
    cases = [
        Settings(), Settings(saturation=130), Settings(saturation=0), Settings(saturation=200),
        Settings(contrast=125), Settings(contrast=75), Settings(gamma=150), Settings(gamma=70),
        Settings(hue=30), Settings(hue=-90), Settings(temperature=4000),
        Settings(temperature=9000), Settings(red=80, green=110, blue=95),
        Settings(saturation=135, contrast=115, gamma=110, hue=5, temperature=6000),
        Settings(saturation=200, contrast=150, gamma=200, hue=180, temperature=2500, red=150),
    ]
    worst = 0
    for settings in cases:
        _, error = cms.checked_profile(settings)
        print(f"PASS {json.dumps(asdict(settings), separators=(',', ':'))}: {error:.6f}")
        worst = max(worst, error)
    print(f"Verified {len(cases)} profiles through saved ICC transforms; maximum RGB error {worst:.6f}")

def hypr(*arguments):
    result = subprocess.run(["hyprctl", *arguments], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError((result.stderr + result.stdout).strip() or "Hyprland command failed")
    return result.stdout.strip()

def target_block(text, connector):
    matches = [m for m in re.finditer(r"^hl\.monitor\(\{(?P<body>.*?)^\}\)", text, re.M | re.S)
               if re.search(r"""\boutput\s*=\s*["']""" + re.escape(connector) + r"""["']""", m["body"])]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one monitor configuration for {connector}; nothing changed")
    return matches[0]

def profile_identity(connector, settings):
    return {"algorithm": ALGORITHM, "monitor": connector, "settings": asdict(settings)}

def profile_path(connector, settings):
    canonical = json.dumps(profile_identity(connector, settings), sort_keys=True).encode()
    digest = hashlib.sha256(canonical).hexdigest()[:16]
    return PROFILE_DIR / f"{connector}-{digest}.icc"

def icc_line(block):
    lines = [line for line in block.splitlines() if re.match(r"\s*icc\s*=", line)]
    if len(lines) > 1:
        raise RuntimeError("Multiple color profiles are configured for this monitor")
    return lines[0] if lines else None

def read_settings(text, connector):
    block = target_block(text, connector).group()
    line = icc_line(block)
    if line is None:
        return Settings()
    if LEGACY_MARKER in line:
        match = re.search(r"/dp2-saturation/saturation-(\d+)\.icc", line)
        if connector != "DP-2" or not match:
            raise RuntimeError("The previous saturation setting could not be read")
        return Settings(saturation=int(match[1]))
    if MARKER not in line:
        raise RuntimeError(f"{connector} already has a separately configured color profile; it was left untouched")
    path_match = re.search(r"""icc\s*=\s*(".*?")\s*,""", line)
    if not path_match:
        raise RuntimeError("The saved color-profile path could not be read")
    path = Path(json.loads(path_match[1]))
    if path.parent != PROFILE_DIR:
        raise RuntimeError("The saved profile is outside Monitor Colors' directory")
    metadata = json.loads(path.with_suffix(".json").read_text())
    settings = Settings(**metadata["settings"])
    if metadata.get("algorithm") != ALGORITHM or metadata.get("monitor") != connector:
        raise RuntimeError("Color-profile metadata does not match this monitor")
    if path != profile_path(connector, settings):
        raise RuntimeError("Color-profile metadata does not match its filename")
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata.get("sha256"):
        raise RuntimeError("The saved color profile has changed; reload or restore it before applying")
    return settings

def updated_config(text, connector, profile):
    match = target_block(text, connector)
    block = match.group()
    line = icc_line(block)
    if line and MARKER not in line and LEGACY_MARKER not in line:
        raise RuntimeError(f"{connector} has an unmanaged color profile; nothing changed")
    lines = [line for line in block.splitlines() if MARKER not in line and LEGACY_MARKER not in line]
    block = "\n".join(lines)
    if profile is not None:
        block = block[:-2] + f"    icc       = {json.dumps(str(profile))}, {MARKER}\n" + "})"
    return text[:match.start()] + block + text[match.end():]

def atomic_bytes(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def save_profile(connector, settings):
    path = profile_path(connector, settings)
    if path.exists() or path.with_suffix(".json").exists():
        metadata = json.loads(path.with_suffix(".json").read_text())
        if any(metadata.get(k) != v for k, v in profile_identity(connector, settings).items()):
            raise RuntimeError("Cached color-profile metadata is inconsistent")
        if hashlib.sha256(path.read_bytes()).hexdigest() != metadata.get("sha256"):
            raise RuntimeError("Cached color profile has changed")
        return path
    data, _ = CMS().checked_profile(settings)
    metadata = profile_identity(connector, settings)
    metadata["sha256"] = hashlib.sha256(data).hexdigest()
    atomic_bytes(path, data)
    atomic_bytes(path.with_suffix(".json"), (json.dumps(metadata, indent=2) + "\n").encode())
    return path

def geometry(monitors):
    keys = ("name", "width", "height", "refreshRate", "x", "y", "scale", "transform", "disabled")
    return sorted(tuple(m.get(key) for key in keys) for m in monitors)

def apply_settings(connector, settings, expected=None):
    if connector not in MODELS:
        raise RuntimeError("This monitor has not been configured for Monitor Colors")
    lock_dir = HOME / ".local/state/monitor-colors"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "apply.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        monitors = json.loads(hypr("-j", "monitors"))
        target = next((m for m in monitors if m["name"] == connector), None)
        if not target or target.get("model") != MODELS[connector]:
            raise RuntimeError(f"The expected monitor is not connected on {connector}")
        old = CONFIG.read_text()
        original = read_settings(old, connector)
        if expected is not None and original != expected:
            raise RuntimeError("These settings changed outside this window. Reopen Monitor Colors to load them.")
        profile = None if settings.is_default() else save_profile(connector, settings)
        new = updated_config(old, connector, profile)
        if new == old:
            return
        backup = CONFIG.with_name("monitors.lua.before-monitor-colors")
        if not backup.exists():
            shutil.copy2(CONFIG, backup)
        mode = CONFIG.stat().st_mode & 0o777
        if CONFIG.read_text() != old:
            raise RuntimeError("The monitor configuration changed during preparation; please try again")
        atomic_bytes(CONFIG, new.encode(), mode)
        try:
            hypr("reload")
            time.sleep(0.5)
            errors = hypr("configerrors")
            if errors:
                raise RuntimeError(errors)
            after = json.loads(hypr("-j", "monitors"))
            if geometry(monitors) != geometry(after):
                raise RuntimeError("The monitor layout changed unexpectedly")
        except Exception as error:
            atomic_bytes(CONFIG, old.encode(), mode)
            try:
                hypr("reload")
            except Exception as rollback:
                raise RuntimeError(f"{error}\nThe original configuration was restored on disk; reload reported: {rollback}") from error
            raise RuntimeError(f"{error}\nYour previous color settings were restored.") from error

def create_window(monitors):
    from PySide6 import QtCore, QtGui, QtWidgets as W

    class ColorStrip(W.QWidget):
        def __init__(self):
            super().__init__()
            self.setFixedHeight(36)
            self.setToolTip("Color and gray reference strips for judging your display settings.")
        def paintEvent(self, event):
            painter = QtGui.QPainter(self)
            gradient = QtGui.QLinearGradient(0, 0, self.width(), 0)
            for point, color in [(0, "#ef4444"), (.17, "#facc15"), (.34, "#22c55e"), (.5, "#22d3ee"), (.67, "#3b82f6"), (.84, "#c026d3"), (1, "#ef4444")]:
                gradient.setColorAt(point, QtGui.QColor(color))
            painter.fillRect(0, 0, self.width(), 16, gradient)
            gray = QtGui.QLinearGradient(0, 0, self.width(), 0)
            gray.setColorAt(0, QtGui.QColor("black"))
            gray.setColorAt(1, QtGui.QColor("white"))
            painter.fillRect(0, 20, self.width(), 16, gray)

    class Window(W.QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Monitor Colors")
            self.setMinimumWidth(620)
            self.resize(660, 710)
            self.setObjectName("monitorColors")
            self.loading = True
            self.current = None
            self.saved, self.drafts = {}, {}
            text = CONFIG.read_text()
            self.connected = {m["name"]: m for m in monitors if m["name"] in MODELS and m.get("model") == MODELS[m["name"]]}
            if not self.connected:
                raise RuntimeError("No configured monitor matches a connected connector and model")
            for connector in self.connected:
                self.saved[connector] = read_settings(text, connector)
                self.drafts[connector] = self.saved[connector]
            outer = W.QVBoxLayout(self)
            outer.setContentsMargins(26, 24, 26, 24)
            outer.setSpacing(15)
            title = W.QLabel("Monitor Colors")
            title.setObjectName("title")
            outer.addWidget(title)
            subtitle = W.QLabel("Independent color controls for each display")
            subtitle.setObjectName("subtitle")
            outer.addWidget(subtitle)
            self.selector = W.QComboBox()
            self.selector.setMinimumHeight(38)
            for connector in MODELS:
                if connector in self.connected:
                    label = MODELS[connector]
                    self.selector.addItem(f"{label}  ({connector})", connector)
            outer.addWidget(self.selector)
            self.controls = {}
            labels = {"saturation": "Saturation", "contrast": "Contrast", "gamma": "Gamma",
                      "hue": "Hue", "temperature": "Temperature", "red": "Red balance",
                      "green": "Green balance", "blue": "Blue balance"}
            tips = {
                "saturation": "100% is normal; 0% removes color; higher values make colors stronger.",
                "contrast": "100% is normal. Stronger contrast can clip shadow and highlight detail.",
                "gamma": "1.00 is normal. Higher values lift midtones; lower values deepen them.",
                "hue": "0° is normal. Rotate colors while preserving the neutral gray axis.",
                "temperature": "6500 K is neutral. Lower is warmer; higher is cooler. This is an approximate color tint.",
                "red": "Balance the red channel; 100% is neutral.",
                "green": "Balance the green channel; 100% is neutral.",
                "blue": "Balance the blue channel; 100% is neutral.",
            }
            for title_text, keys in [("Color", ("saturation", "contrast", "gamma", "hue")),
                                     ("White balance", ("temperature", "red", "green", "blue"))]:
                group = W.QGroupBox(title_text)
                grid = W.QGridLayout(group)
                grid.setContentsMargins(16, 23, 16, 15)
                grid.setHorizontalSpacing(14)
                grid.setVerticalSpacing(13)
                for row, key in enumerate(keys):
                    lo, hi, default = RANGES[key]
                    label = W.QLabel(labels[key])
                    label.setMinimumWidth(105)
                    slider = W.QSlider(QtCore.Qt.Orientation.Horizontal)
                    divisor = 100 if key == "temperature" else 1
                    slider.setRange(lo//divisor, hi//divisor)
                    slider.setSingleStep(1)
                    slider.setPageStep(5)
                    if key == "gamma":
                        spin = W.QDoubleSpinBox()
                        spin.setDecimals(2)
                        spin.setRange(lo/100, hi/100)
                        spin.setSingleStep(.05)
                        spin.setValue(default/100)
                        slider.valueChanged.connect(lambda v, box=spin: box.setValue(v/100))
                        spin.valueChanged.connect(lambda v, slide=slider: slide.setValue(round(v*100)))
                    else:
                        spin = W.QSpinBox()
                        spin.setRange(lo, hi)
                        spin.setSingleStep(100 if key == "temperature" else 1)
                        spin.setSuffix(" K" if key == "temperature" else ("°" if key == "hue" else "%"))
                        spin.setValue(default)
                        slider.valueChanged.connect(lambda v, box=spin, factor=divisor: box.setValue(v*factor))
                        spin.valueChanged.connect(lambda v, slide=slider, factor=divisor: slide.setValue(round(v/factor)))
                    spin.setMinimumWidth(94)
                    for widget in (label, slider, spin):
                        widget.setToolTip(tips[key])
                    spin.valueChanged.connect(self.edited)
                    self.controls[key] = (slider, spin)
                    grid.addWidget(label, row, 0)
                    grid.addWidget(slider, row, 1)
                    grid.addWidget(spin, row, 2)
                grid.setColumnStretch(1, 1)
                outer.addWidget(group)
            outer.addWidget(ColorStrip())
            self.status = W.QLabel()
            self.status.setWordWrap(True)
            self.status.setMinimumHeight(36)
            self.status.setObjectName("status")
            outer.addWidget(self.status)
            actions = W.QHBoxLayout()
            self.reset_button = W.QPushButton("Reset defaults")
            self.reset_button.setToolTip("Prepare normal color settings for this monitor. Click Apply to save.")
            self.reset_button.clicked.connect(self.reset_selected)
            self.revert_button = W.QPushButton("Revert edits")
            self.revert_button.clicked.connect(self.revert_selected)
            self.apply_button = W.QPushButton("Apply to this monitor")
            self.apply_button.setObjectName("apply")
            self.apply_button.setDefault(True)
            self.apply_button.clicked.connect(self.apply_selected)
            actions.addWidget(self.reset_button)
            actions.addWidget(self.revert_button)
            actions.addStretch()
            actions.addWidget(self.apply_button)
            outer.addLayout(actions)
            note = W.QLabel("Changes are saved when you click Apply. Brightness stays in Noctalia.")
            note.setObjectName("subtitle")
            outer.addWidget(note)
            self.selector.currentIndexChanged.connect(self.switch_monitor)
            self.loading = False
            self.switch_monitor()

        def values(self):
            values = {key: (round(spin.value()*100) if key == "gamma" else int(spin.value()))
                      for key, (_, spin) in self.controls.items()}
            return Settings(**values)

        def set_values(self, settings):
            self.loading = True
            for key, (slider, spin) in self.controls.items():
                raw = getattr(settings, key)
                slider.setValue(round(raw/100) if key == "temperature" else raw)
                spin.setValue(raw/100 if key == "gamma" else raw)
            self.loading = False

        def switch_monitor(self, *_):
            if self.current:
                self.drafts[self.current] = self.values()
            self.current = self.selector.currentData()
            self.set_values(self.drafts[self.current])
            self.edited()

        def edited(self, *_):
            if self.loading or not self.current:
                return
            settings = self.values()
            self.drafts[self.current] = settings
            dirty = settings != self.saved[self.current]
            self.apply_button.setEnabled(dirty)
            self.revert_button.setEnabled(dirty)
            self.status.setText(f"Unsaved changes for {self.current} — click Apply when ready." if dirty else
                                f"Saved settings for {self.current}. The other monitor is controlled separately.")
            self.status.setStyleSheet("color: #d3b6ff;" if dirty else "color: #9da7bd;")

        def reset_selected(self):
            self.set_values(Settings())
            self.edited()

        def revert_selected(self):
            self.set_values(self.saved[self.current])
            self.edited()

        def apply_selected(self):
            settings = self.values()
            W.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
            error = None
            try:
                apply_settings(self.current, settings, expected=self.saved[self.current])
                self.saved[self.current] = settings
                self.drafts[self.current] = settings
            except Exception as exc:
                error = str(exc)
            finally:
                W.QApplication.restoreOverrideCursor()
            self.edited()
            if error:
                W.QMessageBox.critical(self, "Color adjustment failed", error)
            else:
                self.status.setText(f"Saved and applied to {self.current}.")
        def closeEvent(self, event):
            if any(self.drafts[k] != self.saved[k] for k in self.saved):
                response = W.QMessageBox.question(self, "Unsaved color changes",
                    "Discard the color adjustments you have not applied?",
                    W.QMessageBox.StandardButton.Discard | W.QMessageBox.StandardButton.Cancel,
                    W.QMessageBox.StandardButton.Cancel)
                if response != W.QMessageBox.StandardButton.Discard:
                    event.ignore()
                    return
            event.accept()

    return Window()

def configure_theme(app):
    from PySide6 import QtGui
    app.setStyle("Fusion")
    palette = QtGui.QPalette()
    colors = {
        "Window": "#171b26", "WindowText": "#e5e9f3", "Base": "#202636",
        "AlternateBase": "#242b3c", "Text": "#e5e9f3", "Button": "#2b3245",
        "ButtonText": "#e5e9f3", "Highlight": "#a78bfa", "HighlightedText": "#171b26",
        "ToolTipBase": "#2b3245", "ToolTipText": "#e5e9f3",
    }
    for role, color in colors.items():
        palette.setColor(getattr(QtGui.QPalette.ColorRole, role), QtGui.QColor(color))
    palette.setColor(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor("#70798e"))
    app.setPalette(palette)
    app.setStyleSheet("""
        QWidget { font-size: 13px; }
        QLabel#title { font-size: 24px; font-weight: 700; }
        QLabel#subtitle { color: #9da7bd; font-size: 12px; }
        QGroupBox { background: #202636; border: 1px solid #30384c; border-radius: 10px; margin-top: 9px; font-weight: 600; }
        QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #c4b5fd; }
        QSpinBox, QDoubleSpinBox, QComboBox { padding: 6px; border: 1px solid #3b455e; border-radius: 6px; }
        QSlider::groove:horizontal { height: 5px; background: #3b455e; border-radius: 2px; }
        QSlider::sub-page:horizontal { background: #a78bfa; border-radius: 2px; }
        QSlider::handle:horizontal { background: #d4c6ff; border: 1px solid #a78bfa; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; }
        QPushButton { padding: 9px 12px; border: 1px solid #3b455e; border-radius: 6px; background: #2b3245; }
        QPushButton:hover { background: #36405a; }
        QPushButton#apply { background: #a78bfa; color: #171b26; font-weight: 600; }
        QPushButton#apply:disabled { background: #343b51; color: #8992a6; }
    """)

def gui():
    from PySide6 import QtWidgets as W
    app = W.QApplication(sys.argv[:1])
    app.setApplicationName("Monitor Colors")
    app.setDesktopFileName("monitor-saturation")
    configure_theme(app)
    try:
        window = create_window(json.loads(hypr("-j", "monitors")))
    except Exception as error:
        W.QMessageBox.critical(None, "Monitor Colors", str(error))
        return 1
    window.show()
    return app.exec()

def main():
    global MODELS, CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--monitor", help="connector from your monitor mapping")
    parser.add_argument("--monitor-map", type=Path, default=MONITOR_MAP,
                        help="connector-to-model JSON file")
    parser.add_argument("--config", type=Path, default=CONFIG,
                        help="Lua file containing the existing connector-based monitor rules")
    parser.add_argument("--list-monitors", action="store_true",
                        help="print connected connector-to-model pairs without changing settings")
    parser.add_argument("--apply", action="store_true", help="save supplied controls for the selected monitor")
    parser.add_argument("--show", action="store_true", help="print saved settings")
    for key in RANGES:
        parser.add_argument("--" + key, type=int)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.list_monitors:
        monitors = json.loads(hypr("-j", "monitors"))
        print(json.dumps({m["name"]: m["model"] for m in monitors}, indent=2))
        return 0
    MODELS = load_monitor_map(args.monitor_map)
    CONFIG = args.config.expanduser()
    if args.monitor is None:
        args.monitor = next(iter(MODELS))
    if args.monitor not in MODELS:
        parser.error("--monitor must name a connector in the monitor mapping")
    if args.show:
        print(json.dumps({k: asdict(read_settings(CONFIG.read_text(), k)) for k in MODELS}, indent=2))
        return 0
    values = {key: getattr(args, key) for key in RANGES if getattr(args, key) is not None}
    if args.apply:
        settings = asdict(read_settings(CONFIG.read_text(), args.monitor))
        settings.update(values)
        apply_settings(args.monitor, Settings(**settings))
        print(f"Saved colors for {args.monitor}: " + json.dumps(settings))
        return 0
    if values:
        parser.error("Use --apply to save command-line adjustments")
    return gui()

def entrypoint():
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Monitor Colors: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
