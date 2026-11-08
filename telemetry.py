"""Usage statistics, so the add-on's author can see which parts get used.

What is sent: the name of each button pressed or step taken (opened a PDF,
made cards, ran Auto-occlude), counts such as pages or cards, the add-on,
Anki and OS versions, the one-way computer id from auto_occlude.device_id,
and the Auto-occlude sign-in when there is one.
Never sent: slide images, file names, card text or anything typed.

Events wait in memory and go to the PDF Occlusion server once a minute and
when the profile closes. Set "usage_stats" to false in the add-on's config to
send nothing.
"""
import os
import platform
import sys
import threading
import time

from aqt import mw
from aqt.qt import QAbstractButton, QTimer

from .version import VERSION

_queue: list = []
_lock = threading.Lock()
_timer = None
_BATCH = 200


def enabled() -> bool:
    cfg = mw.addonManager.getConfig(__name__.split(".")[0]) or {}
    return cfg.get("usage_stats", True) is not False


def track(name: str, **props) -> None:
    try:
        if not enabled():
            return
        with _lock:
            _queue.append({"name": name, "at": int(time.time() * 1000), "props": props or None})
            del _queue[:-2000]
        _start_timer()
    except Exception:       # noqa: BLE001 - statistics must never get in the way
        pass


def _start_timer() -> None:
    global _timer
    if _timer is not None:
        return
    _timer = QTimer(mw)
    _timer.timeout.connect(flush)
    _timer.start(60_000)


def _about() -> dict:
    try:
        from anki.buildinfo import version as anki
    except Exception:       # noqa: BLE001
        anki = ""
    if sys.platform == "darwin":
        os_name = f"macOS {platform.mac_ver()[0]}"
    elif sys.platform == "win32":
        os_name = f"Windows {platform.release()}"
    else:
        os_name = f"{platform.system()} {platform.release()}"
    return {"version": VERSION, "anki": anki, "os": f"{os_name} {platform.machine()}".strip()}


def _send(batch: list, timeout: float) -> dict:
    from .auto_occlude import account, call
    return call("/v1/events", {"events": batch, **_about()},
                token=account().get("token"), timeout=timeout)


def flush(on_reply=None, wait: bool = False) -> None:
    """Send what is waiting. on_reply(reply) runs on the main thread when the
    server answers; wait=True sends in this thread, briefly (for quitting).
    With statistics off, on_reply still gets the server's health reply, which
    carries the newest version too."""
    if not enabled():
        if on_reply:
            def health():
                from .auto_occlude import call
                try:
                    reply = call("/v1/health", timeout=15)
                except Exception:   # noqa: BLE001
                    return
                mw.taskman.run_on_main(lambda: on_reply(reply))
            threading.Thread(target=health, daemon=True).start()
        return
    with _lock:
        batch = _queue[:_BATCH]
        del _queue[:_BATCH]
    if not batch and not on_reply:
        return
    if wait:
        try:
            _send(batch, 3)
        except Exception:   # noqa: BLE001
            pass
        return

    def run():
        try:
            reply, ok = _send(batch, 15), True
        except Exception as e:  # noqa: BLE001
            reply, ok = None, False
            # a server that is down keeps them for next time; one that
            # refused them would refuse them again
            if getattr(e, "status", 0) in (0, 502, 503, 500):
                with _lock:
                    _queue[:0] = batch
                    del _queue[:-2000]
        if ok and on_reply:
            mw.taskman.run_on_main(lambda: on_reply(reply))
    threading.Thread(target=run, daemon=True).start()


def watch_buttons(widget, window: str) -> None:
    """Count every press of every button in widget, by its label."""
    for b in widget.findChildren(QAbstractButton):
        if b.property("_po_watched"):
            continue
        b.setProperty("_po_watched", True)
        # pressed, not clicked: a button that opens a window would otherwise
        # be logged only after that window closes
        b.pressed.connect(lambda b=b: track("click", window=window, button=_label(b)))


def _label(b) -> str:
    text = (b.text() or "").replace("&", "").split("  ")[0].strip()
    return (text or b.toolTip().split("(")[0].strip() or b.objectName() or "unnamed")[:60]


def addon_dir_id():
    """The AnkiWeb id when the add-on was installed from AnkiWeb, else None."""
    name = os.path.basename(os.path.dirname(__file__))
    return int(name) if name.isdigit() else None


def older(a: str, b: str) -> bool:
    pa = [int(x) if x.isdigit() else 0 for x in str(a).split(".")]
    pb = [int(x) if x.isdigit() else 0 for x in str(b).split(".")]
    n = max(len(pa), len(pb))
    return pa + [0] * (n - len(pa)) < pb + [0] * (n - len(pb))
