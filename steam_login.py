r"""Steam 客户端自动登录辅助模块。

只做本地 Steam 客户端控制：定位安装目录、读取/写入 config\loginusers.vdf、
结束当前 Steam 会话、用已保存的账号密码启动 Steam 登录。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from typing import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VDF_ACCOUNT_FIELDS = ("AccountName", "PersonaName", "RememberPassword", "WantsOfflineMode",
                      "SkipOfflineModeWarning", "AutoLogin", "Timestamp")

# 闭合括号前的缩进不一定是制表符：Steam 自己写的是 \n\t}，其它工具可能写成空格或不缩进。
_ENTRY_RE = re.compile(r'"(\d{17})"\s*\{(.*?)\r?\n[ \t]*\}', re.S)
_KV_RE = re.compile(r'"([^"]+)"\s*"([^"]*)"')
_EMPTY_USERS = '"users"\n{\n}\n'


def find_steam_exe() -> str | None:
    """返回 steam.exe 路径；先查注册表，再查常见安装位置。"""
    candidates: list[str] = []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            for name in ("SteamExe", "SteamPath"):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if value:
                    text = str(value).replace("/", "\\")
                    candidates.append(text if text.lower().endswith(".exe") else os.path.join(text, "steam.exe"))
    except OSError:
        pass
    for extra in (r"C:\Program Files (x86)\Steam\steam.exe", r"C:\Program Files\Steam\steam.exe",
                  r"D:\Steam\steam.exe", r"D:\steam\steam.exe"):
        candidates.append(extra)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def loginusers_path(steam_exe: str) -> Path:
    return Path(steam_exe).parent / "config" / "loginusers.vdf"


def parse_loginusers(text: str) -> dict[str, dict[str, str]]:
    """解析 loginusers.vdf -> {SteamID64: {字段: 值}}；解析失败返回空 dict。"""
    users: dict[str, dict[str, str]] = {}
    for steam_id, body in _ENTRY_RE.findall(text):
        users[steam_id] = dict(_KV_RE.findall(body))
    return users


def render_loginusers(users: dict[str, dict[str, str]], bom: bool = False) -> str:
    out = ['"users"', "{"]
    for steam_id, fields in users.items():
        out.append('\t"%s"' % steam_id)
        out.append("\t{")
        names = [n for n in VDF_ACCOUNT_FIELDS if n in fields]
        names += [n for n in fields if n not in VDF_ACCOUNT_FIELDS]
        for name in names:
            out.append('\t\t"%s"\t\t"%s"' % (name, fields[name]))
        out.append("\t}")
    out.append("}")
    return ("\ufeff" if bom else "") + "\n".join(out) + "\n"


def set_auto_login(users: dict[str, dict[str, str]], steam_id: str, account_name: str) -> dict[str, dict[str, str]]:
    """把目标账号标记为记住密码 + 自动登录，其它账号取消自动登录。"""
    entry = users.setdefault(steam_id, {})
    entry.setdefault("PersonaName", account_name)
    entry.setdefault("WantsOfflineMode", "0")
    entry.setdefault("SkipOfflineModeWarning", "0")
    entry["AccountName"] = account_name
    entry["RememberPassword"] = "1"
    entry["AutoLogin"] = "1"
    entry["Timestamp"] = str(int(time.time()))
    for other_id, other in users.items():
        if other_id != steam_id:
            other["AutoLogin"] = "0"
            other["MostRecent"] = "0"
    entry["MostRecent"] = "1"
    return users


def write_auto_login(steam_exe: str, steam_id: str, account_name: str) -> Path:
    """改写 loginusers.vdf 并同步注册表 AutoLoginUser，返回被改写的文件路径。

    写入前备份为 loginusers.vdf.bak；解析不到任何已有账号时放弃写入，避免清空账号列表。
    """
    steam_id = str(steam_id or '').strip()
    if not re.fullmatch(r'\d{17}', steam_id):
        raise ValueError('SteamID64 必须是 17 位数字；未解析的账号不能改写 loginusers.vdf')
    path = loginusers_path(steam_exe)
    raw = path.read_bytes() if path.is_file() else b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="surrogateescape") if raw else _EMPTY_USERS
    users = parse_loginusers(text)
    if not users and text.strip() not in ("", _EMPTY_USERS.strip()):
        raise RuntimeError("无法解析 loginusers.vdf，已放弃写入以避免丢失账号列表")
    if raw:
        path.with_suffix(".vdf.bak").write_bytes(raw)
    set_auto_login(users, steam_id, account_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_loginusers(users, bom=bom).encode("utf-8", errors="surrogateescape"))
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "AutoLoginUser", 0, winreg.REG_SZ, account_name)
            # Steam 账号切换器（V9）会同时写这几个值：记住密码 + 允许自动登录，
            # 这样下次切换回该账号时 Steam 会用缓存的令牌直接进去，不用再传密码。
            winreg.SetValueEx(key, "AutoLoginUser_steamchina", 0, winreg.REG_SZ, account_name)
            winreg.SetValueEx(key, "RememberPassword", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "AllowAutoLogin", 0, winreg.REG_DWORD, 1)
    except OSError:
        pass
    return path


STEAM_PROCESSES = ("steam.exe", "steamwebhelper.exe", "GameOverlayUI.exe")


def _process_running(image: str) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % image, "/NH"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return image.lower() in out.lower()


def steam_running() -> bool:
    """Steam 主进程或它的界面进程还活着就算在运行。"""
    return any(_process_running(image) for image in STEAM_PROCESSES)


def shutdown_steam(steam_exe: str, timeout: float = 40.0) -> bool:
    """先请求 Steam 正常退出，超时后强制结束。返回是否已无 steam.exe 在运行。"""
    if not steam_running():
        return True
    try:
        subprocess.Popen([steam_exe, "-shutdown"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not steam_running():
            time.sleep(2.0)          # 让残留的界面进程彻底退出，否则新实例会自己退出
            return True
        time.sleep(1.5)
    for image in STEAM_PROCESSES:
        subprocess.run(["taskkill", "/IM", image, "/F"], capture_output=True, text=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(2.0)
    return not steam_running()


def launch_login(steam_exe: str, account: str, password: str = '') -> subprocess.Popen:
    """带账号密码直接启动 Steam（这就是 Steam 账号切换器类工具的标准做法）。

    实测（2026-09，本机 Steam）：steam.exe -noreactlogin -login 账号 密码 会在 10 秒左右
    直接登录成功，完全不需要模拟键盘或鼠标；不带密码时 Steam 只会弹出登录框并停在那里。
    代价是密码会出现在进程命令行里（任务管理器/WMI 可见），所以只在真正需要登录时调用。
    """
    command = [steam_exe, "-noreactlogin", "-login", account]
    if password:
        command.append(password)
    return subprocess.Popen(command,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

# ---------------------------------------------------------------------------
# Steam 客户端设置写入（登录首页改为库 / 关闭推销广告弹窗 / 关闭好友列表弹窗，
# 写的都是 Steam 自己的配置文件）
# ---------------------------------------------------------------------------

ACCOUNT_ID_OFFSET = 76561197960265728
VDF_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\{|\}|//[^\n]*')


def account_id_from_steam_id(steam_id64: str) -> str:
    """SteamID64 换算成 32 位账号 ID，也就是 userdata 目录名。"""
    steam_id64 = str(steam_id64 or '').strip()
    if not re.fullmatch(r'\d{17}', steam_id64):
        raise ValueError('SteamID64 必须是 17 位数字')
    return str(int(steam_id64) - ACCOUNT_ID_OFFSET)


def _vdf_unescape(text: str) -> str:
    return text.replace('\\\\', '\\').replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')


def _vdf_escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')


def _parse_block(text: str, pos: int) -> tuple[list, int]:
    """解析一段 KeyValues 文本，返回 (条目列表, 结束位置)；条目记录键名、值或子块及原文位置。"""
    items = []
    length = len(text)
    while pos < length:
        match = VDF_TOKEN_RE.search(text, pos)
        if match is None:
            break
        token = match.group()
        pos = match.end()
        if token.startswith('//'):
            continue
        if token == '}':
            return items, pos
        if token == '{':
            continue
        key = _vdf_unescape(token[1:-1])
        inner = VDF_TOKEN_RE.search(text, pos)
        if inner is None:
            break
        body = inner.group()
        if body == '{':
            children, end = _parse_block(text, inner.end())
            items.append({'key': key, 'block': children, 'open': inner.end(), 'close': end})
            pos = end
        else:
            items.append({'key': key, 'value': _vdf_unescape(body[1:-1]),
                          'val_start': inner.start(), 'val_end': inner.end()})
            pos = inner.end()
    return items, pos


def _find(items, key):
    for item in items:
        if item['key'].lower() == key.lower():
            return item
    return None


def _find_path(items: list, key: str, prefix: list = None) -> list:
    """在整棵 VDF 树里按键名查找，返回从根键之下开始的完整路径；找不到返回 None。"""
    prefix = prefix or []
    for item in items:
        if item['key'].lower() == key.lower():
            return prefix + [item['key']]
        if 'block' in item:
            found = _find_path(item['block'], key, prefix + [item['key']])
            if found:
                return found
    return None


def _get_path(items: list, path: list):
    """按路径取标量值；路径不存在或指向块时返回 None。"""
    node = items
    for key in path:
        item = _find(node, key)
        if item is None:
            return None
        if 'block' in item:
            node = item['block']
        else:
            return item['value']
    return None


def _insert_indent(text: str, open_pos: int) -> str:
    line_start = text.rfind('\n', 0, open_pos) + 1
    prefix = text[line_start:open_pos]
    return prefix[:len(prefix) - len(prefix.lstrip())] + '\t'


def _set_vdf_path(text: str, path: list, value: str) -> tuple:
    """把 VDF 里的 path（从根键下一层算起）设为 value，缺层级会补建。返回 (新文本, 是否改动)。"""
    root = re.search(r'"([^"\n]+)"\s*\{', text)
    if root is None:
        return text, False
    if path and path[0].lower() == root.group(1).lower():
        path = path[1:]  # 路径允许带根键名，这里统一按“根键之下”处理
    items, _ = _parse_block(text, root.end())
    node_items, node_open = items, root.end() - 1
    for step, key in enumerate(path):
        item = _find(node_items, key)
        if item is None:
            tail = node_items[-1]['close'] if node_items and 'close' in node_items[-1] else node_open + 1
            pad0 = _insert_indent(text, node_open)
            remaining = path[step:]
            added = ''
            for depth, name in enumerate(remaining):
                pad = pad0 + '\t' * depth
                if depth == len(remaining) - 1:
                    added += '%s"%s"\t\t"%s"\n' % (pad, name, _vdf_escape(value))
                else:
                    added += '%s"%s"\n%s{\n' % (pad, name, pad)
            for depth in range(len(remaining) - 2, -1, -1):
                added += '%s}\n' % (pad0 + '\t' * depth)
            lead = '\n' if text[tail - 1] != '\n' else ''
            return text[:tail] + lead + added + text[tail:], True
        if 'block' in item:
            node_items, node_open = item['block'], item['open']
            continue
        if item['value'] == value:
            return text, False
        return text[:item['val_start']] + '"%s"' % _vdf_escape(value) + text[item['val_end']:], True
    return text, False


def _patch_vdf_many(path: Path, edits: list, backup_dir: Path) -> bool:
    """一次写入多处改动（同一个文件只备份一次）；字节级保留原有换行与 BOM。"""
    if not path.is_file():
        return False
    raw = path.read_bytes()
    bom = raw.startswith(b'\xef\xbb\xbf')
    # Steam 的配置文件里可能残留 ANSI/GBK 字节（老游戏、云存档）。用 surrogateescape 原样保留，
    # 既不会抛 UnicodeDecodeError 中断登录，也不会在写回时把原有字节改成问号。
    text = raw.decode('utf-8-sig', errors='surrogateescape')
    changed = False
    for keys, value in edits:
        text, one = _set_vdf_path(text, keys, value)
        changed = changed or one
    if not changed:
        return False
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        if not (backup_dir / path.name).is_file():   # 保留最原始的一份，不要被后续写入覆盖
            (backup_dir / path.name).write_bytes(raw)
    except OSError:
        pass
    path.write_bytes((b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8', errors='surrogateescape'))
    return True


def _patch_vdf(path: Path, keys: list, value: str, backup_dir: Path) -> bool:
    """把某个 VDF 文件里的一个键设为指定值。"""
    return _patch_vdf_many(path, [(keys, value)], backup_dir)


MINIMAL_LOCALCONFIG = '"UserLocalConfigStore"\n{\n}\n'
MINIMAL_SHAREDCONFIG = '"UserRoamingConfigStore"\n{\n}\n'


def _ensure_config_files(account_dir: Path) -> bool:
    """首次登录的账号还没有配置文件；先建最小骨架，设置才写得进去。返回是否新建过。"""
    created = False
    local = account_dir / 'config' / 'localconfig.vdf'
    shared = account_dir / '7' / 'remote' / 'sharedconfig.vdf'
    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        shared.parent.mkdir(parents=True, exist_ok=True)
        if not local.is_file():
            local.write_text(MINIMAL_LOCALCONFIG, encoding='utf-8')
            created = True
        if not shared.is_file():
            shared.write_text(MINIMAL_SHAREDCONFIG, encoding='utf-8')
            created = True
    except OSError:
        pass
    return created


def apply_login_tweaks(steam_exe: str, steam_id64: str, open_library: bool = True,
                       disable_promo: bool = True, disable_friends: bool = True) -> list:
    """给指定账号写入 Steam 客户端设置，返回真正做了改动的条目。"""
    notes = []
    account_id = account_id_from_steam_id(steam_id64)
    account_dir = Path(steam_exe).parent / 'userdata' / account_id
    try:
        account_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ['无法创建该账号的 userdata 目录，已跳过 Steam 设置写入']
    if _ensure_config_files(account_dir):
        notes.append('首次登录：已建立配置文件')
    backup_dir = account_dir / 'config' / 'steam_ban_tool_backup'
    shared = account_dir / '7' / 'remote' / 'sharedconfig.vdf'
    local = account_dir / 'config' / 'localconfig.vdf'
    if open_library:
        # 三个配置存储都写一遍：按账号的漫游配置、本地配置、以及全局 config.vdf
        done = False
        for target, path_keys in (
            (shared, ['Software', 'Valve', 'Steam', 'SteamDefaultDialog']),
            (local, ['Software', 'Valve', 'Steam', 'SteamDefaultDialog']),
            (Path(steam_exe).parent / 'config' / 'config.vdf',
             ['Software', 'Valve', 'Steam', 'SteamDefaultDialog']),
        ):
            if _patch_vdf(target, path_keys, '#app_games', backup_dir):
                done = True
        if done:
            notes.append('登录首页改为库')
    if disable_promo and _patch_vdf(local, ['news', 'NotifyAvailableGames'], '0', backup_dir):
        notes.append('关闭推销广告弹窗')
    if disable_friends:
        # 只让好友窗口不自动弹出，同时确保“登录好友系统”是开的，否则账号会显示成离线
        if _hide_friends_window(local, account_id, backup_dir):
            notes.append('好友窗口不自动弹出')
        repaired = _friends_signin(shared, backup_dir, True)
        if _patch_vdf(local, ['friends', 'SignIntoFriends'], '1', backup_dir):
            repaired = True
        if repaired:
            notes.append('保持好友在线')
    _sync_remotecache(account_dir, backup_dir)
    return notes


def _friends_signin(shared: Path, backup_dir: Path, enabled: bool = True) -> bool:
    r"""开关“登录好友系统”（漫游配置 FriendsUI\FriendsUIJSON 里的 bSignIntoFriends）。

    注意：关掉它账号会显示成“离线”，所以这里默认只做“确保是开的”，
    用来纠正旧版本把它写成 false 造成的离线状态。
    """
    if not shared.is_file():
        return False
    raw = shared.read_text(encoding='utf-8-sig', errors='ignore')
    root = re.search(r'"([^"\n]+)"\s*\{', raw)
    if root is None:
        return False
    items, _ = _parse_block(raw, root.end())
    path = _find_path(items, 'FriendsUIJSON')
    state = {}
    if path is not None:
        current = _get_path(items, path)
        if current:
            try:
                loaded = json.loads(current)
                if isinstance(loaded, dict):
                    state = loaded
            except ValueError:
                state = {}
    if bool(state.get('bSignIntoFriends', True)) is enabled:
        return False
    state['bSignIntoFriends'] = enabled
    if path is None:
        path = ['Software', 'Valve', 'Steam', 'FriendsUI', 'FriendsUIJSON']
    return _patch_vdf(shared, path, json.dumps(state, separators=(',', ':')), backup_dir)


def _sync_remotecache(account_dir: Path, backup_dir: Path) -> None:
    """同步 Steam 云缓存登记：让客户端认为本地 sharedconfig.vdf 就是已同步的版本。"""
    cache = account_dir / '7' / 'remotecache.vdf'
    shared = account_dir / '7' / 'remote' / 'sharedconfig.vdf'
    if not cache.is_file() or not shared.is_file():
        return
    raw = shared.read_bytes()
    now = str(int(time.time()))
    _patch_vdf_many(cache, [
        (['sharedconfig.vdf', 'size'], str(len(raw))),
        (['sharedconfig.vdf', 'sha'], hashlib.sha1(raw).hexdigest()),
        (['sharedconfig.vdf', 'localtime'], now),
        (['sharedconfig.vdf', 'time'], now),
        (['sharedconfig.vdf', 'remotetime'], now),
    ], backup_dir)


# ---------------------------------------------------------------------------
# Steam 登录窗口自动填密码
#
# 实测（2026-09）：现在的 Steam 客户端不再接受命令行上的密码，
# "steam.exe -noreactlogin -login 账号 密码" 只会把账号名填好并弹出「登录 Steam」窗口。
# 这类工具都是靠 Win32 输入注入把密码打进这个窗口的，
# 这里用同样的办法：找到窗口 → 切到前台 → 粘贴密码 → 回车。
# ---------------------------------------------------------------------------

LOGIN_WINDOW_TITLES = ('登录 Steam', 'Sign in to Steam', 'Steam 登录', '登入 Steam', 'Steam 登入')
# 客户端启动早期的窗口标题里也带 Steam，别把密码打进这些窗口。
LOGIN_WINDOW_TITLE_EXCLUDES = ('更新', 'updat', 'bootstrapper', 'install', 'waiting')
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STEAM_LOGIN_PROCESS_NAMES = frozenset({'steam.exe', 'steamwebhelper.exe'})
LOGIN_WINDOW_TIMEOUT = 60.0
# 正常情况下 Steam 启动后 4~5 秒就会弹出登录窗口（或直接用记住的信息进去）。
# 10 秒还没有任何动静就说明出问题了，此刻立刻诊断并告知原因，而不是继续干等。
LOGIN_DIAGNOSE_AFTER = 10.0
# 登录窗口 4~5 秒就会出来，但密码要通过 Steam 与服务器的连接提交。网络慢/走代理时
# 先等它变成 [Connected] 再填密码，避免提交时 Steam 还没连上。
LOGIN_CONNECTION_WAIT = 45.0
# 命令行带上密码后，Steam 通常 5~10 秒就登录完成；给它 60 秒上限。
LOGIN_COMMAND_TIMEOUT = 60.0
# 命令行带上账号密码后 Steam 通常 5~10 秒自己就登录成功了。登录窗口只是过程中的旧版
# 对话框，先给它这段时间；真的登录成功就完全不需要模拟键盘鼠标（这也正是 V9 的做法）。
LOGIN_COMMAND_GRACE = 20.0
LOGIN_PROGRESS_INTERVAL = 2.0
LOGIN_FORM_READY_DELAY = 2.5
LOGIN_SUBMIT_RESULT_WAIT = 15.0
# ``import ctypes.wintypes`` 只会把模块挂在 ctypes 命名空间；下面的 Win32 调用使用
# wintypes.DWORD / HANDLE 等短名称，因此必须显式绑定，不能依赖 ctypes 的属性访问。
wintypes = ctypes.wintypes


class LoginAutomationCancelled(Exception):
    """用户在等待 Steam 登录窗口时主动取消自动填密。"""


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ('dx', ctypes.wintypes.LONG),
        ('dy', ctypes.wintypes.LONG),
        ('mouseData', ctypes.wintypes.DWORD),
        ('dwFlags', ctypes.wintypes.DWORD),
        ('time', ctypes.wintypes.DWORD),
        ('dwExtraInfo', ctypes.c_void_p),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [('mi', _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', ctypes.wintypes.DWORD), ('u', _InputUnion)]


_INPUT_MOUSE = 0
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_VK_MENU = 0x12
_VK_CONTROL = 0x11
_VK_V = 0x56
_VK_TAB = 0x09
_VK_RETURN = 0x0D
_VK_A = 0x41
_VK_DELETE = 0x2E
_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
_WIN32_DECLARED = False


def _declare_win32() -> None:
    """声明用到的 Win32 原型（只做一次）。

    ctypes 对没声明的函数按 32 位 int 处理返回值：GlobalAlloc / GlobalLock /
    GetForegroundWindow 这类返回指针或句柄的函数一旦被截断，就会崩成
    "access violation writing 0x00000000"；keybd_event / SendInput 有 64 位参数，
    也必须显式声明，否则第 4、5 个参数可能带上垃圾高位。
    """
    global _WIN32_DECLARED
    if _WIN32_DECLARED or os.name != 'nt':
        return
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    W = ctypes.wintypes
    user32.IsWindowVisible.argtypes = [W.HWND]
    user32.IsWindowVisible.restype = W.BOOL
    user32.IsWindow.argtypes = [W.HWND]
    user32.IsWindow.restype = W.BOOL
    user32.IsIconic.argtypes = [W.HWND]
    user32.IsIconic.restype = W.BOOL
    user32.GetWindowTextLengthW.argtypes = [W.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    user32.GetWindowThreadProcessId.restype = W.DWORD
    user32.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
    user32.ShowWindow.restype = W.BOOL
    user32.SetForegroundWindow.argtypes = [W.HWND]
    user32.SetForegroundWindow.restype = W.BOOL
    user32.BringWindowToTop.argtypes = [W.HWND]
    user32.BringWindowToTop.restype = W.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = W.HWND
    user32.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
    user32.AttachThreadInput.restype = W.BOOL
    user32.OpenClipboard.argtypes = [W.HWND]
    user32.OpenClipboard.restype = W.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = W.BOOL
    user32.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
    user32.SetClipboardData.restype = W.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = W.BOOL
    user32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
    user32.GetWindowRect.restype = W.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = W.BOOL
    user32.SendInput.argtypes = [W.UINT, ctypes.POINTER(_Input), ctypes.c_int]
    user32.SendInput.restype = W.UINT
    user32.keybd_event.argtypes = [W.BYTE, W.BYTE, W.DWORD, ctypes.c_void_p]
    user32.keybd_event.restype = None
    user32.EnumWindows.argtypes = [_WNDENUMPROC, W.LPARAM]
    user32.EnumWindows.restype = W.BOOL
    kernel32.GlobalAlloc.argtypes = [W.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = W.HGLOBAL
    kernel32.GlobalLock.argtypes = [W.HGLOBAL]
    kernel32.GlobalLock.restype = W.LPVOID
    kernel32.GlobalUnlock.argtypes = [W.HGLOBAL]
    kernel32.GlobalUnlock.restype = W.BOOL
    kernel32.GlobalFree.argtypes = [W.HGLOBAL]
    kernel32.GlobalFree.restype = W.HGLOBAL
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = W.DWORD
    _WIN32_DECLARED = True


def _user32():
    _declare_win32()
    return ctypes.windll.user32


def _kernel32():
    _declare_win32()
    return ctypes.windll.kernel32


def _is_login_window_title(title: str) -> bool:
    """判断窗口标题是不是 Steam 的登录窗口。

    先精确匹配常见语言，再退化为"标题里带 Steam"——韩/日/俄/德等客户端只有本地化标题，
    精确列表覆盖不全，但它们的标题里都含 Steam。
    """
    text = (title or '').strip()
    if not text:
        return False
    if text in LOGIN_WINDOW_TITLES:
        return True
    lowered = text.casefold()
    if 'steam' not in lowered:
        return False
    return not any(marker in lowered for marker in LOGIN_WINDOW_TITLE_EXCLUDES)


def _window_process_image(hwnd: int) -> str | None:
    """返回顶层窗口所属进程的完整可执行文件路径；查询失败时不信任该窗口。"""
    if os.name != 'nt':
        return None
    user32 = _user32()
    kernel32 = _kernel32()
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return None
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value)
    if not process:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(process)


def _is_expected_steam_process_image(process_image: str | None, steam_exe: str) -> bool:
    """只接受当前 Steam 安装目录的 steam.exe / steamwebhelper.exe 窗口。

    单靠标题包含“Steam”会把浏览器中的 Steam Community 页面误认为登录框，密码可能被
    粘贴到网页。steam.exe 必须正好是启动的那个文件；登录页通常由
    ``bin\\cef\\...\\steamwebhelper.exe`` 承载，所以对 webhelper 允许安装目录树内的
    子路径。这一校验是输入注入前的硬门槛。
    """
    if not process_image or not steam_exe:
        return False
    candidate = Path(process_image)
    candidate_name = candidate.name.casefold()
    if candidate_name not in STEAM_LOGIN_PROCESS_NAMES:
        return False
    expected_executable = os.path.normcase(os.path.normpath(str(Path(steam_exe))))
    expected_directory = os.path.normcase(os.path.normpath(str(Path(steam_exe).parent)))
    candidate_path = os.path.normcase(os.path.normpath(str(candidate)))
    if candidate_name == 'steam.exe':
        return candidate_path == expected_executable
    try:
        # commonpath 而不是字符串 startswith：D:\\Steam-old 不能被误当成 D:\\Steam 的子目录。
        return os.path.commonpath((candidate_path, expected_directory)) == expected_directory
    except ValueError:
        # 不同盘符或非法路径不可能属于当前 Steam 安装目录。
        return False


def _window_looks_like_login_dialog(hwnd: int) -> bool:
    """窗口必须是可见、未最小化、尺寸正常的对话框。

    最小化的窗口（位置 -21333,-21333、客户区只有 158x26）标题同样含 Steam，
    如果把它当成登录窗口，后面按它的矩形算出来的坐标全是垃圾，密码会填到空气里。
    """
    user32 = _user32()
    if not user32.IsWindowVisible(hwnd):
        return False
    if user32.IsIconic(hwnd):
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    return (rect.right - rect.left) >= 320 and (rect.bottom - rect.top) >= 240


def _find_login_window(steam_exe: str) -> int:
    """找到当前 Steam 安装目录所属的登录窗口句柄；没找到返回 0。

    标题只能用于缩小范围，不能作为身份认证：先精确匹配已知登录标题（最可信），
    再退化为“标题含 Steam”，并且只接受可见、未最小化、尺寸正常的窗口。
    任何无法验证进程路径的窗口一律跳过。
    """
    user32 = _user32()
    exact: list[int] = []
    loose: list[int] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(hwnd, buffer, length + 2)
        title = (buffer.value or '').strip()
        if not _is_login_window_title(title):
            return True
        if not _is_expected_steam_process_image(_window_process_image(hwnd), steam_exe):
            return True
        if not _window_looks_like_login_dialog(hwnd):
            return True
        if title in LOGIN_WINDOW_TITLES:
            exact.append(hwnd)
        else:
            loose.append(hwnd)
        return True

    user32.EnumWindows(_WNDENUMPROC(callback), 0)
    if exact:
        return exact[0]
    return loose[0] if loose else 0


def _key(vk: int, up: bool = False) -> None:
    _user32().keybd_event(vk, 0, 2 if up else 0, None)


def _press(vk: int) -> None:
    _key(vk)
    time.sleep(0.05)
    _key(vk, True)
    time.sleep(0.12)


def _bring_to_foreground(hwnd: int, attempts: int = 12, delay: float = 0.4) -> bool:
    """把登录窗口切到前台，失败会重试。

    Windows 的前台锁会拒绝后台进程直接 SetForegroundWindow，这里依次尝试：
    直接设置、先模拟一次 ALT 按键解锁、再把本线程输入队列临时附加到目标窗口线程。
    都失败时返回 False——绝不在没有前台的情况下盲发按键，那会把密码打进别的窗口。
    """
    user32 = _user32()
    kernel32 = _kernel32()
    for _ in range(max(1, attempts)):
        if user32.GetForegroundWindow() == hwnd:
            return True
        user32.ShowWindow(hwnd, 9)
        _key(_VK_MENU)
        _key(_VK_MENU, True)
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() == hwnd:
            return True
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = kernel32.GetCurrentThreadId()
        if target_thread and target_thread != current_thread:
            if user32.AttachThreadInput(current_thread, target_thread, True):
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    user32.AttachThreadInput(current_thread, target_thread, False)
            if user32.GetForegroundWindow() == hwnd:
                return True
        time.sleep(delay)
    return user32.GetForegroundWindow() == hwnd


def _click_at(x: int, y: int) -> None:
    """在屏幕坐标处点一下左键（SendInput，结构体按 64 位声明）。"""
    user32 = _user32()
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    for flag in (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP):
        item = _Input(type=_INPUT_MOUSE, u=_InputUnion(mi=_MouseInput(0, 0, 0, flag, 0, None)))
        user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_Input))
        time.sleep(0.05)


def _click_password_field(hwnd: int) -> bool:
    """点一下登录窗口里的密码输入框，把键盘焦点确定地放进去。

    CEF 登录页的焦点不一定在密码框（-login 预填失败、页面重绘、用户刚点过别处），
    此时盲贴剪贴板会把密码粘进账号框，Steam 随后报“账号或密码错误”。旧版能登录正是靠
    类似的点击/Tab 兜底，这里把这一步做成确定的一次点击。
    """
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return False
    # 实测（700x440 的 Steam 登录窗）：密码框中心在窗口高度的 34% 处。
    # 点 42% 会落到密码框下方的空白，粘贴随之落空、回车还会提交一个空表单。
    _click_at(rect.left + int(width * 0.5), rect.top + int(height * 0.34))
    return True


def _set_clipboard(text: str) -> bool:
    """把文本放进剪贴板（Unicode），成功后由调用方负责清空。"""
    user32, kernel32 = _user32(), _kernel32()
    if not user32.OpenClipboard(None):
        return False
    handle = None
    try:
        user32.EmptyClipboard()
        size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        ctypes.memmove(pointer, ctypes.create_unicode_buffer(text), size)
        kernel32.GlobalUnlock(handle)
        if user32.SetClipboardData(CF_UNICODETEXT, handle):
            handle = None
            return True
        return False
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


def _clear_clipboard() -> None:
    _set_clipboard('')


def _paste() -> None:
    _key(_VK_CONTROL)
    _press(_VK_V)
    _key(_VK_CONTROL, True)


def _wait_or_cancel(cancel_event: threading.Event | None, seconds: float) -> bool:
    """等待 seconds；若用户取消则立即返回 True。"""
    if cancel_event is None:
        time.sleep(seconds)
        return False
    return cancel_event.wait(seconds)


def _raise_if_login_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise LoginAutomationCancelled('已取消自动登录等待。')


def active_login_account_id() -> int | None:
    r"""读取 Steam 自己记录的当前登录账号（32 位账号 ID）；未登录返回 None。

    Steam 登录成功后会写 HKCU\Software\Valve\Steam\ActiveProcess\ActiveUser，
    这是判断“它已经自己进去了”最可靠的信号。
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess') as key:
            value, _kind = winreg.QueryValueEx(key, 'ActiveUser')
        return int(value) or None
    except (OSError, ValueError):
        return None


# connection_log.txt 里的状态标记 -> 连接状态（后面的状态覆盖前面的）
_CONNECTION_STATES = (
    ('[Logged On', 'logged_on'),
    ('[Logging On', 'connected'),
    ('[Connected', 'connected'),
    ('[Connecting', 'connecting'),
    ('[Logged Off', 'connecting'),
)


def steam_connection_state(steam_exe: str, max_age: float = 300.0) -> str:
    """读 Steam 自己的 connection_log.txt，返回最近的连接状态。

    logged_on  —— 已经登录成功
    connected  —— 连上了 Steam 服务器（可以提交密码）
    connecting —— 还在连接（此时提交密码多半会失败，通常是被代理/VPN 拦了）
    unknown    —— 读不到日志
    """
    log_path = Path(steam_exe).parent / 'logs' / 'connection_log.txt'
    try:
        if not log_path.is_file():
            return 'unknown'
        stat = log_path.stat()
        if time.time() - stat.st_mtime > max_age:
            return 'unknown'
        with log_path.open('rb') as handle:
            handle.seek(max(0, stat.st_size - 262144))
            tail = handle.read().decode('utf-8', 'replace')
    except OSError:
        return 'unknown'
    state = 'unknown'
    for line in tail.splitlines()[-400:]:
        for marker, value in _CONNECTION_STATES:
            if marker in line:
                state = value
                break
    return state


def wait_for_steam_connection(
    steam_exe: str,
    timeout: float = LOGIN_CONNECTION_WAIT,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
) -> bool:
    """等 Steam 连上服务器（[Connected] / [Logged On]）；超时返回 False。"""
    started = time.time()
    deadline = started + max(1.0, timeout)
    last_report = 0.0
    while time.time() < deadline:
        _raise_if_login_cancelled(cancel_event)
        if steam_connection_state(steam_exe) in ('connected', 'logged_on'):
            return True
        elapsed = time.time() - started
        if progress is not None and elapsed - last_report >= LOGIN_PROGRESS_INTERVAL:
            last_report = elapsed
            progress(elapsed)
        if _wait_or_cancel(cancel_event, 1.0):
            _raise_if_login_cancelled(cancel_event)
    return steam_connection_state(steam_exe) in ('connected', 'logged_on')


def steam_is_connecting(steam_exe: str, max_age: float = 180.0) -> bool:
    """从 Steam 自己的 connection_log.txt 判断它是不是还卡在连接服务器。

    登录窗口迟迟不出现时用它区分两种情况：网络/代理导致 Steam 连不上（要用户处理），
    还是 Steam 已经用记住的登录信息进去了。
    """
    log_path = Path(steam_exe).parent / 'logs' / 'connection_log.txt'
    try:
        if not log_path.is_file():
            return False
        stat = log_path.stat()
        if time.time() - stat.st_mtime > max_age:
            return False
        with log_path.open('rb') as handle:
            handle.seek(max(0, stat.st_size - 65536))
            tail = handle.read().decode('utf-8', 'replace')
    except OSError:
        return False
    recent = tail.splitlines()[-80:]
    if any('[Logged On' in line for line in recent):
        return False
    return any(
        ('[Connecting' in line) or ('PingWebSocketCM' in line) or ('YieldingConnect' in line)
        for line in recent
    )


def describe_steam_startup(steam_exe: str) -> str:
    """Steam 启动后迟迟没有任何反应时的原因判断（给用户看的短句）。"""
    if not steam_running():
        return 'Steam 进程没有启动起来（可能被杀软/代理拦了，或客户端安装损坏）'
    if steam_is_connecting(steam_exe):
        return ('Steam 还在连接 Steam 服务器（代理/VPN 环境很常见）：请把 steamserver.net、'
                'steampowered.com、steamcommunity.com 设为直连后重试')
    return 'Steam 既没有自动登录、也没有弹出登录窗口（可能窗口被别的程序挡住，或客户端状态异常）'


def connection_log_size(steam_exe: str) -> int:
    """当前 connection_log.txt 的字节数（用来只认本次会话新写的日志）。"""
    log_path = Path(steam_exe).parent / 'logs' / 'connection_log.txt'
    try:
        return log_path.stat().st_size
    except OSError:
        return 0


def logged_on_since(steam_exe: str, start_offset: int) -> bool:
    """本次会话（start_offset 之后新写入的日志）里是否出现 [Logged On。

    不能只看注册表 ActiveUser：Steam 被结束后那个值不会清零，会把上一次的账号
    当成本次登录成功（假成功）。日志才是分会话的。
    """
    log_path = Path(steam_exe).parent / 'logs' / 'connection_log.txt'
    try:
        if not log_path.is_file():
            return False
        size = log_path.stat().st_size
        if size <= start_offset:
            return False
        with log_path.open('rb') as handle:
            handle.seek(start_offset)
            fresh = handle.read().decode('utf-8', 'replace')
    except OSError:
        return False
    return '[Logged On' in fresh


def wait_for_logged_on(
    steam_exe: str,
    timeout: float,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
    start_offset: int | None = None,
    start_account: int | None = None,
) -> bool:
    """等本次会话登录成功（只认新写入的日志或 ActiveUser 变化）。"""
    if start_offset is None:
        start_offset = connection_log_size(steam_exe)
    if start_account is None:
        start_account = active_login_account_id()
    started = time.time()
    deadline = started + max(0.5, timeout)
    last_report = 0.0
    while time.time() < deadline:
        _raise_if_login_cancelled(cancel_event)
        if logged_on_since(steam_exe, start_offset):
            return True
        account_id = active_login_account_id()
        if account_id and account_id != start_account:
            return True
        elapsed = time.time() - started
        if progress is not None and elapsed - last_report >= LOGIN_PROGRESS_INTERVAL:
            last_report = elapsed
            progress(elapsed)
        if _wait_or_cancel(cancel_event, 0.5):
            _raise_if_login_cancelled(cancel_event)
    return logged_on_since(steam_exe, start_offset)


def wait_for_steam_login(
    steam_exe: str,
    timeout: float = LOGIN_WINDOW_TIMEOUT,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
    diagnose: Callable[[str], None] | None = None,
) -> tuple[str, int]:
    """等 Steam 进入可处理状态，返回 (状态, 句柄或账号 ID)。

    logged_in    —— Steam 已经用本机记住的登录信息自己进去了（loginusers.vdf + 注册表那条路），不需要密码
    login_window —— 出现登录窗口，需要注入密码
    timeout      —— 都没有等到（常见于本机没有该账号的缓存令牌 + 网络慢/连不上）
    """
    started = time.time()
    deadline = started + max(1.0, timeout)
    last_report = 0.0
    diagnosed = False
    start_account = active_login_account_id()
    start_offset = connection_log_size(steam_exe)
    while time.time() < deadline:
        _raise_if_login_cancelled(cancel_event)
        account_id = active_login_account_id()
        if logged_on_since(steam_exe, start_offset):
            return 'logged_in', account_id or 0
        if account_id and account_id != start_account:
            return 'logged_in', account_id
        hwnd = _find_login_window(steam_exe)
        if hwnd:
            return 'login_window', hwnd
        elapsed = time.time() - started
        if not diagnosed and elapsed >= LOGIN_DIAGNOSE_AFTER and diagnose is not None:
            diagnosed = True
            diagnose(describe_steam_startup(steam_exe))
        if progress is not None and elapsed - last_report >= LOGIN_PROGRESS_INTERVAL:
            last_report = elapsed
            progress(elapsed)
        if _wait_or_cancel(cancel_event, 0.5):
            _raise_if_login_cancelled(cancel_event)
    return 'timeout', 0


def automate_steam_login(
    password: str,
    steam_exe: str,
    timeout: float = LOGIN_WINDOW_TIMEOUT,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
    diagnose: Callable[[str], None] | None = None,
) -> str:
    """等 Steam 自己登录或弹出登录窗口；只有后者才需要注入密码。"""
    if os.name != 'nt':
        return '自动填密码仅支持 Windows'
    state, value = wait_for_steam_login(steam_exe, timeout, cancel_event, progress, diagnose)
    if state == 'logged_in':
        # 两条路都会到这里：Steam 用缓存令牌免密进去，或命令行账号密码直接登录成功。
        return '已自动登录成功（Steam 用命令行账号密码或本机记住的登录信息直接进入，全程没有模拟键盘鼠标）'
    if state == 'timeout':
        return f'{int(timeout)} 秒内没有等到 Steam 登录窗口，也没检测到 Steam 已登录：{describe_steam_startup(steam_exe)}'
    hwnd = value
    # 先让命令行账号密码自己登录（实测 5~10 秒）。成功就返回，完全不模拟键盘鼠标。
    if wait_for_logged_on(steam_exe, LOGIN_COMMAND_GRACE, cancel_event, progress):
        return '已自动登录成功（命令行账号密码，全程没有模拟键盘鼠标）'
    user32 = _user32()
    if not _bring_to_foreground(hwnd):
        return 'Steam 登录窗口没能切到前台，已跳过自动填密码；请手动点一下 Steam 窗口后重试'
    if _wait_or_cancel(cancel_event, LOGIN_FORM_READY_DELAY):
        _raise_if_login_cancelled(cancel_event)
    if user32.GetForegroundWindow() != hwnd:
        return 'Steam 登录窗口被其它窗口抢到前台，已跳过自动填密码'
    # 实测：即使 Steam 此刻还显示 connecting（代理环境常见），提交密码后它会在连上
    # 服务器的瞬间完成登录，所以这里不再等待连接，避免白等几十秒。
    connection_note = ''
    if steam_connection_state(steam_exe) == 'connecting':
        connection_note = '（注意：Steam 当时仍在连接服务器；若未登录成功，请先解决代理/VPN）'
    if not _set_clipboard(password):
        return '剪贴板不可用，已跳过自动填密码'
    try:
        _raise_if_login_cancelled(cancel_event)
        # 先把焦点确定地放进密码框，再粘贴一次、只提交一次。旧版靠“Tab/点击 + 反复粘贴”
        # 兜底；纯盲贴会在焦点仍处于账号框时把密码填进账号框，Steam 便报“账号或密码错误”。
        _click_password_field(hwnd)
        if _wait_or_cancel(cancel_event, 0.4):
            _raise_if_login_cancelled(cancel_event)
        _paste()
        if _wait_or_cancel(cancel_event, 0.6):
            _raise_if_login_cancelled(cancel_event)
        _press(_VK_RETURN)
        submit_offset = connection_log_size(steam_exe)
        submit_account = active_login_account_id()
        deadline = time.time() + max(1.0, LOGIN_SUBMIT_RESULT_WAIT)
        while time.time() < deadline:
            if _wait_or_cancel(cancel_event, 1.0):
                _raise_if_login_cancelled(cancel_event)
            if logged_on_since(steam_exe, submit_offset):
                return '密码提交成功，Steam 已登录' + connection_note
            current = active_login_account_id()
            if current and current != submit_account:
                return '密码提交成功，Steam 已登录' + connection_note
            if not user32.IsWindow(hwnd):
                return '已自动填入密码并提交' + connection_note
        return '已填入密码并提交一次；Steam 仍停在登录窗口，请按提示完成 Steam Guard 或手动继续' + connection_note
    finally:
        # 任一 Win32 调用异常、线程被上层捕获或登录窗口消失时都不能把密码留在剪贴板。
        _clear_clipboard()


# ---------------------------------------------------------------------------
# 用「账号 + 密码」查 SteamID64
#
# 依据 Valve 的 IAuthenticationService 接口（参考开源实现 bukson/steampy 的 login.py）：
#   1. GET  GetPasswordRSAPublicKey?account_name=<账号>   -> publickey_mod / publickey_exp / timestamp
#   2. 用 RSA(PKCS#1 v1.5) 加密密码
#   3. POST BeginAuthSessionViaCredentials                -> 返回里带 steamid
# 第 3 步的返回就包含 SteamID64，所以**不需要等待 Steam Guard 验证码**，拿到就放弃这次会话。
# ---------------------------------------------------------------------------

AUTH_API_BASE = 'https://api.steampowered.com/IAuthenticationService'
AUTH_HEADERS = {
    'Referer': 'https://steamcommunity.com/',
    'Origin': 'https://steamcommunity.com',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Steam Client',
}
ERESULT_MESSAGES = {
    5: '密码错误',
    20: '账号或密码错误',
    63: '该账号需要 Steam 令牌验证码',
    65: '验证码错误次数过多，稍后再试',
    84: '请求过于频繁，被 Steam 限流了（等一会儿再试）',
    85: '登录尝试过多，账号被暂时限制（等一段时间再试）',
    88: '验证码不正确',
}


class SteamAuthError(RuntimeError):
    """解析 SteamID64 失败。"""


def _rsa_encrypt(password: bytes, modulus: int, exponent: int) -> bytes:
    """PKCS#1 v1.5 加密（纯标准库实现）。"""
    size = (modulus.bit_length() + 7) // 8
    if len(password) > size - 11:
        raise SteamAuthError('密码过长，无法加密')
    padding = bytes(byte or 1 for byte in os.urandom(size - len(password) - 3))
    block = b'\x00\x02' + padding + b'\x00' + password
    return pow(int.from_bytes(block, 'big'), exponent, modulus).to_bytes(size, 'big')


def _auth_json(request) -> dict:
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode('utf-8', 'ignore'))


def _auth_error(payload: dict, status: int) -> SteamAuthError:
    """把 Steam 的报错翻译成人话。"""
    eresult = None
    message = ''
    response = payload.get('response') if isinstance(payload, dict) else None
    if isinstance(response, dict):
        error = response.get('error')
        if isinstance(error, dict):
            eresult = error.get('eresult')
            message = str(error.get('message') or '')
    if eresult in ERESULT_MESSAGES:
        return SteamAuthError(ERESULT_MESSAGES[eresult])
    if message:
        return SteamAuthError('Steam 返回：%s' % message)
    return SteamAuthError('Steam 没有返回账号信息（HTTP %s）' % status)


def resolve_steam_id(account: str, password: str) -> str:
    """用账号密码换 SteamID64；失败抛 SteamAuthError（错误信息可直接显示给用户）。"""
    if not account or not password:
        raise SteamAuthError('缺少账号或密码')
    key_url = '%s/GetPasswordRSAPublicKey/v1/?%s' % (AUTH_API_BASE, urlencode({'account_name': account}))
    try:
        key_payload = _auth_json(Request(key_url, headers=AUTH_HEADERS))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise SteamAuthError('无法连接 Steam：%s' % exc) from exc
    key = (key_payload or {}).get('response') or {}
    if not key.get('publickey_mod') or not key.get('publickey_exp') or not key.get('timestamp'):
        raise SteamAuthError('Steam 没有返回加密公钥（可能是账号名写错或网络被拦）')
    encrypted = base64.b64encode(
        _rsa_encrypt(password.encode('utf-8'), int(key['publickey_mod'], 16), int(key['publickey_exp'], 16))
    )
    body = urlencode({
        'persistence': '1',
        'encrypted_password': encrypted,
        'account_name': account,
        'encryption_timestamp': key['timestamp'],
    }).encode('ascii')
    request = Request('%s/BeginAuthSessionViaCredentials/v1/' % AUTH_API_BASE, data=body, headers=AUTH_HEADERS)
    try:
        payload = _auth_json(request)
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode('utf-8', 'ignore'))
        except (ValueError, OSError):
            raise SteamAuthError('Steam 拒绝了这次认证（HTTP %s）' % getattr(exc, 'code', '?')) from exc
        raise _auth_error(payload, getattr(exc, 'code', 0)) from exc
    except (URLError, OSError, ValueError) as exc:
        raise SteamAuthError('无法连接 Steam：%s' % exc) from exc
    response = (payload or {}).get('response') or {}
    steam_id = str(response.get('steamid') or '').strip()
    if re.fullmatch(r'\d{17}', steam_id):
        return steam_id
    raise _auth_error(payload, 200)


def _hide_friends_window(local: Path, account_id: str, backup_dir: Path) -> bool:
    """让好友窗口在登录时别弹出来：只改窗口自身的显示状态，与在线状态无关。"""
    if not local.is_file():
        return False
    raw = local.read_text(encoding='utf-8-sig', errors='ignore')
    root = re.search(r'"([^"\n]+)"\s*\{', raw)
    if root is None:
        return False
    items, _ = _parse_block(raw, root.end())
    path = _find_path(items, 'ChatStorePopupState_%s' % account_id)
    state = {}
    if path is not None:
        current = _get_path(items, path)
        if current and current.strip().startswith('{'):
            try:
                loaded = json.loads(current)
                if isinstance(loaded, dict):
                    state = loaded
            except ValueError:
                state = {}
    else:
        path = ['WebStorage', 'ChatStorePopupState_%s' % account_id]
    if state.get('bFriendsListVisible') is False and state.get('always_restore') is False:
        return False
    state['bFriendsListVisible'] = False
    state['always_restore'] = False
    state.setdefault('bFriendsListCollapsed', False)
    return _patch_vdf(local, path, json.dumps(state, separators=(',', ':')), backup_dir)

