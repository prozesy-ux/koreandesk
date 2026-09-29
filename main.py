import base64
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from PySide6.QtCore import QPointF, QSize, Qt, QThread, QTimer, QUrl, Signal
    from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QGridLayout,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPushButton,
        QProgressBar,
        QSizePolicy,
        QSpacerItem,
        QStackedWidget,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    print("PySide6 is required. Install it with: pip install -r requirements.txt")
    raise SystemExit(1) from exc

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    HAS_MULTIMEDIA = True
except ImportError:
    HAS_MULTIMEDIA = False


APP_NAME = "YTN - 1.0"
APP_SUBTITLE = "AI News Studio"
DEMO_LICENSE = "YTN-2026-PRO-DEMO"
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LICENSE_CACHE = DATA_DIR / "license_cache.json"
PROJECTS_DIR = BASE_DIR / "projects"
YOUTUBE_URL_RE = re.compile(r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+", re.IGNORECASE)


class QuietYtdlpLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def machine_id() -> str:
    raw = f"{platform.node()}::{uuid.getnode()}::{platform.system()}::{platform.machine()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def license_hash(key: str, hwid: str) -> str:
    return hashlib.sha256(f"ytn-v1::{key.strip()}::{hwid}".encode("utf-8")).hexdigest()


@dataclass
class LicenseResult:
    ok: bool
    message: str


class LicenseManager:
    def __init__(self) -> None:
        self.hwid = machine_id()

    def activate(self, key: str, remember: bool) -> LicenseResult:
        cleaned = key.strip().upper()
        if cleaned != DEMO_LICENSE:
            return LicenseResult(False, "License key is not valid for this machine.")

        if remember:
            DATA_DIR.mkdir(exist_ok=True)
            payload = {
                "product": APP_NAME,
                "license_hash": license_hash(cleaned, self.hwid),
                "hwid": self.hwid,
                "activated_at": datetime.now(timezone.utc).isoformat(),
                "mode": "offline-cache",
            }
            LICENSE_CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        return LicenseResult(True, "License activated.")

    def cached_key_is_valid(self, key: str) -> bool:
        if not LICENSE_CACHE.exists():
            return False
        try:
            payload = json.loads(LICENSE_CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            payload.get("hwid") == self.hwid
            and payload.get("license_hash") == license_hash(key.strip().upper(), self.hwid)
        )


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:90] or "YTN Project"


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def format_duration(seconds) -> str:
    if not seconds:
        return "--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class VideoInfoWorker(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, url: str, cookie_source: str = "none", cookies_file: str = "") -> None:
        super().__init__()
        self.url = url
        self.cookie_source = cookie_source
        self.cookies_file = cookies_file

    def run(self) -> None:
        try:
            import yt_dlp

            last_error = None
            for source, cookies_file in cookie_attempts(self.cookie_source, self.cookies_file):
                try:
                    options = {
                        "quiet": True,
                        "no_warnings": True,
                        "logger": QuietYtdlpLogger(),
                        "skip_download": True,
                        "extract_flat": False,
                        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv", "mweb", "web_embedded", "web"]}},
                        "http_headers": {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                            "Accept": "*/*",
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                    }
                    apply_cookie_options(options, source, cookies_file)
                    with yt_dlp.YoutubeDL(options) as ydl:
                        info = ydl.extract_info(self.url, download=False)
                    info["_ytn_cookie_source"] = source
                    self.finished_ok.emit(info)
                    return
                except Exception as exc:
                    last_error = exc
            retry_options = {
                "quiet": True,
                "no_warnings": True,
                "logger": QuietYtdlpLogger(),
                "skip_download": True,
                "extract_flat": False,
                "extractor_args": {"youtube": {"player_client": ["android"]}},
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            }
            try:
                with yt_dlp.YoutubeDL(retry_options) as ydl:
                    info = ydl.extract_info(self.url, download=False)
                info["_ytn_cookie_source"] = "android-retry"
                self.finished_ok.emit(info)
                return
            except Exception as exc:
                pass
            raise last_error or RuntimeError("Could not fetch video metadata.")
        except Exception as exc:
            self.failed.emit(str(exc))


class VideoDownloadWorker(QThread):
    progress = Signal(int, str)
    finished_ok = Signal(str, dict)
    failed = Signal(str)

    def __init__(self, url: str, output_dir: Path, resolution: str, cookie_source: str = "none", cookies_file: str = "") -> None:
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self.resolution = resolution
        self.cookie_source = cookie_source
        self.cookies_file = cookies_file
        self.cancel_requested = False

    def cancel(self) -> None:
        self.cancel_requested = True

    def run(self) -> None:
        try:
            import yt_dlp

            self.output_dir.mkdir(parents=True, exist_ok=True)
            height_limit = {
                "2160p 4K": 2160,
                "1440p": 1440,
                "1080p": 1080,
                "720p": 720,
                "Full HD 1080p": 1080,
                "HD 720p": 720,
                "480p": 480,
                "360p": 360,
            }.get(self.resolution)
            if height_limit:
                video_format = f"bestvideo[height<={height_limit}]+bestaudio/best[height<={height_limit}]"
            else:
                video_format = "bestvideo+bestaudio/best"

            def hook(data: dict) -> None:
                if self.cancel_requested:
                    raise RuntimeError("Download cancelled.")
                status = data.get("status")
                if status == "downloading":
                    total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                    downloaded = data.get("downloaded_bytes") or 0
                    percent = int(downloaded * 100 / total) if total else 0
                    speed = data.get("_speed_str", "").strip()
                    eta = data.get("_eta_str", "").strip()
                    self.progress.emit(percent, f"Downloading... {percent}%  Speed {speed or '--'}  ETA {eta or '--'}")
                elif status == "finished":
                    self.progress.emit(100, "Download complete. Merging MP4...")

            last_error = None
            for source, cookies_file in cookie_attempts(self.cookie_source, self.cookies_file):
                try:
                    self.progress.emit(0, f"Trying download mode: {source}")
                    options = {
                        "format": video_format,
                        "outtmpl": str(self.output_dir / "%(title).180s.%(ext)s"),
                        "merge_output_format": "mp4",
                        "progress_hooks": [hook],
                        "quiet": True,
                        "no_warnings": True,
                        "logger": QuietYtdlpLogger(),
                        "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv", "mweb", "web_embedded", "web"]}},
                        "http_headers": {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                            "Accept": "*/*",
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                        "continuedl": True,
                        "noprogress": True,
                    }
                    apply_cookie_options(options, source, cookies_file)
                    with yt_dlp.YoutubeDL(options) as ydl:
                        result = ydl.extract_info(self.url, download=True)
                        filename = ydl.prepare_filename(result)
                    final_path = str(Path(filename).with_suffix(".mp4"))
                    result["_ytn_cookie_source"] = source
                    self.finished_ok.emit(final_path, result)
                    return
                except Exception as exc:
                    last_error = exc
            self.progress.emit(0, "All client attempts failed. Final android-only retry...")
            retry_options = {
                "format": video_format,
                "outtmpl": str(self.output_dir / "%(title).180s.%(ext)s"),
                "merge_output_format": "mp4",
                "progress_hooks": [hook],
                "quiet": True,
                "no_warnings": True,
                "logger": QuietYtdlpLogger(),
                "extractor_args": {"youtube": {"player_client": ["android"]}},
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                "continuedl": True,
                "noprogress": True,
            }
            try:
                with yt_dlp.YoutubeDL(retry_options) as ydl:
                    result = ydl.extract_info(self.url, download=True)
                    filename = ydl.prepare_filename(result)
                final_path = str(Path(filename).with_suffix(".mp4"))
                result["_ytn_cookie_source"] = "android-retry"
                self.finished_ok.emit(final_path, result)
                return
            except Exception as exc:
                pass
            raise last_error or RuntimeError("Could not download video.")
        except Exception as exc:
            self.failed.emit(str(exc))


class FfmpegWorker(QThread):
    finished_ok = Signal(str, str)
    failed = Signal(str)

    def __init__(self, command: list[str], output_path: Path, label: str) -> None:
        super().__init__()
        self.command = command
        self.output_path = output_path
        self.label = label

    def run(self) -> None:
        try:
            result = subprocess.run(
                self.command,
                capture_output=True,
                text=True,
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "FFmpeg failed").strip()[-1200:])
            self.finished_ok.emit(str(self.output_path), self.label)
        except Exception as exc:
            self.failed.emit(str(exc))


class FfmpegFrameWorker(QThread):
    finished_ok = Signal(str, str)
    failed = Signal(str)

    def __init__(self, video_path: str, timestamp: float, filter_args: list[str], preview_dir: Path) -> None:
        super().__init__()
        self.video_path = video_path
        self.timestamp = max(timestamp, 0.0)
        self.filter_args = filter_args
        self.preview_dir = preview_dir

    def run(self) -> None:
        try:
            self.preview_dir.mkdir(parents=True, exist_ok=True)
            before = self.preview_dir / "before.jpg"
            after = self.preview_dir / "after.jpg"
            base = [ffmpeg_exe(), "-y", "-ss", f"{self.timestamp:.3f}", "-i", self.video_path]
            commands = [
                [*base, "-frames:v", "1", "-q:v", "2", str(before)],
                [*base, *self.filter_args, "-frames:v", "1", "-q:v", "2", str(after)],
            ]
            for command in commands:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or "FFmpeg preview failed").strip()[-800:])
            self.finished_ok.emit(str(before), str(after))
        except Exception as exc:
            self.failed.emit(str(exc))


class AiThumbnailWorker(QThread):
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, api_key: str, source_path: Path, prompt: str, output_path: Path) -> None:
        super().__init__()
        self.api_key = api_key
        self.source_path = source_path
        self.prompt = prompt
        self.output_path = output_path

    @staticmethod
    def _multipart(boundary: str, fields: list[tuple[str, str, str]]) -> bytes:
        chunks = []
        for name, value, filepath in fields:
            if filepath:
                with open(filepath, "rb") as handle:
                    image_bytes = handle.read()
                chunks.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"; filename="thumbnail.jpg"\r\n'
                    "Content-Type: image/jpeg\r\n\r\n".encode()
                    + image_bytes
                    + b"\r\n"
                )
            else:
                chunks.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n".encode()
                )
        chunks.append(f"--{boundary}--\r\n".encode())
        return b"".join(chunks)

    def run(self) -> None:
        try:
            boundary = "----YTNAIBoundary" + uuid.uuid4().hex
            body = self._multipart(
                boundary,
                [
                    ("model", "gpt-image-1", ""),
                    ("prompt", self.prompt, ""),
                    ("size", "auto", ""),
                    ("n", "1", ""),
                    ("image", "", str(self.source_path)),
                ],
            )
            request = urllib.request.Request(
                "https://api.openai.com/v1/images/edits",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read().decode("utf-8"))
            item = (payload.get("data") or [{}])[0]
            image_bytes = None
            if item.get("b64_json"):
                image_bytes = base64.b64decode(item["b64_json"])
            elif item.get("url"):
                with urllib.request.urlopen(item["url"], timeout=120) as img_response:
                    image_bytes = img_response.read()
            if not image_bytes:
                raise RuntimeError("The AI provider returned no image. Check your API key and credits.")
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_path.write_bytes(image_bytes)
            self.finished_ok.emit(str(self.output_path))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            self.failed.emit(f"AI API error {exc.code}: {detail or exc.reason}")
        except Exception as exc:
            self.failed.emit(str(exc))


def ffmpeg_exe() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def probe_duration(path: Path) -> float:
    return probe_video_meta(path).get("duration", 0.0)


def probe_has_audio(path: Path) -> bool:
    return probe_video_meta(path).get("audio", False)


def probe_video_size(path: Path) -> tuple[int, int]:
    meta = probe_video_meta(path)
    return meta.get("width", 0), meta.get("height", 0)


def probe_video_meta(path: Path) -> dict:
    try:
        result = subprocess.run(
            [ffmpeg_exe(), "-hide_banner", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = result.stderr or ""
        duration = 0.0
        width = height = 0
        has_audio = False
        for line in output.splitlines():
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", line)
            if match:
                h, m, s = match.groups()
                duration = int(h) * 3600 + int(m) * 60 + float(s)
            if "Audio:" in line:
                has_audio = True
            vmatch = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if vmatch and ("Video:" in line or "Stream" in line):
                width, height = int(vmatch.group(1)), int(vmatch.group(2))
        return {"duration": duration, "width": width, "height": height, "audio": has_audio}
    except Exception:
        return {"duration": 0.0, "width": 0, "height": 0, "audio": False}


def escape_filter_path(path: str) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def sanitize_watermark_text(text: str) -> str:
    return re.sub(r"[':\\\\\[\]]", "", text or "").strip() or "YTN"


def apply_cookie_options(options: dict, cookie_source: str, cookies_file: str) -> None:
    source = cookie_source.strip().lower()
    if source in {"chrome", "edge", "firefox"}:
        options["cookiesfrombrowser"] = (source,)
    elif source == "cookies.txt" and cookies_file:
        options["cookiefile"] = cookies_file


def browser_cookie_available(browser: str) -> bool:
    home = Path.home()
    checks = {
        "edge": [
            home / "AppData/Local/Microsoft/Edge/User Data/Default/Network/Cookies",
            home / "AppData/Local/Microsoft/Edge/User Data/Default/Cookies",
        ],
        "chrome": [
            home / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies",
            home / "AppData/Local/Google/Chrome/User Data/Default/Cookies",
        ],
        "firefox": [
            home / "AppData/Roaming/Mozilla/Firefox/Profiles",
        ],
    }
    paths = checks.get(browser, [])
    if browser == "firefox":
        return any(path.exists() and any(path.glob("*/cookies.sqlite")) for path in paths)
    return any(path.exists() for path in paths)


def browser_is_running(browser: str) -> bool:
    process_names = {
        "edge": ["msedge.exe"],
        "chrome": ["chrome.exe"],
        "firefox": ["firefox.exe"],
    }.get(browser, [])
    if not process_names:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    output = result.stdout.lower()
    return any(name.lower() in output for name in process_names)


def best_cookie_attempts(cookies_file: str = "") -> list[tuple[str, str]]:
    attempts = [("none", "")]
    for browser in ["edge", "chrome", "firefox"]:
        if browser_cookie_available(browser):
            attempts.append((browser, ""))
    if cookies_file:
        attempts.append(("cookies.txt", cookies_file))
    return attempts


def cookie_attempts(cookie_source: str, cookies_file: str) -> list[tuple[str, str]]:
    source = cookie_source.strip().lower()
    if source == "auto":
        return best_cookie_attempts(cookies_file)
    if source == "cookies.txt" and cookies_file:
        return [(source, cookies_file)]
    if source in {"chrome", "edge", "firefox"}:
        return [(source, "")]
    return [("none", "")]


def friendly_download_error(message: str) -> str:
    lower = message.lower()
    if "sign in to confirm" in lower or "not a bot" in lower:
        return "YouTube blocked public download. Use Advanced Access: select Edge/Chrome cookies or cookies.txt, then try again."
    if "could not copy chrome cookie database" in lower:
        return "Chrome cookies are locked. Close Chrome completely, or use Edge/cookies.txt."
    if "could not find firefox cookies" in lower:
        return "Firefox cookies were not found. Use Edge/Chrome/cookies.txt."
    if "dpi" in lower and ("decrypt" in lower or "cookie" in lower):
        return "Browser cookies could not be decrypted (DPAPI). Over RDP this is common: close the browser, reopen it once in this session, or export cookies.txt and use it."
    if any(word in lower for word in ["decrypt", "decryption", "nsig", "po token", "player client", "signature"]):
        return "YouTube signature decryption failed on this network (common over RDP/VPN). yt-dlp will try mobile/TV clients automatically; if it still fails, use the cookies.txt option or try again from the local machine."
    if "googlevideo.com" in lower or "user rights to view this page" in lower or "unable to download data" in lower:
        return "Google denied video delivery to this network/IP (common on RDP or datacenter/VPN IPs). Sign in to YouTube in Edge/Chrome on THIS machine, close the browser, then use 'Use Edge cookies' or 'Use Chrome cookies'. If it keeps failing, download from the local machine instead."
    return f"Download error: {message}"


class ParticleBackground(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.tick = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.advance)
        self.timer.start(33)

    def advance(self) -> None:
        self.tick = (self.tick + 1) % 10000
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0.0, QColor("#07111f"))
        gradient.setColorAt(0.42, QColor("#131a3b"))
        gradient.setColorAt(0.76, QColor("#27205d"))
        gradient.setColorAt(1.0, QColor("#051625"))
        painter.fillRect(self.rect(), gradient)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(43, 182, 255, 36))
        painter.drawEllipse(QPointF(self.width() * 0.18, self.height() * 0.18), 180, 180)
        painter.setBrush(QColor(161, 96, 255, 34))
        painter.drawEllipse(QPointF(self.width() * 0.82, self.height() * 0.72), 240, 240)
        painter.setBrush(QColor(18, 232, 199, 26))
        painter.drawEllipse(QPointF(self.width() * 0.56, self.height() * 0.22), 125, 125)

        pen = QPen(QColor(138, 202, 255, 48), 1)
        painter.setPen(pen)
        for i in range(34):
            x = (i * 97 + self.tick * (1 + i % 3)) % max(self.width(), 1)
            y = (i * 53 + self.tick * (2 + i % 2)) % max(self.height(), 1)
            painter.drawEllipse(QPointF(x, y), 1.8 + (i % 4), 1.8 + (i % 4))
            if i % 3 == 0:
                painter.drawLine(QPointF(x, y), QPointF((x + 86) % self.width(), (y + 34) % self.height()))


class LoginView(QWidget):
    def __init__(self, license_manager: LicenseManager, on_success) -> None:
        super().__init__()
        self.license_manager = license_manager
        self.on_success = on_success
        self.setObjectName("loginRoot")

        root = QGridLayout(self)
        root.setContentsMargins(48, 36, 48, 36)
        root.setColumnStretch(0, 1)
        root.setColumnStretch(1, 0)
        root.setColumnStretch(2, 1)
        root.setRowStretch(0, 1)
        root.setRowStretch(2, 1)

        card = QFrame()
        card.setObjectName("glassCard")
        card.setFixedWidth(460)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(42)
        shadow.setOffset(0, 22)
        shadow.setColor(QColor(0, 0, 0, 130))
        card.setGraphicsEffect(shadow)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(38, 34, 38, 28)
        layout.setSpacing(18)

        logo = QLabel("YTN")
        logo.setObjectName("logo")
        logo.setAlignment(Qt.AlignCenter)
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        version = QLabel("Version 1.0")
        version.setObjectName("version")
        version.setAlignment(Qt.AlignCenter)

        self.license_input = QLineEdit()
        self.license_input.setPlaceholderText("License Key")
        self.license_input.setText(DEMO_LICENSE)
        self.license_input.returnPressed.connect(self.login)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password (optional)")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.returnPressed.connect(self.login)

        checks = QHBoxLayout()
        self.remember = QCheckBox("Remember License")
        self.remember.setChecked(True)
        self.auto_login = QCheckBox("Auto Login")
        checks.addWidget(self.remember)
        checks.addWidget(self.auto_login)

        button_row = QHBoxLayout()
        activate = QPushButton("Activate")
        activate.setObjectName("primaryButton")
        activate.clicked.connect(self.login)
        login = QPushButton("Login")
        login.clicked.connect(self.login)
        exit_button = QPushButton("Exit")
        exit_button.setObjectName("quietButton")
        exit_button.clicked.connect(QApplication.instance().quit)
        button_row.addWidget(activate)
        button_row.addWidget(login)
        button_row.addWidget(exit_button)

        self.status = QLabel("Machine-bound offline demo license ready.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)

        layout.addWidget(logo)
        layout.addWidget(subtitle)
        layout.addWidget(version)
        layout.addSpacing(12)
        layout.addWidget(self.license_input)
        layout.addWidget(self.password_input)
        layout.addLayout(checks)
        layout.addLayout(button_row)
        layout.addWidget(self.status)

        root.addWidget(card, 1, 1)

    def login(self) -> None:
        key = self.license_input.text()
        if self.license_manager.cached_key_is_valid(key):
            self.on_success()
            return
        result = self.license_manager.activate(key, self.remember.isChecked())
        self.status.setText(result.message)
        if result.ok:
            self.on_success()


class DashboardView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.project_tabs = None
        self.video_info = {}
        self.current_project_dir = None
        self.current_video_path = ""
        self.info_worker = None
        self.download_worker = None
        self.ffmpeg_worker = None
        self.preview_worker = None
        self.ai_thumb_worker = None
        self.nav_buttons = {}
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(238)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(22, 26, 22, 22)
        nav.setSpacing(10)
        brand = QLabel("YTN - 1.0")
        brand.setObjectName("navBrand")
        nav.addWidget(brand)
        for item in ["Dashboard", "Projects", "OpenAI API Key"]:
            button = QPushButton(item)
            button.setObjectName("navButtonActive" if item == "Dashboard" else "navButton")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, name=item: self.show_page(name))
            self.nav_buttons[item] = button
            nav.addWidget(button)
        nav.addItem(QSpacerItem(1, 1, QSizePolicy.Minimum, QSizePolicy.Expanding))

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(34, 28, 34, 30)
        main_layout.setSpacing(16)
        self.content_stack = QStackedWidget()
        self.dashboard_page = self.create_dashboard_page()
        self.projects_page = self.create_projects_page()
        self.api_page = self.create_api_page()
        self.content_stack.addWidget(self.dashboard_page)
        self.content_stack.addWidget(self.projects_page)
        self.content_stack.addWidget(self.api_page)
        main_layout.addWidget(self.content_stack, 1)

        root.addWidget(sidebar)
        root.addWidget(main)

    def create_projects_page(self) -> QWidget:
        page = QWidget()
        content = QVBoxLayout(page)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(16)
        title = QLabel("Project Workflow")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Download -> AI Content Studio -> Video Editor")
        subtitle.setObjectName("pageSubtitle")
        content.addWidget(title)
        content.addWidget(subtitle)

        project_bar = QHBoxLayout()
        self.project_name_input = QLineEdit()
        self.project_name_input.setPlaceholderText("Project name")
        self.project_name_input.setText(self.load_settings().get("last_project_name", "My News Project"))
        self.create_project_button = QPushButton("Create Project")
        self.create_project_button.setObjectName("primaryButton")
        self.create_project_button.clicked.connect(self.create_project)
        self.project_status = QLabel("Create a project, then download a YouTube video.")
        self.project_status.setObjectName("metaLine")
        project_bar.addWidget(self.project_name_input, 2)
        project_bar.addWidget(self.create_project_button)
        project_bar.addWidget(self.project_status, 3)
        content.addLayout(project_bar)

        self.project_tabs = QTabWidget()
        self.project_tabs.setObjectName("projectTabs")
        self.project_tabs.addTab(self.create_download_tab(), "1  Download Video")
        self.project_tabs.addTab(self.create_content_tab(), "2  AI Content Studio")
        self.project_tabs.addTab(self.create_editor_tab(), "3  Video Editor")
        content.addWidget(self.project_tabs, 1)
        return page

    def create_dashboard_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        title = QLabel("Dashboard")
        title.setObjectName("pageTitle")
        self.dashboard_summary = QLabel("")
        self.dashboard_summary.setObjectName("pageSubtitle")
        self.projects_list = QListWidget()
        self.projects_list.setObjectName("projectList")
        self.projects_list.itemClicked.connect(lambda item: self.open_project_folder(Path(item.data(Qt.UserRole))))
        self.projects_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.projects_list.customContextMenuRequested.connect(self.show_project_menu)
        refresh = QPushButton("Refresh Projects")
        refresh.setObjectName("primaryButton")
        refresh.clicked.connect(self.refresh_dashboard)
        open_button = QPushButton("Open Project")
        open_button.setObjectName("primaryButton")
        open_button.clicked.connect(self.open_selected_project)
        delete = QPushButton("Delete Project")
        delete.setObjectName("dangerButton")
        delete.clicked.connect(self.delete_selected_project)
        button_row = QHBoxLayout()
        button_row.addWidget(refresh)
        button_row.addWidget(open_button)
        button_row.addWidget(delete)
        button_row.addStretch()
        layout.addWidget(title)
        layout.addWidget(self.dashboard_summary)
        layout.addWidget(self.projects_list, 1)
        layout.addLayout(button_row)
        self.refresh_dashboard()
        return page

    def create_api_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        title = QLabel("OpenAI API Key")
        title.setObjectName("pageTitle")
        self.openai_key_input = QLineEdit()
        self.openai_key_input.setPlaceholderText("Paste OpenAI API key")
        self.openai_key_input.setEchoMode(QLineEdit.Password)
        save = QPushButton("Save API Key")
        save.setObjectName("primaryButton")
        save.clicked.connect(self.save_openai_key)
        self.api_status = QLabel("Stored locally in data/settings.json.")
        self.api_status.setObjectName("metaLine")
        layout.addWidget(title)
        layout.addWidget(self.openai_key_input)
        layout.addWidget(save)
        layout.addWidget(self.api_status)
        layout.addStretch()
        self.load_openai_key()
        return page

    def show_page(self, name: str) -> None:
        pages = {"Dashboard": 0, "Projects": 1, "OpenAI API Key": 2}
        self.content_stack.setCurrentIndex(pages[name])
        if name == "Dashboard":
            self.refresh_dashboard()
        for label, button in self.nav_buttons.items():
            button.setObjectName("navButtonActive" if label == name else "navButton")
            button.style().unpolish(button)
            button.style().polish(button)

    def make_panel(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        panel = QFrame()
        panel.setObjectName("dashCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("cardHeading")
        layout.addWidget(heading)
        return panel, layout

    def local_projects(self) -> list[Path]:
        PROJECTS_DIR.mkdir(exist_ok=True)
        return sorted([path for path in PROJECTS_DIR.iterdir() if path.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)

    def refresh_dashboard(self) -> None:
        projects = self.local_projects()
        videos = sum(len(list((project / "Videos").glob("*.mp4"))) for project in projects if (project / "Videos").exists())
        storage_bytes = 0
        for project in projects:
            for file_path in project.rglob("*"):
                if file_path.is_file():
                    storage_bytes += file_path.stat().st_size
        storage_mb = storage_bytes / (1024 * 1024)
        self.dashboard_summary.setText(f"Projects: {len(projects)}   Videos: {videos}   Local Storage: {storage_mb:.1f} MB")
        self.projects_list.clear()
        for project in projects:
            video_count = len(list((project / "Videos").glob("*.mp4"))) if (project / "Videos").exists() else 0
            item_text = f"{project.name}    Videos: {video_count}    Path: {project}"
            self.projects_list.addItem(item_text)
            self.projects_list.item(self.projects_list.count() - 1).setData(Qt.UserRole, str(project))

    def delete_selected_project(self) -> None:
        item = self.projects_list.currentItem()
        if item is None:
            self.dashboard_summary.setText("Select a project in the list first, then click Delete Project.")
            return
        self.delete_project(Path(item.data(Qt.UserRole)))

    def open_selected_project(self) -> None:
        item = self.projects_list.currentItem()
        if item is None:
            self.dashboard_summary.setText("Select a project in the list first, then click Open Project.")
            return
        self.open_project_folder(Path(item.data(Qt.UserRole)))

    def show_project_menu(self, pos) -> None:
        item = self.projects_list.itemAt(pos)
        if item is None:
            return
        path = Path(item.data(Qt.UserRole))
        self.projects_list.setCurrentItem(item)
        menu = QMenu(self)
        open_action = menu.addAction("Open Project")
        folder_action = menu.addAction("Open Folder")
        menu.addSeparator()
        delete_action = menu.addAction("Delete Project")
        chosen = menu.exec(self.projects_list.mapToGlobal(pos))
        if chosen == open_action:
            self.open_project_folder(path)
        elif chosen == folder_action:
            self.open_folder_external(path)
        elif chosen == delete_action:
            self.delete_project(path)

    def open_folder_external(self, path: Path) -> None:
        try:
            subprocess.Popen(["explorer", str(path)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            self.dashboard_summary.setText(f"Could not open folder: {exc}")

    def delete_project(self, path: Path) -> None:
        confirm = QMessageBox.question(
            self,
            "Delete Project",
            f"Delete project permanently?\n\n{path.name}\n\nThis removes the folder and all its videos, thumbnails, and data. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.release_media_for(path)
        last_error = None
        for attempt in range(4):
            try:
                shutil.rmtree(path)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                QApplication.processEvents()
                if attempt < 3:
                    time.sleep(0.3)
                    QApplication.processEvents()
        if last_error is not None:
            hint = ""
            if getattr(last_error, "winerror", None) == 32:
                hint = "\n\nThe video is still open. Close the Video Editor preview or Windows player, then try again."
            self.dashboard_summary.setText(f"Could not delete project: {last_error}{hint}")
            return
        if self.current_project_dir == path:
            self.current_project_dir = None
            self.current_video_path = ""
        self.dashboard_summary.setText(f"Deleted project: {path.name}")
        self.refresh_dashboard()

    def release_media_for(self, path: Path) -> None:
        try:
            if self.media_player is not None:
                self.media_player.stop()
                self.media_player.setSource(QUrl())
                QApplication.processEvents()
        except Exception:
            pass
        try:
            if self.current_video_path and path != self.current_project_dir:
                src = Path(self.current_video_path)
                if src.exists() and src.is_relative_to(path):
                    self.current_video_path = ""
        except Exception:
            pass

    def project_metadata_path(self) -> Path:
        if self.current_project_dir is None:
            self.create_project()
        return self.current_project_dir / "metadata.json"

    def save_project_metadata(self) -> None:
        if self.current_project_dir is None:
            return
        try:
            payload = {
                "video_info": json_safe(self.video_info or {}),
                "original_text": self.original_text.toPlainText(),
                "versions": [editor.toPlainText() for editor in self.version_editors],
                "thumbnail_path": str(self.current_project_dir / "Thumbnail" / "original_thumbnail.jpg"),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            (self.current_project_dir / "metadata.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self.project_status.setText(f"Could not save project data: {exc}")

    def load_project_metadata(self) -> dict:
        path = self.project_metadata_path()
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def open_project_folder(self, path: Path) -> None:
        self.current_project_dir = Path(path)
        self.project_name_input.setText(self.current_project_dir.name)
        self.project_status.setText(f"Project ready: {self.current_project_dir}")
        self.download_status.setText(f"Opened project: {self.current_project_dir}")
        videos_dir = self.current_project_dir / "Videos"
        videos = sorted(videos_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True) if videos_dir.exists() else []
        meta = self.load_project_metadata()
        if meta.get("video_info"):
            self.video_info = meta.get("video_info") or {}
            self.apply_video_info(self.video_info)
            self.original_text.setPlainText(meta.get("original_text", ""))
            for index, editor in enumerate(self.version_editors):
                editor.setPlainText(meta.get("versions", [""] * 3)[index] if meta.get("versions") else "")
            thumb_path = Path(meta.get("thumbnail_path", ""))
            if thumb_path.exists():
                self.show_thumbnail(thumb_path)
            else:
                self.thumb_preview.setText("Original thumbnail preview")
        else:
            self.video_info = {}
            self.original_text.setPlainText("No saved AI content for this project yet. Download a video to load it.")
            for editor in self.version_editors:
                editor.setPlainText("")
            self.thumb_preview.setText("Original thumbnail preview")
        if videos:
            self.current_video_path = str(videos[0])
            self.open_video_in_editor(self.current_video_path)
        else:
            self.current_video_path = ""
            self.video_info_label.setText("No video in this project yet.")
        self.refresh_exports_list()
        self.show_page("Projects")
        self.project_tabs.setCurrentIndex(1 if meta.get("video_info") else 0)

    def settings_path(self) -> Path:
        DATA_DIR.mkdir(exist_ok=True)
        return DATA_DIR / "settings.json"

    def load_settings(self) -> dict:
        try:
            return json.loads(self.settings_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_settings(self, settings: dict) -> None:
        self.settings_path().write_text(json.dumps(settings, indent=2), encoding="utf-8")

    def load_openai_key(self) -> None:
        key = self.load_settings().get("openai_api_key", "")
        self.openai_key_input.setText(key)

    def save_openai_key(self) -> None:
        settings = self.load_settings()
        settings["openai_api_key"] = self.openai_key_input.text().strip()
        self.save_settings(settings)
        self.api_status.setText("OpenAI API key saved locally.")

    def create_download_tab(self) -> QWidget:
        tab = QWidget()
        root = QGridLayout(tab)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(16)

        source_panel, source = self.make_panel("YouTube Source")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste YouTube video URL")
        self.save_location_input = QLineEdit()
        self.save_location_input.setPlaceholderText("Save folder")
        self.save_location_input.setText(str(PROJECTS_DIR))
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(["Best Quality MP4", "Full HD 1080p", "HD 720p", "480p", "360p"])
        self.cookie_combo = QComboBox()
        self.cookie_combo.addItems(["Auto: public then browser cookies", "Public only", "Use Edge cookies", "Use Chrome cookies", "Use Firefox cookies", "Use cookies.txt"])
        self.cookie_combo.hide()
        self.cookies_file_input = QLineEdit()
        self.cookies_file_input.setPlaceholderText("Advanced Access: cookies.txt path if needed")
        self.cookies_file_input.hide()
        source.addWidget(self.url_input)
        source.addWidget(self.resolution_combo)
        source.addWidget(self.save_location_input)
        source.addWidget(self.cookie_combo)
        source.addWidget(self.cookies_file_input)
        self.access_status = QLabel("Access check: public mode ready.")
        self.access_status.setObjectName("metaLine")
        self.access_status.setWordWrap(True)
        self.access_status.hide()

        actions = QHBoxLayout()
        browse = QPushButton("Save Folder")
        self.download_button = QPushButton("Download Video")
        self.download_button.setObjectName("primaryButton")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        browse.clicked.connect(self.pick_save_folder)
        self.download_button.clicked.connect(self.download_video)
        self.cancel_button.clicked.connect(self.cancel_download)
        actions.addWidget(browse)
        actions.addWidget(self.download_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        source.addLayout(actions)

        self.download_progress = QProgressBar()
        self.download_progress.setValue(0)
        self.download_progress.setFormat("Download Progress  %p%")
        self.download_status = QLabel("Project created. Paste YouTube URL and click Download Video.")
        self.download_status.setObjectName("metaLine")
        self.download_status.setWordWrap(True)
        source.addWidget(self.download_progress)
        source.addWidget(self.download_status)

        preview_panel, preview = self.make_panel("Video Preview")
        self.preview_box = QLabel("Video info will appear here after download starts")
        self.preview_box.setObjectName("previewBox")
        self.preview_box.setAlignment(Qt.AlignCenter)
        self.preview_box.setWordWrap(True)
        preview.addWidget(self.preview_box, 1)

        meta_panel, meta = self.make_panel("Source Metadata")
        self.meta_labels = {}
        for key, label in [
            ("title", "Title: waiting for video"),
            ("channel", "Channel: --"),
            ("duration", "Duration: --"),
            ("resolution", "Resolution: --"),
            ("views", "Views: --"),
            ("output", "Output: MP4"),
            ("next", "Next: after download, move to AI Content Studio"),
        ]:
            item = QLabel(label)
            item.setObjectName("metaLine")
            item.setWordWrap(True)
            self.meta_labels[key] = item
            meta.addWidget(item)
        meta.addStretch()

        root.addWidget(source_panel, 0, 0, 1, 2)
        root.addWidget(preview_panel, 1, 0)
        root.addWidget(meta_panel, 1, 1)
        root.setColumnStretch(0, 2)
        root.setColumnStretch(1, 1)
        root.setRowStretch(1, 1)
        self.update_access_status()
        return tab

    def create_project(self) -> None:
        base = Path(self.save_location_input.text().strip() or PROJECTS_DIR)
        project_name = safe_name(self.project_name_input.text() or "My News Project")
        self.project_name_input.setText(project_name)
        settings = self.load_settings()
        settings["last_project_name"] = project_name
        self.save_settings(settings)
        self.current_project_dir = base / project_name
        for folder in ["Videos", "Thumbnail", "Exports", "Captions", "Assets", "Logs"]:
            (self.current_project_dir / folder).mkdir(parents=True, exist_ok=True)
        self.project_status.setText(f"Project ready: {self.current_project_dir}")
        self.download_status.setText("Project created. Paste YouTube URL and click Download Video.")
        self.original_text.setPlainText(
            f"Project created: {self.current_project_dir.name}\n\n"
            "Paste a YouTube URL in the Download Video tab and click Download Video.\n"
            "The original title, description, tags, and SEO data will load here automatically."
        )
        for editor in self.version_editors:
            editor.setPlainText("")
        self.thumb_preview.setText("Original thumbnail preview")
        self.video_info_label.setText("No video in this project yet. Download a video to preview it here.")
        videos_dir = self.current_project_dir / "Videos"
        videos = sorted(videos_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True) if videos_dir.exists() else []
        if videos:
            self.current_video_path = str(videos[0])
            self.open_video_in_editor(self.current_video_path)
        else:
            self.current_video_path = ""
        self.refresh_exports_list()
        self.refresh_dashboard()

    def pick_save_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose project save folder", self.save_location_input.text())
        if folder:
            self.save_location_input.setText(folder)

    def pick_cookies_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "Choose cookies.txt", "", "Cookie Files (*.txt);;All Files (*)")
        if file_path:
            self.cookies_file_input.setText(file_path)
            self.cookie_combo.setCurrentText("Use cookies.txt")
            self.update_access_status()

    def update_access_status(self) -> None:
        found = []
        for browser in ["edge", "chrome", "firefox"]:
            if browser_cookie_available(browser):
                state = "open" if browser_is_running(browser) else "ready"
                found.append(f"{browser.title()} {state}")
        if self.cookies_file_input.text().strip():
            found.append("cookies.txt ready")
        if found:
            self.access_status.setText("Access check: public + " + ", ".join(found))
        else:
            self.access_status.setText("Access check: public only. For blocked videos, sign in to YouTube in Edge/Chrome or choose cookies.txt.")

    def cookie_settings(self) -> tuple[str, str]:
        label = self.cookie_combo.currentText()
        if label == "Auto: public then browser cookies":
            return "auto", ""
        if label == "Public only":
            return "none", ""
        if label == "Use Edge cookies":
            return "edge", ""
        if label == "Use Chrome cookies":
            return "chrome", ""
        if label == "Use Firefox cookies":
            return "firefox", ""
        if label == "Use cookies.txt":
            return "cookies.txt", self.cookies_file_input.text().strip()
        return "none", ""

    def validate_video_url(self) -> None:
        url = self.url_input.text().strip()
        if not YOUTUBE_URL_RE.match(url):
            self.download_status.setText("Invalid YouTube URL. Paste a youtube.com or youtu.be link.")
            self.download_button.setEnabled(False)
            return
        self.download_button.setEnabled(False)
        cookie_source, cookies_file = self.cookie_settings()
        if cookie_source == "cookies.txt" and not cookies_file:
            self.download_status.setText("Choose a cookies.txt file or select browser sign-in.")
            self.download_button.setEnabled(True)
            return
        self.download_status.setText("Fetching video metadata with yt-dlp...")
        self.preview_box.setText("Loading video information...")
        self.info_worker = VideoInfoWorker(url, cookie_source, cookies_file)
        self.info_worker.finished_ok.connect(self.on_video_info)
        self.info_worker.failed.connect(self.on_video_info_error)
        self.info_worker.start()

    def on_video_info(self, info: dict) -> None:
        self.video_info = info
        self.apply_video_info(info)
        auth_mode = info.get("_ytn_cookie_source", "none")
        mode_text = "public" if auth_mode == "none" else f"{auth_mode} sign-in"
        self.download_status.setText(f"URL validated with {mode_text}. Ready to download MP4.")
        self.download_button.setEnabled(True)

    def apply_video_info(self, info: dict) -> None:
        title = info.get("title") or "Untitled"
        channel = info.get("channel") or info.get("uploader") or "--"
        duration = format_duration(info.get("duration"))
        width = info.get("width") or "--"
        height = info.get("height") or "--"
        views = info.get("view_count")
        views_text = f"{views:,}" if isinstance(views, int) else "--"
        self.meta_labels["title"].setText(f"Title: {title}")
        self.meta_labels["channel"].setText(f"Channel: {channel}")
        self.meta_labels["duration"].setText(f"Duration: {duration}")
        self.meta_labels["resolution"].setText(f"Resolution: {width}x{height}")
        self.meta_labels["views"].setText(f"Views: {views_text}")
        self.preview_box.setText(f"{title}\n\n{channel}\n{duration} | {width}x{height}")

    def on_video_info_error(self, message: str) -> None:
        self.download_status.setText(friendly_download_error(message))
        self.preview_box.setText("Could not load video preview.")
        self.download_button.setEnabled(True)

    def project_output_dir(self) -> Path:
        if self.current_project_dir is None:
            self.create_project()
        return self.current_project_dir / "Videos"

    def download_video(self) -> None:
        url = self.url_input.text().strip()
        if not YOUTUBE_URL_RE.match(url):
            self.download_status.setText("Invalid YouTube URL. Paste a youtube.com or youtu.be link.")
            return
        output_dir = self.project_output_dir()
        self.download_progress.setValue(0)
        self.download_status.setText("Starting download...")
        self.update_access_status()
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        cookie_source, cookies_file = self.cookie_settings()
        if cookie_source == "cookies.txt" and not cookies_file:
            self.download_status.setText("Choose a cookies.txt file or select browser sign-in.")
            self.download_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            return
        self.download_worker = VideoDownloadWorker(url, output_dir, self.resolution_combo.currentText(), cookie_source, cookies_file)
        self.download_worker.progress.connect(self.on_download_progress)
        self.download_worker.finished_ok.connect(self.on_download_done)
        self.download_worker.failed.connect(self.on_download_error)
        self.download_worker.finished.connect(self.on_download_finished)
        self.download_worker.start()

    def on_download_progress(self, value: int, message: str) -> None:
        self.download_progress.setValue(max(0, min(value, 100)))
        self.download_status.setText(message)

    def on_download_done(self, file_path: str, info: dict) -> None:
        self.download_progress.setValue(100)
        self.current_video_path = file_path
        if info:
            self.video_info = info
            self.on_video_info(info)
        self.download_status.setText(f"Downloaded MP4: {file_path}")
        self.meta_labels["next"].setText("Next: moved to AI Content Studio")
        self.populate_content_studio()
        self.open_video_in_editor(file_path)
        self.refresh_dashboard()
        self.project_tabs.setCurrentIndex(1)

    def on_download_error(self, message: str) -> None:
        self.download_status.setText(friendly_download_error(message))

    def on_download_finished(self) -> None:
        self.download_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def cancel_download(self) -> None:
        if self.download_worker and self.download_worker.isRunning():
            self.download_worker.cancel()
            self.download_status.setText("Cancelling download...")

    def populate_content_studio(self) -> None:
        info = self.video_info or {}
        title = info.get("title") or "Untitled"
        description = info.get("description") or ""
        tags = info.get("tags") or []
        channel = info.get("channel") or info.get("uploader") or "--"
        category = info.get("categories") or info.get("category") or "--"
        publish_date = info.get("upload_date") or "--"
        language = info.get("language") or "--"
        thumbnail = info.get("thumbnail") or "--"
        chapters = info.get("chapters") or []
        captions = ", ".join((info.get("subtitles") or {}).keys()) or "--"
        tags_text = ", ".join(tags) if isinstance(tags, list) else str(tags)
        chapters_text = "\n".join(chapter.get("title", "") for chapter in chapters) if chapters else "--"
        self.original_text.setPlainText(
            "\n".join(
                [
                    f"Original Title: {title}",
                    f"Channel Name: {channel}",
                    f"Category: {category}",
                    f"Publish Date: {publish_date}",
                    f"Language: {language}",
                    f"Captions: {captions}",
                    f"Thumbnail: {thumbnail}",
                    "",
                    "Original Description:",
                    description or "--",
                    "",
                    f"Original Tags: {tags_text or '--'}",
                    "",
                    "SEO Information:",
                    f"Primary keyword base: {title}",
                    f"Chapters: {chapters_text}",
                ]
            )
        )
        self.thumb_preview.setText(f"Original thumbnail loaded\n\n{thumbnail}")
        self.save_original_thumbnail()
        self.generate_news_versions()
        self.save_project_metadata()

    def generate_news_versions(self) -> None:
        info = self.video_info or {}
        title = info.get("title") or "Breaking News Update"
        channel = info.get("channel") or info.get("uploader") or "News Channel"
        base_keywords = [word.strip(".,:;!?") for word in title.split() if len(word.strip(".,:;!?")) > 3]
        keywords = ", ".join(dict.fromkeys(base_keywords[:10])) or "news, latest update, breaking news"
        templates = [
            ("Breaking Update", "fast, clear, headline-focused"),
            ("Full Report", "detailed, trustworthy, SEO-focused"),
            ("Latest News", "human, natural, high-CTR"),
        ]
        for index, editor in enumerate(self.version_editors):
            label, tone = templates[index]
            new_title = title
            editor.setPlainText(
                "\n".join(
                    [
                        f"New Title: {new_title}",
                        "",
                        "New Description:",
                        f"{channel} brings a clear news-style update on {title}. Watch the full report for the latest details, context, and key moments.",
                        "",
                        f"SEO Keywords: {keywords}",
                        f"Meta Tags: {keywords}, youtube news, news update",
                        f"Hashtags: #{' #'.join(base_keywords[:5]) if base_keywords else 'News #BreakingNews #Update'}",
                        f"Tags: {keywords}",
                        "",
                        "Chapters:",
                        "00:00 Main story",
                        "01:00 Key details",
                        "02:00 Full context",
                        "",
                        "Pinned Comment:",
                        "What is your opinion on this latest update? Share your thoughts below.",
                        "",
                        "Community Post:",
                        f"New update published: {title}. Watch now for the full story.",
                        "",
                        f"Style: {tone}",
                    ]
                )
            )
        self.save_project_metadata()

    def show_seo_score(self) -> None:
        current = self.version_tabs.currentWidget()
        text = current.toPlainText() if current else ""
        checks = {
            "Title present": "New Title:" in text,
            "Description present": "New Description:" in text,
            "SEO keywords included": "SEO Keywords:" in text,
            "Hashtags included": "Hashtags:" in text,
            "Chapters included": "Chapters:" in text,
            "Pinned comment included": "Pinned Comment:" in text,
            "Community post included": "Community Post:" in text,
            "Call to action": "opinion" in text.lower() or "watch now" in text.lower() or "share" in text.lower(),
            "High-CTR phrasing": "breaking" in text.lower() or "latest" in text.lower() or "update" in text.lower(),
        }
        passed = sum(checks.values())
        score = int(round(passed * 100 / len(checks)))
        lines = [f"{name}: {'✓' if ok else '✗'}" for name, ok in checks.items()]
        self.project_status.setText(f"SEO Score {score}/100  ({passed}/{len(checks)} checks)  |  Generate all versions first if blank.")
        QMessageBox.information(self, "SEO Score", f"SEO Score: {score}/100\n\n" + "\n".join(lines))

    def next_to_editor(self) -> None:
        self.save_project_metadata()
        self.project_tabs.setCurrentIndex(2)

    def open_video_in_editor(self, file_path: str) -> None:
        path = Path(file_path)
        if not path.exists():
            self.video_info_label.setText(f"Video file not found: {path}")
            return
        self.current_video_path = str(path)
        if self.media_player is not None:
            self.media_player.setSource(QUrl.fromLocalFile(str(path)))
            self.media_player.play()
        self.video_info_label.setText(f"Video loaded: {path.name}\nFolder: {path.parent}")

    def play_video(self) -> None:
        if self.media_player is not None:
            self.media_player.play()
        else:
            self.open_video_external()

    def pause_video(self) -> None:
        if self.media_player is not None:
            self.media_player.pause()

    def toggle_play(self) -> None:
        if self.media_player is None:
            self.open_video_external()
            return
        if self.media_player.playbackState() == QMediaPlayer.PlayingState:
            self.media_player.pause()
        else:
            self.media_player.play()

    def update_play_overlay(self, state) -> None:
        if getattr(self, "play_overlay", None) is None:
            return
        if state == QMediaPlayer.PlayingState:
            self.play_overlay.setText("❚❚")
        else:
            self.play_overlay.setText("▶")

    def on_player_error(self, error, error_string) -> None:
        if error != QMediaPlayer.NoError:
            self.video_info_label.setText(f"Player error: {error_string}")

    def open_video_external(self) -> None:
        if not self.current_video_path:
            self.video_info_label.setText("No video loaded yet.")
            return
        path = Path(self.current_video_path)
        if not path.exists():
            self.video_info_label.setText(f"Video file not found: {path}")
            return
        try:
            subprocess.Popen(["cmd", "/c", "start", "", str(path)], shell=False)
        except Exception as exc:
            self.video_info_label.setText(f"Could not open video:\n{exc}")

    def require_video(self) -> Path | None:
        if not self.current_video_path:
            self.editor_status.setText("No video loaded. Download a video first.")
            return None
        path = Path(self.current_video_path)
        if not path.exists():
            self.editor_status.setText(f"Video file not found: {path}")
            return None
        return path

    def export_path(self, label: str, suffix: str = ".mp4") -> Path:
        if self.current_project_dir is None:
            self.create_project()
        exports = self.current_project_dir / "Exports"
        exports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return exports / f"{safe_name(label)}-{stamp}{suffix}"

    def _stage(self, label: str, args: list[str], preview: list[str] | None = None, suffix: str = ".mp4") -> None:
        self.pending_edit = (label, args, suffix)
        self.apply_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        if preview:
            self.preview_args = preview
            self.editor_status.setText(f"{label} selected. Rendering live preview...")
            self.run_frame_preview(preview)
        else:
            self.preview_args = None
            self.clear_frame_preview()
            self.editor_status.setText(f"{label} ready. Click Apply to process the video.")

    def apply_edit(self) -> None:
        if not self.pending_edit:
            self.editor_status.setText("Choose a tool first, then click Apply.")
            return
        source = self.require_video()
        if source is None:
            return
        label, args, suffix = self.pending_edit
        output = self.export_path(label, suffix)
        command = [ffmpeg_exe(), "-y", "-i", str(source), *args, str(output)]
        self.editor_status.setText(f"Processing {label}...")
        self.apply_button.setEnabled(False)
        self.ffmpeg_worker = FfmpegWorker(command, output, label)
        self.ffmpeg_worker.finished_ok.connect(self.on_ffmpeg_done)
        self.ffmpeg_worker.failed.connect(self.on_ffmpeg_error)
        self.ffmpeg_worker.finished.connect(self.on_ffmpeg_finished)
        self.ffmpeg_worker.start()

    def cancel_edit(self) -> None:
        self.pending_edit = None
        self.preview_args = None
        self.apply_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.clear_frame_preview()
        self.editor_status.setText("Edit cancelled. Live preview cleared.")

    def on_ffmpeg_done(self, output_path: str, label: str) -> None:
        self.editor_status.setText(f"{label} complete: {output_path}")
        self.open_video_in_editor(output_path)
        self.refresh_dashboard()
        self.refresh_exports_list()

    def on_ffmpeg_error(self, message: str) -> None:
        self.editor_status.setText(f"Edit failed: {message}")

    def on_ffmpeg_finished(self) -> None:
        self.apply_button.setEnabled(True)
        self.cancel_button.setEnabled(True)

    def run_frame_preview(self, filter_args: list[str]) -> None:
        source = self.require_video()
        if source is None:
            return
        ts = 0.0
        if HAS_MULTIMEDIA and self.media_player is not None and self.media_player.position() > 0:
            ts = self.media_player.position() / 1000.0
        self.preview_ts = ts
        if self.current_project_dir is None:
            self.create_project()
        preview_dir = self.current_project_dir / "Exports" / "preview"
        self.preview_worker = FfmpegFrameWorker(str(source), ts, filter_args, preview_dir)
        self.preview_worker.finished_ok.connect(self.on_frame_preview_ready)
        self.preview_worker.failed.connect(self.on_frame_preview_error)
        self.preview_worker.start()

    def refresh_preview(self) -> None:
        if self.preview_args:
            self.run_frame_preview(self.preview_args)
        elif self.pending_edit:
            self.editor_status.setText("This tool has no frame preview. Click Apply to process the full video.")
        else:
            self.editor_status.setText("Select a tool first to preview it.")

    def on_frame_preview_ready(self, before_path: str, after_path: str) -> None:
        for label, path in ((self.before_label, before_path), (self.after_label, after_path)):
            pix = QPixmap(path)
            if not pix.isNull():
                label.setPixmap(pix.scaled(label.width(), label.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.editor_status.setText(f"Live preview ready at {self.preview_ts:.1f}s. Before | After shown. Click Apply to render the full video.")

    def on_frame_preview_error(self, message: str) -> None:
        self.clear_frame_preview()
        self.editor_status.setText(f"Preview failed: {message}")

    def clear_frame_preview(self) -> None:
        self.before_label.clear()
        self.after_label.clear()
        self.before_label.setText("Before")
        self.after_label.setText("After")

    def refresh_exports_list(self) -> None:
        if not self.current_project_dir:
            return
        exports = self.current_project_dir / "Exports"
        if not exports.exists():
            return
        files = sorted(exports.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        if files:
            names = "\n".join(f"• {f.name}" for f in files)
            self.exports_list.setText(f"Recent exports:\n{names}")
        else:
            self.exports_list.setText("No exports yet. Apply an edit to create one.")

    def trim_video(self) -> None:
        start, ok1 = QInputDialog.getText(self, "Trim", "Start (seconds or MM:SS):", text="00:00:00")
        if not ok1:
            return
        duration, ok2 = QInputDialog.getText(self, "Trim", "Duration (seconds or MM:SS):", text="00:00:10")
        if not ok2:
            return
        self._stage("Trim", ["-ss", start.strip() or "0", "-t", duration.strip() or "10", "-c", "copy"])

    def split_video(self) -> None:
        source = self.require_video()
        if source is None:
            return
        total = probe_duration(source)
        if total <= 0:
            self.editor_status.setText("Could not read the video duration for Split.")
            return
        half = total / 2.0
        part1 = self.export_path("Split Part 1")
        part2 = self.export_path("Split Part 2")
        command = [
            ffmpeg_exe(), "-y", "-i", str(source),
            "-ss", "0", "-t", f"{half:.3f}", "-c", "copy", str(part1),
            "-ss", f"{half:.3f}", "-c", "copy", str(part2),
        ]
        self.pending_edit = None
        self.preview_args = None
        self.apply_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.clear_frame_preview()
        self.editor_status.setText("Splitting video into two parts...")
        self.ffmpeg_worker = FfmpegWorker(command, part1, "Split Part 1")
        self.ffmpeg_worker.finished_ok.connect(self.on_ffmpeg_done)
        self.ffmpeg_worker.failed.connect(self.on_ffmpeg_error)
        self.ffmpeg_worker.start()

    def merge_video(self) -> None:
        source = self.require_video()
        if source is None:
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose videos to merge AFTER the current one", str(source.parent), "Video Files (*.mp4 *.mkv *.mov *.avi)"
        )
        if not files:
            return
        list_path = DATA_DIR / "concat_list.txt"
        paths = [source, *[Path(f) for f in files]]
        list_path.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in paths), encoding="utf-8"
        )
        output = self.export_path("Merge")
        command = [ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output)]
        self.pending_edit = None
        self.preview_args = None
        self.apply_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.clear_frame_preview()
        self.editor_status.setText("Merging videos...")
        self.ffmpeg_worker = FfmpegWorker(command, output, "Merge")
        self.ffmpeg_worker.finished_ok.connect(self.on_ffmpeg_done)
        self.ffmpeg_worker.failed.connect(self.on_ffmpeg_error)
        self.ffmpeg_worker.start()

    def speed_video(self) -> None:
        choice, ok = QInputDialog.getItem(self, "Speed Control", "Playback speed:", ["0.5x", "0.75x", "1.5x", "2x", "4x"], 3, False)
        if not ok:
            return
        factor = float(choice.rstrip("x"))
        source = self.require_video()
        if source is None:
            return
        if probe_has_audio(source):
            atempo = []
            remaining = factor
            while remaining > 2.0:
                atempo.append("atempo=2.0")
                remaining /= 2.0
            atempo.append(f"atempo={remaining:.4f}")
            args = ["-filter_complex", f"[0:v]setpts={1.0 / factor:.6f}*PTS[v];[0:a]{','.join(atempo)}[a]", "-map", "[v]", "-map", "[a]"]
        else:
            args = ["-vf", f"setpts={1.0 / factor:.6f}*PTS"]
        self._stage("Speed Control", args)

    def rotate_video(self) -> None:
        choice, ok = QInputDialog.getItem(
            self, "Rotate", "Rotation:", ["90° clockwise", "90° counter-clockwise", "180°", "270° clockwise"], 0, False
        )
        if not ok:
            return
        vf = {
            "90° clockwise": "transpose=1",
            "90° counter-clockwise": "transpose=2",
            "180°": "transpose=1,transpose=1",
            "270° clockwise": "transpose=2,transpose=2",
        }[choice]
        self._stage("Rotate", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def flip_video(self) -> None:
        choice, ok = QInputDialog.getItem(self, "Flip", "Direction:", ["Horizontal", "Vertical"], 0, False)
        if not ok:
            return
        vf = "hflip" if choice == "Horizontal" else "vflip"
        self._stage("Flip", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def crop_video(self) -> None:
        source = self.require_video()
        if source is None:
            return
        width, height = probe_video_size(source)
        default_w = str(width) if width else "1280"
        default_h = str(height) if height else "720"
        new_width, ok1 = QInputDialog.getText(self, "Crop", "Width (px):", text=default_w)
        if not ok1:
            return
        new_height, ok2 = QInputDialog.getText(self, "Crop", "Height (px):", text=default_h)
        if not ok2:
            return
        cw = new_width.strip() or default_w
        ch = new_height.strip() or default_h
        if width and height and (int(cw) > width or int(ch) > height):
            self.editor_status.setText(f"Crop {cw}x{ch} is larger than the source {width}x{height}. Enter smaller dimensions.")
            return
        vf = f"crop={cw}:{ch}:(in_w-{cw})/2:(in_h-{ch})/2"
        self._stage("Crop", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def zoom_video(self) -> None:
        choice, ok = QInputDialog.getItem(self, "Zoom", "Zoom level:", ["1.2x", "1.5x", "2x"], 1, False)
        if not ok:
            return
        z = float(choice.rstrip("x"))
        vf = (
            f"scale=trunc(iw*{z}/2)*2:trunc(ih*{z}/2)*2,"
            f"crop=trunc(iw/{z}/2)*2:trunc(ih/{z}/2)*2:"
            f"(iw-trunc(iw/{z}/2)*2)/2:(ih-trunc(ih/{z}/2)*2)/2"
        )
        self._stage("Zoom", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def color_correct(self) -> None:
        brightness, ok1 = QInputDialog.getText(self, "Color Correction", "Brightness (-1 to 1):", text="0")
        if not ok1:
            return
        contrast, ok2 = QInputDialog.getText(self, "Color Correction", "Contrast (0 to 2):", text="1")
        if not ok2:
            return
        saturation, ok3 = QInputDialog.getText(self, "Color Correction", "Saturation (0 to 3):", text="1.2")
        if not ok3:
            return
        vf = f"eq=brightness={brightness.strip() or 0}:contrast={contrast.strip() or 1}:saturation={saturation.strip() or 1}"
        self._stage("Color Correction", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def effects_preset(self) -> None:
        preset, ok = QInputDialog.getItem(
            self, "Effects", "Choose a preset:", ["Grayscale", "Sepia", "Negative", "VHS", "Warm", "Cool", "Sharpen"], 0, False
        )
        if not ok:
            return
        filters = {
            "Grayscale": "hue=s=0",
            "Sepia": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
            "Negative": "negate",
            "VHS": "noise=alls=15:allf=t,hue=s=3",
            "Warm": "colorbalance=rs=.25:gs=.05:bs=-.25",
            "Cool": "colorbalance=rs=-.25:bs=.25",
            "Sharpen": "unsharp=5:5:1.0",
        }
        vf = filters[preset]
        self._stage("Effects", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def green_screen(self) -> None:
        color, ok = QInputDialog.getText(self, "Green Screen", "Chroma key color (RRGGBB, default 00FF00):", text="00FF00")
        if not ok:
            return
        vf = f"chromakey=0x{color.strip().lstrip('#') or '00FF00'}:0.15:0.2"
        self._stage("Green Screen", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def blur_background(self) -> None:
        self._stage("Blur Background", ["-vf", "boxblur=8:1", "-c:a", "copy"], preview=["-vf", "boxblur=8:1"])

    def add_watermark(self) -> None:
        text, ok1 = QInputDialog.getText(self, "Watermark", "Watermark text:", text="YTN")
        if not ok1:
            return
        size, ok2 = QInputDialog.getItem(self, "Watermark", "Text size:", ["24", "32", "48", "64"], 1, False)
        if not ok2:
            return
        safe = sanitize_watermark_text(text)
        vf = f"drawtext=fontfile='C\\:/Windows/Fonts/arialbd.ttf':text='{safe}':fontcolor=white:fontsize={size}:x=w-tw-24:y=24:shadowx=2:shadowy=2:borderw=1:bordercolor=black"
        self._stage("Watermark", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def add_subtitles(self) -> None:
        srt_file, _ = QFileDialog.getOpenFileName(
            self, "Choose subtitle file", str(Path.home()), "Subtitle Files (*.srt *.ass *.ssa);;All Files (*)"
        )
        if not srt_file:
            return
        escaped = escape_filter_path(srt_file)
        vf = f"subtitles='{escaped}'"
        self._stage("Subtitles", ["-vf", vf, "-c:a", "copy"], preview=["-vf", vf])

    def add_transition(self) -> None:
        source = self.require_video()
        if source is None:
            return
        duration, ok = QInputDialog.getItem(self, "Fade Transition", "Fade duration (seconds):", ["0.5", "1", "2"], 1, False)
        if not ok:
            return
        total = probe_duration(source)
        d = float(duration)
        start_out = max(total - d, 0.0) if total > 0 else 0.0
        vf = f"fade=t=in:st=0:d={d},fade=t=out:st={start_out:.3f}:d={d}"
        self._stage("Fade Transition", ["-vf", vf, "-c:a", "copy"])

    def noise_removal(self) -> None:
        self._stage("Noise Removal", ["-af", "afftdn=nf=-25", "-c:v", "copy"])

    def audio_enhance(self) -> None:
        self._stage("Audio Enhancement", ["-af", "volume=1.5,dynaudnorm", "-c:v", "copy"])

    def voice_enhance(self) -> None:
        self._stage("Voice Enhancement", ["-af", "highpass=f=100,lowpass=f=8000,volume=1.5", "-c:v", "copy"])

    def music_overlay(self) -> None:
        source = self.require_video()
        if source is None:
            return
        music_file, _ = QFileDialog.getOpenFileName(
            self, "Choose background music", str(Path.home()), "Audio Files (*.mp3 *.wav *.m4a *.aac *.flac)"
        )
        if not music_file:
            return
        if probe_has_audio(source):
            args = [
                "-i", music_file,
                "-filter_complex", "[1:a]volume=0.35[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=2[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy",
            ]
        else:
            args = ["-i", music_file, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-shortest"]
        self._stage("Music Overlay", args)

    def extract_audio(self) -> None:
        self._stage("Extract Audio", ["-vn", "-c:a", "mp3"], suffix=".mp3")

    def show_thumbnail(self, path: Path) -> None:
        try:
            pix = QPixmap(str(path))
            if pix.isNull():
                self.thumb_preview.setText(str(path))
                return
            self.thumb_preview.setPixmap(pix.scaled(self.thumb_preview.width(), self.thumb_preview.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception as exc:
            self.thumb_preview.setText(f"Thumbnail load failed:\n{exc}")

    def save_original_thumbnail(self) -> None:
        thumbnail = (self.video_info or {}).get("thumbnail")
        if not thumbnail:
            self.thumb_preview.setText("No thumbnail URL found. Download video metadata first.")
            return
        if self.current_project_dir is None:
            self.create_project()
        thumbnail_dir = self.current_project_dir / "Thumbnail"
        thumbnail_dir.mkdir(parents=True, exist_ok=True)
        output = thumbnail_dir / "original_thumbnail.jpg"
        try:
            urllib.request.urlretrieve(thumbnail, output)
        except Exception as exc:
            self.thumb_preview.setText(f"Thumbnail save failed:\n{exc}")
            return
        self.show_thumbnail(output)
        self.thumb_preview.setText(f"Thumbnail saved\n\n{output}")

    def enhance_thumbnail_image(self, source: Path, output: Path, brightness: float = 0.0, contrast: float = 1.1, saturation: float = 1.18, sharpness: float = 1.5, upscale: bool = True) -> Path:
        from PIL import Image, ImageEnhance, ImageOps

        image = Image.open(source).convert("RGB")
        image = ImageOps.exif_transpose(image)
        if upscale:
            target = 1280
            if max(image.size) < target:
                scale = target / max(image.size)
                image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
        image = ImageEnhance.Color(image).enhance(saturation)
        image = ImageEnhance.Contrast(image).enhance(contrast)
        image = ImageEnhance.Brightness(image).enhance(1.0 + brightness)
        image = ImageEnhance.Sharpness(image).enhance(sharpness)
        image = ImageOps.autocontrast(image, cutoff=1)
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, "JPEG", quality=95)
        return output

    def ai_generate_thumbnail(self, source: Path) -> bool:
        key = self.load_settings().get("openai_api_key", "").strip()
        if not key:
            return False
        info = self.video_info or {}
        title = info.get("title") or "News update"
        topic = title[:120]
        prompt = (
            f"Make a better, premium YouTube news thumbnail based on this exact original image. "
            f"Keep the exact same person, face, identity, expression, and topic. Do NOT change who is shown. "
            f"Only improve: lighting, sharpness, color, contrast, composition, background polish, text readability, "
            f"overall quality and visual impact. Subject: {topic}. Make it a high-resolution, clear, eye-catching "
            f"professional news thumbnail. Do not add different faces or change the person."
        )
        output = self.current_project_dir / "Thumbnail" / "ai_generated_thumbnail.jpg"
        self.thumb_preview.setText("AI is generating a new improved thumbnail...")
        self.project_status.setText("AI thumbnail generation started (uses your OpenAI key).")
        self.ai_thumb_worker = AiThumbnailWorker(key, source, prompt, output)
        self.ai_thumb_worker.finished_ok.connect(self.on_ai_thumbnail_done)
        self.ai_thumb_worker.failed.connect(self.on_ai_thumbnail_error)
        self.ai_thumb_worker.start()
        return True

    def on_ai_thumbnail_done(self, output_path: str) -> None:
        self.show_thumbnail(Path(output_path))
        self.thumb_preview.setText(f"AI generated new thumbnail (same face, better quality)\n\n{output_path}")
        self.project_status.setText("AI thumbnail generated and saved.")
        self.save_project_metadata()

    def on_ai_thumbnail_error(self, message: str) -> None:
        self.thumb_preview.setText(f"AI thumbnail failed: {message}")
        self.project_status.setText("AI thumbnail failed. Check your OpenAI API key/credits, or use the local enhance.")

    def improve_thumbnail(self) -> None:
        source = self.current_project_dir / "Thumbnail" / "original_thumbnail.jpg" if self.current_project_dir else None
        if source is None or not source.exists():
            self.thumb_preview.setText("Download the original thumbnail first (Download Video loads it automatically).")
            return
        if self.ai_generate_thumbnail(source):
            return
        output = self.current_project_dir / "Thumbnail" / "ai_improved.jpg"
        try:
            self.enhance_thumbnail_image(source, output, contrast=1.12, saturation=1.18, sharpness=1.6, upscale=True)
        except Exception as exc:
            self.thumb_preview.setText(f"AI improve failed:\n{exc}")
            return
        self.show_thumbnail(output)
        self.thumb_preview.setText(f"AI improved (same face, better lighting/color/clarity)\n\n{output}")
        self.save_project_metadata()

    def auto_generate_thumbnail(self) -> None:
        source = self.current_project_dir / "Thumbnail" / "original_thumbnail.jpg" if self.current_project_dir else None
        if source is None or not source.exists():
            self.thumb_preview.setText("Download the original thumbnail first (Download Video loads it automatically).")
            return
        if self.ai_generate_thumbnail(source):
            return
        output = self.current_project_dir / "Thumbnail" / "generated_thumbnail.jpg"
        try:
            self.enhance_thumbnail_image(source, output, contrast=1.15, saturation=1.22, sharpness=1.7, upscale=True)
        except Exception as exc:
            self.thumb_preview.setText(f"Auto generate failed:\n{exc}")
            return
        self.show_thumbnail(output)
        self.thumb_preview.setText(f"Auto generated (same face, same topic, boosted clarity & color)\n\n{output}")
        self.save_project_metadata()

    def manual_generate_thumbnail(self) -> None:
        source = self.current_project_dir / "Thumbnail" / "original_thumbnail.jpg" if self.current_project_dir else None
        if source is None or not source.exists():
            self.thumb_preview.setText("Download the original thumbnail first (Download Video loads it automatically).")
            return
        brightness, ok1 = QInputDialog.getText(self, "Manual Generate", "Brightness (-0.5 to 0.5):", text="0")
        if not ok1:
            return
        contrast, ok2 = QInputDialog.getText(self, "Manual Generate", "Contrast (0.5 to 2):", text="1.15")
        if not ok2:
            return
        saturation, ok3 = QInputDialog.getText(self, "Manual Generate", "Saturation (0.5 to 2):", text="1.2")
        if not ok3:
            return
        sharpness, ok4 = QInputDialog.getText(self, "Manual Generate", "Sharpness (0.5 to 3):", text="1.6")
        if not ok4:
            return
        output = self.current_project_dir / "Thumbnail" / "manual_thumbnail.jpg"
        try:
            self.enhance_thumbnail_image(
                source, output,
                brightness=float(brightness or 0),
                contrast=float(contrast or 1.15),
                saturation=float(saturation or 1.2),
                sharpness=float(sharpness or 1.6),
                upscale=True,
            )
        except ValueError as exc:
            self.thumb_preview.setText(f"Invalid numbers: {exc}")
            return
        except Exception as exc:
            self.thumb_preview.setText(f"Manual generate failed:\n{exc}")
            return
        self.show_thumbnail(output)
        self.thumb_preview.setText(f"Manual generated\n\n{output}")
        self.save_project_metadata()

    def create_content_tab(self) -> QWidget:
        tab = QWidget()
        root = QGridLayout(tab)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(16)

        original_panel, original = self.make_panel("Original YouTube Data")
        self.original_text = QTextEdit()
        self.original_text.setPlaceholderText("Original title, description, tags, channel, category, publish date, language, captions, and SEO data will load here.")
        original.addWidget(self.original_text)

        ai_panel, ai = self.make_panel("AI Generate 3 Complete Versions")
        self.version_tabs = QTabWidget()
        self.version_tabs.setObjectName("innerTabs")
        self.version_editors = []
        for number in range(1, 4):
            editor = QTextEdit()
            editor.setPlaceholderText(
                "New Title\n\nNew Description\n\nSEO Keywords\nMeta Tags\nHashtags\nTags\nChapters\nPinned Comment\nCommunity Post"
            )
            self.version_editors.append(editor)
            self.version_tabs.addTab(editor, f"Version {number}")
        ai.addWidget(self.version_tabs, 1)
        ai_buttons = QHBoxLayout()
        generate = QPushButton("Generate 3 Versions")
        generate.setObjectName("primaryButton")
        generate.clicked.connect(self.generate_news_versions)
        seo_score = QPushButton("SEO Score")
        seo_score.clicked.connect(self.show_seo_score)
        next_button = QPushButton("Next")
        next_button.clicked.connect(self.next_to_editor)
        ai_buttons.addWidget(generate)
        ai_buttons.addWidget(seo_score)
        ai_buttons.addWidget(next_button)
        ai.addLayout(ai_buttons)

        thumb_panel, thumb = self.make_panel("Thumbnail Studio")
        self.thumb_preview = QLabel("Original thumbnail preview")
        self.thumb_preview.setObjectName("thumbnailBox")
        self.thumb_preview.setAlignment(Qt.AlignCenter)
        self.thumb_preview.setWordWrap(True)
        thumb.addWidget(self.thumb_preview, 1)
        thumb_rules = QLabel("Enhance only lighting, sharpness, color, contrast, readability, quality, resolution, and CTR. Keep face, identity, topic, and branding.")
        thumb_rules.setObjectName("metaLine")
        thumb_rules.setWordWrap(True)
        thumb.addWidget(thumb_rules)
        thumb_buttons = QHBoxLayout()
        improve = QPushButton("AI Improve Thumbnail")
        auto = QPushButton("Auto Generate")
        manual = QPushButton("Manual Generate")
        improve.clicked.connect(self.improve_thumbnail)
        auto.clicked.connect(self.auto_generate_thumbnail)
        manual.clicked.connect(self.manual_generate_thumbnail)
        thumb_buttons.addWidget(improve)
        thumb_buttons.addWidget(auto)
        thumb_buttons.addWidget(manual)
        thumb.addLayout(thumb_buttons)

        root.addWidget(original_panel, 0, 0)
        root.addWidget(ai_panel, 0, 1)
        root.addWidget(thumb_panel, 1, 0, 1, 2)
        root.setColumnStretch(0, 1)
        root.setColumnStretch(1, 2)
        root.setRowStretch(0, 2)
        root.setRowStretch(1, 1)
        return tab

    def create_editor_tab(self) -> QWidget:
        tab = QWidget()
        root = QGridLayout(tab)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(16)

        self.pending_edit = None
        self.preview_args = None
        self.preview_worker = None
        self.ffmpeg_worker = None

        tools_panel, tools = self.make_panel("Editing Tools")
        tools_grid = QGridLayout()
        tools_grid.setSpacing(6)
        tool_names = [
            "Trim", "Split", "Merge", "Speed Control",
            "Rotate", "Flip", "Crop", "Zoom",
            "Color Correction", "Effects", "Green Screen", "Blur Background",
            "Watermark", "Subtitles", "Transitions", "Noise Removal",
            "Audio Enhancement", "Voice Enhancement", "Music Overlay", "Extract Audio",
        ]
        tool_handlers = {
            "Trim": self.trim_video,
            "Split": self.split_video,
            "Merge": self.merge_video,
            "Speed Control": self.speed_video,
            "Rotate": self.rotate_video,
            "Flip": self.flip_video,
            "Crop": self.crop_video,
            "Zoom": self.zoom_video,
            "Color Correction": self.color_correct,
            "Effects": self.effects_preset,
            "Green Screen": self.green_screen,
            "Blur Background": self.blur_background,
            "Watermark": self.add_watermark,
            "Subtitles": self.add_subtitles,
            "Transitions": self.add_transition,
            "Noise Removal": self.noise_removal,
            "Audio Enhancement": self.audio_enhance,
            "Voice Enhancement": self.voice_enhance,
            "Music Overlay": self.music_overlay,
            "Extract Audio": self.extract_audio,
        }
        for index, name in enumerate(tool_names):
            btn = QPushButton(name)
            btn.setObjectName("toolButton")
            btn.clicked.connect(tool_handlers[name])
            tools_grid.addWidget(btn, index // 3, index % 3)
        tools.addLayout(tools_grid)
        tools_hint = QLabel("Click a tool, check the Before/After preview instantly, then Apply.")
        tools_hint.setObjectName("metaLine")
        tools_hint.setWordWrap(True)
        tools.addWidget(tools_hint)
        self.editor_status = QLabel("Editor ready. Every tool works: choose one to preview, then Apply.")
        self.editor_status.setObjectName("metaLine")
        self.editor_status.setWordWrap(True)
        tools.addWidget(self.editor_status)

        preview_panel, preview = self.make_panel("Preview")
        self.media_player = None
        self.video_widget = None
        if HAS_MULTIMEDIA:
            self.video_widget = QVideoWidget()
            self.video_widget.setObjectName("videoPreview")
            self.video_widget.setMinimumHeight(250)
            self.media_player = QMediaPlayer(self)
            self.audio_output = QAudioOutput(self)
            self.media_player.setAudioOutput(self.audio_output)
            self.media_player.setVideoOutput(self.video_widget)
            video_container = QWidget()
            video_layout = QVBoxLayout(video_container)
            video_layout.setContentsMargins(0, 0, 0, 0)
            video_layout.addWidget(self.video_widget, 1)
            self.play_overlay = QPushButton("▶")
            self.play_overlay.setObjectName("playOverlay")
            self.play_overlay.setCursor(Qt.PointingHandCursor)
            self.play_overlay.setFixedSize(72, 72)
            self.play_overlay.clicked.connect(self.toggle_play)
            video_layout.addWidget(self.play_overlay, 0, Qt.AlignCenter)
            self.media_player.playbackStateChanged.connect(self.update_play_overlay)
            self.media_player.errorOccurred.connect(self.on_player_error)
            preview.addWidget(video_container, 1)
        else:
            fallback_preview = QLabel("Embedded video player is not installed.\nUse Open Video to play with Windows player.")
            fallback_preview.setObjectName("videoPreview")
            fallback_preview.setAlignment(Qt.AlignCenter)
            fallback_preview.setWordWrap(True)
            preview.addWidget(fallback_preview, 1)
        self.video_info_label = QLabel("Downloaded video will open here")
        self.video_info_label.setObjectName("metaLine")
        self.video_info_label.setWordWrap(True)
        player_controls = QHBoxLayout()
        play = QPushButton("Play")
        pause = QPushButton("Pause")
        open_video = QPushButton("Open Video")
        play.clicked.connect(self.play_video)
        pause.clicked.connect(self.pause_video)
        open_video.clicked.connect(self.open_video_external)
        player_controls.addWidget(play)
        player_controls.addWidget(pause)
        player_controls.addWidget(open_video)
        player_controls.addStretch()
        preview.addWidget(self.video_info_label)
        preview.addLayout(player_controls)

        live_caption = QLabel("Live Preview  -  Before | After (instant snapshot of the current frame)")
        live_caption.setObjectName("previewCaption")
        preview.addWidget(live_caption)
        strip = QHBoxLayout()
        strip.setSpacing(10)
        before_wrap = QFrame()
        before_wrap.setObjectName("previewStrip")
        before_lay = QVBoxLayout(before_wrap)
        before_lay.setContentsMargins(8, 6, 8, 8)
        self.before_label = QLabel("Before")
        self.before_label.setObjectName("previewFrame")
        self.before_label.setAlignment(Qt.AlignCenter)
        self.before_label.setFixedSize(300, 170)
        before_lay.addWidget(self.before_label)
        after_wrap = QFrame()
        after_wrap.setObjectName("previewStrip")
        after_lay = QVBoxLayout(after_wrap)
        after_lay.setContentsMargins(8, 6, 8, 8)
        self.after_label = QLabel("After")
        self.after_label.setObjectName("previewFrame")
        self.after_label.setAlignment(Qt.AlignCenter)
        self.after_label.setFixedSize(300, 170)
        after_lay.addWidget(self.after_label)
        strip.addWidget(before_wrap)
        strip.addStretch(1)
        strip.addWidget(after_wrap)
        preview.addLayout(strip)

        action_row = QHBoxLayout()
        refresh_preview = QPushButton("Refresh Preview")
        refresh_preview.clicked.connect(self.refresh_preview)
        self.apply_button = QPushButton("Apply Edit")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_edit)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_edit)
        action_row.addWidget(refresh_preview)
        action_row.addWidget(self.apply_button)
        action_row.addWidget(self.cancel_button)
        action_row.addStretch()
        preview.addLayout(action_row)

        timeline_panel, timeline = self.make_panel("Timeline & Exports")
        timeline_box = QLabel("Video track | Audio track | Effects\nScrub the preview, choose a tool, then Apply to export.")
        timeline_box.setObjectName("timelineBox")
        timeline_box.setAlignment(Qt.AlignCenter)
        timeline.addWidget(timeline_box)
        self.exports_list = QLabel("No exports yet. Apply an edit to create one.")
        self.exports_list.setObjectName("metaLine")
        self.exports_list.setWordWrap(True)
        timeline.addWidget(self.exports_list)

        root.addWidget(tools_panel, 0, 0, 2, 1)
        root.addWidget(preview_panel, 0, 1)
        root.addWidget(timeline_panel, 1, 1)
        root.setColumnStretch(0, 1)
        root.setColumnStretch(1, 4)
        root.setRowStretch(0, 4)
        root.setRowStretch(1, 1)
        self.refresh_exports_list()
        return tab


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 740)
        self.setMinimumSize(QSize(980, 640))

        self.background = ParticleBackground()
        self.stack = QStackedWidget(self.background)
        layout = QVBoxLayout(self.background)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        self.setCentralWidget(self.background)

        self.license_manager = LicenseManager()
        self.login_view = LoginView(self.license_manager, self.show_dashboard)
        self.dashboard_view = DashboardView()
        self.stack.addWidget(self.login_view)
        self.stack.addWidget(self.dashboard_view)

        self.apply_styles()

    def show_dashboard(self) -> None:
        self.stack.setCurrentWidget(self.dashboard_view)

    def apply_styles(self) -> None:
        self.setStyleSheet(
            """
            * {
                font-family: "Segoe UI Variable", "Inter", "SF Pro Display", "Segoe UI", Arial;
                color: #f5f8ff;
                font-size: 14px;
                letter-spacing: 0;
            }
            QStackedWidget { background: transparent; }
            #glassCard {
                background: rgba(10, 18, 35, 0.76);
                border: 1px solid rgba(177, 216, 255, 0.22);
                border-radius: 22px;
            }
            #logo {
                font-size: 68px;
                font-weight: 800;
                color: #ffffff;
            }
            #subtitle {
                font-size: 22px;
                color: #9fd9ff;
                font-weight: 600;
            }
            #version, #status {
                color: #aab7cc;
                font-size: 12px;
            }
            QLineEdit {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(170, 210, 255, 0.22);
                border-radius: 12px;
                padding: 13px 15px;
                selection-background-color: #2bb6ff;
            }
            QLineEdit:focus {
                border: 1px solid #2bb6ff;
                background: rgba(255, 255, 255, 0.11);
            }
            QCheckBox {
                color: #d4deef;
                spacing: 8px;
            }
            QPushButton {
                background: rgba(255, 255, 255, 0.10);
                border: 1px solid rgba(190, 220, 255, 0.18);
                border-radius: 12px;
                padding: 12px 16px;
                font-weight: 700;
                min-height: 18px;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 0.16);
                border-color: rgba(138, 202, 255, 0.58);
            }
            #primaryButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2bb6ff, stop:1 #9d63ff);
                border: 0;
            }
            #quietButton {
                color: #bac5d8;
            }
            #sidebar {
                background: rgba(7, 13, 26, 0.90);
                border-right: 1px solid rgba(180, 210, 255, 0.12);
            }
            #navBrand {
                font-size: 24px;
                font-weight: 800;
                padding-bottom: 18px;
            }
            #navButton, #navButtonActive {
                text-align: left;
                padding: 11px 14px;
                border-radius: 10px;
                border: 0;
            }
            #navButton {
                background: transparent;
                color: #b8c5da;
            }
            #navButtonActive {
                background: rgba(43, 182, 255, 0.18);
                color: #ffffff;
            }
            #pageTitle {
                font-size: 34px;
                font-weight: 800;
            }
            #pageSubtitle {
                color: #9fb1c8;
                font-size: 15px;
                padding-bottom: 8px;
            }
            #projectTabs::pane, #innerTabs::pane {
                border: 1px solid rgba(180, 210, 255, 0.14);
                border-radius: 14px;
                background: rgba(6, 13, 27, 0.42);
                top: -1px;
            }
            QTabBar::tab {
                background: rgba(255, 255, 255, 0.07);
                color: #b8c5da;
                border: 1px solid rgba(180, 210, 255, 0.12);
                border-bottom: 0;
                padding: 12px 18px;
                margin-right: 6px;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
                font-weight: 800;
            }
            QTabBar::tab:selected {
                background: rgba(43, 182, 255, 0.20);
                color: #ffffff;
                border-color: rgba(43, 182, 255, 0.42);
            }
            #dashCard {
                background: rgba(11, 21, 40, 0.76);
                border: 1px solid rgba(180, 210, 255, 0.13);
                border-radius: 14px;
                min-height: 150px;
            }
            #cardHeading {
                color: #9fb1c8;
                font-weight: 700;
            }
            #cardValue {
                font-size: 22px;
                font-weight: 800;
                color: #ffffff;
            }
            #metaLine {
                color: #c1cee2;
                line-height: 1.4;
            }
            #previewBox, #thumbnailBox, #videoPreview, #timelineBox {
                background: rgba(0, 0, 0, 0.28);
                border: 1px solid rgba(138, 202, 255, 0.18);
                border-radius: 12px;
                color: #9fb1c8;
                font-weight: 800;
            }
            #previewBox {
                min-height: 220px;
            }
            #thumbnailBox {
                min-height: 145px;
            }
            #videoPreview {
                min-height: 250px;
                font-size: 24px;
            }
            #timelineBox {
                min-height: 86px;
            }
            #previewStrip {
                background: rgba(0, 0, 0, 0.22);
                border: 1px solid rgba(138, 202, 255, 0.16);
                border-radius: 10px;
            }
            #previewFrame {
                background: rgba(0, 0, 0, 0.45);
                border: 1px solid rgba(138, 202, 255, 0.20);
                border-radius: 8px;
                color: #9fb1c8;
                font-weight: 800;
            }
            #previewCaption {
                color: #9fd9ff;
                font-size: 13px;
                font-weight: 700;
            }
            #toolButton {
                text-align: left;
                padding: 6px 9px;
                border-radius: 8px;
                font-size: 11px;
                font-weight: 700;
            }
            QTextEdit {
                background: rgba(255, 255, 255, 0.07);
                border: 1px solid rgba(170, 210, 255, 0.18);
                border-radius: 12px;
                padding: 12px;
                selection-background-color: #2bb6ff;
            }
            QProgressBar {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(170, 210, 255, 0.18);
                border-radius: 10px;
                height: 22px;
                text-align: center;
                font-weight: 800;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #13e8c7, stop:1 #2bb6ff);
                border-radius: 9px;
            }
            QComboBox {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(170, 210, 255, 0.22);
                border-radius: 12px;
                padding: 12px 14px;
                min-height: 18px;
            }
            #projectList {
                background: rgba(11, 21, 40, 0.82);
                border: 1px solid rgba(180, 210, 255, 0.15);
                border-radius: 14px;
                padding: 6px;
                color: #dbe6f5;
            }
            #projectList::item {
                color: #dbe6f5;
                padding: 12px 10px;
                margin: 2px 0;
                border-radius: 8px;
            }
            #projectList::item:hover {
                background: rgba(43, 182, 255, 0.12);
                color: #ffffff;
            }
            #projectList::item:selected {
                background: rgba(43, 182, 255, 0.28);
                color: #ffffff;
            }
            #playOverlay {
                background: rgba(0, 0, 0, 0.55);
                border: 2px solid rgba(255, 255, 255, 0.85);
                border-radius: 36px;
                color: #ffffff;
                font-size: 26px;
                font-weight: 800;
                padding: 0;
            }
            #playOverlay:hover {
                background: rgba(43, 182, 255, 0.65);
            }
            #dangerButton {
                background: rgba(255, 82, 82, 0.16);
                border: 1px solid rgba(255, 110, 110, 0.45);
                color: #ffb3b3;
            }
            #dangerButton:hover {
                background: rgba(255, 82, 82, 0.32);
                border-color: #ff7a7a;
                color: #ffffff;
            }
            QMessageBox {
                background: #0e1a30;
                color: #f5f8ff;
            }
            QMessageBox QLabel {
                color: #f5f8ff;
            }
            QMenu {
                background: #0e1a30;
                border: 1px solid rgba(180, 210, 255, 0.25);
                border-radius: 10px;
                padding: 6px;
                color: #f5f8ff;
            }
            QMenu::item {
                padding: 8px 28px 8px 14px;
                border-radius: 6px;
                color: #dbe6f5;
            }
            QMenu::item:selected {
                background: rgba(43, 182, 255, 0.28);
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background: rgba(180, 210, 255, 0.18);
                margin: 6px 10px;
            }
            QInputDialog {
                background: #0e1a30;
                color: #f5f8ff;
            }
            QInputDialog QLabel {
                color: #f5f8ff;
            }
            QInputDialog QLineEdit, QInputDialog QComboBox {
                background: rgba(255, 255, 255, 0.08);
                color: #f5f8ff;
                border: 1px solid rgba(170, 210, 255, 0.22);
                border-radius: 10px;
                padding: 8px 10px;
                min-height: 16px;
            }
            QInputDialog QPushButton {
                padding: 8px 14px;
            }
            """
        )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("YTN Studio")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
