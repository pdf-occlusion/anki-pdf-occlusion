"""Text recognition for slides that are pictures rather than text.

A lecture PDF exported the wrong way, or one slide holding a screenshot of a
table, has no text layer at all: pdfium reports nothing and Detect has
nothing to box. macOS and Windows both carry a text recogniser, and both can
be reached without any dependency at all:

- macOS: the Vision framework, through `osascript -l JavaScript`, which has
  an Objective-C bridge.
- Windows 10 and later: Windows.Media.Ocr, through Windows PowerShell, which
  can load WinRT types.

Either way the whole thing is a script string and a subprocess. Nothing is
bundled and nothing is installed.

On Linux `available()` is False and Detect behaves exactly as it did before,
which is the honest outcome: the cross-platform engines are all either a
user-installed binary or a Python-ABI-specific wheel, and a wheel per Python
version would undo what vendoring pypdfium2 buys us (see pdf_renderer).

On macOS the first call on a machine takes ~30s while it loads its models;
every call after is a fraction of a second, so `warm_up()` gets that out of
the way in the background when a PDF is opened. Windows has no such pause,
so there it does nothing.
"""
import json
import os
import subprocess
import sys
import tempfile
from typing import Optional

from aqt.qt import QImage


class OcrError(RuntimeError):
    pass


# Vision hands back one observation per line, but Detect boxes words, so
# each line is walked and boundingBoxForRange asked for the box of every
# run of non-space characters in it. boundingBox is normalised with a
# bottom-left origin; the caller flips it, because only the caller knows
# the image height.
_JXA = r"""
ObjC.import('Vision');
ObjC.import('Foundation');

function run(argv) {
    var handler = $.VNImageRequestHandler.alloc.initWithURLOptions(
        $.NSURL.fileURLWithPath($(argv[0])), $());
    var req = $.VNRecognizeTextRequest.alloc.init;
    req.recognitionLevel = 0;          // accurate; fast is worse and barely quicker
    req.usesLanguageCorrection = true;
    handler.performRequestsError($([req]), Ref());
    var out = [];
    var results = req.results;
    if (results) {
        for (var i = 0; i < results.count; i++) {
            var obs = results.objectAtIndex(i);
            var cands = obs.topCandidates(1);
            if (!cands || cands.count === 0) { continue; }
            var top = cands.objectAtIndex(0);
            var s = ObjC.unwrap(top.string);
            var j = 0;
            while (j < s.length) {
                while (j < s.length && /\s/.test(s[j])) { j++; }
                var start = j;
                while (j < s.length && !/\s/.test(s[j])) { j++; }
                if (j <= start) { break; }
                var ro = top.boundingBoxForRangeError(
                    $.NSMakeRange(start, j - start), Ref());
                if (!ro) { continue; }
                var bb = ro.boundingBox;
                out.push({t: s.substring(start, j), c: top.confidence,
                          x: bb.origin.x, y: bb.origin.y,
                          w: bb.size.width, h: bb.size.height});
            }
        }
    }
    return JSON.stringify(out);
}
"""

# Windows.Media.Ocr from Windows PowerShell 5.1, which every Windows 10 and
# 11 install has. WinRT calls are async; AsTask turns each one into a .NET
# Task the script can wait on. Words come back in image pixels with a
# top-left origin already. The engine uses the languages in the user's
# Windows settings, falling back to English, and gives no confidence, so
# nothing is filtered on it. Output is forced to UTF-8 so non-ASCII words
# survive the console.
_PS1 = r"""
param([string]$Path)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
    Select-Object -First 1
function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $task.Wait(-1)
    $task.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($engine -eq $null) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
        [Windows.Globalization.Language]::new('en-US'))
}
if ($engine -eq $null) {
    [Console]::Error.WriteLine('No text recognition language is installed in Windows.')
    exit 2
}
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$stream.Dispose()
$out = New-Object System.Collections.ArrayList
foreach ($line in $result.Lines) {
    foreach ($word in $line.Words) {
        $r = $word.BoundingRect
        $null = $out.Add([ordered]@{ t = $word.Text; x = $r.X; y = $r.Y; w = $r.Width; h = $r.Height })
    }
}
ConvertTo-Json -InputObject @($out) -Compress
"""

_MIN_CONF = 0.3     # below this Vision is guessing at noise
_scripts: dict = {}


def _powershell() -> Optional[str]:
    """Windows PowerShell 5.1, which ships with every Windows 10 and 11.
    Found by its fixed path rather than PATH, so a PowerShell 7 install
    (which cannot load WinRT types this way) is never picked up instead."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    exe = os.path.join(root, "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    return exe if os.path.exists(exe) else None


def _windows_ok() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().major >= 10 and _powershell() is not None
    except AttributeError:
        return False


def available() -> bool:
    """Can this machine recognise text in an image?"""
    if sys.platform == "darwin":
        return os.path.exists("/usr/bin/osascript")
    return _windows_ok()


def _script_file(suffix: str = ".js", body: str = _JXA) -> str:
    """The script on disk. Written once per Anki session, into the temp dir."""
    path = _scripts.get(suffix)
    if path and os.path.exists(path):
        return path
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="pdfocc_ocr_")
    # PowerShell 5.1 reads a script without a byte order mark as the
    # system code page, so the Windows one gets one
    with os.fdopen(fd, "w", encoding="utf-8-sig" if suffix == ".ps1"
                   else "utf-8") as fh:
        fh.write(body)
    _scripts[suffix] = path
    return path


def _run_recogniser(png: str, timeout: float) -> subprocess.CompletedProcess:
    if sys.platform == "darwin":
        return subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", _script_file(), png],
            capture_output=True, text=True, timeout=timeout,
        )
    return subprocess.run(
        [_powershell() or "powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass",
         "-File", _script_file(".ps1", _PS1), png],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
        # no console window flashing up over Anki on every Detect
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def recognize(img: QImage, timeout: float = 30.0) -> list:
    """Read the text in an image. Returns one (x, y, w, h, text) per word.

    Coordinates are top-left origin, matching everything else the add-on
    passes around. Raises OcrError if the recogniser could not be run at all;
    an image with no readable text is an empty list, not an error.
    """
    if not available():
        raise OcrError("Text recognition needs macOS or Windows 10 or later.")
    if img.isNull() or img.width() < 4 or img.height() < 4:
        return []

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".png", prefix="pdfocc_ocr_")
        os.close(fd)
        if not img.save(tmp, "PNG"):
            raise OcrError("Could not write the image out for recognition.")
        try:
            proc = _run_recogniser(tmp, timeout)
        except subprocess.TimeoutExpired:
            raise OcrError(
                "Text recognition timed out. The first run on a Mac loads "
                "its models and can take half a minute. Try again."
                if sys.platform == "darwin" else
                "Text recognition timed out. Try again."
            )
        except OSError as exc:
            raise OcrError(f"Could not start text recognition: {exc}")
        if proc.returncode != 0:
            raise OcrError((proc.stderr or "The recogniser failed.").strip())
        try:
            obs = json.loads(proc.stdout or "[]")
        except ValueError:
            raise OcrError("Could not read the recogniser's output.")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    if sys.platform != "darwin":
        return _from_windows(obs)

    w_px, h_px = float(img.width()), float(img.height())
    out = []
    for o in obs:
        try:
            if float(o["c"]) < _MIN_CONF:
                continue
            x = float(o["x"]) * w_px
            # Vision's origin is bottom-left, the image's is top-left
            y = (1.0 - float(o["y"]) - float(o["h"])) * h_px
            out.append((x, y, float(o["w"]) * w_px, float(o["h"]) * h_px,
                        o.get("t", "")))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda r: (round(r[1]), r[0]))
    return out


def _from_windows(obs) -> list:
    """Windows already reports pixels with a top-left origin."""
    if isinstance(obs, dict):       # one word: older PowerShell unwraps it
        obs = [obs]
    out = []
    for o in obs or []:
        try:
            w, h = float(o["w"]), float(o["h"])
            if w <= 0 or h <= 0:
                continue
            out.append((float(o["x"]), float(o["y"]), w, h, o.get("t", "")))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda r: (round(r[1]), r[0]))
    return out


def warm_up() -> None:
    """Nudge macOS into loading its models, without waiting for it.

    Fire and forget: nothing here reads the result or reports a failure,
    because the only thing it buys is that the first real call is quick.
    """
    if sys.platform != "darwin" or not available():
        return
    img = QImage(64, 32, QImage.Format.Format_RGB32)
    img.fill(0xFFFFFF)
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="pdfocc_warm_")
    os.close(fd)
    if not img.save(tmp, "PNG"):
        return
    try:
        subprocess.Popen(
            ["/usr/bin/osascript", "-l", "JavaScript", _script_file(), tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
