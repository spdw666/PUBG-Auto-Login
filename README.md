# PUBG自动登陆系统

Windows 桌面工具：**批量导入 Steam/PUBG 账号 → 一键自动登录 → 批量查询封禁状态**。

当前发布版：`v4.1.2`。

> 请只用于管理**你自己拥有**的账号。批量登录/账号共享可能违反 Steam 用户协议，风险自负。

## 功能

- **导入**：CSV / JSON / TXT（`账号----密码`），也可以直接粘贴文本导入
- **自动登录**：结束当前 Steam → 用账号密码直接启动 Steam 完成登录 → 写入客户端设置（首页为库、关闭推销广告与好友列表弹窗）
- **补全 SteamID64**：只有账号密码也能用，查询时自动解析出 64 位 ID（不需要 Steam Guard 验证码）
- **批量查询封禁**：用你自己的 Steam Web API Key 查 VAC / 游戏 / 社区 / 库存封禁，并给出 PUBG 关联判断
- **账号列表**：最近导入或登录的账号排在最上面；勾选多选、双击直接登录、右键菜单（登录/编辑/删除）、仅显示有风险或无风险；密码默认隐藏，点「显示密码」查看
- **导出**：账号导出为 CSV / JSON / TXT
- **数据不丢**：账号库与 Key 固定保存在 `%LOCALAPPDATA%\PUBG-Auto-Login\`，换版本、换下载目录都继续用同一份；旧版数据首次启动自动继承

## 目录结构

```
steam_ban_manager.py   主程序（界面 / 账号库 / 导入导出 / 封禁查询）
steam_login.py         Steam 客户端控制（自动登录、写客户端设置、SteamID64 解析）
编译成exe.cmd          一键打包成单文件 exe
自检.cmd               运行内置自检
使用说明.md            详细使用说明
```

## 运行 / 打包 / 自检

```bat
py -3.14 -m pip install ttkbootstrap
py -3.14 steam_ban_manager.py

py -3.14 -m PyInstaller --noconfirm --clean --onefile --windowed --name "PUBG自动登陆系统" steam_ban_manager.py

py -3.14 steam_ban_manager.py --self-test
```

> `steam_ban_manager.py` 里 `import steam_login`，两个文件必须放在同一目录一起打包。

## 需要你自己准备

- **Steam Web API Key**：<https://steamcommunity.com/dev/apikey>（程序里也有一键打开的链接）
- 本机已安装 Steam 客户端（自动登录、写客户端设置需要）

## 数据与隐私

- 账号库与 API Key 都存在本机 `%LOCALAPPDATA%\PUBG-Auto-Login\`；API Key 用 Windows DPAPI 加密，**换 Windows 用户或换机器解不开**
- 账号密码为便于核对与导出按明文保存，**账号库、程序截图、导出文件都属敏感资料，不要外发**
- 本仓库**不包含任何账号数据**：`.gitignore` 已排除 `*.sqlite3` / `steam_ban_settings.json` / 账号清单文件

## 免责声明

本项目仅供学习与个人账号管理使用。使用者需自行承担因批量登录、账号共享等行为导致的账号封禁、限流等风险，作者不对任何后果负责。
