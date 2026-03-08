from __future__ import annotations

import os
import select
import sys
import time

if os.name != "nt":
    import termios
    import tty
else:
    import msvcrt


class InputHandler:
    def __enter__(self) -> "InputHandler":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read_keys(self, timeout: float) -> list[str]:
        raise NotImplementedError


class WindowsInputHandler(InputHandler):
    def read_keys(self, timeout: float) -> list[str]:
        deadline = time.monotonic() + timeout
        keys: list[str] = []
        while True:
            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getwch()
                    continue
                keys.append(key)
            if keys or time.monotonic() >= deadline:
                return keys
            time.sleep(0.01)


class UnixInputHandler(InputHandler):
    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.original_mode = None

    def __enter__(self) -> "UnixInputHandler":
        self.original_mode = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.original_mode is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.original_mode)

    def read_keys(self, timeout: float) -> list[str]:
        keys: list[str] = []
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return keys
        while True:
            chunk = os.read(self.fd, 1)
            if not chunk:
                break
            keys.append(chunk.decode("utf-8", errors="ignore"))
            ready, _, _ = select.select([self.fd], [], [], 0)
            if not ready:
                break
        return keys


def create_input_handler() -> InputHandler:
    if os.name == "nt":
        return WindowsInputHandler()
    return UnixInputHandler()
