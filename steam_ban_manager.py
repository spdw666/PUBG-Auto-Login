"""Steam 封禁批量查询器（原生 Windows 桌面版）。

只使用 SteamID64 与用户自行提供的 Steam Web API Key 查询 Steam 的公开封禁字段。
不会保存或上传 Steam 账号密码，也不执行 Steam 登录、启动游戏或凭据自动化。
"""
from __future__ import annotations

import base64
import csv
import ctypes
import json
import os
import queue
import re
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


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


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
            if len(parts) == 3 and STEAM_ID_PATTERN.fullmatch(parts[2]):
                return account, parts[2], '', parts[1]
            return account, placeholder_for(account), parts[2], parts[1]
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
    for encoding in ('utf-8-sig', 'gb18030', 'utf-8'):
        try:
            sample = sample_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
        break
    else:
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
    if id_index is None:
        raise ValueError('检测到表头，但找不到 SteamID64 列。请将列名设为 steam_id、steamid64 或 64位ID。')
    return CsvLayout(
        True,
        id_index,
        next((index for index, item in enumerate(header) if item in NAME_HEADERS), None),
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
        first_is_steam_id = bool(STEAM_ID_PATTERN.fullmatch(first))
        steam_id = first if first_is_steam_id else cell(1)
        account_name = '' if first_is_steam_id else first
        note = cell(1) if first_is_steam_id else cell(2)
        password = ''
    if not STEAM_ID_PATTERN.fullmatch(steam_id):
        # 没有 64 位 ID：先用账号名占位，点「开始查询」时会自动去 Steam 解析真实 ID
        if not account_name:
            raise ValueError('缺少 SteamID64，也没有账号名可用于解析。')
        return account_name, placeholder_for(account_name), note, password
    return account_name, normalize_steam_id(steam_id), note, password


class AccountStore:
    UPSERT_SQL = """
        INSERT INTO accounts (account_name, steam_id, note, password_enc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(steam_id) DO UPDATE SET
            account_name=excluded.account_name,
            note=excluded.note,
            password_enc=COALESCE(NULLIF(excluded.password_enc, ''), accounts.password_enc)
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
                password_enc TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_accounts_steam_id ON accounts(steam_id);
            CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(account_name COLLATE NOCASE);
            """)
        existing = {row[1] for row in connection.execute('PRAGMA table_info(accounts)')}
        if 'password_enc' not in existing:
            connection.execute("ALTER TABLE accounts ADD COLUMN password_enc TEXT NOT NULL DEFAULT ''")
        connection.commit()

    @staticmethod
    def _encrypt_password(password: str) -> str:
        """用当前 Windows 用户的 DPAPI 加密 Steam 密码，空密码返回空串。"""
        if not password:
            return ''
        return base64.b64encode(DpapiKeyStore._protect(password.encode('utf-8'))).decode('ascii')

    @staticmethod
    def _decrypt_password(password_enc: str) -> str:
        """解密入库的 Steam 密码；密文损坏或换了 Windows 用户时返回空串而不是崩溃。"""
        if not password_enc:
            return ''
        try:
            return DpapiKeyStore._unprotect(base64.b64decode(password_enc)).decode('utf-8')
        except (ValueError, UnicodeDecodeError, KeyStorageError):
            return ''

    @staticmethod
    def _risk_where() -> str:
        return """
            (COALESCE(vac_banned, 0) > 0
             OR COALESCE(game_bans, 0) > 0
             OR COALESCE(community_banned, 0) > 0
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
            ' ORDER BY account_name COLLATE NOCASE, steam_id LIMIT ? OFFSET ?',
            (limit, offset),
        ).fetchall()
        return rows, total

    def account_objects(self, account_ids: Iterable[int]) -> list[Account]:
        ids = [int(item) for item in account_ids]
        if not ids:
            return []
        marks = ','.join('?' for _ in ids)
        rows = self.connection.execute(
            f'SELECT id, account_name, steam_id, note FROM accounts WHERE id IN ({marks}) ORDER BY id',
            ids,
        ).fetchall()
        return [Account(row['id'], row['account_name'], row['steam_id'], row['note']) for row in rows]

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
                marks = ','.join('?' for _ in ids)
                cursor = connection.execute(
                    f'SELECT id, account_name, steam_id, note FROM accounts WHERE id IN ({marks}) ORDER BY id',
                    ids,
                )
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
        """导入 CSV / JSON / TXT（账号----密码），按批提交；密码用 DPAPI 加密后与账号一起保存。"""
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
        """把一批记录加密后写库（records 为 (账号, ID/占位, 备注, 密码)）。"""
        for account_name, steam_id, note, password in records:
            account_name = (account_name or '').strip()
            steam_id = (steam_id or '').strip()
            if not account_name and not steam_id:
                continue
            if not steam_id:                      # 兜底：绝不允许写入空 ID（会和 UNIQUE 冲突挤成一条）
                steam_id = placeholder_for(account_name)
            if password:
                stats['passwords_saved'] += 1
            password_enc = AccountStore._encrypt_password(password)
            real_id = bool(STEAM_ID_PATTERN.fullmatch(steam_id))
            existing = None
            if account_name:
                existing = connection.execute(
                    'SELECT id, steam_id FROM accounts WHERE account_name = ? COLLATE NOCASE', (account_name,)
                ).fetchone()
            if existing is not None and (not real_id or not STEAM_ID_PATTERN.fullmatch(existing['steam_id'] or '')):
                # 同名账号只保留一行：占位行这次拿到真实 ID 就地升级，不会再多插一条
                target = steam_id if real_id else existing['steam_id']
                taken = connection.execute('SELECT id FROM accounts WHERE steam_id=?', (target,)).fetchone()
                if taken is None or int(taken['id']) == int(existing['id']):
                    with connection:
                        connection.execute(
                            'UPDATE accounts SET steam_id=?, note=?,'
                            " password_enc=COALESCE(NULLIF(?, ''), password_enc) WHERE id=?",
                            (target, note, password_enc, int(existing['id'])),
                        )
                    stats['imported'] += 1
                    continue
            with connection:
                connection.execute(AccountStore.UPSERT_SQL, (account_name, steam_id, note, password_enc))
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
            records = []
            for item in json_records(data):
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
                    stats['percent'] = min(99, int(stats['read'] * 100 / max(1, stats['read'] + 1)))
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
                for row in reader:
                    if cancel_event.is_set():
                        stats['canceled'] = True
                        break
                    try:
                        record = import_record(row, layout)
                    except ValueError:
                        stats['read'] += 1
                        stats['invalid'] += 1
                        continue
                    if record is None:
                        continue
                    stats['read'] += 1
                    account_name, steam_id, note, password = record
                    if password:
                        stats['passwords_saved'] += 1
                    pending.append((account_name, steam_id, note, AccountStore._encrypt_password(password)))
                    if len(pending) >= IMPORT_DB_BATCH_SIZE:
                        with connection:
                            connection.executemany(AccountStore.UPSERT_SQL, pending)
                        stats['imported'] += len(pending)
                        pending.clear()
                        report_progress(source)
                if pending:
                    with connection:
                        connection.executemany(AccountStore.UPSERT_SQL, pending)
                    stats['imported'] += len(pending)
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
        password_enc = self._encrypt_password(password)
        if account_id is None:
            self.connection.execute(self.UPSERT_SQL, (account_name, steam_id, note, password_enc))
            created_id = self.connection.execute(
                'SELECT id FROM accounts WHERE steam_id=?',
                (steam_id,),
            ).fetchone()['id']
        else:
            self.connection.execute(
                "UPDATE accounts SET account_name=?, steam_id=?, note=?,"
                " password_enc=COALESCE(NULLIF(?, ''), password_enc) WHERE id=?",
                (account_name, steam_id, note, password_enc, account_id),
            )
            created_id = account_id
        self.connection.commit()
        return int(created_id)

    def account_password(self, account_id: int) -> str:
        """取出某个账号保存的 Steam 密码（DPAPI 解密）；没有保存或解密失败时返回空串。"""
        row = self.connection.execute(
            'SELECT password_enc FROM accounts WHERE id=?',
            (int(account_id),),
        ).fetchone()
        if row is None:
            return ''
        return self._decrypt_password(row['password_enc'])

    def update_steam_id(self, account_id: int, steam_id: str) -> bool:
        """解析出真实 SteamID64 后写回。

        如果这个 ID 已经被另一条记录占用，说明库里已经有同账号，直接删掉这条占位记录。
        """
        steam_id = normalize_steam_id(steam_id)
        existing = self.connection.execute('SELECT id FROM accounts WHERE steam_id=?', (steam_id,)).fetchone()
        if existing is not None and int(existing['id']) != int(account_id):
            self.connection.execute('DELETE FROM accounts WHERE id=?', (int(account_id),))
            self.connection.commit()
            return False
        self.connection.execute('UPDATE accounts SET steam_id=? WHERE id=?', (steam_id, int(account_id)))
        self.connection.commit()
        return True

    def delete_accounts(self, account_ids: Iterable[int]) -> None:
        ids = [int(item) for item in account_ids]
        if not ids:
            return
        marks = ','.join('?' for _ in ids)
        self.connection.execute(f'DELETE FROM accounts WHERE id IN ({marks})', ids)
        self.connection.commit()

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

    def export_account_rows(self, account_ids: Iterable[int] | None = None) -> Iterator[tuple]:
        """导出账号（账号名、SteamID64、备注、解密后的密码）；account_ids 为 None 时导出全部。"""
        columns = 'SELECT account_name, steam_id, note, password_enc FROM accounts'
        if account_ids is None:
            cursor = self.connection.execute(columns + ' ORDER BY account_name COLLATE NOCASE, steam_id')
        else:
            ids = [int(item) for item in account_ids]
            if not ids:
                return
            marks = ','.join('?' for _ in ids)
            cursor = self.connection.execute(
                f'{columns} WHERE id IN ({marks}) ORDER BY account_name COLLATE NOCASE, steam_id', ids
            )
        for row in cursor:
            yield (row['account_name'], row['steam_id'], row['note'], self._decrypt_password(row['password_enc']))

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
    if rejected_statuses:
        codes = ', '.join(str(code) for code in sorted(set(rejected_statuses)))
        raise SteamApiFatalError('Steam 的两个兼容 API 主机都拒绝了当前 Web API Key（HTTP %s）。请在 Steam 的 API Key 页面确认该 Key 未被撤销，重新生成后完整复制粘贴，再重新查询。' % codes)
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

        self.name_var = StringVar(value=initial.account_name if initial else '')
        self.steam_var = StringVar(value=initial.steam_id if initial else '')
        self.note_var = StringVar(value=initial.note if initial else '')
        self.password_var = StringVar(value=password)

        frame = ttk.Frame(self.window, padding=18)
        frame.grid(sticky='nsew')
        ttk.Label(frame, text='账号标签（可选）').grid(row=0, column=0, sticky='w', pady=(0, 7))
        ttk.Entry(frame, textvariable=self.name_var, width=42).grid(row=0, column=1, sticky='ew', pady=(0, 7))
        ttk.Label(frame, text='SteamID64 *').grid(row=1, column=0, sticky='w', pady=7)
        steam_entry = ttk.Entry(frame, textvariable=self.steam_var, width=42)
        steam_entry.grid(row=1, column=1, sticky='ew', pady=7)
        ttk.Label(frame, text='备注（可选）').grid(row=2, column=0, sticky='w', pady=7)
        ttk.Entry(frame, textvariable=self.note_var, width=42).grid(row=2, column=1, sticky='ew', pady=7)
        ttk.Label(frame, text='Steam 密码（自动登录用）').grid(row=3, column=0, sticky='w', pady=7)
        ttk.Entry(frame, textvariable=self.password_var, width=42, show='*').grid(row=3, column=1, sticky='ew', pady=7)
        ttk.Label(
            frame,
            text='密码以当前 Windows 用户的 DPAPI 加密后保存在本机数据库，仅用于自动登录 Steam。',
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
        try:
            steam_id = normalize_steam_id(self.steam_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.window)
            return
        self.result = (self.name_var.get().strip(), steam_id, self.note_var.get().strip(), self.password_var.get())
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
            writer.writerow([account_name, steam_id, note, password])
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
    """导出账号前先问一下要什么格式。"""

    def __init__(self, parent: Any):
        self.window = Toplevel(parent)
        self.window.title("导出账号")
        self.window.transient(parent)
        self.result = None
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="选择导出格式", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="文件里是明文密码，请只保存在自己机器上。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(4, 10))
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
        self.result = kind
        self.window.destroy()


class SteamBanApp:
    columns = (
        ("pick", "选择", 52),
        ("account_name", "账号标签", 150),
        ("steam_id", "SteamID64", 175),
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

        self.base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        self.store = AccountStore(self.base_dir / "steam_ban_accounts.sqlite3")
        cleared_key_failures = self.store.clear_rejected_key_failures()
        self.key_store = DpapiKeyStore(self.base_dir / "steam_ban_settings.json")
        try:
            saved_api_key = self.key_store.load()
            key_load_problem = ""
        except KeyStorageError as exc:
            saved_api_key = None
            key_load_problem = str(exc)

        self.api_key_var = StringVar(value=saved_api_key or "")
        self.remember_key_var = BooleanVar(value=bool(saved_api_key))
        self.key_state_var = StringVar(value="已加密保存" if saved_api_key else "仅本次运行")
        self.status_var = StringVar(
            value=(
                f"已清除上次 API Key 被拒绝造成的 {cleared_key_failures} 条无效失败标记。请更换有效 Key 后重新查询。"
                if cleared_key_failures
                else key_load_problem or "就绪：请添加或导入 SteamID64，然后输入自己的 Steam Web API Key。"
            )
        )
        self.count_var = StringVar(value="0 个账号")
        self.list_hint_var = StringVar(value="正在准备账号清单…")
        self.show_only_risk_var = BooleanVar(value=False)
        self.show_only_safe_var = BooleanVar(value=False)
        self.checked_ids = set()
        self._menu_account_id = None
        self.events = queue.Queue()
        self.query_running = False
        self.import_running = False
        self.import_cancel_event = None
        self.loaded_rows = 0
        self.table_total = 0
        self.loading_table_rows = False
        self._build_ui()
        self.refresh_table()
        self.root.after(120, self._drain_events)

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
        ttk.Label(sidebar, text="安全设计", style="SidebarKicker.TLabel").pack(anchor="w")
        ttk.Label(sidebar, text="支持 CSV / JSON / TXT（账号----密码）；密码用本机 DPAPI 加密保存。", style="SidebarHint.TLabel", justify="left").pack(anchor="w", pady=(6, 0))
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
        ttk.Label(header, text="Windows 桌面版", style="Badge.TLabel").pack(side="right", anchor="n", pady=(6, 0))

        key_card = ttk.Frame(outer, style="Card.TFrame", padding=UiMetrics.CARD_PADDING)
        key_card.pack(fill="x", pady=(0, UiMetrics.SECTION_GAP))
        key_heading = ttk.Frame(key_card, style="Card.TFrame")
        key_heading.pack(fill="x", pady=(0, 10))
        ttk.Label(key_heading, text="连接与凭据", style="SectionTitle.TLabel").pack(side="left")
        self.key_state_label = ttk.Label(key_heading, textvariable=self.key_state_var, style="SuccessBadge.TLabel")
        self.key_state_label.pack(side="right")
        key_row = ttk.Frame(key_card, style="Card.TFrame")
        key_row.pack(fill="x")
        ttk.Label(key_row, text="Steam Web API Key", style="Body.TLabel").pack(anchor="w", pady=(0, 6))
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

        table_card = ttk.Frame(outer, style="Card.TFrame", padding=UiMetrics.CARD_PADDING)
        table_card.pack(fill="both", expand=True)
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
            self.tree.column(name, width=width, minwidth=45, anchor="center" if name not in {"note", "account_name", "pubg"} else "w")
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

    def open_steam_login(self, account_id: int | None = None) -> None:
        """用本机保存的账号密码自动登录指定账号；不传就登录列表里选中的那个。"""
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
        self._set_controls(False)
        self.status_var.set(f"正在退出当前 Steam 并登录账号 {account_name}，请稍候…")
        threading.Thread(
            target=self._steam_login_worker,
            args=(steam_exe, account_name, account.steam_id, password),
            daemon=True,
        ).start()

    def _steam_login_worker(self, steam_exe: str, account_name: str, steam_id: str, password: str) -> None:
        """后台线程：结束当前 Steam，写入自动登录配置，再带账号密码启动客户端。"""
        try:
            if not steam_login.shutdown_steam(steam_exe):
                self.events.put(("steam_login_failed", "无法结束当前 Steam 进程，请手动退出 Steam 后重试。"))
                return
            steam_login.write_auto_login(steam_exe, steam_id, account_name)
            tweaks = steam_login.apply_login_tweaks(steam_exe, steam_id)
            steam_login.launch_login(steam_exe, account_name, password)
            login_result = steam_login.automate_steam_login(password)
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
                f"账号 {account_name}：{login_result}{applied}。若 Valve 要求 Steam Guard 验证码，请在客户端里完成一次验证；"
                "该账号在本机验证过一次后，之后即可直接自动登录。",
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
            or row["status"] == "查询失败"
        )
        tag = "risk" if risk and row["status"] != "查询失败" else "failure" if row["status"] == "查询失败" else "success" if row["status"] == "查询成功" else ""
        values = (
            "■" if row["id"] in self.checked_ids else "□",
            row["account_name"],
            row["steam_id"] if STEAM_ID_PATTERN.fullmatch(row["steam_id"] or "") else "未解析（查询时自动获取）",
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
        """导出账号（含密码）：先选格式，再选保存位置；勾选/选中的账号优先，没勾选就导出全部。"""
        format_dialog = ExportFormatDialog(self.root)
        self.root.wait_window(format_dialog.window)
        if not format_dialog.result:
            return
        kind = format_dialog.result
        suffix = {'csv': '.csv', 'txt': '.txt', 'json': '.json'}[kind]
        filters = {
            'csv': [('CSV 文件', '*.csv')],
            'txt': [('文本文件（账号----密码）', '*.txt')],
            'json': [('JSON 文件', '*.json')],
        }[kind]
        ids = self._selected_ids()
        scope = f"勾选/选中的 {len(ids)} 个账号" if ids else "全部账号"
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
            count = write_account_export(target, self.store.export_account_rows(ids or None))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"导出失败：{exc}", parent=self.root)
            return
        self.status_var.set(f"已导出 {count} 个账号到 {target}（含明文密码，注意保管）。")

    def start_query(self, query_all: bool) -> None:
        if self.query_running or self.import_running:
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
        self.status_var.set(f"正在查询 0 / {total} 条记录…")
        threading.Thread(target=self._query_worker, args=(api_key, selected_ids, total), daemon=True).start()

    def _resolve_batch_ids(self, batch: list, failures: int):
        """把这一批里缺 SteamID64 的账号解析出来（账号密码 -> SteamID64）。

        返回 (可继续查询的账号, 连续失败次数, 需要中止时的提示)。
        """
        resolved = []
        for account in batch:
            if STEAM_ID_PATTERN.fullmatch(account.steam_id):
                resolved.append(account)
                continue
            name = account.account_name or account.steam_id
            password = self.store.account_password(account.account_id)
            if not password:
                self.store.save_result(account.account_id, None, "缺少密码，无法解析 SteamID64（请编辑该账号填入密码后重试）")
                continue
            self.events.put(("resolve_progress", name))
            try:
                real_id = steam_login.resolve_steam_id(name, password)
            except steam_login.SteamAuthError as exc:
                failures += 1
                self.store.save_result(account.account_id, None, f"无法解析 SteamID64：{exc}")
                if failures >= 5:
                    return resolved, failures, f"连续 {failures} 个账号解析 SteamID64 失败（最后一次：{exc}）。已停止，请检查账号密码或稍后再试。"
                continue
            except Exception as exc:
                failures += 1
                self.store.save_result(account.account_id, None, f"无法解析 SteamID64：{exc!r}")
                continue
            failures = 0
            if self.store.update_steam_id(account.account_id, real_id):
                resolved.append(Account(account.account_id, account.account_name, real_id, account.note))
            time.sleep(1.0)          # 放慢节奏，降低被 Steam 限流的概率
        return resolved, failures, None

    def _query_worker(self, api_key: str, account_ids: list[int] | None, total: int) -> None:
        completed = 0
        resolve_failures = 0
        for batch in AccountStore.account_batches(self.store.db_path, account_ids):
            batch, resolve_failures, stop_message = self._resolve_batch_ids(batch, resolve_failures)
            if stop_message:
                self.events.put(("query_blocked", (completed, total, stop_message)))
                return
            if not batch:
                completed += resolve_failures
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
            completed += len(batch)
            self.events.put(("query_progress", (completed, total)))
            if completed < total:
                time.sleep(0.35)
        self.events.put(("query_done", total))

    def _drain_events(self) -> None:
        changed = False
        processed = 0
        try:
            while processed < MAX_UI_EVENTS_PER_TICK:
                kind, payload = self.events.get_nowait()
                processed += 1
                if kind == "result_batch":
                    self.store.save_results_batch(payload)
                elif kind == "query_progress":
                    completed, total = payload
                    self.status_var.set(f"正在查询 {completed} / {total} 条记录…")
                elif kind == "resolve_progress":
                    self.status_var.set(f"正在解析 SteamID64：{payload}…（需要账号+密码，每个约 1 秒）")
                elif kind == "query_done":
                    self.query_running = False
                    self._set_controls(not self.import_running)
                    self.status_var.set(f"查询完成：共处理 {payload} 条记录。")
                    changed = True
                elif kind == "query_blocked":
                    completed, total, message = payload
                    self.query_running = False
                    self._set_controls(not self.import_running)
                    self.status_var.set(f"查询已停止：已完成 {completed} / {total} 条；没有把未查询账号标记为失败。")
                    messagebox.showerror(APP_NAME, message, parent=self.root)
                    changed = completed > 0
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
                    self._set_controls(not self.query_running)
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
                    self._set_controls(not self.query_running)
                    self.status_var.set("导入失败。")
                    messagebox.showerror(APP_NAME, f"无法导入文件：{payload}", parent=self.root)
                elif kind == "steam_login_done":
                    self._set_controls(not (self.query_running or self.import_running))
                    self.status_var.set(payload)
                elif kind == "steam_login_failed":
                    self._set_controls(not (self.query_running or self.import_running))
                    self.status_var.set("自动登录未完成。")
                    messagebox.showerror(APP_NAME, f"自动登录失败：{payload}", parent=self.root)
        except queue.Empty:
            pass
        if changed:
            self.refresh_table()
        self.root.after(30 if processed else 120, self._drain_events)

    def _on_close(self) -> None:
        self.api_key_var.set("")
        if self.import_cancel_event is not None:
            self.import_cancel_event.set()
        self.store.close()
        self.root.destroy()


# NOTE: these three imports are needed for byte-identical code generation:
# the compiler emits the generic call form (LOAD_ATTR without NULL|self +
# PUSH_NULL) for attribute calls on names bound by an "import" statement.
# The module header contains the same imports; duplicates are harmless.


def run_self_test() -> None:
    assert normalize_steam_id("76561198000000000") == "76561198000000000"
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
        assert expected_password not in (root / "large.sqlite3").read_text(encoding="latin-1")
        large_store.close()

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

        # 导出账号 → 再导入，数据应当对得上（导出含明文密码）
        export_store = AccountStore(root / "txt.sqlite3")
        exported_rows = list(export_store.export_account_rows(None))
        assert len(exported_rows) == 3, exported_rows
        assert {row[0]: row[3] for row in exported_rows}["user-one"] == "pass-one"
        csv_path = root / "export.csv"
        assert write_account_export(csv_path, exported_rows) == 3
        assert "pass-one" in csv_path.read_text(encoding="utf-8-sig")
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
    print("self-test passed")

if __name__ == '__main__':
    if '--self-test' in sys.argv:
        run_self_test()
    else:
        app_root = ttk.Window(themename='flatly')
        SteamBanApp(app_root)
        app_root.mainloop()
