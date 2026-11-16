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
import hashlib
import html as html_mod
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
from urllib.parse import quote, unquote, urljoin, urlsplit

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
APP_VERSION = "2.0.0"

MAX_RETRIES = 5
RETRY_DELAYS = [5, 10, 20, 40, 60]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
}
OTA_URL = "https://developers.google.cn/android/ota"
HTTP_TIMEOUT = 60
LOLINET_BASE = "https://mirrors.lolinet.com/firmware"

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


def _name_from_disposition(session, url):
    """URL 里没有可用文件名时，向服务器要 Content-Disposition"""
    if session is None:
        return ""
    try:
        response = session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        try:
            header = response.headers.get("Content-Disposition", "")
        finally:
            response.close()
    except Exception:
        return ""
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
        alt = _name_from_disposition(session, url)
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
        finally:
            os.close(handle)
        self._log("数据量 %s，使用磁盘缓冲" % fmt_size(total))
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
            raise
        executor.shutdown(wait=True)
        return BufferView(segments, self.buffer, tmp_path)


# --------------------------------------------------------------------------- #
# 来源解析（h5ai 目录 / Google OTA 页面）
# --------------------------------------------------------------------------- #

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


def parse_ota_page(html_text):
    headings = list(re.finditer(r'<h2[^>]*id="([a-zA-Z0-9_]+)"[^>]*>', html_text))
    devices = []
    for index, heading in enumerate(headings):
        codename = heading.group(1)
        segment = html_text[heading.start(): heading.start() + 500]
        name_match = re.search(r'data-text="([^"]*)"', segment)
        name = name_match.group(1) if name_match else codename
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
            pieces = file_name.split("-")
            version = "-".join(pieces[1:]) if len(pieces) > 1 else file_name
            versions.append((version, link))
        if versions:
            devices.append({"codename": codename, "name": name, "versions": versions})
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
        self.phase = "idle"
        self.speed = SpeedMeter()
        self._last_pct = -1
        self._probe_cache = {}

    # -- helpers ---------------------------------------------------------- #

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
        session = self.new_session()
        response = session.get(url, timeout=HTTP_TIMEOUT)
        try:
            if response.status_code != 200:
                raise RuntimeError("HTTP %d" % response.status_code)
            return parse_h5ai(response.text, url)
        finally:
            response.close()

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
                if "429" in str(exc) and attempt < MAX_RETRIES - 1:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    self.log("被限流，%ds 后重试 (%d/%d)..." % (delay, attempt + 1, MAX_RETRIES))
                    time.sleep(delay)
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

        info = self.probe(url)
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
        done = pyqtSignal(str, object, object)
        failed = pyqtSignal(str)

        def __init__(self, url, proxy, parent=None):
            super(DirFetch, self).__init__(parent)
            self._url = url
            self._proxy = proxy

        def run(self):
            try:
                dirs, files = ExtractorCore(proxy=self._proxy).list_dir(self._url)
                self.done.emit(self._url, dirs, files)
            except Exception as exc:
                self.failed.emit(str(exc))

    class MainWindow(QMainWindow):
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

            self._build_ui()

            self.bridge = Bridge()
            self.bridge.log_line.connect(self.append_log)
            self.bridge.progress.connect(self.progress.setValue)

            self.speed_timer = QTimer(self)
            self.speed_timer.setInterval(250)
            self.speed_timer.timeout.connect(self.refresh_speed)
            self.speed_timer.start()

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
                           self.btn_cancel, self.btn_open):
                button.setMinimumHeight(28)

            bottom = QHBoxLayout()
            self.log_edit = QPlainTextEdit()
            self.log_edit.setReadOnly(True)
            self.log_edit.setFont(QFont("Consolas", 9))
            self.log_edit.setMaximumHeight(130)
            bottom.addWidget(self.log_edit)
            self.btn_open = QPushButton("打开输出目录")
            self.btn_open.clicked.connect(self.open_output_dir)
            bottom.addWidget(self.btn_open)
            root.addLayout(bottom)

        # -- 日志 / 测速 ------------------------------------------------ #

        def append_log(self, message):
            self.log_edit.appendPlainText(message)

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

        def _job_done(self, result):
            self.set_busy(False)
            if self.thread is not None:
                self.thread.quit()
                self.thread.wait(3000)
            callback, self._on_done = self._on_done, None
            if callback is not None:
                callback(result)

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

        def cancel_job(self):
            if self.core is not None:
                self.core.cancel.set()
                self.append_log("已请求取消，等待当前块结束…")

        # -- OTA 列表 --------------------------------------------------- #

        def start_fetch_ota(self):
            core = self.make_core()
            self.run_job(lambda: fetch_ota_devices(core), self.show_ota_dialog)

        def show_ota_dialog(self, devices):
            if not devices:
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("OTA 下载链接列表")
            dialog.resize(900, 520)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("1. 选择机型:"))
            combo = QComboBox()
            combo.addItems(["%s  (%s)" % (item["name"], item["codename"]) for item in devices])
            layout.addWidget(combo)
            layout.addWidget(QLabel("2. 选择版本:"))
            tree = QTreeWidget()
            tree.setHeaderLabels(["版本", "下载链接"])
            tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
            tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
            layout.addWidget(tree, stretch=1)

            def refresh():
                tree.clear()
                index = combo.currentIndex()
                if 0 <= index < len(devices):
                    for version, link in devices[index]["versions"]:
                        QTreeWidgetItem(tree, [version, link])

            combo.currentIndexChanged.connect(refresh)
            refresh()

            def accept_selection():
                items = tree.selectedItems()
                if not items:
                    self.append_log("请先选择一个版本")
                    return
                self.url_edit.setText(items[0].text(1))
                self.append_log("已选择链接 (已转换为 /android/ 格式)")
                dialog.accept()

            tree.itemDoubleClicked.connect(lambda *_: accept_selection())
            buttons = QHBoxLayout()
            buttons.addStretch()
            accept = QPushButton("选择该版本")
            accept.clicked.connect(accept_selection)
            close = QPushButton("关闭")
            close.clicked.connect(dialog.reject)
            buttons.addWidget(accept)
            buttons.addWidget(close)
            layout.addLayout(buttons)
            dialog.exec_()

        # -- 目录树 ----------------------------------------------------- #

        def current_source(self):
            if self.radio_moto.isChecked():
                return "lenomola"
            if self.radio_lenovo.isChecked():
                return "lenowow"
            return "direct"

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

        def fetch_children(self, item, url):
            if not url or item.childCount() > 0 or item.data(0, Qt.UserRole + 1):
                return
            if not url.endswith("/"):
                return
            item.setData(0, Qt.UserRole + 1, True)
            worker = DirFetch(url, self.proxy_dict(), self)
            worker.done.connect(self._fill_children)
            worker.failed.connect(self._dir_failed)
            worker.finished.connect(worker.deleteLater)
            self._dir_threads = [item for item in self._dir_threads if not item.isFinished()]
            self._dir_threads.append(worker)
            worker.start()

        def _dir_failed(self, message):
            self.append_log("加载目录失败: %s" % message)

        def _fill_children(self, url, dirs, files):
            target = None
            for item in self._walk_items():
                if item.data(0, Qt.UserRole) == url:
                    target = item
                    break
            if target is None:
                return
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

        def on_tree_expanded(self, item):
            self.fetch_children(item, item.data(0, Qt.UserRole))

        def on_tree_clicked(self, item, _column):
            href = item.data(0, Qt.UserRole)
            if not href:
                return
            if href.lower().split("?")[0].endswith(".zip"):
                self.url_edit.setText(href)
                self.append_log("固件 URL 已填充: %s" % href)
            else:
                item.setExpanded(True)

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

        def start_list(self):
            url = self.url_edit.text().strip()
            if not url:
                self.append_log("请先输入固件链接")
                return
            core = self.make_core()
            self.run_job(lambda: core.list_remote(url), self.show_list)

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

        def extract_done(self, path):
            if not path:
                self.append_log("提取已取消")
                self.speed_label.setText("已取消")
                return
            self.speed_label.setText("完成：%s" % path)
            QMessageBox.information(self, "完成", "提取完成！\n保存至: %s" % path)
            self.open_path(path)

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
            for worker in list(self._dir_threads):
                try:
                    if worker.isRunning():
                        worker.quit()
                        worker.wait(1000)
                except RuntimeError:
                    pass
            if self.thread is not None:
                try:
                    self.thread.quit()
                    self.thread.wait(3000)
                except RuntimeError:
                    pass
            event.accept()

    application = QApplication(sys.argv)
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
                application.quit()

        QTimer.singleShot(2500, grab_and_quit)

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
