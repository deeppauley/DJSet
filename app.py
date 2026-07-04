import ctypes
import logging
import threading
import time
from pathlib import Path

import keyboard


TRACKS_FILE = Path(__file__).with_name("tracks.txt")
LOG_FILE = Path(__file__).with_name("track_paster.log")
HOTKEY_NEXT = "`"
HOTKEY_NEXT_FALLBACK = "f8"
HOTKEY_RESET = "shift+`"
HOTKEY_RESET_FALLBACK = "shift+f8"
HOTKEY_QUIT = "esc"

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KERNEL32 = ctypes.windll.kernel32
USER32 = ctypes.windll.user32

KERNEL32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
KERNEL32.GlobalAlloc.restype = ctypes.c_void_p
KERNEL32.GlobalLock.argtypes = [ctypes.c_void_p]
KERNEL32.GlobalLock.restype = ctypes.c_void_p
KERNEL32.GlobalUnlock.argtypes = [ctypes.c_void_p]
KERNEL32.GlobalUnlock.restype = ctypes.c_bool
KERNEL32.GlobalFree.argtypes = [ctypes.c_void_p]
KERNEL32.GlobalFree.restype = ctypes.c_void_p
USER32.OpenClipboard.argtypes = [ctypes.c_void_p]
USER32.OpenClipboard.restype = ctypes.c_bool
USER32.EmptyClipboard.restype = ctypes.c_bool
USER32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
USER32.SetClipboardData.restype = ctypes.c_void_p
USER32.CloseClipboard.restype = ctypes.c_bool


logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def load_tracks(path: Path) -> list[str]:
    return [
        line
        for line in (raw_line.strip() for raw_line in path.read_text(encoding="utf-8").splitlines())
        if line
    ]


def set_clipboard_text(text: str) -> None:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    handle = KERNEL32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise OSError("GlobalAlloc failed")

    locked = KERNEL32.GlobalLock(handle)
    if not locked:
        KERNEL32.GlobalFree(handle)
        raise OSError("GlobalLock failed")

    try:
        ctypes.memmove(locked, ctypes.addressof(data), size)
    finally:
        KERNEL32.GlobalUnlock(handle)

    if not USER32.OpenClipboard(None):
        KERNEL32.GlobalFree(handle)
        raise OSError("OpenClipboard failed")

    try:
        USER32.EmptyClipboard()
        if not USER32.SetClipboardData(CF_UNICODETEXT, handle):
            KERNEL32.GlobalFree(handle)
            raise OSError("SetClipboardData failed")
    finally:
        USER32.CloseClipboard()


class TrackPaster:
    def __init__(self, tracks: list[str]) -> None:
        if not tracks:
            raise ValueError("No tracks found in tracks.txt")
        self.tracks = tracks
        self.index = 0
        self.lock = threading.Lock()

    def handle_next_hotkey(self, hotkey: str) -> None:
        message = f"Hotkey received: {hotkey}"
        print(message, flush=True)
        logging.info(message)
        self.paste_next()

    def handle_reset_hotkey(self, hotkey: str) -> None:
        message = f"Hotkey received: {hotkey}"
        print(message, flush=True)
        logging.info(message)
        self.reset()

    def paste_next(self) -> None:
        try:
            with self.lock:
                track = self.tracks[self.index]
                current_number = self.index + 1
                self.index = (self.index + 1) % len(self.tracks)

            set_clipboard_text(track)
            time.sleep(0.1)
            keyboard.send("ctrl+v")
            time.sleep(0.05)
            keyboard.send("enter")

            message = f"Pasted {current_number}/{len(self.tracks)}: {track}"
            print(message, flush=True)
            logging.info(message)
        except Exception:
            logging.exception("paste_next failed")

    def reset(self) -> None:
        with self.lock:
            self.index = 0
        print("Sequence reset to the first track.", flush=True)
        logging.info("Sequence reset to the first track.")


def main() -> None:
    tracks = load_tracks(TRACKS_FILE)
    paster = TrackPaster(tracks)

    print("Track paster is running.")
    print(f"Press {HOTKEY_NEXT} or {HOTKEY_NEXT_FALLBACK} to paste the next track.")
    print(f"Press {HOTKEY_RESET} or {HOTKEY_RESET_FALLBACK} to reset to the first track.")
    print(f"Press {HOTKEY_QUIT} to quit.")
    print(f"Loaded {len(tracks)} tracks from {TRACKS_FILE.name}.")
    logging.info("Track paster started with %s tracks.", len(tracks))

    keyboard.add_hotkey(HOTKEY_NEXT, lambda: paster.handle_next_hotkey(HOTKEY_NEXT), suppress=True)
    keyboard.add_hotkey(HOTKEY_NEXT_FALLBACK, lambda: paster.handle_next_hotkey(HOTKEY_NEXT_FALLBACK), suppress=True)
    keyboard.add_hotkey(HOTKEY_RESET, lambda: paster.handle_reset_hotkey(HOTKEY_RESET), suppress=True)
    keyboard.add_hotkey(HOTKEY_RESET_FALLBACK, lambda: paster.handle_reset_hotkey(HOTKEY_RESET_FALLBACK), suppress=True)
    keyboard.wait(HOTKEY_QUIT)


if __name__ == "__main__":
    main()
