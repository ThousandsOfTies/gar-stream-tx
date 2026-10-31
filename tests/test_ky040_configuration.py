import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]

class EncoderConfigurationTests(unittest.TestCase):
    def test_constructor_passes_period_after_edge_and_bias_setup(self):
        inputs = []
        class FakeGPIO:
            def __init__(self, *args):
                self.direction = "in"
                self.edge = "none"
                self.bias = "default"
                self.inverted = False
                self.fd = 100 + len(inputs)
                self.closed = False
                inputs.append(self)
            def close(self):
                self.closed = True
        configured = []
        def record(gpios, period):
            configured.append((tuple(gpios), period, tuple(gpio.edge for gpio in gpios)))
        helper = SimpleNamespace(configure_inputs=Mock(side_effect=record))
        spec = importlib.util.spec_from_file_location("ky040_configuration_test", ROOT / "ky040.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"periphery": SimpleNamespace(GPIO=FakeGPIO), "gar_gpio_debounce": helper}), patch.dict(os.environ, {"GAR_GPIO_CHIP": "/dev/gpiochip2"}):
            sys.path.insert(0, str(ROOT))
            try:
                spec.loader.exec_module(module)
                encoder = module.KY040(4, 5, 6, debounce_us=1000)
                helper.configure_inputs.assert_called_once_with((encoder.clk, encoder.dt, encoder.sw), 1000)
                self.assertNotEqual("none", configured[0][2][0])
                self.assertEqual("falling", configured[0][2][2])
                helper.configure_inputs.reset_mock()
                module.KY040(4, 5, 6, debounce_us=0)
                helper.configure_inputs.assert_not_called()
            finally:
                sys.path.remove(str(ROOT))

if __name__ == "__main__":
    unittest.main()
