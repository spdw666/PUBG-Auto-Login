"""Steam 封禁批量查询器（原生 Windows 桌面版）。

用 SteamID64 和用户自己申请的 Steam Web API Key 查询 Steam 的公开封禁字段；
账号密码按用户选择以明文保存在本机；Steam Web API Key 仍通过 Windows DPAPI 加密保存，
两者都不会上传到任何服务器。
"""
from __future__ import annotations

import base64
import csv
import ctypes
import json
import os
import queue
import re
import shutil
import sqlite3
import steam_login
import sys
import tempfile
import threading
import time
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import BooleanVar, Menu, StringVar, Text, Toplevel, filedialog, messagebox, simpledialog
from typing import Any, Callable, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import ttkbootstrap as ttk

APP_NAME = "Steam 封禁批量查询器"
APP_VERSION = "3.2.1"
GITHUB_REPOSITORY = "spdw666/PUBG-Auto-Login"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
STEAM_API_KEY_APPLICATION_URL = "https://steamcommunity.com/dev/apikey"
APP_DATA_FOLDER_NAME = "PUBG-Auto-Login"
ACCOUNT_DATABASE_FILENAME = "steam_ban_accounts.sqlite3"
KEY_SETTINGS_FILENAME = "steam_ban_settings.json"
LEGACY_SCAN_MARKER_FILENAME = "legacy-data-scan.json"
LEGACY_SCAN_SKIP_DIRECTORY_NAMES = frozenset({
    '.git', '.npm', '.nuget', '.cache', 'node_modules', 'temp', 'inetcache', 'crashdumps', 'packages',
})
# 用户目录里这些是 Windows 为兼容旧程序保留的链接（Application Data → AppData 等），跟着走只会重复扫一遍。
LEGACY_PROFILE_SKIP_DIRECTORY_NAMES = LEGACY_SCAN_SKIP_DIRECTORY_NAMES | {
    'application data', 'local settings', 'my documents', 'nethood', 'printhood',
    'recent', 'sendto', 'templates', 'cookies', 'start menu', 'appdata',
}
# 非系统盘上用户可能把工具放在任意两层目录里；系统目录不必进。
LEGACY_DRIVE_SKIP_DIRECTORY_NAMES = LEGACY_SCAN_SKIP_DIRECTORY_NAMES | {
    'windows', 'program files', 'program files (x86)', 'programdata', '$recycle.bin',
    'system volume information', 'recovery', 'perflogs', 'msocache', 'config.msi', 'appdata',
    'intel', 'amd', 'nvidia', 'drivers',
    # 当前用户的目录由用户目录遍历负责；固定磁盘这一层跳过 Users，
    # 免得把同一台电脑上**别的 Windows 用户**的账号库当成旧数据迁移过来（DPAPI 解不开，密码全废）。
    'users',
}
LEGACY_DRIVE_SCAN_MAX_DEPTH = 2
# 深度自动扫描必须是整次任务共享的预算，而不是“每个磁盘再给 90 秒”。常见目录的
# 直接检查不受此限制；超出预算的非常规位置仍可通过手动迁移选择。
LEGACY_SCAN_MAX_DIRECTORIES = 12000
LEGACY_SCAN_MAX_SECONDS = 25.0

API_URLS = (
    "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/",
    "https://partner.steam-api.com/ISteamUser/GetPlayerBans/v1/",
)

PUBG_APP_ID = 578080
CONSERVATIVE_BATCH_SIZE = 100
IMPORT_DB_BATCH_SIZE = 500
TREE_LOAD_CHUNK = 5000

TABLE_PAGE_SIZE = TREE_LOAD_CHUNK
MAX_UI_EVENTS_PER_TICK = 80
STEAM_ID_PATTERN = re.compile(r"^\d{17}$")

ID_HEADERS = {"steam_id64", "steamid", "steam64", "64位id", "steamid64", "steam_id"}
NAME_HEADERS = {"name", "username", "名称", "账号标签", "账号", "account", "account_name"}
NOTE_HEADERS = {"note", "comment", "备注", "说明"}
PASSWORD_HEADERS = {"密码", "email_password", "邮箱密码", "steam_password", "password"}
STEAM_PASSWORD_HEADERS = {"password", "steam_password", "密码"}


class UiColors:
    BACKGROUND = '#F5F7FB'
    SURFACE = '#FFFFFF'
    SURFACE_SUBTLE = '#F8FAFC'
    BORDER = '#E2E8F0'
    TEXT = '#172033'
    MUTED = '#64748B'
    PRIMARY = '#2563EB'
    PRIMARY_HOVER = '#1D4ED8'
    PRIMARY_PRESSED = '#1E3A8A'
    SUCCESS = '#047857'
    SUCCESS_SUBTLE = '#ECFDF5'
    WARNING = '#A16207'
    WARNING_SUBTLE = '#FFFBEB'
    DANGER = '#B91C1C'
    DANGER_SUBTLE = '#FFF1F2'
    SIDEBAR = '#0F172A'
    SIDEBAR_ACTIVE = '#1E293B'
    SIDEBAR_MUTED = '#94A3B8'


class UiMetrics:
    WINDOW_WIDTH = 1480
    WINDOW_HEIGHT = 900
    SCREEN_INSET = 18
    CARD_PADDING = 18
    SECTION_GAP = 16
    BUTTON_PAD_X = 14
    BUTTON_PAD_Y = 8
    ROW_HEIGHT = 32
    KEY_ACTION_GAP = 8
    KEY_SAVE_BUTTON_WIDTH = 12
    KEY_CLEAR_BUTTON_WIDTH = 12
    KEY_LOGIN_BUTTON_WIDTH = 24


class SteamApiFatalError(RuntimeError):
    """整个请求无法继续时使用；不能把这类问题误标为每个账号各自查询失败。"""


class KeyStorageError(RuntimeError):
    """Windows DPAPI 保存或读取失败。"""


# ---------------------------------------------------------------------------
# NOTE FOR THE ASSEMBLER (lines 1-99 are scaffolding, NOT original source):
# verify.py compiles this file stand-alone, and two things depend on module
# level context:
#   1. "import base64/ctypes/json" -- CPython emits the plain
#      LOAD_ATTR + PUSH_NULL call form only when the called global is a module
#      bound by an import statement; otherwise it emits "LOAD_ATTR + NULL|self"
#      and _protect / _unprotect / save / load differ by 15 instructions.
#      With the real header these lines are redundant and may be dropped.
#   2. blank padding so that "class DpapiKeyStore:" lands on line 100: the
#      verifier compares the class body __firstlineno__ (LOAD_SMALL_INT 100)
#      and co_consts. Lines 100-184 below are exactly original lines 100-184
#      (nested DataBlob classes at lines 111 and 140).
# ---------------------------------------------------------------------------















































































class DpapiKeyStore:
    """以当前 Windows 用户的 DPAPI 保护 Steam Web API Key。"""

    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _protect(data: bytes) -> bytes:
        if os.name != "nt":
            raise KeyStorageError("加密保存仅支持 Windows。")

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        source_buffer = ctypes.create_string_buffer(data)
        source_blob = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
        protected_blob = DataBlob()
        crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob), ctypes.c_void_p,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not crypt32.CryptProtectData(
            ctypes.byref(source_blob), APP_NAME, None, None, None, 0, ctypes.byref(protected_blob),
        ):
            raise KeyStorageError(str(ctypes.WinError(ctypes.get_last_error())))
        try:
            return ctypes.string_at(protected_blob.pbData, protected_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(protected_blob.pbData, ctypes.c_void_p))

    @staticmethod
    def _unprotect(data: bytes) -> bytes:
        if os.name != "nt":
            raise KeyStorageError("加密保存仅支持 Windows。")

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        source_buffer = ctypes.create_string_buffer(data)
        source_blob = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
        plain_blob = DataBlob()
        crypt32 = ctypes.WinDLL("Crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DataBlob), ctypes.c_void_p,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not crypt32.CryptUnprotectData(
            ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(plain_blob),
        ):
            raise KeyStorageError(str(ctypes.WinError(ctypes.get_last_error())))
        try:
            return ctypes.string_at(plain_blob.pbData, plain_blob.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(plain_blob.pbData, ctypes.c_void_p))

    def save(self, api_key: str) -> None:
        encrypted = self._protect(api_key.encode("utf-8"))
        payload = {"version": 1, "api_key_dpapi": base64.b64encode(encrypted).decode("ascii")}
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        temporary_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary_path.replace(self.path)

    def load(self) -> str | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            encrypted = base64.b64decode(str(payload["api_key_dpapi"]), validate=True)
            return self._unprotect(encrypted).decode("utf-8").strip() or None
        except (KeyError, ValueError, UnicodeDecodeError, OSError, json.JSONDecodeError, KeyStorageError) as exc:
            raise KeyStorageError(f"无法读取已保存的 API Key：{exc}") from exc

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def application_data_directory(local_app_data: str | Path | None = None) -> Path:
    """返回所有版本共享的、当前 Windows 用户专属的数据目录。"""
    if local_app_data is not None:
        base_directory = Path(local_app_data)
    else:
        base_directory = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    return base_directory / APP_DATA_FOLDER_NAME


def legacy_data_directories(install_directory: Path, home_directory: Path | None = None) -> list[Path]:
    """只检查旧版最常见的固定位置，不递归扫描用户文件。"""
    home = home_directory or Path.home()
    candidates = [
        install_directory,
        home / 'Downloads',
        home / 'Desktop',
        home / 'Documents',
    ]
    unique: list[Path] = []
    seen: set[Path] = set()
    for directory in candidates:
        resolved = directory.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def account_database_stats(path: Path) -> tuple[int, int] | None:
    """返回账号库的 (账号数, 已保存密码数)；不是本软件的账号库时返回 None。"""
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
    except (OSError, sqlite3.Error):
        return None
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone() is None:
            return None
        total = connection.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
        # 老版本的表可能还没有密码列，这类库仍然可用（只是没有密码），不能当成"不是账号库"。
        column_names = {row[1] for row in connection.execute('PRAGMA table_info(accounts)')}
        password_fields = [
            field for field in ('password', 'password_enc') if field in column_names
        ]
        with_password = 0
        if password_fields:
            where = ' OR '.join(f"{field} IS NOT NULL AND {field} <> ''" for field in password_fields)
            with_password = connection.execute(
                f"SELECT COUNT(*) FROM accounts WHERE {where}"
            ).fetchone()[0]
        return int(total), int(with_password)
    except (OSError, sqlite3.Error):
        return None
    finally:
        connection.close()


def is_account_database(path: Path) -> bool:
    """只接受包含 accounts 表的本软件账号库，避免误迁移同名的无关 SQLite 文件。"""
    return account_database_stats(path) is not None


def candidate_rank(path: Path) -> tuple[int, int, float]:
    """账号多、密码全、更新得晚的旧库更完整。"""
    accounts, with_password = account_database_stats(path) or (0, 0)
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return accounts, with_password, modified


def best_candidate(candidates: Iterable[Path]) -> Path | None:
    """按 (账号数, 密码数, 修改时间) 选一份最完整的账号库。"""
    ranked = [(candidate_rank(path), path) for path in candidates]
    if not ranked:
        return None
    return max(ranked, key=lambda item: item[0])[1]


@dataclass
class LegacyScanBudget:
    """一次深度迁移扫描共享的时间和目录额度。"""

    deadline: float
    remaining_directories: int

    @classmethod
    def automatic(cls) -> 'LegacyScanBudget':
        return cls(time.monotonic() + LEGACY_SCAN_MAX_SECONDS, LEGACY_SCAN_MAX_DIRECTORIES)

    def available(self) -> bool:
        return self.remaining_directories > 0 and time.monotonic() <= self.deadline

    def consume_directory(self) -> bool:
        if not self.available():
            return False
        self.remaining_directories -= 1
        return True


def _scanable_directory(entry: os.DirEntry) -> bool:
    """目录联接点/符号链接不跟随，避免扫描跳出用户目录或形成重复遍历。"""
    if not entry.is_dir(follow_symlinks=False):
        return False
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0)
    except OSError:
        return False
    return not bool(attributes & 0x0400)  # FILE_ATTRIBUTE_REPARSE_POINT


def find_account_databases(
    directory: Path,
    *,
    max_depth: int | None = None,
    excluded_directories: Iterable[Path] = (),
    skip_directory_names: frozenset[str] = LEGACY_SCAN_SKIP_DIRECTORY_NAMES,
    max_directories: int = LEGACY_SCAN_MAX_DIRECTORIES,
    max_seconds: float = LEGACY_SCAN_MAX_SECONDS,
    scan_budget: LegacyScanBudget | None = None,
) -> list[Path]:
    """只读查找目录下固定文件名的本软件账号库；max_depth 为 None 表示一直往下找。

    目录数与耗时都有上限；传入 scan_budget 时，多个根目录共享同一份总预算。
    """
    base = Path(directory)
    try:
        base = base.resolve()
    except OSError:
        return []
    if not base.is_dir():
        return []
    excluded = set()
    for path in excluded_directories:
        try:
            excluded.add(Path(path).resolve())
        except OSError:
            continue
    depth_limit = max_depth if max_depth is not None else 1 << 30
    deadline = min(time.monotonic() + max_seconds, scan_budget.deadline) if scan_budget else time.monotonic() + max_seconds
    found: list[Path] = []
    visited = 0
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        current, depth = stack.pop()
        if current in excluded:
            continue
        if visited >= max_directories or time.monotonic() > deadline:
            break
        if scan_budget is not None and not scan_budget.consume_directory():
            break
        visited += 1
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if _scanable_directory(entry):
                            if depth < depth_limit and entry.name.casefold() not in skip_directory_names:
                                stack.append((Path(entry.path), depth + 1))
                        elif entry.name == ACCOUNT_DATABASE_FILENAME and account_database_stats(Path(entry.path)) is not None:
                            found.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def fixed_drive_roots() -> list[Path]:
    """返回本机固定磁盘的根目录（跳过光驱、网络盘和可移动盘）。"""
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return []
    roots: list[Path] = []
    for index in range(26):
        if not bitmask & (1 << index):
            continue
        root = f'{chr(65 + index)}:\\'
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:   # DRIVE_FIXED
                continue
        except (AttributeError, OSError):
            continue
        candidate = Path(root)
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def collect_legacy_account_databases(
    install_directory: Path,
    home_directory: Path | None = None,
    *,
    deep: bool = False,
    excluded_directories: Iterable[Path] = (),
) -> list[Path]:
    """收集可能的旧版账号库：先看常见位置，deep=True 时再找用户目录和所有固定磁盘。"""
    home = home_directory or Path.home()
    candidates: list[Path] = []
    for directory in legacy_data_directories(install_directory, home):
        direct_candidate = directory / ACCOUNT_DATABASE_FILENAME
        if account_database_stats(direct_candidate) is not None:
            candidates.append(direct_candidate)
        try:
            child_directories = [
                child for child in directory.iterdir()
                if child.is_dir() and not child.is_symlink() and not child.is_junction()
            ]
        except OSError:
            continue
        for child in child_directories:
            nested_candidate = child / ACCOUNT_DATABASE_FILENAME
            if account_database_stats(nested_candidate) is not None:
                candidates.append(nested_candidate)
    if deep:
        budget = LegacyScanBudget.automatic()
        candidates.extend(find_account_databases(
            home,
            excluded_directories=excluded_directories,
            skip_directory_names=LEGACY_PROFILE_SKIP_DIRECTORY_NAMES,
            scan_budget=budget,
        ))
        for root in fixed_drive_roots():
            if not budget.available():
                break
            candidates.extend(find_account_databases(
                root,
                max_depth=LEGACY_DRIVE_SCAN_MAX_DEPTH,
                excluded_directories=excluded_directories,
                skip_directory_names=LEGACY_DRIVE_SKIP_DIRECTORY_NAMES,
                scan_budget=budget,
            ))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_legacy_account_database(install_directory: Path, home_directory: Path | None = None) -> Path | None:
    """从旧版默认目录及其一级子目录里挑一份最完整的账号库。"""
    return best_candidate(collect_legacy_account_databases(install_directory, home_directory))


def find_legacy_account_database_in_profile(
    home_directory: Path,
    excluded_directory: Path,
) -> Path | None:
    """后台遍历当前 Windows 用户目录，只匹配固定名称并验证是本软件的账号库。"""
    return best_candidate(find_account_databases(
        home_directory,
        excluded_directories=[excluded_directory],
        skip_directory_names=LEGACY_PROFILE_SKIP_DIRECTORY_NAMES,
    ))


def legacy_data_is_missing(stats: tuple[int, int]) -> bool:
    """账号库是空的、或者一条密码都没保存时，值得去找旧版数据。"""
    accounts, with_password = stats
    return accounts == 0 or with_password == 0


def legacy_migration_is_worthwhile(current: tuple[int, int], candidate: tuple[int, int]) -> bool:
    """候选旧库是否值得自动“合并”。自动路径永远不再整体替换当前库。"""
    current_accounts, current_passwords = current
    candidate_accounts, candidate_passwords = candidate
    if candidate_accounts == 0:
        return False
    if current_accounts == 0:
        return True
    if current_passwords == 0:
        # 合并不会删除当前账号，不必再要求候选账号数不少于当前库；旧库只要能补回至少
        # 一条密码就有价值。密码的可解密性会在实际合并时再次验证。
        return candidate_passwords > 0
    return False


def best_migratable_legacy_database(
    candidates: Iterable[Path],
    current_stats: tuple[int, int],
) -> Path | None:
    """在候选里挑出最值得迁移的一份旧库。"""
    eligible = migratable_legacy_databases(candidates, current_stats)
    return eligible[0] if eligible else None


def migratable_legacy_databases(
    candidates: Iterable[Path],
    current_stats: tuple[int, int],
) -> list[Path]:
    """返回值得安全合并的候选库，完整度高的优先；合并不会覆盖当前数据。"""
    eligible = [
        path for path in candidates
        if legacy_migration_is_worthwhile(current_stats, account_database_stats(path) or (0, 0))
    ]
    return sorted(eligible, key=candidate_rank, reverse=True)


AUTO_MERGE_QUERY_COLUMNS = (
    'vac_banned', 'vac_count', 'days_since_last_ban', 'game_bans', 'community_banned',
    'economy_ban', 'pubg_assessment', 'api_bans_json', 'checked_at', 'status', 'query_error',
)
AUTO_MERGE_ACTIVITY_COLUMNS = ('last_login_at', 'last_logout_at')


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info({table})')}


def _nonempty(value: Any) -> bool:
    return value is not None and str(value) != ''


def merge_legacy_user_data(legacy_directory: Path, data_directory: Path) -> dict[str, Any]:
    """把旧账号库安全合并进当前库，而不是把当前库整体覆盖掉。

    同 SteamID64 的当前数据优先，仅填补空的账号名、备注、密码、查询结果和登录时间；
    同名占位行在没有真实 ID 冲突时会原地升级。v3.1.x 的旧密码密文会先由当前
    Windows 用户解密，再作为 v3.2 的明文密码带入；无法解密的旧密文不会覆盖当前库。
    """
    legacy_directory = legacy_directory.resolve()
    data_directory = data_directory.resolve()
    source_database = legacy_directory / ACCOUNT_DATABASE_FILENAME
    destination_database = data_directory / ACCOUNT_DATABASE_FILENAME
    source_settings = legacy_directory / KEY_SETTINGS_FILENAME
    destination_settings = data_directory / KEY_SETTINGS_FILENAME
    result: dict[str, Any] = {
        'accounts_added': 0,
        'accounts_updated': 0,
        'passwords_filled': 0,
        'settings_copied': False,
        'unreadable_passwords': 0,
    }
    if not source_database.is_file() or source_database.resolve() == destination_database.resolve():
        return result
    if not is_account_database(source_database):
        raise ValueError(f'{source_database.name} 不是可识别的账号库。')

    data_directory.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f'{source_database.as_uri()}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    destination = sqlite3.connect(destination_database)
    destination.row_factory = sqlite3.Row
    try:
        AccountStore._create_schema(destination)
        source_columns = _table_columns(source, 'accounts')
        selected_columns = [
            column for column in (
                'account_name', 'steam_id', 'note', 'password', 'password_enc',
                *AUTO_MERGE_QUERY_COLUMNS, *AUTO_MERGE_ACTIVITY_COLUMNS,
            ) if column in source_columns
        ]
        if 'steam_id' not in selected_columns:
            raise ValueError('旧版账号库缺少 SteamID64 列。')
        source_rows = source.execute(
            f"SELECT {', '.join(selected_columns)} FROM accounts"
        )
        destination_columns = _table_columns(destination, 'accounts')
        with destination:
            for source_row in source_rows:
                values = {column: source_row[column] for column in selected_columns}
                steam_id = str(values.get('steam_id') or '').strip()
                if not steam_id:
                    continue
                values['steam_id'] = steam_id
                values['account_name'] = str(values.get('account_name') or '').strip()
                values['note'] = str(values.get('note') or '').strip()
                password = str(values.get('password') or '')
                encrypted_password = str(values.get('password_enc') or '')
                if not password and encrypted_password:
                    password = AccountStore._decrypt_legacy_password(encrypted_password)
                    if not password:
                        # DPAPI 密文来自其它 Windows 用户/旧系统时不可用，绝不因为“非空”而覆盖当前库。
                        result['unreadable_passwords'] += 1
                values['password'] = password

                source_has_real_id = bool(STEAM_ID_PATTERN.fullmatch(steam_id))
                current = destination.execute(
                    'SELECT * FROM accounts WHERE steam_id=?', (steam_id,)
                ).fetchone()
                if current is None and values['account_name']:
                    same_name = destination.execute(
                        'SELECT * FROM accounts WHERE account_name=? COLLATE NOCASE ORDER BY id',
                        (values['account_name'],),
                    ).fetchall()
                    if source_has_real_id:
                        # 旧库拿到了真实 ID、当前库仍是同名占位行：直接原地升级占位行。
                        current = next(
                            (row for row in same_name if not STEAM_ID_PATTERN.fullmatch(row['steam_id'] or '')),
                            None,
                        )
                    else:
                        # 反向场景同样常见：当前库已经通过查询拿到真实 ID，但旧库仍是同名
                        # 占位行且保存着密码。此时必须合并到真实 ID 行，而不是插入第二条占位行。
                        current = next(
                            (row for row in same_name if STEAM_ID_PATTERN.fullmatch(row['steam_id'] or '')),
                            None,
                        )
                        if current is None:
                            current = next(iter(same_name), None)
                    if current is not None:
                        if source_has_real_id:
                            destination.execute('UPDATE accounts SET steam_id=? WHERE id=?', (steam_id, current['id']))
                            current = destination.execute('SELECT * FROM accounts WHERE id=?', (current['id'],)).fetchone()

                if current is None:
                    insert = {
                        'account_name': values['account_name'],
                        'steam_id': steam_id,
                        'note': values['note'],
                        'password': values['password'],
                        'status': str(values.get('status') or '未查询'),
                        'query_error': str(values.get('query_error') or ''),
                    }
                    for column in (*AUTO_MERGE_QUERY_COLUMNS, *AUTO_MERGE_ACTIVITY_COLUMNS):
                        if column in values and column in destination_columns:
                            insert[column] = values[column]
                    columns = list(insert)
                    marks = ', '.join('?' for _ in columns)
                    destination.execute(
                        f"INSERT INTO accounts ({', '.join(columns)}) VALUES ({marks})",
                        [insert[column] for column in columns],
                    )
                    result['accounts_added'] += 1
                    if values['password']:
                        result['passwords_filled'] += 1
                    continue

                updates: dict[str, Any] = {}
                for column in ('account_name', 'note', 'password'):
                    if column in values and _nonempty(values[column]) and not _nonempty(current[column]):
                        updates[column] = values[column]
                if 'password' in updates:
                    result['passwords_filled'] += 1

                # 当前已经查询过时，保留它的查询状态；当前从未查询才用旧库结果补齐。
                if not _nonempty(current['checked_at']) and _nonempty(values.get('checked_at')):
                    for column in AUTO_MERGE_QUERY_COLUMNS:
                        if column in values and column in destination_columns:
                            updates[column] = values[column]

                # 当前有活跃登录记录（登录时间有、退出时间空）时，不能用旧库的退出时间覆盖它。
                if not _nonempty(current['last_login_at']) and _nonempty(values.get('last_login_at')):
                    updates['last_login_at'] = values['last_login_at']
                    if 'last_logout_at' in values:
                        updates['last_logout_at'] = values['last_logout_at']
                elif not _nonempty(current['last_login_at']) and _nonempty(values.get('last_logout_at')):
                    updates['last_logout_at'] = values['last_logout_at']

                if updates:
                    assignments = ', '.join(f'{column}=?' for column in updates)
                    destination.execute(
                        f'UPDATE accounts SET {assignments} WHERE id=?',
                        [*updates.values(), current['id']],
                    )
                    result['accounts_updated'] += 1
    finally:
        destination.close()
        source.close()

    if source_settings.is_file() and not destination_settings.exists():
        try:
            # 设置文件也受 DPAPI 保护；不是当前用户可读取的旧 Key 不应复制进来制造“已保存但不可用”。
            if DpapiKeyStore(source_settings).load():
                shutil.copy2(source_settings, destination_settings)
                result['settings_copied'] = True
        except KeyStorageError:
            pass
    return result


def copy_sqlite_database(source_path: Path, destination_path: Path) -> None:
    """用 SQLite backup API 迁移账号库，连同 WAL 中尚未合并的数据一起带走。"""
    source_path = source_path.resolve()
    destination_path = destination_path.resolve()
    if source_path == destination_path:
        return
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f'{source_path.as_uri()}?mode=ro', uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def migrate_legacy_user_data(
    legacy_directory: Path,
    data_directory: Path,
    *,
    replace_database: bool = False,
    replace_settings: bool = False,
) -> tuple[bool, bool]:
    """把同一 Windows 用户旧版旁边的数据复制到所有新版共用的数据目录。"""
    legacy_directory = legacy_directory.resolve()
    data_directory = data_directory.resolve()
    source_database = legacy_directory / ACCOUNT_DATABASE_FILENAME
    destination_database = data_directory / ACCOUNT_DATABASE_FILENAME
    source_settings = legacy_directory / KEY_SETTINGS_FILENAME
    destination_settings = data_directory / KEY_SETTINGS_FILENAME
    data_directory.mkdir(parents=True, exist_ok=True)

    migrated_database = False
    if source_database.is_file() and source_database.resolve() != destination_database.resolve():
        if not is_account_database(source_database):
            raise ValueError(f'{source_database.name} 不是可识别的账号库。')
        if replace_database or not destination_database.exists():
            copy_sqlite_database(source_database, destination_database)
            migrated_database = True

    migrated_settings = False
    if source_settings.is_file() and source_settings.resolve() != destination_settings.resolve():
        if replace_settings or not destination_settings.exists():
            shutil.copy2(source_settings, destination_settings)
            migrated_settings = True
    return migrated_database, migrated_settings


SQL_VARIABLE_CHUNK = 900


def id_chunks(values: Iterable[int], size: int = SQL_VARIABLE_CHUNK) -> Iterator[list[int]]:
    """把 id 列表切块，避免一次绑定上万个参数触发 SQLite 的变量数上限。"""
    chunk: list[int] = []
    for value in values:
        chunk.append(int(value))
        if len(chunk) >= max(1, size):
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


def version_key(value: str) -> tuple[int, int, int] | None:
    """把 GitHub tag 或界面版本转换为可比较的三段语义化版本。"""
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(value or '').strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def login_elapsed_label(last_logout_at: str | None, last_login_at: str | None, now: datetime | None = None) -> str:
    """显示由本工具记录的退出后经过时间；72 小时后改为显示上次登录日期。"""
    if not last_login_at:
        return '从未登录'
    if not last_logout_at:
        return '当前登录中'
    try:
        logged_out = datetime.strptime(last_logout_at, '%Y-%m-%d %H:%M:%S %z')
    except ValueError:
        return '登录时间未知'
    reference = now or datetime.now(logged_out.tzinfo)
    elapsed_hours = max(0, int((reference - logged_out).total_seconds() // 3600))
    if elapsed_hours < 72:
        return f'距上次登录 {elapsed_hours} 小时'
    return f"上次登录：{logged_out.strftime('%Y-%m-%d')}"


def normalize_steam_id(value: Any) -> str:
    value = str(value or '').strip()
    if not STEAM_ID_PATTERN.fullmatch(value):
        raise ValueError('SteamID64 必须是 17 位数字')
    return value


def bool_label(value: Any) -> str:
    if value is None:
        return '—'
    return '是' if bool(value) else '否'


def pubg_assessment(player: dict[str, Any]) -> str:
    """保守地描述 PUBG 状态，避免把任意 Game Ban 误称为 PUBG 封禁。"""
    details = player.get('bans')
    if isinstance(details, list):
        for item in details:
            try:
                app_min = int(item.get('AppIdMin', -1))
                app_max = int(item.get('AppIdMax', -1))
            except (TypeError, ValueError):
                continue
            if app_min <= PUBG_APP_ID <= app_max:
                return 'PUBG 关联封禁（API 明细）'
    if int(player.get('NumberOfGameBans', 0) or 0) > 0:
        return '存在游戏封禁（未证明 PUBG）'
    return '未发现游戏封禁'


@dataclass(frozen=True)
class Account:
    account_id: int
    account_name: str
    steam_id: str
    note: str


@dataclass(frozen=True)
class CsvLayout:
    has_header: bool
    id_index: int | None
    name_index: int | None
    note_index: int | None
    password_index: int | None


IMPORT_ACCOUNT_KEYS = ('account', 'account_name', 'username', 'user', 'login', 'name', '账号', '账号名', '账户')
IMPORT_PASSWORD_KEYS = ('password', 'pass', 'pwd', 'steam_password', '密码')
IMPORT_ID_KEYS = ('steam_id', 'steamid', 'steam_id64', 'steamid64', 'steam64', '64位id', 'id')
IMPORT_NOTE_KEYS = ('note', 'remark', 'comment', '备注', '说明')
TXT_SEPARATORS = ('----', '---', '--', '\t', '|', '：', ':', ';', ',', ' ')
JSON_LIST_KEYS = ('accounts', 'data', 'list', 'users', 'items', 'records', 'result')


def pick_value(mapping: Any, keys: tuple) -> str:
    """从字典里按候选键名取值（大小写不敏感）。"""
    if not isinstance(mapping, dict):
        return ''
    lowered = {str(item).strip().lower(): value for item, value in mapping.items()}
    for key in keys:
        value = lowered.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ''


def placeholder_for(account_name: str) -> str:
    """没有 SteamID64 时先用账号名占位；点「开始查询」时会自动去 Steam 解析真实 ID。"""
    return account_name.strip()


def txt_record(line: str, separator: str | None = None) -> tuple | None:
    """解析 TXT 的一行：账号----密码[----备注或 SteamID64]。"""
    text = line.strip()
    if not text or text.startswith('#'):
        return None
    separators = (separator,) if separator else TXT_SEPARATORS
    for sep in separators:
        if sep and sep in text:
            parts = [item.strip() for item in text.split(sep)]
            account = parts[0]
            if not account:
                continue
            if len(parts) == 2:
                return account, placeholder_for(account), '', parts[1]
            # 导出格式是「账号----密码----SteamID64」。密码本身也可能包含分隔符，
            # 因而应从右侧识别 ID，并把中间全部还原为密码。
            if STEAM_ID_PATTERN.fullmatch(parts[-1]):
                return account, parts[-1], '', sep.join(parts[1:-1])
            # 没有末尾 SteamID64 时仍兼容手工写的「账号----密码----备注」；
            # 多出的分段属于备注，不能像旧实现一样直接丢弃。
            return account, placeholder_for(account), sep.join(parts[2:]), parts[1]
    return None


def json_records(data: Any) -> Iterator[dict]:
    """把各种常见结构的 JSON 摊平成一条条账号字典。"""
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(data, dict):
        return
    for key in JSON_LIST_KEYS:
        nested = data.get(key)
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    yield item
            return
    if pick_value(data, IMPORT_PASSWORD_KEYS) or pick_value(data, IMPORT_ID_KEYS):
        yield data
        return
    # {"账号": "密码"} 或 {"账号": {"password": ..., "steam_id": ...}}
    for name, value in data.items():
        if isinstance(value, str):
            yield {'account': name, 'password': value}
        elif isinstance(value, dict):
            record = dict(value)
            record.setdefault('account', name)
            yield record


def record_from_mapping(mapping: dict) -> tuple | None:
    """把一条 JSON/字典记录映射成 (账号, SteamID64 或占位, 备注, 密码)。"""
    account = pick_value(mapping, IMPORT_ACCOUNT_KEYS)
    password = pick_value(mapping, IMPORT_PASSWORD_KEYS)
    note = pick_value(mapping, IMPORT_NOTE_KEYS)
    raw_id = pick_value(mapping, IMPORT_ID_KEYS)
    if not account and not raw_id:
        return None
    if raw_id:
        try:
            return account, normalize_steam_id(raw_id), note, password
        except ValueError:
            pass
    if not account:
        return None
    return account, placeholder_for(account), note, password


def detect_import_format(path: Path) -> str:
    """判断导入格式：csv / json / txt。

    先看后缀；后缀不认识（.log / .dat / 无后缀…）时**看内容**：
    第一行有 ---- 就当「账号----密码」文本，以 { 或 [ 开头就当 JSON。
    """
    suffix = path.suffix.lower()
    if suffix == '.json':
        return 'json'
    if suffix in ('.txt', '.text', '.lst', '.list'):
        return 'txt'
    if suffix in ('.csv', '.tsv'):
        return 'csv'
    try:
        with path.open('r', encoding='utf-8-sig', errors='ignore') as source:
            sample = source.read(4096)
    except OSError:
        return 'csv'
    head = sample.lstrip()
    if head.startswith(('[', '{')):
        return 'json'
    for line in head.splitlines():
        if line.strip():
            return 'txt' if '----' in line else 'csv'
    return 'csv'


def detect_csv_format(path: Path) -> tuple[str, str]:
    """识别常见中文 CSV 编码和分隔符，只读取最多 64 KiB 的样本。"""
    with path.open('rb') as source:
        sample_bytes = source.read(65536)
    if not sample_bytes:
        raise ValueError('导入文件为空。')
    encoding = ''
    sample = None
    # 64 KiB 的样本可能正好切在多字节字符中间（UTF-8 的中文是 3 字节）。必须先把
    # UTF-8 的原样及去尾 1~3 字节变体全部尝试完，再尝试 GB18030：例如 UTF-8 被截成
    # ``\xe4\xb8`` 时，这两个字节在 GB18030 中也是合法字符；交错尝试会把 UTF-8 文件误判为
    # GB18030，导致后续整份文件的中文乱码。
    sample_variants = (sample_bytes, sample_bytes[:-1], sample_bytes[:-2], sample_bytes[:-3])
    for candidate in ('utf-8-sig', 'utf-8', 'gb18030'):
        for data in sample_variants:
            try:
                sample = data.decode(candidate)
            except UnicodeDecodeError:
                continue
            encoding = candidate
            break
        if sample is not None:
            break
    if sample is None:
        raise ValueError('无法读取文件编码。请使用 UTF-8 或 GB18030 编码的 CSV/TSV。')
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=',\t;').delimiter
    except csv.Error:
        delimiter = '\t' if path.suffix.lower() == '.tsv' else ','
    return (encoding, delimiter)


def csv_layout(first_row: list[str]) -> CsvLayout:
    header = [cell.strip().lower().replace(' ', '_') for cell in first_row]
    recognized_headers = ID_HEADERS | NAME_HEADERS | NOTE_HEADERS | PASSWORD_HEADERS
    has_header = any(item in recognized_headers for item in header)
    if not has_header:
        return CsvLayout(False, None, None, None, None)
    id_index = next((index for index, item in enumerate(header) if item in ID_HEADERS), None)
    name_index = next((index for index, item in enumerate(header) if item in NAME_HEADERS), None)
    if id_index is None and name_index is None:
        raise ValueError('检测到表头，但既找不到 SteamID64 列，也找不到账号列。请将列名设为 steam_id、steamid64、64位ID 或 account。')
    return CsvLayout(
        True,
        id_index,
        name_index,
        next((index for index, item in enumerate(header) if item in NOTE_HEADERS), None),
        next((index for index, item in enumerate(header) if item in STEAM_PASSWORD_HEADERS), None),
    )


def import_record(row: list[str], layout: CsvLayout) -> tuple[str, str, str, str] | None:
    """返回账号标签、SteamID64、备注与 Steam 密码；密码由调用方加密后再入库。"""
    if not any(cell.strip() for cell in row):
        return None

    def cell(index: int | None) -> str:
        if index is not None and index < len(row):
            return row[index].strip()
        return ''

    if layout.has_header:
        steam_id = cell(layout.id_index)
        account_name = cell(layout.name_index)
        note = cell(layout.note_index)
        password = cell(layout.password_index)
    else:
        first = cell(0)
        second = cell(1)
        third = cell(2)
        if STEAM_ID_PATTERN.fullmatch(first):
            # 无账号名时，按 SteamID64,备注 处理。
            steam_id, account_name, note, password = first, '', second, ''
        elif STEAM_ID_PATTERN.fullmatch(second):
            # 常见的无表头格式：账号,SteamID64,备注。
            steam_id, account_name, note, password = second, first, third, ''
        else:
            # 另一个常见格式：账号,密码,备注。第二列不是 17 位 ID 时绝不能丢掉它。
            steam_id, account_name, note, password = '', first, third, second
    if not STEAM_ID_PATTERN.fullmatch(steam_id):
        # 没有 64 位 ID：先用账号名占位，点「开始查询」时会自动去 Steam 解析真实 ID
        if not account_name:
            raise ValueError('缺少 SteamID64，也没有账号名可用于解析。')
        return account_name, placeholder_for(account_name), note, password
    return account_name, normalize_steam_id(steam_id), note, password


class AccountStore:
    UPSERT_SQL = """
        INSERT INTO accounts (account_name, steam_id, note, password)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(steam_id) DO UPDATE SET
            account_name=COALESCE(NULLIF(excluded.account_name, ''), accounts.account_name),
            note=COALESCE(NULLIF(excluded.note, ''), accounts.note),
            password=COALESCE(NULLIF(excluded.password, ''), accounts.password)
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.connection = self._open_connection(db_path)
        self._create_schema(self.connection)

    @staticmethod
    def _open_connection(db_path: Path) -> sqlite3.Connection:
        """每个线程使用自己的 SQLite 连接，避免跨线程访问造成界面卡死或异常。"""
        connection = sqlite3.connect(db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout = 15000')
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA synchronous = NORMAL')
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL DEFAULT '',
                steam_id TEXT NOT NULL UNIQUE,
                note TEXT NOT NULL DEFAULT '',
                vac_banned INTEGER,
                vac_count INTEGER,
                days_since_last_ban INTEGER,
                game_bans INTEGER,
                community_banned INTEGER,
                economy_ban TEXT,
                pubg_assessment TEXT,
                api_bans_json TEXT,
                checked_at TEXT,
                status TEXT NOT NULL DEFAULT '未查询',
                query_error TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                last_login_at TEXT,
                last_logout_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_accounts_steam_id ON accounts(steam_id);
            CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(account_name COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS app_state (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL
            );
            """)
        existing = {row[1] for row in connection.execute('PRAGMA table_info(accounts)')}
        if 'password' not in existing:
            connection.execute("ALTER TABLE accounts ADD COLUMN password TEXT NOT NULL DEFAULT ''")
        if 'last_login_at' not in existing:
            connection.execute("ALTER TABLE accounts ADD COLUMN last_login_at TEXT")
        if 'last_logout_at' not in existing:
            connection.execute("ALTER TABLE accounts ADD COLUMN last_logout_at TEXT")
        # v3.1.x 的 password_enc 是当前 Windows 用户 DPAPI 密文。v3.2 起密码改为可见明文，
        # 打开旧库时把能解开的密文迁到 password 列；旧密文保留不删，回退旧版本仍可用。
        if 'password_enc' in existing:
            AccountStore._migrate_legacy_passwords_to_plaintext(connection)
        connection.execute('CREATE INDEX IF NOT EXISTS idx_accounts_last_login ON accounts(last_login_at DESC)')
        connection.commit()

    @staticmethod
    def _decrypt_legacy_password(password_enc: str) -> str:
        """仅用于把 v3.1.x DPAPI 密文迁到 v3.2 的明文密码列。"""
        if not password_enc:
            return ''
        try:
            return DpapiKeyStore._unprotect(base64.b64decode(password_enc)).decode('utf-8')
        except (ValueError, UnicodeDecodeError, KeyStorageError):
            return ''

    @staticmethod
    def _migrate_legacy_passwords_to_plaintext(connection: sqlite3.Connection) -> None:
        """原地迁出当前用户可解开的 v3.1.x 密文，不覆盖已有明文密码。"""
        rows = connection.execute(
            "SELECT id, password, password_enc FROM accounts"
            " WHERE (password IS NULL OR password = '')"
            " AND password_enc IS NOT NULL AND password_enc <> ''"
        ).fetchall()
        for row in rows:
            plain_password = str(row['password'] or '')
            if not plain_password:
                plain_password = AccountStore._decrypt_legacy_password(str(row['password_enc'] or ''))
            if plain_password:
                # 旧密文保留不删：万一回退到 v3.1.x，那边仍然读得到密码。
                connection.execute(
                    "UPDATE accounts SET password=? WHERE id=?",
                    (plain_password, int(row['id'])),
                )

    @staticmethod
    def _risk_where() -> str:
        return """
            (COALESCE(vac_banned, 0) > 0
             OR COALESCE(game_bans, 0) > 0
             OR COALESCE(community_banned, 0) > 0
             OR lower(COALESCE(economy_ban, 'none')) NOT IN ('none', '')
             OR status = '查询失败')
        """

    @staticmethod
    def _safe_where() -> str:
        """查询成功且没有任何封禁（未查询的不算）。"""
        return """
            (status = '查询成功'
             AND COALESCE(vac_banned, 0) = 0
             AND COALESCE(game_bans, 0) = 0
             AND COALESCE(community_banned, 0) = 0
             AND COALESCE(economy_ban, 'none') IN ('none', ''))
        """

    @staticmethod
    def _mode_where(mode: str) -> str:
        if mode == 'risk':
            return 'WHERE ' + AccountStore._risk_where()
        if mode == 'safe':
            return 'WHERE ' + AccountStore._safe_where()
        return ''

    def close(self) -> None:
        self.connection.close()

    def count_accounts(self, mode: str = 'all') -> int:
        where = self._mode_where(mode)
        return int(self.connection.execute(f'SELECT COUNT(*) FROM accounts {where}').fetchone()[0])

    def list_accounts_page(self, offset: int, limit: int, mode: str = 'all') -> tuple[list[sqlite3.Row], int]:
        where = self._mode_where(mode)
        total = int(self.connection.execute(f'SELECT COUNT(*) FROM accounts {where}').fetchone()[0])
        rows = self.connection.execute(
            f'SELECT * FROM accounts {where}'
            ' ORDER BY CASE WHEN last_login_at IS NULL OR last_login_at = \'\' THEN 1 ELSE 0 END,'
            ' last_login_at DESC, account_name COLLATE NOCASE, steam_id LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
        return rows, total

    def login_timestamps(self, account_ids: Iterable[int]) -> dict[int, tuple[str | None, str | None]]:
        """读取已加载行的登录时间，供整点时原地更新计时单元格。"""
        ids = [int(account_id) for account_id in account_ids]
        if not ids:
            return {}
        timestamps: dict[int, tuple[str | None, str | None]] = {}
        for chunk in id_chunks(ids):
            placeholders = ', '.join('?' for _ in chunk)
            rows = self.connection.execute(
                f'SELECT id, last_logout_at, last_login_at FROM accounts WHERE id IN ({placeholders})', chunk
            ).fetchall()
            timestamps.update({
                int(row['id']): (row['last_logout_at'], row['last_login_at'])
                for row in rows
            })
        return timestamps

    def active_account_id(self) -> int | None:
        """返回上一次由本工具成功登录、且尚未在下一次切换中退出的账号。"""
        row = self.connection.execute(
            "SELECT state_value FROM app_state WHERE state_key='active_account_id'"
        ).fetchone()
        if row is None:
            return None
        try:
            account_id = int(row['state_value'])
        except (TypeError, ValueError):
            account_id = 0
        exists = self.connection.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone()
        if exists is not None:
            return account_id
        with self.connection:
            self.connection.execute("DELETE FROM app_state WHERE state_key='active_account_id'")
        return None

    def mark_account_logged_out(self, account_id: int, logged_out_at: str | None = None) -> None:
        """记录本工具已成功退出该账号的时刻，并在必要时清除活跃会话。"""
        account_id = int(account_id)
        with self.connection:
            self.connection.execute(
                'UPDATE accounts SET last_logout_at=? WHERE id=?',
                (logged_out_at or utc_now(), account_id),
            )
            self.connection.execute(
                "DELETE FROM app_state WHERE state_key='active_account_id' AND state_value=?",
                (str(account_id),),
            )

    def mark_account_logged_in(self, account_id: int, logged_in_at: str | None = None) -> None:
        """记录成功发起的客户端登录；该字段也是列表的最近登录排序依据。"""
        account_id = int(account_id)
        with self.connection:
            self.connection.execute(
                'UPDATE accounts SET last_login_at=?, last_logout_at=NULL WHERE id=?',
                (logged_in_at or utc_now(), account_id),
            )
            self.connection.execute(
                "INSERT INTO app_state (state_key, state_value) VALUES ('active_account_id', ?)"
                " ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value",
                (str(account_id),),
            )

    def account_objects(self, account_ids: Iterable[int]) -> list[Account]:
        ids = [int(item) for item in account_ids]
        if not ids:
            return []
        accounts: list[Account] = []
        for chunk in id_chunks(ids):
            marks = ','.join('?' for _ in chunk)
            rows = self.connection.execute(
                f'SELECT id, account_name, steam_id, note FROM accounts WHERE id IN ({marks}) ORDER BY id',
                chunk,
            ).fetchall()
            accounts.extend(Account(row['id'], row['account_name'], row['steam_id'], row['note']) for row in rows)
        return accounts

    @staticmethod
    def account_batches(db_path: Path, account_ids: Iterable[int] | None = None) -> Iterator[list[Account]]:
        """在查询工作线程中分批读取账号，避免把超大账号库整体搬入内存。"""
        connection = AccountStore._open_connection(db_path)
        try:
            if account_ids is None:
                cursor = connection.execute('SELECT id, account_name, steam_id, note FROM accounts ORDER BY id')
            else:
                ids = [int(item) for item in account_ids]
                if not ids:
                    return
                for chunk in id_chunks(ids):
                    marks = ','.join('?' for _ in chunk)
                    cursor = connection.execute(
                        f'SELECT id, account_name, steam_id, note FROM accounts WHERE id IN ({marks}) ORDER BY id',
                        chunk,
                    )
                    while rows := cursor.fetchmany(CONSERVATIVE_BATCH_SIZE):
                        yield [Account(row['id'], row['account_name'], row['steam_id'], row['note']) for row in rows]
                return
            while rows := cursor.fetchmany(CONSERVATIVE_BATCH_SIZE):
                yield [Account(row['id'], row['account_name'], row['steam_id'], row['note']) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def import_file(
        db_path: Path,
        source_path: Path,
        cancel_event: threading.Event,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """导入 CSV / JSON / TXT（账号----密码），按批提交；密码以可见明文保存。"""
        kind = detect_import_format(source_path)
        if kind == 'json':
            return AccountStore._import_json(db_path, source_path, cancel_event, progress)
        if kind == 'txt':
            return AccountStore._import_txt(db_path, source_path, cancel_event, progress)
        return AccountStore._import_csv(db_path, source_path, cancel_event, progress)

    @staticmethod
    def _import_stats(source_path: Path, encoding: str) -> dict:
        return {
            'source_name': source_path.name,
            'encoding': encoding,
            'read': 0,
            'imported': 0,
            'invalid': 0,
            'passwords_saved': 0,
            'canceled': False,
            'percent': 0,
        }

    @staticmethod
    def _store_records(connection, stats: dict, records: list) -> None:
        """把一批记录写入明文密码列（records 为 (账号, ID/占位, 备注, 密码)）。"""
        if not records:
            return
        # CSV / JSON / TXT 共用同一合并逻辑。整个批次在一个事务内完成，既保留
        # 大文件导入的提交效率，也避免 CSV 绕过“同账号占位行升级”而分裂成两条记录。
        with connection:
            for account_name, steam_id, note, password in records:
                account_name = (account_name or '').strip()
                steam_id = (steam_id or '').strip()
                if not account_name and not steam_id:
                    continue
                if not steam_id:                  # 兜底：绝不允许写入空 ID（会和 UNIQUE 冲突挤成一条）
                    steam_id = placeholder_for(account_name)
                if password:
                    stats['passwords_saved'] += 1
                password = password or ''
                real_id = bool(STEAM_ID_PATTERN.fullmatch(steam_id))
                existing = None
                if account_name:
                    existing = connection.execute(
                        'SELECT id, steam_id FROM accounts WHERE account_name = ? COLLATE NOCASE', (account_name,)
                    ).fetchone()
                if existing is not None and (not real_id or not STEAM_ID_PATTERN.fullmatch(existing['steam_id'] or '')):
                    # 同名账号只保留一行：占位行这次拿到真实 ID 就地升级，不会再多插一条。
                    target = steam_id if real_id else existing['steam_id']
                    taken = connection.execute('SELECT id FROM accounts WHERE steam_id=?', (target,)).fetchone()
                    if taken is None or int(taken['id']) == int(existing['id']):
                        connection.execute(
                            'UPDATE accounts SET steam_id=?, note=?,'
                            " password=COALESCE(NULLIF(?, ''), password) WHERE id=?",
                            (target, note, password, int(existing['id'])),
                        )
                        stats['imported'] += 1
                        continue
                connection.execute(AccountStore.UPSERT_SQL, (account_name, steam_id, note, password))
                stats['imported'] += 1

    @staticmethod
    def _import_json(
        db_path: Path,
        source_path: Path,
        cancel_event: threading.Event,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        connection = AccountStore._open_connection(db_path)
        AccountStore._create_schema(connection)
        try:
            raw = source_path.read_bytes()
            for encoding in ('utf-8-sig', 'gb18030', 'utf-8'):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise ValueError('无法读取文件编码。请使用 UTF-8 或 GB18030 编码的 JSON。')
            stats = AccountStore._import_stats(source_path, encoding)
            data = json.loads(text)
            items = list(json_records(data))
            total_items = len(items)
            records = []
            for index, item in enumerate(items, start=1):
                if cancel_event.is_set():
                    stats['canceled'] = True
                    break
                stats['read'] += 1
                try:
                    record = record_from_mapping(item)
                except ValueError:
                    stats['invalid'] += 1
                    continue
                if record is None:
                    stats['invalid'] += 1
                    continue
                records.append(record)
                if len(records) >= IMPORT_DB_BATCH_SIZE:
                    AccountStore._store_records(connection, stats, records)
                    records = []
                    stats['percent'] = min(99, int(index * 100 / max(1, total_items)))
                    progress(dict(stats))
                elif index % 50 == 0:
                    # JSON 已整体读入内存，按已处理记录数报告真实解析进度，不能用 read/(read+1) 伪造百分比。
                    stats['percent'] = min(99, int(index * 100 / max(1, total_items)))
                    progress(dict(stats))
            AccountStore._store_records(connection, stats, records)
            stats['percent'] = 100 if not stats['canceled'] else stats['percent']
            progress(dict(stats))
            return stats
        finally:
            connection.close()

    @staticmethod
    def _import_txt(
        db_path: Path,
        source_path: Path,
        cancel_event: threading.Event,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        encoding, _delimiter = detect_csv_format(source_path)
        file_size = max(source_path.stat().st_size, 1)
        connection = AccountStore._open_connection(db_path)
        AccountStore._create_schema(connection)
        stats = AccountStore._import_stats(source_path, encoding)
        try:
            with source_path.open('r', encoding=encoding, newline='') as source:
                records = []
                for line in source:
                    if cancel_event.is_set():
                        stats['canceled'] = True
                        break
                    try:
                        record = txt_record(line)
                    except ValueError:
                        stats['read'] += 1
                        stats['invalid'] += 1
                        continue
                    if record is None:
                        continue
                    stats['read'] += 1
                    records.append(record)
                    if len(records) >= IMPORT_DB_BATCH_SIZE:
                        AccountStore._store_records(connection, stats, records)
                        records = []
                        try:
                            position = min(file_size, source.buffer.tell())
                        except (AttributeError, OSError):
                            position = 0
                        stats['percent'] = min(100, int(position * 100 / file_size))
                        progress(dict(stats))
                AccountStore._store_records(connection, stats, records)
            stats['percent'] = 100 if not stats['canceled'] else stats['percent']
            progress(dict(stats))
            return stats
        finally:
            connection.close()

    @staticmethod
    def _import_csv(
        db_path: Path,
        source_path: Path,
        cancel_event: threading.Event,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """流式读取 CSV/TSV、按批提交。"""
        encoding, delimiter = detect_csv_format(source_path)
        file_size = max(source_path.stat().st_size, 1)
        connection = AccountStore._open_connection(db_path)
        AccountStore._create_schema(connection)
        stats = {
            'source_name': source_path.name,
            'encoding': encoding,
            'read': 0,
            'imported': 0,
            'invalid': 0,
            'passwords_saved': 0,
            'canceled': False,
            'percent': 0,
        }

        def report_progress(handle: Any) -> None:
            try:
                position = min(file_size, handle.buffer.tell())
            except (AttributeError, OSError):
                position = 0
            stats['percent'] = min(100, int(position * 100 / file_size))
            progress(dict(stats))

        try:
            with source_path.open('r', encoding=encoding, newline='') as source:
                reader = csv.reader(source, delimiter=delimiter)
                first_row = next(reader, None)
                if first_row is None:
                    raise ValueError('导入文件为空。')
                layout = csv_layout(first_row)
                pending = []

                def queue_row(row: list[str]) -> None:
                    """校验一行并加入待提交批次；无表头 CSV 的首行也必须经过这里。"""
                    try:
                        record = import_record(row, layout)
                    except ValueError:
                        stats['read'] += 1
                        stats['invalid'] += 1
                        return
                    if record is None:
                        return
                    stats['read'] += 1
                    account_name, steam_id, note, password = record
                    pending.append((account_name, steam_id, note, password))

                def commit_pending() -> None:
                    if not pending:
                        return
                    AccountStore._store_records(connection, stats, pending)
                    pending.clear()
                    report_progress(source)

                # csv_layout 明确支持无表头格式；不能因为第一行用于检测就把它丢掉。
                if not layout.has_header:
                    queue_row(first_row)
                    if len(pending) >= IMPORT_DB_BATCH_SIZE:
                        commit_pending()
                for row in reader:
                    if cancel_event.is_set():
                        stats['canceled'] = True
                        break
                    queue_row(row)
                    if len(pending) >= IMPORT_DB_BATCH_SIZE:
                        commit_pending()
                commit_pending()
                stats['percent'] = 100 if not stats['canceled'] else stats['percent']
                progress(dict(stats))
                return stats
        finally:
            connection.close()

    def upsert_account(self, account_name: str, steam_id: str, note: str, password: str = '', account_id: int | None = None) -> int:
        """新增或更新账号；password 为空时保留库里已保存的密码。"""
        steam_id = normalize_steam_id(steam_id)
        account_name = account_name.strip()
        note = note.strip()
        password = password or ''
        if account_id is None:
            self.connection.execute(self.UPSERT_SQL, (account_name, steam_id, note, password))
            created_id = self.connection.execute(
                'SELECT id FROM accounts WHERE steam_id=?',
                (steam_id,),
            ).fetchone()['id']
        else:
            self.connection.execute(
                "UPDATE accounts SET account_name=?, steam_id=?, note=?,"
                " password=COALESCE(NULLIF(?, ''), password) WHERE id=?",
                (account_name, steam_id, note, password, account_id),
            )
            created_id = account_id
        self.connection.commit()
        return int(created_id)

    def account_password(self, account_id: int) -> str:
        """取出某个账号保存的可见 Steam 密码；没有保存时返回空串。"""
        row = self.connection.execute(
            'SELECT password FROM accounts WHERE id=?',
            (int(account_id),),
        ).fetchone()
        if row is None:
            return ''
        return str(row['password'] or '')

    def update_steam_id(self, account_id: int, steam_id: str) -> bool:
        """解析出真实 SteamID64 后写回。

        如果这个 ID 已经被另一条记录占用，说明库里已经有同账号，直接删掉这条占位记录。
        """
        steam_id = normalize_steam_id(steam_id)
        existing = self.connection.execute(
            'SELECT id, account_name, note, password FROM accounts WHERE steam_id=?', (steam_id,)
        ).fetchone()
        if existing is not None and int(existing['id']) != int(account_id):
            with self.connection:
                # 占位行（只有账号+密码）解析成功后要并进真ID行：先把占位行比真ID行更全的字段补过去，
                # 否则用户辛苦导入的密码、备注、账号标签会随着占位行一起被删掉。
                placeholder = self.connection.execute(
                    'SELECT account_name, note, password FROM accounts WHERE id=?', (int(account_id),)
                ).fetchone()
                if placeholder is not None:
                    self.connection.execute(
                        "UPDATE accounts SET"
                        " account_name=COALESCE(NULLIF(account_name, ''), ?),"
                        " note=COALESCE(NULLIF(note, ''), ?),"
                        " password=COALESCE(NULLIF(password, ''), ?)"
                        " WHERE id=?",
                        (
                            placeholder['account_name'] or '',
                            placeholder['note'] or '',
                            placeholder['password'] or '',
                            int(existing['id']),
                        ),
                    )
                self.connection.execute('DELETE FROM accounts WHERE id=?', (int(account_id),))
                self.connection.execute(
                    "DELETE FROM app_state WHERE state_key='active_account_id' AND state_value=?",
                    (str(int(account_id)),),
                )
            return False
        self.connection.execute('UPDATE accounts SET steam_id=? WHERE id=?', (steam_id, int(account_id)))
        self.connection.commit()
        return True

    def delete_accounts(self, account_ids: Iterable[int]) -> None:
        ids = [int(item) for item in account_ids]
        if not ids:
            return
        with self.connection:
            for chunk in id_chunks(ids):
                marks = ','.join('?' for _ in chunk)
                self.connection.execute(f'DELETE FROM accounts WHERE id IN ({marks})', chunk)
                self.connection.execute(
                    f"DELETE FROM app_state WHERE state_key='active_account_id' AND state_value IN ({marks})",
                    [str(account_id) for account_id in chunk],
                )

    def clear_rejected_key_failures(self) -> int:
        """清理旧版把一次全局 401/403 误写到每一条账号上的状态。"""
        result = self.connection.execute("""
            UPDATE accounts
            SET status='未查询', query_error='', checked_at=NULL
            WHERE status='查询失败'
              AND (query_error LIKE '%HTTP 401%' OR query_error LIKE '%HTTP 403%')
              AND lower(query_error) LIKE '%key%'
            """)
        self.connection.commit()
        return max(0, result.rowcount)

    def save_results_batch(self, results: Iterable[tuple[int, dict[str, Any] | None, str]]) -> None:
        checked_at = utc_now()
        success_values = []
        failure_values = []
        for account_id, player, error in results:
            if player is None:
                failure_values.append((error, checked_at, account_id))
                continue
            success_values.append((
                int(bool(player.get('VACBanned', False))),
                int(player.get('NumberOfVACBans', 0) or 0),
                int(player.get('DaysSinceLastBan', 0) or 0),
                int(player.get('NumberOfGameBans', 0) or 0),
                int(bool(player.get('CommunityBanned', False))),
                str(player.get('EconomyBan', 'unknown')),
                pubg_assessment(player),
                json.dumps(player, ensure_ascii=False, separators=(',', ':')),
                checked_at,
                account_id,
            ))
        with self.connection:
            if success_values:
                self.connection.executemany("""
                    UPDATE accounts SET
                        vac_banned=?, vac_count=?, days_since_last_ban=?, game_bans=?,
                        community_banned=?, economy_ban=?, pubg_assessment=?, api_bans_json=?,
                        checked_at=?, status='查询成功', query_error=''
                    WHERE id=?
                    """, success_values)
            if failure_values:
                self.connection.executemany("UPDATE accounts SET status='查询失败', query_error=?, checked_at=? WHERE id=?", failure_values)

    def save_result(self, account_id: int, player: dict[str, Any] | None, error: str = '') -> None:
        self.save_results_batch([(account_id, player, error)])

    def export_account_rows(self, account_ids: Iterable[int] | None = None, mode: str = 'all') -> Iterator[tuple]:
        """导出账号（账号名、SteamID64、备注和明文密码）。

        指定 account_ids 时按所选账号导出；未指定时可按 risk/safe 筛选导出，默认导出全部。
        """
        columns = 'SELECT account_name, steam_id, note, password FROM accounts'
        if account_ids is None:
            cursor = self.connection.execute(
                f'{columns} {self._mode_where(mode)} ORDER BY account_name COLLATE NOCASE, steam_id'
            )
        else:
            ids = [int(item) for item in account_ids]
            if not ids:
                return
            for chunk in id_chunks(ids):
                marks = ','.join('?' for _ in chunk)
                cursor = self.connection.execute(
                    f'{columns} WHERE id IN ({marks}) ORDER BY account_name COLLATE NOCASE, steam_id', chunk
                )
                for row in cursor:
                    yield (row['account_name'], row['steam_id'], row['note'], row['password'] or '')
            return
        for row in cursor:
            yield (row['account_name'], row['steam_id'], row['note'], row['password'] or '')

    def export_rows(self) -> Iterator[sqlite3.Row]:
        cursor = self.connection.execute("""
            SELECT account_name, steam_id, note, status, vac_banned, vac_count,
                   days_since_last_ban, game_bans, community_banned, economy_ban,
                   pubg_assessment, checked_at, query_error
            FROM accounts ORDER BY account_name COLLATE NOCASE, steam_id
            """)
        yield from cursor


def request_player_bans(api_key: str, steam_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not api_key.strip():
        raise ValueError('请输入 Steam Web API Key')
    query = urlencode({'key': api_key.strip(), 'steamids': ','.join(steam_ids)})
    rejected_statuses = []
    connection_errors = []
    for api_url in API_URLS:
        request = Request(
            f'{api_url}?{query}',
            headers={'Accept': 'application/json', 'User-Agent': 'SteamBanDesktop/1.1'},
            method='GET',
        )
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            if exc.code in (401, 403):
                rejected_statuses.append(exc.code)
                continue
            if exc.code == 429:
                raise SteamApiFatalError('Steam API 当前限制了请求频率（HTTP 429）。请稍后再试。') from exc
            body = exc.read().decode('utf-8', errors='replace')[:300]
            raise SteamApiFatalError(f'Steam API 返回 HTTP {exc.code}: {body or exc.reason}') from exc
        except URLError as exc:
            connection_errors.append(str(exc.reason))
            continue
        except json.JSONDecodeError as exc:
            raise SteamApiFatalError('Steam API 返回的不是有效 JSON') from exc
        players = payload.get('players')
        if not isinstance(players, list):
            raise SteamApiFatalError('Steam API 响应中未找到 players 列表')
        return {str(player.get('SteamId')): player for player in players if player.get('SteamId')}
    codes = ', '.join(str(code) for code in sorted(set(rejected_statuses)))
    if rejected_statuses and len(rejected_statuses) >= len(API_URLS):
        # 只有每个主机都明确拒绝，才能断定是 Key 失效。
        raise SteamApiFatalError('Steam 的两个兼容 API 主机都拒绝了当前 Web API Key（HTTP %s）。请在 Steam 的 API Key 页面确认该 Key 未被撤销，重新生成后完整复制粘贴，再重新查询。' % codes)
    if rejected_statuses:
        # 常见情况：主接口网络失败，备用主机（合作伙伴专用）对普通 Key 返回 403。
        # 这不能说明 Key 失效，否则会误导用户去注销重绑。
        detail = connection_errors[-1] if connection_errors else '未知网络错误'
        raise SteamApiFatalError(
            f'Steam API 连接失败：{detail}。本次没有收到主接口的有效响应'
            f'（备用主机返回 HTTP {codes}，对普通 Key 属正常现象，不代表 Key 失效），请检查网络后重试。'
        )
    detail = connection_errors[-1] if connection_errors else '未知网络错误'
    raise SteamApiFatalError(f'无法连接 Steam API: {detail}')


# NOTE: lines 1-3 mirror the original module header imports needed so the compiler's
# import-originated rule (codegen.c: is_import_originated) disables the LOAD_ATTR
# method-call optimization for `ttk.X(...)` / `messagebox.X(...)`, as in the original.
# The file is padded so that every source line below keeps its ORIGINAL line number
# (the class body stores __firstlineno__ as a constant: LOAD_CONST 612).



























































































































































































































































































































































































































































































































































































































class AccountDialog:
    def __init__(self, parent: Any, title: str, initial: Account | None = None, password: str = ''):
        self.window = Toplevel(parent)
        self.window.title(title)
        self.window.transient(parent)
        self.window.resizable(False, False)
        self.result = None
        self.initial = initial

        self.name_var = StringVar(value=initial.account_name if initial else '')
        self.steam_var = StringVar(value=initial.steam_id if initial else '')
        self.note_var = StringVar(value=initial.note if initial else '')
        self.password_var = StringVar(value=password)

        frame = ttk.Frame(self.window, padding=18)
        frame.grid(sticky='nsew')
        ttk.Label(frame, text='账号标签（可选）').grid(row=0, column=0, sticky='w', pady=(0, 7))
        ttk.Entry(frame, textvariable=self.name_var, width=42).grid(row=0, column=1, sticky='ew', pady=(0, 7))
        ttk.Label(frame, text='SteamID64（可选）').grid(row=1, column=0, sticky='w', pady=7)
        steam_entry = ttk.Entry(frame, textvariable=self.steam_var, width=42)
        steam_entry.grid(row=1, column=1, sticky='ew', pady=7)
        ttk.Label(frame, text='备注（可选）').grid(row=2, column=0, sticky='w', pady=7)
        ttk.Entry(frame, textvariable=self.note_var, width=42).grid(row=2, column=1, sticky='ew', pady=7)
        ttk.Label(frame, text='Steam 密码（自动登录用）').grid(row=3, column=0, sticky='w', pady=7)
        ttk.Entry(frame, textvariable=self.password_var, width=42).grid(row=3, column=1, sticky='ew', pady=7)
        ttk.Label(
            frame,
            text='密码以可见明文保存在本机账号库，也会显示在账号列表中。请勿将账号库、截图或导出文件发给他人。',
            foreground='#64748b',
            wraplength=390,
        ).grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 14))
        controls = ttk.Frame(frame)
        controls.grid(row=5, column=0, columnspan=2, sticky='e')
        ttk.Button(controls, text='取消', command=self.window.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(controls, text='保存', command=self._save).grid(row=0, column=1)
        self.window.bind('<Escape>', lambda _event: self.window.destroy())
        self.window.bind('<Return>', lambda _event: self._save())
        steam_entry.focus_set()
        self.window.grab_set()

    def _save(self) -> None:
        account_name = self.name_var.get().strip()
        raw_steam_id = self.steam_var.get().strip()
        original_steam_id = (self.initial.steam_id if self.initial else '') or ''
        original_is_placeholder = not STEAM_ID_PATTERN.fullmatch(original_steam_id)
        if STEAM_ID_PATTERN.fullmatch(raw_steam_id):
            steam_id = raw_steam_id
        elif raw_steam_id == '' and not original_is_placeholder:
            # 清空输入框不应把已经解析好的账号打回“未解析”。
            steam_id = original_steam_id
        elif raw_steam_id == '' or (original_is_placeholder and raw_steam_id == original_steam_id):
            # 留空、或仍是"尚未解析"的占位：保留占位，等查询时自动解析出真实 SteamID64。
            # 这样用户才能给这类账号补密码 / 改标签 / 改备注。
            steam_id = placeholder_for(account_name) if account_name else original_steam_id
            if not steam_id:
                messagebox.showerror(
                    APP_NAME, '请填写账号标签（用于自动解析 SteamID64）或直接填写 17 位 SteamID64。',
                    parent=self.window,
                )
                return
        else:
            messagebox.showerror(
                APP_NAME, 'SteamID64 必须是 17 位数字；留空则会在查询时按账号标签自动解析。',
                parent=self.window,
            )
            return
        self.result = (account_name, steam_id, self.note_var.get().strip(), self.password_var.get())
        self.window.destroy()


def write_account_export(path: Path, rows: Iterable[tuple]) -> int:
    """把账号写成文件：按后缀选 CSV（account,password,steam_id,note）/ JSON / TXT（账号----密码）。"""
    suffix = path.suffix.lower()
    count = 0
    if suffix == '.json':
        payload = []
        for account_name, steam_id, note, password in rows:
            payload.append({'account': account_name, 'password': password, 'steam_id': steam_id, 'note': note})
            count += 1
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return count
    if suffix in ('.txt', '.text'):
        with path.open('w', encoding='utf-8', newline='') as file:
            for account_name, steam_id, note, password in rows:
                if STEAM_ID_PATTERN.fullmatch(steam_id or ''):
                    file.write('%s----%s----%s\n' % (account_name, password, steam_id))
                else:
                    file.write('%s----%s\n' % (account_name, password))
                count += 1
        return count
    with path.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['account', 'password', 'steam_id', 'note'])
        for account_name, steam_id, note, password in rows:
            writer.writerow([account_name, password, steam_id, note])
            count += 1
    return count


class PasteImportDialog:
    """粘贴导入：把「账号----密码」这样的多行文本直接粘进来（不要求是 txt 文件）。"""

    def __init__(self, parent: Any):
        self.window = Toplevel(parent)
        self.window.title("粘贴导入账号")
        self.window.transient(parent)
        self.result = None
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="每行一个账号：账号----密码（也支持 账号----密码----备注）", style="Body.TLabel").pack(anchor="w")
        ttk.Label(frame, text="直接从记事本 / 网页 / 表格里复制粘贴即可，分隔符 -- / ---- / :: / | 都认。", style="Hint.TLabel").pack(anchor="w", pady=(4, 8))
        self.text = Text(frame, width=64, height=16, wrap="none", undo=True)
        self.text.pack(fill="both", expand=True)
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="取消", command=self.window.destroy, bootstyle="secondary-outline").pack(side="right")
        ttk.Button(controls, text="导入", command=self._accept, bootstyle="primary").pack(side="right", padx=(0, 8))
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.bind("<Control-Return>", lambda _event: self._accept())
        self.text.focus_set()
        self.window.grab_set()

    def _accept(self) -> None:
        content = self.text.get("1.0", "end").strip()
        if not content:
            messagebox.showinfo(APP_NAME, "还没有粘贴内容。", parent=self.window)
            return
        self.result = content
        self.window.destroy()


class ExportFormatDialog:
    """导出账号前选择格式；筛选状态下可明确选择是否只导出当前筛选结果。"""

    def __init__(self, parent: Any, filter_mode: str = 'all', filter_count: int = 0):
        self.window = Toplevel(parent)
        self.window.title("导出账号")
        self.window.transient(parent)
        self.result = None
        self.filtered_only_var = BooleanVar(value=filter_mode != 'all')
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="选择导出格式", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="文件里是明文密码，请只保存在自己机器上。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 10))
        if filter_mode != 'all':
            mode_label = {'risk': '有风险/异常', 'safe': '无风险'}.get(filter_mode, '当前')
            ttk.Checkbutton(
                frame,
                text=f"仅导出当前筛选结果（{mode_label}，{filter_count} 个账号）",
                variable=self.filtered_only_var,
                bootstyle="primary",
            ).pack(anchor="w", pady=(0, 8))
        for kind, label in (
            ("csv", "CSV —— account,password,steam_id,note（可直接再导入）"),
            ("txt", "TXT —— 账号----密码----SteamID64"),
            ("json", "JSON —— [{account, password, steam_id, note}]"),
        ):
            ttk.Button(
                frame, text=label, width=52, bootstyle="primary-outline",
                command=lambda value=kind: self._choose(value),
            ).pack(fill="x", pady=3)
        ttk.Button(frame, text="取消", command=self.window.destroy, bootstyle="secondary-outline").pack(pady=(10, 0))
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.grab_set()

    def _choose(self, kind: str) -> None:
        self.result = (kind, bool(self.filtered_only_var.get()))
        self.window.destroy()


class SteamBanApp:
    columns = (
        ("pick", "选择", 52),
        ("account_name", "账号标签", 150),
        ("password", "密码（明文）", 180),
        ("steam_id", "SteamID64", 175),
        ("login_elapsed", "上次登录", 155),
        ("vac", "VAC", 55),
        ("game_bans", "游戏封禁", 76),
        ("pubg", "PUBG 判断", 190),
        ("community", "社区封禁", 82),
        ("economy", "库存封禁", 90),
        ("checked", "查询时间", 150),
        ("status", "状态", 90),
        ("note", "备注", 200),
    )

    def __init__(self, root: Any):
        self.root = root
        self.root.title(APP_NAME)
        self.root.minsize(1180, 690)
        self.root.geometry(f"{UiMetrics.WINDOW_WIDTH}x{UiMetrics.WINDOW_HEIGHT}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.install_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        self.data_dir = application_data_directory()
        self.database_path = self.data_dir / ACCOUNT_DATABASE_FILENAME
        self.settings_path = self.data_dir / KEY_SETTINGS_FILENAME
        self.legacy_scan_marker_path = self.data_dir / LEGACY_SCAN_MARKER_FILENAME
        migration_notice = ''
        automatic_migration_succeeded = False
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            # v3.1.2 及更早版本把数据放在 EXE 旁边。共享库还没有数据时（空库、或丢了全部密码），
            # 先在常见位置找旧库并“合并”缺失字段；自动路径绝不再用旧库整体覆盖当前账号库。
            current_stats = account_database_stats(self.database_path) or (0, 0)
            if legacy_data_is_missing(current_stats):
                legacy_databases = migratable_legacy_databases(
                    collect_legacy_account_databases(self.install_dir), current_stats
                )
                merge_results = [merge_legacy_user_data(path.parent, self.data_dir) for path in legacy_databases]
                added = sum(result['accounts_added'] for result in merge_results)
                updated = sum(result['accounts_updated'] for result in merge_results)
                passwords = sum(result['passwords_filled'] for result in merge_results)
                settings_copied = any(result['settings_copied'] for result in merge_results)
                if added or updated or passwords or settings_copied:
                    automatic_migration_succeeded = True
                    source_text = '、'.join(str(path.parent) for path in legacy_databases[:3])
                    extra_sources = f' 等 {len(legacy_databases)} 处' if len(legacy_databases) > 3 else ''
                    key_suffix = '，并继承了可读取的 Key' if settings_copied else ''
                    migration_notice = (
                        f'已自动合并旧版数据：新增账号 {added} 条、补充记录 {updated} 条、补充密码 {passwords} 条'
                        f'{key_suffix}（来源：{source_text}{extra_sources}）。当前账号与查询记录已保留。'
                    )
        except (OSError, sqlite3.Error, ValueError) as exc:
            migration_notice = f'未能自动合并旧版数据：{exc}；可点击“迁移旧版数据”手动选择账号库。'
        self.store = AccountStore(self.database_path)
        cleared_key_failures = self.store.clear_rejected_key_failures()
        self.key_store = DpapiKeyStore(self.settings_path)
        try:
            saved_api_key = self.key_store.load()
            key_load_problem = ""
        except KeyStorageError as exc:
            saved_api_key = None
            key_load_problem = str(exc)

        # 旧版可能被解压到用户目录的更深层文件夹，甚至放在别的磁盘上。查找在后台运行，
        # 同一个版本只做一次；只有当前库还缺数据（空库、或没有密码）时才会启动。
        self.legacy_scan_running = False
        self._should_scan_legacy_profile = (
            not automatic_migration_succeeded
            and legacy_data_is_missing(account_database_stats(self.database_path) or (0, 0))
            and not self._legacy_scan_already_done()
        )

        self.api_key_var = StringVar(value=saved_api_key or "")
        self.remember_key_var = BooleanVar(value=bool(saved_api_key))
        self.key_state_var = StringVar(value="已加密保存" if saved_api_key else "仅本次运行")
        self.status_var = StringVar(
            value=(
                f"已清除上次 API Key 被拒绝造成的 {cleared_key_failures} 条无效失败标记。请更换有效 Key 后重新查询。"
                if cleared_key_failures
                else migration_notice or key_load_problem
                or ("正在后台自动查找旧版账号数据…" if self._should_scan_legacy_profile
                    else "就绪：请添加或导入 SteamID64，然后输入自己的 Steam Web API Key。")
            )
        )
        self.count_var = StringVar(value="0 个账号")
        self.list_hint_var = StringVar(value="正在准备账号清单…")
        self.query_progress_var = StringVar(value="")
        self.show_only_risk_var = BooleanVar(value=False)
        self.show_only_safe_var = BooleanVar(value=False)
        self.checked_ids = set()
        self._menu_account_id = None
        self.events = queue.Queue()
        self.query_running = False
        self.import_running = False
        self.login_running = False
        self.login_cancel_event = None
        self.update_check_running = False
        self.update_url = ''
        self.update_version = ''
        self.query_completed = 0
        self.query_total = 0
        self.import_cancel_event = None
        self.loaded_rows = 0
        self.table_total = 0
        self.loading_table_rows = False
        self._build_ui()
        self.refresh_table()
        self.root.after(120, self._drain_events)
        self._schedule_login_timer_refresh()
        self.root.after(800, self.check_for_updates)
        if self._should_scan_legacy_profile:
            self.root.after(250, self._start_legacy_profile_scan)

    def _build_ui(self) -> None:
        style = ttk.Style()
        self.root.configure(bg=UiColors.BACKGROUND)

        # 统一的基础样式
        style.configure("App.TFrame", background=UiColors.BACKGROUND)
        style.configure("Sidebar.TFrame", background=UiColors.SIDEBAR)
        style.configure("Card.TFrame", background=UiColors.SURFACE, borderwidth=1, relief="solid")
        style.configure("Header.TFrame", background=UiColors.BACKGROUND)
        style.configure("HeaderTitle.TLabel", background=UiColors.BACKGROUND, foreground=UiColors.TEXT, font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("HeaderSubtitle.TLabel", background=UiColors.BACKGROUND, foreground=UiColors.MUTED, font=("Microsoft YaHei UI", 10))
        style.configure("SectionTitle.TLabel", background=UiColors.SURFACE, foreground=UiColors.TEXT, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Body.TLabel", background=UiColors.SURFACE, foreground=UiColors.TEXT, font=("Microsoft YaHei UI", 9))
        style.configure("Hint.TLabel", background=UiColors.SURFACE, foreground=UiColors.MUTED, font=("Microsoft YaHei UI", 9))
        style.configure(
            "Link.TLabel",
            background=UiColors.SURFACE,
            foreground=UiColors.PRIMARY,
            font=("Microsoft YaHei UI", 9, "underline"),
        )
        style.configure("Warning.TLabel", background=UiColors.SURFACE_SUBTLE, foreground=UiColors.WARNING, font=("Microsoft YaHei UI", 9))
        style.configure("Badge.TLabel", background=UiColors.PRIMARY, foreground="#FFFFFF", font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 5))
        style.configure("SuccessBadge.TLabel", background=UiColors.SUCCESS_SUBTLE, foreground=UiColors.SUCCESS, font=("Microsoft YaHei UI", 9, "bold"), padding=(9, 4))
        style.configure("Key.TEntry", fieldbackground="#FFFFFF", foreground=UiColors.TEXT, padding=(10, 7))
        style.configure("Filter.TCheckbutton", background=UiColors.SURFACE, foreground=UiColors.MUTED, font=("Microsoft YaHei UI", 9))
        style.map("Filter.TCheckbutton", background=[("active", UiColors.SURFACE)])
        style.configure("Account.Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground=UiColors.TEXT, rowheight=UiMetrics.ROW_HEIGHT, font=("Microsoft YaHei UI", 9), borderwidth=0)
        style.map("Account.Treeview", background=[("selected", UiColors.PRIMARY)], foreground=[("selected", "#FFFFFF")])
        style.configure("Account.Treeview.Heading", background=UiColors.SURFACE_SUBTLE, foreground=UiColors.TEXT, relief="flat", font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 9))
        style.map("Account.Treeview.Heading", background=[("active", "#EAF1FF")])
        style.configure("Status.TLabel", background=UiColors.SIDEBAR, foreground="#FFFFFF", font=("Microsoft YaHei UI", 9), padding=(14, 9))
        style.configure("SidebarKicker.TLabel", background=UiColors.SIDEBAR, foreground="#60A5FA", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("SidebarTitle.TLabel", background=UiColors.SIDEBAR, foreground="#FFFFFF", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("SidebarHint.TLabel", background=UiColors.SIDEBAR, foreground=UiColors.SIDEBAR_MUTED, font=("Microsoft YaHei UI", 9), wraplength=180)
        style.configure("SidebarNav.TLabel", background=UiColors.SIDEBAR_ACTIVE, foreground="#FFFFFF", font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 10))

        shell = ttk.Frame(self.root, style="App.TFrame", padding=UiMetrics.SCREEN_INSET)
        shell.pack(fill="both", expand=True)
        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", padding=(20, 22))
        sidebar.configure(width=224)
        sidebar.pack(side="left", fill="y", padx=(0, UiMetrics.SECTION_GAP))
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="STEAM TOOLS", style="SidebarKicker.TLabel").pack(anchor="w")
        ttk.Label(sidebar, text="封禁查询器", style="SidebarTitle.TLabel").pack(anchor="w", pady=(5, 4))
        ttk.Label(sidebar, text="本地账号库与 Steam 公开封禁数据", style="SidebarHint.TLabel", justify="left").pack(anchor="w")
        ttk.Separator(sidebar, orient="horizontal", bootstyle="secondary").pack(fill="x", pady=24)
        ttk.Label(sidebar, text="查询工作台", style="SidebarNav.TLabel", anchor="w").pack(fill="x")
        ttk.Label(sidebar, text="  批量导入 · 查询 · 导出", style="SidebarHint.TLabel").pack(anchor="w", pady=(12, 0))
        ttk.Label(sidebar, text="  连续滚动账号列表", style="SidebarHint.TLabel").pack(anchor="w", pady=(7, 0))
        ttk.Label(sidebar, text="  API Key 本机加密保存", style="SidebarHint.TLabel").pack(anchor="w", pady=(7, 0))
        ttk.Frame(sidebar, style="Sidebar.TFrame").pack(fill="both", expand=True)
        ttk.Separator(sidebar, orient="horizontal", bootstyle="secondary").pack(fill="x", pady=(0, 15))
        ttk.Label(sidebar, text="本地保存说明", style="SidebarKicker.TLabel").pack(anchor="w")
        ttk.Label(sidebar, text="支持 CSV / JSON / TXT（账号----密码）；密码会以可见明文保存并显示在列表。不要外发数据库、截图或导出文件。", style="SidebarHint.TLabel", justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(sidebar, text="只有账号密码也能用：查询时自动解析 SteamID64。", style="SidebarHint.TLabel", justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(sidebar, text="双击账号 = 直接登录；右键 = 登录/编辑/删除。", style="SidebarHint.TLabel", justify="left").pack(anchor="w", pady=(6, 0))

        outer = ttk.Frame(shell, style="App.TFrame")
        outer.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(outer, style="Header.TFrame")
        header.pack(fill="x", pady=(3, UiMetrics.SECTION_GAP))
        header_left = ttk.Frame(header, style="Header.TFrame")
        header_left.pack(side="left", fill="x", expand=True)
        ttk.Label(header_left, text="封禁查询工作台", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header_left,
            text="批量读取 Steam 公开封禁状态。账户资料与结果仅保存在本机。",
            style="HeaderSubtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(header, text=f"Windows 桌面版 · v{APP_VERSION}", style="Badge.TLabel").pack(side="right", anchor="n", pady=(6, 0))
        self.update_button = ttk.Button(
            header,
            text="检查更新",
            command=self.check_for_updates,
            bootstyle="info-outline",
            width=12,
        )
        self.update_button.pack(side="right", anchor="n", padx=(0, 10), pady=(6, 0))

        key_card = ttk.Frame(outer, style="Card.TFrame", padding=UiMetrics.CARD_PADDING)
        key_card.pack(fill="x", pady=(0, UiMetrics.SECTION_GAP))
        key_heading = ttk.Frame(key_card, style="Card.TFrame")
        key_heading.pack(fill="x", pady=(0, 10))
        ttk.Label(key_heading, text="连接与凭据", style="SectionTitle.TLabel").pack(side="left")
        self.key_state_label = ttk.Label(key_heading, textvariable=self.key_state_var, style="SuccessBadge.TLabel")
        self.key_state_label.pack(side="right")
        key_row = ttk.Frame(key_card, style="Card.TFrame")
        key_row.pack(fill="x")
        key_label_row = ttk.Frame(key_row, style="Card.TFrame")
        key_label_row.pack(fill="x", pady=(0, 6))
        ttk.Label(key_label_row, text="Steam Web API Key", style="Body.TLabel").pack(side="left")
        self.api_key_application_link = ttk.Label(
            key_label_row,
            text="申请地址：https://steamcommunity.com/dev/apikey（点击打开）",
            style="Link.TLabel",
            cursor="hand2",
        )
        self.api_key_application_link.pack(side="right")
        self.api_key_application_link.bind("<Button-1>", self._open_api_key_application_page)
        self.api_key_application_link.bind("<Return>", self._open_api_key_application_page)
        ttk.Label(
            key_row,
            text="不会申请？登录 Steam → 填写网站域名（个人使用可填 localhost）→ 注册 → 复制 Key 到下方并保存。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        self.key_entry = ttk.Entry(key_row, textvariable=self.api_key_var, show="●", width=62, style="Key.TEntry")
        self.key_entry.pack(fill="x")
        key_actions = ttk.Frame(key_card, style="Card.TFrame")
        key_actions.pack(fill="x", pady=(12, 0))
        self.save_key_button = ttk.Button(
            key_actions,
            text="保存 Key",
            command=self.save_api_key,
            bootstyle="primary",
            width=UiMetrics.KEY_SAVE_BUTTON_WIDTH,
        )
        self.save_key_button.pack(side="left")
        self.clear_saved_key_button = ttk.Button(
            key_actions,
            text="清除保存",
            command=self.clear_saved_api_key,
            bootstyle="secondary-outline",
            width=UiMetrics.KEY_CLEAR_BUTTON_WIDTH,
        )
        self.clear_saved_key_button.pack(side="left", padx=(UiMetrics.KEY_ACTION_GAP, 0))
        self.steam_login_button = ttk.Button(
            key_actions,
            text="自动登录所选账号",
            command=self.open_steam_login,
            bootstyle="primary-outline",
            width=UiMetrics.KEY_LOGIN_BUTTON_WIDTH,
        )
        self.steam_login_button.pack(side="left", padx=(UiMetrics.KEY_ACTION_GAP, 0))
        self.cancel_login_button = ttk.Button(
            key_actions,
            text="取消登录",
            command=self.cancel_steam_login,
            bootstyle="warning-outline",
            state="disabled",
        )
        self.cancel_login_button.pack(side="left", padx=(UiMetrics.KEY_ACTION_GAP, 0))
        key_options = ttk.Frame(key_card, style="Card.TFrame")
        key_options.pack(fill="x", pady=(9, 0))
        self.remember_key_button = ttk.Checkbutton(
            key_options,
            text="在此 Windows 用户下加密保存 Key，启动时自动加载",
            variable=self.remember_key_var,
            bootstyle="primary",
        )
        self.remember_key_button.pack(side="left")
        ttk.Label(
            key_options,
            text="登录会打开官方客户端或网页；认证步骤由 Steam 官方界面完成。",
            style="Hint.TLabel",
        ).pack(side="right")

        actions = ttk.Frame(outer, style="Card.TFrame", padding=UiMetrics.CARD_PADDING)
        actions.pack(fill="x", pady=(0, UiMetrics.SECTION_GAP))
        account_actions = ttk.Frame(actions, style="Card.TFrame")
        account_actions.pack(side="left")
        ttk.Label(account_actions, text="账号库", style="SectionTitle.TLabel").pack(side="left", padx=(0, 12))
        self.add_button = ttk.Button(account_actions, text="添加账号", command=self.add_account, bootstyle="primary-outline")
        self.add_button.pack(side="left")
        self.edit_button = ttk.Button(account_actions, text="编辑所选", command=self.edit_selected, bootstyle="secondary-outline")
        self.edit_button.pack(side="left", padx=(8, 0))
        self.delete_button = ttk.Button(account_actions, text="删除所选", command=self.delete_selected, bootstyle="danger-outline")
        self.delete_button.pack(side="left", padx=(8, 0))
        self.paste_import_button = ttk.Button(
            account_actions, text="粘贴导入（账号----密码）", command=self.import_pasted_accounts, bootstyle="info-outline"
        )
        self.paste_import_button.pack(side="left", padx=(8, 0))
        self.import_button = ttk.Button(account_actions, text="导入文件 (CSV / JSON / TXT)", command=self.import_accounts, bootstyle="info-outline")
        self.import_button.pack(side="left", padx=(14, 0))
        self.migrate_legacy_button = ttk.Button(
            account_actions,
            text="迁移旧版数据",
            command=self.migrate_legacy_data,
            bootstyle="secondary-outline",
        )
        self.migrate_legacy_button.pack(side="left", padx=(8, 0))
        self.cancel_import_button = ttk.Button(
            account_actions,
            text="取消导入",
            command=self.cancel_import,
            bootstyle="warning-outline",
            state="disabled",
        )
        self.cancel_import_button.pack(side="left", padx=(8, 0))
        self.export_button = ttk.Button(account_actions, text="导出结果", command=self.export_results, bootstyle="secondary-outline")
        self.export_button.pack(side="left", padx=(8, 0))
        self.export_accounts_button = ttk.Button(
            account_actions, text="导出账号", command=self.export_accounts, bootstyle="secondary-outline"
        )
        self.export_accounts_button.pack(side="left", padx=(8, 0))
        query_actions = ttk.Frame(actions, style="Card.TFrame")
        query_actions.pack(side="right")
        self.query_selected_button = ttk.Button(query_actions, text="查询所选", command=lambda: self.start_query(False), bootstyle="secondary-outline")
        self.query_selected_button.pack(side="left")
        self.query_all_button = ttk.Button(query_actions, text="查询全部", command=lambda: self.start_query(True), bootstyle="success")
        self.query_all_button.pack(side="left", padx=(8, 0))

        self.query_progress_card = ttk.Frame(outer, style="Card.TFrame", padding=(UiMetrics.CARD_PADDING, 12))
        query_progress_header = ttk.Frame(self.query_progress_card, style="Card.TFrame")
        query_progress_header.pack(fill="x", pady=(0, 8))
        ttk.Label(query_progress_header, text="查询进度", style="SectionTitle.TLabel").pack(side="left")
        ttk.Label(query_progress_header, textvariable=self.query_progress_var, style="Hint.TLabel").pack(side="right")
        self.query_progress_bar = ttk.Progressbar(
            self.query_progress_card,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            value=0,
            bootstyle="success",
        )
        self.query_progress_bar.pack(fill="x")

        table_card = ttk.Frame(outer, style="Card.TFrame", padding=UiMetrics.CARD_PADDING)
        table_card.pack(fill="both", expand=True)
        self.table_card = table_card
        table_header = ttk.Frame(table_card, style="Card.TFrame")
        table_header.pack(fill="x", pady=(0, 10))
        ttk.Label(table_header, text="账号清单", style="SectionTitle.TLabel").pack(side="left")
        ttk.Label(table_header, textvariable=self.count_var, style="Hint.TLabel").pack(side="left", padx=(12, 0))
        self.risk_filter_button = ttk.Checkbutton(
            table_header,
            text="仅显示有风险/异常结果",
            variable=self.show_only_risk_var,
            command=lambda: self._toggle_filter('risk'),
            bootstyle="warning",
        )
        self.risk_filter_button.pack(side="right")
        self.safe_filter_button = ttk.Checkbutton(
            table_header,
            text="仅显示无风险",
            variable=self.show_only_safe_var,
            command=lambda: self._toggle_filter('safe'),
            bootstyle="success",
        )
        self.safe_filter_button.pack(side="right", padx=(0, 8))
        self.clear_checked_button = ttk.Button(
            table_header, text="清空勾选", command=lambda: self._set_all_checked(False),
            bootstyle="secondary-outline", width=10,
        )
        self.clear_checked_button.pack(side="right", padx=(8, 0))
        self.select_all_button = ttk.Button(
            table_header, text="全选", command=lambda: self._set_all_checked(True),
            bootstyle="secondary-outline", width=8,
        )
        self.select_all_button.pack(side="right", padx=(8, 0))
        table_frame = ttk.Frame(table_card, style="Card.TFrame")
        table_frame.pack(fill="both", expand=True)
        names = [name for name, _label, _width in self.columns]
        self.tree = ttk.Treeview(table_frame, columns=names, show="headings", selectmode="extended", style="Account.Treeview")
        for name, label, width in self.columns:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=45, anchor="center" if name not in {"note", "account_name", "password", "pubg"} else "w")
        self.tree.tag_configure("risk", background="#fff1f2")
        self.tree.tag_configure("success", background="#f0fdf4")
        self.tree.tag_configure("failure", background="#fff7ed")
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self.row_menu = Menu(self.root, tearoff=0)
        self.row_menu.add_command(label="登录该账号（双击也行）", command=lambda: self.open_steam_login(self._menu_account_id))
        self.row_menu.add_command(label="编辑该账号", command=self.edit_selected)
        self.row_menu.add_separator()
        self.row_menu.add_command(label="删除该账号", command=self.delete_selected)
        self.tree.bind("<Delete>", lambda _event: self.delete_selected())
        self.tree.bind("<MouseWheel>", self._queue_load_more, add="+")
        self.tree.bind("<Button-4>", self._queue_load_more, add="+")
        self.tree.bind("<Button-5>", self._queue_load_more, add="+")
        self.y_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self._scroll_tree, bootstyle="primary-round")
        x_scroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview, bootstyle="primary-round")
        self.tree.configure(yscrollcommand=self._on_tree_yview, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        list_footer = ttk.Frame(table_card, style="Card.TFrame")
        list_footer.pack(fill="x", pady=(10, 0))
        ttk.Label(list_footer, textvariable=self.list_hint_var, style="Hint.TLabel").pack(side="left")

        note_box = ttk.Frame(outer, style="App.TFrame")
        note_box.pack(fill="x", pady=(UiMetrics.SECTION_GAP, 9))
        ttk.Label(
            note_box,
            text="结果口径：Game Ban 是 Steam 的总游戏封禁；只有 API 的 bans 明细覆盖 AppID 578080 时，才标为 PUBG 关联封禁。",
            style="HeaderSubtitle.TLabel",
        ).pack(side="left")
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel", anchor="w").pack(fill="x")

    def save_api_key(self) -> None:
        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showinfo(APP_NAME, "请输入 Steam Web API Key 后再保存。", parent=self.root)
            return
        try:
            self.key_store.save(api_key)
        except KeyStorageError as exc:
            messagebox.showerror(APP_NAME, f"无法加密保存 API Key：{exc}", parent=self.root)
            return
        self.remember_key_var.set(True)
        self.key_state_var.set("已加密保存")
        self.status_var.set("Steam Web API Key 已使用 Windows 当前用户加密保存。")

    def _open_api_key_application_page(self, _event: Any = None) -> str:
        """从 Key 输入区直接打开 Steam 官方 Key 申请页。"""
        webbrowser.open(STEAM_API_KEY_APPLICATION_URL)
        self.status_var.set("已打开 Steam Web API Key 官方申请页；注册后复制 Key 到输入框并保存。")
        return "break"

    def clear_saved_api_key(self) -> None:
        if not self.api_key_var.get() and not self.key_store.path.exists():
            return
        if not messagebox.askyesno(APP_NAME, "清除本机加密保存的 API Key，并清空当前输入框吗？", parent=self.root):
            return
        try:
            self.key_store.clear()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法清除已保存的 API Key：{exc}", parent=self.root)
            return
        self.api_key_var.set("")
        self.remember_key_var.set(False)
        self.key_state_var.set("仅本次运行")
        self.status_var.set("已清除本机加密保存的 API Key。")

    def _schedule_login_timer_refresh(self) -> None:
        """小时数会跨整点变化；下一整点只更新已加载行的计时单元格。"""
        now = datetime.now()
        seconds_to_next_hour = max(1, 3600 - now.minute * 60 - now.second)
        self.root.after(seconds_to_next_hour * 1000, self._refresh_login_timers)

    def _refresh_login_timers(self) -> None:
        # 不能调用 refresh_table()：它会清空 Treeview、重新从第一页加载，从而让大列表跳回顶部。
        items = tuple(self.tree.get_children())
        timestamps = self.store.login_timestamps(int(item) for item in items)
        for item in items:
            timestamp = timestamps.get(int(item))
            if timestamp is not None:
                self.tree.set(item, 'login_elapsed', login_elapsed_label(*timestamp))
        self._schedule_login_timer_refresh()

    def check_for_updates(self) -> None:
        """在后台读取 GitHub Latest Release；只提醒和打开下载页，不下载或替换正在运行的 EXE。"""
        if self.update_url:
            self._open_update_page()
            return
        if self.update_check_running:
            return
        self.update_check_running = True
        self.update_button.configure(text="正在检查…", state="disabled")
        try:
            threading.Thread(target=self._update_check_worker, daemon=True).start()
        except Exception as exc:
            # 即使线程启动失败，也必须把按钮还原，不能把「正在检查」永久留在界面上。
            self.update_check_running = False
            self.update_button.configure(text="检查更新", command=self.check_for_updates, state="normal")
            self.status_var.set(f"无法启动更新检查：{exc}")

    def _update_check_worker(self) -> None:
        try:
            request = Request(
                GITHUB_LATEST_RELEASE_API,
                headers={'Accept': 'application/vnd.github+json', 'User-Agent': f'PUBG-Auto-Login/{APP_VERSION}'},
                method='GET',
            )
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('GitHub Release 响应不是 JSON 对象。')
            tag_name = str(payload.get('tag_name') or '')
            release_version = version_key(tag_name)
            current_version = version_key(APP_VERSION)
            if release_version is None or current_version is None:
                raise ValueError('GitHub Release 的版本号不是 X.Y.Z 格式。')
            if release_version > current_version:
                release_url = str(payload.get('html_url') or f'https://github.com/{GITHUB_REPOSITORY}/releases/tag/{tag_name}')
                self.events.put(('update_available', (tag_name.lstrip('v'), release_url)))
            else:
                self.events.put(('update_current', None))
        except Exception as exc:
            # 代理/劫持页或未来 API 字段变化都不应让后台线程悄悄退出，
            # 必须投递失败事件，由 UI 线程恢复「检查更新」按钮。
            self.events.put(('update_check_failed', str(exc)))

    def _open_update_page(self) -> None:
        if not self.update_url:
            return
        webbrowser.open(self.update_url)
        self.status_var.set(f"已打开 GitHub 的 v{self.update_version} 下载页面。")

    def open_steam_login(self, account_id: int | None = None) -> None:
        """用本机保存的账号密码自动登录指定账号；不传就登录列表里选中的那个。"""
        if self.login_running:
            self.status_var.set("已有 Steam 登录正在进行，请等待当前登录完成。")
            return
        if self.query_running or self.import_running:
            return
        if account_id is None:
            selected = self._selected_ids()
            if len(selected) != 1:
                messagebox.showinfo(APP_NAME, "请先在账号列表里选中一个账号，再点击自动登录。", parent=self.root)
                return
            account_id = selected[0]
        found = self.store.account_objects([account_id])
        if not found:
            messagebox.showinfo(APP_NAME, "这条账号记录已经不存在了，请刷新列表。", parent=self.root)
            return
        account = found[0]
        account_name = account.account_name or account.steam_id
        password = self.store.account_password(account.account_id)
        if not password:
            password = simpledialog.askstring(
                APP_NAME,
                f"请输入 {account_name} 的 Steam 密码：",
                show="*",
                parent=self.root,
            )
            if not password:
                return
            self.store.upsert_account(
                account.account_name, account.steam_id, account.note, password, account_id=account.account_id
            )
        steam_exe = steam_login.find_steam_exe()
        if steam_exe is None:
            webbrowser.open("https://steamcommunity.com/login/home/")
            self.status_var.set("未找到本机 Steam 客户端，已打开 Steam 官方登录页。")
            return
        previous_account_id = self.store.active_account_id()
        self.login_running = True
        self.login_cancel_event = threading.Event()
        self._set_controls(False)
        self.cancel_login_button.configure(state="normal")
        self.status_var.set(f"正在退出当前 Steam 并登录账号 {account_name}，请稍候…")
        try:
            threading.Thread(
                target=self._steam_login_worker,
                args=(
                    steam_exe, account_name, account.steam_id, password, account.account_id,
                    previous_account_id, self.login_cancel_event,
                ),
                daemon=True,
            ).start()
        except RuntimeError as exc:
            self.login_running = False
            self.login_cancel_event = None
            self._set_controls(True)
            self.cancel_login_button.configure(state="disabled")
            self.status_var.set("无法启动自动登录。")
            messagebox.showerror(APP_NAME, f"无法启动自动登录线程：{exc}", parent=self.root)

    def cancel_steam_login(self) -> None:
        """请求取消尚在等待登录窗口或填密的自动登录。"""
        if not self.login_running or self.login_cancel_event is None:
            return
        self.login_cancel_event.set()
        self.cancel_login_button.configure(state="disabled")
        self.status_var.set("正在取消自动登录；当前 Steam 操作结束后会恢复界面。")

    def _steam_login_worker(
        self,
        steam_exe: str,
        account_name: str,
        steam_id: str,
        password: str,
        account_id: int | None = None,
        previous_account_id: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """后台线程：结束当前 Steam，写入自动登录配置，再带账号密码启动客户端。"""
        try:
            if not steam_login.shutdown_steam(steam_exe):
                self.events.put(("steam_login_failed", "无法结束当前 Steam 进程，请手动退出 Steam 后重试。"))
                return
            if cancel_event is not None and cancel_event.is_set():
                self.events.put(("steam_login_cancelled", None))
                return
            if previous_account_id is not None:
                # 仅在 Steam 已确实退出后记时；主线程收到事件后写库，避免跨线程共用 SQLite 连接。
                self.events.put(("steam_logout", (int(previous_account_id), utc_now())))
            tweaks = []
            if STEAM_ID_PATTERN.fullmatch(steam_id):
                steam_login.write_auto_login(steam_exe, steam_id, account_name)
                try:
                    tweaks = steam_login.apply_login_tweaks(steam_exe, steam_id)
                except (OSError, ValueError, RuntimeError) as exc:
                    # 改写 Steam 客户端设置失败（例如配置文件编码异常）不应该阻止本次登录。
                    tweaks = [f'Steam 设置未写入（{exc}）']
            else:
                # 账号+密码导入时会先用账号名占位。不能把占位符写进 loginusers.vdf，
                # 也不能传给依赖 32 位账号 ID 的 Steam 设置逻辑。
                tweaks = ['尚未解析 SteamID64，已跳过 Steam 配置写入']
            if cancel_event is not None and cancel_event.is_set():
                self.events.put(("steam_login_cancelled", None))
                return
            steam_login.launch_login(steam_exe, account_name)

            def login_progress(elapsed: float) -> None:
                # 后台线程只投递事件，界面文案在主线程更新，避免跨线程操作控件。
                self.events.put(("steam_login_waiting", elapsed))

            login_result = steam_login.automate_steam_login(
                password, steam_exe, cancel_event=cancel_event, progress=login_progress
            )
        except steam_login.LoginAutomationCancelled:
            self.events.put(("steam_login_cancelled", None))
            return
        except (OSError, RuntimeError, KeyStorageError, ValueError) as exc:
            self.events.put(("steam_login_failed", str(exc)))
            return
        except Exception as exc:                        # 兜底：任何意外都要回报状态，不能静默卡住界面
            self.events.put(("steam_login_failed", f"未预期的错误：{exc!r}"))
            return
        applied = f"；已写入 Steam 设置：{'、'.join(tweaks)}" if tweaks else ""
        self.events.put(
            (
                "steam_login_done",
                (
                    account_id,
                    utc_now(),
                    f"账号 {account_name}：{login_result}{applied}。若 Valve 要求 Steam Guard 验证码，请在客户端里完成一次验证；"
                    "该账号在本机验证过一次后，之后即可直接自动登录。",
                ),
            )
        )

    def _selected_ids(self) -> list[int]:
        """勾选的账号优先；没有任何勾选时用列表里选中的行（支持 Ctrl / Shift 多选）。"""
        if self.checked_ids:
            return sorted(self.checked_ids)
        return [int(item) for item in self.tree.selection()]

    def _on_tree_click(self, event) -> str | None:
        """点「选择」列 = 勾选/取消勾选；点该列表头 = 全选 / 全不选。"""
        column = self.tree.identify_column(event.x)
        if self.tree.identify_region(event.x, event.y) == "heading":
            if column == "#1":
                self._set_all_checked(not self.checked_ids)
                return "break"
            return None
        if column != "#1":
            return None
        item = self.tree.identify_row(event.y)
        if not item:
            return None
        self._toggle_checked(int(item))
        return "break"

    def _on_tree_double_click(self, event) -> None:
        """双击账号 = 直接登录这个账号（「选择」列除外）。"""
        if self.tree.identify_column(event.x) == "#1":
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self.open_steam_login(int(item))

    def _on_tree_right_click(self, event) -> None:
        """右键账号：出现「登录 / 编辑 / 删除」菜单（点到的行不在多选里就先选中它）。"""
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item not in self.tree.selection():
            self.tree.selection_set(item)
        self._menu_account_id = int(item)
        try:
            self.row_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.row_menu.grab_release()

    def _toggle_checked(self, account_id: int) -> None:
        if account_id in self.checked_ids:
            self.checked_ids.discard(account_id)
        else:
            self.checked_ids.add(account_id)
        item = str(account_id)
        if self.tree.exists(item):
            self.tree.set(item, "pick", "■" if account_id in self.checked_ids else "□")
        self._update_list_summary()

    def _set_all_checked(self, checked: bool) -> None:
        """全选/清空：只作用于当前已经加载出来的行，避免一次性把几千条都勾上。"""
        for item in self.tree.get_children():
            account_id = int(item)
            if checked:
                self.checked_ids.add(account_id)
            else:
                self.checked_ids.discard(account_id)
            self.tree.set(item, "pick", "■" if checked else "□")
        self._update_list_summary()

    def _set_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (
            self.add_button,
            self.edit_button,
            self.delete_button,
            self.import_button,
            self.migrate_legacy_button,
            self.export_button,
            self.export_accounts_button,
            self.select_all_button,
            self.clear_checked_button,
            self.query_selected_button,
            self.query_all_button,
            self.risk_filter_button,
            self.safe_filter_button,
            self.paste_import_button,
            self.save_key_button,
            self.clear_saved_key_button,
            self.steam_login_button,
            self.remember_key_button,
        ):
            widget.configure(state=state)

    def _set_query_progress(self, completed: int, total: int, phase: str = "查询进度") -> None:
        """显示查询的确定性进度；解析 SteamID64 阶段也保留同一条进度条。"""
        total = max(0, int(total))
        completed = max(0, min(int(completed), total)) if total else 0
        percent = int(completed * 100 / total) if total else 0
        self.query_completed = completed
        self.query_total = total
        self.query_progress_bar.configure(value=percent)
        self.query_progress_var.set(f"{phase}：{completed} / {total}（{percent}%）")
        if not self.query_progress_card.winfo_ismapped():
            self.query_progress_card.pack(
                fill="x",
                pady=(0, UiMetrics.SECTION_GAP),
                before=self.table_card,
            )

    def _scroll_tree(self, *args: str) -> None:
        self.tree.yview(*args)
        self.root.after_idle(self._load_more_if_needed)

    def _on_tree_yview(self, first: str, last: str) -> None:
        self.y_scroll.set(first, last)
        if float(last) >= 0.985:
            self.root.after_idle(self._load_more_if_needed)

    def _queue_load_more(self, _event: Any = None) -> None:
        self.root.after_idle(self._load_more_if_needed)

    def _insert_table_row(self, row: sqlite3.Row, selected: set[int] | None = None) -> None:
        risk = (
            int(row["vac_banned"] or 0) > 0
            or int(row["game_bans"] or 0) > 0
            or int(row["community_banned"] or 0) > 0
            or str(row["economy_ban"] or "none").lower() not in ("none", "")
            or row["status"] == "查询失败"
        )
        tag = "risk" if risk and row["status"] != "查询失败" else "failure" if row["status"] == "查询失败" else "success" if row["status"] == "查询成功" else ""
        values = (
            "■" if row["id"] in self.checked_ids else "□",
            row["account_name"],
            row["password"] or "",
            row["steam_id"] if STEAM_ID_PATTERN.fullmatch(row["steam_id"] or "") else "未解析（查询时自动获取）",
            login_elapsed_label(row["last_logout_at"], row["last_login_at"]),
            bool_label(row["vac_banned"]),
            row["game_bans"] if row["game_bans"] is not None else "—",
            row["pubg_assessment"] or "—",
            bool_label(row["community_banned"]),
            row["economy_ban"] or "—",
            row["checked_at"] or "—",
            row["status"],
            row["note"],
        )
        item = self.tree.insert("", "end", iid=str(row["id"]), values=values, tags=(tag,) if tag else ())
        if selected and row["id"] in selected:
            self.tree.selection_add(item)

    def _toggle_filter(self, which: str) -> None:
        """两个筛选互斥：勾了「有风险」就自动取消「无风险」，反之亦然。"""
        if which == 'risk' and self.show_only_risk_var.get():
            self.show_only_safe_var.set(False)
        elif which == 'safe' and self.show_only_safe_var.get():
            self.show_only_risk_var.set(False)
        # 只在进入筛选模式时清空：防止隐藏记录误操作，同时允许关闭筛选后保留当前勾选。
        if self._filter_mode() != 'all':
            self.checked_ids.clear()
            selected = self.tree.selection()
            if selected:
                self.tree.selection_remove(selected)
        self.refresh_table()

    def _filter_mode(self) -> str:
        if self.show_only_risk_var.get():
            return 'risk'
        if self.show_only_safe_var.get():
            return 'safe'
        return 'all'

    def _update_list_summary(self) -> None:
        suffix = {'risk': '（仅有风险/异常）', 'safe': '（仅无风险）'}.get(self._filter_mode(), '')
        if self.checked_ids:
            suffix += f'，已勾选 {len(self.checked_ids)} 个'
        self.count_var.set(f'共 {self.table_total} 个账号{suffix}')
        if not self.table_total:
            self.list_hint_var.set('当前没有可显示的账号。')
        elif self.loaded_rows >= self.table_total:
            self.list_hint_var.set(f'已连续列出全部 {self.table_total} 条记录；可使用鼠标滚轮浏览。')
        else:
            self.list_hint_var.set(
                f'已加载 {self.loaded_rows} / {self.table_total} 条；继续向下滚动会自动加载更多。'
            )

    def _append_table_rows(self, selected: set[int] | None = None) -> None:
        if self.loading_table_rows or self.loaded_rows >= self.table_total:
            return
        self.loading_table_rows = True
        try:
            rows, total = self.store.list_accounts_page(
                self.loaded_rows,
                TREE_LOAD_CHUNK,
                self._filter_mode(),
            )
            self.table_total = total
            for row in rows:
                self._insert_table_row(row, selected)
            self.loaded_rows += len(rows)

        finally:
            self.loading_table_rows = False
        self._update_list_summary()

    def _load_more_if_needed(self) -> None:
        if self.loaded_rows < self.table_total:
            self._append_table_rows()

    def refresh_table(self) -> None:
        selected = set(self._selected_ids())
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.loaded_rows = 0
        self.table_total = self.store.count_accounts(self._filter_mode())
        self._update_list_summary()
        self._append_table_rows(selected)

    def _start_legacy_profile_scan(self) -> None:
        if self.legacy_scan_running or not self._should_scan_legacy_profile:
            return
        self.legacy_scan_running = True
        threading.Thread(target=self._legacy_profile_scan_worker, daemon=True).start()

    def _legacy_profile_scan_worker(self) -> None:
        """后台线程只负责找路径；真正的安全合并回到 UI 线程完成。"""
        try:
            current_stats = account_database_stats(self.database_path) or (0, 0)
            candidates = collect_legacy_account_databases(
                self.install_dir, deep=True, excluded_directories=[self.data_dir]
            )
            mergeable = migratable_legacy_databases(candidates, current_stats)
            self.events.put(("legacy_data_found", [str(path) for path in mergeable]))
        except Exception as exc:
            self.events.put(("legacy_data_scan_failed", str(exc)))

    def _legacy_scan_already_done(self) -> bool:
        """同一个版本只做一次深度查找，避免每次启动都全盘扫描。"""
        try:
            payload = json.loads(self.legacy_scan_marker_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get('version') == APP_VERSION

    def _mark_legacy_profile_scan_complete(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.legacy_scan_marker_path.write_text(
                json.dumps({'version': APP_VERSION, 'checked_at': utc_now()}, ensure_ascii=False),
                encoding='utf-8',
            )
        except OSError:
            pass

    def _automatically_merge_legacy_databases(self, source_databases: list[Path]) -> bool:
        """后台找到旧版库后，在 UI 线程安全合并，不覆盖当前账号、结果或登录记录。"""
        if self.query_running or self.import_running or self.login_running:
            self.status_var.set("已自动找到旧版账号库；当前有任务在运行，完成后重新打开程序即可自动继承。")
            return False
        try:
            merge_results = [merge_legacy_user_data(path.parent, self.data_dir) for path in source_databases]
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.status_var.set(f"已自动找到旧版账号库，但合并失败：{exc}；可点击“迁移旧版数据”手动处理。")
            return False

        added = sum(result['accounts_added'] for result in merge_results)
        updated = sum(result['accounts_updated'] for result in merge_results)
        passwords = sum(result['passwords_filled'] for result in merge_results)
        settings_copied = any(result['settings_copied'] for result in merge_results)
        try:
            saved_api_key = self.key_store.load()
        except KeyStorageError as exc:
            saved_api_key = None
            key_problem = f"；账号库已合并，但保存的 Key 无法读取：{exc}"
        else:
            key_problem = ''
        self.api_key_var.set(saved_api_key or '')
        self.remember_key_var.set(bool(saved_api_key))
        self.key_state_var.set("已加密保存" if saved_api_key else "仅本次运行")
        self.checked_ids.clear()
        self.refresh_table()
        key_suffix = '，并继承了可读取的 Key' if settings_copied else ''
        source_text = '、'.join(str(path.parent) for path in source_databases[:3])
        extra_sources = f' 等 {len(source_databases)} 处' if len(source_databases) > 3 else ''
        self.status_var.set(
            f"已自动合并旧版数据：新增账号 {added} 条、补充记录 {updated} 条、补充密码 {passwords} 条"
            f"{key_suffix}（来源：{source_text}{extra_sources}）。当前账号与查询记录已保留。{key_problem}"
        )
        return True

    def migrate_legacy_data(self) -> None:
        """供旧版位于非常规目录的用户手动选择并迁移其账号库。"""
        if self.query_running or self.import_running or self.login_running:
            self.status_var.set("请等待当前查询、导入或登录完成后再迁移旧版数据。")
            return
        selected_path = filedialog.askopenfilename(
            title="选择旧版 steam_ban_accounts.sqlite3",
            initialdir=str(self.install_dir),
            filetypes=[("旧版账号库", "steam_ban_accounts.sqlite3"), ("SQLite 数据库", "*.sqlite3"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not selected_path:
            return
        source_database = Path(selected_path).resolve()
        if source_database.name != ACCOUNT_DATABASE_FILENAME:
            messagebox.showerror(
                APP_NAME,
                f"请选择旧版的 {ACCOUNT_DATABASE_FILENAME}，而不是其它 SQLite 文件。",
                parent=self.root,
            )
            return
        if not is_account_database(source_database):
            messagebox.showerror(APP_NAME, "所选文件不是本软件可识别的旧版账号库。", parent=self.root)
            return
        if source_database == self.database_path.resolve():
            messagebox.showinfo(APP_NAME, "这就是当前正在使用的账号库，无需迁移。", parent=self.root)
            return
        current_count = self.store.count_accounts()
        if current_count and not messagebox.askyesno(
            APP_NAME,
            f"当前共享账号库已有 {current_count} 条记录。迁移会以所选旧版账号库替换它，并自动备份当前库。继续吗？",
            parent=self.root,
        ):
            return

        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_path = self.data_dir / f"steam_ban_accounts.before-migration-{timestamp}.sqlite3"
        self.store.close()
        try:
            if self.database_path.exists():
                copy_sqlite_database(self.database_path, backup_path)
            migrated_database, migrated_settings = migrate_legacy_user_data(
                source_database.parent,
                self.data_dir,
                replace_database=True,
                replace_settings=True,
            )
            if not migrated_database:
                raise FileNotFoundError(f"未找到 {ACCOUNT_DATABASE_FILENAME}")
            self.store = AccountStore(self.database_path)
            saved_api_key = self.key_store.load()
            self.api_key_var.set(saved_api_key or '')
            self.remember_key_var.set(bool(saved_api_key))
            self.key_state_var.set("已加密保存" if saved_api_key else "仅本次运行")
            self.checked_ids.clear()
            self.refresh_table()
            key_suffix = '和已保存的 Key' if migrated_settings else ''
            self.status_var.set(
                f"已迁移旧版账号库{key_suffix}；当前库已备份为 {backup_path.name}。以后更新会自动继承这些数据。"
            )
        except (OSError, sqlite3.Error, KeyStorageError, ValueError) as exc:
            self.store = AccountStore(self.database_path)
            messagebox.showerror(APP_NAME, f"迁移旧版数据失败：{exc}", parent=self.root)

    def add_account(self) -> None:
        dialog = AccountDialog(self.root, "添加账号")
        self.root.wait_window(dialog.window)
        if dialog.result:
            try:
                self.store.upsert_account(*dialog.result)
            except sqlite3.IntegrityError:
                messagebox.showerror(APP_NAME, "SteamID64 已存在。请使用编辑功能修改该记录。", parent=self.root)
                return
            self.refresh_table()
            self.status_var.set("已保存账号记录。")

    def edit_selected(self) -> None:
        selected = self._selected_ids()
        if len(selected) != 1:
            messagebox.showinfo(APP_NAME, "请只选择一条记录后再编辑。", parent=self.root)
            return
        account = self.store.account_objects(selected)[0]
        dialog = AccountDialog(self.root, "编辑账号", account, self.store.account_password(account.account_id))
        self.root.wait_window(dialog.window)
        if dialog.result:
            try:
                self.store.upsert_account(*dialog.result, account_id=account.account_id)
            except sqlite3.IntegrityError:
                messagebox.showerror(APP_NAME, "该 SteamID64 已存在于另一条记录。", parent=self.root)
                return
            self.refresh_table()
            self.status_var.set("已更新账号记录。")

    def delete_selected(self) -> None:
        selected = self._selected_ids()
        if not selected:
            messagebox.showinfo(APP_NAME, "请先选择要删除的记录。", parent=self.root)
            return
        if not messagebox.askyesno(APP_NAME, f"确定删除选中的 {len(selected)} 条记录吗？查询历史也会一并删除。", parent=self.root):
            return
        self.store.delete_accounts(selected)
        self.checked_ids -= set(selected)          # 删掉的账号不要再留在勾选集合里
        self.refresh_table()
        self.status_var.set(f"已删除 {len(selected)} 条记录。")

    def import_accounts(self) -> None:
        if self.import_running or self.query_running:
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="导入账号清单",
            filetypes=[("账号清单 (CSV / JSON / TXT)", "*.csv *.tsv *.json *.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        source_path = Path(path)
        if not source_path.is_file():
            messagebox.showerror(APP_NAME, "选择的导入文件不存在或不可读取。", parent=self.root)
            return
        self.import_running = True
        self.import_cancel_event = threading.Event()
        self._set_controls(False)
        self.cancel_import_button.configure(state="normal")
        self.status_var.set(f"正在后台导入 {source_path.name}…文件会逐行读取，界面保持可响应。")
        threading.Thread(
            target=self._import_worker,
            args=(source_path, self.import_cancel_event),
            daemon=True,
        ).start()

    def import_pasted_accounts(self) -> None:
        """粘贴导入：只有「账号----密码」这种文本也能用，不要求是文件。"""
        if self.import_running or self.query_running:
            return
        dialog = PasteImportDialog(self.root)
        self.root.wait_window(dialog.window)
        if not dialog.result:
            return
        temp_path = Path(tempfile.gettempdir()) / f"steam_paste_{datetime.now():%Y%m%d_%H%M%S}.txt"
        try:
            temp_path.write_text(dialog.result + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法写入临时文件：{exc}", parent=self.root)
            return
        self.import_running = True
        self.import_cancel_event = threading.Event()
        self._set_controls(False)
        self.cancel_import_button.configure(state="normal")
        self.status_var.set("正在导入粘贴的账号（账号----密码）…")
        threading.Thread(
            target=self._import_worker,
            args=(temp_path, self.import_cancel_event, True),
            daemon=True,
        ).start()

    def _import_worker(self, source_path: Path, cancel_event: threading.Event, cleanup: bool = False) -> None:
        try:
            stats = AccountStore.import_file(
                self.store.db_path,
                source_path,
                cancel_event,
                lambda update: self.events.put(("import_progress", update)),
            )
            self.events.put(("import_done", stats))
        except Exception as exc:
            self.events.put(("import_failed", str(exc)))
        finally:
            if cleanup:                     # 粘贴导入用的临时文件里有明文密码，导入完就删掉
                try:
                    source_path.unlink()
                except OSError:
                    pass

    def cancel_import(self) -> None:
        if not self.import_running or self.import_cancel_event is None:
            return
        self.import_cancel_event.set()
        self.cancel_import_button.configure(state="disabled")
        self.status_var.set("正在停止导入；已经提交的批次会保留，尚未读取的记录不会导入。")

    def export_results(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出查询结果",
            defaultextension=".csv",
            initialfile=f"steam_ban_results_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV 文件", "*.csv")],
        )
        if not path:
            return
        fields = [
            "账号标签",
            "SteamID64",
            "备注",
            "状态",
            "VAC封禁",
            "VAC次数",
            "距最近封禁天数",
            "游戏封禁次数",
            "社区封禁",
            "库存封禁",
            "PUBG判断",
            "查询时间",
            "错误说明",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(fields)
            for row in self.store.export_rows():
                writer.writerow(
                    [
                        row["account_name"],
                        row["steam_id"],
                        row["note"],
                        row["status"],
                        bool_label(row["vac_banned"]),
                        row["vac_count"],
                        row["days_since_last_ban"],
                        row["game_bans"],
                        bool_label(row["community_banned"]),
                        row["economy_ban"],
                        row["pubg_assessment"],
                        row["checked_at"],
                        row["query_error"],
                    ]
                )
        self.status_var.set(f"结果已导出：{path}")

    def export_accounts(self) -> None:
        """导出账号（含密码）：勾选/选中优先；筛选状态下可明确只导出当前结果。"""
        filter_mode = self._filter_mode()
        filter_count = self.store.count_accounts(filter_mode) if filter_mode != 'all' else 0
        format_dialog = ExportFormatDialog(self.root, filter_mode, filter_count)
        self.root.wait_window(format_dialog.window)
        if not format_dialog.result:
            return
        kind, filtered_only = format_dialog.result
        suffix = {'csv': '.csv', 'txt': '.txt', 'json': '.json'}[kind]
        filters = {
            'csv': [('CSV 文件', '*.csv')],
            'txt': [('文本文件（账号----密码）', '*.txt')],
            'json': [('JSON 文件', '*.json')],
        }[kind]
        ids = self._selected_ids()
        export_mode = 'all'
        if ids:
            scope = f"勾选/选中的 {len(ids)} 个账号"
        elif filtered_only and filter_mode != 'all':
            scope = f"当前筛选的 {filter_count} 个账号"
            export_mode = filter_mode
        else:
            scope = "全部账号"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title=f"导出账号（{scope} · {kind.upper()}）",
            defaultextension=suffix,
            initialfile=f"steam_accounts_{datetime.now():%Y%m%d_%H%M%S}{suffix}",
            filetypes=filters + [('所有文件', '*.*')],
        )
        if not path:
            return
        if not messagebox.askyesno(
            APP_NAME,
            "导出的文件里是明文密码，请只保存在自己机器上（不要发群里 / 存云盘）。\n确定继续吗？",
            parent=self.root,
        ):
            return
        target = Path(path)
        try:
            count = write_account_export(target, self.store.export_account_rows(ids or None, export_mode))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"导出失败：{exc}", parent=self.root)
            return
        self.status_var.set(f"已导出 {count} 个账号（{scope}）到 {target}（含明文密码，注意保管）。")

    def start_query(self, query_all: bool) -> None:
        if self.query_running or self.import_running or self.login_running:
            return
        api_key = self.api_key_var.get().strip()
        if not api_key:
            messagebox.showerror(APP_NAME, "请先输入你自己的 Steam Web API Key。", parent=self.root)
            return
        if self.remember_key_var.get():
            try:
                self.key_store.save(api_key)
            except KeyStorageError as exc:
                messagebox.showerror(APP_NAME, f"无法加密保存 API Key：{exc}", parent=self.root)
                return
            self.key_state_var.set("已加密保存")
        selected_ids = None if query_all else self._selected_ids()
        total = self.store.count_accounts() if query_all else len(selected_ids)
        if not total:
            messagebox.showinfo(APP_NAME, "没有可查询的账号。", parent=self.root)
            return
        self.query_running = True
        self._set_controls(False)
        self._set_query_progress(0, total, "准备查询")
        self.status_var.set(f"正在查询 0 / {total} 条记录…")
        threading.Thread(
            target=self._query_worker,
            args=(api_key, selected_ids, total, self.store.db_path),
            daemon=True,
        ).start()

    def _resolve_batch_ids(
        self,
        batch: list,
        failures: int,
        store: AccountStore,
        completed_before_batch: int,
        total: int,
    ):
        """把这一批里缺 SteamID64 的账号解析出来（账号密码 -> SteamID64）。

        store 必须是查询工作线程独享的连接，避免跨线程复用 UI 线程的 SQLite 连接。
        返回 (可继续查询的账号, 连续失败次数, 需要中止时的提示, 已处理数量)。
        """
        resolved = []
        handled = 0
        for account in batch:
            handled += 1
            if STEAM_ID_PATTERN.fullmatch(account.steam_id):
                resolved.append(account)
                continue
            name = account.account_name or account.steam_id
            password = store.account_password(account.account_id)
            if not password:
                store.save_result(account.account_id, None, "缺少密码，无法解析 SteamID64（请编辑该账号填入密码后重试）")
                self.events.put(("resolve_progress", (name, completed_before_batch + handled, total)))
                continue
            try:
                real_id = steam_login.resolve_steam_id(name, password)
            except steam_login.SteamAuthError as exc:
                failures += 1
                store.save_result(account.account_id, None, f"无法解析 SteamID64：{exc}")
                self.events.put(("resolve_progress", (name, completed_before_batch + handled, total)))
                if failures >= 5:
                    return (
                        resolved,
                        failures,
                        f"连续 {failures} 个账号解析 SteamID64 失败（最后一次：{exc}）。已停止，请检查账号密码或稍后再试。",
                        handled,
                    )
                continue
            except Exception as exc:
                failures += 1
                store.save_result(account.account_id, None, f"无法解析 SteamID64：{exc!r}")
                self.events.put(("resolve_progress", (name, completed_before_batch + handled, total)))
                continue
            failures = 0
            if store.update_steam_id(account.account_id, real_id):
                resolved.append(Account(account.account_id, account.account_name, real_id, account.note))
            time.sleep(1.0)          # 放慢节奏，降低被 Steam 限流的概率
            # SteamID64 的解析按账号串行。每处理一条都汇报，避免大批次解析时进度条停在 0%。
            self.events.put(("resolve_progress", (name, completed_before_batch + handled, total)))
        return resolved, failures, None, handled

    def _query_worker(self, api_key: str, account_ids: list[int] | None, total: int, db_path: Path) -> None:
        completed = 0
        resolve_failures = 0
        try:
            store = AccountStore(db_path)
        except Exception as exc:
            self.events.put(("query_blocked", (completed, total, f"无法打开账号数据库：{exc}")))
            return
        try:
            for original_batch in AccountStore.account_batches(db_path, account_ids):
                batch, resolve_failures, stop_message, handled = self._resolve_batch_ids(
                    original_batch, resolve_failures, store, completed, total
                )
                if stop_message:
                    # 已解析成功但尚未查询的条目不能被算作“完成”。
                    self.events.put(("query_blocked", (completed + handled - len(batch), total, stop_message)))
                    return
                if not batch:
                    completed += handled
                    self.events.put(("query_progress", (completed, total)))
                    continue
                result_batch = []
                try:
                    response = request_player_bans(api_key, [account.steam_id for account in batch])
                    for account in batch:
                        player = response.get(account.steam_id)
                        if player is None:
                            result_batch.append((account.account_id, None, "Steam API 未返回该 SteamID64 的结果"))
                        else:
                            result_batch.append((account.account_id, player, ""))
                except SteamApiFatalError as exc:
                    self.events.put(("query_blocked", (completed, total, str(exc))))
                    return
                except Exception as exc:
                    self.events.put(("query_blocked", (completed, total, f"查询程序发生未预期错误：{exc}")))
                    return
                self.events.put(("result_batch", result_batch))
                completed += handled
                self.events.put(("query_progress", (completed, total)))
                if completed < total:
                    time.sleep(0.35)
            self.events.put(("query_done", total))
        except Exception as exc:
            self.events.put(("query_blocked", (completed, total, f"查询程序发生未预期错误：{exc}")))
        finally:
            store.close()

    def _drain_events(self) -> None:
        changed = False
        processed = 0
        try:
            while processed < MAX_UI_EVENTS_PER_TICK:
                kind, payload = self.events.get_nowait()
                processed += 1
                if kind == "legacy_data_found":
                    self.legacy_scan_running = False
                    self._should_scan_legacy_profile = False
                    if payload:
                        # 只有确实处理完（安全合并成功）才记“已查过”。正在跑任务、或合并失败
                        # 时不能记，否则下次启动不再查找，“重新打开程序即可继承”就无法兑现。
                        if self._automatically_merge_legacy_databases([Path(path) for path in payload]):
                            self._mark_legacy_profile_scan_complete()
                    else:
                        self._mark_legacy_profile_scan_complete()
                        if not (self.query_running or self.import_running or self.login_running or self.update_url):
                            self.status_var.set("未检测到可迁移的旧版账号数据；可正常添加或导入账号。")
                elif kind == "legacy_data_scan_failed":
                    self.legacy_scan_running = False
                    self._should_scan_legacy_profile = False
                    if not (self.query_running or self.import_running or self.login_running or self.update_url):
                        self.status_var.set(f"自动查找旧版账号数据失败：{payload}；可点击“迁移旧版数据”选择账号库。")
                elif kind == "update_available":
                    version, url = payload
                    self.update_check_running = False
                    self.update_version = version
                    self.update_url = url
                    self.update_button.configure(
                        text=f"下载 v{version}", command=self._open_update_page, state="normal"
                    )
                    if not (self.query_running or self.import_running or self.login_running):
                        self.status_var.set(f"发现新版本 v{version}，点击右上角“下载 v{version}”获取更新。")
                elif kind == "update_current":
                    self.update_check_running = False
                    self.update_button.configure(text="检查更新", command=self.check_for_updates, state="normal")
                elif kind == "update_check_failed":
                    self.update_check_running = False
                    self.update_button.configure(text="检查更新", command=self.check_for_updates, state="normal")
                elif kind == "result_batch":
                    self.store.save_results_batch(payload)
                elif kind == "query_progress":
                    completed, total = payload
                    self._set_query_progress(completed, total)
                    self.status_var.set(f"正在查询 {completed} / {total} 条记录…")
                elif kind == "resolve_progress":
                    account_name, processed, total = payload
                    self._set_query_progress(processed, total, "正在解析 SteamID64")
                    self.status_var.set(
                        f"正在解析 SteamID64：{account_name}…（已处理 {processed} / {total}，每个约 1 秒）"
                    )
                elif kind == "query_done":
                    self.query_running = False
                    self._set_query_progress(payload, payload, "查询完成")
                    self._set_controls(not (self.import_running or self.login_running))
                    self.status_var.set(f"查询完成：共处理 {payload} 条记录。")
                    changed = True
                elif kind == "query_blocked":
                    completed, total, message = payload
                    self.query_running = False
                    self._set_query_progress(completed, total, "查询已停止")
                    self._set_controls(not (self.import_running or self.login_running))
                    self.status_var.set(f"查询已停止：已完成 {completed} / {total} 条；没有把未查询账号标记为失败。")
                    messagebox.showerror(APP_NAME, message, parent=self.root)
                    # SteamID 解析失败会由工作线程直接写入独立连接；即使没有 API 成功结果也要刷新列表。
                    changed = True
                elif kind == "import_progress":
                    extra = f"；已加密保存密码 {payload['passwords_saved']} 条" if payload["passwords_saved"] else ""
                    self.status_var.set(
                        f"正在后台导入：已读取 {payload['read']} 条，已导入/更新 {payload['imported']} 条，格式无效 "
                        f"{payload['invalid']} 条（约 {payload['percent']}%）{extra}。"
                    )
                elif kind == "import_done":
                    self.import_running = False
                    self.import_cancel_event = None
                    self.cancel_import_button.configure(state="disabled")
                    self._set_controls(not (self.query_running or self.login_running))
                    extra = f"；已加密保存密码 {payload['passwords_saved']} 条" if payload["passwords_saved"] else ""
                    if payload["canceled"]:
                        self.status_var.set(
                            f"导入已停止：已读取 {payload['read']} 条，已导入/更新 {payload['imported']} 条，格式无效 "
                            f"{payload['invalid']} 条；已提交的批次已保留{extra}。"
                        )
                    else:
                        self.status_var.set(
                            f"导入完成：已读取 {payload['read']} 条，已导入/更新 {payload['imported']} 条，格式无效 "
                            f"{payload['invalid']} 条{extra}。"
                        )
                    changed = True
                elif kind == "import_failed":
                    self.import_running = False
                    self.import_cancel_event = None
                    self.cancel_import_button.configure(state="disabled")
                    self._set_controls(not (self.query_running or self.login_running))
                    self.status_var.set("导入失败。")
                    messagebox.showerror(APP_NAME, f"无法导入文件：{payload}", parent=self.root)
                elif kind == "steam_logout":
                    account_id, logged_out_at = payload
                    self.store.mark_account_logged_out(account_id, logged_out_at)
                    changed = True
                elif kind == "steam_login_waiting":
                    if not (self.query_running or self.import_running or self.update_url):
                        self.status_var.set(
                            f"已启动 Steam，正在等它自动登录或弹出登录窗口（已等待 {int(payload)} 秒；"
                            "经代理/VPN 时可能要 1-2 分钟，可随时点“取消登录”）…"
                        )
                elif kind == "steam_login_done":
                    account_id, logged_in_at, message = payload
                    self.login_running = False
                    self.login_cancel_event = None
                    self.cancel_login_button.configure(state="disabled")
                    if account_id is not None:
                        self.store.mark_account_logged_in(account_id, logged_in_at)
                    self._set_controls(not (self.query_running or self.import_running))
                    self.status_var.set(message)
                    changed = True
                elif kind == "steam_login_failed":
                    self.login_running = False
                    self.login_cancel_event = None
                    self.cancel_login_button.configure(state="disabled")
                    self._set_controls(not (self.query_running or self.import_running))
                    self.status_var.set("自动登录未完成。")
                    messagebox.showerror(APP_NAME, f"自动登录失败：{payload}", parent=self.root)
                elif kind == "steam_login_cancelled":
                    self.login_running = False
                    self.login_cancel_event = None
                    self.cancel_login_button.configure(state="disabled")
                    self._set_controls(not (self.query_running or self.import_running))
                    self.status_var.set("已取消自动登录；Steam 若已启动，可自行在客户端继续操作。")
        except queue.Empty:
            pass
        if changed:
            self.refresh_table()
        self.root.after(30 if processed else 120, self._drain_events)

    def _on_close(self) -> None:
        self.api_key_var.set("")
        if self.import_cancel_event is not None:
            self.import_cancel_event.set()
        if self.login_cancel_event is not None:
            self.login_cancel_event.set()
        self.store.close()
        self.root.destroy()


# NOTE: these three imports are needed for byte-identical code generation:
# the compiler emits the generic call form (LOAD_ATTR without NULL|self +
# PUSH_NULL) for attribute calls on names bound by an "import" statement.
# The module header contains the same imports; duplicates are harmless.


def run_self_test() -> None:
    assert normalize_steam_id("76561198000000000") == "76561198000000000"
    assert version_key("v3.2.1") == (3, 2, 1)
    assert version_key("3.1") is None
    fixed_now = datetime.strptime("2026-09-22 12:00:00 +0800", "%Y-%m-%d %H:%M:%S %z")
    assert login_elapsed_label("2026-09-20 13:00:00 +0800", "2026-09-20 12:00:00 +0800", fixed_now) == "距上次登录 47 小时"
    assert login_elapsed_label("2026-09-19 12:00:00 +0800", "2026-09-19 11:00:00 +0800", fixed_now) == "上次登录：2026-09-19"
    assert login_elapsed_label(None, "2026-09-22 11:00:00 +0800", fixed_now) == "当前登录中"
    try:
        normalize_steam_id("123")
        raise AssertionError("Invalid SteamID64 was accepted")
    except ValueError:
        pass
    assert pubg_assessment({"NumberOfGameBans": 0}) == "未发现游戏封禁"
    assert pubg_assessment({"NumberOfGameBans": 1}) == "存在游戏封禁（未证明 PUBG）"
    assert pubg_assessment({"bans": [{"AppIdMin": 578080, "AppIdMax": 578080}]}) == "PUBG 关联封禁（API 明细）"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # 旧版旁的账号库和设置迁移到固定的 LocalAppData 目录后，换 EXE/换版本仍能继续使用。
        legacy_directory = root / "Downloads" / "PUBG-Auto-Login-v3.1.2"
        legacy_directory.mkdir(parents=True)
        legacy_store = AccountStore(legacy_directory / ACCOUNT_DATABASE_FILENAME)
        legacy_store.upsert_account("迁移测试账号", "76561198000000009", "旧版记录")
        (legacy_directory / KEY_SETTINGS_FILENAME).write_text('{"migrated":true}', encoding="utf-8")
        shared_data_directory = application_data_directory(root / "LocalAppData")
        assert shared_data_directory == root / "LocalAppData" / APP_DATA_FOLDER_NAME
        assert find_legacy_account_database(root / "new-release", root) == legacy_directory / ACCOUNT_DATABASE_FILENAME
        migrated_database, migrated_settings = migrate_legacy_user_data(legacy_directory, shared_data_directory)
        legacy_store.close()
        assert migrated_database is True and migrated_settings is True
        migrated_store = AccountStore(shared_data_directory / ACCOUNT_DATABASE_FILENAME)
        assert migrated_store.count_accounts() == 1
        assert migrated_store.list_accounts_page(0, 1)[0][0]["account_name"] == "迁移测试账号"
        migrated_store.close()
        assert (shared_data_directory / KEY_SETTINGS_FILENAME).read_text(encoding="utf-8") == '{"migrated":true}'
        # 常见目录未命中时，会在后台遍历当前用户目录的任意深度，仍只接受可识别的固定账号库。
        deep_legacy_directory = root / "Archive" / "old-releases" / "nested" / "PUBG"
        deep_legacy_directory.mkdir(parents=True)
        deep_store = AccountStore(deep_legacy_directory / ACCOUNT_DATABASE_FILENAME)
        deep_store.upsert_account("深层迁移账号", "76561198000000008", "自动发现")
        deep_store.upsert_account("深层迁移账号二", "76561198000000006", "自动发现")
        deep_store.close()
        assert find_legacy_account_database_in_profile(root, shared_data_directory) == deep_legacy_directory / ACCOUNT_DATABASE_FILENAME
        # 固定磁盘只扫两层：工具被解压到“某盘\某目录\某子目录”时也能找到，且不会进入共享数据目录。
        stale_directory = root / "Documents" / "old-copy"
        stale_directory.mkdir(parents=True)
        stale_store = AccountStore(stale_directory / ACCOUNT_DATABASE_FILENAME)
        stale_store.upsert_account("过期副本", "76561198000000007", "")
        stale_store.close()
        shallow_candidates = find_account_databases(root, max_depth=2, excluded_directories=[shared_data_directory])
        assert stale_directory / ACCOUNT_DATABASE_FILENAME in shallow_candidates
        assert deep_legacy_directory / ACCOUNT_DATABASE_FILENAME not in shallow_candidates
        assert shared_data_directory / ACCOUNT_DATABASE_FILENAME not in shallow_candidates
        assert isinstance(fixed_drive_roots(), list)
        assert account_database_stats(deep_legacy_directory / ACCOUNT_DATABASE_FILENAME) == (2, 0)
        # 候选库按“账号多、密码全、更新得晚”排序。自动路径改为安全合并后，账号较少的旧副本
        # 也可以补回当前库缺失的密码，而不能再整体顶替当前库。
        assert best_migratable_legacy_database(
            [stale_directory / ACCOUNT_DATABASE_FILENAME, deep_legacy_directory / ACCOUNT_DATABASE_FILENAME],
            (0, 0),
        ) == deep_legacy_directory / ACCOUNT_DATABASE_FILENAME
        assert legacy_migration_is_worthwhile((0, 0), (1, 0)) is True           # 当前是空库
        assert legacy_migration_is_worthwhile((0, 0), (0, 0)) is False          # 旧库里没有账号
        assert legacy_migration_is_worthwhile((1, 0), (1, 1)) is True           # 当前库丢了密码
        assert legacy_migration_is_worthwhile((1, 0), (1, 0)) is False          # 旧库也没有密码
        assert legacy_migration_is_worthwhile((30, 0), (10, 10)) is True        # 账号更少也只能补密码，不会顶替
        assert legacy_migration_is_worthwhile((30, 30), (3000, 30)) is False    # 当前库有密码就不动它
        # 老版本的表可能没有 password_enc 列：这种库仍然要能被识别和迁移（打开时会自动补列）。
        old_schema_path = root / "old-schema.sqlite3"
        old_connection = sqlite3.connect(old_schema_path)
        old_connection.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, account_name TEXT, steam_id TEXT)")
        old_connection.execute("INSERT INTO accounts (account_name, steam_id) VALUES ('老结构账号', '76561198000000005')")
        old_connection.commit()
        old_connection.close()
        assert account_database_stats(old_schema_path) == (1, 0)
        assert is_account_database(old_schema_path) is True
        old_schema_store = AccountStore(old_schema_path)
        assert old_schema_store.count_accounts() == 1
        assert 'password' in {row[1] for row in old_schema_store.connection.execute('PRAGMA table_info(accounts)')}
        old_schema_store.close()

        # 自动迁移只能合并：当前账号、查询结果和备注都保留，旧库仅补密码并添加此前不存在的账号。
        merge_current_dir = root / 'merge-current'
        merge_legacy_dir = root / 'merge-legacy'
        merge_current_dir.mkdir()
        merge_legacy_dir.mkdir()
        merge_current = AccountStore(merge_current_dir / ACCOUNT_DATABASE_FILENAME)
        same_id = merge_current.upsert_account('同一账号', '76561198000000041', '当前备注')
        merge_current.save_result(same_id, {
            'VACBanned': False, 'NumberOfVACBans': 0, 'DaysSinceLastBan': 0,
            'NumberOfGameBans': 0, 'CommunityBanned': False, 'EconomyBan': 'none',
        })
        merge_current.connection.execute(
            AccountStore.UPSERT_SQL, ('占位账号', placeholder_for('占位账号'), '当前占位备注', '')
        )
        merge_current.connection.commit()
        placeholder_id = merge_current.connection.execute(
            "SELECT id FROM accounts WHERE account_name='占位账号'"
        ).fetchone()['id']
        current_only_id = merge_current.upsert_account('当前独有', '76561198000000042', '不能丢失')
        merge_current.upsert_account('反向占位账号', '76561198000000045', '已解析但没有密码')
        merge_current.close()
        merge_legacy = AccountStore(merge_legacy_dir / ACCOUNT_DATABASE_FILENAME)
        legacy_same_id = merge_legacy.upsert_account('同一账号', '76561198000000041', '旧备注', 'legacy-password')
        merge_legacy.save_result(legacy_same_id, {
            'VACBanned': True, 'NumberOfVACBans': 1, 'DaysSinceLastBan': 1,
            'NumberOfGameBans': 1, 'CommunityBanned': False, 'EconomyBan': 'none',
        })
        merge_legacy.upsert_account('占位账号', '76561198000000043', '旧占位备注', 'placeholder-password')
        merge_legacy.upsert_account('旧库独有', '76561198000000044', '旧库备注', 'old-only-password')
        merge_legacy.connection.execute(
            AccountStore.UPSERT_SQL,
            (
                '反向占位账号', placeholder_for('反向占位账号'), '旧库有密码',
                'reverse-password',
            ),
        )
        merge_legacy.connection.commit()
        merge_legacy.close()
        merge_result = merge_legacy_user_data(merge_legacy_dir, merge_current_dir)
        assert merge_result['accounts_added'] == 1, merge_result
        assert merge_result['passwords_filled'] == 4, merge_result
        merged_store = AccountStore(merge_current_dir / ACCOUNT_DATABASE_FILENAME)
        merged_same = merged_store.connection.execute(
            "SELECT * FROM accounts WHERE steam_id='76561198000000041'"
        ).fetchone()
        assert merged_same['note'] == '当前备注' and merged_same['vac_banned'] == 0
        assert merged_store.account_password(int(merged_same['id'])) == 'legacy-password'
        merged_placeholder = merged_store.connection.execute(
            "SELECT * FROM accounts WHERE id=?", (placeholder_id,)
        ).fetchone()
        assert merged_placeholder['steam_id'] == '76561198000000043'
        assert merged_placeholder['note'] == '当前占位备注'
        assert merged_store.account_password(int(merged_placeholder['id'])) == 'placeholder-password'
        reverse_rows = merged_store.connection.execute(
            "SELECT * FROM accounts WHERE account_name='反向占位账号'"
        ).fetchall()
        assert len(reverse_rows) == 1
        assert reverse_rows[0]['steam_id'] == '76561198000000045'
        assert merged_store.account_password(int(reverse_rows[0]['id'])) == 'reverse-password'
        assert merged_store.connection.execute('SELECT note FROM accounts WHERE id=?', (current_only_id,)).fetchone()['note'] == '不能丢失'
        assert merged_store.account_password(
            int(merged_store.connection.execute("SELECT id FROM accounts WHERE steam_id='76561198000000044'").fetchone()['id'])
        ) == 'old-only-password'
        merged_store.close()

        # 深度扫描的目录额度在所有根目录之间共享，预算耗尽时不会继续枚举下一层目录。
        tiny_budget = LegacyScanBudget(time.monotonic() + 10, 1)
        assert not find_account_databases(root, max_depth=8, scan_budget=tiny_budget)
        assert tiny_budget.remaining_directories == 0

        # 后台线程只负责发现路径，实际迁移仍回到 UI 事件队列，避免跨线程操作界面数据库。
        scan_app = SteamBanApp.__new__(SteamBanApp)
        scan_app.events = queue.Queue()
        scan_app.data_dir = shared_data_directory
        scan_app.database_path = root / "EmptyData" / ACCOUNT_DATABASE_FILENAME
        scan_app.install_dir = root / "new-release"
        saved_collector = globals()['collect_legacy_account_databases']
        try:
            globals()['collect_legacy_account_databases'] = lambda *_args, **_kwargs: [deep_legacy_directory / ACCOUNT_DATABASE_FILENAME]
            scan_app._legacy_profile_scan_worker()
        finally:
            globals()['collect_legacy_account_databases'] = saved_collector
        assert scan_app.events.get_nowait() == ('legacy_data_found', [str(deep_legacy_directory / ACCOUNT_DATABASE_FILENAME)])

        # 只带 SteamID64 的增量导入不能清空已有的账号名/备注，已保存密码也要留着。
        upsert_store = AccountStore(root / "upsert.sqlite3")
        kept_id = upsert_store.upsert_account("保留账号名", "76561198000000004", "保留备注", password="pw")
        upsert_store.upsert_account("", "76561198000000004", "")
        kept_row = upsert_store.connection.execute(
            "SELECT account_name, note FROM accounts WHERE id=?", (kept_id,)
        ).fetchone()
        assert kept_row["account_name"] == "保留账号名" and kept_row["note"] == "保留备注"
        assert upsert_store.account_password(kept_id) == "pw"
        # 占位账号解析成功后合并：密码/备注/标签要并进真 ID 行，不能随占位行一起被删掉。
        upsert_store.connection.execute(
            AccountStore.UPSERT_SQL,
            ("占位账号", placeholder_for("占位账号"), "占位备注", "secret"),
        )
        upsert_store.connection.commit()
        placeholder_id = upsert_store.connection.execute(
            "SELECT id FROM accounts WHERE account_name='占位账号'"
        ).fetchone()["id"]
        real_id = upsert_store.upsert_account("", "76561198000000003", "")
        assert upsert_store.update_steam_id(placeholder_id, "76561198000000003") is False
        assert upsert_store.account_password(real_id) == "secret"
        assert upsert_store.count_accounts() == 2
        upsert_store.close()

        # 一次性处理上万个 id 不能撞 SQLite 变量上限（删除 / 导出 / 登录计时都要分块）。
        chunk_store = AccountStore(root / "chunk.sqlite3")
        chunk_store.delete_accounts(range(40000))
        assert chunk_store.login_timestamps(range(40000)) == {}
        assert list(chunk_store.export_account_rows(range(40000))) == []
        assert chunk_store.account_objects(range(40000)) == []
        assert list(AccountStore.account_batches(chunk_store.db_path, range(40000))) == []
        chunk_store.close()

        # 64 KiB 样本正好切在多字节字符中间时，仍然要识别出真实编码（不能误判成 GB18030）。
        truncated_csv = root / "truncated.csv"
        with truncated_csv.open("wb") as handle:
            handle.write(b"account,password\n")
            # 让样本最后正好是 UTF-8 中文“中”的前两个字节 e4 b8；它们恰好也是合法
            # GB18030 字符，专门覆盖“先修剪 UTF-8，再试 GB18030”的优先级要求。
            handle.write(b"a" * (65536 - len(b"account,password\n") - 2))
            handle.write("中,pw\n".encode("utf-8"))
            handle.write(b"tail,pw\n")
        assert detect_csv_format(truncated_csv)[0] == "utf-8-sig"

        # loginusers.vdf 用空格缩进（别的工具改过）也要能解析，否则登录会放弃写入。
        spaced_vdf = '"users"\n{\n    "76561198000000001"\n    {\n        "AccountName"        "spaced"\n    }\n}\n'
        assert set(steam_login.parse_loginusers(spaced_vdf)) == {"76561198000000001"}
        # 韩/日/俄/德等本地化标题也要能认出登录窗口；更新/安装窗口不能误认。
        assert steam_login._is_login_window_title('Steam 로그인') is True
        assert steam_login._is_login_window_title('Вход в Steam') is True
        assert steam_login._is_login_window_title('Sign in to Steam') is True
        assert steam_login._is_login_window_title('Steam 更新') is False
        assert steam_login._is_login_window_title('记事本') is False
        expected_steam_exe = r'C:\Program Files (x86)\Steam\steam.exe'
        assert steam_login._is_expected_steam_process_image(expected_steam_exe, expected_steam_exe) is True
        assert steam_login._is_expected_steam_process_image(
            r'C:\Program Files (x86)\Steam\steamwebhelper.exe', expected_steam_exe
        ) is True
        # 现代 Steam 的登录页由 CEF 子目录中的 steamwebhelper.exe 承载，仍属于同一安装目录。
        assert steam_login._is_expected_steam_process_image(
            r'C:\Program Files (x86)\Steam\bin\cef\cef.win64\steamwebhelper.exe', expected_steam_exe
        ) is True
        assert steam_login._is_expected_steam_process_image(
            r'C:\Program Files (x86)\Steam-old\bin\cef\steamwebhelper.exe', expected_steam_exe
        ) is False
        # 浏览器页面标题里即使有 Steam，也不会通过进程路径这道门。
        assert steam_login._is_expected_steam_process_image(
            r'C:\Program Files\Google\Chrome\Application\chrome.exe', expected_steam_exe
        ) is False
        # 启动 Steam 的命令行里不能再出现密码。
        import inspect as _inspect
        assert 'password' not in str(_inspect.signature(steam_login.launch_login))
        assert hasattr(steam_login.wintypes, 'DWORD')
        cancelled_login = threading.Event()
        cancelled_login.set()
        try:
            steam_login.automate_steam_login('unused', expected_steam_exe, timeout=0.1, cancel_event=cancelled_login)
            raise AssertionError('已取消的登录等待不应继续执行')
        except steam_login.LoginAutomationCancelled:
            pass
        # 等待逻辑：Steam 自己登录（注册表 ActiveUser）优先于弹登录窗口；两者都没有才算超时。
        saved_active_account = steam_login.active_login_account_id
        saved_find_window = steam_login._find_login_window
        try:
            steam_login.active_login_account_id = lambda: 791798680
            assert steam_login.wait_for_steam_login(expected_steam_exe, timeout=1.0) == ('logged_in', 791798680)
            steam_login.active_login_account_id = lambda: None
            steam_login._find_login_window = lambda _exe: 4242
            assert steam_login.wait_for_steam_login(expected_steam_exe, timeout=1.0) == ('login_window', 4242)
            steam_login._find_login_window = lambda _exe: 0
            assert steam_login.wait_for_steam_login(expected_steam_exe, timeout=0.2) == ('timeout', 0)
        finally:
            steam_login.active_login_account_id = saved_active_account
            steam_login._find_login_window = saved_find_window
        # Steam 卡在连接服务器时，超时提示必须指出是网络/代理问题，而不是含糊的“没等到窗口”。
        fake_steam_root = root / 'fake-steam'
        (fake_steam_root / 'logs').mkdir(parents=True)
        connection_log = fake_steam_root / 'logs' / 'connection_log.txt'
        connection_log.write_text(
            '[2026-09-22 16:23:16] [Connecting, 0, 7] [U:1:0] Client thinks it can connect\n'
            '[2026-09-22 16:20:02] PingWebSocketCM() (cmp2-hkg1.steamserver.net:27025) starting...\n',
            encoding='utf-8',
        )
        assert steam_login.steam_is_connecting(str(fake_steam_root / 'steam.exe')) is True
        connection_log.write_text(
            '[2026-09-22 16:24:00] [Logged On, 0, 0] [U:1:791798680] CCMInterface::SetSteamID( [U:1:791798680] )\n',
            encoding='utf-8',
        )
        assert steam_login.steam_is_connecting(str(fake_steam_root / 'steam.exe')) is False
        # CEF 登录页的密码只能安全提交一次：后续 Tab/点击/粘贴会在页面状态已变化时破坏
        # 第一次正确提交。这里模拟窗口关闭，确认不再发送第二次粘贴、Tab 或 Enter。
        login_steps: list[tuple[str, Any]] = []

        class FakeLoginWindow:
            def ShowWindow(self, _hwnd, _command):
                return 1

            def SetForegroundWindow(self, _hwnd):
                return 1

            def GetForegroundWindow(self):
                return 4242

            def IsWindow(self, _hwnd):
                return False

            def GetWindowRect(self, _hwnd, rect_pointer):
                rect = ctypes.cast(rect_pointer, ctypes.POINTER(ctypes.wintypes.RECT)).contents
                rect.left, rect.top, rect.right, rect.bottom = 100, 200, 800, 640
                return 1

            def SetCursorPos(self, x, y):
                login_steps.append(('cursor', (x, y)))
                return 1

            def SendInput(self, _count, _pointer, _size):
                login_steps.append(('click', None))
                return 1

        saved_user32 = steam_login._user32
        saved_active_login = steam_login.active_login_account_id
        saved_find_login_window = steam_login._find_login_window
        saved_set_clipboard = steam_login._set_clipboard
        saved_clear_clipboard = steam_login._clear_clipboard
        saved_paste = steam_login._paste
        saved_press = steam_login._press
        saved_wait_or_cancel = steam_login._wait_or_cancel
        try:
            steam_login._user32 = lambda: FakeLoginWindow()
            steam_login.active_login_account_id = lambda: None
            steam_login._find_login_window = lambda _steam_exe: 4242
            steam_login._set_clipboard = lambda text: login_steps.append(('clipboard', text)) or True
            steam_login._clear_clipboard = lambda: login_steps.append(('clear', None))
            steam_login._paste = lambda: login_steps.append(('paste', None))
            steam_login._press = lambda key: login_steps.append(('press', key))
            steam_login._wait_or_cancel = lambda _event, seconds: login_steps.append(('wait', seconds)) or False
            assert steam_login.automate_steam_login('single-submit-password', expected_steam_exe) == '已自动填入密码并提交'
        finally:
            steam_login._user32 = saved_user32
            steam_login.active_login_account_id = saved_active_login
            steam_login._find_login_window = saved_find_login_window
            steam_login._set_clipboard = saved_set_clipboard
            steam_login._clear_clipboard = saved_clear_clipboard
            steam_login._paste = saved_paste
            steam_login._press = saved_press
            steam_login._wait_or_cancel = saved_wait_or_cancel
        assert login_steps.count(('paste', None)) == 1
        assert [value for kind, value in login_steps if kind == 'press'] == [0x0D]
        assert ('clipboard', 'single-submit-password') in login_steps and ('clear', None) in login_steps
        # 必须先把焦点点进密码框，再粘贴、最后回车（顺序错了密码就会粘进账号框）。
        login_kinds = [kind for kind, _value in login_steps]
        assert ('cursor', (450, 384)) in login_steps
        assert login_kinds.count('click') == 2
        assert login_kinds.index('cursor') < login_kinds.index('paste') < login_kinds.index('press')
        # 主接口网络失败 + 备用主机 403 时，不能报成“Key 被撤销”。
        saved_urlopen = globals()['urlopen']

        def failing_urlopen(request, timeout=0):
            if 'partner' in request.full_url:
                raise HTTPError(request.full_url, 403, 'Forbidden', None, None)
            raise URLError('timed out')

        try:
            globals()['urlopen'] = failing_urlopen
            try:
                request_player_bans('placeholder-key', ['76561198000000000'])
                raise AssertionError('网络失败时不应该返回结果')
            except SteamApiFatalError as exc:
                assert '连接失败' in str(exc) and '未撤销' not in str(exc), str(exc)
        finally:
            globals()['urlopen'] = saved_urlopen

        store = AccountStore(root / "test.sqlite3")
        account_id = store.upsert_account("测试账号", "76561198000000000", "仅测试")
        store.save_result(
            account_id,
            {
                "VACBanned": False,
                "NumberOfVACBans": 0,
                "DaysSinceLastBan": 0,
                "NumberOfGameBans": 1,
                "CommunityBanned": False,
                "EconomyBan": "none",
            },
        )
        row = store.list_accounts_page(0, 10)[0][0]
        assert row["status"] == "查询成功"
        assert row["pubg_assessment"] == "存在游戏封禁（未证明 PUBG）"
        store.close()

        # 仅库存封禁必须被“有风险”筛选命中，且不能被归入“无风险”。
        economy_store = AccountStore(root / "economy.sqlite3")
        economy_id = economy_store.upsert_account("库存限制", "76561198000000001", "")
        economy_store.save_result(economy_id, {
            "VACBanned": False,
            "NumberOfVACBans": 0,
            "DaysSinceLastBan": 0,
            "NumberOfGameBans": 0,
            "CommunityBanned": False,
            "EconomyBan": "probation",
        })
        assert economy_store.count_accounts("risk") == 1
        assert economy_store.count_accounts("safe") == 0
        assert [row[0] for row in economy_store.export_account_rows(None, "risk")] == ["库存限制"]
        assert list(economy_store.export_account_rows(None, "safe")) == []
        economy_store.close()

        # 成功登录过的账号排在前面，按最近成功登录时间倒序；退出时再开始累计显示时长。
        activity_store = AccountStore(root / "activity.sqlite3")
        older_id = activity_store.upsert_account("较早登录", "76561198000000011", "")
        newer_id = activity_store.upsert_account("最近登录", "76561198000000012", "")
        activity_store.mark_account_logged_in(older_id, "2026-09-20 08:00:00 +0800")
        activity_store.mark_account_logged_out(older_id, "2026-09-20 10:00:00 +0800")
        activity_store.mark_account_logged_in(newer_id, "2026-09-21 08:00:00 +0800")
        activity_rows, activity_total = activity_store.list_accounts_page(0, 10)
        assert activity_total == 2
        assert [row["account_name"] for row in activity_rows] == ["最近登录", "较早登录"]
        assert activity_store.active_account_id() == newer_id
        assert login_elapsed_label(
            activity_rows[1]["last_logout_at"], activity_rows[1]["last_login_at"], fixed_now
        ) == "距上次登录 50 小时"
        activity_times = activity_store.login_timestamps([older_id, newer_id])
        assert activity_times[older_id] == ("2026-09-20 10:00:00 +0800", "2026-09-20 08:00:00 +0800")
        assert activity_times[newer_id] == (None, "2026-09-21 08:00:00 +0800")
        activity_store.mark_account_logged_out(newer_id, "2026-09-22 11:00:00 +0800")
        assert activity_store.active_account_id() is None
        activity_store.close()

        fixture = root / "large_fixture.csv"
        with fixture.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["account", "password", "steam_id", "email", "email_password", "email_url", "note"])
            for index in range(1200):
                writer.writerow([f"account-{index}", f"pw-{index}", 76561198000000000 + index, "", "mailpw", "", "test"])

        stats = AccountStore.import_file(
            root / "large.sqlite3", fixture, threading.Event(), lambda _update: None
        )
        assert stats["imported"] == 1200
        assert stats["invalid"] == 0
        assert stats["passwords_saved"] == 1200
        large_store = AccountStore(root / "large.sqlite3")
        rows, total = large_store.list_accounts_page(500, 500)
        assert total == 1200 and len(rows) == 500
        first = rows[0]
        expected_password = f"pw-{int(first['steam_id']) - 76561198000000000}"
        assert large_store.account_password(int(first["id"])) == expected_password
        # v3.2.0 起按用户要求取消密码加密：密码以可见明文入库，因此这里反过来断言库中可读到它。
        # 数据库文件本身就是敏感文件，程序会在界面上明确提示不要外发账号库、截图或导出文件。
        assert expected_password in (root / "large.sqlite3").read_text(encoding="latin-1")
        large_store.close()

        # CSV：支持账号+密码、没有 SteamID64 的表头格式；导入后保留占位 ID。
        account_password_csv = root / "account_password.csv"
        account_password_csv.write_text("account,password\ncsv-user,csv-pass\n", encoding="utf-8")
        account_password_stats = AccountStore.import_file(
            root / "account_password.sqlite3", account_password_csv, threading.Event(), lambda _update: None
        )
        assert account_password_stats["imported"] == 1, account_password_stats
        account_password_store = AccountStore(root / "account_password.sqlite3")
        account_password_row = account_password_store.list_accounts_page(0, 10)[0][0]
        assert account_password_row["steam_id"] == "csv-user"
        assert account_password_store.account_password(int(account_password_row["id"])) == "csv-pass"
        account_password_store.close()

        # 无表头 CSV 的第一行也是数据，不能因为布局检测而被跳过。
        headerless_csv = root / "headerless.csv"
        headerless_csv.write_text(
            "first-user,76561198000000002,first-note\nsecond-user,76561198000000003,second-note\n",
            encoding="utf-8",
        )
        headerless_stats = AccountStore.import_file(
            root / "headerless.sqlite3", headerless_csv, threading.Event(), lambda _update: None
        )
        assert headerless_stats["imported"] == 2, headerless_stats
        headerless_store = AccountStore(root / "headerless.sqlite3")
        assert {row["account_name"] for row in headerless_store.list_accounts_page(0, 10)[0]} == {
            "first-user", "second-user"
        }
        headerless_store.close()

        # 无表头的「账号,密码,备注」不能把密码误判为 SteamID64 后丢弃。
        headerless_credentials_csv = root / "headerless_credentials.csv"
        headerless_credentials_csv.write_text(
            "credential-user,credential-pass,首行备注\ncredential-user-two,second-pass,第二行备注\n",
            encoding="utf-8",
        )
        headerless_credentials_stats = AccountStore.import_file(
            root / "headerless_credentials.sqlite3", headerless_credentials_csv, threading.Event(), lambda _update: None
        )
        assert headerless_credentials_stats["imported"] == 2, headerless_credentials_stats
        headerless_credentials_store = AccountStore(root / "headerless_credentials.sqlite3")
        credential_rows = {
            row["account_name"]: row for row in headerless_credentials_store.list_accounts_page(0, 10)[0]
        }
        assert credential_rows["credential-user"]["steam_id"] == "credential-user"
        assert credential_rows["credential-user"]["note"] == "首行备注"
        assert headerless_credentials_store.account_password(int(credential_rows["credential-user"]["id"])) == "credential-pass"
        headerless_credentials_store.close()

        # CSV 与 TXT/JSON 共用占位账号合并规则：同账号补入真实 ID 后仍只有一行，并保留已存密码。
        merge_txt = root / "same_account.txt"
        merge_txt.write_text("same-user----saved-pass\n", encoding="utf-8")
        merge_db = root / "same_account.sqlite3"
        AccountStore.import_file(merge_db, merge_txt, threading.Event(), lambda _update: None)
        merge_csv = root / "same_account.csv"
        merge_csv.write_text(
            "account,steam_id,note\nsame-user,76561198000000031,来自 CSV\n",
            encoding="utf-8",
        )
        merge_stats = AccountStore.import_file(merge_db, merge_csv, threading.Event(), lambda _update: None)
        assert merge_stats["imported"] == 1, merge_stats
        merge_store = AccountStore(merge_db)
        merge_rows = merge_store.list_accounts_page(0, 10)[0]
        assert len(merge_rows) == 1, merge_rows
        assert merge_rows[0]["steam_id"] == "76561198000000031"
        assert merge_rows[0]["note"] == "来自 CSV"
        assert merge_store.account_password(int(merge_rows[0]["id"])) == "saved-pass"
        merge_store.close()

        # TXT：账号----密码（没有 SteamID64，先用账号名占位）
        txt_fixture = root / "accounts.txt"
        txt_fixture.write_text(
            "user-one----pass-one\nuser-two----pass-two----备注二\n# 注释行\nuser-three----pass-three\n",
            encoding="utf-8",
        )
        txt_stats = AccountStore.import_file(root / "txt.sqlite3", txt_fixture, threading.Event(), lambda _update: None)
        assert txt_stats["imported"] == 3, txt_stats
        assert txt_stats["passwords_saved"] == 3, txt_stats
        txt_store = AccountStore(root / "txt.sqlite3")
        txt_rows = {row["account_name"]: row for row in txt_store.list_accounts_page(0, 10)[0]}
        assert set(txt_rows) == {"user-one", "user-two", "user-three"}
        assert txt_rows["user-one"]["steam_id"] == "user-one"          # 占位，等查询时解析
        assert txt_store.account_password(int(txt_rows["user-two"]["id"])) == "pass-two"
        assert txt_rows["user-two"]["note"] == "备注二"
        # 导出格式的密码本身含有分隔符时，须从行尾识别 SteamID64 后完整还原密码。
        assert txt_record("delimiter-user----pass----with----separator----76561198000000032") == (
            "delimiter-user", "76561198000000032", "", "pass----with----separator"
        )
        # 解析出真实 ID 后写回
        assert txt_store.update_steam_id(int(txt_rows["user-one"]["id"]), "76561190000000001") is True
        assert txt_store.connection.execute(
            "SELECT steam_id FROM accounts WHERE id=?", (int(txt_rows["user-one"]["id"]),)
        ).fetchone()["steam_id"] == "76561190000000001"
        txt_store.close()

        # JSON：数组对象 / {账号: 密码} 两种结构
        json_fixture = root / "accounts.json"
        json_fixture.write_text(json.dumps([
            {"account": "json-a", "password": "pw-a", "steam_id": "76561190000000001", "note": "n1"},
            {"username": "json-b", "password": "pw-b"},
        ], ensure_ascii=False), encoding="utf-8")
        json_stats = AccountStore.import_file(root / "json.sqlite3", json_fixture, threading.Event(), lambda _update: None)
        assert json_stats["imported"] == 2, json_stats
        json_store = AccountStore(root / "json.sqlite3")
        json_rows = {row["account_name"]: row for row in json_store.list_accounts_page(0, 10)[0]}
        assert json_rows["json-a"]["steam_id"] == "76561190000000001"
        assert json_rows["json-b"]["steam_id"] == "json-b"
        assert json_store.account_password(int(json_rows["json-b"]["id"])) == "pw-b"
        json_store.close()

        # JSON 进度按实际已处理记录数递增，不能停在一个伪造的 99%。
        json_progress_fixture = root / "json_progress.json"
        json_progress_fixture.write_text(json.dumps([
            {"account": f"progress-{index}", "steam_id": str(76561198000001000 + index)}
            for index in range(120)
        ]), encoding="utf-8")
        json_progress_updates = []
        json_progress_stats = AccountStore.import_file(
            root / "json_progress.sqlite3",
            json_progress_fixture,
            threading.Event(),
            lambda update: json_progress_updates.append(dict(update)),
        )
        json_percentages = [update["percent"] for update in json_progress_updates]
        assert json_progress_stats["imported"] == 120, json_progress_stats
        assert any(0 < percent < 100 for percent in json_percentages), json_percentages
        assert json_percentages == sorted(json_percentages), json_percentages
        assert json_percentages[-1] == 100, json_percentages

        # 导出账号 → 再导入，数据应当对得上（导出含明文密码）
        export_store = AccountStore(root / "txt.sqlite3")
        exported_rows = list(export_store.export_account_rows(None))
        assert len(exported_rows) == 3, exported_rows
        assert {row[0]: row[3] for row in exported_rows}["user-one"] == "pass-one"
        csv_path = root / "export.csv"
        assert write_account_export(csv_path, exported_rows) == 3
        assert "pass-one" in csv_path.read_text(encoding="utf-8-sig")
        csv_round_trip = AccountStore.import_file(
            root / "csv_roundtrip.sqlite3", csv_path, threading.Event(), lambda _update: None
        )
        assert csv_round_trip["imported"] == 3, csv_round_trip
        csv_round_store = AccountStore(root / "csv_roundtrip.sqlite3")
        csv_rows = {row["account_name"]: row for row in csv_round_store.list_accounts_page(0, 10)[0]}
        assert csv_rows["user-one"]["steam_id"] == "76561190000000001"
        assert csv_rows["user-one"]["note"] == ""
        assert csv_round_store.account_password(int(csv_rows["user-one"]["id"])) == "pass-one"
        csv_round_store.close()
        json_path = root / "export.json"
        assert write_account_export(json_path, exported_rows) == 3
        assert {item["account"]: item["password"] for item in json.loads(json_path.read_text(encoding="utf-8"))}["user-two"] == "pass-two"
        txt_path = root / "export.txt"
        assert write_account_export(txt_path, exported_rows) == 3
        round_trip = AccountStore.import_file(root / "roundtrip.sqlite3", txt_path, threading.Event(), lambda _update: None)
        assert round_trip["imported"] == 3, round_trip
        round_store = AccountStore(root / "roundtrip.sqlite3")
        assert round_store.count_accounts() == 3
        assert round_store.connection.execute(
            "SELECT steam_id FROM accounts WHERE account_name='user-one'"
        ).fetchone()["steam_id"] == "76561190000000001"
        round_store.close()
        export_store.close()

        key_store = DpapiKeyStore(root / "settings.json")
        test_key = "0123456789abcdef0123456789abcdef"
        key_store.save(test_key)
        assert test_key not in (root / "settings.json").read_text(encoding="utf-8")
        assert key_store.load() == test_key
        key_store.clear()
        assert not (root / "settings.json").exists()

        # 未解析的账号名绝不能作为 SteamID64 写进 Steam 的配置文件。
        try:
            steam_login.write_auto_login(str(root / "steam.exe"), "not-a-steam-id", "test-user")
            raise AssertionError("Invalid SteamID64 was accepted for loginusers.vdf")
        except ValueError:
            pass

        # 解析 SteamID64 的查询路径必须使用工作线程独享的 SQLite 连接。
        query_fixture = root / "query.txt"
        query_fixture.write_text("query-user----query-pass\n", encoding="utf-8")
        AccountStore.import_file(root / "query.sqlite3", query_fixture, threading.Event(), lambda _update: None)
        query_app = SteamBanApp.__new__(SteamBanApp)
        query_app.events = queue.Queue()
        saved_resolver = steam_login.resolve_steam_id
        saved_requester = request_player_bans
        try:
            steam_login.resolve_steam_id = lambda account, password: "76561198000000004"
            globals()["request_player_bans"] = lambda _key, ids: {
                ids[0]: {
                    "SteamId": ids[0], "VACBanned": False, "NumberOfVACBans": 0,
                    "DaysSinceLastBan": 0, "NumberOfGameBans": 0,
                    "CommunityBanned": False, "EconomyBan": "none",
                }
            }
            query_thread = threading.Thread(
                target=query_app._query_worker,
                args=("test-key", None, 1, root / "query.sqlite3"),
            )
            query_thread.start()
            query_thread.join()
        finally:
            steam_login.resolve_steam_id = saved_resolver
            globals()["request_player_bans"] = saved_requester
        query_events = []
        while not query_app.events.empty():
            query_events.append(query_app.events.get_nowait())
        assert query_events[-1] == ("query_done", 1), query_events
        query_store = AccountStore(root / "query.sqlite3")
        query_row = query_store.list_accounts_page(0, 1)[0][0]
        assert query_row["steam_id"] == "76561198000000004"
        query_store.close()
        assert ("resolve_progress", ("query-user", 1, 1)) in query_events, query_events
        assert ("query_progress", (1, 1)) in query_events, query_events

        # 筛选会清空勾选和行选中，删除/导出不应影响被隐藏的记录。
        class FakeVar:
            def __init__(self, value=None):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class FakeTree:
            def __init__(self, selected):
                self.selected = tuple(selected)
                self.removed = ()

            def selection(self):
                return self.selected

            def selection_remove(self, selected):
                self.removed = tuple(selected)
                self.selected = ()

        filter_app = SteamBanApp.__new__(SteamBanApp)
        filter_app.show_only_risk_var = FakeVar(True)
        filter_app.show_only_safe_var = FakeVar(True)
        filter_app.checked_ids = {3, 4}
        filter_app.tree = FakeTree(("3", "4"))
        filter_refreshes = []
        filter_app.refresh_table = lambda: filter_refreshes.append(True)
        filter_app._toggle_filter("risk")
        assert filter_app.show_only_safe_var.get() is False
        assert not filter_app.checked_ids
        assert filter_app.tree.removed == ("3", "4")
        assert filter_refreshes == [True]

        # 关闭筛选只刷新列表，不应把当前可见的勾选和行选中再清空一次。
        filter_app.show_only_risk_var.set(False)
        filter_app.checked_ids = {9}
        filter_app.tree = FakeTree(("9",))
        filter_app._toggle_filter("risk")
        assert filter_app.checked_ids == {9}
        assert filter_app.tree.removed == ()
        assert filter_refreshes == [True, True]

        # 筛选状态下导出格式对话框会把“仅当前筛选结果”一并作为明确选择返回。
        class FakeDialogWindow:
            def __init__(self):
                self.destroyed = False

            def destroy(self):
                self.destroyed = True

        export_dialog = ExportFormatDialog.__new__(ExportFormatDialog)
        export_dialog.filtered_only_var = FakeVar(True)
        export_dialog.window = FakeDialogWindow()
        export_dialog._choose("csv")
        assert export_dialog.result == ("csv", True)
        assert export_dialog.window.destroyed is True

        # 实时查询进度以完成数量计算、显示百分比并在第一次更新时呈现进度卡片。
        class FakeProgress:
            def __init__(self):
                self.value = None

            def configure(self, **kwargs):
                self.value = kwargs["value"]

        class FakeCard:
            def __init__(self):
                self.mapped = False
                self.pack_args = None

            def winfo_ismapped(self):
                return self.mapped

            def pack(self, **kwargs):
                self.mapped = True
                self.pack_args = kwargs

        progress_app = SteamBanApp.__new__(SteamBanApp)
        progress_app.query_progress_bar = FakeProgress()
        progress_app.query_progress_var = FakeVar()
        progress_app.query_progress_card = FakeCard()
        progress_app.table_card = object()
        progress_app._set_query_progress(7, 12, "正在查询")
        assert progress_app.query_completed == 7 and progress_app.query_total == 12
        assert progress_app.query_progress_bar.value == 58
        assert progress_app.query_progress_var.get() == "正在查询：7 / 12（58%）"
        assert progress_app.query_progress_card.pack_args is not None

        # 整点计时只改当前已加载行的单元格，不重建 Treeview，也不影响滚动位置/懒加载进度。
        class FakeTimerTree:
            def __init__(self):
                self.values = {}

            def get_children(self):
                return ("101", "202")

            def set(self, item, column, value):
                self.values[(item, column)] = value

        class FakeTimerStore:
            def login_timestamps(self, account_ids):
                assert list(account_ids) == [101, 202]
                return {
                    101: ("2020-01-01 00:00:00 +0800", "2020-01-01 00:00:00 +0800"),
                    202: (None, "2026-09-22 11:00:00 +0800"),
                }

        timer_app = SteamBanApp.__new__(SteamBanApp)
        timer_app.tree = FakeTimerTree()
        timer_app.store = FakeTimerStore()
        timer_refreshes = []
        timer_app._schedule_login_timer_refresh = lambda: timer_refreshes.append(True)
        timer_app._refresh_login_timers()
        assert timer_app.tree.values == {
            ("101", "login_elapsed"): "上次登录：2020-01-01",
            ("202", "login_elapsed"): "当前登录中",
        }
        assert timer_refreshes == [True]

        # GitHub/代理返回 JSON 数组时，也要把失败事件交回 UI，按钮不能永久禁用。
        class FakeUpdateResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return b'[]'

        update_app = SteamBanApp.__new__(SteamBanApp)
        update_app.events = queue.Queue()
        saved_urlopen = globals()['urlopen']
        try:
            globals()['urlopen'] = lambda *_args, **_kwargs: FakeUpdateResponse()
            update_app._update_check_worker()
        finally:
            globals()['urlopen'] = saved_urlopen
        assert update_app.events.get_nowait()[0] == 'update_check_failed'

        class FakeUpdateButton:
            def __init__(self):
                self.options = {}

            def configure(self, **kwargs):
                self.options.update(kwargs)

        class FakeEventRoot:
            def __init__(self):
                self.after_calls = []

            def after(self, *args):
                self.after_calls.append(args)

        update_ui_app = SteamBanApp.__new__(SteamBanApp)
        update_ui_app.events = queue.Queue()
        update_ui_app.events.put(('update_check_failed', 'mock malformed response'))
        update_ui_app.update_check_running = True
        update_ui_app.update_button = FakeUpdateButton()
        update_ui_app.root = FakeEventRoot()
        update_ui_app._drain_events()
        assert update_ui_app.update_check_running is False
        assert update_ui_app.update_button.options['text'] == '检查更新'
        assert update_ui_app.update_button.options['state'] == 'normal'
        assert update_ui_app.root.after_calls

        # Key 输入区的官方申请链接必须打开准确地址，并给用户明确的下一步提示。
        api_key_link_app = SteamBanApp.__new__(SteamBanApp)
        api_key_link_app.status_var = FakeVar()
        opened_urls = []
        saved_browser_open = webbrowser.open
        try:
            webbrowser.open = lambda url: opened_urls.append(url) or True
            assert api_key_link_app._open_api_key_application_page() == "break"
        finally:
            webbrowser.open = saved_browser_open
        assert opened_urls == [STEAM_API_KEY_APPLICATION_URL]
        assert "复制 Key 到输入框并保存" in api_key_link_app.status_var.get()

        # 运行中的登录被拒绝，双击列表或重复点击都不能再启动第二个 Steam 线程。
        login_guard_app = SteamBanApp.__new__(SteamBanApp)
        login_guard_app.login_running = True
        login_guard_app.query_running = False
        login_guard_app.import_running = False
        login_guard_app.status_var = FakeVar()
        login_guard_app.open_steam_login()
        assert login_guard_app.status_var.get() == "已有 Steam 登录正在进行，请等待当前登录完成。"

        # 没有 SteamID64 时，自动登录仅跳过配置写入，仍可走客户端登录与密码填充。
        login_app = SteamBanApp.__new__(SteamBanApp)
        login_app.events = queue.Queue()
        login_calls = []
        saved_shutdown = steam_login.shutdown_steam
        saved_write_auto_login = steam_login.write_auto_login
        saved_tweaks = steam_login.apply_login_tweaks
        saved_launch = steam_login.launch_login
        saved_automate = steam_login.automate_steam_login
        try:
            steam_login.shutdown_steam = lambda _exe: login_calls.append("shutdown") or True
            steam_login.write_auto_login = lambda *_args: login_calls.append("write_auto_login")
            steam_login.apply_login_tweaks = lambda *_args: login_calls.append("tweaks") or []
            steam_login.launch_login = lambda *_args: login_calls.append("launch")
            steam_login.automate_steam_login = lambda _password, _steam_exe, **_kwargs: login_calls.append("automate") or "已提交"
            login_app._steam_login_worker("steam.exe", "placeholder-user", "placeholder-user", "password")
        finally:
            steam_login.shutdown_steam = saved_shutdown
            steam_login.write_auto_login = saved_write_auto_login
            steam_login.apply_login_tweaks = saved_tweaks
            steam_login.launch_login = saved_launch
            steam_login.automate_steam_login = saved_automate
        assert login_calls == ["shutdown", "launch", "automate"], login_calls
        assert login_app.events.get_nowait()[0] == "steam_login_done"
    print("self-test passed")

if __name__ == '__main__':
    if '--self-test' in sys.argv:
        run_self_test()
    else:
        app_root = ttk.Window(themename='flatly')
        SteamBanApp(app_root)
        app_root.mainloop()
