# PUBG自动登陆系统

Windows 桌面工具：**批量导入 Steam/PUBG 账号 → 一键自动登录 → 批量查询封禁状态**。

当前发布版：`v3.1.5`。

> 请只用于管理**你自己拥有**的账号。批量登录/账号共享可能违反 Steam 用户协议，风险自负。

## 功能

| 功能 | 说明 |
|---|---|
| 多格式导入 | CSV / TSV / JSON / TXT（`账号----密码`）；支持表头 `account,password`，及无表头的 `账号,SteamID64,备注`、`账号,密码,备注`；同账号的占位记录与后续真实 ID 会合并；后缀不认识也能按内容识别；还能**直接粘贴**导入 |
| 缺少 SteamID64 也能用 | 只有账号+密码时，查询阶段自动调用 Valve `IAuthenticationService` 解析出 SteamID64（不需要 Steam Guard 验证码） |
| 一键自动登录 | 结束当前 Steam → 启动 Steam → **自动把密码填进登录窗口**；已有 SteamID64 时还会改写 `loginusers.vdf` 与注册表；登录进行中会拒绝重复启动，避免双击拉起两个 Steam 线程 |
| 登录计时与排序 | 本工具下一次成功退出已登录账号时开始计时；72 小时内显示“距上次登录 N 小时”，之后显示上次登录日期；登录过的账号按最近成功登录时间排在前面；整点更新只改已加载行，不会把长列表拉回顶部 |
| 登录时写入 Steam 设置 | 已解析 SteamID64 的账号会写入：首页改为库、关闭推销广告弹窗、好友窗口不自动弹出（**保持在线状态**） |
| 批量查询封禁 | 用你自己的 Steam Web API Key 查 VAC / 游戏封禁 / 社区封禁 / 库存封禁，并给出 PUBG 关联判断；Key 标题旁附有可点击的官方申请地址和简短申请步骤；界面实时显示已完成数量与百分比进度 |
| 列表操作 | 最左列勾选多选、全选/清空、**双击账号 = 直接登录**、**右键 = 登录/编辑/删除**、仅显示有风险 / 仅显示无风险；进入筛选时会清空勾选，避免对隐藏行误删/误导出，关闭筛选不重复清空 |
| 导出 | 账号导出（CSV / JSON / TXT，可选范围）、查询结果导出；筛选状态下可明确选择仅导出当前筛选结果 |
| 云更新提醒 | 启动时后台检查 GitHub Latest Release；发现新版本会显示下载按钮并打开 Release 页面，异常响应会自动恢复更新按钮；**不会静默下载或替换 EXE** |
| 跨版本数据继承 | 从 v3.1.3 起，账号库与保存的 Key 统一保存在当前 Windows 用户的 LocalAppData。**下载新版直接打开就能用**：程序会依次查看 EXE 所在目录、下载/桌面/文档（含一级子目录）、整个用户目录、以及所有固定磁盘的前两层，把找到的旧账号库按「账号多、密码全、更新得晚」排序，自动选最完整的一份继承，并先备份当前库；旧数据在另一台机器或外接盘时仍可点「迁移旧版数据」 |
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

覆盖：账号库质量排序与自动继承判定（空库/缺密码才自动换、账号更少的旧库不许顶替）、CSV/JSON/TXT 三种导入（含无表头账号+密码 CSV、同账号占位合并）、密码 DPAPI 加解密回读、明文不入库、CSV/TXT/JSON 导出→再导入往返（含密码含分隔符）、JSON 真实进度、筛选清空选择、重复登录拦截、登录计时与排序、版本比较、封禁结果写入与库存封禁筛选、分页、API Key 加解密、未解析 SteamID64 的配置写入保护。返回 0 即通过。

## 需要你自己准备

- **Steam Web API Key**：<https://steamcommunity.com/dev/apikey>（程序的 Key 标题旁也可直接点击打开）。登录 Steam 后填写网站域名（个人使用可填 `localhost`）并注册，复制生成的 Key 粘贴进程序后保存；程序会 DPAPI 加密保存在本机。
- 本机已安装 Steam 客户端（自动登录/写设置需要）

## 数据与隐私

- 账号密码：DPAPI 加密后存在 `%LOCALAPPDATA%\PUBG-Auto-Login\steam_ban_accounts.sqlite3`，**换 Windows 用户/换机器解不开**
- API Key：DPAPI 加密存在 `%LOCALAPPDATA%\PUBG-Auto-Login\steam_ban_settings.json`
- v3.1.2 及更早版本把上述文件放在 EXE 旁边。v3.1.5 开始时会在**后台**按固定文件名 `steam_ban_accounts.sqlite3` 查找旧库：先看 EXE 目录和下载/桌面/文档（含一级子目录），再遍历当前用户目录，最后查看所有固定磁盘的前两层；候选文件必须包含本软件的 `accounts` 表，然后按「账号数 → 已保存密码数 → 修改时间」挑最完整的一份自动继承。**当前库已经存有密码时不会被自动替换**；只有库是空的、或一条密码都没有时才会自动换，并且替换前会把当前库备份成 `steam_ban_accounts.before-auto-migration-时间.sqlite3`。查找只读目录名和固定文件名，不打开其它文件、不上传任何内容、不阻塞界面；同一个版本只查一次。旧数据在**另一个 Windows 用户、外接盘或网络盘**时无法自动读取，可点「迁移旧版数据」手动选择。
- 本仓库**不包含任何账号数据**：`.gitignore` 已排除 `*.sqlite3` / `steam_ban_settings.json` / 账号清单文件
- 导出账号时程序会提醒：导出文件是**明文密码**，别外发

## 免责声明

本项目仅供学习与个人账号管理使用。使用者需自行承担因批量登录、账号共享等行为导致的账号封禁、限流等风险，作者不对任何后果负责。
