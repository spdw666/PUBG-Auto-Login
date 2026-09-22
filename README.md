# PUBG自动登陆系统

Windows 桌面工具：**批量导入 Steam/PUBG 账号 → 一键自动登录 → 批量查询封禁状态**。

> 请只用于管理**你自己拥有**的账号。批量登录/账号共享可能违反 Steam 用户协议，风险自负。

## 功能

| 功能 | 说明 |
|---|---|
| 多格式导入 | CSV / TSV / JSON / TXT（`账号----密码`）；支持仅 `account,password` 的两列表头 CSV，也支持无表头 CSV；后缀不认识也能按内容识别；还能**直接粘贴**导入 |
| 缺少 SteamID64 也能用 | 只有账号+密码时，查询阶段自动调用 Valve `IAuthenticationService` 解析出 SteamID64（不需要 Steam Guard 验证码） |
| 一键自动登录 | 结束当前 Steam → 启动 Steam → **自动把密码填进登录窗口**；已有 SteamID64 时还会改写 `loginusers.vdf` 与注册表（Win32 输入注入，参考同类工具做法） |
| 登录时写入 Steam 设置 | 已解析 SteamID64 的账号会写入：首页改为库、关闭推销广告弹窗、好友窗口不自动弹出（**保持在线状态**） |
| 批量查询封禁 | 用你自己的 Steam Web API Key 查 VAC / 游戏封禁 / 社区封禁 / 库存封禁，并给出 PUBG 关联判断 |
| 列表操作 | 最左列勾选多选、全选/清空、**双击账号 = 直接登录**、**右键 = 登录/编辑/删除**、仅显示有风险 / 仅显示无风险 |
| 导出 | 账号导出（CSV / JSON / TXT，可选范围）、查询结果导出 |
| 本地加密 | 账号密码用 Windows DPAPI 加密后存本机，只有当前 Windows 用户能解开 |

## 目录结构

```
steam_ban_manager.py   主程序（界面 / 账号库 / CSV 导入 / 封禁查询 / 导出）
steam_login.py         Steam 客户端控制（自动登录、填密码、写客户端设置、SteamID 解析）
编译成exe.cmd          一键打包成单文件 exe
自检.cmd               运行内置自检
使用说明.md            详细使用说明（含各功能的实现原理与注意事项）
```

## 运行（源码方式）

```bat
py -3.14 -m pip install ttkbootstrap
py -3.14 steam_ban_manager.py
```

## 打包成 exe

```bat
py -3.14 -m pip install --upgrade pyinstaller ttkbootstrap pillow
py -3.14 -m PyInstaller --noconfirm --clean --onefile --windowed --name "PUBG自动登陆系统" steam_ban_manager.py
```

或直接双击 `编译成exe.cmd`。产物在 `dist\` 下（单文件，约 22 MB）。

> 注意：`steam_ban_manager.py` 里 `import steam_login`，两个文件必须放在同一目录一起打包。

## 自检

```bat
py -3.14 steam_ban_manager.py --self-test
```

覆盖：CSV/JSON/TXT 三种导入（含无表头 CSV、仅账号+密码 CSV）、密码 DPAPI 加解密回读、明文不入库、CSV/TXT/JSON 导出→再导入往返、封禁结果写入与库存封禁筛选、分页、API Key 加解密、未解析 SteamID64 的配置写入保护。返回 0 即通过。

## 需要你自己准备

- **Steam Web API Key**：<https://steamcommunity.com/dev/apikey>（程序里填，会 DPAPI 加密保存在本机）
- 本机已安装 Steam 客户端（自动登录/写设置需要）

## 数据与隐私

- 账号密码：DPAPI 加密后存在程序同目录的 `steam_ban_accounts.sqlite3`，**换 Windows 用户/换机器解不开**
- API Key：DPAPI 加密存在 `steam_ban_settings.json`
- 本仓库**不包含任何账号数据**：`.gitignore` 已排除 `*.sqlite3` / `steam_ban_settings.json` / 账号清单文件
- 导出账号时程序会提醒：导出文件是**明文密码**，别外发

## 免责声明

本项目仅供学习与个人账号管理使用。使用者需自行承担因批量登录、账号共享等行为导致的账号封禁、限流等风险，作者不对任何后果负责。
