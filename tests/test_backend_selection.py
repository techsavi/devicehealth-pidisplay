"""Regression checks for preserving HUGA and selecting the new KeDei backend.

Run from pi_display with:
    python3 -m unittest discover -s tests -p 'test_backend_selection.py' -v

The display constructors and watcher constructors are replaced at their public
boundary; these tests neither load the native helper nor open GPIO/SPI/input.
"""
import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, sentinel

from PIL import Image

from wifi_meter import display, touch


def fake_kedei():
    module = types.ModuleType("wifi_meter.kedei")
    module.available = Mock(return_value=True)
    module.KeDeiDisplay = Mock(return_value=sentinel.kedei_display)
    module.XPT2046Watcher = Mock()
    return module


class DisplaySelectionTests(unittest.TestCase):
    def test_auto_keeps_existing_huga_framebuffer_when_kedei_is_available(self):
        kedei = fake_kedei()
        with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                patch.object(display.Path, "exists", return_value=True), \
                patch.object(display, "Framebuffer", return_value=sentinel.framebuffer) as framebuffer:
            selected = display.open_display()
        self.assertIs(selected, sentinel.framebuffer)
        framebuffer.assert_called_once_with("/dev/fb0")
        kedei.available.assert_not_called()
        kedei.KeDeiDisplay.assert_not_called()

    def test_auto_uses_configured_kedei_when_framebuffer_is_missing(self):
        kedei = fake_kedei()
        with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                patch.object(display.Path, "exists", return_value=False), \
                patch.object(display, "Framebuffer") as framebuffer:
            selected = display.open_display(speed_hz=2_000_000)
        self.assertIs(selected, sentinel.kedei_display)
        framebuffer.assert_not_called()
        kedei.available.assert_called_once_with()
        kedei.KeDeiDisplay.assert_called_once_with(speed_hz=2_000_000)

    def test_auto_reports_no_display_when_neither_backend_is_available(self):
        kedei = fake_kedei()
        kedei.available.return_value = False
        with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                patch.object(display.Path, "exists", return_value=False), \
                patch.object(display, "Framebuffer") as framebuffer:
            with self.assertRaisesRegex(RuntimeError, "No framebuffer"):
                display.open_display()
        framebuffer.assert_not_called()
        kedei.KeDeiDisplay.assert_not_called()

    def test_explicit_framebuffer_preserves_requested_device(self):
        kedei = fake_kedei()
        with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                patch.object(display.Path, "exists", return_value=False), \
                patch.object(display, "Framebuffer", return_value=sentinel.framebuffer) as framebuffer:
            selected = display.open_display("/dev/fb1", backend="framebuffer")
        self.assertIs(selected, sentinel.framebuffer)
        framebuffer.assert_called_once_with("/dev/fb1")
        kedei.available.assert_not_called()
        kedei.KeDeiDisplay.assert_not_called()

    def test_explicit_kedei_overrides_existing_framebuffer(self):
        kedei = fake_kedei()
        with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                patch.object(display.Path, "exists", return_value=True), \
                patch.object(display, "Framebuffer") as framebuffer:
            selected = display.open_display(backend="kedei", speed_hz=4_000_000)
        self.assertIs(selected, sentinel.kedei_display)
        framebuffer.assert_not_called()
        kedei.available.assert_not_called()
        kedei.KeDeiDisplay.assert_called_once_with(speed_hz=4_000_000)

    def test_existing_framebuffer_failure_does_not_silently_switch_hardware(self):
        kedei = fake_kedei()
        with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                patch.object(display.Path, "exists", return_value=True), \
                patch.object(display, "Framebuffer", side_effect=RuntimeError("expected 16 bpp")):
            with self.assertRaisesRegex(RuntimeError, "expected 16 bpp"):
                display.open_display()
        kedei.available.assert_not_called()
        kedei.KeDeiDisplay.assert_not_called()


class TouchSelectionTests(unittest.TestCase):
    def test_huga_uses_discovered_native_input_device(self):
        callback = Mock()
        old_display = Mock(spec=["size", "width", "height"])
        watcher = Mock()
        kedei = fake_kedei()
        with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                patch.object(touch, "find_device", return_value="/dev/input/event7") as find_device, \
                patch.object(touch, "TapWatcher", return_value=watcher) as native_watcher:
            selected = touch.start_watcher(old_display, callback)
        self.assertIs(selected, watcher)
        find_device.assert_called_once_with()
        native_watcher.assert_called_once_with("/dev/input/event7", callback)
        watcher.start.assert_called_once_with()
        kedei.XPT2046Watcher.assert_not_called()

    def test_huga_preserves_explicit_touch_device(self):
        callback = Mock()
        old_display = Mock(spec=["size", "width", "height"])
        watcher = Mock()
        with patch.object(touch, "find_device") as find_device, \
                patch.object(touch, "TapWatcher", return_value=watcher) as native_watcher:
            selected = touch.start_watcher(old_display, callback, "/dev/input/event2")
        self.assertIs(selected, watcher)
        find_device.assert_not_called()
        native_watcher.assert_called_once_with("/dev/input/event2", callback)
        watcher.start.assert_called_once_with()

    def test_huga_without_touch_device_still_runs_without_watcher(self):
        old_display = Mock(spec=["size", "width", "height"])
        with patch.object(touch, "find_device", return_value=None), \
                patch.object(touch, "TapWatcher") as native_watcher:
            self.assertIsNone(touch.start_watcher(old_display, Mock()))
        native_watcher.assert_not_called()

    def test_kedei_uses_display_owner_instead_of_opening_native_input(self):
        callback = Mock()
        shared_display = Mock(spec=["read_touch", "size", "width", "height"])
        watcher = Mock()
        kedei = fake_kedei()
        kedei.XPT2046Watcher.return_value = watcher
        with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                patch.object(touch, "find_device") as find_device, \
                patch.object(touch, "TapWatcher") as native_watcher:
            selected = touch.start_watcher(shared_display, callback)
        self.assertIs(selected, watcher)
        find_device.assert_not_called()
        native_watcher.assert_not_called()
        kedei.XPT2046Watcher.assert_called_once_with(shared_display, callback)
        watcher.start.assert_called_once_with()


class PreviewSelectionTests(unittest.TestCase):
    def test_demo_preview_writes_png_without_opening_either_hardware_backend(self):
        from wifi_meter import __main__ as application

        kedei = fake_kedei()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "preview.png"
            with patch.dict(sys.modules, {"wifi_meter.kedei": kedei}), \
                    patch.object(sys, "argv", ["wifi_meter", "--preview", str(destination), "--demo"]), \
                    patch.object(display, "open_display", side_effect=AssertionError("hardware opened")) as open_display, \
                    patch.object(display, "Framebuffer") as framebuffer, \
                    patch.object(touch, "start_watcher") as start_watcher, \
                    patch.object(application.scanner, "scan") as scan, \
                    contextlib.redirect_stdout(io.StringIO()):
                application.main()
            with Image.open(destination) as preview:
                self.assertEqual(preview.format, "PNG")
                self.assertEqual(preview.size, (480, 320))
                self.assertGreater(len(preview.getcolors(480 * 320)), 1)
        open_display.assert_not_called()
        framebuffer.assert_not_called()
        kedei.KeDeiDisplay.assert_not_called()
        kedei.available.assert_not_called()
        start_watcher.assert_not_called()
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
