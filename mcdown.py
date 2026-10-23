#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 HWQ277

"""Minecraft 原版核心下载器（BMCLAPI）。

只使用 Python 标准库，同时提供图形界面和命令行模式。
下载内容为客户端 JAR 与版本 JSON，并按版本号命名、归档。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import queue
import re
import shutil
import socket
import zipfile
from xml.etree import ElementTree as ET
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

APP_NAME = "MC 版本下载器"
APP_VERSION = "1.3.0"
API_BASE = "https://bmclapi2.bangbang93.com"
MANIFEST_URL = API_BASE + "/mc/game/version_manifest_v2.json"
USER_AGENT = "MCDown/%s (Python urllib; BMCLAPI downloader)" % APP_VERSION
HTTP_TIMEOUT = 45
MAX_ATTEMPTS = 3
CHUNK_SIZE = 256 * 1024
MANIFEST_MAX_BYTES = 16 * 1024 * 1024
JSON_MAX_BYTES = 8 * 1024 * 1024
LOADER_META_MAX_BYTES = 32 * 1024 * 1024
INSTALLER_MAX_BYTES = 64 * 1024 * 1024
LOADER_INSTALL_TIMEOUT = 30 * 60

LOADER_LABELS = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
}
LOADER_ORDER = ("fabric", "forge", "neoforge", "quilt")
DIRECT_PROFILE_LOADERS = ("fabric", "quilt")

FABRIC_META_BASE = API_BASE + "/fabric-meta/v2"
FABRIC_META_OFFICIAL = "https://meta.fabricmc.net/v2"
QUILT_META_BASE = API_BASE + "/quilt-meta/v3"
QUILT_META_OFFICIAL = "https://meta.quiltmc.org/v3"
NEOFORGE_MAVEN_OFFICIAL = "https://maven.neoforged.net/releases"
HMCL_INSTANCE_SCHEMA_URL = "https://schemas.glavo.site/hmcl/instance-game-settings/1.0.0"
PCL_INSTANCE_SETTINGS_FILE = "PCL/Setup.ini"
HMCL_INSTANCE_SETTINGS_FILE = ".hmcl/config/instance-game-settings.json"

VERSION_TYPE_NAMES = {
    "release": "正式版",
    "snapshot": "快照",
    "old_beta": "远古 Beta",
    "old_alpha": "远古 Alpha",
}

ProgressCallback = Callable[["ProgressUpdate"], None]


class MCDownError(Exception):
    """可以直接展示给用户的错误。"""


class DownloadCancelled(MCDownError):
    """用户取消了下载。"""


class _RetryableNetworkError(Exception):
    """可以安全重试的临时网络错误。"""


class ResourceNotFound(MCDownError):
    """服务器明确返回 404。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressUpdate:
    stage: str
    message: str
    current: int = 0
    total: int = 0


@dataclass(frozen=True)
class VersionInfo:
    id: str
    type: str
    release_time: str = ""
    sha1: str = ""
    url: str = ""


@dataclass(frozen=True)
class VersionManifest:
    latest_release: str
    latest_snapshot: str
    versions: Tuple[VersionInfo, ...]

    def find(self, version: str) -> Optional[VersionInfo]:
        for item in self.versions:
            if item.id == version:
                return item
        return None


@dataclass(frozen=True)
class DownloadResult:
    version: str
    folder: Path
    jar_path: Path
    json_path: Path
    jar_skipped: bool
    json_skipped: bool
    jar_size: int
    json_size: int
    downloaded_bytes: int
    elapsed: float
    game_root: Path


@dataclass(frozen=True)
class LoaderVersion:
    version: str
    stable: bool
    installer_url: str = ""
    installer_sha1: str = ""
    installer_size: int = 0
    artifact_version: str = ""
    fallback_installer_url: str = ""


@dataclass(frozen=True)
class LoaderCatalog:
    loader: str
    mc_version: str
    versions: Tuple[LoaderVersion, ...]
    recommended: Optional[str]


@dataclass(frozen=True)
class JavaInfo:
    path: str
    version: str
    raw: str = ""


@dataclass(frozen=True)
class LoaderInstallResult:
    loader: str
    loader_version: str
    mc_version: str
    game_dir: Path
    profile_paths: Tuple[Path, ...]
    installer_path: Optional[Path]
    installer_log: Optional[Path]
    java_path: Optional[str]
    downloaded_bytes: int
    elapsed: float
    method: str
    instance_dirs: Tuple[Path, ...] = ()
    isolation_files: Tuple[Path, ...] = ()


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def default_download_root() -> Path:
    """默认的 .minecraft 文件夹位置。"""
    return Path.home() / "Downloads" / "Minecraft"


def format_bytes(value: int) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024.0 or unit == "GB":
            if unit == "B":
                return "%d B" % int(number)
            return "%.1f %s" % (number, unit)
        number /= 1024.0
    return "%.1f GB" % number


def version_type_name(value: str) -> str:
    return VERSION_TYPE_NAMES.get(value, value or "未知")


def validate_version_name(value: Any) -> str:
    """校验版本号，确保它不会被拼成目录穿越路径。

    Minecraft 有少量带空格的版本号（例如 ``3D Shareware v1.34``），
    因此只拒绝两个平台真正不能安全使用的字符，而不是限制为纯数字。
    """
    version = str(value or "").strip()
    if not version:
        raise MCDownError("请先选择或输入 Minecraft 版本号。")
    if len(version) > 120:
        raise MCDownError("版本号过长，无法作为文件名使用。")
    if version in (".", ".."):
        raise MCDownError("版本号不能是 . 或 ..。")
    if not version.isprintable():
        raise MCDownError("版本号中包含不可打印字符。")
    forbidden = set('<>:"/\\|?*')
    if any(char in forbidden for char in version):
        raise MCDownError('版本号不能包含下列字符：< > : " / \\ | ? *')
    if version.endswith(" ") or version.endswith("."):
        raise MCDownError("版本号不能以空格或英文句点结尾。")
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update("COM%d" % i for i in range(1, 10))
    reserved.update("LPT%d" % i for i in range(1, 10))
    if version.split(".")[0].upper() in reserved:
        raise MCDownError("版本号不能使用 Windows 保留名称。")
    return version


def _safe_remove(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("已取消下载。")


def _wait_with_cancel(cancel_event: Optional[threading.Event], seconds: float) -> None:
    if cancel_event is None:
        time.sleep(seconds)
    elif cancel_event.wait(seconds):
        raise DownloadCancelled("已取消下载。")


def _emit(
    callback: Optional[ProgressCallback],
    stage: str,
    message: str,
    current: int = 0,
    total: int = 0,
) -> None:
    if callback is None:
        return
    try:
        callback(ProgressUpdate(stage, message, int(current), int(total)))
    except Exception:
        # 界面回调不能影响真正的下载流程。
        pass


# ---------------------------------------------------------------------------
# 网络层
# ---------------------------------------------------------------------------


def _open_url(url: str, timeout: Optional[float] = None) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        },
    )
    return urllib.request.urlopen(request, timeout=HTTP_TIMEOUT if timeout is None else timeout)


def _content_length(response: Any) -> Optional[int]:
    try:
        raw = response.headers.get("Content-Length")
        if raw is None:
            return None
        value = int(raw)
        return value if value >= 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 425, 429, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return False
        return True
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(
        exc,
        (
            ssl.SSLError,
            socket.timeout,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
            _RetryableNetworkError,
        ),
    ):
        return True
    return False


def _retry_delay(exc: BaseException, attempt: int) -> float:
    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
        try:
            retry_after = exc.headers.get("Retry-After")
            if retry_after:
                return max(1.0, min(30.0, float(retry_after)))
        except (TypeError, ValueError, AttributeError):
            pass
        return 5.0
    return min(8.0, float(2 ** (attempt - 1)))


def _describe_network_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "资源不存在（HTTP 404），请检查版本号是否正确"
        if exc.code == 403:
            return "请求被服务器拒绝（HTTP 403）"
        if exc.code == 429:
            return "请求过于频繁（HTTP 429），请稍后再试"
        if 500 <= exc.code < 600:
            return "BMCLAPI 服务暂时不可用（HTTP %s）" % exc.code
        return "服务器返回 HTTP %s %s" % (exc.code, exc.reason)
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return "TLS 证书校验失败，请检查系统时间或网络代理"
        if isinstance(reason, ssl.SSLError):
            return "TLS 连接失败（%s）" % reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "连接超时"
        return "网络连接失败（%s）" % reason
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "TLS 证书校验失败，请检查系统时间或网络代理"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "连接超时"
    if isinstance(exc, http.client.IncompleteRead):
        return "数据接收不完整"
    if isinstance(exc, _RetryableNetworkError):
        return str(exc)
    if isinstance(exc, ConnectionError):
        return "网络连接中断（%s）" % exc
    if isinstance(exc, OSError):
        return "输入/输出错误（%s）" % exc
    return str(exc)


def _parse_json_bytes(raw: bytes, description: str) -> Any:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MCDownError("%s不是有效的 UTF-8 文本：%s" % (description, exc)) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCDownError(
            "%s不是有效的 JSON（第 %d 行，第 %d 列）。" % (description, exc.lineno, exc.colno)
        ) from exc


def _fetch_bytes(
    url: str,
    description: str,
    stage: str,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
    max_bytes: int,
) -> bytes:
    last_error: Optional[BaseException] = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        _check_cancel(cancel_event)
        try:
            with _open_url(url) as response:
                length = _content_length(response)
                if length is not None and length > max_bytes:
                    raise MCDownError("%s体积异常，已停止下载。" % description)

                data = bytearray()
                last_emit = 0.0
                _emit(progress, stage, "正在下载%s" % description, 0, length or 0)

                while True:
                    _check_cancel(cancel_event)
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise MCDownError("%s体积异常，已停止下载。" % description)
                    now = time.monotonic()
                    if now - last_emit >= 0.1:
                        _emit(progress, stage, "正在下载%s" % description, len(data), length or 0)
                        last_emit = now

                if length is not None and len(data) != length:
                    raise _RetryableNetworkError("接收到的数据长度与服务器声明不一致")
                if not data:
                    raise _RetryableNetworkError("服务器返回了空内容")

                _emit(progress, stage, "%s下载完成" % description, len(data), length or len(data))
                return bytes(data)

        except DownloadCancelled:
            raise
        except MCDownError:
            raise
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            ssl.SSLError,
            socket.timeout,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
            _RetryableNetworkError,
        ) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS and _is_retryable(exc):
                delay = _retry_delay(exc, attempt)
                _emit(
                    progress,
                    stage,
                    "%s连接中断，%d 秒后重试（%d/%d）"
                    % (description, int(round(delay)), attempt, MAX_ATTEMPTS - 1),
                    0,
                    0,
                )
                _wait_with_cancel(cancel_event, delay)
                continue
            break

    detail = _describe_network_error(last_error) if last_error is not None else "未知网络错误"
    message = "%s 失败：%s。" % (description, detail)
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 404:
        raise ResourceNotFound(message)
    raise MCDownError(message)


# ---------------------------------------------------------------------------
# BMCLAPI 数据
# ---------------------------------------------------------------------------


def fetch_manifest(
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> VersionManifest:
    """从 BMCLAPI 获取版本清单。"""
    _emit(progress, "manifest", "正在获取版本列表", 0, 0)
    raw = _fetch_bytes(
        MANIFEST_URL,
        "版本列表",
        "manifest",
        progress,
        cancel_event,
        MANIFEST_MAX_BYTES,
    )
    data = _parse_json_bytes(raw, "版本列表")
    if not isinstance(data, dict):
        raise MCDownError("版本列表格式异常：根节点不是 JSON 对象。")

    latest_raw = data.get("latest")
    versions_raw = data.get("versions")
    if not isinstance(latest_raw, dict) or not isinstance(versions_raw, list):
        raise MCDownError("版本列表格式异常：缺少 latest 或 versions。")

    versions: List[VersionInfo] = []
    for item in versions_raw:
        if not isinstance(item, dict):
            continue
        version_id = str(item.get("id") or "").strip()
        if not version_id:
            continue
        versions.append(
            VersionInfo(
                id=version_id,
                type=str(item.get("type") or ""),
                release_time=str(item.get("releaseTime") or ""),
                sha1=str(item.get("sha1") or "").lower(),
                url=str(item.get("url") or ""),
            )
        )
    if not versions:
        raise MCDownError("版本列表为空，无法选择版本。")

    latest_release = str(latest_raw.get("release") or "").strip()
    latest_snapshot = str(latest_raw.get("snapshot") or "").strip()
    if not latest_release:
        for item in versions:
            if item.type == "release":
                latest_release = item.id
                break
    if not latest_snapshot:
        for item in versions:
            if item.type == "snapshot":
                latest_snapshot = item.id
                break

    _emit(
        progress,
        "manifest",
        "版本列表加载完成，共 %d 个版本" % len(versions),
        len(versions),
        len(versions),
    )
    return VersionManifest(latest_release, latest_snapshot, tuple(versions))


def _version_endpoint(version: str, kind: str) -> str:
    quoted = urllib.parse.quote(version, safe="")
    return "%s/version/%s/%s" % (API_BASE, quoted, kind)


def _sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_output_root(output_root: Any) -> Path:
    """把用户选择的文件夹规范化为 .minecraft 根目录。"""
    raw = str(output_root or "").strip()
    if not raw:
        raise MCDownError("请填写 .minecraft 文件夹。")
    try:
        raw = os.path.expandvars(raw)
        root = Path(raw).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        root = Path(os.path.abspath(str(root)))
        if root.exists() and not root.is_dir():
            raise MCDownError("所选 .minecraft 路径被同名文件占用，请换一个位置：%s" % root)
        root.mkdir(parents=True, exist_ok=True)
    except MCDownError:
        raise
    except OSError as exc:
        raise MCDownError("无法创建 .minecraft 文件夹 %s：%s" % (raw, exc.strerror or exc)) from exc
    return root


def _ensure_minecraft_skeleton(root: Path) -> Path:
    """创建标准 .minecraft 根目录骨架，游戏实例只放在 versions/ 下。"""
    try:
        for relative in (
            "assets",
            "assets/indexes",
            "assets/objects",
            "assets/log_configs",
            "libraries",
            "versions",
        ):
            (root / relative).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MCDownError("无法准备 .minecraft 目录 %s：%s" % (root, exc.strerror or exc)) from exc
    return root


def _prepare_target_dir(versions_root: Path, version: str) -> Path:
    target_dir = versions_root / version
    try:
        if target_dir.exists() and not target_dir.is_dir():
            raise MCDownError("版本目录被同名文件占用：%s" % target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
    except MCDownError:
        raise
    except OSError as exc:
        raise MCDownError("无法创建版本目录 %s：%s" % (target_dir, exc.strerror or exc)) from exc
    return target_dir


def _write_atomic(path: Path, data: bytes) -> None:
    temp_path = path.with_name(path.name + ".part")
    try:
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
        os.replace(temp_path, path)
    except OSError as exc:
        _safe_remove(temp_path)
        raise MCDownError("无法写入文件 %s：%s" % (path, exc.strerror or exc)) from exc


def _parse_version_json(raw: bytes, version: str) -> Dict[str, Any]:
    data = _parse_json_bytes(raw, "版本 JSON")
    if not isinstance(data, dict):
        raise MCDownError("版本 JSON 格式异常：根节点不是 JSON 对象。")
    actual_id = str(data.get("id") or "").strip()
    if actual_id != version:
        raise MCDownError(
            "BMCLAPI 返回的版本与请求不一致（请求 %s，返回 %s）。"
            % (version, actual_id or "空")
        )
    downloads = data.get("downloads")
    client = downloads.get("client") if isinstance(downloads, dict) else None
    if not isinstance(client, dict):
        raise MCDownError("该版本没有可下载的客户端 JAR（downloads.client 缺失）。")
    return data


def _ensure_version_json(
    version: str,
    target_dir: Path,
    expected_sha1: str,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> Tuple[bytes, Dict[str, Any], bool]:
    """优先复用本地有效 JSON，否则从 BMCLAPI 下载并校验。"""
    json_path = target_dir / (version + ".json")
    if json_path.exists() and json_path.is_dir():
        raise MCDownError("JSON 文件路径被同名目录占用：%s" % json_path)

    if json_path.is_file():
        _emit(progress, "json", "正在检查已有的版本 JSON", 0, 0)
        try:
            existing = json_path.read_bytes()
            if len(existing) > JSON_MAX_BYTES:
                raise MCDownError("本地 JSON 文件体积异常。")
            existing_data = _parse_version_json(existing, version)
            if expected_sha1 and _sha1_bytes(existing) != expected_sha1.lower():
                raise MCDownError("本地 JSON 与服务器版本不一致。")
        except (OSError, MCDownError):
            _emit(progress, "json", "本地版本 JSON 不可用，正在重新下载", 0, 0)
        else:
            _emit(
                progress,
                "json",
                "版本 JSON 已存在且校验通过，跳过下载",
                len(existing),
                len(existing),
            )
            return existing, existing_data, True

    url = _version_endpoint(version, "json")
    raw = _fetch_bytes(
        url,
        "版本 JSON",
        "json",
        progress,
        cancel_event,
        JSON_MAX_BYTES,
    )
    _check_cancel(cancel_event)
    if expected_sha1 and _sha1_bytes(raw) != expected_sha1.lower():
        raise MCDownError("版本 JSON 的 SHA-1 校验失败，下载内容可能已损坏，请重试。")
    _emit(progress, "json", "正在解析版本 JSON", len(raw), len(raw))
    data = _parse_version_json(raw, version)
    _write_atomic(json_path, raw)
    _emit(progress, "json", "版本 JSON 已保存", len(raw), len(raw))
    return raw, data, False


def _client_descriptor(data: Dict[str, Any]) -> Tuple[str, int]:
    downloads = data.get("downloads")
    client = downloads.get("client") if isinstance(downloads, dict) else None
    if not isinstance(client, dict):
        raise MCDownError("版本 JSON 中缺少客户端 JAR 信息。")
    expected_sha1 = str(client.get("sha1") or "").strip().lower()
    if not expected_sha1:
        raise MCDownError("版本 JSON 中缺少客户端 JAR 的 SHA-1，无法安全校验。")
    try:
        expected_size = int(client.get("size") or 0)
    except (TypeError, ValueError):
        expected_size = 0
    if expected_size < 0:
        expected_size = 0
    return expected_sha1, expected_size


def _download_file(
    url: str,
    destination: Path,
    display_name: str,
    expected_sha1: str,
    expected_size: int,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
    stage: str = "jar",
    verify_stage: str = "verify",
    attempts: int = MAX_ATTEMPTS,
    timeout: Optional[float] = None,
    deadline_seconds: Optional[float] = None,
) -> Tuple[int, str]:
    temp_path = destination.with_name(destination.name + ".part")
    last_error: Optional[BaseException] = None
    attempts = max(1, int(attempts))

    for attempt in range(1, attempts + 1):
        attempt_started = time.monotonic()
        if destination.parent and not destination.parent.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
        _check_cancel(cancel_event)
        hasher = hashlib.sha1()
        bytes_written = 0
        header_size: Optional[int] = None
        try:
            with _open_url(url, timeout=timeout) as response:
                header_size = _content_length(response)
                total = expected_size or header_size or 0
                _emit(progress, stage, "正在下载 %s" % display_name, 0, total)
                last_emit = 0.0
                with open(temp_path, "wb") as handle:
                    while True:
                        _check_cancel(cancel_event)
                        if deadline_seconds and time.monotonic() - attempt_started > deadline_seconds:
                            raise _RetryableNetworkError("下载超过时间限制")
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
                        hasher.update(chunk)
                        bytes_written += len(chunk)
                        now = time.monotonic()
                        if now - last_emit >= 0.1:
                            _emit(
                                progress,
                                stage,
                                "正在下载 %s" % display_name,
                                bytes_written,
                                total,
                            )
                            last_emit = now
                    handle.flush()

            if bytes_written <= 0:
                raise _RetryableNetworkError("服务器返回了空文件")
            if header_size is not None and bytes_written != header_size:
                raise _RetryableNetworkError("接收到的数据长度与服务器声明不一致")
            if expected_size and bytes_written != expected_size:
                raise _RetryableNetworkError(
                    "文件大小不符（期望 %d 字节，实际 %d 字节）"
                    % (expected_size, bytes_written)
                )

            if expected_sha1:
                _emit(
                    progress,
                    verify_stage,
                    "正在校验 %s 的 SHA-1" % display_name,
                    bytes_written,
                    expected_size or bytes_written,
                )
            _check_cancel(cancel_event)
            digest = hasher.hexdigest()
            if expected_sha1 and digest.lower() != expected_sha1.lower():
                raise _RetryableNetworkError("SHA-1 校验失败，文件可能已损坏")
            os.replace(temp_path, destination)
            _emit(
                progress,
                stage,
                "%s 下载完成" % display_name,
                bytes_written,
                expected_size or bytes_written,
            )
            return bytes_written, digest

        except DownloadCancelled:
            _safe_remove(temp_path)
            raise
        except MCDownError:
            _safe_remove(temp_path)
            raise
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            ssl.SSLError,
            socket.timeout,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
            _RetryableNetworkError,
        ) as exc:
            _safe_remove(temp_path)
            last_error = exc
            if attempt < attempts and _is_retryable(exc):
                delay = _retry_delay(exc, attempt)
                _emit(
                    progress,
                    stage,
                    "下载 %s 中断，%d 秒后重试（%d/%d）"
                    % (display_name, int(round(delay)), attempt, attempts - 1),
                    0,
                    expected_size,
                )
                _wait_with_cancel(cancel_event, delay)
                continue
            break
        except OSError as exc:
            # 写磁盘时的权限/空间问题会到这里；这类错误重试也没有意义。
            _safe_remove(temp_path)
            raise MCDownError("保存 %s 失败：%s" % (display_name, exc.strerror or exc)) from exc

    detail = _describe_network_error(last_error) if last_error is not None else "未知网络错误"
    message = "下载 %s 失败：%s。" % (display_name, detail)
    if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 404:
        raise ResourceNotFound(message)
    raise MCDownError(message)


def download_game(
    version: str,
    output_root: Any = None,
    version_info: Optional[VersionInfo] = None,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> DownloadResult:
    """下载一个版本的 JAR 与 JSON。

    ``output_root`` 是用户选择的 ``.minecraft`` 根目录，不是父目录。
    最终结构：
        <.minecraft>/versions/<version>/<version>.jar
        <.minecraft>/versions/<version>/<version>.json
    """
    started_at = time.monotonic()
    version = validate_version_name(version)
    if output_root is None:
        output_root = default_download_root()
    if version_info is not None and version_info.id != version:
        version_info = None

    _check_cancel(cancel_event)
    root = _prepare_output_root(output_root)
    _ensure_minecraft_skeleton(root)
    target_dir = _prepare_target_dir(root / "versions", version)
    json_path = target_dir / (version + ".json")
    jar_path = target_dir / (version + ".jar")
    if jar_path.exists() and jar_path.is_dir():
        raise MCDownError("JAR 文件路径被同名目录占用：%s" % jar_path)

    expected_json_sha1 = version_info.sha1 if version_info is not None else ""
    _emit(progress, "prepare", "正在准备下载 Minecraft %s" % version, 0, 0)

    json_raw, version_data, json_skipped = _ensure_version_json(
        version,
        target_dir,
        expected_json_sha1,
        progress,
        cancel_event,
    )
    expected_jar_sha1, expected_jar_size = _client_descriptor(version_data)

    jar_skipped = False
    if jar_path.is_file():
        _emit(progress, "jar", "正在校验已有的 %s.jar" % version, 0, expected_jar_size)
        try:
            existing_digest = _sha1_file(jar_path)
        except OSError as exc:
            raise MCDownError(
                "无法读取已有 JAR 文件 %s：%s" % (jar_path, exc.strerror or exc)
            ) from exc
        if existing_digest.lower() == expected_jar_sha1:
            jar_skipped = True
            jar_size = jar_path.stat().st_size
            _emit(
                progress,
                "jar",
                "%s.jar 已存在且校验通过，跳过下载" % version,
                jar_size,
                jar_size,
            )
        else:
            _emit(progress, "jar", "已有 JAR 校验不一致，将重新下载", 0, expected_jar_size)

    if not jar_skipped:
        jar_url = _version_endpoint(version, "client")
        jar_size, _ = _download_file(
            jar_url,
            jar_path,
            version + ".jar",
            expected_jar_sha1,
            expected_jar_size,
            progress,
            cancel_event,
        )
    else:
        jar_size = jar_path.stat().st_size

    try:
        json_size = json_path.stat().st_size
    except OSError:
        json_size = len(json_raw)
    _check_cancel(cancel_event)

    downloaded_bytes = 0
    if not json_skipped:
        downloaded_bytes += json_size
    if not jar_skipped:
        downloaded_bytes += jar_size
    elapsed = max(0.0, time.monotonic() - started_at)
    _emit(progress, "done", "下载完成，文件已按版本号归档", jar_size + json_size, jar_size + json_size)

    return DownloadResult(
        version=version,
        folder=target_dir,
        jar_path=jar_path,
        json_path=json_path,
        jar_skipped=jar_skipped,
        json_skipped=json_skipped,
        jar_size=jar_size,
        json_size=json_size,
        downloaded_bytes=downloaded_bytes,
        elapsed=elapsed,
        game_root=root,
    )



# ---------------------------------------------------------------------------
# 模组加载器：元数据、Java 与安装
# ---------------------------------------------------------------------------


def loader_label(loader: str) -> str:
    return LOADER_LABELS.get(loader, loader)


def _validate_loader(loader: str) -> str:
    value = str(loader or "").strip().lower()
    if value not in LOADER_LABELS:
        raise MCDownError("不支持的模组加载器：%s" % loader)
    return value


def _is_prerelease(version: str) -> bool:
    lowered = version.lower()
    return any(
        token in lowered
        for token in ("alpha", "beta", "snapshot", "-pre", "-rc", ".pre", ".rc")
    )


def _numeric_version_key(version: str) -> Tuple[int, ...]:
    core = str(version).split("+", 1)[0]
    if "-" in core:
        core = core.split("-", 1)[0]
    numbers = [int(part) for part in re.findall(r"\d+", core)]
    if not numbers:
        numbers = [0]
    while len(numbers) < 5:
        numbers.append(0)
    return tuple(numbers[:5])


def _loader_version_sort(entry: LoaderVersion) -> Tuple[Tuple[int, ...], bool]:
    return (_numeric_version_key(entry.version), entry.stable)


def _sorted_loader_entries(entries: List[LoaderVersion]) -> Tuple[LoaderVersion, ...]:
    seen = set()
    unique: List[LoaderVersion] = []
    for entry in entries:
        if entry.version in seen:
            continue
        seen.add(entry.version)
        unique.append(entry)
    unique.sort(key=_loader_version_sort, reverse=True)
    return tuple(unique)


def _recommend_loader_version(entries: Tuple[LoaderVersion, ...]) -> Optional[str]:
    if not entries:
        return None
    stable_entries = [entry for entry in entries if entry.stable]
    pool = stable_entries if stable_entries else list(entries)
    best = max(pool, key=_loader_version_sort)
    return best.version


def choose_loader_version(
    catalog: LoaderCatalog, requested: Optional[str] = None
) -> Optional[LoaderVersion]:
    if not catalog.versions:
        return None
    if requested:
        wanted = str(requested).strip()
        for entry in catalog.versions:
            if entry.version == wanted:
                return entry
        raise MCDownError(
            "%s 没有加载器版本 %s。可从可用版本中选择，或留空使用推荐版。"
            % (loader_label(catalog.loader), wanted)
        )
    if catalog.recommended:
        for entry in catalog.versions:
            if entry.version == catalog.recommended:
                return entry
    stable_entries = [entry for entry in catalog.versions if entry.stable]
    pool = stable_entries if stable_entries else list(catalog.versions)
    return max(pool, key=_loader_version_sort)


def _fetch_json_sources(
    urls: Tuple[str, ...],
    description: str,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
    stage: str = "loader_meta",
    max_bytes: int = LOADER_META_MAX_BYTES,
) -> Any:
    """按顺序尝试多个地址，返回第一个成功解析的 JSON。"""
    last_error: Optional[BaseException] = None
    for url in urls:
        _check_cancel(cancel_event)
        try:
            raw = _fetch_bytes(
                url,
                description,
                stage,
                progress,
                cancel_event,
                max_bytes,
            )
            return _parse_json_bytes(raw, description)
        except ResourceNotFound as exc:
            last_error = exc
            continue
        except MCDownError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise MCDownError("无法获取%s。" % description)


def _fetch_forge_catalog(
    mc_version: str,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> LoaderCatalog:
    url = API_BASE + "/forge/minecraft/" + urllib.parse.quote(mc_version, safe="")
    try:
        raw = _fetch_bytes(
            url,
            "Forge 版本列表",
            "loader_meta",
            progress,
            cancel_event,
            LOADER_META_MAX_BYTES,
        )
    except ResourceNotFound:
        return LoaderCatalog("forge", mc_version, (), None)
    data = _parse_json_bytes(raw, "Forge 版本列表")
    if not isinstance(data, list):
        raise MCDownError("Forge 版本列表格式异常。")

    entries: List[LoaderVersion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        version = str(item.get("version") or "").strip()
        if not version:
            continue
        files = item.get("files")
        installer_hash = ""
        if isinstance(files, list):
            for file_info in files:
                if not isinstance(file_info, dict):
                    continue
                if file_info.get("category") == "installer" and file_info.get("format") == "jar":
                    installer_hash = str(file_info.get("hash") or "").lower()
                    break
        artifact_version = "%s-%s" % (mc_version, version)
        encoded = urllib.parse.quote(artifact_version, safe="")
        installer_url = "%s/maven/net/minecraftforge/forge/%s/forge-%s-installer.jar" % (
            API_BASE,
            encoded,
            encoded,
        )
        fallback_url = (
            "https://maven.minecraftforge.net/net/minecraftforge/forge/"
            "%s/forge-%s-installer.jar" % (encoded, encoded)
        )
        entries.append(
            LoaderVersion(
                version=version,
                stable=True,
                installer_url=installer_url,
                installer_sha1=installer_hash,
                artifact_version=artifact_version,
                fallback_installer_url=fallback_url,
            )
        )

    versions = _sorted_loader_entries(entries)
    _emit(
        progress,
        "loader_meta",
        "Forge 版本列表已就绪，共 %d 个版本" % len(versions),
        len(versions),
        len(versions),
    )
    return LoaderCatalog("forge", mc_version, versions, _recommend_loader_version(versions))


def _fetch_fabric_catalog(
    mc_version: str,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> LoaderCatalog:
    path = "/versions/loader/" + urllib.parse.quote(mc_version, safe="")
    data = _fetch_json_sources(
        (FABRIC_META_BASE + path, FABRIC_META_OFFICIAL + path),
        "Fabric 版本列表",
        progress,
        cancel_event,
    )
    if not isinstance(data, list):
        raise MCDownError("Fabric 版本列表格式异常。")

    entries: List[LoaderVersion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        loader_info = item.get("loader")
        if not isinstance(loader_info, dict):
            continue
        version = str(loader_info.get("version") or "").strip()
        if not version:
            continue
        entries.append(
            LoaderVersion(
                version=version,
                stable=bool(loader_info.get("stable")),
            )
        )
    versions = _sorted_loader_entries(entries)
    _emit(
        progress,
        "loader_meta",
        "Fabric 版本列表已就绪，共 %d 个版本" % len(versions),
        len(versions),
        len(versions),
    )
    return LoaderCatalog("fabric", mc_version, versions, _recommend_loader_version(versions))


def _fetch_quilt_catalog(
    mc_version: str,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> LoaderCatalog:
    path = "/versions/loader/" + urllib.parse.quote(mc_version, safe="")
    data = _fetch_json_sources(
        (QUILT_META_BASE + path, QUILT_META_OFFICIAL + path),
        "Quilt 版本列表",
        progress,
        cancel_event,
    )
    if not isinstance(data, list):
        raise MCDownError("Quilt 版本列表格式异常。")

    entries: List[LoaderVersion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        loader_info = item.get("loader")
        if not isinstance(loader_info, dict):
            continue
        version = str(loader_info.get("version") or "").strip()
        if not version:
            continue
        entries.append(
            LoaderVersion(
                version=version,
                stable=not _is_prerelease(version),
            )
        )
    versions = _sorted_loader_entries(entries)
    _emit(
        progress,
        "loader_meta",
        "Quilt 版本列表已就绪，共 %d 个版本" % len(versions),
        len(versions),
        len(versions),
    )
    return LoaderCatalog("quilt", mc_version, versions, _recommend_loader_version(versions))


def _neoforge_game_prefix(mc_version: str) -> Tuple[str, Optional[str]]:
    """返回 (Maven artifactId, 版本前缀)；版本前缀为 None 表示无法匹配。"""
    if mc_version == "1.20.1":
        return "forge", "1.20.1-"
    if mc_version.startswith("1."):
        remainder = mc_version[2:]
        if re.fullmatch(r"\d+(?:\.\d+)+", remainder):
            return "neoforge", remainder + "."
        return "neoforge", None
    if re.fullmatch(r"\d+(?:\.\d+)+", mc_version):
        return "neoforge", mc_version + "."
    return "neoforge", None


def _fetch_neoforge_catalog(
    mc_version: str,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> LoaderCatalog:
    artifact, prefix = _neoforge_game_prefix(mc_version)
    if not prefix:
        return LoaderCatalog("neoforge", mc_version, (), None)

    metadata_url = "%s/maven/net/neoforged/%s/maven-metadata.xml" % (API_BASE, artifact)
    try:
        raw = _fetch_bytes(
            metadata_url,
            "NeoForge 版本列表",
            "loader_meta",
            progress,
            cancel_event,
            LOADER_META_MAX_BYTES,
        )
    except ResourceNotFound:
        return LoaderCatalog("neoforge", mc_version, (), None)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise MCDownError("NeoForge 版本列表格式异常：%s" % exc) from exc

    entries: List[LoaderVersion] = []
    for node in root.findall(".//version"):
        full_version = str(node.text or "").strip()
        if not full_version or not full_version.startswith(prefix):
            continue
        if artifact == "forge":
            version = full_version[len(mc_version) + 1 :]
            if not version:
                continue
        else:
            version = full_version
        encoded = urllib.parse.quote(full_version, safe="")
        installer_url = "%s/maven/net/neoforged/%s/%s/%s-%s-installer.jar" % (
            API_BASE,
            artifact,
            encoded,
            artifact,
            encoded,
        )
        entries.append(
            LoaderVersion(
                version=version,
                stable=not _is_prerelease(version),
                installer_url=installer_url,
                artifact_version=full_version,
            )
        )

    versions = _sorted_loader_entries(entries)
    _emit(
        progress,
        "loader_meta",
        "NeoForge 版本列表已就绪，共 %d 个版本" % len(versions),
        len(versions),
        len(versions),
    )
    return LoaderCatalog("neoforge", mc_version, versions, _recommend_loader_version(versions))


def fetch_loader_catalog(
    loader: str,
    mc_version: str,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> LoaderCatalog:
    loader = _validate_loader(loader)
    mc_version = validate_version_name(mc_version)
    _check_cancel(cancel_event)
    _emit(
        progress,
        "loader_meta",
        "正在获取 %s 加载器版本…" % loader_label(loader),
        0,
        0,
    )
    if loader == "forge":
        return _fetch_forge_catalog(mc_version, progress, cancel_event)
    if loader == "fabric":
        return _fetch_fabric_catalog(mc_version, progress, cancel_event)
    if loader == "quilt":
        return _fetch_quilt_catalog(mc_version, progress, cancel_event)
    return _fetch_neoforge_catalog(mc_version, progress, cancel_event)


# ---------------------------------------------------------------------------
# Java 检测
# ---------------------------------------------------------------------------


def _normalise_java_candidate(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    expanded = os.path.expandvars(raw)
    candidate = Path(expanded).expanduser()
    try:
        if candidate.is_dir():
            candidate = candidate / "bin" / ("java.exe" if os.name == "nt" else "java")
        if candidate.is_file():
            return str(candidate)
    except OSError:
        pass
    found = shutil.which(expanded)
    return found


def _java_candidates(preferred: Optional[str] = None) -> List[str]:
    values: List[str] = []
    if preferred:
        values.append(preferred)
    for env_name in ("MCDOWN_JAVA", "JAVA_HOME", "JRE_HOME"):
        env_value = os.environ.get(env_name)
        if env_value:
            values.append(env_value)
    found = shutil.which("java")
    if found:
        values.append(found)

    if os.name == "nt":
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base_raw = os.environ.get(env_name)
            if not base_raw:
                continue
            base = Path(base_raw)
            patterns = (
                "Java/*/bin/java.exe",
                "Eclipse Adoptium/*/bin/java.exe",
                "Microsoft/jdk-*/bin/java.exe",
                "Zulu/*/bin/java.exe",
                "BellSoft/*/bin/java.exe",
                "Programs/*/bin/java.exe",
            )
            for pattern in patterns:
                try:
                    values.extend(str(path) for path in base.glob(pattern))
                except OSError:
                    pass
    elif sys.platform == "darwin":
        base = Path("/Library/Java/JavaVirtualMachines")
        if base.is_dir():
            try:
                values.extend(
                    str(path / "Contents" / "Home" / "bin" / "java")
                    for path in base.glob("*")
                )
            except OSError:
                pass
    else:
        for base_path in ("/usr/lib/jvm", "/usr/java", "/opt/java"):
            base = Path(base_path)
            if not base.is_dir():
                continue
            try:
                values.extend(str(path / "bin" / "java") for path in base.glob("*"))
            except OSError:
                pass

    result: List[str] = []
    seen = set()
    for value in values:
        candidate = _normalise_java_candidate(value)
        if not candidate:
            continue
        key = candidate.casefold() if os.name == "nt" else candidate
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _probe_java(path: str) -> Optional[JavaInfo]:
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        completed = subprocess.run(
            [path, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            text=True,
            errors="replace",
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stderr or "") + (completed.stdout or "")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    raw = "\n".join(lines[:2])
    version = ""
    match = re.search(r'version\s+"([^"]+)"', output)
    if match:
        version = match.group(1)
    if not version:
        version = lines[0]
    return JavaInfo(path=path, version=version, raw=raw)


def find_java_info(preferred: Optional[str] = None) -> Optional[JavaInfo]:
    for candidate in _java_candidates(preferred):
        info = _probe_java(candidate)
        if info is not None:
            return info
    return None


def _require_java_info(preferred: Optional[str] = None) -> JavaInfo:
    info = find_java_info(preferred)
    if info is None:
        if preferred:
            raise MCDownError(
                "无法使用指定的 Java：%s。请选择有效的 java 可执行文件。" % preferred
            )
        raise MCDownError(
            "未找到 Java。Forge / NeoForge 安装需要 Java 17 或更高版本，"
            "请安装 Java 或通过启动脚本/命令行指定 Java 路径。"
        )
    return info



_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text_value: str) -> str:
    return _ANSI_RE.sub("", text_value)


def _library_artifact_from_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把 Fabric/Quilt profile 的 library 条目转换成可下载信息。"""
    name = str(entry.get("name") or "").strip()
    if not name:
        raise MCDownError("加载器元数据中存在空的依赖名称。")
    downloads = entry.get("downloads")
    artifact = downloads.get("artifact") if isinstance(downloads, dict) else None
    artifact = artifact if isinstance(artifact, dict) else {}

    relative_path = str(artifact.get("path") or "").strip()
    official_url = str(artifact.get("url") or "").strip()
    sha1 = str(artifact.get("sha1") or entry.get("sha1") or "").strip().lower()
    try:
        expected_size = int(artifact.get("size") or entry.get("size") or 0)
    except (TypeError, ValueError):
        expected_size = 0
    if expected_size < 0:
        expected_size = 0

    if not relative_path:
        raw_name = name
        extension = "jar"
        if "@" in raw_name:
            raw_name, extension = raw_name.rsplit("@", 1)
        parts = raw_name.split(":")
        if len(parts) < 3:
            raise MCDownError("无法解析加载器依赖坐标：%s" % name)
        group, artifact_id, version = parts[0], parts[1], parts[2]
        classifier = parts[3] if len(parts) > 3 else ""
        filename = "%s-%s%s.%s" % (
            artifact_id,
            version,
            ("-" + classifier) if classifier else "",
            extension,
        )
        relative_path = "/".join(
            [part for part in group.split(".") if part]
            + [artifact_id, version, filename]
        )

    path_parts = relative_path.replace("\\", "/").split("/")
    if (
        not relative_path
        or relative_path.startswith("/")
        or any(part in ("", ".", "..") for part in path_parts)
    ):
        raise MCDownError("加载器依赖路径异常：%s" % relative_path)

    base_url = str(entry.get("url") or "").strip()
    if base_url and not base_url.endswith("/"):
        base_url += "/"
    official_sources: List[str] = []
    if base_url:
        official_sources.append(base_url + relative_path)
    if official_url:
        official_sources.append(official_url)

    return {
        "name": name,
        "path": relative_path,
        "sha1": sha1,
        "size": expected_size,
        "official_sources": official_sources,
    }


def _library_is_valid(path: Path, expected_sha1: str, expected_size: int) -> bool:
    if not path.is_file():
        return False
    if expected_sha1:
        try:
            return _sha1_file(path) == expected_sha1.lower()
        except OSError:
            return False
    if expected_size:
        try:
            return path.stat().st_size == expected_size
        except OSError:
            return False
    return zipfile.is_zipfile(str(path))


def _copy_file_atomic(source: Path, destination: Path) -> None:
    temp_path = destination.with_name(destination.name + ".part")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, destination)
    except OSError as exc:
        _safe_remove(temp_path)
        raise MCDownError(
            "复制文件到 %s 失败：%s" % (destination, exc.strerror or exc)
        ) from exc


def _read_launcher_profiles(game_dir: Path) -> Dict[str, Any]:
    path = game_dir / "launcher_profiles.json"
    if not path.exists():
        doc: Dict[str, Any] = {"profiles": {}, "settings": {}, "version": 3}
        _write_atomic(path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return doc
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCDownError(
            "launcher_profiles.json 不是有效的 JSON，无法安全写入启动配置：%s" % exc
        ) from exc
    if not isinstance(data, dict):
        raise MCDownError("launcher_profiles.json 格式异常。")
    if not isinstance(data.get("profiles"), dict):
        data["profiles"] = {}
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    data.setdefault("version", 3)
    return data


def _write_launcher_profiles(game_dir: Path, data: Dict[str, Any]) -> None:
    path = game_dir / "launcher_profiles.json"
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_atomic(path, raw)


def _record_launcher_profile(
    game_dir: Path,
    loader: str,
    mc_version: str,
    loader_version: str,
    profile_id: str,
) -> None:
    data = _read_launcher_profiles(game_dir)
    profiles = data["profiles"]
    key = "%s-loader-%s" % (loader, mc_version)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    previous = profiles.get(key)
    created = now
    if isinstance(previous, dict) and previous.get("created"):
        created = str(previous.get("created"))
    profiles[key] = {
        "name": "%s %s - %s" % (loader_label(loader), loader_version, mc_version),
        "type": "custom",
        "lastVersionId": profile_id,
        "created": created,
        "lastUsed": now,
    }
    _write_launcher_profiles(game_dir, data)


def _prepare_loader_game_dir(
    game_dir: Path,
    mc_version: str,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
) -> Path:
    """准备用户选定的 .minecraft 根目录；原版文件已经在 versions/<mc>/ 中。"""
    _check_cancel(cancel_event)
    _ensure_minecraft_skeleton(game_dir)
    base_dir = game_dir / "versions" / mc_version
    if not (base_dir / (mc_version + ".jar")).is_file() or not (
        base_dir / (mc_version + ".json")
    ).is_file():
        raise MCDownError("原版 JAR/JSON 未位于 versions/%s/ 中，无法准备加载器安装。" % mc_version)
    _emit(progress, "loader_prepare", "正在准备 .minecraft 目录…", 0, 0)
    _read_launcher_profiles(game_dir)
    return game_dir


def _write_pcl_instance_isolation(instance_dir: Path, log: Callable[[str], None]) -> Path:
    """PCL 2.x 的每版本设置文件，强制启用版本隔离。

    PCL 的版本设置保存在 ``versions/<id>/PCL/Setup.ini``，格式为
    ``Key:Value``（冒号分隔）并使用 CRLF。源码中的 PathIndie 会优先检查
    ``VersionArgumentIndieV2``；旧版本使用整数 ``VersionArgumentIndie``。
    """
    path = instance_dir / PCL_INSTANCE_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    target_keys = {"VersionArgumentIndie", "VersionArgumentIndieV2"}
    lines: List[str] = []
    if path.is_file():
        try:
            current = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            current = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise MCDownError("无法读取 PCL 版本设置 %s：%s" % (path, exc.strerror or exc)) from exc
        for line in current.splitlines():
            stripped = line.strip()
            if ":" in stripped:
                key = stripped.split(":", 1)[0].strip()
                if key in target_keys:
                    continue
            if stripped:
                lines.append(line.rstrip("\r\n"))
    lines.append("VersionArgumentIndie:1")
    lines.append("VersionArgumentIndieV2:True")
    _write_atomic(path, ("\r\n".join(lines) + "\r\n").encode("utf-8"))
    log("已写入 PCL 版本隔离设置：%s" % path)
    return path


def _write_hmcl_instance_settings(
    instance_dir: Path,
    loader: str,
    log: Callable[[str], None],
) -> Path:
    """写入 HMCL 3.16.x 的实例级设置，让运行目录指向版本隔离目录。

    HMCL 读取 ``versions/<id>/.hmcl/config/instance-game-settings.json``。
    ``runningDirectory`` 为空且 overrideProperties 包含该字段时，
    HMCL 会把游戏目录设为 ``versions/<id>``（见 HMCLGameRepository）。
    """
    path = instance_dir / HMCL_INSTANCE_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    document: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
            else:
                raise ValueError("根节点不是对象")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            backup = path.with_name(
                path.name + ".invalid-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak"
            )
            try:
                os.replace(path, backup)
            except OSError as backup_exc:
                raise MCDownError(
                    "无法备份损坏的 HMCL 实例设置 %s：%s" % (path, backup_exc.strerror or backup_exc)
                ) from backup_exc
            log("现有 HMCL 实例设置损坏，已备份为：%s（%s）" % (backup, exc))
            document = {}
    if not document:
        document = {
            "$schema": HMCL_INSTANCE_SCHEMA_URL,
            "icon": {
                "fabric": "FABRIC",
                "forge": "FORGE",
                "neoforge": "NEO_FORGE",
                "quilt": "QUILT",
            }.get(loader, "DEFAULT"),
        }
    document.setdefault("$schema", HMCL_INSTANCE_SCHEMA_URL)
    overrides = document.get("overrideProperties")
    if not isinstance(overrides, list):
        overrides = []
    if "runningDirectory" not in overrides:
        overrides.append("runningDirectory")
    document["overrideProperties"] = overrides
    if not isinstance(document.get("runningDirectory"), str):
        document["runningDirectory"] = ""
    _write_atomic(
        path,
        (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    log("已写入 HMCL 版本隔离设置：%s" % path)
    return path


def _prepare_isolated_instance(
    instance_dir: Path,
    loader: str,
    mc_version: str,
    log: Callable[[str], None],
) -> Tuple[Path, Tuple[Path, ...]]:
    """为加载器实例创建版本隔离数据目录和 PCL/HMCL 隔离元数据。"""
    if not instance_dir.is_dir():
        raise MCDownError("版本实例目录不存在：%s" % instance_dir)
    for child in (
        "mods",
        "saves",
        "resourcepacks",
        "shaderpacks",
        "config",
        "logs",
        "PCL",
        ".hmcl/config",
    ):
        try:
            (instance_dir / child).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MCDownError(
                "无法创建版本隔离目录 %s：%s" % (instance_dir / child, exc.strerror or exc)
            ) from exc
    metadata: List[Path] = []
    metadata.append(_write_pcl_instance_isolation(instance_dir, log))
    metadata.append(_write_hmcl_instance_settings(instance_dir, loader, log))
    log(
        "版本隔离目录：%s（Minecraft %s，%s）"
        % (instance_dir, mc_version, loader_label(loader))
    )
    return instance_dir, tuple(metadata)


def _verify_launcher_structure(
    game_dir: Path,
    instance_dirs: Tuple[Path, ...],
    profile_paths: Tuple[Path, ...],
    log: Callable[[str], None],
) -> None:
    """对生成的标准 .minecraft / 版本隔离目录做一次快速自检。"""
    missing: List[str] = []
    for relative in ("assets/indexes", "assets/objects", "libraries", "versions"):
        path = game_dir / relative
        if not path.is_dir():
            missing.append(str(path))
    for profile_path in profile_paths:
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            if profile.get("inheritsFrom"):
                missing.append("%s 仍继承原版" % profile_path)
            if profile.get("id") != profile_path.parent.name:
                missing.append("%s 的 id 与目录名不一致" % profile_path)
            if not isinstance(profile.get("mainClass"), str) or not profile["mainClass"]:
                missing.append("%s 的 mainClass" % profile_path)
            if not isinstance(profile.get("libraries"), list) or not profile["libraries"]:
                missing.append("%s 的 libraries" % profile_path)
            if not (profile_path.parent / (profile_path.parent.name + ".jar")).is_file():
                missing.append("%s 的独立 JAR" % profile_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            missing.append(str(profile_path))
    for instance_dir in instance_dirs:
        for relative in ("mods", "saves", "resourcepacks"):
            path = instance_dir / relative
            if not path.is_dir():
                missing.append(str(path))
        pcl_ini = instance_dir / PCL_INSTANCE_SETTINGS_FILE
        try:
            values = {}
            for line in pcl_ini.read_text(encoding="utf-8-sig").splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    values[key.strip()] = value.strip()
            if values.get("VersionArgumentIndieV2", "").lower() != "true":
                missing.append("%s 中的 PCL 版本隔离开关" % pcl_ini)
        except OSError:
            missing.append(str(pcl_ini))
        hmcl_json = instance_dir / HMCL_INSTANCE_SETTINGS_FILE
        try:
            hmcl_data = json.loads(hmcl_json.read_text(encoding="utf-8"))
            schema = hmcl_data.get("$schema")
            if not isinstance(schema, str) or not schema.startswith(
                "https://schemas.glavo.site/hmcl/instance-game-settings/1."
            ):
                missing.append("%s 中的 HMCL schema" % hmcl_json)
            if "runningDirectory" not in (hmcl_data.get("overrideProperties") or []):
                missing.append("%s 中的 HMCL runningDirectory 隔离标记" % hmcl_json)
            if not isinstance(hmcl_data.get("runningDirectory"), str):
                missing.append("%s 中的 HMCL runningDirectory 值" % hmcl_json)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            missing.append(str(hmcl_json))
    if missing:
        raise MCDownError("启动器目录结构自检失败，缺失：%s" % "；".join(missing[:4]))
    log("PCL/HMCL 版本隔离目录结构自检通过。")


def _profile_loader_sources(loader: str, mc_version: str, loader_version: str) -> Tuple[str, ...]:
    path = "/versions/loader/%s/%s/profile/json" % (
        urllib.parse.quote(mc_version, safe=""),
        urllib.parse.quote(loader_version, safe=""),
    )
    if loader == "fabric":
        return (FABRIC_META_BASE + path, FABRIC_META_OFFICIAL + path)
    return (QUILT_META_BASE + path, QUILT_META_OFFICIAL + path)


def _validate_profile_id(profile_id: str) -> str:
    value = str(profile_id or "").strip()
    if not value or value in (".", "..") or any(char in '<>:"/\\|?*' for char in value):
        raise MCDownError("加载器返回了不安全的版本 ID：%s" % profile_id)
    if not value.isprintable() or value.endswith(" ") or value.endswith("."):
        raise MCDownError("加载器返回了不安全的版本 ID：%s" % profile_id)
    return value


def _find_loader_profiles(
    game_dir: Path, loader: str, mc_version: str, loader_version: str
) -> Tuple[Path, ...]:
    versions_dir = game_dir / "versions"
    if not versions_dir.is_dir():
        return ()
    results: List[Path] = []
    for child in versions_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name.casefold()
        if loader == "fabric" and "fabric-loader" not in name:
            continue
        if loader == "quilt" and "quilt-loader" not in name:
            continue
        if loader == "forge" and ("forge" not in name or "neoforge" in name):
            continue
        if loader == "neoforge" and not (
            "neoforge" in name
            or (mc_version.casefold() + "-forge-" in name)
            or ("forge" in name and loader_version.casefold() in name)
        ):
            continue
        json_path = child / (child.name + ".json")
        if json_path.is_file():
            results.append(json_path)
            continue
        json_files = list(child.glob("*.json"))
        if json_files:
            results.append(json_files[0])
    return tuple(sorted(results, key=lambda path: path.name.casefold()))


def _merge_version_fragment(parent: Any, child: Any) -> Any:
    """按 HMCL/PCL 的版本继承语义合并两个 JSON 片段。

    父级与子级都是对象时递归合并；数组按“父级在前、子级在后”拼接。
    加载器 libraries 的先后顺序由调用方单独处理（子级在前）。
    """
    if isinstance(parent, dict) and isinstance(child, dict):
        merged = {key: copy.deepcopy(value) for key, value in parent.items()}
        for key, value in child.items():
            if key in merged:
                merged[key] = _merge_version_fragment(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    if isinstance(parent, list) and isinstance(child, list):
        return copy.deepcopy(parent) + copy.deepcopy(child)
    return copy.deepcopy(child)


def _make_standalone_loader_version(
    game_dir: Path,
    base_version: str,
    profile_path: Path,
    log: Callable[[str], None],
) -> None:
    """把 inheritsFrom 原版的加载器配置合并成独立的版本目录。

    合并后不会在启动器版本列表中留下额外的原版版本；原版 JAR 会复制到
    加载器目录并改名为 ``<loader-id>.jar``。assets/libraries 仍然是共享的。
    """
    loader_dir = profile_path.parent
    loader_id = loader_dir.name
    try:
        loader_data = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCDownError("无法读取加载器版本 JSON %s：%s" % (profile_path, exc)) from exc
    if not isinstance(loader_data, dict):
        raise MCDownError("加载器版本 JSON 格式异常：%s" % profile_path)

    parent_id = str(loader_data.get("inheritsFrom") or "").strip()
    if parent_id and parent_id != base_version:
        raise MCDownError(
            "加载器版本 %s 继承自 %s，而不是本次准备的原版 %s。"
            % (loader_id, parent_id, base_version)
        )
    base_path = game_dir / "versions" / base_version / (base_version + ".json")
    try:
        base_data = json.loads(base_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCDownError("无法读取原版版本 JSON %s：%s" % (base_path, exc)) from exc
    if not isinstance(base_data, dict):
        raise MCDownError("原版版本 JSON 格式异常：%s" % base_path)

    merged = _merge_version_fragment(base_data, loader_data)
    # 加载器的 libraries 必须排在原版 libraries 之前，避免类被原版旧版本覆盖。
    loader_libraries = loader_data.get("libraries")
    base_libraries = base_data.get("libraries")
    merged["libraries"] = copy.deepcopy(
        (loader_libraries if isinstance(loader_libraries, list) else [])
        + (base_libraries if isinstance(base_libraries, list) else [])
    )
    merged["id"] = loader_id
    merged["jar"] = loader_id
    merged.pop("inheritsFrom", None)
    base_launcher_version = max(
        int(base_data.get("minimumLauncherVersion") or 0),
        int(loader_data.get("minimumLauncherVersion") or 0),
    )
    if base_launcher_version:
        merged["minimumLauncherVersion"] = base_launcher_version
    if not isinstance(merged.get("mainClass"), str) or not merged["mainClass"]:
        raise MCDownError("加载器版本 JSON 缺少 mainClass：%s" % profile_path)
    if not isinstance(merged.get("libraries"), list) or not merged["libraries"]:
        raise MCDownError("加载器版本 JSON 缺少 libraries：%s" % profile_path)
    if "assetIndex" not in merged and "assets" not in merged:
        raise MCDownError("加载器版本 JSON 缺少资源索引信息，无法合并原版：%s" % profile_path)

    base_jar_owner = str(base_data.get("jar") or base_version).strip() or base_version
    base_jar = game_dir / "versions" / base_jar_owner / (base_jar_owner + ".jar")
    if not base_jar.is_file():
        raise MCDownError("合并加载器版本时找不到原版 JAR：%s" % base_jar)
    destination_jar = loader_dir / (loader_id + ".jar")
    _copy_file_atomic(base_jar, destination_jar)

    raw = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_atomic(profile_path, raw)
    log(
        "已合并为独立加载器版本：%s（原版 JAR 已复制为 %s）"
        % (loader_id, destination_jar.name)
    )


def _versions_depending_on_base(game_dir: Path, base_version: str) -> Tuple[Path, ...]:
    """找出仍然通过 inheritsFrom 依赖原版版本的其他版本。"""
    result: List[Path] = []
    versions_dir = game_dir / "versions"
    if not versions_dir.is_dir():
        return ()
    for child in versions_dir.iterdir():
        if not child.is_dir() or child.name == base_version:
            continue
        json_path = child / (child.name + ".json")
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("inheritsFrom") == base_version:
            result.append(json_path)
    return tuple(sorted(result, key=lambda path: path.name.casefold()))


def _move_installer_root_logs(game_dir: Path, log: Callable[[str], None]) -> None:
    """把 Forge/NeoForge 落在 .minecraft 根目录的安装器日志收进 .mcdown。"""
    installers_dir = game_dir / ".mcdown" / "installers"
    for path in game_dir.glob("*-installer*.jar.log"):
        if not path.is_file():
            continue
        try:
            installers_dir.mkdir(parents=True, exist_ok=True)
            destination = installers_dir / path.name
            os.replace(path, destination)
            log("安装器根目录日志已移动：%s" % destination)
        except OSError as exc:
            log("警告：无法移动安装器日志 %s：%s" % (path, exc.strerror or exc))


def _download_loader_installer(
    loader: str,
    entry: LoaderVersion,
    destination_dir: Path,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
    log: Callable[[str], None],
) -> Tuple[Path, int]:
    safe_artifact = re.sub(r"[^0-9A-Za-z._+-]+", "_", entry.artifact_version or entry.version)
    filename = "%s-%s-installer.jar" % (loader, safe_artifact)
    destination = destination_dir / filename
    sources: List[Tuple[str, str]] = []
    if entry.installer_url:
        sources.append((entry.installer_url, entry.installer_sha1))
    if entry.fallback_installer_url:
        sources.append((entry.fallback_installer_url, entry.installer_sha1))
    if not sources:
        raise MCDownError("%s 没有可用的安装器下载地址。" % loader_label(loader))
    if destination.is_file() and zipfile.is_zipfile(str(destination)):
        if entry.installer_sha1:
            try:
                if _sha1_file(destination) == entry.installer_sha1.lower():
                    log("安装器已存在且校验通过，跳过下载：%s" % destination)
                    return destination, destination.stat().st_size
            except OSError:
                pass
        else:
            log("安装器已存在，跳过下载：%s" % destination)
            return destination, destination.stat().st_size

    errors: List[str] = []
    for url, sha1 in sources:
        host = urllib.parse.urlparse(url).netloc
        _emit(
            progress,
            "loader_installer",
            "正在下载 %s 安装器（%s）…" % (loader_label(loader), host),
            0,
            0,
        )
        log("下载 %s 安装器：%s" % (loader_label(loader), url))
        try:
            _download_file(
                url,
                destination,
                "%s 安装器" % loader_label(loader),
                sha1,
                entry.installer_size,
                progress,
                cancel_event,
                stage="loader_installer",
                verify_stage="loader_installer_verify",
                attempts=1,
                timeout=30,
                deadline_seconds=(120 if loader == "forge" else 600),
            )
        except MCDownError as exc:
            errors.append(str(exc))
            log("此下载源失败：%s" % exc)
            continue
        if not zipfile.is_zipfile(str(destination)):
            errors.append("%s 下载到的不是有效 JAR 文件。" % host)
            _safe_remove(destination)
            continue
        return destination, destination.stat().st_size
    raise MCDownError(
        "下载 %s 安装器失败。%s" % (loader_label(loader), "；".join(errors[-2:]))
    )


def _patch_neoforge_installer(source: Path, destination: Path, log: Callable[[str], None]) -> Tuple[Path, int]:
    """把 NeoForge 安装器内置的 Maven 地址改写为 BMCLAPI，保证国内可安装。"""
    replacements = (
        ("https://maven.neoforged.net/releases/", API_BASE + "/maven/"),
    )
    changed = 0
    try:
        with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(destination, "w") as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename.endswith(".json"):
                    for old, new in replacements:
                        count = data.count(old.encode("utf-8"))
                        if count:
                            data = data.replace(old.encode("utf-8"), new.encode("utf-8"))
                            changed += count
                zout.writestr(info, data)
    except (OSError, zipfile.BadZipFile) as exc:
        _safe_remove(destination)
        raise MCDownError("无法为 NeoForge 安装器应用 BMCLAPI 镜像：%s" % exc) from exc
    if not zipfile.is_zipfile(str(destination)):
        _safe_remove(destination)
        raise MCDownError("生成的 NeoForge 安装器不是有效的 JAR 文件。")
    if changed:
        log("NeoForge 安装器已改用 BMCLAPI Maven（%d 处地址）。" % changed)
    else:
        log("NeoForge 安装器无需地址改写。")
    return destination, changed


def _is_important_installer_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith(
        (
            "patching ",
            "considering ",
            "injecting profile",
            "        ",
            "at ",
            "java.net",
            "java.base",
            "caused by",
        )
    ):
        return False
    if len(stripped) > 180:
        return False
    return True


def _run_installer(
    java_path: str,
    installer_path: Path,
    game_dir: Path,
    loader: str,
    log_path: Path,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
    log_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[int, List[str]]:
    command = [java_path, "-jar", str(installer_path), "--installClient", str(game_dir)]
    try:
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MCDownError("无法创建安装器日志文件 %s：%s" % (log_path, exc.strerror or exc)) from exc

    tail: List[str] = []
    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    proc: Optional[subprocess.Popen] = None
    reader_thread: Optional[threading.Thread] = None
    try:
        log_handle.write("命令：%s\n\n" % " ".join(command))
        log_handle.flush()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.Popen(
            command,
            cwd=str(game_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )

        def reader() -> None:
            try:
                if proc is not None and proc.stdout is not None:
                    for raw_line in proc.stdout:
                        line_queue.put(raw_line)
            except Exception as exc:  # 防御性兜底：安装器输出异常不能卡死界面
                line_queue.put("[MCDown 读取安装器输出失败：%s]\n" % exc)
            finally:
                line_queue.put(None)

        reader_thread = threading.Thread(target=reader, name="mcdown-installer-output", daemon=True)
        reader_thread.start()
        _emit(
            progress,
            "loader_installer_run",
            "正在运行 %s 安装器，首次安装可能需要几分钟…" % loader_label(loader),
            0,
            0,
        )
        if log_callback:
            log_callback("安装器日志：%s" % log_path)

        started = time.monotonic()
        reader_done = False
        last_status_emit = 0.0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process(proc)
                raise DownloadCancelled("已取消安装。")
            if time.monotonic() - started > LOADER_INSTALL_TIMEOUT:
                _terminate_process(proc)
                raise MCDownError("安装器运行超时，请查看日志：%s" % log_path)
            try:
                raw_line = line_queue.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is not None and reader_done:
                    break
                continue
            if raw_line is None:
                reader_done = True
                if proc.poll() is not None:
                    break
                continue
            line = _strip_ansi(str(raw_line)).rstrip()
            log_handle.write(line + "\n")
            log_handle.flush()
            if not line:
                continue
            stripped = line.strip()
            if stripped:
                tail.append(stripped)
                if len(tail) > 80:
                    del tail[:-80]
                now = time.monotonic()
                if _is_important_installer_line(line) and now - last_status_emit >= 0.4:
                    _emit(
                        progress,
                        "loader_installer_run",
                        "正在安装：%s" % (stripped[:110] + ("…" if len(stripped) > 110 else "")),
                        0,
                        0,
                    )
                    last_status_emit = now
        try:
            returncode = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            raise MCDownError("安装器没有正常退出，请查看日志：%s" % log_path)
        log_handle.write("\n退出码：%s\n" % returncode)
        log_handle.flush()
        if reader_thread is not None:
            reader_thread.join(timeout=1.0)
        return returncode, tail
    except KeyboardInterrupt:
        if proc is not None:
            _terminate_process(proc)
        raise
    except (OSError, ValueError) as exc:
        if proc is not None:
            _terminate_process(proc)
        raise MCDownError("无法启动 Java 安装器：%s" % exc) from exc
    finally:
        try:
            log_handle.close()
        except OSError:
            pass


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _install_direct_profile(
    loader: str,
    mc_version: str,
    entry: LoaderVersion,
    game_dir: Path,
    progress: Optional[ProgressCallback],
    cancel_event: Optional[threading.Event],
    log: Callable[[str], None],
) -> Tuple[Tuple[Path, ...], int]:
    description = "%s 启动配置" % loader_label(loader)
    data = _fetch_json_sources(
        _profile_loader_sources(loader, mc_version, entry.version),
        description,
        progress,
        cancel_event,
        stage="loader_profile",
        max_bytes=LOADER_META_MAX_BYTES,
    )
    if not isinstance(data, dict):
        raise MCDownError("%s格式异常。" % description)
    profile_id = _validate_profile_id(str(data.get("id") or ""))
    inherits_from = str(data.get("inheritsFrom") or "").strip()
    if inherits_from and inherits_from != mc_version:
        raise MCDownError(
            "%s对应的 Minecraft 版本不一致（请求 %s，返回 %s）。"
            % (description, mc_version, inherits_from)
        )
    libraries = data.get("libraries")
    if not isinstance(libraries, list):
        libraries = []

    library_entries: List[Dict[str, Any]] = []
    for item in libraries:
        if not isinstance(item, dict):
            continue
        library_entries.append(_library_artifact_from_entry(item))

    total = len(library_entries)
    downloaded = 0
    _check_cancel(cancel_event)
    for index, library in enumerate(library_entries, 1):
        _check_cancel(cancel_event)
        relative = library["path"]
        destination = game_dir / "libraries" / Path(*str(relative).split("/"))
        display = str(library["name"])
        _emit(
            progress,
            "loader_library",
            "正在安装加载器依赖（%d/%d）：%s" % (index, total, display),
            index - 1,
            total,
        )
        if _library_is_valid(destination, str(library["sha1"]), int(library["size"])):
            _emit(
                progress,
                "loader_library",
                "依赖已存在，跳过（%d/%d）：%s" % (index, total, display),
                index,
                total,
            )
            continue

        bmclapi_url = API_BASE + "/maven/" + str(relative)
        sources: List[str] = [bmclapi_url]
        for source in library["official_sources"]:
            if source and source not in sources:
                sources.append(source)
        errors: List[str] = []
        installed = False
        for source in sources:
            try:
                size, _digest = _download_file(
                    source,
                    destination,
                    display,
                    str(library["sha1"]),
                    int(library["size"]),
                    None,
                    cancel_event,
                    stage="loader_library",
                    verify_stage="loader_library_verify",
                    attempts=1,
                    timeout=20,
                    deadline_seconds=60,
                )
            except MCDownError as exc:
                errors.append(str(exc))
                log("依赖下载源失败：%s（%s）" % (source, exc))
                continue
            if not zipfile.is_zipfile(str(destination)):
                errors.append("%s 返回的文件不是有效 JAR" % source)
                _safe_remove(destination)
                log("依赖文件损坏，尝试其他源：%s" % source)
                continue
            downloaded += size
            installed = True
            break
        if not installed:
            raise MCDownError(
                "加载器依赖下载失败：%s。%s"
                % (display, "；".join(errors[-2:]))
            )
        _emit(
            progress,
            "loader_library",
            "依赖安装完成（%d/%d）：%s" % (index, total, display),
            index,
            total,
        )

    _check_cancel(cancel_event)
    version_dir = game_dir / "versions" / profile_id
    version_dir.mkdir(parents=True, exist_ok=True)
    profile_path = version_dir / (profile_id + ".json")
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_atomic(profile_path, raw)
    _record_launcher_profile(game_dir, loader, mc_version, entry.version, profile_id)
    log("已写入启动配置：%s" % profile_path)
    downloaded += len(raw)
    _emit(progress, "loader_config", "加载器启动配置已写入", 1, 1)
    return (profile_path,), downloaded


def install_loader(
    loader: str,
    mc_version: str,
    loader_version: Optional[str] = None,
    output_root: Any = None,
    version_info: Optional[VersionInfo] = None,
    java_path: Optional[str] = None,
    progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    loader_entry: Optional[LoaderVersion] = None,
) -> LoaderInstallResult:
    """下载原版文件并一键安装 Fabric/Forge/NeoForge/Quilt。"""
    started_at = time.monotonic()
    loader = _validate_loader(loader)
    mc_version = validate_version_name(mc_version)
    if output_root is None:
        output_root = default_download_root()

    def log(message: str) -> None:
        if log_callback is None:
            return
        try:
            log_callback(message)
        except Exception:
            pass

    _check_cancel(cancel_event)
    if loader_entry is None:
        catalog = fetch_loader_catalog(loader, mc_version, progress, cancel_event)
        loader_entry = choose_loader_version(catalog, loader_version)
        if loader_entry is None:
            raise MCDownError(
                "Minecraft %s 没有可用的 %s 加载器版本。" % (mc_version, loader_label(loader))
            )
    elif loader_version and loader_entry.version != loader_version:
        raise MCDownError("加载器版本与所选条目不一致，请重新选择。")
    log("准备安装 Minecraft %s + %s %s" % (mc_version, loader_label(loader), loader_entry.version))

    def core_progress(update: ProgressUpdate) -> None:
        if update.stage == "done":
            return
        stage_map = {
            "prepare": "loader_core_prepare",
            "json": "loader_core_json",
            "jar": "loader_core_jar",
            "verify": "loader_core_verify",
        }
        _emit(
            progress,
            stage_map.get(update.stage, "loader_core"),
            update.message,
            update.current,
            update.total,
        )

    game_dir = _prepare_output_root(output_root)
    base_version_dir = game_dir / "versions" / mc_version
    base_version_preexisting = base_version_dir.is_dir() and any(base_version_dir.iterdir())
    core_result = download_game(
        mc_version,
        game_dir,
        version_info=version_info,
        progress=core_progress,
        cancel_event=cancel_event,
    )
    downloaded_bytes = core_result.downloaded_bytes
    _prepare_loader_game_dir(game_dir, mc_version, progress, cancel_event)

    installer_path: Optional[Path] = None
    installer_log: Optional[Path] = None
    used_java: Optional[str] = None
    method = "profile"
    before_profiles = set(_find_loader_profiles(game_dir, loader, mc_version, loader_entry.version))

    if loader in DIRECT_PROFILE_LOADERS:
        profiles, direct_bytes = _install_direct_profile(
            loader,
            mc_version,
            loader_entry,
            game_dir,
            progress,
            cancel_event,
            log,
        )
        downloaded_bytes += direct_bytes
    else:
        method = "installer"
        java_info = _require_java_info(java_path)
        used_java = java_info.path
        log("使用 Java：%s（%s）" % (java_info.path, java_info.version))
        installers_dir = game_dir / ".mcdown" / "installers"
        try:
            installers_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MCDownError("无法创建安装器目录：%s" % exc) from exc

        installer_path, installer_size = _download_loader_installer(
            loader,
            loader_entry,
            installers_dir,
            progress,
            cancel_event,
            log,
        )
        downloaded_bytes += installer_size
        run_installer_path = installer_path
        if loader == "neoforge":
            patched = installers_dir / (installer_path.stem + "-bmclapi.jar")
            run_installer_path, _changed = _patch_neoforge_installer(
                installer_path, patched, log
            )
            installer_path = patched

        if not java_info:
            raise MCDownError("未找到可用的 Java。")
        log("正在运行安装器，完整日志将保存到本地文件。")
        log_path = installers_dir / ("%s-%s-install.log" % (loader, re.sub(r'[^0-9A-Za-z._+-]+', '_', loader_entry.version)))
        try:
            returncode, tail = _run_installer(
                java_info.path,
                run_installer_path,
                game_dir,
                loader,
                log_path,
                progress,
                cancel_event,
                log,
            )
        except DownloadCancelled:
            raise
        except MCDownError:
            raise
        installer_log = log_path
        _move_installer_root_logs(game_dir, log)
        if returncode != 0:
            useful = [line for line in tail[-12:] if _is_important_installer_line(line)]
            detail = "；".join(useful[-4:]) if useful else "安装器返回了非零退出码"
            raise MCDownError(
                "%s 安装器执行失败（退出码 %s）：%s。完整日志：%s"
                % (loader_label(loader), returncode, detail, log_path)
            )
        profiles = _find_loader_profiles(game_dir, loader, mc_version, loader_entry.version)
        new_profiles = tuple(path for path in profiles if path not in before_profiles)
        if new_profiles:
            profiles = new_profiles
        if not profiles:
            raise MCDownError(
                "%s 安装器已结束，但没有找到新的启动配置。请查看日志：%s"
                % (loader_label(loader), log_path)
            )
        log("安装器完成，发现 %d 个启动配置。" % len(profiles))

    # 加载器版本不再单独显示成一个“原版 + 加载器”的两个版本：
    # 将原版版本 JSON 合并进加载器 JSON，并复制原版 JAR 到加载器目录。
    for profile_path in profiles:
        _make_standalone_loader_version(game_dir, mc_version, profile_path, log)
    dependent_versions = _versions_depending_on_base(game_dir, mc_version)
    if base_version_preexisting:
        log("检测到安装前已有原版版本，保留：%s" % base_version_dir)
    elif dependent_versions:
        log(
            "仍有其他版本依赖原版，保留原版版本目录：%s（%s）"
            % (base_version_dir, dependent_versions[0].parent.name)
        )
    else:
        try:
            shutil.rmtree(base_version_dir)
        except OSError as exc:
            raise MCDownError(
                "加载器已安装，但无法删除临时原版版本目录 %s：%s"
                % (base_version_dir, exc.strerror or exc)
            ) from exc
        log("已移除临时原版版本目录；加载器版本现在是独立版本：%s" % base_version_dir)

    # 按照 HMCL 与 PCL 的版本隔离约定：
    #   <.minecraft>/versions/<instance>/ 是隔离后的游戏目录；
    #   assets/ 与 libraries/ 仍然共享于 .minecraft 根目录。
    # 同时写入两个启动器各自的实例级隔离设置，确保导入后直接使用隔离目录。
    instance_dirs: List[Path] = []
    isolation_files: List[Path] = []
    for profile_path in profiles:
        instance_dir, metadata = _prepare_isolated_instance(
            profile_path.parent,
            loader,
            mc_version,
            log,
        )
        instance_dirs.append(instance_dir)
        isolation_files.extend(metadata)

    _verify_launcher_structure(game_dir, tuple(instance_dirs), profiles, log)
    _check_cancel(cancel_event)
    elapsed = max(0.0, time.monotonic() - started_at)
    _emit(progress, "loader_done", "%s 安装完成" % loader_label(loader), 1, 1)
    log("加载器安装完成，游戏根目录（.minecraft）：%s" % game_dir)
    for instance_dir in instance_dirs:
        log("版本隔离目录：%s" % instance_dir)
    return LoaderInstallResult(
        loader=loader,
        loader_version=loader_entry.version,
        mc_version=mc_version,
        game_dir=game_dir,
        profile_paths=profiles,
        installer_path=installer_path,
        installer_log=installer_log,
        java_path=used_java,
        downloaded_bytes=downloaded_bytes,
        elapsed=elapsed,
        method=method,
        instance_dirs=tuple(instance_dirs),
        isolation_files=tuple(isolation_files),
    )


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------


def _pick_font(root: Any, candidates: Tuple[str, ...], fallback: str) -> str:
    import tkinter.font as tkfont

    available = {name.casefold(): name for name in tkfont.families(root)}
    for candidate in candidates:
        found = available.get(candidate.casefold())
        if found:
            return found
    try:
        return tkfont.nametofont("TkDefaultFont").actual("family")
    except Exception:
        return fallback


def gui_main() -> int:
    """启动图形界面。"""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print("未安装 Tkinter，无法启动图形界面。")
        print("Debian/Ubuntu 可执行：sudo apt install python3-tk")
        print("也可以使用命令行模式，例如：python mcdown.py --version 1.20.1 --output ./downloads")
        return 1

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print("无法启动图形界面：%s" % exc)
        print("在没有桌面环境时，请使用命令行模式，例如：")
        print("  python mcdown.py --version 1.20.1 --output ./downloads")
        print("  python mcdown.py --list release")
        return 1

    app = _MCDownApp(root, tk, ttk, filedialog, messagebox)
    try:
        root.mainloop()
    finally:
        if app.alive:
            app.shutdown()
    return 0


class _MCDownApp:
    BG = "#EEF2F7"
    CARD = "#FFFFFF"
    TEXT = "#172231"
    MUTED = "#66758A"
    FAINT = "#94A3B8"
    BORDER = "#DCE4EE"
    ACCENT = "#1E7A44"
    ACCENT_DARK = "#135B32"
    ACCENT_SOFT = "#E7F4EB"
    PREVIEW_BG = "#F4F9F5"
    SOFT = "#F4F7FB"
    AMBER = "#A86500"
    AMBER_BG = "#FFF4DE"
    RED = "#B42318"
    RED_BG = "#FEECE9"
    BLUE = "#2563A5"

    def __init__(self, root: Any, tk: Any, ttk: Any, filedialog: Any, messagebox: Any):
        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.alive = True
        self.busy = False
        self._manifest_loading = False
        self.manifest: Optional[VersionManifest] = None
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.cancel_event: Optional[threading.Event] = None
        self.last_folder: Optional[Path] = None
        self._last_stage: Optional[str] = None
        self._last_message: Optional[str] = None
        self._current_progress = 0.0
        self._indeterminate = False
        self.loader_dialog: Optional["_LoaderDialog"] = None
        self.loader_catalog_cache: Dict[Tuple[str, str], LoaderCatalog] = {}
        self.last_loader = "fabric"

        self._setup_fonts_and_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F5>", lambda _event: self.refresh_manifest())
        self.root.bind("<Escape>", lambda _event: self.cancel_download())
        self.root.after(80, self._drain_events)
        self._on_version_changed()
        self._on_path_changed()
        self.root.after(150, self.refresh_manifest)
        self.root.after(200, lambda: self.version_box.focus_set())

    # -- 初始化界面 ------------------------------------------------------

    def _setup_fonts_and_style(self) -> None:
        self.ui_family = _pick_font(
            self.root,
            (
                "Microsoft YaHei UI",
                "Microsoft YaHei",
                "PingFang SC",
                "Noto Sans CJK SC",
                "WenQuanYi Micro Hei",
                "Source Han Sans SC",
                "Segoe UI",
                "DejaVu Sans",
            ),
            "TkDefaultFont",
        )
        self.mono_family = _pick_font(
            self.root,
            ("Cascadia Mono", "Consolas", "SF Mono", "DejaVu Sans Mono", "Courier New"),
            self.ui_family,
        )
        self.body_font = (self.ui_family, 10)
        self.small_font = (self.ui_family, 9)
        self.bold_font = (self.ui_family, 10, "bold")
        self.section_font = (self.ui_family, 11, "bold")
        self.title_font = (self.ui_family, 21, "bold")
        self.subtitle_font = (self.ui_family, 9)

        self.style = self.ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except self.tk.TclError:
            pass

        self.root.configure(bg=self.BG)
        self.root.option_add("*TCombobox*Listbox.background", self.CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", self.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", self.ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
        self.root.option_add("*TCombobox*Listbox.font", self.body_font)

        self.style.configure(
            "TEntry",
            fieldbackground=self.CARD,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            insertcolor=self.TEXT,
            padding=(9, 7),
        )
        self.style.map(
            "TEntry",
            bordercolor=[("focus", self.ACCENT), ("hover", "#B9C7D8")],
            lightcolor=[("focus", self.ACCENT)],
            darkcolor=[("focus", self.ACCENT)],
            fieldbackground=[("disabled", self.SOFT)],
            foreground=[("disabled", self.FAINT)],
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=self.CARD,
            background=self.CARD,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            arrowcolor=self.MUTED,
            padding=(9, 7),
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.CARD), ("disabled", self.SOFT)],
            foreground=[("readonly", self.TEXT), ("disabled", self.FAINT)],
            selectbackground=[("readonly", self.CARD)],
            selectforeground=[("readonly", self.TEXT)],
            bordercolor=[("focus", self.ACCENT), ("hover", "#B9C7D8")],
            lightcolor=[("focus", self.ACCENT)],
            darkcolor=[("focus", self.ACCENT)],
        )
        self.style.configure(
            "Accent.TButton",
            background=self.ACCENT,
            foreground="#FFFFFF",
            font=self.bold_font,
            padding=(18, 9),
            borderwidth=0,
            relief="flat",
            focusthickness=0,
        )
        self.style.map(
            "Accent.TButton",
            background=[
                ("disabled", "#A9C9B6"),
                ("pressed", self.ACCENT_DARK),
                ("active", "#176238"),
            ],
            foreground=[("disabled", "#F4FAF6")],
        )
        self.style.configure(
            "Secondary.TButton",
            background="#EDF2F8",
            foreground="#314156",
            font=self.body_font,
            padding=(12, 7),
            borderwidth=0,
            relief="flat",
            focusthickness=0,
        )
        self.style.map(
            "Secondary.TButton",
            background=[("disabled", "#F2F5F9"), ("pressed", "#DCE5F0"), ("active", "#E2EAF4")],
            foreground=[("disabled", self.FAINT)],
        )
        self.style.configure(
            "Loader.TButton",
            background=self.ACCENT_SOFT,
            foreground=self.ACCENT_DARK,
            font=self.bold_font,
            padding=(12, 7),
            borderwidth=0,
            relief="flat",
            focusthickness=0,
        )
        self.style.map(
            "Loader.TButton",
            background=[
                ("disabled", "#EEF3F0"),
                ("pressed", "#D3E8DA"),
                ("active", "#D9ECDF"),
            ],
            foreground=[("disabled", self.FAINT)],
        )
        self.style.configure(
            "Small.TButton",
            background=self.CARD,
            foreground=self.MUTED,
            font=self.small_font,
            padding=(6, 3),
            borderwidth=0,
            relief="flat",
            focusthickness=0,
        )
        self.style.map(
            "Small.TButton",
            background=[("active", self.SOFT), ("pressed", "#E8EEF6")],
            foreground=[("active", self.TEXT)],
        )
        self.style.configure(
            "Link.TButton",
            background=self.BG,
            foreground=self.ACCENT,
            font=self.bold_font,
            padding=(4, 3),
            borderwidth=0,
            relief="flat",
            focusthickness=0,
        )
        self.style.map(
            "Link.TButton",
            background=[("active", self.BG)],
            foreground=[("disabled", self.FAINT), ("active", self.ACCENT_DARK)],
        )
        self.style.configure(
            "Green.Horizontal.TProgressbar",
            background=self.ACCENT,
            troughcolor="#E7EDF5",
            bordercolor="#E7EDF5",
            lightcolor=self.ACCENT,
            darkcolor=self.ACCENT,
            thickness=9,
        )
        self.style.configure("TSeparator", background=self.BORDER)

    def _build_ui(self) -> None:
        self.root.title("%s %s" % (APP_NAME, APP_VERSION))
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(860, max(720, screen_w - 100))
        height = min(790, max(720, screen_h - 100))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self.root.minsize(760, 720)
        self._set_window_icon()

        outer = self.tk.Frame(self.root, bg=self.BG)
        outer.pack(fill="both", expand=True, padx=22, pady=(12, 8))
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(4, weight=1, minsize=106)

        self.version_var = self.tk.StringVar()
        self.path_var = self.tk.StringVar(value=str(default_download_root()))
        self.status_var = self.tk.StringVar(value="正在准备…")
        self.percent_var = self.tk.StringVar(value="0%")
        self.version_meta_var = self.tk.StringVar(value="正在获取版本列表…")
        self.latest_meta_var = self.tk.StringVar(value="连接 BMCLAPI 中…")
        self.preview_root_var = self.tk.StringVar()
        self.preview_jar_var = self.tk.StringVar()
        self.preview_json_var = self.tk.StringVar()

        self._build_header(outer)
        self._build_version_card(outer, row=1)
        self._build_location_card(outer, row=2)
        self._build_action_card(outer, row=3)
        self._build_log_card(outer, row=4)
        self._build_footer(outer, row=5)

        try:
            self.version_var.trace_add("write", lambda *_args: self._on_version_changed())
        except AttributeError:
            self.version_var.trace("w", lambda *_args: self._on_version_changed())
        try:
            self.path_var.trace_add("write", lambda *_args: self._on_path_changed())
        except AttributeError:
            self.path_var.trace("w", lambda *_args: self._on_path_changed())

    def _build_header(self, parent: Any) -> None:
        header = self.tk.Frame(parent, bg=self.BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.grid_columnconfigure(1, weight=1)

        canvas = self.tk.Canvas(header, width=62, height=62, bg=self.BG, highlightthickness=0)
        canvas.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 14))
        # 一个简洁的等距草方块，充当窗口图标之外的界面标识。
        canvas.create_polygon(31, 3, 59, 19, 31, 35, 3, 19, fill="#7FC65C", outline="")
        canvas.create_polygon(3, 19, 31, 35, 31, 59, 3, 43, fill="#8A613C", outline="")
        canvas.create_polygon(59, 19, 31, 35, 31, 59, 59, 43, fill="#6A482A", outline="")
        canvas.create_polygon(3, 19, 31, 35, 31, 41, 3, 25, fill="#5D923F", outline="")
        canvas.create_polygon(59, 19, 31, 35, 31, 41, 59, 25, fill="#4C7934", outline="")

        title = self.tk.Label(
            header,
            text="Minecraft 版本下载器",
            bg=self.BG,
            fg=self.TEXT,
            font=self.title_font,
            anchor="w",
        )
        title.grid(row=0, column=1, sticky="sw")
        subtitle = self.tk.Label(
            header,
            text="BMCLAPI 镜像  ·  原版核心 + 常用模组加载器  ·  Linux / Windows",
            bg=self.BG,
            fg=self.MUTED,
            font=self.subtitle_font,
            anchor="w",
        )
        subtitle.grid(row=1, column=1, sticky="nw", pady=(3, 0))

        right = self.tk.Frame(header, bg=self.BG)
        right.grid(row=0, column=2, rowspan=2, sticky="e", padx=(12, 0))
        self.conn_label = self.tk.Label(
            right,
            text="●  正在连接",
            bg=self.AMBER_BG,
            fg=self.AMBER,
            font=(self.ui_family, 9, "bold"),
            padx=11,
            pady=5,
        )
        self.conn_label.pack(side="left", padx=(0, 8))
        self.loader_button = self.ttk.Button(
            right,
            text="安装模组加载器",
            style="Loader.TButton",
            command=self.open_loader_dialog,
        )
        self.loader_button.pack(side="left")
        self.loader_button.state(["disabled"])

    def _make_card(self, parent: Any, row: int, sticky: str = "ew") -> Any:
        card = self.tk.Frame(
            parent,
            bg=self.CARD,
            highlightbackground=self.BORDER,
            highlightthickness=1,
        )
        card.grid(row=row, column=0, sticky=sticky, pady=(0, 8))
        inner = self.tk.Frame(card, bg=self.CARD)
        inner.pack(fill="both", expand=True, padx=16, pady=12)
        return card, inner

    def _section_head(self, parent: Any, number: str, title: str, hint: str = "") -> Any:
        row = self.tk.Frame(parent, bg=self.CARD)
        row.pack(fill="x")
        badge = self.tk.Label(
            row,
            text=number,
            bg=self.ACCENT_SOFT,
            fg=self.ACCENT_DARK,
            font=(self.ui_family, 9, "bold"),
            padx=7,
            pady=2,
        )
        badge.pack(side="left")
        label = self.tk.Label(
            row,
            text=title,
            bg=self.CARD,
            fg=self.TEXT,
            font=self.section_font,
        )
        label.pack(side="left", padx=(8, 0))
        if hint:
            hint_label = self.tk.Label(
                row,
                text=hint,
                bg=self.CARD,
                fg=self.FAINT,
                font=self.small_font,
            )
            hint_label.pack(side="right")
        return row

    def _build_version_card(self, parent: Any, row: int) -> None:
        _card, inner = self._make_card(parent, row)
        self._section_head(inner, "01", "选择游戏版本", "可直接输入版本号")

        line = self.tk.Frame(inner, bg=self.CARD)
        line.pack(fill="x", pady=(9, 0))
        self.version_box = self.ttk.Combobox(
            line,
            textvariable=self.version_var,
            style="TCombobox",
            font=self.body_font,
            height=18,
        )
        self.version_box.pack(side="left", fill="x", expand=True)
        self.version_box.bind("<<ComboboxSelected>>", lambda _event: self._on_version_changed())
        self.refresh_button = self.ttk.Button(
            line,
            text="刷新版本",
            style="Secondary.TButton",
            command=self.refresh_manifest,
        )
        self.refresh_button.pack(side="left", padx=(9, 0))

        meta = self.tk.Frame(inner, bg=self.CARD)
        meta.pack(fill="x", pady=(7, 0))
        self.version_meta_label = self.tk.Label(
            meta,
            textvariable=self.version_meta_var,
            bg=self.CARD,
            fg=self.MUTED,
            font=self.small_font,
            anchor="w",
        )
        self.version_meta_label.pack(side="left")
        self.latest_meta_label = self.tk.Label(
            meta,
            textvariable=self.latest_meta_var,
            bg=self.CARD,
            fg=self.FAINT,
            font=self.small_font,
            anchor="e",
        )
        self.latest_meta_label.pack(side="right")

    def _build_location_card(self, parent: Any, row: int) -> None:
        _card, inner = self._make_card(parent, row)
        self._section_head(inner, "02", "选择 .minecraft 文件夹", "所选文件夹直接作为 .minecraft 根目录")

        line = self.tk.Frame(inner, bg=self.CARD)
        line.pack(fill="x", pady=(9, 0))
        self.path_entry = self.ttk.Entry(
            line,
            textvariable=self.path_var,
            style="TEntry",
            font=self.body_font,
        )
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = self.ttk.Button(
            line,
            text="浏览…",
            style="Secondary.TButton",
            command=self.choose_directory,
        )
        self.browse_button.pack(side="left", padx=(9, 0))

        preview = self.tk.Frame(inner, bg=self.PREVIEW_BG)
        preview.pack(fill="x", pady=(9, 0))
        preview_left = self.tk.Frame(preview, bg=self.PREVIEW_BG)
        preview_left.pack(side="left", fill="x", expand=True, padx=11, pady=8)
        self.preview_folder_label = self.tk.Label(
            preview_left,
            textvariable=self.preview_root_var,
            bg=self.PREVIEW_BG,
            fg=self.ACCENT_DARK,
            font=(self.ui_family, 9, "bold"),
            anchor="w",
            justify="left",
            wraplength=690,
        )
        self.preview_folder_label.pack(fill="x", anchor="w")

        chips = self.tk.Frame(preview_left, bg=self.PREVIEW_BG)
        chips.pack(fill="x", pady=(5, 0))
        self.jar_chip = self.tk.Label(
            chips,
            textvariable=self.preview_jar_var,
            bg=self.ACCENT_SOFT,
            fg=self.ACCENT_DARK,
            font=(self.mono_family, 9),
            padx=8,
            pady=2,
        )
        self.jar_chip.pack(side="left")
        self.json_chip = self.tk.Label(
            chips,
            textvariable=self.preview_json_var,
            bg=self.ACCENT_SOFT,
            fg=self.ACCENT_DARK,
            font=(self.mono_family, 9),
            padx=8,
            pady=2,
        )
        self.json_chip.pack(side="left", padx=(7, 0))
        self.tk.Label(
            chips,
            text="文件名 = 版本号",
            bg=self.PREVIEW_BG,
            fg=self.FAINT,
            font=self.small_font,
        ).pack(side="left", padx=(10, 0))

    def _build_action_card(self, parent: Any, row: int) -> None:
        _card, inner = self._make_card(parent, row)
        status_line = self.tk.Frame(inner, bg=self.CARD)
        status_line.pack(fill="x")
        self.status_label = self.tk.Label(
            status_line,
            textvariable=self.status_var,
            bg=self.CARD,
            fg=self.MUTED,
            font=self.body_font,
            anchor="w",
            justify="left",
            wraplength=610,
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.percent_label = self.tk.Label(
            status_line,
            textvariable=self.percent_var,
            bg=self.CARD,
            fg=self.ACCENT,
            font=(self.ui_family, 10, "bold"),
            anchor="e",
        )
        self.percent_label.pack(side="right", padx=(10, 0))

        self.progress = self.ttk.Progressbar(
            inner,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            value=0,
            style="Green.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(8, 10))

        actions = self.tk.Frame(inner, bg=self.CARD)
        actions.pack(fill="x")
        self.action_hint_label = self.tk.Label(
            actions,
            text="将下载 2 个文件 · SHA-1 校验 · 已存在文件会自动跳过",
            bg=self.CARD,
            fg=self.FAINT,
            font=self.small_font,
            anchor="w",
        )
        self.action_hint_label.pack(side="left")

        self.download_button = self.ttk.Button(
            actions,
            text="开始下载",
            style="Accent.TButton",
            command=self.start_download,
        )
        self.download_button.pack(side="right")
        self.cancel_button = self.ttk.Button(
            actions,
            text="取消",
            style="Secondary.TButton",
            command=self.cancel_download,
        )
        self.cancel_button.pack(side="right", padx=(0, 8))
        self.download_button.state(["disabled"])
        self.cancel_button.state(["disabled"])

    def _build_log_card(self, parent: Any, row: int) -> None:
        card, inner = self._make_card(parent, row, sticky="nsew")
        head = self.tk.Frame(inner, bg=self.CARD)
        head.pack(fill="x")
        self.tk.Label(
            head,
            text="运行日志",
            bg=self.CARD,
            fg=self.TEXT,
            font=self.bold_font,
        ).pack(side="left")
        self.ttk.Button(
            head,
            text="清空",
            style="Small.TButton",
            command=self.clear_log,
        ).pack(side="right")

        body = self.tk.Frame(inner, bg=self.CARD)
        body.pack(fill="both", expand=True, pady=(7, 0))
        self.log_text = self.tk.Text(
            body,
            height=4,
            bg="#FBFCFE",
            fg=self.MUTED,
            insertbackground=self.TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.BORDER,
            font=(self.ui_family, 9),
            padx=9,
            pady=6,
            wrap="word",
            state="disabled",
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar = self.ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y", padx=(4, 0))
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.tag_configure("time", foreground=self.FAINT)
        self.log_text.tag_configure("error", foreground=self.RED)
        self.log("就绪。正在连接 BMCLAPI 获取版本列表…")

    def _build_footer(self, parent: Any, row: int) -> None:
        footer = self.tk.Frame(parent, bg=self.BG)
        footer.grid(row=row, column=0, sticky="ew", pady=(1, 0))
        footer.grid_columnconfigure(0, weight=1)
        source = self.tk.Label(
            footer,
            text="原版下载源：BMCLAPI   ·   加载器优先使用镜像，缺失时自动回退官方源",
            bg=self.BG,
            fg=self.FAINT,
            font=self.small_font,
            anchor="w",
        )
        source.grid(row=0, column=0, sticky="w")
        self.open_button = self.ttk.Button(
            footer,
            text="打开 .minecraft 文件夹",
            style="Link.TButton",
            command=self.open_download_folder,
        )
        self.open_button.grid(row=0, column=1, sticky="e")
        self.open_button.state(["disabled"])

    def _set_window_icon(self) -> None:
        try:
            icon = self.tk.PhotoImage(width=32, height=32)
            icon.put(self.BG, to=(0, 0, 32, 32))
            icon.put("#5B873C", to=(3, 3, 29, 29))
            icon.put("#8A613C", to=(5, 14, 27, 27))
            icon.put("#7FC65C", to=(5, 5, 27, 14))
            icon.put("#4C7934", to=(5, 14, 27, 17))
            self.root.iconphoto(True, icon)
            self._window_icon = icon
        except Exception:
            pass

    # -- 界面状态 --------------------------------------------------------

    def log(self, message: str, error: bool = False) -> None:
        if not self.alive:
            return
        self.log_text.configure(state="normal")
        if self.log_text.index("end-1c") != "1.0":
            self.log_text.insert("end", "\n")
        self.log_text.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        self.log_text.insert("end", message, "error" if error else ())
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _set_connection(self, state: str) -> None:
        if state == "online":
            self.conn_label.configure(text="●  镜像已连接", bg="#E8F5EC", fg=self.ACCENT_DARK)
        elif state == "offline":
            self.conn_label.configure(text="●  连接失败", bg=self.RED_BG, fg=self.RED)
        elif state == "working":
            self.conn_label.configure(text="●  正在下载", bg="#EAF1FB", fg=self.BLUE)
        else:
            self.conn_label.configure(text="●  正在连接", bg=self.AMBER_BG, fg=self.AMBER)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for widget in (
            self.version_box,
            self.path_entry,
            self.browse_button,
            self.refresh_button,
        ):
            if busy:
                widget.state(["disabled"])
            else:
                widget.state(["!disabled"])
        if busy:
            self.cancel_button.state(["!disabled"])
            self.download_button.state(["disabled"])
            self.loader_button.state(["disabled"])
        else:
            self.cancel_button.state(["disabled"])
            self._update_download_state()
        if not self._manifest_loading:
            if busy:
                self.refresh_button.state(["disabled"])
            else:
                self.refresh_button.state(["!disabled"])

    def _update_download_state(self) -> None:
        if not self.alive:
            return
        has_values = bool(self.version_var.get().strip()) and bool(self.path_var.get().strip())
        if self.busy or not has_values:
            self.download_button.state(["disabled"])
            self.loader_button.state(["disabled"])
        else:
            self.download_button.state(["!disabled"])
            self.loader_button.state(["!disabled"])

    def _set_progress(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        self._current_progress = value
        if not self._indeterminate:
            self.progress.configure(mode="determinate", value=value * 100.0)
        self.percent_var.set("%d%%" % int(round(value * 100.0)))

    def _start_indeterminate(self) -> None:
        if self._indeterminate:
            return
        self._indeterminate = True
        self.progress.configure(mode="indeterminate")
        self.progress.start(14)
        self.percent_var.set("…")

    def _stop_indeterminate(self) -> None:
        if not self._indeterminate:
            return
        self._indeterminate = False
        try:
            self.progress.stop()
        except Exception:
            pass
        self.progress.configure(mode="determinate")
        self.progress.configure(value=self._current_progress * 100.0)
        self.percent_var.set("%d%%" % int(round(self._current_progress * 100.0)))

    def _reset_progress(self) -> None:
        self._stop_indeterminate()
        self._current_progress = 0.0
        self.progress.configure(mode="determinate", value=0)
        self.percent_var.set("0%")

    # -- 版本与路径 ------------------------------------------------------

    def _on_version_changed(self) -> None:
        if not hasattr(self, "version_var"):
            return
        version = self.version_var.get().strip()
        display_version = version or "版本号"
        self.preview_jar_var.set("%s.jar" % display_version)
        self.preview_json_var.set("%s.json" % display_version)
        self._update_path_preview()
        self._update_version_meta(version)
        self._update_download_state()

    def _on_path_changed(self) -> None:
        if not hasattr(self, "path_var"):
            return
        self._update_path_preview()
        self._update_download_state()

    def _preview_root(self) -> Path:
        version = self.version_var.get().strip() or "版本号"
        raw = self.path_var.get().strip()
        if not raw:
            return Path(".minecraft 文件夹") / "versions" / version
        try:
            root = Path(os.path.expandvars(raw)).expanduser()
        except (OSError, ValueError):
            root = Path(raw)
        return root / "versions" / version

    def _update_path_preview(self) -> None:
        if not hasattr(self, "preview_root_var"):
            return
        path = self._preview_root()
        try:
            display = str(path)
        except Exception:
            display = ".minecraft 文件夹 / versions / 版本号"
        self.preview_root_var.set("版本目录   %s%s" % (display, os.sep))

    def _update_version_meta(self, version: str) -> None:
        info = self.manifest.find(version) if self.manifest and version else None
        if not version:
            self.version_meta_var.set("请选择版本，或直接输入版本号")
        elif info is not None:
            date = info.release_time[:10] if info.release_time else "未知"
            self.version_meta_var.set(
                "类型：%s   发布：%s" % (version_type_name(info.type), date)
            )
        elif self.manifest is not None:
            self.version_meta_var.set("自定义版本号，将直接从 BMCLAPI 获取版本信息")
        else:
            self.version_meta_var.set("版本列表尚未载入，将按输入的版本号下载")

    def _refresh_version_values(self) -> None:
        if not self.manifest:
            return
        values = [item.id for item in self.manifest.versions]
        self.version_box.configure(values=values)
        latest = self.manifest
        self.latest_meta_var.set(
            "最新正式版：%s   ·   最新快照：%s"
            % (latest.latest_release or "未知", latest.latest_snapshot or "未知")
        )

    def choose_directory(self) -> None:
        if self.busy:
            return
        current = self.path_var.get().strip()
        initial = Path(os.path.expandvars(current)).expanduser() if current else Path.home()
        if not initial.exists() or not initial.is_dir():
            initial = Path.home()
        selected = self.filedialog.askdirectory(
            parent=self.root,
            title="选择 .minecraft 文件夹（直接作为游戏根目录）",
            initialdir=str(initial),
            mustexist=False,
        )
        if selected:
            self.path_var.set(selected)

    # -- 网络事件 --------------------------------------------------------

    def refresh_manifest(self) -> None:
        if not self.alive or self.busy or self._manifest_loading:
            return
        self._manifest_loading = True
        self.refresh_button.state(["disabled"])
        if not self.manifest:
            self._set_connection("loading")
            self.status_var.set("正在获取 BMCLAPI 版本列表…")
            if not self.busy:
                self._start_indeterminate()
        self.log("正在刷新 BMCLAPI 版本列表…")

        def worker() -> None:
            try:
                manifest = fetch_manifest()
            except DownloadCancelled:
                self.events.put(("manifest_cancelled", None))
            except Exception as exc:
                self.events.put(("manifest_error", str(exc)))
            else:
                self.events.put(("manifest_ok", manifest))

        threading.Thread(target=worker, name="mcdown-manifest", daemon=True).start()

    def _on_manifest_ok(self, manifest: VersionManifest) -> None:
        self._manifest_loading = False
        self.manifest = manifest
        self._refresh_version_values()
        if not self.busy:
            self.refresh_button.state(["!disabled"])
            self._stop_indeterminate()
            self._set_connection("online")
            self.status_var.set("版本列表已载入，共 %d 个版本可选。" % len(manifest.versions))
            self._reset_progress()
        # 首次加载时选中最新正式版；不覆盖用户已经输入的内容。
        if not self.version_var.get().strip():
            if manifest.latest_release:
                self.version_var.set(manifest.latest_release)
            elif manifest.versions:
                self.version_var.set(manifest.versions[0].id)
        self._update_version_meta(self.version_var.get().strip())
        self.log(
            "版本列表加载完成：共 %d 个版本，最新正式版 %s。"
            % (len(manifest.versions), manifest.latest_release or "未知")
        )
        self._update_download_state()

    def _on_manifest_error(self, message: str) -> None:
        self._manifest_loading = False
        if not self.busy:
            self.refresh_button.state(["!disabled"])
            self._stop_indeterminate()
            self._set_connection("offline")
            self._reset_progress()
            self.status_var.set("版本列表获取失败。可手动输入版本号后直接下载。")
            self.latest_meta_var.set("版本列表不可用")
        self.log("版本列表获取失败：%s" % message, error=True)
        self._update_version_meta(self.version_var.get().strip())
        self._update_download_state()

    def _on_manifest_cancelled(self) -> None:
        self._manifest_loading = False
        if not self.busy:
            self.refresh_button.state(["!disabled"])
            self._stop_indeterminate()
            self.status_var.set("已取消获取版本列表。")
        self.log("已取消获取版本列表。")

    # -- 下载 ------------------------------------------------------------

    def start_download(self) -> None:
        if self.busy:
            return
        try:
            version = validate_version_name(self.version_var.get())
            raw_path = self.path_var.get().strip()
            if not raw_path:
                raise MCDownError("请填写 .minecraft 文件夹。")
            root = Path(os.path.expandvars(raw_path)).expanduser()
            if root.exists() and not root.is_dir():
                raise MCDownError("所选 .minecraft 路径被同名文件占用，请换一个位置。")
        except MCDownError as exc:
            self.messagebox.showwarning("无法开始下载", str(exc), parent=self.root)
            return
        except (OSError, ValueError) as exc:
            self.messagebox.showwarning(".minecraft 文件夹无效", str(exc), parent=self.root)
            return

        info = self.manifest.find(version) if self.manifest else None
        if info is None and self.manifest is not None:
            self.log("版本 %s 不在当前列表中，将直接向 BMCLAPI 请求。" % version)

        self.version_var.set(version)
        self._stop_indeterminate()
        self._reset_progress()
        self._set_busy(True)
        self._last_stage = None
        self._last_message = None
        self.status_var.set("正在准备下载 Minecraft %s…" % version)
        self.percent_var.set("0%")
        self.cancel_event = threading.Event()
        cancel_event = self.cancel_event
        self.open_button.state(["disabled"])
        self.last_folder = None
        self._set_connection("working")
        self.log("开始下载 Minecraft %s。" % version)

        def worker() -> None:
            def on_progress(update: ProgressUpdate) -> None:
                self.events.put(("progress", update))

            try:
                result = download_game(
                    version,
                    root,
                    version_info=info,
                    progress=on_progress,
                    cancel_event=cancel_event,
                )
            except DownloadCancelled:
                self.events.put(("download_cancelled", None))
            except Exception as exc:
                self.events.put(("download_error", str(exc)))
            else:
                self.events.put(("download_ok", result))

        threading.Thread(target=worker, name="mcdown-download", daemon=True).start()

    def _on_progress(self, update: ProgressUpdate) -> None:
        if not self.busy:
            return
        stage = update.stage
        if update.message != self._last_message or stage != self._last_stage:
            self.log(update.message)
            self._last_message = update.message
            self._last_stage = stage

        if stage == "done":
            self._set_progress(1.0)
            self.status_var.set(update.message)
        elif stage == "verify":
            self._set_progress(0.975)
            self.status_var.set(update.message)
        elif stage == "json":
            fraction = (update.current / update.total) if update.total else 0.0
            self._set_progress(0.03 + 0.10 * min(1.0, fraction))
            self.status_var.set(self._progress_detail(update))
        elif stage == "jar":
            fraction = (update.current / update.total) if update.total else 0.0
            self._set_progress(0.13 + 0.84 * min(1.0, fraction))
            self.status_var.set(self._progress_detail(update))
        elif stage == "prepare":
            self._set_progress(0.02)
            self.status_var.set(update.message)
        else:
            self.status_var.set(self._progress_detail(update))

    def _progress_detail(self, update: ProgressUpdate) -> str:
        if update.total:
            return "%s  ·  %s / %s" % (
                update.message,
                format_bytes(update.current),
                format_bytes(update.total),
            )
        if update.current:
            return "%s  ·  %s" % (update.message, format_bytes(update.current))
        return update.message

    def _on_download_ok(self, result: DownloadResult) -> None:
        self._set_busy(False)
        self.cancel_event = None
        self._set_connection("online")
        self._set_progress(1.0)
        self.last_folder = result.game_root
        self.open_button.state(["!disabled"])
        self.status_var.set("下载完成，已保存到：%s" % result.folder)
        self._last_stage = None
        self._last_message = None
        saved = []
        if result.json_skipped:
            saved.append("JSON 已存在并跳过")
        if result.jar_skipped:
            saved.append("JAR 已存在并跳过")
        self.log(
            "完成：%s  ·  JAR %s，JSON %s  ·  用时 %.1f 秒%s"
            % (
                result.version,
                format_bytes(result.jar_size),
                format_bytes(result.json_size),
                result.elapsed,
                ("  ·  " + "，".join(saved)) if saved else "",
            )
        )
        self.log("文件位置：%s" % result.folder)

    def _on_download_error(self, message: str) -> None:
        self._set_busy(False)
        self.cancel_event = None
        self._set_connection("online" if self.manifest else "offline")
        self._reset_progress()
        self.status_var.set("下载失败：%s" % message)
        self.percent_var.set("失败")
        self._last_stage = None
        self._last_message = None
        self.log("下载失败：%s" % message, error=True)
        self.messagebox.showerror("下载失败", message, parent=self.root)

    def _on_download_cancelled(self) -> None:
        self._set_busy(False)
        self.cancel_event = None
        self._reset_progress()
        self.status_var.set("已取消下载。")
        self._last_stage = None
        self._last_message = None
        self.log("用户取消了下载。")

    def cancel_download(self) -> None:
        if not self.busy or self.cancel_event is None:
            return
        if not self.cancel_event.is_set():
            self.cancel_event.set()
            self.cancel_button.state(["disabled"])
            self.status_var.set("正在取消下载…")
            self.log("正在取消下载…")

    def open_download_folder(self) -> None:
        if self.last_folder is None:
            return
        folder = self.last_folder
        if not folder.exists():
            self.messagebox.showwarning("文件夹不存在", "下载文件夹已不存在：\n%s" % folder, parent=self.root)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(
                    ["xdg-open", str(folder)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError as exc:
            self.messagebox.showerror("无法打开文件夹", "%s\n%s" % (folder, exc), parent=self.root)

    # -- 事件循环与关闭 --------------------------------------------------

    def _drain_events(self) -> None:
        if not self.alive:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "manifest_ok":
                    self._on_manifest_ok(payload)
                elif kind == "manifest_error":
                    self._on_manifest_error(payload)
                elif kind == "manifest_cancelled":
                    self._on_manifest_cancelled()
                elif kind == "progress":
                    self._on_progress(payload)
                elif kind == "download_ok":
                    self._on_download_ok(payload)
                elif kind == "download_error":
                    self._on_download_error(payload)
                elif kind == "download_cancelled":
                    self._on_download_cancelled()
        except queue.Empty:
            pass
        finally:
            if self.alive:
                self.root.after(80, self._drain_events)

    def open_loader_dialog(self) -> None:
        if self.busy or not self.alive:
            return
        if self.loader_dialog is not None and self.loader_dialog.alive:
            self.loader_dialog.top.lift()
            return
        try:
            version = validate_version_name(self.version_var.get())
            raw_path = self.path_var.get().strip()
            if not raw_path:
                raise MCDownError("请先填写 .minecraft 文件夹。")
            expanded = Path(os.path.expandvars(raw_path)).expanduser()
            if not expanded.is_absolute():
                expanded = Path.cwd() / expanded
            game_root = Path(os.path.abspath(str(expanded)))
            if game_root.exists() and not game_root.is_dir():
                raise MCDownError("所选 .minecraft 路径被同名文件占用，请换一个位置。")
        except MCDownError as exc:
            self.messagebox.showwarning("无法安装加载器", str(exc), parent=self.root)
            return
        except (OSError, ValueError) as exc:
            self.messagebox.showwarning(".minecraft 文件夹无效", str(exc), parent=self.root)
            return
        info = self.manifest.find(version) if self.manifest else None
        self.loader_dialog = _LoaderDialog(
            self,
            version,
            game_root,
            info,
        )

    def _on_close(self) -> None:
        if self.loader_dialog is not None and self.loader_dialog.alive:
            self.messagebox.showinfo(
                "加载器安装窗口已打开",
                "请先关闭模组加载器安装窗口，再退出主程序。",
                parent=self.root,
            )
            return
        if self.busy:
            should_close = self.messagebox.askyesno(
                "正在下载",
                "下载尚未完成，确定要退出并取消吗？",
                parent=self.root,
            )
            if not should_close:
                return
            if self.cancel_event is not None:
                self.cancel_event.set()
        self.shutdown()

    def shutdown(self) -> None:
        self.alive = False
        try:
            self.root.destroy()
        except Exception:
            pass



class _LoaderDialog:
    """一键安装 Forge/Fabric/NeoForge/Quilt 的模态窗口。"""

    def __init__(
        self,
        app: "_MCDownApp",
        mc_version: str,
        game_root: Path,
        version_info: Optional[VersionInfo],
    ):
        self.app = app
        self.root = app.root
        self.tk = app.tk
        self.ttk = app.ttk
        self.filedialog = app.filedialog
        self.messagebox = app.messagebox
        self.mc_version = mc_version
        self.game_root = game_root
        self.version_info = version_info
        self.alive = True
        self.busy = False
        self.catalog_loading = False
        self.catalog: Optional[LoaderCatalog] = None
        self.catalog_generation = 0
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.cancel_event: Optional[threading.Event] = None
        self.java_info: Optional[JavaInfo] = None
        self._spinner = False
        self._close_after_cancel = False
        self._finished = False
        self._progress_message = ""
        self.last_game_dir: Optional[Path] = None
        self.last_profiles: Tuple[Path, ...] = ()
        self.last_installer_log: Optional[Path] = None
        self.initial_loader = app.last_loader if app.last_loader in LOADER_LABELS else "fabric"

        self.window = self.tk.Toplevel(self.root)
        self.window.title("一键安装模组加载器")
        self.window.configure(bg=app.BG)
        self.window.transient(self.root)
        self.window.protocol("WM_DELETE_WINDOW", self._on_wm_close)
        self._build_ui()
        self._center_window()
        try:
            self.window.grab_set()
        except self.tk.TclError:
            pass
        self.window.after(80, self._drain_events)
        self.window.after(80, self._detect_java)
        self.window.after(150, self._request_catalog)

    # -- 界面 -----------------------------------------------------------

    def _card(self, parent: Any, pady: Tuple[int, int] = (0, 10)) -> Tuple[Any, Any]:
        card = self.tk.Frame(
            parent,
            bg=self.app.CARD,
            highlightbackground=self.app.BORDER,
            highlightthickness=1,
        )
        card.pack(fill="x", pady=pady)
        inner = self.tk.Frame(card, bg=self.app.CARD)
        inner.pack(fill="both", expand=True, padx=16, pady=12)
        return card, inner

    def _build_ui(self) -> None:
        app = self.app
        self.window.title("一键安装模组加载器 · Minecraft %s" % self.mc_version)

        outer = self.tk.Frame(self.window, bg=app.BG)
        outer.pack(fill="both", expand=True, padx=22, pady=16)

        header = self.tk.Frame(outer, bg=app.BG)
        header.pack(fill="x", pady=(0, 10))
        self.tk.Label(
            header,
            text="一键安装模组加载器",
            bg=app.BG,
            fg=app.TEXT,
            font=(app.ui_family, 18, "bold"),
            anchor="w",
        ).pack(fill="x")
        self.tk.Label(
            header,
            text="Forge / Fabric / NeoForge / Quilt  ·  生成独立加载器版本，不额外保留临时原版",
            bg=app.BG,
            fg=app.MUTED,
            font=app.subtitle_font,
            anchor="w",
        ).pack(fill="x", pady=(3, 0))

        _summary_card, summary = self._card(outer, (0, 10))
        summary_line = self.tk.Frame(summary, bg=app.CARD)
        summary_line.pack(fill="x")
        self.tk.Label(
            summary_line,
            text="Minecraft %s" % self.mc_version,
            bg=app.CARD,
            fg=app.TEXT,
            font=(app.ui_family, 12, "bold"),
            anchor="w",
        ).pack(side="left")
        self.tk.Label(
            summary_line,
            text="  将生成独立加载器版本",
            bg=app.CARD,
            fg=app.FAINT,
            font=app.small_font,
            anchor="w",
        ).pack(side="left", padx=(4, 0))
        self.tk.Label(
            summary,
            text="游戏根目录（.minecraft）：%s" % self.game_root,
            bg=app.CARD,
            fg=app.MUTED,
            font=(app.mono_family, 9),
            anchor="w",
            justify="left",
            wraplength=680,
        ).pack(fill="x", pady=(5, 0))

        _config_card, config = self._card(outer, (0, 10))
        config.grid_columnconfigure(3, weight=1)

        def config_label(text: str, row: int, column: int) -> Any:
            widget = self.tk.Label(
                config,
                text=text,
                bg=app.CARD,
                fg=app.MUTED,
                font=app.body_font,
                anchor="w",
            )
            widget.grid(row=row, column=column, sticky="w", padx=(0, 8), pady=(0, 7))
            return widget

        config_label("加载器", 0, 0)
        self.loader_var = self.tk.StringVar(value=LOADER_LABELS[self.initial_loader])
        self.loader_combo = self.ttk.Combobox(
            config,
            textvariable=self.loader_var,
            values=[LOADER_LABELS[key] for key in LOADER_ORDER],
            state="readonly",
            font=app.body_font,
            width=12,
        )
        self.loader_combo.grid(row=0, column=1, sticky="w", padx=(0, 18), pady=(0, 7))
        self.loader_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_loader_changed())
        config_label("加载器版本", 0, 2)
        self.loader_version_var = self.tk.StringVar()
        self.loader_version_combo = self.ttk.Combobox(
            config,
            textvariable=self.loader_version_var,
            state="disabled",
            font=app.body_font,
            width=26,
        )
        self.loader_version_combo.grid(row=0, column=3, sticky="ew", pady=(0, 7))
        self.loader_version_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_version_changed()
        )
        self.refresh_versions_button = self.ttk.Button(
            config,
            text="刷新",
            style="Small.TButton",
            command=self._refresh_catalog,
        )
        self.refresh_versions_button.grid(row=0, column=4, sticky="e", padx=(6, 0), pady=(0, 7))
        self.refresh_versions_button.state(["disabled"])

        self.loader_meta_var = self.tk.StringVar(value="正在获取加载器版本…")
        self.tk.Label(
            config,
            textvariable=self.loader_meta_var,
            bg=app.CARD,
            fg=app.FAINT,
            font=app.small_font,
            anchor="w",
            justify="left",
            wraplength=640,
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(0, 9))

        config_label("Java", 2, 0)
        self.java_var = self.tk.StringVar()
        self.java_entry = self.ttk.Entry(
            config,
            textvariable=self.java_var,
            style="TEntry",
            font=(app.mono_family, 9),
        )
        self.java_entry.grid(row=2, column=1, columnspan=3, sticky="ew", pady=(0, 7))
        self.java_entry.bind("<Return>", lambda _event: self._validate_java_entry(True))
        self.java_entry.bind("<FocusOut>", lambda _event: self._validate_java_entry(False))
        self.java_browse_button = self.ttk.Button(
            config,
            text="浏览…",
            style="Secondary.TButton",
            command=self._browse_java,
        )
        self.java_browse_button.grid(row=2, column=4, sticky="e", padx=(6, 0), pady=(0, 7))
        self.java_status_var = self.tk.StringVar(value="正在检测 Java…")
        self.java_status_label = self.tk.Label(
            config,
            textvariable=self.java_status_var,
            bg=app.CARD,
            fg=app.FAINT,
            font=app.small_font,
            anchor="w",
            justify="left",
            wraplength=640,
        )
        self.java_status_label.grid(row=3, column=0, columnspan=5, sticky="w", pady=(0, 7))

        self.tk.Label(
            config,
            text="会生成共享 assets/libraries 和版本隔离目录 versions/<实例>/，并写入 PCL/HMCL 的隔离设置；不包含完整 assets，可由启动器补齐。加载器元数据与安装包优先走 BMCLAPI。",
            bg=app.CARD,
            fg=app.FAINT,
            font=app.small_font,
            anchor="w",
            justify="left",
            wraplength=640,
        ).grid(row=4, column=0, columnspan=5, sticky="w")
        self._update_java_controls()

        progress_frame = self.tk.Frame(outer, bg=app.BG)
        progress_frame.pack(fill="x", pady=(1, 9))
        progress_line = self.tk.Frame(progress_frame, bg=app.BG)
        progress_line.pack(fill="x")
        self.status_var = self.tk.StringVar(value="正在准备安装器…")
        self.status_label = self.tk.Label(
            progress_line,
            textvariable=self.status_var,
            bg=app.BG,
            fg=app.MUTED,
            font=app.body_font,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.percent_var = self.tk.StringVar(value="0%")
        self.percent_label = self.tk.Label(
            progress_line,
            textvariable=self.percent_var,
            bg=app.BG,
            fg=app.ACCENT,
            font=(app.ui_family, 10, "bold"),
        )
        self.percent_label.pack(side="right", padx=(10, 0))
        self.progress = self.ttk.Progressbar(
            progress_frame,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            value=0,
            style="Green.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(7, 0))

        log_card = self.tk.Frame(
            outer,
            bg=app.CARD,
            highlightbackground=app.BORDER,
            highlightthickness=1,
        )
        log_card.pack(fill="both", expand=True, pady=(0, 10))
        log_inner = self.tk.Frame(log_card, bg=app.CARD)
        log_inner.pack(fill="both", expand=True, padx=16, pady=10)
        log_head = self.tk.Frame(log_inner, bg=app.CARD)
        log_head.pack(fill="x")
        self.tk.Label(
            log_head,
            text="安装日志",
            bg=app.CARD,
            fg=app.MUTED,
            font=app.small_font,
            anchor="w",
        ).pack(side="left")
        self.ttk.Button(
            log_head,
            text="清空",
            style="Small.TButton",
            command=self._clear_log,
        ).pack(side="right")
        log_body = self.tk.Frame(log_inner, bg=app.CARD)
        log_body.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = self.tk.Text(
            log_body,
            height=4,
            bg="#FBFCFE",
            fg=app.MUTED,
            insertbackground=app.TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=app.BORDER,
            highlightcolor=app.BORDER,
            font=(app.ui_family, 9),
            padx=9,
            pady=6,
            wrap="word",
            state="disabled",
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar = self.ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y", padx=(4, 0))
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.tag_configure("time", foreground=app.FAINT)
        self.log_text.tag_configure("error", foreground=app.RED)

        buttons = self.tk.Frame(outer, bg=app.BG)
        buttons.pack(fill="x")
        self.open_dir_button = self.ttk.Button(
            buttons,
            text="打开 .minecraft 根目录",
            style="Link.TButton",
            command=self._open_game_dir,
        )
        self.open_dir_button.pack(side="left")
        self.open_dir_button.state(["disabled"])
        self.install_button = self.ttk.Button(
            buttons,
            text="一键安装",
            style="Accent.TButton",
            command=self._start_install,
        )
        self.install_button.pack(side="right")
        self.install_button.state(["disabled"])
        self.close_button = self.ttk.Button(
            buttons,
            text="关闭",
            style="Secondary.TButton",
            command=self._close_button_command,
        )
        self.close_button.pack(side="right", padx=(0, 8))

        self._log("选择加载器后点击「一键安装」。")
        self._log("安装器与依赖优先从 BMCLAPI 获取，缺失时会自动回退到官方源。")

    def _center_window(self) -> None:
        self.window.update_idletasks()
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        width = min(760, screen_w - 80)
        height = min(700, screen_h - 90)
        width = max(720, width)
        height = max(640, height)
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2)
        self.window.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self.window.minsize(720, 680)

    # -- 日志与进度 -----------------------------------------------------

    def _clear_log(self) -> None:
        if not self.alive:
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log(self, message: str, error: bool = False) -> None:
        if not self.alive:
            return
        self.log_text.configure(state="normal")
        if self.log_text.index("end-1c") != "1.0":
            self.log_text.insert("end", "\n")
        self.log_text.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        self.log_text.insert("end", message, "error" if error else ())
        # 限制日志长度，避免安装器运行过久时占用过多内存。
        try:
            line_count = int(self.log_text.index("end-1c").split(".")[0])
        except (ValueError, AttributeError):
            line_count = 0
        if line_count > 300:
            self.log_text.delete("1.0", "%d.0" % (line_count - 250))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _set_status(self, message: str, color: Optional[str] = None) -> None:
        self.status_var.set(message)
        if color:
            self.status_label.configure(fg=color)

    def _start_spinner(self, message: Optional[str] = None) -> None:
        if message:
            self._set_status(message)
        self._spinner = True
        self.progress.configure(mode="indeterminate")
        try:
            self.progress.start(14)
        except Exception:
            pass
        self.percent_var.set("…")

    def _stop_spinner(self) -> None:
        if not self._spinner:
            return
        self._spinner = False
        try:
            self.progress.stop()
        except Exception:
            pass
        self.progress.configure(mode="determinate")

    def _reset_progress(self) -> None:
        self._stop_spinner()
        self.progress.configure(mode="determinate", value=0)
        self.percent_var.set("0%")
        self.status_label.configure(fg=self.app.MUTED)

    def _set_progress(self, fraction: Optional[float], status: Optional[str] = None) -> None:
        if status is not None:
            self._set_status(status)
        if fraction is None:
            self._start_spinner()
            return
        self._stop_spinner()
        fraction = max(0.0, min(1.0, fraction))
        self.progress.configure(mode="determinate", value=fraction * 100.0)
        self.percent_var.set("%d%%" % int(round(fraction * 100.0)))

    # -- 加载器版本 -----------------------------------------------------

    def _current_loader(self) -> str:
        selected = self.loader_var.get()
        for key, label in LOADER_LABELS.items():
            if label == selected:
                return key
        return self.initial_loader

    def _loader_requires_java(self) -> bool:
        return self._current_loader() not in DIRECT_PROFILE_LOADERS

    def _restore_controls(self) -> None:
        if not self.alive:
            return
        if self.busy or self.catalog_loading or self._finished:
            self.loader_combo.configure(state="disabled")
            self.loader_version_combo.configure(state="disabled")
            self.refresh_versions_button.state(["disabled"])
        else:
            self.loader_combo.configure(state="readonly")
            has_versions = bool(self.catalog and self.catalog.versions)
            self.loader_version_combo.configure(state="readonly" if has_versions else "disabled")
            self.refresh_versions_button.state(["!disabled"])
        self._update_java_controls()
        self._update_install_state()

    def _update_java_controls(self) -> None:
        if not hasattr(self, "java_entry"):
            return
        app = self.app
        direct = not self._loader_requires_java()
        if direct or self.busy or self._finished:
            self.java_entry.state(["disabled"])
            self.java_browse_button.state(["disabled"])
        else:
            self.java_entry.state(["!disabled"])
            self.java_browse_button.state(["!disabled"])
        if direct:
            self.java_status_var.set("Fabric / Quilt 安装无需 Java（仍需要 Java 来启动游戏）")
            self.java_status_label.configure(fg=app.FAINT)
        elif self.java_info is not None:
            self.java_status_var.set("已找到 Java：%s" % self.java_info.version)
            self.java_status_label.configure(fg=app.ACCENT_DARK)
        elif self.java_var.get().strip():
            self.java_status_var.set("Java 路径尚未验证，请按回车或点击「浏览…」")
            self.java_status_label.configure(fg=app.AMBER)
        else:
            self.java_status_var.set("未找到 Java，请选择 java / java.exe")
            self.java_status_label.configure(fg=app.RED)

    def _update_install_state(self) -> None:
        if not hasattr(self, "install_button") or not self.alive:
            return
        can_install = (
            not self.busy
            and not self.catalog_loading
            and not self._finished
            and self.catalog is not None
            and bool(self.catalog.versions)
            and bool(self.loader_version_var.get().strip())
        )
        if self._loader_requires_java() and self.java_info is None:
            can_install = False
        if can_install:
            self.install_button.state(["!disabled"])
        else:
            self.install_button.state(["disabled"])

    def _request_catalog(self, force: bool = False) -> None:
        if not self.alive or self.busy or self.catalog_loading:
            return
        loader = self._current_loader()
        self.app.last_loader = loader
        key = (loader, self.mc_version)
        self.catalog_generation += 1
        generation = self.catalog_generation
        if not force and key in self.app.loader_catalog_cache:
            cached = self.app.loader_catalog_cache[key]
            self.catalog_loading = False
            self._apply_catalog(loader, cached, generation)
            return
        self.catalog_loading = True
        self.catalog = None
        self.loader_version_var.set("")
        self.loader_meta_var.set("正在获取 %s 版本列表…" % loader_label(loader))
        self._set_status("正在从 BMCLAPI 获取 %s 版本列表…" % loader_label(loader))
        self._start_spinner()
        self.refresh_versions_button.state(["disabled"])
        self._restore_controls()

        def worker() -> None:
            try:
                catalog = fetch_loader_catalog(loader, self.mc_version)
            except Exception as exc:
                self.events.put(("catalog_error", (generation, loader, str(exc))))
            else:
                self.events.put(("catalog_ok", (generation, loader, catalog)))

        threading.Thread(target=worker, name="mcdown-loader-catalog", daemon=True).start()

    def _refresh_catalog(self) -> None:
        if self.busy or self.catalog_loading:
            return
        self._request_catalog(force=True)

    def _on_loader_changed(self) -> None:
        if self.busy:
            return
        loader = self._current_loader()
        self.app.last_loader = loader
        self._update_java_controls()
        self._log("已选择加载器：%s" % loader_label(loader))
        self._request_catalog()

    def _apply_catalog(self, loader: str, catalog: LoaderCatalog, generation: int) -> None:
        if not self.alive or generation != self.catalog_generation or self.busy:
            return
        self.catalog_loading = False
        self.catalog = catalog
        self.app.loader_catalog_cache[(loader, self.mc_version)] = catalog
        self._stop_spinner()
        self._reset_progress()
        if not catalog.versions:
            self.loader_version_combo.configure(values=[], state="disabled")
            self.loader_version_var.set("")
            self.loader_meta_var.set(
                "Minecraft %s 没有可用的 %s 版本。"
                % (self.mc_version, loader_label(loader))
            )
            self._set_status(
                "没有找到适用于 Minecraft %s 的 %s 版本。" % (self.mc_version, loader_label(loader)),
                self.app.RED,
            )
            self._log("没有找到 %s 的兼容版本。" % loader_label(loader), error=True)
            self._restore_controls()
            return

        values = [entry.version for entry in catalog.versions]
        self.loader_version_combo.configure(values=values, state="readonly")
        selected = catalog.recommended or values[0]
        self.loader_version_var.set(selected)
        self.loader_meta_var.set(
            "推荐：%s  ·  共 %d 个版本  ·  可手动选择其他版本"
            % (selected, len(values))
        )
        self._log(
            "%s 版本列表已加载：共 %d 个版本，推荐 %s。"
            % (loader_label(loader), len(values), selected)
        )
        self._update_version_meta()
        self._set_status("版本列表已就绪，点击「一键安装」开始。", self.app.MUTED)
        self._restore_controls()
        try:
            self.loader_version_combo.focus_set()
        except self.tk.TclError:
            pass

    def _on_catalog_ok(self, payload: Tuple[int, str, LoaderCatalog]) -> None:
        generation, loader, catalog = payload
        if not self.alive or generation != self.catalog_generation:
            return
        self._apply_catalog(loader, catalog, generation)

    def _on_catalog_error(self, payload: Tuple[int, str, str]) -> None:
        generation, loader, message = payload
        if not self.alive or generation != self.catalog_generation:
            return
        self.catalog_loading = False
        self.catalog = None
        self._stop_spinner()
        self._reset_progress()
        self.loader_version_combo.configure(values=[], state="disabled")
        self.loader_version_var.set("")
        self.loader_meta_var.set("版本列表获取失败")
        self._set_status("获取 %s 版本列表失败。" % loader_label(loader), self.app.RED)
        self._log("获取 %s 版本列表失败：%s" % (loader_label(loader), message), error=True)
        self._restore_controls()

    def _selected_entry(self) -> Optional[LoaderVersion]:
        if not self.catalog:
            return None
        wanted = self.loader_version_var.get().strip()
        for entry in self.catalog.versions:
            if entry.version == wanted:
                return entry
        return None

    def _on_version_changed(self) -> None:
        self._update_version_meta()
        self._update_install_state()

    def _update_version_meta(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        parts = ["稳定版" if entry.stable else "测试版"]
        if self.catalog and self.catalog.recommended == entry.version:
            parts.append("推荐版本")
        parts.append("共 %d 个版本" % (len(self.catalog.versions) if self.catalog else 0))
        self.loader_meta_var.set("  ·  ".join(parts))

    # -- Java -----------------------------------------------------------

    def _detect_java(self) -> None:
        if not self.alive:
            return
        self.java_status_var.set("正在检测 Java…")
        info = find_java_info()
        self.java_info = info
        if info is not None:
            self.java_var.set(info.path)
            self._log("检测到 Java：%s（%s）" % (info.version, info.path))
        else:
            self.java_var.set("")
            self._log("未在 PATH/JAVA_HOME 中找到 Java；Forge/NeoForge 安装前需要选择 Java。", error=False)
        self._update_java_controls()
        self._update_install_state()

    def _validate_java_entry(self, notify: bool) -> None:
        if not self.alive or self.busy:
            return
        raw = self.java_var.get().strip()
        if not raw:
            self.java_info = None
        else:
            info = find_java_info(raw)
            if info is not None:
                self.java_info = info
                if info.path != raw:
                    self.java_var.set(info.path)
            else:
                self.java_info = None
                if notify:
                    self.messagebox.showwarning(
                        "Java 不可用",
                        "无法运行该 Java：\n%s\n请选择有效的 java / java.exe 文件。" % raw,
                        parent=self.window,
                    )
        self._update_java_controls()
        self._update_install_state()

    def _browse_java(self) -> None:
        if self.busy:
            return
        initial = self.java_var.get().strip()
        initial_dir = str(Path(initial).parent) if initial and Path(initial).parent.exists() else str(Path.home())
        selected = self.filedialog.askopenfilename(
            parent=self.window,
            title="选择 Java 可执行文件",
            initialdir=initial_dir,
            filetypes=[
                ("Java executable", "java.exe" if os.name == "nt" else "java"),
                ("All files", "*"),
            ],
        )
        if selected:
            self.java_var.set(selected)
            self._validate_java_entry(True)

    # -- 安装 -----------------------------------------------------------

    def _start_install(self) -> None:
        if not self.alive or self.busy:
            return
        loader = self._current_loader()
        entry = self._selected_entry()
        if entry is None:
            self.messagebox.showwarning("请选择版本", "请先选择加载器版本。", parent=self.window)
            return
        java_path: Optional[str] = None
        if self._loader_requires_java():
            if not self.java_var.get().strip():
                self.messagebox.showwarning(
                    "需要 Java",
                    "安装 %s 需要 Java。请先安装 Java 17+，或点击「浏览…」选择 java 可执行文件。"
                    % loader_label(loader),
                    parent=self.window,
                )
                return
            info = find_java_info(self.java_var.get().strip())
            if info is None:
                self.messagebox.showwarning(
                    "Java 不可用",
                    "无法运行指定的 Java。请重新选择 Java。",
                    parent=self.window,
                )
                return
            self.java_info = info
            self.java_var.set(info.path)
            java_path = info.path
        if self.loader_version_var.get().strip() == "":
            self.messagebox.showwarning("请选择版本", "请先选择加载器版本。", parent=self.window)
            return

        self.busy = True
        self._finished = False
        self._close_after_cancel = False
        self.cancel_event = threading.Event()
        self.install_button.configure(text="安装中…")
        self.install_button.state(["disabled"])
        self.close_button.configure(text="取消安装")
        self.open_dir_button.state(["disabled"])
        self._progress_message = ""
        self._clear_log()
        self._reset_progress()
        self._set_status("开始准备下载原版与加载器…")
        self.percent_var.set("0%")
        self._log(
            "开始安装 Minecraft %s + %s %s…"
            % (self.mc_version, loader_label(loader), entry.version)
        )
        self.app.log(
            "开始安装加载器：Minecraft %s + %s %s"
            % (self.mc_version, loader_label(loader), entry.version)
        )
        self._restore_controls()
        cancel_event = self.cancel_event

        def worker() -> None:
            def on_progress(update: ProgressUpdate) -> None:
                self.events.put(("progress", update))

            def on_log(message: str) -> None:
                self.events.put(("log", message))

            try:
                result = install_loader(
                    loader,
                    self.mc_version,
                    loader_version=entry.version,
                    output_root=self.game_root,
                    version_info=self.version_info,
                    java_path=java_path,
                    progress=on_progress,
                    cancel_event=cancel_event,
                    log_callback=on_log,
                    loader_entry=entry,
                )
            except DownloadCancelled:
                self.events.put(("install_cancelled", None))
            except Exception as exc:
                self.events.put(("install_error", str(exc)))
            else:
                self.events.put(("install_ok", result))

        threading.Thread(target=worker, name="mcdown-loader-install", daemon=True).start()

    def _progress_fraction(self, update: ProgressUpdate) -> Optional[float]:
        stage = update.stage
        fraction = (update.current / update.total) if update.total else 0.0
        fraction = max(0.0, min(1.0, fraction))
        if stage == "loader_core_prepare":
            return 0.01
        if stage == "loader_core_json":
            return 0.02 + 0.08 * fraction
        if stage == "loader_core_jar":
            return 0.10 + 0.30 * fraction
        if stage == "loader_core_verify":
            return 0.41
        if stage == "loader_prepare":
            return 0.44
        if stage == "loader_profile":
            return 0.46 + 0.02 * fraction
        if stage == "loader_meta":
            return 0.44
        if stage == "loader_installer":
            return 0.46 + 0.10 * fraction
        if stage == "loader_installer_verify":
            return 0.57
        if stage == "loader_installer_run":
            return None
        if stage == "loader_library":
            return 0.47 + 0.35 * fraction
        if stage == "loader_config":
            return 0.92
        if stage == "loader_done":
            return 1.0
        return 0.45

    def _format_progress_status(self, update: ProgressUpdate) -> str:
        message = update.message
        if (
            update.total
            and update.current > 0
            and update.stage in ("loader_core_json", "loader_core_jar", "loader_installer")
        ):
            return "%s  ·  %s / %s" % (
                message,
                format_bytes(update.current),
                format_bytes(update.total),
            )
        return message

    def _on_progress(self, update: ProgressUpdate) -> None:
        if not self.busy or not self.alive:
            return
        message = self._format_progress_status(update)
        # 日志只记录阶段变化，不记录每个下载字节，避免日志被进度刷屏。
        if update.stage != "loader_installer_run" and update.message != self._progress_message:
            self._progress_message = update.message
            self._log(update.message)
        if update.stage == "loader_installer_run":
            if not self._spinner:
                self._start_spinner(message)
            else:
                self._set_status(message)
            return
        fraction = self._progress_fraction(update)
        if fraction is None:
            self._start_spinner(message)
        else:
            if self._spinner:
                self._stop_spinner()
                self.progress.configure(mode="determinate")
            self._set_progress(fraction, message)

    def _on_install_ok(self, result: LoaderInstallResult) -> None:
        if not self.alive:
            return
        self.busy = False
        self.cancel_event = None
        self._finished = True
        self._stop_spinner()
        self.progress.configure(mode="determinate", value=100)
        self.percent_var.set("100%")
        self._set_status("安装完成，启动配置已写入。", self.app.ACCENT)
        self.close_button.configure(text="关闭")
        self.close_button.state(["!disabled"])
        self.install_button.configure(text="安装完成")
        self.install_button.state(["disabled"])
        self.last_game_dir = result.game_dir
        self.last_profiles = result.profile_paths
        self.last_installer_log = result.installer_log
        self.open_dir_button.state(["!disabled"])
        if hasattr(self.app, "open_button"):
            self.app.last_folder = result.game_dir
            self.app.open_button.state(["!disabled"])
        self.loader_combo.configure(state="disabled")
        self.loader_version_combo.configure(state="disabled")
        self.refresh_versions_button.state(["disabled"])
        self.java_entry.state(["disabled"])
        self.java_browse_button.state(["disabled"])
        self.app.log(
            "加载器安装完成：Minecraft %s + %s %s（%s）"
            % (
                result.mc_version,
                loader_label(result.loader),
                result.loader_version,
                result.game_dir,
            )
        )
        self._log("安装完成：%s %s" % (loader_label(result.loader), result.loader_version))
        self._log("游戏根目录（.minecraft）：%s" % result.game_dir)
        if (result.game_dir / "versions" / result.mc_version).is_dir():
            self._log("安装前已有原版版本，已保留：%s" % (result.game_dir / "versions" / result.mc_version))
        else:
            self._log("未额外保留原版版本，只生成独立加载器版本。")
        for instance_dir in result.instance_dirs:
            self._log("版本隔离目录：%s" % instance_dir)
        for profile in result.profile_paths:
            self._log("启动配置：%s" % profile)
        for metadata in result.isolation_files:
            self._log("隔离设置：%s" % metadata)
        if result.installer_log:
            self._log("安装器日志：%s" % result.installer_log)
        self._log("用时 %.1f 秒。请把上面的游戏根目录交给 PCL / HMCL，并选择加载器版本。" % result.elapsed)
        self.close_button.focus_set()

    def _on_install_error(self, message: str) -> None:
        if not self.alive:
            return
        self.busy = False
        self.cancel_event = None
        self._stop_spinner()
        self.progress.configure(mode="determinate", value=0)
        self.percent_var.set("失败")
        self._set_status("安装失败：%s" % message, self.app.RED)
        self.close_button.configure(text="关闭")
        self.install_button.configure(text="一键安装")
        self._log("安装失败：%s" % message, error=True)
        self._restore_controls()
        self.app.log("加载器安装失败：%s" % message, error=True)
        self.messagebox.showerror("加载器安装失败", message, parent=self.window)

    def _on_install_cancelled(self) -> None:
        if not self.alive:
            return
        self.busy = False
        self.cancel_event = None
        self._stop_spinner()
        self.progress.configure(mode="determinate", value=0)
        self.percent_var.set("已取消")
        self.close_button.configure(text="关闭")
        self.close_button.state(["!disabled"])
        self._set_status("已取消安装。")
        self._log("已取消安装。")
        self.install_button.configure(text="一键安装")
        self.app.log("已取消加载器安装。")
        if self._close_after_cancel:
            self._destroy()
        else:
            self._restore_controls()

    def _request_cancel(self, close_after: bool) -> None:
        if not self.busy or self.cancel_event is None:
            if close_after:
                self._destroy()
            return
        self._close_after_cancel = close_after
        self.cancel_event.set()
        self.close_button.state(["disabled"])
        self._set_status("正在取消安装…")
        self._log("正在取消安装…")

    def _close_button_command(self) -> None:
        if self.busy:
            self._request_cancel(True)
        else:
            self._destroy()

    def _on_wm_close(self) -> None:
        if self.busy:
            if not self.messagebox.askyesno(
                "正在安装",
                "安装尚未完成，确定要取消安装并退出吗？",
                parent=self.window,
            ):
                return
            self._request_cancel(True)
        else:
            self._destroy()

    def _open_game_dir(self) -> None:
        path = self.last_game_dir or self.game_root
        if not path.exists():
            self.messagebox.showwarning("目录不存在", "目录不存在：\n%s" % path, parent=self.window)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(
                    ["xdg-open", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError as exc:
            self.messagebox.showerror("无法打开目录", "%s\n%s" % (path, exc), parent=self.window)

    # -- 事件循环与销毁 -------------------------------------------------

    def _drain_events(self) -> None:
        if not self.alive:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "catalog_ok":
                    self._on_catalog_ok(payload)
                elif kind == "catalog_error":
                    self._on_catalog_error(payload)
                elif kind == "progress":
                    self._on_progress(payload)
                elif kind == "log":
                    self._log(str(payload))
                elif kind == "install_ok":
                    self._on_install_ok(payload)
                elif kind == "install_error":
                    self._on_install_error(str(payload))
                elif kind == "install_cancelled":
                    self._on_install_cancelled()
        except queue.Empty:
            pass
        finally:
            if self.alive:
                self.window.after(80, self._drain_events)

    def _destroy(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.window.grab_release()
        except self.tk.TclError:
            pass
        if self.app.loader_dialog is self:
            self.app.loader_dialog = None
        try:
            self.window.destroy()
        except self.tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 命令行界面
# ---------------------------------------------------------------------------


def _print_version_list(manifest: VersionManifest, type_filter: str) -> None:
    print("BMCLAPI 版本列表")
    print("  最新正式版：%s" % (manifest.latest_release or "未知"))
    print("  最新快照：  %s" % (manifest.latest_snapshot or "未知"))
    print("  版本总数：  %d" % len(manifest.versions))
    print()

    items = [
        item
        for item in manifest.versions
        if type_filter == "all" or item.type == type_filter
    ]
    if not items:
        print("没有符合筛选条件的版本。")
        return
    print("%-8s  %-30s  %s" % ("类型", "版本号", "发布时间"))
    print("-" * 68)
    for item in items:
        date = item.release_time[:10] if item.release_time else "未知"
        print("%-8s  %-30s  %s" % (version_type_name(item.type), item.id, date))


class _ConsoleProgress:
    def __init__(self) -> None:
        self.interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self.last_percent = -1
        self.last_message = ""

    def __call__(self, update: ProgressUpdate) -> None:
        if update.stage == "done":
            self._finish_line()
            return
        fraction = (update.current / update.total) if update.total else 0.0
        percent = int(fraction * 100.0)
        if update.stage == "verify":
            self._finish_line()
            print("  正在校验 SHA-1…")
            self.last_message = update.message
            return
        if update.message != self.last_message:
            if self.interactive:
                self._finish_line()
                if not update.total:
                    print("  " + update.message)
            else:
                print(update.message)
            self.last_message = update.message
            self.last_percent = -1
        if self.interactive and update.total and percent != self.last_percent:
            sys.stdout.write(
                "\r  %s  %3d%%  (%s / %s)"
                % (
                    update.message,
                    percent,
                    format_bytes(update.current),
                    format_bytes(update.total),
                )
            )
            sys.stdout.flush()
            self.last_percent = percent

    def _finish_line(self) -> None:
        if self.interactive and self.last_percent >= 0:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.last_percent = -1


class _LoaderConsoleProgress(_ConsoleProgress):
    """命令行加载器安装用：安装器过程只提示一次，完整日志写入文件。"""

    def __init__(self) -> None:
        super().__init__()
        self.installer_announced = False

    def __call__(self, update: ProgressUpdate) -> None:
        if update.stage == "loader_installer_run":
            if not self.installer_announced:
                self._finish_line()
                print("  安装器正在运行，完整日志会保存到本地…")
                self.installer_announced = True
            return
        super().__call__(update)


def _print_loader_versions(catalog: LoaderCatalog, limit: int = 12) -> None:
    print("  %s 可用版本：%d 个" % (loader_label(catalog.loader), len(catalog.versions)))
    if catalog.recommended:
        print("  推荐版本：%s" % catalog.recommended)
    if catalog.versions:
        shown = [entry.version for entry in catalog.versions[:limit]]
        print("  最新版本：%s%s" % (", ".join(shown), " …" if len(catalog.versions) > limit else ""))


def _choose_latest(manifest: VersionManifest, channel: str) -> Optional[VersionInfo]:
    version_id = manifest.latest_release if channel == "release" else manifest.latest_snapshot
    if version_id:
        found = manifest.find(version_id)
        if found is not None:
            return found
    wanted_type = "release" if channel == "release" else "snapshot"
    for item in manifest.versions:
        if item.type == wanted_type:
            return item
    return None


def cli_main(args: argparse.Namespace) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    except (AttributeError, ValueError):
        pass
    print("%s %s" % (APP_NAME, APP_VERSION))
    print("下载源：BMCLAPI  ·  保存结构：<位置>/<版本>/<版本>.jar + <版本>.json")
    print()

    root = args.output.strip() if args.output else str(default_download_root())
    try:
        manifest: Optional[VersionManifest] = None
        if args.list is not None:
            manifest = fetch_manifest(progress=_ConsoleProgress())
            _print_version_list(manifest, args.list)
            return 0

        target_info: Optional[VersionInfo] = None
        version = args.version.strip() if args.version else ""

        if args.latest_release or args.latest_snapshot:
            channel = "snapshot" if args.latest_snapshot else "release"
            manifest = fetch_manifest(progress=_ConsoleProgress())
            target_info = _choose_latest(manifest, channel)
            if target_info is None:
                print("错误：无法找到最新的%s版本。" % version_type_name(channel))
                return 1
            version = target_info.id
        elif not version:
            # 不带版本参数时，默认下载最新正式版。
            manifest = fetch_manifest(progress=_ConsoleProgress())
            target_info = _choose_latest(manifest, "release")
            if target_info is None:
                print("错误：无法从版本列表中找到正式版。")
                return 1
            version = target_info.id

        if not version:
            print("错误：没有可下载的版本，请指定 --version 或最新版本参数。")
            return 1

        version = validate_version_name(version)

        if args.loader:
            loader = _validate_loader(args.loader)
            print("准备为 Minecraft %s 安装 %s" % (version, loader_label(loader)))
            print(".minecraft 文件夹：%s" % root)
            print()
            progress = _LoaderConsoleProgress()
            catalog = fetch_loader_catalog(loader, version, progress=progress)
            _print_loader_versions(catalog)
            entry = choose_loader_version(catalog, args.loader_version)
            if entry is None:
                print()
                print(
                    "错误：Minecraft %s 没有适用于 %s 的版本。"
                    % (version, loader_label(loader)),
                    file=sys.stderr,
                )
                return 1
            print()
            print("选择加载器版本：%s" % entry.version)
            print()
            progress.installer_announced = False

            def loader_log(message: str) -> None:
                print("  加载器：%s" % message)

            try:
                result = install_loader(
                    loader,
                    version,
                    loader_version=entry.version,
                    output_root=root,
                    version_info=target_info,
                    java_path=args.java,
                    progress=progress,
                    log_callback=loader_log,
                    loader_entry=entry,
                )
            except DownloadCancelled:
                raise
            progress._finish_line()
            print()
            print("安装完成")
            print("  加载器：%s %s" % (loader_label(result.loader), result.loader_version))
            print("  Minecraft：%s" % result.mc_version)
            print("  游戏根目录（.minecraft）：%s" % result.game_dir)
            if (result.game_dir / "versions" / result.mc_version).is_dir():
                print("  原版版本：安装前已存在，保留：%s" % (result.game_dir / "versions" / result.mc_version))
            else:
                print("  原版版本：未保留（加载器版本已合并为独立版本）")
            for instance_dir in result.instance_dirs:
                print("  版本隔离目录：%s" % instance_dir)
            if result.java_path:
                print("  Java：%s" % result.java_path)
            for profile in result.profile_paths:
                print("  启动配置：%s" % profile)
            if result.installer_log:
                print("  安装器日志：%s" % result.installer_log)
            print(
                "  用时：%.1f 秒  ·  本次下载：%s"
                % (result.elapsed, format_bytes(result.downloaded_bytes))
            )
            print()
            print(
                "提示：在 PCL / HMCL 中把「%s」作为 .minecraft 游戏根目录，"
                "并选择加载器版本；版本隔离目录已经写入启动器配置。"
                % result.game_dir
            )
            return 0

        print("准备下载 Minecraft %s" % version)
        print(".minecraft 文件夹：%s" % root)
        print()

        progress = _ConsoleProgress()
        result = download_game(version, root, version_info=target_info, progress=progress)
        progress._finish_line()
        print()
        print("下载完成")
        print("  版本：%s" % result.version)
        print("  目录：%s" % result.folder)
        print("  JAR： %s%s" % (result.jar_path, "（已存在，跳过）" if result.jar_skipped else ""))
        print("  JSON：%s%s" % (result.json_path, "（已存在，跳过）" if result.json_skipped else ""))
        print(
            "  用时：%.1f 秒  ·  本次下载：%s"
            % (result.elapsed, format_bytes(result.downloaded_bytes))
        )
        return 0

    except DownloadCancelled:
        print()
        print("已取消下载。")
        return 130
    except KeyboardInterrupt:
        print()
        print("已取消下载。")
        return 130
    except MCDownError as exc:
        print()
        sys.stdout.flush()
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:  # 命令行模式下给出直白错误，不让用户面对长堆栈。
        print()
        sys.stdout.flush()
        print("发生未预期的错误：%s" % exc, file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcdown.py",
        description="从 BMCLAPI 下载 Minecraft 客户端 JAR/版本 JSON，并安装 Fabric/Forge/NeoForge/Quilt。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python mcdown.py\n"
            "      启动图形界面\n"
            "  python mcdown.py --version 1.20.1 --output ./Downloads/Minecraft/.minecraft\n"
            "      下载 1.20.1 到 .minecraft/versions/1.20.1/\n"
            "  python mcdown.py --latest-release --output ./Downloads/Minecraft/.minecraft\n"
            "      下载最新正式版\n"
            "  python mcdown.py --version 1.20.1 --loader fabric --output ./Downloads/Minecraft/.minecraft\n"
            "      安装 Fabric；最终只保留带加载器的独立版本\n"
            "  python mcdown.py --version 1.20.1 --loader forge --loader-version 47.4.28\n"
            "      指定 Forge 版本安装\n"
            "  python mcdown.py --list release\n"
            "      列出所有正式版\n"
        ),
    )
    parser.add_argument("--gui", action="store_true", help="启动图形界面（默认）")
    parser.add_argument("--cli", action="store_true", help="强制使用命令行模式")
    parser.add_argument("--version", metavar="版本号", help="要下载的 Minecraft 版本，例如 1.20.1")
    parser.add_argument(
        "-o",
        "--output",
        metavar="目录",
        help="所选文件夹直接作为 .minecraft 根目录；默认 ~/Downloads/Minecraft",
    )
    parser.add_argument(
        "--loader",
        choices=LOADER_ORDER,
        help="同时一键安装模组加载器：fabric / forge / neoforge / quilt",
    )
    parser.add_argument(
        "--loader-version",
        metavar="版本号",
        help="加载器版本；默认使用推荐的稳定版本",
    )
    parser.add_argument(
        "--java",
        metavar="路径",
        help="供 Forge/NeoForge 安装使用的 java 可执行文件；默认自动检测",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--latest-release", action="store_true", help="下载最新正式版")
    group.add_argument("--latest-snapshot", action="store_true", help="下载最新快照")
    parser.add_argument(
        "--list",
        nargs="?",
        const="all",
        choices=("all", "release", "snapshot", "old_beta", "old_alpha"),
        help="列出 BMCLAPI 版本，可加类型筛选：release/snapshot/old_beta/old_alpha",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cli_requested = bool(
        args.cli
        or args.version
        or args.output
        or args.latest_release
        or args.latest_snapshot
        or args.list is not None
        or args.loader
        or args.loader_version
        or args.java
    )
    if args.gui and (cli_requested or args.cli):
        parser.error("--gui 不能与命令行下载参数同时使用。")
    if args.version and (args.latest_release or args.latest_snapshot):
        parser.error("--version 不能与 --latest-release/--latest-snapshot 同时使用。")
    if args.list is not None and (
        args.version or args.latest_release or args.latest_snapshot or args.output
        or args.loader or args.loader_version or args.java
    ):
        parser.error("--list 不能与下载参数同时使用。")
    if (args.loader_version or args.java) and not args.loader:
        parser.error("--loader-version/--java 必须与 --loader 一起使用。")
    if args.loader and args.loader not in LOADER_ORDER:
        parser.error("不支持的加载器：%s" % args.loader)

    if args.gui or not cli_requested:
        return gui_main()
    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
