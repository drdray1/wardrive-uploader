"""Scroll pHAT status display: glyphs, animations, and a progress bar.

The Scroll pHAT is 11x5 WHITE-ONLY LEDs (no color), driver IS31FL3730 @ I2C 0x74.
Status is conveyed by glyphs + animation + brightness.

Three backends, chosen at runtime:
  - ScrollphatBackend : the pimoroni `scrollphat` library (preferred)
  - Smbus2Backend     : minimal direct IS31FL3730 driver (fallback if the
                        2016 library won't import on Bookworm)
  - NullBackend       : logs only (no hardware / dev on a laptop)

A background thread renders the current state so animations keep moving while
the main pipeline does slow work (mount/merge/upload).
"""
import logging
import threading
import time

log = logging.getLogger("wardrive.display")

WIDTH, HEIGHT = 11, 5

# States
IDLE = "idle"
SCANNING = "scanning"
COPYING = "copying"
MERGING = "merging"
UPLOADING = "uploading"
SAFE_REMOVE = "safe_remove"
SUCCESS = "success"
ERROR = "error"
NONE_FOUND = "none"


# ---------------------------------------------------------------------------
# Backends. Each implements: set_pixel(x,y,on), set_brightness(0-255),
# show(), clear(). Coordinates: x 0..10 (left->right), y 0..4 (top->bottom).
# ---------------------------------------------------------------------------
class NullBackend:
    available = True

    def __init__(self, **_):
        log.warning("display: using NullBackend (no hardware output)")

    def set_pixel(self, x, y, on):
        pass

    def set_brightness(self, value):
        pass

    def show(self):
        pass

    def clear(self):
        pass


class ScrollphatBackend:
    def __init__(self, rotate=0, **_):
        # NOTE: the 2016 `scrollphat` lib calls sys.exit() (SystemExit) if the
        # system `smbus` module is missing - make_backend catches BaseException.
        import scrollphat  # raises / exits if unavailable
        self._sp = scrollphat
        self._rotate = rotate
        self._sp.clear()

    def set_pixel(self, x, y, on):
        if self._rotate == 180:
            x, y = WIDTH - 1 - x, HEIGHT - 1 - y
        self._sp.set_pixel(x, y, 1 if on else 0)

    def set_brightness(self, value):
        self._sp.set_brightness(int(value))

    def show(self):
        self._sp.update()

    def clear(self):
        self._sp.clear()
        self._sp.update()


class Smbus2Backend:
    """Minimal IS31FL3730 framebuffer driver (matrix mode, 11x5).

    The IS31FL3730 maps to an 11-column x 7-row matrix; the Scroll pHAT wires
    5 of those rows. Each column is one register byte (bit per row).
    """
    ADDR = 0x74
    CMD_SET_MODE = 0x00
    CMD_MATRIX_1 = 0x01
    CMD_UPDATE = 0x0C
    CMD_BRIGHTNESS = 0x19
    MODE_5X11 = 0x03      # IS31FL3730: matrix 1 only, 5-row x 11-col addressing

    def __init__(self, bus=1, rotate=0, **_):
        from smbus2 import SMBus  # raises if unavailable
        self._bus = SMBus(bus)
        self._rotate = rotate
        self._cols = [0] * WIDTH
        self._bus.write_byte_data(self.ADDR, self.CMD_SET_MODE, self.MODE_5X11)
        self.set_brightness(128)

    def set_pixel(self, x, y, on):
        if self._rotate == 180:
            x, y = WIDTH - 1 - x, HEIGHT - 1 - y
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            if on:
                self._cols[x] |= (1 << y)
            else:
                self._cols[x] &= ~(1 << y)

    def set_brightness(self, value):
        # IS31FL3730 brightness register 0x19: 0..255 written directly.
        pwm = max(0, min(255, int(value)))
        try:
            self._bus.write_byte_data(self.ADDR, self.CMD_BRIGHTNESS, pwm)
        except OSError:
            pass

    def show(self):
        try:
            self._bus.write_i2c_block_data(self.ADDR, self.CMD_MATRIX_1, self._cols)
            self._bus.write_byte_data(self.ADDR, self.CMD_UPDATE, 0x01)
        except OSError as e:
            log.error("smbus2 show failed: %s", e)

    def clear(self):
        self._cols = [0] * WIDTH
        self.show()


def make_backend(prefer="auto", rotate=0):
    order = []
    if prefer in ("auto", "scrollphat"):
        order.append(ScrollphatBackend)
    if prefer in ("auto", "smbus2"):
        order.append(Smbus2Backend)
    for cls in order:
        try:
            be = cls(rotate=rotate)
            log.info("display backend: %s", cls.__name__)
            return be
        except BaseException as e:  # noqa: BLE001
            # BaseException, not Exception: the scrollphat lib calls sys.exit()
            # (SystemExit) when system smbus is missing - must not kill us.
            log.warning("display backend %s unavailable: %s", cls.__name__, e)
    return NullBackend()


# ---------------------------------------------------------------------------
# 3x5 glyphs for the few characters we draw directly (per column, top->bottom).
# ---------------------------------------------------------------------------
def _g(rows):
    """rows: 5 strings of '#'/' '. Returns list[ (x,y) ] of lit pixels."""
    pts = []
    for y, line in enumerate(rows):
        for x, ch in enumerate(line):
            if ch == "#":
                pts.append((x, y))
    return pts


GLYPH_CHECK = _g(["     ", "    #", "   # ", "# #  ", " #   "])
GLYPH_X = _g(["#   #", " # # ", "  #  ", " # # ", "#   #"])
# Up arrow = "eject / lift the card out".
GLYPH_UP = _g(["  #  ", " ### ", "#####", "  #  ", "  #  "])


# ---------------------------------------------------------------------------
# Controller with background render thread.
# ---------------------------------------------------------------------------
class Display:
    def __init__(self, backend=None, brightness=128, rotate=0):
        self.be = backend or make_backend(rotate=rotate)
        self.base_brightness = brightness
        self._state = IDLE
        self._progress = 0.0
        self._marks = {}        # per-uploader ok flags for SUCCESS/ERROR
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._frame = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_state(self, state, progress=None):
        with self._lock:
            self._state = state
            if progress is not None:
                self._progress = max(0.0, min(1.0, progress))
        log.info("display state -> %s%s", state,
                 "" if progress is None else f" ({int(self._progress*100)}%)")

    def set_progress(self, progress):
        with self._lock:
            self._progress = max(0.0, min(1.0, progress))

    def set_result(self, state, marks):
        """Set SUCCESS/ERROR plus per-uploader marks, e.g.
        {'wigle': True, 'wdgowars': False}. Left edge = wigle, right = wdgowars."""
        with self._lock:
            self._state = state
            self._marks = dict(marks or {})
        log.info("display result -> %s marks=%s", state, marks)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self.be.clear()
        except Exception:
            pass

    # -- internal rendering -------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                state, progress, frame = self._state, self._progress, self._frame
                marks = dict(self._marks)
            try:
                self._render(state, progress, frame, marks)
            except Exception as e:
                log.debug("render error: %s", e)
            with self._lock:
                self._frame += 1
            time.sleep(0.18)

    def _blank(self):
        for x in range(WIDTH):
            for y in range(HEIGHT):
                self.be.set_pixel(x, y, False)

    def _draw(self, points, dx=0, dy=0):
        for (x, y) in points:
            self.be.set_pixel(x + dx, y + dy, True)

    def _draw_marks(self, marks):
        """Side-column flags: left edge (x=0) = wigle ok, right edge (x=10) = wdgowars ok."""
        if marks.get("wigle"):
            for y in range(HEIGHT):
                self.be.set_pixel(0, y, True)
        if marks.get("wdgowars"):
            for y in range(HEIGHT):
                self.be.set_pixel(WIDTH - 1, y, True)

    def _render(self, state, progress, frame, marks=None):
        self._blank()
        marks = marks or {}

        if state == IDLE:
            # Slow, dim dot drifting left->right (calm "waiting" cue).
            self.be.set_brightness(max(8, self.base_brightness // 5))
            pos = (frame // 3) % WIDTH
            self.be.set_pixel(pos, 2, True)

        elif state == COPYING:
            # Brighter 2-px "comet" sweeping right = pulling data off the card.
            self.be.set_brightness(self.base_brightness)
            head = (frame // 2) % (WIDTH + 2)
            for x in (head - 1, head):
                if 0 <= x < WIDTH:
                    self.be.set_pixel(x, 2, True)

        elif state == SAFE_REMOVE:
            # Steady up-arrow with a gentle pulse: "lift the card out now".
            self.be.set_brightness(60 + int(150 * _tri(frame, 18)))
            self._draw(GLYPH_UP, dx=3)

        elif state == SCANNING:
            self.be.set_brightness(self.base_brightness)
            # Dot easing around a small ring (advances every 3rd frame).
            ring = [(4, 1), (5, 1), (6, 1), (6, 2), (6, 3),
                    (5, 3), (4, 3), (4, 2)]
            self._draw([ring[(frame // 3) % len(ring)]])

        elif state == MERGING:
            self.be.set_brightness(self.base_brightness)
            # Three dots filling 1->2->3 then repeating, unhurried.
            phase = (frame // 5) % 4
            for i, x in enumerate((3, 5, 7)):
                if i < phase:
                    self.be.set_pixel(x, 2, True)

        elif state == UPLOADING:
            # Steady bar (no blink) - only changes with real progress.
            self.be.set_brightness(self.base_brightness)
            lit = int(round(progress * WIDTH))
            for x in range(lit):
                for y in range(HEIGHT):
                    self.be.set_pixel(x, y, True)

        elif state == SUCCESS:
            # Steady check with a slow, gentle breathing pulse + uploader marks.
            b = 60 + int(150 * _tri(frame, 32))
            self.be.set_brightness(b)
            self._draw(GLYPH_CHECK, dx=3)
            self._draw_marks(marks)

        elif state == ERROR:
            # Steady X with a slow brightness breathe - attention without strobing.
            b = 40 + int(140 * _tri(frame, 24))
            self.be.set_brightness(b)
            self._draw(GLYPH_X, dx=3)
            self._draw_marks(marks)

        elif state == NONE_FOUND:
            # Dim steady centre dash (no blink).
            self.be.set_brightness(max(8, self.base_brightness // 4))
            for x in range(4, 7):
                self.be.set_pixel(x, 2, True)

        self.be.show()


def _tri(frame, period):
    """Triangle wave in [0, 1] for smooth pulsing (0 -> 1 -> 0)."""
    p = (frame % period) / period
    return 1 - abs(2 * p - 1)
