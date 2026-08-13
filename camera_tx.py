"""gar-stream-tx: OV3660 USB UVC camera -> MJPEG/RTP/UDP.

The KY-040 controls the same on-screen menu on a physical TX device and in
the GAR panel.  It configures the outgoing stream rather than introducing a
simulation-only control path:

  press             -> open a menu / enter or confirm an item
  rotate in menu     -> select an item or change its value

The menu contains Profile, Mirror, Rotate and Overlay.  Overlay controls the
status/menu text on the TX's local ILI9341 monitor only.  The outgoing RTP
program feed is always clean; a menu is always shown locally while it is
being operated so it remains usable when the normal overlay is off.

The TX advertises itself as a Source. It has no configured RX address;
renewable requests from RX devices supply the RTP destinations at runtime.

Pipeline shape (see README.md "Architecture"):

    v4l2src (configured native MJPEG mode) -> jpegdec -> tee
      branch A: videoscale/videorate -> capsfilter(out_caps) -> jpegenc
                -> rtpjpegpay -> multiudpsink  (requested receivers only)
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

import os
import socket
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from ky040 import KY040
from metrics import MetricsWriter, measured_fps
from source_advertiser import (
    DEFAULT_DISCOVERY_PORT,
    SourceAdvertiser,
)


def _optional_env_int(name):
    value = os.environ.get(name)
    return int(value) if value else None


CONFIG = {
    "enc_clk_gpio": int(os.environ.get("GAR_ENC_CLK_GPIO", "17")),
    "enc_dt_gpio": int(os.environ.get("GAR_ENC_DT_GPIO", "27")),
    "enc_sw_gpio": int(os.environ.get("GAR_ENC_SW_GPIO", "22")),
    "camera_device": os.environ.get("GAR_CAMERA_DEVICE", "/dev/video0"),
    # Native capture mode. The current Pi camera advertises
    # 2048x1536/30fps MJPEG; override these values for another UVC device.
    # We always capture at this mode and downscale/downsample in software,
    # rather than asking v4l2src for the target size/rate directly, since we
    # can't be sure every SIZE_PRESETS entry is an actual discrete UVC mode
    # this camera advertises (see README's "SIZE/RATE presets" section).
    "native_width": int(os.environ.get("GAR_CAMERA_WIDTH", "2048")),
    "native_height": int(os.environ.get("GAR_CAMERA_HEIGHT", "1536")),
    "native_fps": int(os.environ.get("GAR_CAMERA_FPS", "30")),
    "camera_caps": os.environ.get("GAR_CAMERA_CAPS", "image/jpeg"),
    "camera_io_mode": os.environ.get("GAR_CAMERA_IO_MODE", "auto"),
    "source_id": os.environ.get("GAR_STREAM_SOURCE_ID", f"{socket.gethostname()}-tx"),
    "source_name": os.environ.get("GAR_STREAM_SOURCE_NAME", socket.gethostname()),
    "discovery_port": int(
        os.environ.get("GAR_STREAM_DISCOVERY_PORT", str(DEFAULT_DISCOVERY_PORT))
    ),
    "jpeg_quality": 85,
    # Optional local preview: an ILI9341 wired directly to this Pi 5 over
    # SPI (same driver/wiring as gar-stream-rx). Leave False if you don't
    # have a panel attached to the TX board.
    "local_display": os.environ.get("GAR_LOCAL_DISPLAY", "0") == "1",
    "spi_bus": 0,
    "spi_device": 0,
    "spi_max_hz": 24_000_000,
    "dc_gpio": _optional_env_int("GAR_LCD_DC_GPIO"),
    "rst_gpio": _optional_env_int("GAR_LCD_RST_GPIO"),
}

# 4:3 profiles keep the receiver's 320x240 ILI9341 free of letterboxing.
# All outgoing profiles intentionally use a stable 15fps after videorate,
# independently of the camera capture frame rate.
PROFILES = (
    ("Low latency", 320, 240),
    ("Standard", 640, 480),
    ("High quality", 1024, 768),
    ("Maximum", 2048, 1536),
)
DEFAULT_PROFILE_INDEX = 1
FIXED_FPS = 15

MENU_ITEMS = ("Profile", "Mirror", "Rotate", "Overlay")
MAIN_MENU_ITEMS = (*MENU_ITEMS, "EXIT")
ROTATION_METHODS = (
    ("0°", "none"),
    ("90°", "clockwise"),
    ("180°", "rotate-180"),
    ("270°", "counterclockwise"),
)

# The local preview always renders at the panel's native size, independent
# of the SIZE_PRESETS chosen for the network branch.
PREVIEW_WIDTH, PREVIEW_HEIGHT = 320, 240


def _build_pipeline_string(config, with_preview):
    network_branch = (
        "t. ! queue max-size-buffers=2 leaky=downstream "
        "! videoscale ! videorate name=rate_limiter "
        "! capsfilter name=out_caps "
        "! videoflip name=rotate_transform method=none "
        "! videoflip name=mirror_transform method=none "
        f"! jpegenc name=jpeg_encoder quality={config['jpeg_quality']} "
        "! rtpjpegpay name=rtp_pay "
        "! multiudpsink name=stream_sink sync=false async=false"
    )
    branches = [network_branch]
    if with_preview:
        branches.append(
            "t. ! queue max-size-buffers=2 leaky=downstream "
            "! videoflip name=preview_rotate_transform method=none "
            "! videoflip name=preview_mirror_transform method=none "
            '! textoverlay name=preview_status_overlay text="" valignment=top '
            "halignment=left shaded-background=true "
            "! videoconvert ! videoscale "
            f"! video/x-raw,format=RGB16,width={PREVIEW_WIDTH},height={PREVIEW_HEIGHT} "
            "! appsink name=preview_sink emit-signals=true sync=false max-buffers=1 drop=true"
        )
    capture_caps = config["camera_caps"]
    decoder = "! jpegdec " if capture_caps.startswith("image/jpeg") else ""
    return (
        f"v4l2src name=camera_source device={config['camera_device']} io-mode={config['camera_io_mode']} "
        f"! {capture_caps},width={config['native_width']},height={config['native_height']},"
        f"framerate={config['native_fps']}/1 "
        f"{decoder}! videoconvert ! tee name=t " + " ".join(branches)
    )


class StreamTx:
    """Owns the GStreamer pipeline and the TX's physical-control menu."""

    def __init__(self, config, profile_index=DEFAULT_PROFILE_INDEX, display=None):
        self.config = config
        self.profile_index = profile_index
        self.mirror = False
        self.rotation_index = 0
        self.overlay_enabled = True
        self.menu_open = False
        self.menu_index = 0
        self.submenu_index = None
        self.display = display
        self.restart_pending = False
        self.sent_first_packet = False
        self.stream_clients = ()
        self.captured_first_frame = False
        self.encoded_first_frame = False
        self.sent_frame_count = 0
        self.first_sent_monotonic_ns = 0
        self.last_sent_monotonic_ns = 0
        self.encoder_rotate_count = 0
        self.encoder_press_count = 0
        self.pipeline = None
        self.out_caps = None
        self.rotate_transform = None
        self.mirror_transform = None
        self.preview_rotate_transform = None
        self.preview_mirror_transform = None
        self.preview_status_overlay = None
        self.rate_limiter = None

        self._create_pipeline()

    def _create_pipeline(self):
        self.pipeline = Gst.parse_launch(
            _build_pipeline_string(self.config, self.display is not None)
        )
        self.out_caps = self.pipeline.get_by_name("out_caps")
        self.rate_limiter = self.pipeline.get_by_name("rate_limiter")
        self.rotate_transform = self.pipeline.get_by_name("rotate_transform")
        self.mirror_transform = self.pipeline.get_by_name("mirror_transform")
        camera_source = self.pipeline.get_by_name("camera_source")
        jpeg_encoder = self.pipeline.get_by_name("jpeg_encoder")
        payloader = self.pipeline.get_by_name("rtp_pay")
        self.stream_sink = self.pipeline.get_by_name("stream_sink")
        camera_source.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._on_camera_frame
        )
        jpeg_encoder.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._on_jpeg_frame
        )
        payloader.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST,
            self._on_rtp_packet,
        )

        if self.display is not None:
            self.preview_rotate_transform = self.pipeline.get_by_name(
                "preview_rotate_transform"
            )
            self.preview_mirror_transform = self.pipeline.get_by_name(
                "preview_mirror_transform"
            )
            self.preview_status_overlay = self.pipeline.get_by_name(
                "preview_status_overlay"
            )
            sink = self.pipeline.get_by_name("preview_sink")
            sink.connect("new-sample", self._on_new_sample)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        self._apply_video_options()
        self._apply_stream_clients()

    def _on_camera_frame(self, _pad, _info):
        if not self.captured_first_frame:
            self.captured_first_frame = True
            print(f"[stream_tx] first camera frame <- {self.config['camera_device']}")
        return Gst.PadProbeReturn.OK

    def _on_jpeg_frame(self, _pad, _info):
        if not self.encoded_first_frame:
            self.encoded_first_frame = True
            print("[stream_tx] first JPEG frame encoded")
        # This probe is one JPEG frame (unlike rtpjpegpay's packet probe).
        # Count it only while a receiver lease has made the UDP sink active.
        if self.stream_clients:
            self.sent_frame_count += 1
            self.last_sent_monotonic_ns = time.monotonic_ns()
            if not self.first_sent_monotonic_ns:
                self.first_sent_monotonic_ns = self.last_sent_monotonic_ns
        return Gst.PadProbeReturn.OK

    def _on_rtp_packet(self, _pad, _info):
        if not self.sent_first_packet:
            self.sent_first_packet = True
            print("[stream_tx] first RTP packet ready")
        return Gst.PadProbeReturn.OK

    def _profile(self):
        return PROFILES[self.profile_index]

    def _apply_output_caps(self):
        _name, width, height = self._profile()
        self.out_caps.set_property(
            "caps",
            Gst.Caps.from_string(
                f"video/x-raw,width={width},height={height},framerate={FIXED_FPS}/1"
            ),
        )
        print(
            f"[stream_tx] profile {self._profile()[0]}: {width}x{height}@{FIXED_FPS}fps"
        )

    def _apply_stream_clients(self):
        clients = ",".join(
            f"{client.host}:{client.port}" for client in self.stream_clients
        )
        self.stream_sink.set_property("clients", clients)

    def set_stream_clients(self, clients):
        """Apply receiver leases on the GLib/GStreamer thread."""
        clients = tuple(clients)
        if clients == self.stream_clients:
            return GLib.SOURCE_REMOVE
        self.stream_clients = clients
        self.sent_first_packet = False
        self._apply_stream_clients()
        destinations = ", ".join(
            f"{client.receiver_id}@{client.host}:{client.port}" for client in clients
        )
        print(f"[stream_tx] receivers: {destinations or 'none'}")
        self._refresh_status_overlay()
        return GLib.SOURCE_REMOVE

    def _status_text(self):
        profile, width, height = self._profile()
        mirror = "ON" if self.mirror else "OFF"
        rotation = ROTATION_METHODS[self.rotation_index][0]
        if self.menu_open:
            if self.submenu_index is not None:
                item = MENU_ITEMS[self.menu_index]
                rows = [f"TX MENU > {item.upper()}"]
                for index, value in enumerate(self._submenu_values()):
                    marker = ">" if index == self.submenu_index else " "
                    rows.append(f"{marker} {value}")
                return "\n".join(rows)
            rows = ["TX MENU"]
            for index, item in enumerate(MAIN_MENU_ITEMS):
                marker = ">" if index == self.menu_index else " "
                if item == "Profile":
                    value = profile
                elif item == "Mirror":
                    value = mirror
                elif item == "Rotate":
                    value = rotation
                elif item == "Overlay":
                    value = "ON" if self.overlay_enabled else "OFF"
                else:
                    value = ""
                rows.append(f"{marker} {item}{': ' + value if value else ''}")
            return "\n".join(rows)
        if not self.overlay_enabled:
            return ""
        receiver_status = (
            f"Streaming to {len(self.stream_clients)} RX"
            if self.stream_clients
            else "Available · waiting for RX"
        )
        return (
            f"TX · {profile}\n{width}x{height} · {FIXED_FPS} fps · "
            f"Mirror {mirror} · Rotate {rotation}\n{receiver_status}"
        )

    def _refresh_status_overlay(self):
        text = self._status_text()
        if self.preview_status_overlay is not None:
            # Keep normal status unobtrusive, but make menu text readable on
            # any camera image. RX follows the same menu-background rule.
            self.preview_status_overlay.set_property(
                "shaded-background", self.menu_open
            )
            self.preview_status_overlay.set_property("text", text)

    def _apply_video_options(self):
        self._apply_output_caps()
        self.rotate_transform.set_property(
            "method", ROTATION_METHODS[self.rotation_index][1]
        )
        self.mirror_transform.set_property(
            "method", "horizontal-flip" if self.mirror else "none"
        )
        if self.preview_rotate_transform is not None:
            self.preview_rotate_transform.set_property(
                "method", ROTATION_METHODS[self.rotation_index][1]
            )
            self.preview_mirror_transform.set_property(
                "method", "horizontal-flip" if self.mirror else "none"
            )
        self._refresh_status_overlay()

    def _on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                self.display.blit_native_rgb565(
                    0, 0, PREVIEW_WIDTH, PREVIEW_HEIGHT, mapinfo.data
                )
            finally:
                buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    def _on_bus_message(self, bus, message):
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"[gst error] {err}: {debug}", file=sys.stderr)
            if not self.restart_pending:
                self.restart_pending = True
                GLib.timeout_add_seconds(3, self._restart_pipeline)
        elif message.type == Gst.MessageType.EOS:
            print("[gst] end of stream", file=sys.stderr)

    def _restart_pipeline(self):
        self.pipeline.set_state(Gst.State.NULL)
        self._create_pipeline()
        self.pipeline.set_state(Gst.State.PLAYING)
        self.restart_pending = False
        print("[stream_tx] rebuilt camera pipeline")
        return GLib.SOURCE_REMOVE

    def _submenu_values(self):
        if self.menu_index == 0:
            return tuple(profile[0] for profile in PROFILES)
        if self.menu_index == 1:
            return ("OFF", "ON")
        if self.menu_index == 2:
            return tuple(rotation[0] for rotation in ROTATION_METHODS)
        return ("OFF", "ON")

    def _current_submenu_index(self):
        if self.menu_index == 0:
            return self.profile_index
        if self.menu_index == 1:
            return int(self.mirror)
        if self.menu_index == 2:
            return self.rotation_index
        return int(self.overlay_enabled)

    def _apply_submenu_value(self):
        if self.menu_index == 0:
            self.profile_index = self.submenu_index
        elif self.menu_index == 1:
            self.mirror = bool(self.submenu_index)
        elif self.menu_index == 2:
            self.rotation_index = self.submenu_index
        else:
            self.overlay_enabled = bool(self.submenu_index)
        self._apply_video_options()

    def rotate_control(self, direction):
        """Handle a physical encoder turn using the menu's current state."""
        self.encoder_rotate_count += 1
        if not self.menu_open:
            return
        step = 1 if direction >= 0 else -1
        if self.submenu_index is None:
            self.menu_index = (self.menu_index + step) % len(MAIN_MENU_ITEMS)
        else:
            self.submenu_index = (self.submenu_index + step) % len(
                self._submenu_values()
            )
        self._refresh_status_overlay()

    def press_control(self):
        """Open the menu, select an item, then confirm its submenu value."""
        self.encoder_press_count += 1
        if not self.menu_open:
            self.menu_open = True
            self.menu_index = 0
        elif self.submenu_index is None and self.menu_index == len(MENU_ITEMS):
            self.menu_open = False
        elif self.submenu_index is None:
            self.submenu_index = self._current_submenu_index()
        else:
            self._apply_submenu_value()
            self.submenu_index = None
        self._refresh_status_overlay()

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    def diagnostics(self):
        item = MAIN_MENU_ITEMS[self.menu_index] if self.menu_open else None
        frame_rate = measured_fps(
            self.sent_frame_count,
            self.first_sent_monotonic_ns,
            self.last_sent_monotonic_ns,
        )
        drop_count = int(self.rate_limiter.get_property("drop"))
        latency_ms = self._pipeline_latency_ms()
        return {
            "frames": {
                "sent_count": self.sent_frame_count,
                "last_sent_monotonic_ns": self.last_sent_monotonic_ns or None,
                "fps": frame_rate,
                "configured_fps": FIXED_FPS,
                "drop_count": drop_count,
                "latency_ms": latency_ms,
            },
            "menu": {
                "open": self.menu_open,
                "item": item,
                "submenu_open": self.submenu_index is not None,
            },
            "encoder": {
                "rotate_count": self.encoder_rotate_count,
                "press_count": self.encoder_press_count,
            },
        }

    def _pipeline_latency_ms(self):
        query = Gst.Query.new_latency()
        if not self.pipeline.query(query):
            return None
        _live, minimum, _maximum = query.parse_latency()
        if minimum == Gst.CLOCK_TIME_NONE:
            return None
        return minimum / Gst.MSECOND


def main():
    Gst.init(None)

    display = None
    if CONFIG["local_display"]:
        missing = [k for k in ("dc_gpio", "rst_gpio") if CONFIG[k] is None]
        if missing:
            raise SystemExit(
                f"Fill in CONFIG{missing} for the local ILI9341 preview, or set "
                "CONFIG['local_display'] = False."
            )
        from ili9341 import ILI9341  # local import: spidev only needed for this path

        display = ILI9341(
            CONFIG["spi_bus"],
            CONFIG["spi_device"],
            CONFIG["dc_gpio"],
            CONFIG["rst_gpio"],
            spi_max_hz=CONFIG["spi_max_hz"],
            rotation=1,
            bgr=True,
        )

    tx = StreamTx(CONFIG, display=display)
    tx.start()

    advertiser = SourceAdvertiser(
        CONFIG["source_id"],
        CONFIG["source_name"],
        control_port=CONFIG["discovery_port"],
        on_clients_changed=lambda clients: GLib.idle_add(
            tx.set_stream_clients, clients
        ),
    )
    advertiser.start()
    metrics = MetricsWriter()

    def publish_metrics():
        payload = {
            "schema_version": 1,
            "role": "tx",
            "health": {"ok": True},
            "build": {
                "id": os.environ.get("GAR_ARTIFACT_BUILD_ID"),
                "hash": os.environ.get("GAR_ARTIFACT_HASH"),
            },
            "source": advertiser.diagnostics(),
            **tx.diagnostics(),
        }
        metrics.write(payload)
        return GLib.SOURCE_CONTINUE

    publish_metrics()
    GLib.timeout_add(1000, publish_metrics)
    print(
        f"[stream_tx] advertising {CONFIG['source_name']} ({CONFIG['source_id']}) "
        f"on UDP {advertiser.control_port}"
    )

    encoder = KY040(
        CONFIG["enc_clk_gpio"],
        CONFIG["enc_dt_gpio"],
        CONFIG["enc_sw_gpio"],
        on_rotate=lambda direction, counter: tx.rotate_control(direction),
        on_press=tx.press_control,
    )
    encoder.start()

    loop = GLib.MainLoop()
    print("Running. Press KY-040 for TX menu; rotate to select/change. Ctrl+C to quit.")
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        advertiser.stop()
        encoder.stop()
        tx.stop()
        if display is not None:
            display.close()


if __name__ == "__main__":
    main()
