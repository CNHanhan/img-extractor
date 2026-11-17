# -*- coding: utf-8 -*-
"""固件远程提取工具 v2.0 —— PyQt5 单文件版

相对 v1 的改动：
  1. 多线程并行 Range 下载（按 8MB 分块 + 线程池，默认 8 并发，可调 1~16）
  2. 实时下载速度 / 已下载字节 / 剩余时间显示（滑动窗口测速）
  3. 输出目录名智能推导：百分号编码、GBK/UTF-8 乱码、Content-Disposition、
     无文件名的 API 直链，全部能推出可读目录名，不再出现空目录 / `download` / 乱码
  4. payload 头部按 manifest 长度自适应下载（不再固定 5MB）
  5. 提取过程可随时取消

用法：
  python qt_extractor.py            # 图形界面
  python qt_extractor.py --selftest # 无界面自检（返回码 0 = 全部通过）
"""

import bz2
import functools
import hashlib
import html as html_mod
import inspect
import lzma
import mmap
import os
import re
import struct
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

import urllib3

import requests
from remotezip import RemoteZip

from payload_extractor import (
    PayloadError,
    UnsupportedOpError,
    _write_extents,
    get_zip_ranges_for_partitions,
    header_info,
    list_partitions,
    parse_header,
    write_zeros,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

APP_NAME = "固件远程提取工具"
APP_VERSION = "2.0.7"

# --------------------------------------------------------------------------- #
# 主题（浅色 / 深色）
# --------------------------------------------------------------------------- #

BASE_STYLE = (
    "QPushButton { min-height: 26px; padding: 3px 10px; }"
    "QLineEdit, QSpinBox, QComboBox { min-height: 22px; }"
)

LIGHT_STYLE = BASE_STYLE + (
    "QTreeWidget, QListWidget { alternate-background-color: palette(base); }"
)

DARK_STYLE = BASE_STYLE + """
QWidget { background: #1e1e1e; color: #d4d4d4; }
QMainWindow, QDialog { background: #1e1e1e; }
QGroupBox { border: 1px solid #3c3c3c; border-radius: 4px; margin-top: 9px; }
QGroupBox::title { subcontrol-origin: margin; padding: 0 5px; color: #d4d4d4; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTreeWidget {
    background: #252526; color: #d4d4d4; border: 1px solid #3c3c3c; border-radius: 3px;
    padding: 2px;
}
QPushButton {
    background: #2d2d30; color: #d4d4d4; border: 1px solid #3c3c3c;
    border-radius: 3px; min-height: 26px; padding: 3px 10px;
}
QPushButton:hover { background: #3e3e42; }
QPushButton:pressed { background: #094771; }
QPushButton:disabled { background: #252526; color: #6b6b6b; border-color: #333; }
QCheckBox, QRadioButton { color: #d4d4d4; background: transparent; }
QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {
    background: #252526; border: 1px solid #6b6b6b;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: #0e639c; border: 1px solid #0e639c;
}
QProgressBar {
    background: #252526; border: 1px solid #3c3c3c; border-radius: 3px;
    text-align: center; color: #d4d4d4;
}
QProgressBar::chunk { background: #0e639c; border-radius: 2px; }
QHeaderView::section { background: #2d2d30; color: #d4d4d4; border: 1px solid #3c3c3c; padding: 3px; }
QTreeWidget::item, QListWidget::item { padding: 2px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #094771; color: #ffffff; }
QScrollBar:vertical, QScrollBar:horizontal { background: #1e1e1e; border: none; }
QScrollBar::handle { background: #424242; border-radius: 4px; min-height: 18px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; width: 0px; }
QToolTip { background: #2d2d30; color: #d4d4d4; border: 1px solid #3c3c3c; }
"""


def build_stylesheet(dark):
    """按主题返回完整 QSS（纯字符串函数，自检里不依赖 Qt 也能测）"""
    return DARK_STYLE if dark else LIGHT_STYLE


MAX_RETRIES = 5
RETRY_DELAYS = [5, 10, 20, 40, 60]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
}
OTA_URL = "https://developers.google.cn/android/ota"
HTTP_TIMEOUT = 60
LOLINET_BASE = "https://mirrors.lolinet.com/firmware"
# 官网按机型年份分流：2015-2019 -> mirrors-obs-2，2020-2021 -> mirrors-obs-1
LOLINET_HOSTS = ("mirrors.lolinet.com", "mirrors-obs-1.lolinet.com",
                 "mirrors-obs-2.lolinet.com")

DEFAULT_WORKERS = 8
CHUNK_SIZE = 8 * 1024 * 1024          # 单次 Range 请求最大字节数
MEM_BUFFER_LIMIT = 256 * 1024 * 1024  # 超过此大小改用临时文件 + mmap
MAX_MANIFEST_SIZE = 96 * 1024 * 1024  # manifest 上限，防止异常包把内存撑爆
OP_DATA_TYPES = (0, 1, 8, 14)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def resource_path(name):
    if getattr(sys, "_MEIPASS", None):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def theme_config_path():
    """主题偏好配置文件：放在 exe 同级，纯文本文件（dark / light）"""
    return os.path.join(app_dir(), "theme.txt")


_THEME_OVERRIDE = None   # --theme=dark/light 显式覆盖（截图/自动化用）


def load_theme_setting():
    """读取上次保存的主题；没存过返回默认浅色"""
    if _THEME_OVERRIDE is not None:
        return _THEME_OVERRIDE == "dark"
    try:
        with open(theme_config_path(), "r", encoding="utf-8") as handle:
            return "dark" in handle.read().lower()
    except Exception:
        return False


def default_output_root():
    target = os.path.join(app_dir(), "extracted_images")
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".write_test")
        with open(probe, "w") as handle:
            handle.write("1")
        os.remove(probe)
        return target
    except Exception:
        fallback = os.path.join(os.path.expanduser("~"), "Documents", "extracted_images")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def fmt_speed(bps):
    return "%s/s" % fmt_size(bps) if bps else "0 B/s"


def fmt_eta(done, total, bps):
    if bps <= 0 or total <= 0:
        return "--:--"
    left = max(0.0, float(total - done)) / bps
    minutes, seconds = divmod(int(left), 60)
    hours, minutes = divmod(minutes, 60)
    return ("%d:%02d:%02d" % (hours, minutes, seconds)) if hours else ("%02d:%02d" % (minutes, seconds))


class CancelledError(Exception):
    """用户取消提取"""


def slot_guard(func):
    """Qt 槽函数兜底

    PyQt5 对槽函数里未捕获的异常会直接 qFatal() —— 表现就是整个窗口瞬间消失
    （闪退，退出码 0xC0000409）。所有会碰界面/线程的槽都套一层，异常只记日志。

    另外：Qt 的 clicked 信号会带一个 bool（checked），而 `def start_list(self)` 这类
    槽不接受参数，PyQt 一旦看到 *args 签名就会把 bool 传进来 -> TypeError。
    这里按被装饰函数的真实入参个数裁剪，避免按钮点了没反应。
    """
    try:
        accepted = len(inspect.signature(func).parameters) - 1   # 减去 self
        if any(param.kind == inspect.Parameter.VAR_POSITIONAL
               for param in inspect.signature(func).parameters.values()):
            accepted = None
    except (TypeError, ValueError):
        accepted = None

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if accepted is not None and len(args) > accepted:
            args = args[:accepted]
        try:
            return func(self, *args, **kwargs)
        except Exception:
            detail = traceback.format_exc()
            try:
                with open(os.path.join(app_dir(), "extractor_error.log"),
                          "a", encoding="utf-8") as handle:
                    handle.write(detail + "\n")
            except Exception:
                pass
            try:
                print(detail, file=sys.stderr)
            except Exception:
                pass
            return None

    return wrapper


def prune_finished_threads(threads):
    """去掉已结束（或被 deleteLater 回收）的 QThread 引用

    被 Qt 回收过的对象调用 isFinished() 会抛 RuntimeError，
    直接留在列表里会在下一次展开目录时炸掉整个程序。
    """
    alive = []
    for item in threads:
        try:
            if not item.isFinished():
                alive.append(item)
        except RuntimeError:
            pass
        except Exception:
            alive.append(item)
    return alive


# --------------------------------------------------------------------------- #
# 输出目录名推导（修乱码 / 修空名 / 修 download 之类的伪文件名）
# --------------------------------------------------------------------------- #

_WIN_BAD = '<>:"/\\|?*'
_RESERVED = {"CON", "PRN", "AUX", "NUL"}
_RESERVED |= {"COM%d" % i for i in range(1, 10)}
_RESERVED |= {"LPT%d" % i for i in range(1, 10)}
_ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".img.gz", ".img.xz",
                     ".tgz", ".txz", ".zip", ".7z", ".rar", ".tar", ".gz", ".xz")
_FAKE_NAMES = {"download", "downloads", "index", "file", "files", "get", "api",
               "dl", "aosp", "ota", "firmware", "latest", "view", "open", "redirect"}


def _has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" for ch in text)


def _fix_mojibake(text):
    """把被按 cp1252/latin-1 误解码的 UTF-8/GBK 中文还原：'ç³»ç»Ÿ' -> '系统'。

    只在这串字符完全不含中日文、但含有 0x80-0xFF 区的“乱码特征”时才尝试还原，
    避免把正常中文二次编码搞坏。
    """
    if not text or _has_cjk(text):
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            raw = text.encode(codec)
        except (UnicodeEncodeError, LookupError):
            continue
        for encoding in ("utf-8", "gbk", "big5"):
            try:
                candidate = raw.decode(encoding)
            except UnicodeDecodeError:
                continue
            if _has_cjk(candidate):
                return candidate
    return text


def _decode_segment(segment):
    """URL 路径段 -> 真实文件名

    优先按 UTF-8 解百分号编码（互联网标准）；UTF-8 解不出来时退回 GBK
    （部分国内镜像站按 GBK 编码中文）；最后再修 cp1252/latin-1 乱码。
    """
    if not segment:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return _fix_mojibake(unquote(segment, encoding=encoding, errors="strict"))
        except UnicodeDecodeError:
            continue
    return _fix_mojibake(unquote(segment, encoding="utf-8", errors="replace"))


def _name_from_cd_header(header):
    """从 Content-Disposition 头里取文件名（支持 filename*=UTF-8''...）"""
    if not header:
        return ""
    match = re.search(r"filename\*\s*=\s*([\w\-]+)'[^']*'([^;]+)", header, re.I)
    if match:
        charset = (match.group(1) or "utf-8").strip() or "utf-8"
        raw = match.group(2).strip().strip('"')
        try:
            return unquote(raw, encoding=charset, errors="replace")
        except Exception:
            pass
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', header, re.I)
    if match:
        return _fix_mojibake(match.group(1).strip())
    return ""


def _name_from_disposition(session, url, log=None):
    """URL 里没有可用文件名时，向服务器要 Content-Disposition。

    三级兜底：
      ① URL 查询串里的 response-content-disposition（对象存储预签名直链常这么带）
      ② HEAD 请求的响应头
      ③ Range: bytes=0-0 的 GET 响应头
         （AWS SigV4 预签名链接只对 GET 签名，HEAD 会 403，只能退回 GET）
    """
    query = parse_qs(urlsplit(url).query)
    for key in ("response-content-disposition", "response_content_disposition"):
        for value in query.get(key, []):
            name = _name_from_cd_header(value)
            if name:
                if log:
                    log("文件名取自 URL 的 %s 参数: %s" % (key, name))
                return name

    if session is None:
        return ""

    header = ""
    try:
        response = session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        try:
            if response.status_code < 400:
                header = response.headers.get("Content-Disposition", "")
        finally:
            response.close()
    except Exception:
        header = ""

    if not header:
        # 预签名 GET 链接用 HEAD 会签名不匹配（403），退化成 1 字节 GET 读头
        try:
            response = session.get(url, headers={**HEADERS, "Range": "bytes=0-0"},
                                   timeout=(15, 30), stream=True)
            try:
                header = response.headers.get("Content-Disposition", "")
            finally:
                response.close()
        except Exception:
            header = ""
        if header and log:
            log("HEAD 取不到文件名，用 1 字节 GET 读到: %s" % _name_from_cd_header(header))

    return _name_from_cd_header(header)


def sanitize_name(name):
    """去掉 Windows 非法字符，处理保留名、超长名、结尾点/空格"""
    if not name:
        return ""
    name = re.sub(r"[\x00-\x1f]", "", name)
    for char in _WIN_BAD:
        name = name.replace(char, "_")
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    if not name:
        return ""
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > 120:
        stem, suffix = os.path.splitext(name)
        name = stem[: 120 - len(suffix)] + suffix
    return name


def strip_archive_suffix(name):
    low = name.lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if low.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def derive_folder_name(url, session=None, log=None):
    """从直链推导输出目录名，解决乱码 / 空名 / download 之类伪文件名"""
    parts = urlsplit(url)
    raw_segment = parts.path.rstrip("/").rsplit("/", 1)[-1] if parts.path.strip("/") else ""
    decoded = _decode_segment(raw_segment)
    name = sanitize_name(strip_archive_suffix(decoded))

    weak = (not name) or ("." not in decoded) or (name.lower() in _FAKE_NAMES)
    if weak:
        alt = _name_from_disposition(session, url, log)
        alt = sanitize_name(strip_archive_suffix(_decode_segment(alt))) if alt else ""
        if alt and alt.lower() not in _FAKE_NAMES:
            if log:
                log("目录名取自 Content-Disposition: %s" % alt)
            name = alt
        else:
            name = ""

    if not name or name.lower() in _FAKE_NAMES:
        host = sanitize_name(parts.hostname or "firmware") or "firmware"
        name = "%s_%s" % (host, hashlib.sha1(url.encode("utf-8")).hexdigest()[:8])
        if log:
            log("URL 不含有效文件名，回退为 %s" % name)
    return name


# --------------------------------------------------------------------------- #
# 测速
# --------------------------------------------------------------------------- #

def _set_mem_limit(value):
    """自检用：临时改“多大才落磁盘”的阈值，强制走磁盘缓冲分支"""
    global MEM_BUFFER_LIMIT
    MEM_BUFFER_LIMIT = int(value)


def cleanup_stale_buffers(max_age_seconds=2 * 3600):
    """清理上次被强杀留下的下载缓冲（%TEMP%\\fw_buf_*.bin）

    正常结束或取消时会自己删；只有进程被任务管理器强杀才可能残留。
    这里只删 2 小时前的，删不掉的（正被别的实例占用）直接跳过。
    """
    removed = freed = 0
    try:
        base = tempfile.gettempdir()
        now = time.time()
        for name in os.listdir(base):
            if not (name.startswith("fw_buf_") and name.endswith(".bin")):
                continue
            path = os.path.join(base, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if now - info.st_mtime < max_age_seconds:
                continue
            try:
                os.remove(path)
            except OSError:
                continue
            removed += 1
            freed += info.st_size
    except Exception:
        pass
    return removed, freed


class SpeedMeter:
    """滑动窗口测速：最近 window 秒内的平均字节速率"""

    def __init__(self, window=2.0):
        self.window = float(window)
        self._points = deque()
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self._points.clear()

    def sample(self, total_bytes, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            self._points.append((now, total_bytes))
            while len(self._points) > 2 and now - self._points[0][0] > self.window:
                self._points.popleft()
            if len(self._points) < 2:
                return 0.0
            start_time, start_bytes = self._points[0]
            elapsed = now - start_time
            if elapsed <= 0:
                return 0.0
            return max(0.0, (total_bytes - start_bytes) / elapsed)


# --------------------------------------------------------------------------- #
# 并行 Range 下载
# --------------------------------------------------------------------------- #

class Segment(object):
    __slots__ = ("zip_start", "zip_end", "buf_off", "parts")

    def __init__(self, zip_start, zip_end, buf_off, parts):
        self.zip_start = zip_start
        self.zip_end = zip_end
        self.buf_off = buf_off
        self.parts = parts


class BufferView(object):
    """把若干段合并下载结果映射成“按 zip 绝对偏移随机访问”"""

    def __init__(self, segments, buffer, tmp_path=None):
        self.segments = segments
        self.buffer = buffer
        self.tmp_path = tmp_path
        self._starts = [seg.zip_start for seg in segments]

    def slice(self, abs_start, abs_end):
        import bisect

        index = bisect.bisect_right(self._starts, abs_start) - 1
        if index < 0:
            return None
        segment = self.segments[index]
        if not (segment.zip_start <= abs_start and abs_end <= segment.zip_end):
            return None
        local = abs_start - segment.zip_start
        length = abs_end - abs_start + 1
        return bytes(self.buffer[segment.buf_off + local:
                                 segment.buf_off + local + length])

    def close(self):
        try:
            if isinstance(self.buffer, mmap.mmap):
                self.buffer.close()
        except Exception:
            pass
        if self.tmp_path:
            try:
                os.remove(self.tmp_path)
            except Exception:
                pass
        self.buffer = None


class ParallelDownloader(object):
    """多线程并行 Range 下载器

    - 把每个数据段切成 <= chunk_size 的小块，线程池并发抓取
    - 大文件落到临时文件 + mmap，避免几百 MB 常驻内存
    - 单块失败按 5/10/20/40/60s 退避重试
    """

    def __init__(self, url, session_factory, workers=DEFAULT_WORKERS,
                 chunk_size=CHUNK_SIZE, log=None, on_progress=None,
                 cancel=None, retries=MAX_RETRIES):
        self.url = url
        self._session_factory = session_factory
        self.workers = max(1, int(workers))
        self.chunk_size = max(1024 * 1024, int(chunk_size))
        self._log = log or (lambda message: None)
        self._on_progress = on_progress or (lambda done, total: None)
        self.cancel = cancel or threading.Event()
        self.retries = retries
        self.total = 0
        self.done = 0
        self.buffer = None
        self._lock = threading.Lock()
        self._local = threading.local()

    # -- internals -------------------------------------------------------- #

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory()
            self._local.session = session
        return session

    def _check_cancel(self):
        if self.cancel.is_set():
            raise CancelledError("已取消")

    def _fetch(self, start, end):
        expected = end - start + 1
        last_error = None
        for attempt in range(self.retries):
            self._check_cancel()
            try:
                response = self._session().get(
                    self.url,
                    headers={**HEADERS, "Range": "bytes=%d-%d" % (start, end)},
                    timeout=(HTTP_TIMEOUT, 180),
                )
                try:
                    status = response.status_code
                    if status not in (206, 200):
                        raise RuntimeError("HTTP %d" % status)
                    data = response.content
                    if status == 200 and len(data) != expected:
                        raise RuntimeError("服务器未按 Range 返回（可能不支持断点续传）")
                finally:
                    response.close()
                if len(data) != expected:
                    raise RuntimeError("长度不匹配 %d != %d" % (len(data), expected))
                return data
            except CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    self._log("  下载失败(%s)，%ds 后重试 (%d/%d)" %
                              (exc, delay, attempt + 1, self.retries))
                    for _ in range(delay * 10):
                        self._check_cancel()
                        time.sleep(0.1)
        raise RuntimeError("下载失败: %s" % last_error)

    def _alloc(self, total):
        if total <= MEM_BUFFER_LIMIT:
            return bytearray(total), None
        handle, path = tempfile.mkstemp(prefix="fw_buf_", suffix=".bin")
        try:
            os.ftruncate(handle, total)
            buffer = mmap.mmap(handle, total)
        except Exception:
            # 磁盘满 / mmap 失败：别把刚建的空文件留在 %TEMP%
            try:
                os.close(handle)
            except OSError:
                pass
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        os.close(handle)
        self._log("数据量 %s，使用磁盘缓冲：%s" % (fmt_size(total), path))
        return buffer, path

    def _worker(self, task):
        start, end, buffer_offset = task
        data = self._fetch(start, end)
        self.buffer[buffer_offset: buffer_offset + len(data)] = data
        with self._lock:
            self.done += len(data)
            done = self.done
        self._on_progress(done, self.total)

    def download(self, ranges):
        """ranges: [(zip_start, zip_end, {partition names})] -> BufferView"""
        ranges = [item for item in ranges if item[1] >= item[0]]
        if not ranges:
            raise PayloadError("没有需要下载的数据段")

        segments = []
        offset = 0
        for start, end, parts in ranges:
            segments.append(Segment(start, end, offset, parts))
            offset += end - start + 1

        self.total = offset
        self.done = 0
        self.buffer, tmp_path = self._alloc(offset)

        tasks = []
        for segment in segments:
            position = segment.zip_start
            while position <= segment.zip_end:
                end = min(position + self.chunk_size - 1, segment.zip_end)
                tasks.append((position, end, segment.buf_off + (position - segment.zip_start)))
                position = end + 1

        self._log("并行下载：%d 段 -> %d 块，%d 线程，共 %s" %
                  (len(segments), len(tasks), self.workers, fmt_size(self.total)))

        executor = ThreadPoolExecutor(max_workers=self.workers)
        futures = []
        try:
            futures = [executor.submit(self._worker, task) for task in tasks]
            for future in as_completed(futures):
                future.result()
        except Exception:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False)
            self._release_buffer(tmp_path)     # 取消/失败也要把磁盘缓冲清掉
            raise
        executor.shutdown(wait=True)
        return BufferView(segments, self.buffer, tmp_path)

    def _release_buffer(self, tmp_path):
        """释放 mmap 并删除临时缓冲文件（异常路径专用）"""
        try:
            if isinstance(self.buffer, mmap.mmap):
                self.buffer.close()
        except Exception:
            pass
        self.buffer = None
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 来源解析（h5ai 目录 / Google OTA 页面）
# --------------------------------------------------------------------------- #

_MIRROR_HINT = {}   # 路径前缀 -> 上次成功的主机，避免每次都先撞死主站


def mirror_key(url):
    parts = urlsplit(url)
    return "/".join(parts.path.split("/")[:4]) or parts.path


def mirror_candidates(url):
    """把 lolinet 的目录 URL 换成其它镜像主机，用于主站没内容时兜底

    命中过镜像的路径前缀会记住上次成功的主机，下次直接先问它。
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host.endswith("lolinet.com"):
        return [url]
    order = list(LOLINET_HOSTS)
    hint = _MIRROR_HINT.get(mirror_key(url))
    if hint in order:
        order.remove(hint)
        order.insert(0, hint)
    candidates = []
    for mirror in order:
        candidate = urlunsplit((parts.scheme or "https", mirror, parts.path,
                                parts.query, parts.fragment))
        if candidate not in candidates:
            candidates.append(candidate)
    if url not in candidates:
        candidates.insert(0, url)
    return candidates


def parse_h5ai(html_text, base_url):
    dirs, files = [], []
    skip = {"..", "Parent Directory", "powered by h5ai", "modern browsers"}
    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.I | re.S):
        href = html_mod.unescape(match.group(1)).strip()
        name = re.sub(r"<[^>]+>", "", html_mod.unescape(match.group(2))).strip()
        if not name or name in skip or not href or href.startswith(("?", "#")):
            continue
        if href.endswith("/"):
            dir_name = _decode_segment(name.rstrip("/")) or name.rstrip("/")
            if dir_name and all(dir_name != existing[0] for existing in dirs):
                dirs.append((dir_name, urljoin(base_url, href)))
        elif href.lower().split("?")[0].endswith(".zip"):
            raw_name = href.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            file_name = _decode_segment(raw_name) or name
            files.append((file_name, urljoin(base_url, href)))
    return dirs, sorted(files, key=lambda item: item[0].lower())


_MODEL_RE = re.compile(
    r"Pixel\s+(?:\d+[a-z]?(?:\s+(?:Pro|XL|Fold|Tablet|Slate|a)){0,3}"
    r"|XL|C|Fold|Tablet|Slate|Watch|Buds)\b", re.I)


_DEVICE_YEAR_HINTS = {
    "pixel fold": 2023.0, "pixel tablet": 2023.0, "pixel c": 2015.0,
    "pixel watch": 2022.0, "pixel xl": 2016.0, "pixel": 2016.0,
    "nexus player": 2014.0, "nexus 10": 2012.0, "nexus 9": 2014.0,
    "nexus 7": 2013.0, "nexus 6p": 2015.0, "nexus 6": 2014.0,
    "nexus 5x": 2015.0, "nexus 5": 2013.0, "nexus 4": 2012.0,
    "nexus s": 2010.0, "nexus one": 2010.0,
}


def device_year(name):
    """估算机型发布年份，用于“新的排前面”

    Pixel N 的年份 ≈ N + 2015（Pixel 6=2021 … Pixel 11=2026）；
    带 a 的（Pixel 9a）比同代晚半年；Pixel Fold / Tablet / Nexus 等查表。
    """
    text = (name or "").lower().strip()
    match = re.search(r"pixel\s+(\d+)", text)
    if match:
        year = float(int(match.group(1)) + 2015)
        if re.search(r"\d+\s*a\b", text):
            year += 0.5
        return year
    for key in sorted(_DEVICE_YEAR_HINTS, key=len, reverse=True):
        if text.startswith(key):
            return _DEVICE_YEAR_HINTS[key]
    return 0.0


def version_sort_key(version):
    """构建号里带 YYMMDD（cp4a.261105.040），用它排序最准"""
    match = re.search(r"\.(\d{2})(\d{2})(\d{2})(?:\.|$)", version or "")
    if match:
        year, month, day = (int(part) for part in match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return (1, year * 10000 + month * 100 + day)
    return (0, 0)


def clean_device_name(raw_text, codename=""):
    """把 Google OTA 页上的冗长标题压成干净的机型名

    'Pixel 11 的代号为“cubs”'           -> 'Pixel 11'
    '“grizzly”（适用于 Pixel 11 Pro）'   -> 'Pixel 11 Pro'
    'Pixel 11 Pro XL 的代号为“kodiak”'  -> 'Pixel 11 Pro XL'
    '“stallion” : Pixel 10a'            -> 'Pixel 10a'
    '适用于 Pixel 9a 的 “tegu”'          -> 'Pixel 9a'
    """
    text = (raw_text or "")
    text = (text.replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'").replace("\u00a0", " "))
    code = (codename or "").strip()
    if code:
        escaped = re.escape(code)
        for pattern in (
            r"\s*的代号为\s*[\"']?%s[\"']?" % escaped,
            r"[\"']?%s[\"']?\s*[:：]" % escaped,
            r"[\"']%s[\"']" % escaped,
            r"[\"']?%s[\"']?" % escaped,
        ):
            text = re.sub(pattern, " ", text, flags=re.I)
    text = re.sub(r"[（(]\s*[)）]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -:：,，")
    match = _MODEL_RE.search(text)
    if match:
        return match.group(0).strip()
    # 非 Pixel 机型（Nexus 等）：清掉句子成分，再修掉不配对的括号
    text = re.sub(r"适用于|的代号为|的|表示|对应", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -:：,，\"'")
    # 句子里混进来的中文前缀（“…对应 Nexus 9”）直接切到产品名
    marker = re.search(r"(Nexus|Pixel|Galaxy|Moto|Xiaomi|Redmi|OnePlus)\b", text, re.I)
    if marker and marker.start() > 0:
        text = text[marker.start():]
    while len(text) > 2 and text[0] in "（([" and text[-1] in "）)]":
        text = text[1:-1].strip()          # 去掉整体包裹的括号
    for opener, closer in (("(", ")"), ("（", "）"), ("[", "]")):
        while text.count(opener) > text.count(closer):
            text = text[: text.rfind(opener)].rstrip()
        while text.count(closer) > text.count(opener):
            text = text[: text.rfind(closer)].rstrip()
    text = re.sub(r"\s+", " ", text).strip(" -:：,，\"'")
    return text or code or "unknown"


def parse_ota_page(html_text):
    headings = list(re.finditer(r'<h2[^>]*id="([a-zA-Z0-9_]+)"[^>]*>', html_text))
    devices = []
    for index, heading in enumerate(headings):
        codename = heading.group(1)
        segment = html_text[heading.start(): heading.start() + 500]
        name_match = re.search(r'data-text="([^"]*)"', segment)
        raw_name = html_mod.unescape(name_match.group(1)) if name_match else codename
        name = clean_device_name(raw_name, codename)
        end = headings[index + 1].start() if index + 1 < len(headings) else len(html_text)
        block = html_text[heading.start(): end]
        raw_links = re.findall(
            r'https://googledownloads\.cn/dl/android/aosp/([a-zA-Z0-9_.\-]+)\.zip', block)
        seen, versions = set(), []
        for file_name in raw_links:
            if file_name in seen:
                continue
            seen.add(file_name)
            link = "https://googledownloads.cn/android/aosp/%s.zip" % file_name
            pieces = [piece for piece in file_name.split("-") if piece]
            if len(pieces) > 1:
                rest = [piece for piece in pieces[1:] if piece.lower() != "ota"]
                version = "-".join(rest) if rest else "-".join(pieces[1:])
            else:
                version = file_name
            versions.append((version, link))
        if versions:
            versions.sort(key=lambda item: (version_sort_key(item[0]), item[0]), reverse=True)
            devices.append({"codename": codename, "name": name, "versions": versions})
    # 从新到旧：Pixel 11/Android 17 的机型排在 Pixel 10/Android 16 前面
    devices.sort(key=lambda device: (-device_year(device["name"]),
                                     device["name"].lower(), device["codename"].lower()))
    return devices


def fetch_ota_devices(core):
    session = core.new_session()
    core.log("正在获取 OTA 列表...")
    response = session.get(OTA_URL, timeout=HTTP_TIMEOUT)
    try:
        if response.status_code != 200:
            raise RuntimeError("获取 OTA 列表失败: HTTP %d" % response.status_code)
        devices = parse_ota_page(response.text)
    finally:
        response.close()
    if not devices:
        raise RuntimeError("未找到 OTA 下载链接")
    core.log("找到 %d 个机型, 共 %d 个版本" %
             (len(devices), sum(len(device["versions"]) for device in devices)))
    return devices


# --------------------------------------------------------------------------- #
# 提取核心（不依赖 Qt，自检可直接调用）
# --------------------------------------------------------------------------- #

def _is_rate_limited(exc):
    text = str(exc)
    return "429" in text or "限流" in text or "Too Many Requests" in text


class ExtractorCore(object):
    def __init__(self, proxy=None, workers=DEFAULT_WORKERS, log=None,
                 progress=None, cancel=None, output_root=None):
        self.proxy = proxy
        self.workers = max(1, int(workers or DEFAULT_WORKERS))
        self._log_fn = log
        self._progress_fn = progress
        self.cancel = cancel or threading.Event()
        self.output_root = output_root or default_output_root()

        self.bytes_done = 0
        self.dl_total = 0
        self.dl_started = 0.0
        self.dl_finished = 0.0
        self.phase = "idle"
        self.speed = SpeedMeter()
        self._last_pct = -1
        self._probe_cache = {}

    # -- helpers ---------------------------------------------------------- #

    def average_speed(self):
        """本次下载阶段的平均速度（字节/秒），用于完成提示"""
        if self.dl_started <= 0 or self.dl_finished <= self.dl_started:
            return 0.0
        return max(0.0, float(self.bytes_done) / (self.dl_finished - self.dl_started))

    def log(self, message):
        if self._log_fn:
            self._log_fn(str(message))

    def progress(self, percent):
        pct = max(0, min(100, int(percent)))
        if pct == self._last_pct:
            return
        self._last_pct = pct
        if self._progress_fn:
            self._progress_fn(pct)

    def check_cancel(self):
        if self.cancel.is_set():
            raise CancelledError("已取消")

    def new_session(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        session.verify = False
        session.trust_env = False
        if self.proxy:
            session.proxies.update(self.proxy)
        return session

    @staticmethod
    def _range_get(session, url, start, end):
        response = session.get(url, headers={**HEADERS, "Range": "bytes=%d-%d" % (start, end)},
                               timeout=(HTTP_TIMEOUT, 180))
        try:
            if response.status_code not in (206, 200):
                raise RuntimeError("HTTP %d" % response.status_code)
            return response.content
        finally:
            response.close()

    # -- 目录浏览 --------------------------------------------------------- #

    def list_dir(self, url):
        """返回 (dirs, files, effective_url)：主站目录为空时自动换 mirrors-obs-*"""
        last_error = None
        for candidate in mirror_candidates(url):
            self.check_cancel()
            session = self.new_session()
            try:
                response = session.get(candidate, timeout=8)
                try:
                    if response.status_code != 200:
                        raise RuntimeError("HTTP %d" % response.status_code)
                    dirs, files = parse_h5ai(response.text, candidate)
                finally:
                    response.close()
                if dirs or files:
                    if candidate != url:
                        self.log("目录改走镜像: %s" % candidate)
                        _MIRROR_HINT[mirror_key(url)] = (urlsplit(candidate).hostname or "")
                    return dirs, files, candidate
                last_error = RuntimeError("目录为空")
            except CancelledError:
                raise
            except Exception as exc:
                last_error = exc
        raise RuntimeError("目录获取失败: %s" % last_error)

    # -- 远端探测 --------------------------------------------------------- #

    def probe(self, url, refresh=False):
        cached = self._probe_cache.get(url)
        if cached and not refresh:
            return cached

        info = {"url": url, "names": [], "sizes": {}, "is_payload": False,
                "payload_zip_offset": 0, "data_offset": 0, "dam": None,
                "version": 0, "block_size": 0}
        session = self.new_session()
        with RemoteZip(url, session=session) as remote_zip:
            names = remote_zip.namelist()
            info["names"] = names
            for name in names:
                try:
                    info["sizes"][name] = remote_zip.getinfo(name).file_size
                except Exception:
                    info["sizes"][name] = 0
            if "payload.bin" in names:
                zip_info = remote_zip.getinfo("payload.bin")
                payload_zip_offset = (zip_info.header_offset + 30
                                      + len(zip_info.filename.encode("utf-8"))
                                      + len(zip_info.extra))
                head24 = self._range_get(session, url, payload_zip_offset,
                                         payload_zip_offset + 23)
                version, manifest_size, _sig, need = header_info(head24)
                if need > MAX_MANIFEST_SIZE:
                    raise PayloadError("manifest 过大 (%d 字节)，疑似非法包" % need)
                head = self._range_get(session, url, payload_zip_offset,
                                       payload_zip_offset + need - 1)
                dam, data_offset, version = parse_header(head)
                info.update(is_payload=True, payload_zip_offset=payload_zip_offset,
                            data_offset=data_offset, dam=dam, version=version,
                            block_size=dam.block_size)
        self._probe_cache[url] = info
        return info

    def list_remote(self, url):
        self.phase = "probe"
        info = None
        for attempt in range(MAX_RETRIES):
            try:
                info = self.probe(url)
                break
            except Exception as exc:
                if _is_rate_limited(exc) and attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    self.log("被限流(HTTP 429)，%ds 后重试 (%d/%d)..." % (delay, attempt + 1, MAX_RETRIES))
                    time.sleep(delay)
                elif _is_rate_limited(exc):
                    self.log("服务器限流(HTTP 429)：请稍后再试，或勾选代理换个出口 IP")
                    raise
                else:
                    raise
        if info is None:
            raise RuntimeError("探测失败")

        self.log("ZIP 内文件: %s" %
                 [name for name in info["names"] if not name.startswith("META-INF")])
        if info["is_payload"]:
            dam = info["dam"]
            partitions = list_partitions(dam)
            self.log("payload: version=%s, block_size=%d, %d 个分区" %
                     (info["version"], info["block_size"], len(partitions)))
            items = [(part["name"],
                      "%s  (%.1f MB, %d ops)" % (part["name"], part["size_mb"], part["ops_count"]),
                      None) for part in partitions]
            return items, True

        items = []
        for name in sorted(info["names"], key=lambda value: value.lower()):
            size = info["sizes"].get(name) or 0
            items.append((name, "%s  (%.1f MB)" % (name, size / 1024.0 / 1024.0), size))
        return items, False

    # -- 提取 ------------------------------------------------------------- #

    def extract(self, url, targets):
        if not targets:
            raise ValueError("请至少选择一个文件")

        session = self.new_session()
        folder = derive_folder_name(url, session, self.log)
        final_path = os.path.join(self.output_root, folder)
        os.makedirs(final_path, exist_ok=True)
        self.log("输出目录: %s" % final_path)

        try:
            info = self.probe(url)
        except Exception as exc:
            if _is_rate_limited(exc):
                self.log("服务器限流(HTTP 429)：请稍后再试，或勾选代理换个出口 IP")
            raise
        if info["is_payload"]:
            self._extract_payload(url, info, final_path, targets)
        else:
            self._extract_plain(url, info, final_path, targets)

        self.phase = "done"
        self.progress(100)
        self.log("完成: %s" % final_path)
        return final_path

    # .. payload 分支 ..................................................... #

    def _extract_payload(self, url, info, final_path, targets):
        dam = info["dam"]
        payload_zip_offset = info["payload_zip_offset"]
        data_offset = info["data_offset"]

        available = [part.partition_name for part in dam.partitions]
        wanted = []
        for target in targets:
            name = target[:-4] if target.lower().endswith(".img") else target
            if name in available and name not in wanted:
                wanted.append(name)
        if not wanted:
            raise PayloadError("所选分区均未找到，可用分区: %s" % ", ".join(available))

        ranges = get_zip_ranges_for_partitions(
            wanted, dam, data_offset, payload_zip_offset)
        if not ranges:
            raise PayloadError("目标分区不含可下载数据（可能全是 ZERO/DISCARD）")

        total = sum(end - start + 1 for start, end, _parts in ranges)
        self.log("目标分区: %s" % ", ".join(wanted))
        self.log("需要下载 %d 个数据段, 共 %s" % (len(ranges), fmt_size(total)))

        self.phase = "download"
        self.dl_total = total
        self.bytes_done = 0
        self.dl_started = time.monotonic()
        self.dl_finished = 0.0
        self.speed.reset()
        downloader = ParallelDownloader(
            url=url,
            session_factory=self.new_session,
            workers=self.workers,
            log=self.log,
            on_progress=self._on_download_progress,
            cancel=self.cancel,
        )
        view = downloader.download(ranges)
        self.dl_finished = time.monotonic()
        try:
            self.phase = "write"
            self._write_partitions(dam, wanted, view,
                                   payload_zip_offset + data_offset, final_path)
        finally:
            view.close()
            self.speed.reset()

    def _on_download_progress(self, done, total):
        self.bytes_done = done
        if total:
            self.progress(done * 50.0 / total)

    def _write_partitions(self, dam, wanted, view, payload_base, final_path):
        block = dam.block_size
        plan, total_bytes = [], 0
        for name in wanted:
            part = next((item for item in dam.partitions if item.partition_name == name), None)
            if part is None:
                continue
            part_bytes = sum(extent.num_blocks * block
                             for op in part.operations for extent in op.dst_extents)
            total_bytes += part_bytes
            plan.append((name, part, part_bytes))

        written = 0
        for index, (name, part, _part_bytes) in enumerate(plan):
            self.check_cancel()
            out_path = os.path.join(final_path, "%s.img" % name)
            self.log("[%d/%d] 写入 %s ..." % (index + 1, len(plan), os.path.basename(out_path)))
            with open(out_path, "wb") as out_file:
                for op in part.operations:
                    self.check_cancel()
                    self._apply_op(op, view, payload_base, block, out_file)
                    written += sum(extent.num_blocks * block for extent in op.dst_extents)
                    if total_bytes:
                        self.progress(50 + written * 50.0 / total_bytes)
            self.log("      -> %s (%s)" % (out_path, fmt_size(os.path.getsize(out_path))))

    def _apply_op(self, op, view, payload_base, block, out_file):
        if op.type in OP_DATA_TYPES:
            abs_start = payload_base + op.data_offset
            abs_end = abs_start + op.data_length - 1
            raw = view.slice(abs_start, abs_end)
            if raw is None:
                raise PayloadError("数据段未命中缓冲区 (op type %d)" % op.type)
            if op.data_sha256_hash:
                if hashlib.sha256(raw).digest() != op.data_sha256_hash:
                    raise PayloadError("SHA256 校验失败 (op type %d)" % op.type)
            if op.type == 0:
                data = raw
            elif op.type == 1:
                data = bz2.BZ2Decompressor().decompress(raw)
            elif op.type == 8:
                data = lzma.LZMADecompressor().decompress(raw)
            else:
                import zstandard
                data = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
            _write_extents(out_file, data, op.dst_extents, block)
        elif op.type == 6:
            write_zeros(out_file, op.dst_extents, block)
        elif op.type == 7:
            pass
        else:
            raise UnsupportedOpError(
                "不支持的 op 类型 %d（增量包不支持，仅支持 Full OTA）" % op.type)

    # .. 普通 zip 分支 .................................................... #

    def _extract_plain(self, url, info, final_path, targets):
        names = info["names"]
        wanted = [target for target in targets if target in names]
        if not wanted:
            raise PayloadError("包内未找到所选文件")

        total = sum(info["sizes"].get(name, 0) for name in wanted) or 1
        self.phase = "download"
        self.dl_total = total
        self.bytes_done = 0
        self.dl_started = time.monotonic()
        self.dl_finished = 0.0
        self.speed.reset()
        lock = threading.Lock()
        workers = max(1, min(self.workers, len(wanted)))
        self.log("并行提取 %d 个文件，%d 线程" % (len(wanted), workers))

        def extract_one(name):
            self.check_cancel()
            out_path = os.path.join(final_path, name.replace("/", os.sep))
            parent = os.path.dirname(out_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            session = self.new_session()
            with RemoteZip(url, session=session) as remote_zip:
                with remote_zip.open(name) as source, open(out_path, "wb") as sink:
                    while True:
                        chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        sink.write(chunk)
                        with lock:
                            self.bytes_done += len(chunk)
                            done = self.bytes_done
                        self.progress(done * 100.0 / total)
                        self.check_cancel()
            self.log("      -> %s (%s)" % (out_path, fmt_size(os.path.getsize(out_path))))

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(extract_one, name) for name in wanted]
            for future in as_completed(futures):
                future.result()
        self.dl_finished = time.monotonic()


# --------------------------------------------------------------------------- #
# 自检（不依赖 Qt，可在窗口版 exe 上跑）
# --------------------------------------------------------------------------- #

def _build_range_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        root = "."
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _file(self):
            path = unquote(urlsplit(self.path).path).lstrip("/")
            if not path:
                return None
            full = os.path.join(self.root, path.replace("/", os.sep))
            return full if os.path.isfile(full) else None

        def do_HEAD(self):
            full = self._file()
            if not full:
                self.send_error(404)
                return
            if os.path.basename(full).startswith("presign"):
                self.send_error(403)      # 模拟 S3/EOS 预签名链接：HEAD 签名不匹配
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(full)))
            self.send_header("Accept-Ranges", "bytes")
            base = os.path.basename(full)
            if base in ("download", "file") or base.startswith("disp_"):
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''%E7%9C%9F%E5%AE%9E%E5%90%8D.zip")
            self.end_headers()

        def do_GET(self):
            full = self._file()
            if not full:
                self.send_error(404)
                return
            size = os.path.getsize(full)
            start, end = 0, size - 1
            partial = False
            header = self.headers.get("Range")
            if header:
                match = re.match(r"bytes=(\d*)-(\d*)", header.strip())
                if match:
                    first, second = match.group(1), match.group(2)
                    if first == "" and second:
                        start = max(0, size - int(second))
                    elif first:
                        start = int(first)
                        end = int(second) if second else size - 1
                    end = min(end, size - 1)
                    partial = True
            length = max(0, end - start + 1)
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if os.path.basename(full).startswith("presign"):
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''%E9%A2%84%E7%AD%BE%E5%90%8D.zip")
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            with open(full, "rb") as handle:
                handle.seek(start)
                left = length
                while left > 0:
                    chunk = handle.read(min(262144, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    left -= len(chunk)

    return Handler


def _make_ota_zip(zip_path, block=4096, blocks=8):
    """造一个 Full OTA 包：raw / zstd / bz2 / xz / zero 五种 op"""
    import random
    import zipfile

    import update_metadata_pb2 as um

    rnd = random.Random(20260101)
    dam = um.DeltaArchiveManifest()
    dam.block_size = block
    blob = bytearray()
    expected = {}

    specs = [("boot", "raw"), ("init_boot", "zstd"), ("vbmeta", "bz2"),
             ("vendor_boot", "xz"), ("userdata", "zero")]

    for name, kind in specs:
        raw = bytes(rnd.getrandbits(8) for _ in range(blocks * block))
        part = dam.partitions.add()
        part.partition_name = name

        if kind == "zero":
            op = part.operations.add()
            op.type = 6
            extent = op.dst_extents.add()
            extent.start_block = 0
            extent.num_blocks = blocks
            part.new_partition_info.size = blocks * block
            expected[name] = b"\x00" * (blocks * block)
            continue

        if kind == "raw":
            compressed, op_type = raw, 0
        elif kind == "zstd":
            import zstandard
            compressed, op_type = zstandard.ZstdCompressor(level=3).compress(raw), 14
        elif kind == "bz2":
            compressed, op_type = bz2.compress(raw), 1
        else:
            compressed, op_type = lzma.compress(raw), 8

        op = part.operations.add()
        op.type = op_type
        op.data_offset = len(blob)
        op.data_length = len(compressed)
        op.data_sha256_hash = hashlib.sha256(compressed).digest()
        extent = op.dst_extents.add()
        extent.start_block = 0
        extent.num_blocks = blocks
        part.new_partition_info.size = len(raw)
        blob += compressed
        expected[name] = raw

    manifest = dam.SerializeToString()
    payload = b"CrAU" + struct.pack(">QQI", 2, len(manifest), 0) + manifest + bytes(blob)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("META-INF/com/android/metadata", "post-build=selftest\n")
        archive.writestr("payload.bin", payload)
    return expected


def run_selftest():
    import shutil
    import zipfile

    lines = []
    failures = []

    def check(name, ok, detail=""):
        ok = bool(ok)
        lines.append("%s %s%s" % ("PASS" if ok else "FAIL", name,
                                  (" | " + detail) if detail else ""))
        if not ok:
            failures.append(name)
        print(lines[-1])

    # CLI 的 --theme= 覆盖只影响 GUI，不影响自检对配置文件的读写
    global _THEME_OVERRIDE
    _THEME_OVERRIDE = None

    # 0) 主题 stylesheet ------------------------------------------------ #
    light_css = build_stylesheet(False)
    dark_css = build_stylesheet(True)
    check("深色主题样式表", "#1e1e1e" in dark_css and "#0e639c" in dark_css
          and len(dark_css) > 800, "%d 字符" % len(dark_css))
    check("浅色主题样式表", "alternate-background-color" in light_css
          and "#1e1e1e" not in light_css, "%d 字符" % len(light_css))
    theme_file = theme_config_path()
    original_theme = None
    try:
        with open(theme_file, "r", encoding="utf-8") as handle:
            original_theme = handle.read()
    except Exception:
        pass
    try:
        with open(theme_file, "w", encoding="utf-8") as handle:
            handle.write("dark")
        check("主题偏好读取", load_theme_setting() is True)
        with open(theme_file, "w", encoding="utf-8") as handle:
            handle.write("light")
        check("主题偏好读取(浅色)", load_theme_setting() is False)
    finally:
        try:
            if original_theme is None:
                os.remove(theme_file)
            else:
                with open(theme_file, "w", encoding="utf-8") as handle:
                    handle.write(original_theme)
        except Exception:
            pass

    # 1) 目录名推导 --------------------------------------------------- #
    name_cases = [
        ("https://mirrors.lolinet.com/firmware/lenomola/2024/x/%E5%9B%BA%E4%BB%B6.zip", "固件"),
        ("https://x.com/%E5%9B%BA%E4%BB%B6%20OTA%20%E6%B5%8B%E8%AF%95.zip", "固件 OTA 测试"),
        ("https://x.com/%B9%CC%BC%FE%B0%FC.zip", "固件包"),
        ("https://x.com/" + quote("ç³»ç»Ÿ.zip"), "系统"),
        ("https://x.com/dir/CON.zip", "_CON"),
        ("https://x.com/a<b>c:d.zip", "a_b_c_d"),
        ("https://x.com/%E5%9B%BA%E4%BB%B6-ota.zip?token=abc", "固件-ota"),
    ]
    for url, expect in name_cases:
        got = derive_folder_name(url)
        check("目录名 %r" % expect, got == expect, "got=%r" % got)

    for url in ("https://x.com/api/download?id=5", "https://x.com/firmware/lenomola/"):
        got = derive_folder_name(url)
        check("目录名回退 %s" % url, got.startswith("x.com_") and len(got) > 8, "got=%r" % got)

    # 2) 测速 --------------------------------------------------------- #
    mirror_list = mirror_candidates("https://mirrors.lolinet.com/firmware/lenomola/")
    check("镜像候选主机",
          len(mirror_list) == 3 and "mirrors-obs-1" in " ".join(mirror_list)
          and "mirrors-obs-2" in " ".join(mirror_list),
          " ".join(mirror_list))
    check("非 lolinet 主机不改写",
          mirror_candidates("https://example.com/a/") == ["https://example.com/a/"])

    # 2b) 机型名清洗（Google OTA 页面标题很啰嗦）
    name_cases = [
        ('Pixel 11 的代号为“cubs”', "cubs", "Pixel 11"),
        ('“grizzly”（适用于 Pixel 11 Pro）', "grizzly", "Pixel 11 Pro"),
        ('Pixel 11 Pro XL 的代号为“kodiak”', "kodiak", "Pixel 11 Pro XL"),
        ('“stallion” : Pixel 10a', "stallion", "Pixel 10a"),
        ('适用于 Pixel 9a 的 “tegu”', "tegu", "Pixel 9a"),
        ('Pixel 10 Pro Fold 的代号为“rango”', "rango", "Pixel 10 Pro Fold"),
    ]
    for raw, code, expect in name_cases:
        got = clean_device_name(raw, code)
        check("机型名清洗 %r" % expect, got == expect, "got=%r" % got)

    # 2c) 机型排序：新的在前（Pixel 11 / Android 17 早于 Pixel 10 / Android 16）
    years = [device_year(name) for name in
             ("Pixel 11", "Pixel 10 Pro XL", "Pixel 10", "Pixel 9a", "Pixel 9", "Pixel Fold")]
    check("机型年份估算(Pixel 9a 晚于 9)", years[2] > years[3] > years[4],
          " ".join("%s=%.1f" % (n, y) for n, y in zip(("P10", "P9a", "P9"), years[2:])))
    check("机型年份估算(11 > 10)",
          device_year("Pixel 11 Pro XL") > device_year("Pixel 10 Pro XL"),
          "%.1f > %.1f" % (device_year("Pixel 11 Pro XL"), device_year("Pixel 10 Pro XL")))

    # 2d) 版本号排序：构建号里的 YYMMDD 说了算
    newer = version_sort_key("cp4a.261105.040.d1-37020ad7")
    older = version_sort_key("cd1a.260618.001.a7-7f02515d")
    unknown = version_sort_key("mob31e-284a96a5")
    check("版本号按日期排序", newer > older > unknown, "%s > %s > %s" % (newer, older, unknown))

    # 2e) 页面解析后的顺序就是从新到旧
    fake_page = (
        '<h2 id="frankel" data-text="Pixel 10 的代号为“frankel”">X</h2>'
        '<a href="https://googledownloads.cn/dl/android/aosp/frankel-ota-cp1a.251020.001-aaaa.zip">l</a>'
        '<h2 id="cubs" data-text="Pixel 11 的代号为“cubs”">X</h2>'
        '<a href="https://googledownloads.cn/dl/android/aosp/cubs-ota-cp4a.261105.040-bbbb.zip">l</a>'
        '<h2 id="tegu" data-text="适用于 Pixel 9a 的 “tegu”">X</h2>'
        '<a href="https://googledownloads.cn/dl/android/aosp/tegu-ota-cd1a.260618.001-cccc.zip">l</a>'
    )
    parsed = parse_ota_page(fake_page)
    order = [device["name"] for device in parsed]
    check("OTA 列表从新到旧排序", order == ["Pixel 11", "Pixel 10", "Pixel 9a"],
          " → ".join(order))

    # 2f) 已回收 QThread 引用过滤（否则点第二次展开目录就闪退）
    class _DeadThread(object):                     # 模拟被 deleteLater 回收的 QThread
        def isFinished(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    class _LiveThread(object):
        def __init__(self, running):
            self.running = running

        def isFinished(self):
            return not self.running

    survivors = prune_finished_threads(
        [_LiveThread(True), _DeadThread(), _LiveThread(False)])
    check("已回收线程被过滤", len(survivors) == 1 and survivors[0].running,
          "剩 %d 个" % len(survivors))
    meter = SpeedMeter(window=2.0)
    meter.sample(0, 1000.0)
    bps = meter.sample(2 * 1024 * 1024, 1001.0)
    check("测速 2 MiB/s", abs(bps - 2 * 1024 * 1024) < 1.0, "%.0f B/s" % bps)

    # 3) 端到端 -------------------------------------------------------- #
    tmpdir = tempfile.mkdtemp(prefix="fw_selftest_")
    handler = _build_range_handler()
    handler.root = tmpdir
    from http.server import ThreadingHTTPServer
    httpd = None
    try:
        ota_name = "固件 OTA 测试.zip"
        expected = _make_ota_zip(os.path.join(tmpdir, ota_name))
        with open(os.path.join(tmpdir, "download"), "wb") as handle:
            handle.write(b"x")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        url = "http://127.0.0.1:%d/%s" % (port, quote(ota_name))
        out_root = os.path.join(tmpdir, "out")
        logs = []
        targets = ["boot.img", "init_boot.img", "vbmeta.img",
                   "vendor_boot.img", "userdata.img"]

        for workers in (1, 8):
            core = ExtractorCore(workers=workers, log=logs.append, output_root=out_root)
            final_path = core.extract(url, targets)
            check("端到端目录名 workers=%d" % workers,
                  os.path.basename(final_path) == "固件 OTA 测试",
                  os.path.basename(final_path))
            same = True
            for name, raw in expected.items():
                path = os.path.join(final_path, name + ".img")
                if not os.path.exists(path):
                    same = False
                    break
                with open(path, "rb") as handle:
                    if handle.read() != raw:
                        same = False
                        break
            check("端到端镜像一致性 workers=%d" % workers, same)

        # 4) 取消 ------------------------------------------------------ #
        core = ExtractorCore(workers=4, log=logs.append, output_root=out_root)
        core.cancel.set()
        cancelled = False
        try:
            core.extract(url, ["boot.img"])
        except CancelledError:
            cancelled = True
        check("取消机制", cancelled)

        # 4b) 下载缓冲：大文件走 %TEMP%，成功/取消都不留垃圾
        import glob as _glob

        def buffers():
            return set(_glob.glob(os.path.join(tempfile.gettempdir(), "fw_buf_*.bin")))

        before = buffers()
        saved_limit = MEM_BUFFER_LIMIT
        mem_buffer_limit_override = 1024        # 强制走磁盘缓冲分支
        try:
            _set_mem_limit(mem_buffer_limit_override)
            core_ok = ExtractorCore(workers=4, log=logs.append, output_root=out_root)
            core_ok.extract(url, ["boot.img"])
            leaked_ok = buffers() - before

            core_cancel = ExtractorCore(workers=4, log=logs.append, output_root=out_root)
            core_cancel.cancel.set()
            try:
                core_cancel.extract(url, ["boot.img"])
            except CancelledError:
                pass
            leaked_cancel = buffers() - before
            check("下载缓冲用完即删", not leaked_ok and not leaked_cancel,
                  "成功残留 %d 个，取消残留 %d 个" % (len(leaked_ok), len(leaked_cancel)))
        finally:
            _set_mem_limit(saved_limit)

        # 5) 普通 zip 分支（无 payload.bin）---------------------------- #
        plain = os.path.join(tmpdir, "plain.zip")
        with zipfile.ZipFile(plain, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("system.img", b"A" * 200000)
            archive.writestr("vendor.img", b"B" * 100000)
        core = ExtractorCore(workers=4, log=logs.append, output_root=out_root)
        path = core.extract("http://127.0.0.1:%d/plain.zip" % port,
                            ["system.img", "vendor.img"])
        check("普通 zip 并行提取",
              os.path.getsize(os.path.join(path, "system.img")) == 200000
              and os.path.getsize(os.path.join(path, "vendor.img")) == 100000,
              os.path.basename(path))

        # 6) Content-Disposition 兜底 ---------------------------------- #
        dispatch_url = "http://127.0.0.1:%d/download?id=1" % port
        session = ExtractorCore().new_session()
        got = derive_folder_name(dispatch_url, session)
        check("Content-Disposition 兜底", got == "真实名", "got=%r" % got)

        # 6b) 对象存储预签名直链：文件名在 response-content-disposition 查询参数里
        presigned = ("https://ykj-eos-dg5-01.eos-dongguan-6.cmecloud.cn/"
                     "d97289a8a7eb408d8167a659fd6f7c48086"
                     "?response-content-disposition=attachment%3B%20filename%2A%3D"
                     "UTF-8%27%27PLQ110_16.0.5.702.zip&X-Amz-Signature=c620d9f2")
        check("预签名取名(query 参数)",
              derive_folder_name(presigned) == "PLQ110_16.0.5.702",
              "got=%r" % derive_folder_name(presigned))

        # 6c) HEAD 被拒（403）、只有 GET 头带文件名 -> 用 1 字节 Range GET 取名
        with open(os.path.join(tmpdir, "presign_hash"), "wb") as handle:
            handle.write(b"x")
        session2 = ExtractorCore().new_session()
        got = derive_folder_name("http://127.0.0.1:%d/presign_hash" % port, session2)
        check("HEAD 403 时用 GET 取名", got == "预签名", "got=%r" % got)

        # 7) GUI 冒烟（子进程起窗口并截图后退出；无桌面环境时设 FW_NO_GUI=1 跳过）
        if os.environ.get("FW_NO_GUI") != "1":
            import subprocess

            shot = os.path.join(tmpdir, "gui.png")
            argument = "--screenshot=" + shot
            if getattr(sys, "frozen", False):
                command = [sys.executable, argument]
            else:
                command = [sys.executable, os.path.abspath(__file__), argument]
            ok, detail = False, ""
            child_env = dict(os.environ)
            # onefile 打包后嵌套启动自身时，必须清掉 PyInstaller 的继承状态，
            # 否则子进程会复用父进程的解包目录并崩溃（0xC0000409）
            for key in ("_MEIPASS", "_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                        "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL",
                        "_PYI_APPLICATION_HOME_DIR"):
                child_env.pop(key, None)
            child_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            try:
                import subprocess as _sp
                proc = _sp.run(command, timeout=180, env=child_env,
                               stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                ok = proc.returncode == 0 and os.path.exists(shot)
                detail = "rc=%s" % proc.returncode
                if not ok:
                    error_log = os.path.join(app_dir(), "extractor_error.log")
                    if os.path.exists(error_log):
                        with open(error_log, "r", encoding="utf-8", errors="replace") as handle:
                            tail = handle.read().strip().splitlines()
                        if tail:
                            detail += " | " + tail[-1][:160]
            except Exception as exc:
                detail = str(exc)
            check("GUI 窗口冒烟", ok, detail)

    except Exception:
        check("自检异常", False, traceback.format_exc().splitlines()[-1])
        lines.append(traceback.format_exc())
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        with open(os.path.join(app_dir(), "selftest.log"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except Exception:
        pass

    print("自检结果: %d 项通过, %d 项失败" % (len(lines) - len(failures), len(failures)))
    return 0 if not failures else 1


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #

REAL_OTA_URL = ("https://googledownloads.cn/android/aosp/"
                "cubs-ota-cd1a.260618.001.a7-7f02515d.zip")


def run_e2e(window, log_path):
    """真·端到端点测：驱动真实窗口 + 真实网络 + 真实提取，逐项写日志

    用法：python qt_extractor.py --e2e=e2e.log
    返回 True 表示全部通过。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance()
    results = []

    try:
        open(log_path, "w", encoding="utf-8").close()
    except Exception:
        pass

    def record(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        line = "%s %s%s" % ("PASS" if ok else "FAIL", name, (" | " + detail) if detail else "")
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    def skip(name, detail=""):
        results.append((name, None, detail))
        line = "SKIP %s%s" % (name, (" | " + detail) if detail else "")
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    def log_tail(count=2):
        try:
            text = window.log_edit.toPlainText().strip().splitlines()
        except Exception:
            return ""
        return " / ".join(text[-count:]) if text else ""

    def pump(ms):
        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.03)

    def wait(pred, timeout, on_tick=None):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if on_tick is not None:
                on_tick()
            if pred():
                return True
            time.sleep(0.03)
        return False

    # 弹窗类阻塞全部打桩，避免自动化被模态框卡住
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    opened = []
    window.open_path = staticmethod(lambda path: opened.append(path))

    loading_mark = getattr(type(window), "LOADING_MARK", "__loading__")

    def real_children(item):
        """排除“加载中…”占位项后的真实子节点数"""
        if item is None:
            return 0
        return sum(1 for index in range(item.childCount())
                   if item.child(index).data(0, Qt.UserRole) != loading_mark)

    pump(1200)

    # 1. 窗口
    record("GUI 启动", window.isVisible(), window.windowTitle())

    # 2. 目录树根节点
    root = window.tree.topLevelItem(0)
    ok = wait(lambda: real_children(root) > 0, 60)
    record("目录树根节点加载", ok, "子节点 %d" % real_children(root))

    # 3. 展开年份（2020 在主站为空，走镜像兜底）
    year_item = None
    if root is not None:
        for index in range(root.childCount()):
            child = root.child(index)
            if child.text(0).startswith("2020"):
                year_item = child
                break
        if year_item is None and root.childCount():
            year_item = root.child(0)
    if year_item is not None:
        year_item.setExpanded(True)
        ok = wait(lambda: real_children(year_item) > 0, 180)
        record("展开年份目录", ok, "%s → %d 个机型" % (year_item.text(0), real_children(year_item)))
    else:
        record("展开年份目录", False, "没有年份节点")

    # 4. 展开机型目录
    device_item = None
    if year_item is not None:
        for index in range(year_item.childCount()):
            child = year_item.child(index)
            if child.data(0, Qt.UserRole) != loading_mark:
                device_item = child
                break
    if device_item is not None:
        device_item.setExpanded(True)
        ok = wait(lambda: real_children(device_item) > 0, 180)
        record("展开机型目录", ok,
               "%s → %d 项" % (device_item.text(0), real_children(device_item)))
    else:
        record("展开机型目录", False, "没有机型节点")

    # 5. 单击 zip 节点填充链接（机型目录下面还可能有子目录，逐层下钻）
    def find_zip_item():
        for node in window._walk_items():
            href = node.data(0, Qt.UserRole)
            if isinstance(href, str) and href.lower().split("?")[0].endswith(".zip"):
                return node
        return None

    zip_item = None
    for _round in range(6):
        zip_item = find_zip_item()
        if zip_item is not None:
            break
        leaves = []
        for node in window._walk_items():
            href = node.data(0, Qt.UserRole)
            if isinstance(href, str) and href.endswith("/") and real_children(node) == 0:
                leaves.append(node)
        if not leaves:
            break
        leaves[0].setExpanded(True)
        wait(lambda: real_children(leaves[0]) > 0, 60)

    if zip_item is not None:
        window.on_tree_clicked(zip_item, 0)
        pump(300)
        url_text = window.url_edit.text()
        record("单击 zip 填充链接", url_text.lower().split("?")[0].endswith(".zip"),
               url_text[-70:])
    else:
        record("单击 zip 填充链接", False, "展开 6 层仍没有 zip 节点")

    # 6. 型号搜索
    if device_item is not None:
        keyword = device_item.text(0).rstrip("/")
        window.search_edit.setText(keyword)
        window.search_model()
        pump(300)
        record("型号搜索", window.tree.currentItem() is not None, keyword,)
    else:
        record("型号搜索", False, "无节点可搜")

    # 7. 来源切换
    window.radio_direct.setChecked(True)
    pump(300)
    hidden_on_direct = not window.browser_box.isVisible()
    window.radio_moto.setChecked(True)
    pump(1200)
    record("来源切换(直接链接隐藏浏览器)", hidden_on_direct and window.browser_box.isVisible())

    # 7b. 主题切换（真实点击按钮 + 截图）
    window.btn_theme.click()  # 切到深色
    pump(300)
    dark_on = (window._dark_theme
               and "#1e1e1e" in (QApplication.instance().styleSheet() or ""))
    try:
        window.grab().save(os.path.join(os.path.dirname(log_path) or ".", "dark-mode.png"))
    except Exception:
        pass
    window.btn_theme.click()  # 切回浅色
    pump(300)
    light_back = (not window._dark_theme
                  and "#1e1e1e" not in (QApplication.instance().styleSheet() or ""))
    record("深色模式切换", dark_on and light_back,
           "深色生效=%s 浅色恢复=%s" % (dark_on, light_back))

    # 8. OTA 列表抓取（真实页面，用桩替代模态对话框）
    captured = {}

    def fake_dialog(devices):
        captured["devices"] = devices
        captured["versions"] = sum(len(d["versions"]) for d in devices)

    window.show_ota_dialog = fake_dialog
    window.btn_ota.click()          # 走真实按钮 -> clicked(bool) -> 槽
    ok = wait(lambda: "devices" in captured, 90)
    record("获取 Google OTA 列表", ok,
           "机型 %d / 版本 %d" % (len(captured.get("devices", [])), captured.get("versions", 0)))
    window.show_ota_dialog = type(window).show_ota_dialog

    # 8b. OTA 对话框排版与选择
    dialog = None
    try:
        dialog = window.ota_dialog_class(window, captured.get("devices", []))
        labels = [dialog.device_list.item(index).text()
                  for index in range(dialog.device_list.count())]
        dirty = []
        for label in labels:
            name, _sep, code = label.partition(" — ")
            if any(word in label for word in ("的代号为", "适用于", "表示", "对应")):
                dirty.append(label)
            elif code and (name.endswith("（%s）" % code) or name.endswith("(%s)" % code)):
                dirty.append(label)          # 代号在两侧重复出现
            elif name.count("(") != name.count(")") or name.count("（") != name.count("）"):
                dirty.append(label)          # 括号不配对
        version_rows = dialog.version_tree.topLevelItemCount()
        link_text = dialog.link_edit.text()
        try:
            shot = os.path.join(os.path.dirname(log_path) or ".", "ota-dialog.png")
            dialog.grab().save(shot)
        except Exception:
            pass
        dialog.filter_edit.setText("pixel 11")
        pump(250)
        filtered = dialog.device_list.count()
        dialog.filter_edit.setText("")
        pump(250)
        restored = dialog.device_list.count()
        if dialog.version_tree.topLevelItemCount():
            dialog.version_tree.setCurrentItem(dialog.version_tree.topLevelItem(0))
            dialog._accept_selection()
        chosen = dialog.chosen or ""
        dialog.close()
        dialog = None

        # 列表必须从新到旧（Pixel 11 在 Pixel 10 之前）
        names = [label.partition(" — ")[0] for label in labels]
        years = [device_year(name) for name in names]
        ordered = all(years[index] >= years[index + 1]
                      for index in range(len(years) - 1))
        record("OTA 列表从新到旧",
               ordered and names and device_year(names[0]) >= 2025,
               "首项 %s(%.1f)，末项 %s(%.1f)" % (names[0], years[0],
                                               names[-1], years[-1]) if names else "空列表")
        record("OTA 对话框排版与选择",
               not dirty and version_rows > 0 and link_text.lower().endswith(".zip")
               and 0 < filtered < restored and chosen.lower().endswith(".zip"),
               "机型 %d 个(脏标签 %d)，首个机型 %d 个版本，筛选后 %d 个，选中 %s" % (
                   len(labels), len(dirty), version_rows, filtered,
                   chosen.rsplit("/", 1)[-1][:36]))
    except Exception as exc:
        record("OTA 对话框排版与选择", False, str(exc)[:120])
        if dialog is not None:
            try:
                dialog.close()
            except Exception:
                pass

    # 9. 解析真实 Full OTA
    rate_limited = False
    window.url_edit.setText(REAL_OTA_URL)
    window.btn_list.click()         # 走真实按钮
    ok = wait(lambda: window.file_list.count() > 0, 150)
    status = window.status_label.text()
    tail = log_tail(2)
    if not ok and ("429" in tail or "限流" in tail):
        rate_limited = True
        skip("解析列表(真实 Full OTA)", "Google 限流(HTTP 429)，稍后重跑即可：%s" % tail[-60:])
    else:
        record("解析列表(真实 Full OTA)", ok and "Full OTA" in status,
               "%d 项 | %s" % (window.file_list.count(), status[:40]))

    # 10. 真实提取 boot.img（并行下载 + 网速 + 进度）
    if rate_limited:
        skip("真实提取 boot.img", "上一步被 Google 限流，跳过")
    else:
        for widget in (window.chk_init, window.chk_vbmeta, window.chk_vendor):
            widget.setChecked(False)
        window.chk_boot.setChecked(True)
        speeds = []
        window.btn_extract.click()      # 走真实按钮

        def tick():
            window.refresh_speed()
            speeds.append(window.speed_label.text())

        ok = wait(lambda: window.btn_extract.isEnabled() and window.progress.value() >= 100, 300,
                  on_tick=tick)
        boot_path = None
        for root_dir, _dirs, files in os.walk(default_output_root()):
            if "boot.img" in files:
                candidate = os.path.join(root_dir, "boot.img")
                if boot_path is None or os.path.getmtime(candidate) > os.path.getmtime(boot_path):
                    boot_path = candidate
        magic = b""
        if boot_path:
            with open(boot_path, "rb") as handle:
                magic = handle.read(8)
        live_speed_seen = any("MB/s" in text for text in speeds)
        final_label = window.speed_label.text()
        record("真实提取 boot.img",
               ok and magic == b"ANDROID!" and (live_speed_seen or "平均" in final_label),
               "img=%s 大小=%s 实时网速=%s 完成提示=%s" % (
                   "有" if boot_path else "无",
                   fmt_size(os.path.getsize(boot_path)) if boot_path else "-",
                   "有" if live_speed_seen else "无",
                   final_label.split("（")[-1][:40] if "（" in final_label else "无平均速度"))

    # 11. 取消提取
    if rate_limited:
        skip("提取中取消", "上一步被 Google 限流，跳过")
    else:
        cancel_target = None
        for index in range(window.file_list.count()):
            item = window.file_list.item(index)
            if item.data(Qt.UserRole) in ("system", "vendor", "product", "system_ext"):
                item.setCheckState(Qt.Checked)
                cancel_target = item.data(Qt.UserRole)
                break
        window.chk_boot.setChecked(False)
        window.btn_extract.click()      # 走真实按钮
        pump(4000)
        window.btn_cancel.click()       # 走真实按钮
        ok = wait(lambda: window.btn_extract.isEnabled(), 120)
        record("提取中取消", ok, "目标分区 %s，日志/状态已恢复" % cancel_target)

    # 12. 代理可用场景
    window.chk_proxy.setChecked(True)
    window.port_edit.setText("10808")
    core = window.make_core()
    proxy_ok, proxy_detail = False, ""
    for _attempt in range(2):                 # 代理链路偶尔抖动，重试一次
        try:
            devices = fetch_ota_devices(core)
            proxy_ok = len(devices) > 10
            proxy_detail = "机型 %d" % len(devices)
            break
        except Exception as exc:
            proxy_detail = str(exc)[:80]
            pump(600)
    lowered = proxy_detail.lower()
    if not proxy_ok and ("timed out" in lowered or "time out" in lowered
                         or "429" in lowered):
        skip("代理下载(127.0.0.1:10808)", "网络抖动/限流：%s" % proxy_detail)
    else:
        record("代理下载(127.0.0.1:10808)", proxy_ok, proxy_detail)

    # 13. 代理不可用场景（必须优雅报错，不能崩）
    window.port_edit.setText("1")
    core = window.make_core()
    dead_ok = False
    try:
        fetch_ota_devices(core)
        dead_ok = False
    except Exception:
        dead_ok = True
    window.chk_proxy.setChecked(False)
    record("代理端口不可用时报错不崩", dead_ok)

    # 14. 打开输出目录
    window.btn_open.click()
    pump(300)
    record("打开输出目录按钮", bool(opened) and os.path.isdir(default_output_root()),
           opened[0] if opened else "未触发")

    # 15. 关闭窗口
    window.close()
    pump(500)
    record("关闭窗口", not window.isVisible())

    failures = [name for name, ok, _detail in results if ok is False]
    skips = [name for name, ok, _detail in results if ok is None]
    passed = len(results) - len(failures) - len(skips)
    summary = "端到端点测: %d 项通过, %d 项失败, %d 项跳过" % (passed, len(failures), len(skips))
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    except Exception:
        pass
    print(summary, flush=True)
    return not failures


def _qt_namespace():
    from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
    from PyQt5.QtGui import QFont, QIcon
    from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                                 QFrame, QGroupBox, QHBoxLayout, QHeaderView,
                                 QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                 QMainWindow, QMessageBox, QPlainTextEdit,
                                 QProgressBar, QPushButton, QRadioButton,
                                 QSizePolicy, QSpinBox, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout, QWidget)
    return dict(globals=locals())


def _ensure_qt_plugins():
    """源码运行时（依赖装在 libs/ 下）显式指定 Qt 插件目录；打包后不需要"""
    if getattr(sys, "frozen", False):
        return
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs",
                     "PyQt5", "Qt5", "plugins"),
        os.path.join(sys.prefix, "Lib", "site-packages", "PyQt5", "Qt5", "plugins"),
    ]
    for path in candidates:
        if os.path.isdir(path):
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", path)
            os.environ.setdefault("QT_PLUGIN_PATH", path)
            return


def main():
    if "--selftest" in sys.argv[1:]:
        sys.exit(run_selftest())
    if "--version" in sys.argv[1:]:
        print(APP_VERSION)
        return

    _ensure_qt_plugins()
    ns = _qt_namespace()["globals"]
    QObject = ns["QObject"]
    QThread = ns["QThread"]
    QTimer = ns["QTimer"]
    Qt = ns["Qt"]
    pyqtSignal = ns["pyqtSignal"]
    QApplication = ns["QApplication"]
    QWidget = ns["QWidget"]
    QVBoxLayout = ns["QVBoxLayout"]
    QHBoxLayout = ns["QHBoxLayout"]
    QGroupBox = ns["QGroupBox"]
    QLabel = ns["QLabel"]
    QLineEdit = ns["QLineEdit"]
    QPushButton = ns["QPushButton"]
    QCheckBox = ns["QCheckBox"]
    QRadioButton = ns["QRadioButton"]
    QTreeWidget = ns["QTreeWidget"]
    QTreeWidgetItem = ns["QTreeWidgetItem"]
    QHeaderView = ns["QHeaderView"]
    QProgressBar = ns["QProgressBar"]
    QPlainTextEdit = ns["QPlainTextEdit"]
    QFrame = ns["QFrame"]
    QComboBox = ns["QComboBox"]
    QDialog = ns["QDialog"]
    QMessageBox = ns["QMessageBox"]
    QSizePolicy = ns["QSizePolicy"]
    QSpinBox = ns["QSpinBox"]
    QListWidget = ns["QListWidget"]
    QListWidgetItem = ns["QListWidgetItem"]
    QMainWindow = ns["QMainWindow"]
    QFont = ns["QFont"]
    QIcon = ns["QIcon"]

    class Bridge(QObject):
        log_line = pyqtSignal(str)
        progress = pyqtSignal(int)

    class Job(QObject):
        done = pyqtSignal(object)
        failed = pyqtSignal(str)

        def __init__(self, fn):
            super(Job, self).__init__()
            self._fn = fn

        def run(self):
            try:
                self.done.emit(self._fn())
            except CancelledError:
                self.done.emit(None)
            except Exception as exc:
                self.failed.emit("%s\n%s" % (exc, traceback.format_exc()))

    class DirFetch(QThread):
        done = pyqtSignal(str, object, object, str)
        failed = pyqtSignal(str, str)

        def __init__(self, url, proxy, parent=None):
            super(DirFetch, self).__init__(parent)
            self._url = url
            self._proxy = proxy
            self._core = None

        def stop(self):
            if self._core is not None:
                self._core.cancel.set()

        def run(self):
            try:
                self._core = ExtractorCore(proxy=self._proxy)
                dirs, files, base = self._core.list_dir(self._url)
                self.done.emit(self._url, dirs, files, base)
            except CancelledError:
                pass
            except Exception as exc:
                self.failed.emit(self._url, str(exc))

    class OtaDialog(QDialog):
        """OTA 下载链接选择框：左边机型（可筛选），右边版本 + 文件名"""

        def __init__(self, parent, devices):
            super(OtaDialog, self).__init__(parent)
            self.setWindowTitle("OTA 下载链接")
            self.resize(1000, 580)
            self.devices = devices
            self.chosen = None
            self._device_index = []

            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(8)

            filter_row = QHBoxLayout()
            filter_row.addWidget(QLabel("筛选机型:"))
            self.filter_edit = QLineEdit()
            self.filter_edit.setPlaceholderText("输入机型或代号，例如 Pixel 11 / grizzly")
            filter_row.addWidget(self.filter_edit, stretch=1)
            layout.addLayout(filter_row)

            body = QHBoxLayout()
            self.device_list = QListWidget()
            self.device_list.setFixedWidth(300)
            body.addWidget(self.device_list)

            right = QVBoxLayout()
            self.version_tree = QTreeWidget()
            self.version_tree.setHeaderLabels(["版本", "文件名"])
            self.version_tree.setRootIsDecorated(False)
            self.version_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
            self.version_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
            right.addWidget(self.version_tree, stretch=1)

            link_row = QHBoxLayout()
            link_row.addWidget(QLabel("链接:"))
            self.link_edit = QLineEdit()
            self.link_edit.setReadOnly(True)
            link_row.addWidget(self.link_edit, stretch=1)
            right.addLayout(link_row)
            body.addLayout(right, stretch=1)
            layout.addLayout(body, stretch=1)

            buttons = QHBoxLayout()
            buttons.addStretch()
            self.btn_choose = QPushButton("选择该版本")
            self.btn_choose.setMinimumHeight(28)
            self.btn_choose.clicked.connect(self._accept_selection)
            self.btn_close = QPushButton("关闭")
            self.btn_close.setMinimumHeight(28)
            self.btn_close.clicked.connect(self.reject)
            buttons.addWidget(self.btn_choose)
            buttons.addWidget(self.btn_close)
            layout.addLayout(buttons)

            self.filter_edit.textChanged.connect(self._refresh_devices)
            self.device_list.currentRowChanged.connect(self._on_device_changed)
            self.version_tree.currentItemChanged.connect(self._on_version_changed)
            self.version_tree.itemDoubleClicked.connect(lambda *_: self._accept_selection())
            self._refresh_devices()

        def _filtered(self):
            keyword = self.filter_edit.text().strip().lower()
            if not keyword:
                return list(range(len(self.devices)))
            return [index for index, device in enumerate(self.devices)
                    if keyword in ("%s %s" % (device["name"], device["codename"])).lower()]

        def _refresh_devices(self, *_args):
            self.device_list.blockSignals(True)
            self.device_list.clear()
            self._device_index = self._filtered()
            for index in self._device_index:
                device = self.devices[index]
                item = QListWidgetItem("%s — %s" % (device["name"], device["codename"]))
                item.setToolTip("%s（%s）%d 个版本" % (
                    device["name"], device["codename"], len(device["versions"])))
                self.device_list.addItem(item)
            self.device_list.blockSignals(False)
            if self._device_index:
                self.device_list.setCurrentRow(0)
            else:
                self.version_tree.clear()
                self.link_edit.clear()

        def _on_device_changed(self, row):
            if row < 0 or row >= len(self._device_index):
                return
            device = self.devices[self._device_index[row]]
            self.version_tree.clear()
            self.link_edit.clear()
            for version, link in device["versions"]:
                file_name = link.rstrip("/").rsplit("/", 1)[-1]
                item = QTreeWidgetItem(self.version_tree, [version, file_name])
                item.setData(0, Qt.UserRole, link)
                item.setToolTip(1, link)
            if self.version_tree.topLevelItemCount():
                self.version_tree.setCurrentItem(self.version_tree.topLevelItem(0))

        def _on_version_changed(self, current, _previous=None):
            if current is None:
                return
            self.link_edit.setText(current.data(0, Qt.UserRole) or "")

        def _accept_selection(self):
            item = self.version_tree.currentItem()
            if item is None:
                return
            self.chosen = item.data(0, Qt.UserRole)
            self.accept()

    class MainWindow(QMainWindow):
        LOADING_MARK = "__loading__"
        ota_dialog_class = None

        def __init__(self):
            super(MainWindow, self).__init__()
            self.setWindowTitle("%s  v%s" % (APP_NAME, APP_VERSION))
            self.resize(980, 840)
            try:
                self.setWindowIcon(QIcon(resource_path("app.ico")))
            except Exception:
                pass

            self.core = None
            self.job = None
            self.thread = None
            self._on_done = None
            self._dir_threads = []
            self._last_speed = 0.0
            self._dark_theme = False

            self._build_ui()

            self.bridge = Bridge()
            self.bridge.log_line.connect(self.append_log)
            self.bridge.progress.connect(self.progress.setValue)

            self.speed_timer = QTimer(self)
            self.speed_timer.setInterval(250)
            self.speed_timer.timeout.connect(self.refresh_speed)
            self.speed_timer.start()

            removed, freed = cleanup_stale_buffers()
            if removed:
                self.append_log("已清理上次残留的下载缓冲 %d 个（%s），位置: %s"
                                % (removed, fmt_size(freed), tempfile.gettempdir()))

            self.btn_theme.toggled.connect(self.toggle_theme)
            self.apply_theme(load_theme_setting(), save=False)

            self._load_tree_root()

        # -- UI -------------------------------------------------------- #

        def _build_ui(self):
            central = QWidget()
            self.setCentralWidget(central)
            root = QVBoxLayout(central)
            root.setContentsMargins(12, 12, 12, 12)
            root.setSpacing(8)

            top = QHBoxLayout()
            self.chk_proxy = QCheckBox("开启代理")
            top.addWidget(self.chk_proxy)
            top.addWidget(QLabel("127.0.0.1 :"))
            self.port_edit = QLineEdit("7897")
            self.port_edit.setFixedWidth(64)
            top.addWidget(self.port_edit)

            divider = QFrame()
            divider.setFrameShape(QFrame.VLine)
            top.addWidget(divider)

            top.addWidget(QLabel("并发下载:"))
            self.spin_workers = QSpinBox()
            self.spin_workers.setRange(1, 16)
            self.spin_workers.setValue(DEFAULT_WORKERS)
            self.spin_workers.setFixedWidth(60)
            top.addWidget(self.spin_workers)

            divider2 = QFrame()
            divider2.setFrameShape(QFrame.VLine)
            top.addWidget(divider2)

            top.addWidget(QLabel("固件来源:"))
            self.radio_moto = QRadioButton("Lenomola (Moto)")
            self.radio_lenovo = QRadioButton("Lenowow (Lenovo)")
            self.radio_direct = QRadioButton("直接链接")
            self.radio_moto.setChecked(True)
            for radio in (self.radio_moto, self.radio_lenovo, self.radio_direct):
                top.addWidget(radio)
                radio.toggled.connect(self._on_source_changed)
            top.addStretch()

            self.btn_theme = QPushButton("🌙 深色")
            self.btn_theme.setCheckable(True)
            self.btn_theme.setMinimumHeight(28)
            self.btn_theme.setToolTip("切换浅色 / 深色主题（自动记住上次选择）")
            top.addWidget(self.btn_theme)
            root.addLayout(top)

            self.browser_box = QGroupBox("固件浏览器")
            box = QVBoxLayout(self.browser_box)
            search_row = QHBoxLayout()
            search_row.addWidget(QLabel("型号搜索:"))
            self.search_edit = QLineEdit()
            self.search_edit.setFixedWidth(180)
            self.search_edit.returnPressed.connect(self.search_model)
            search_row.addWidget(self.search_edit)
            search_row.addStretch()
            box.addLayout(search_row)

            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["目录", "大小"])
            self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
            self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            self.tree.setIndentation(18)
            self.tree.itemExpanded.connect(self.on_tree_expanded)
            self.tree.itemClicked.connect(self.on_tree_clicked)
            box.addWidget(self.tree)
            root.addWidget(self.browser_box)

            url_row = QHBoxLayout()
            self.btn_ota = QPushButton("获取 OTA 列表")
            self.btn_ota.clicked.connect(self.start_fetch_ota)
            url_row.addWidget(self.btn_ota)
            self.btn_load = QPushButton("加载固件")
            self.btn_load.clicked.connect(self.load_selected_firmware)
            url_row.addWidget(self.btn_load)
            self.url_edit = QLineEdit()
            self.url_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            url_row.addWidget(self.url_edit)
            self.btn_list = QPushButton("解析列表")
            self.btn_list.clicked.connect(self.start_list)
            url_row.addWidget(self.btn_list)
            self.btn_extract = QPushButton("开始提取")
            self.btn_extract.clicked.connect(self.start_extract)
            url_row.addWidget(self.btn_extract)
            self.btn_cancel = QPushButton("取消")
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.clicked.connect(self.cancel_job)
            url_row.addWidget(self.btn_cancel)
            root.addLayout(url_row)

            quick = QHBoxLayout()
            self.chk_boot = QCheckBox("boot.img")
            self.chk_init = QCheckBox("init_boot.img")
            self.chk_vbmeta = QCheckBox("vbmeta.img")
            self.chk_vendor = QCheckBox("vendor_boot.img")
            self.chk_boot.setChecked(True)
            self.chk_init.setChecked(True)
            for widget in (self.chk_boot, self.chk_init, self.chk_vbmeta, self.chk_vendor):
                quick.addWidget(widget)
            quick.addStretch()
            root.addLayout(quick)

            self.status_label = QLabel("")
            root.addWidget(self.status_label)
            root.addWidget(QLabel("文件列表:"))

            self.file_list = QListWidget()
            root.addWidget(self.file_list, stretch=1)

            self.progress = QProgressBar()
            self.progress.setValue(0)
            self.progress.setFormat("%p%")
            root.addWidget(self.progress)

            self.speed_label = QLabel("就绪")
            root.addWidget(self.speed_label)

            for button in (self.btn_ota, self.btn_load, self.btn_list, self.btn_extract,
                           self.btn_cancel):
                button.setMinimumHeight(28)

            bottom = QHBoxLayout()
            self.log_edit = QPlainTextEdit()
            self.log_edit.setReadOnly(True)
            self.log_edit.setFont(QFont("Consolas", 9))
            self.log_edit.setMaximumHeight(130)
            bottom.addWidget(self.log_edit)
            self.btn_open = QPushButton("打开输出目录")
            self.btn_open.setMinimumHeight(28)
            self.btn_open.setMinimumWidth(110)
            self.btn_open.clicked.connect(self.open_output_dir)
            bottom.addWidget(self.btn_open)
            root.addLayout(bottom)

        # -- 主题 -------------------------------------------------------- #

        def apply_theme(self, dark, save=True):
            """切换主题：整套 QSS + 日志底色 + 按钮文案，并记住选择"""
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(build_stylesheet(dark))
            self._dark_theme = bool(dark)
            self.log_edit.setStyleSheet(
                "QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; }"
                if dark else "QPlainTextEdit { background: #f8f8f8; color: #202020; }")
            self.btn_theme.blockSignals(True)   # 防止 setChecked 反过来触发 toggle
            self.btn_theme.setChecked(bool(dark))
            self.btn_theme.blockSignals(False)
            self.btn_theme.setText("☀️ 浅色" if dark else "🌙 深色")
            if save:
                try:
                    with open(theme_config_path(), "w", encoding="utf-8") as handle:
                        handle.write("dark" if dark else "light")
                except Exception:
                    pass

        @slot_guard
        def toggle_theme(self, *_args):
            self.apply_theme(not self._dark_theme)

        # -- 日志 / 测速 ------------------------------------------------ #

        def append_log(self, message):
            self.log_edit.appendPlainText(message)

        @slot_guard
        def refresh_speed(self):
            core = self.core
            if core is None:
                return
            if core.phase == "download":
                instant = core.speed.sample(core.bytes_done)
                if instant > 0:
                    self._last_speed = (instant if self._last_speed <= 0
                                        else 0.65 * self._last_speed + 0.35 * instant)
                self.speed_label.setText(
                    "↓ %s    已下载 %s / %s    剩余 %s" % (
                        fmt_speed(self._last_speed),
                        fmt_size(core.bytes_done),
                        fmt_size(core.dl_total),
                        fmt_eta(core.bytes_done, core.dl_total, self._last_speed)))
            elif core.phase == "write":
                self.speed_label.setText("正在写入镜像，请稍候…")
            elif core.phase == "done":
                # 不要覆盖 extract_done() 写好的“完成：xxx（下载…平均…）”摘要
                if not (self.speed_label.text().startswith("完成")
                        or self.speed_label.text().startswith("已取消")):
                    self.speed_label.setText("完成")

        # -- 配置 ------------------------------------------------------- #

        def proxy_dict(self):
            if not self.chk_proxy.isChecked():
                return None
            port = self.port_edit.text().strip() or "7897"
            return {"http": "http://127.0.0.1:%s" % port,
                    "https": "http://127.0.0.1:%s" % port}

        def make_core(self):
            core = ExtractorCore(proxy=self.proxy_dict(),
                                 workers=self.spin_workers.value(),
                                 log=self.bridge.log_line.emit,
                                 progress=self.bridge.progress.emit)
            self.core = core
            return core

        # -- 任务调度 --------------------------------------------------- #

        def run_job(self, fn, on_done=None):
            if self.thread is not None and self.thread.isRunning():
                self.append_log("已有任务在运行，请等待或取消")
                return
            self.set_busy(True)
            self._on_done = on_done
            self.thread = QThread()
            self.job = Job(fn)
            self.job.moveToThread(self.thread)
            self.thread.started.connect(self.job.run)
            self.job.done.connect(self._job_done)
            self.job.failed.connect(self._job_failed)
            self.thread.start()

        @slot_guard
        def _job_done(self, result):
            self.set_busy(False)
            if self.thread is not None:
                self.thread.quit()
                self.thread.wait(3000)
            callback, self._on_done = self._on_done, None
            if callback is not None:
                callback(result)

        @slot_guard
        def _job_failed(self, message):
            self.set_busy(False)
            if self.thread is not None:
                self.thread.quit()
                self.thread.wait(3000)
            self._on_done = None
            self.append_log("错误: %s" % message)
            self.progress.setValue(0)
            self.speed_label.setText("失败")

        def set_busy(self, busy):
            for button in (self.btn_ota, self.btn_list, self.btn_extract, self.btn_load):
                button.setEnabled(not busy)
            self.btn_cancel.setEnabled(busy)
            if busy:
                self._last_speed = 0.0
                self.progress.setValue(0)
                self.speed_label.setText("准备中…")

        @slot_guard
        def cancel_job(self):
            if self.core is not None:
                self.core.cancel.set()
                self.append_log("已请求取消，等待当前块结束…")

        # -- OTA 列表 --------------------------------------------------- #

        @slot_guard
        def start_fetch_ota(self):
            core = self.make_core()
            self.run_job(lambda: fetch_ota_devices(core), self.show_ota_dialog)

        @slot_guard
        def show_ota_dialog(self, devices):
            if not devices:
                return
            dialog = OtaDialog(self, devices)
            if dialog.exec_() == QDialog.Accepted and dialog.chosen:
                self.url_edit.setText(dialog.chosen)
                self.append_log("已选择链接: %s" % dialog.chosen)

        # -- 目录树 ----------------------------------------------------- #

        def current_source(self):
            if self.radio_moto.isChecked():
                return "lenomola"
            if self.radio_lenovo.isChecked():
                return "lenowow"
            return "direct"

        @slot_guard
        def _on_source_changed(self, *_args):
            source = self.current_source()
            self.tree.clear()
            if source == "direct":
                self.browser_box.setVisible(False)
                return
            self.browser_box.setVisible(True)
            self._load_tree_root()

        def _load_tree_root(self):
            source = self.current_source()
            if source == "direct":
                self.browser_box.setVisible(False)
                return
            base = "%s/%s/" % (LOLINET_BASE, source)
            root_item = QTreeWidgetItem(self.tree, [source, ""])
            root_item.setData(0, Qt.UserRole, base)
            self.fetch_children(root_item, base)

        @slot_guard
        def fetch_children(self, item, url):
            if not url or item.childCount() > 0 or item.data(0, Qt.UserRole + 1):
                return
            if not url.endswith("/"):
                return
            item.setData(0, Qt.UserRole + 1, True)
            loading = QTreeWidgetItem(item, ["加载中…", ""])
            loading.setData(0, Qt.UserRole, self.LOADING_MARK)
            worker = DirFetch(url, self.proxy_dict(), self)
            worker.done.connect(self._fill_children)
            worker.failed.connect(self._dir_failed)
            worker.finished.connect(worker.deleteLater)
            self._dir_threads = prune_finished_threads(self._dir_threads)
            self._dir_threads.append(worker)
            worker.start()

        @slot_guard
        def _dir_failed(self, url, message):
            target = self._find_item(url)
            if target is not None:
                self._clear_loading(target)
                target.setData(0, Qt.UserRole + 1, False)   # 允许重试
            self.append_log("加载目录失败: %s" % message)

        def _clear_loading(self, item):
            for index in range(item.childCount() - 1, -1, -1):
                child = item.child(index)
                if child.data(0, Qt.UserRole) == self.LOADING_MARK:
                    item.removeChild(child)

        def _find_item(self, url):
            for item in self._walk_items():
                if item.data(0, Qt.UserRole) == url:
                    return item
            return None

        @slot_guard
        def _fill_children(self, url, dirs, files, base):
            target = self._find_item(url)
            if target is None:
                return
            self._clear_loading(target)
            if base and base != url:
                target.setData(0, Qt.UserRole, base)   # 记住实际可用的镜像主机
            for name, href in dirs:
                child = QTreeWidgetItem(target, [name + "/", ""])
                child.setData(0, Qt.UserRole, href)
            for name, href in files:
                child = QTreeWidgetItem(target, [name, ""])
                child.setData(0, Qt.UserRole, href)

        def _walk_items(self, root=None):
            items = ([self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
                     if root is None
                     else [root.child(i) for i in range(root.childCount())])
            for item in items:
                if item is None:
                    continue
                yield item
                for sub in self._walk_items(item):
                    yield sub

        @slot_guard
        def on_tree_expanded(self, item):
            self.fetch_children(item, item.data(0, Qt.UserRole))

        @slot_guard
        def on_tree_clicked(self, item, _column):
            href = item.data(0, Qt.UserRole)
            if not href:
                return
            if href.lower().split("?")[0].endswith(".zip"):
                self.url_edit.setText(href)
                self.append_log("固件 URL 已填充: %s" % href)
            else:
                item.setExpanded(True)

        @slot_guard
        def load_selected_firmware(self):
            items = self.tree.selectedItems()
            if not items:
                self.append_log("请先在固件浏览器中选择一个固件")
                return
            href = items[0].data(0, Qt.UserRole)
            if href and href.lower().split("?")[0].endswith(".zip"):
                self.url_edit.setText(href)
                self.append_log("固件 URL 已填充: %s" % href)
            else:
                self.append_log("当前选中的是目录，请展开后选择 zip 固件")

        @slot_guard
        def search_model(self):
            query = self.search_edit.text().strip().lower()
            if not query:
                return
            hits = [item for item in self._walk_items() if query in item.text(0).lower()]
            if hits:
                self.tree.scrollToItem(hits[0])
                self.tree.setCurrentItem(hits[0])
                self.append_log("型号搜索: '%s' → %d 个匹配" % (query, len(hits)))
            else:
                self.append_log("型号搜索: '%s' 未找到，请先展开年份→设备目录" % query)

        # -- 解析 / 提取 ------------------------------------------------ #

        @slot_guard
        def start_list(self):
            url = self.url_edit.text().strip()
            if not url:
                self.append_log("请先输入固件链接")
                return
            core = self.make_core()
            self.run_job(lambda: core.list_remote(url), self.show_list)

        @slot_guard
        def show_list(self, result):
            if not result:
                return
            items, is_payload = result
            self.file_list.clear()
            for key, label, _size in sorted(items, key=lambda entry: entry[1].lower()):
                item = QListWidgetItem(label)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                item.setData(Qt.UserRole, key)
                self.file_list.addItem(item)
            total = sum(size for _key, _label, size in items if size)
            text = "共 %d 个" % len(items)
            if total:
                text += ", %s" % fmt_size(total)
            if is_payload:
                text += "（Full OTA payload，按分区提取）"
            self.status_label.setText(text)
            self.append_log("解析完成: %s" % text)

        def selected_targets(self):
            targets = []
            if self.chk_boot.isChecked():
                targets.append("boot.img")
            if self.chk_init.isChecked():
                targets.append("init_boot.img")
            if self.chk_vbmeta.isChecked():
                targets.append("vbmeta.img")
            if self.chk_vendor.isChecked():
                targets.append("vendor_boot.img")
            for index in range(self.file_list.count()):
                item = self.file_list.item(index)
                if item.checkState() == Qt.Checked:
                    targets.append(item.data(Qt.UserRole))
            ordered, seen = [], set()
            for target in targets:
                if target not in seen:
                    seen.add(target)
                    ordered.append(target)
            return ordered

        @slot_guard
        def start_extract(self):
            url = self.url_edit.text().strip()
            if not url:
                self.append_log("请先输入或选择固件链接")
                return
            targets = self.selected_targets()
            if not targets:
                self.append_log("请至少选择一个文件或分区")
                return
            core = self.make_core()
            self.append_log("开始提取，目标: %s" % ", ".join(targets))
            self.run_job(lambda: core.extract(url, targets), self.extract_done)

        @slot_guard
        def extract_done(self, path):
            if not path:
                self.append_log("提取已取消")
                self.speed_label.setText("已取消")
                return
            summary = ""
            core = self.core
            if core is not None and core.dl_total:
                average = core.average_speed()
                if average > 0:
                    summary = "（下载 %s，平均 %s）" % (fmt_size(core.dl_total), fmt_speed(average))
            self.speed_label.setText("完成：%s%s" % (path, summary))
            self.append_log("完成: %s%s" % (path, summary))
            QMessageBox.information(self, "完成", "提取完成！\n保存至: %s%s" % (path, summary))
            self.open_path(path)

        @slot_guard
        def open_output_dir(self):
            base = self.core.output_root if self.core is not None else default_output_root()
            self.open_path(base)

        @staticmethod
        def open_path(path):
            try:
                os.startfile(path)  # noqa: S606
            except Exception:
                pass

        def closeEvent(self, event):
            if self.core is not None:
                self.core.cancel.set()
            if self.thread is not None:
                try:
                    self.thread.quit()
                    self.thread.wait(5000)
                except RuntimeError:
                    pass
            still_running = False
            for worker in list(self._dir_threads):
                try:
                    worker.stop()
                    worker.quit()
                    if not worker.wait(800):
                        still_running = True
                except RuntimeError:
                    pass
            event.accept()
            if still_running:
                # 目录线程卡在 socket 上（requests 无法中断），
                # 直接结束进程，避免 QThread 运行中被析构导致 0xC0000409 崩溃
                time.sleep(0.2)
                os._exit(0)

    MainWindow.ota_dialog_class = OtaDialog
    global _THEME_OVERRIDE
    _THEME_OVERRIDE = None
    for argument in sys.argv[1:]:
        if argument.startswith("--theme="):
            _THEME_OVERRIDE = argument.split("=", 1)[1].lower()
            break
    application = QApplication(sys.argv)
    application.setStyleSheet(build_stylesheet(load_theme_setting()))
    window = MainWindow()
    window.show()

    shot_path = None
    for argument in sys.argv[1:]:
        if argument.startswith("--screenshot="):
            shot_path = argument.split("=", 1)[1]
    if shot_path:
        def grab_and_quit():
            try:
                window.grab().save(shot_path)
            finally:
                window.close()          # 走 closeEvent，等后台线程收尾后再退出

        QTimer.singleShot(2500, grab_and_quit)

    if "--expand-demo" in sys.argv[1:]:
        demo_log = os.path.join(app_dir(), "expand-demo.log")
        try:
            open(demo_log, "w").close()
        except Exception:
            pass

        def demo_write(message):
            try:
                with open(demo_log, "a", encoding="utf-8") as handle:
                    handle.write(message + "\n")
            except Exception:
                pass
            try:
                print(message, flush=True)
            except Exception:
                pass

        def demo_expand_root():
            root = window.tree.topLevelItem(0)
            if root is None:
                demo_write("demo: 没有根节点")
                window.close()
                return
            demo_write("demo: 展开根节点 %s" % root.text(0))
            root.setExpanded(True)

            def demo_expand_child():
                demo_write("demo: 一级子节点数 = %d" % root.childCount())
                if root.childCount():
                    child = root.child(0)
                    demo_write("demo: 展开 %s" % child.text(0))
                    child.setExpanded(True)

                    def demo_report():
                        demo_write("demo: 二级子节点数 = %d" % child.childCount())
                        demo_write("demo: 完成，无崩溃")
                        window.close()

                    QTimer.singleShot(14000, demo_report)

            QTimer.singleShot(6000, demo_expand_child)

        QTimer.singleShot(1500, demo_expand_root)

    e2e_log = None
    for argument in sys.argv[1:]:
        if argument.startswith("--e2e="):
            e2e_log = argument.split("=", 1)[1]
    if e2e_log:
        ok = run_e2e(window, e2e_log)
        application.quit()
        sys.exit(0 if ok else 1)

    sys.exit(application.exec_())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        try:
            with open(os.path.join(app_dir(), "extractor_error.log"), "w", encoding="utf-8") as handle:
                handle.write(traceback.format_exc())
        except Exception:
            pass
        raise





