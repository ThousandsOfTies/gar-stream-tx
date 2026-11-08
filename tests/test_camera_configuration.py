import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class CameraConfigurationTests(unittest.TestCase):
    def load(self, values):
        spec = importlib.util.spec_from_file_location('camera_configuration_test', ROOT / 'camera_tx.py')
        module = importlib.util.module_from_spec(spec)
        fake_gi = SimpleNamespace(require_version=lambda *args: None)
        with patch.dict(sys.modules, {'gi': fake_gi, 'gi.repository': SimpleNamespace(GLib=None, Gst=None),
                                      'ky040': SimpleNamespace(KY040=None)}), patch.dict(os.environ, values, clear=True):
            sys.path.insert(0, str(ROOT))
            try:
                spec.loader.exec_module(module)
            finally:
                sys.path.remove(str(ROOT))
        return module.CONFIG

    def test_default_spi_settings_remain_compatible(self):
        config = self.load({})
        self.assertEqual((0, 0, 24000000), (config['spi_bus'], config['spi_device'], config['spi_max_hz']))

    def test_product_can_select_spi_bus_chip_select_and_speed(self):
        config = self.load({'GAR_LCD_SPI_BUS': '1', 'GAR_LCD_SPI_DEVICE': '2', 'GAR_LCD_SPI_MAX_HZ': '12000000'})
        self.assertEqual((1, 2, 12000000), (config['spi_bus'], config['spi_device'], config['spi_max_hz']))


if __name__ == '__main__':
    unittest.main()
