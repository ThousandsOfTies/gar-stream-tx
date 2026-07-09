"""gar-stream-tx: OV3660 USB UVC camera -> MJPEG/RTP/UDP, with an optional
local ILI9341 preview and KY-040 control of the network output's size and
framerate.

  rotate -> cycle through SIZE_PRESETS  (resolution sent to the RX side)
  press  -> cycle through RATE_PRESETS  (framerate sent to the RX side)

Pipeline shape (see README.md "Architecture"):

    v4l2src (native 2048x1536@15fps) -> jpegdec -> tee
      branch A: videoscale/videorate -> capsfilter(out_caps) -> jpegenc
                -> rtpjpegpay -> udpsink  (to gar-stream-rx)
      branch B (only if local_display is on): videoconvert/videoscale
                -> RGB565 -> appsink -> ILI9341 over SPI

Capture always happens at the camera's native mode; the tee means a
size/rate change only has to touch the `out_caps` capsfilter's `caps`
property on branch A - GStreamer renegotiates videoscale/videorate/jpegenc
downstream of that live, so there's no dropped connection / process
restart on every KY-040 turn (unlike an early version of this script that
respawned gst-launch-1.0 as a subprocess).

Requires PyGObject + the GStreamer 1.0 typelib (system packages, not pip -
see README.md "Dependencies"). Uses the same ili9341.py/ky040.py as
gar-stream-rx.
"""
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from ky040 import KY040  # noqa: E402

CONFIG = {
    "enc_clk_gpio": 17,
    "enc_dt_gpio": 27,
    "enc_sw_gpio": 22,
    "camera_device": "/dev/video0",
    # Native capture mode - the OV3660 module's max (2048x1536/15fps MJPEG).
    # We always capture at this mode and downscale/downsample in software,
    # rather than asking v4l2src for the target size/rate directly, since we
    # can't be sure every SIZE_PRESETS entry is an actual discrete UVC mode
    # this camera advertises (see README's "SIZE/RATE presets" section).
    "native_width": 2048,
    "native_height": 1536,
    "native_fps": 15,
    "rx_host": None,   # <- fill in gar-stream-rx's (Lyra Plus) IP address
    "rx_port": 5600,
    "jpeg_quality": 85,

    # Optional local preview: an ILI9341 wired directly to this Pi 5 over
    # SPI (same driver/wiring as gar-stream-rx). Leave False if you don't
    # have a panel attached to the TX board.
    "local_display": False,
    "spi_bus": 0,
    "spi_device": 0,
    "spi_max_hz": 24_000_000,
    "dc_gpio": None,    # <- required if local_display is True
    "rst_gpio": None,   # <- required if local_display is True
}

# 4:3 presets - gar-stream-rx's ILI9341 panel is 320x240 (4:3), so keeping
# these 4:3 avoids letterboxing on the RX side. Index 1 (640x480, exactly 2x
# the panel resolution) is the recommended default - see
# gar-stream-rx/README.md's "SPI bandwidth note".
SIZE_PRESETS = [(320, 240), (640, 480), (1024, 768), (2048, 1536)]
DEFAULT_SIZE_INDEX = 1

# Capped at the camera's native 15fps max.
RATE_PRESETS = [5, 10, 15]
DEFAULT_RATE_INDEX = 2

# The local preview always renders at the panel's native size, independent
# of the SIZE_PRESETS chosen for the network branch.
PREVIEW_WIDTH, PREVIEW_HEIGHT = 320, 240


def _build_pipeline_string(config, with_preview):
    network_branch = (
        "t. ! queue max-size-buffers=2 leaky=downstream "
        "! videoscale ! videorate "
        "! capsfilter name=out_caps "
        f"! jpegenc quality={config['jpeg_quality']} "
        "! rtpjpegpay "
        f"! udpsink host={config['rx_host']} port={config['rx_port']} sync=false"
    )
    branches = [network_branch]
    if with_preview:
        branches.append(
            "t. ! queue max-size-buffers=2 leaky=downstream "
            "! videoconvert ! videoscale "
            f"! video/x-raw,format=RGB16,width={PREVIEW_WIDTH},height={PREVIEW_HEIGHT} "
            "! appsink name=preview_sink emit-signals=true sync=false max-buffers=1 drop=true"
        )
    return (
        f"v4l2src device={config['camera_device']} "
        f"! image/jpeg,width={config['native_width']},height={config['native_height']},"
        f"framerate={config['native_fps']}/1 "
        "! jpegdec ! tee name=t "
        + " ".join(branches)
    )


class StreamTx:
    """Owns the GStreamer pipeline; size/rate changes are live caps changes
    on the network branch's capsfilter, not a pipeline restart."""

    def __init__(self, config, size_index=DEFAULT_SIZE_INDEX, rate_index=DEFAULT_RATE_INDEX,
                 display=None):
        self.config = config
        self.size_index = size_index
        self.rate_index = rate_index
        self.display = display

        self.pipeline = Gst.parse_launch(_build_pipeline_string(config, display is not None))
        self.out_caps = self.pipeline.get_by_name("out_caps")

        if display is not None:
            sink = self.pipeline.get_by_name("preview_sink")
            sink.connect("new-sample", self._on_new_sample)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._apply_output_caps()

    def _apply_output_caps(self):
        width, height = SIZE_PRESETS[self.size_index]
        fps = RATE_PRESETS[self.rate_index]
        self.out_caps.set_property(
            "caps", Gst.Caps.from_string(f"video/x-raw,width={width},height={height},framerate={fps}/1"))
        print(f"[stream_tx] output now {width}x{height}@{fps}fps -> "
              f"{self.config['rx_host']}:{self.config['rx_port']}")

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                self.display.blit(0, 0, PREVIEW_WIDTH, PREVIEW_HEIGHT, bytes(mapinfo.data))
            finally:
                buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    def _on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            # Log and keep running rather than crashing the whole TX on a
            # transient camera hiccup.
            print(f"[gst error] {err}: {debug}", file=sys.stderr)
        elif message.type == Gst.MessageType.EOS:
            print("[gst] end of stream", file=sys.stderr)

    def next_size(self, direction):
        self.size_index = (self.size_index + (1 if direction >= 0 else -1)) % len(SIZE_PRESETS)
        self._apply_output_caps()

    def next_rate(self):
        self.rate_index = (self.rate_index + 1) % len(RATE_PRESETS)
        self._apply_output_caps()

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)


def main():
    if not CONFIG["rx_host"]:
        raise SystemExit(
            "Fill in CONFIG['rx_host'] in camera_tx.py first - the IP address "
            "gar-stream-rx (Lyra Plus) is reachable at."
        )

    Gst.init(None)

    display = None
    if CONFIG["local_display"]:
        missing = [k for k in ("dc_gpio", "rst_gpio") if CONFIG[k] is None]
        if missing:
            raise SystemExit(
                "Fill in CONFIG%s for the local ILI9341 preview, or set "
                "CONFIG['local_display'] = False." % missing
            )
        from ili9341 import ILI9341  # local import: spidev only needed for this path
        display = ILI9341(
            CONFIG["spi_bus"], CONFIG["spi_device"],
            CONFIG["dc_gpio"], CONFIG["rst_gpio"],
            spi_max_hz=CONFIG["spi_max_hz"], rotation=1, bgr=True,
        )

    tx = StreamTx(CONFIG, display=display)
    tx.start()

    encoder = KY040(
        CONFIG["enc_clk_gpio"], CONFIG["enc_dt_gpio"], CONFIG["enc_sw_gpio"],
        on_rotate=lambda direction, counter: tx.next_size(direction),
        on_press=lambda: tx.next_rate(),
    )
    encoder.start()

    loop = GLib.MainLoop()
    print("Running. Rotate KY-040 to change size, press to change rate. Ctrl+C to quit.")
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        encoder.stop()
        tx.stop()
        if display is not None:
            display.close()


if __name__ == "__main__":
    main()

