# GPU 切换助手（GpuSwitcher）

[简体中文](README.md) | [English](README_EN.md)

**让独显真正「关掉」，而不只是「切过去」。**

想给笔记本省电，Windows 自带的「图形性能首选项」只能一个程序一个程序地勾，
改完还得**逐个重启那些程序**才生效；想彻底断电省电，只能进设备管理器手动
禁用独显——步骤多，而且很容易把自己锁在黑屏里。

GpuSwitcher 把这两件事都收进一个 exe：

| 痛点 | 怎么做 |
| --- | --- |
| 不知道哪些程序在吃独显 | 实时列出每个程序的显卡占用，🔴 标出正在用独显的，附显存与利用率 |
| 程序太多逐个改太慢 | **多选批量**改集显/独显，一键**重启程序**让新分配立刻生效 |
| 想彻底省电 | 整机切集显，**直接禁用独显设备**，温度与风扇噪音立刻下来 |
| 多选的程序里有好几个占卡的 | 「**选中全部占用独显的**」+「⚡ 一键释放独显」 |
| 怕操作翻车黑屏 | 每次操作前自动建**还原点**，并生成**恢复脚本**，黑屏时双击救回 |

- 单文件 exe，绿色免安装，双击即用
- 开源协议：**MIT**
- 仅依赖 Windows 自带能力与 `nvidia-smi`，无需联网
- 完整支持高 DPI（150% / 200% 缩放）

| 切换 | 应用分配 |
| --- | --- |
| ![切换](docs/screenshot-switch.png) | ![应用分配](docs/screenshot-apps.png) |

| 监控 | 更多工具 |
| --- | --- |
| ![监控](docs/screenshot-monitor.png) | ![更多工具](docs/screenshot-tools.png) |

---

## 同系列工具

| 项目 | 说明 |
| --- | --- |
| [极速连点器 AutoClicker](https://github.com/xavier111222/AutoClicker) | 连点 + 流程宏 + 跨应用后台点击 |
| [屏幕录制器 ScreenRecorder](https://github.com/xavier111222/ScreenRecorder) | 全屏 / 框选 / 应用窗口 / 自动识别四种录制区域 |

三个项目共用同一套高 DPI UI 骨架（`ui_kit.py`），界面风格一致。

---

## 下载

👉 **[Releases 页面下载](https://github.com/xavier111222/GpuSwitcher/releases/latest)**
→ 资产 `GpuSwitcher-v1.1.0.exe`（单文件绿色版，约 11 MB，下载后双击即用）

同一页面附 `SHA256SUMS.txt`，校验：

```bat
certutil -hashfile GpuSwitcher-v1.1.0.exe SHA256
```

> GitHub Release 的资产名不支持中文，所以发布文件名用了英文；
> 复制到桌面后会自动叫 `GPU切换助手.exe`（源码内置名即为此）。

## 快速开始

1. 双击 **`GPU切换助手.exe`**
2. 首次使用点右上角 **「获取管理员权限」**（切换显卡必须管理员权限）
3. 点 **「🍃 节能（集显）」** → 独显被禁用，画面由集显输出，功耗/发热/风扇噪音立刻下降
4. 需要打游戏 / 渲染 / 跑 AI 时点 **「⚡ 高性能（独显）」** 切回来

> 切换后建议 **注销或重启一次**，才能彻底释放被进程占用的独显。

---

## 功能一览

### 🍃 整机切换（「切换」页）

| 功能 | 说明 |
| --- | --- |
| 节能模式 | 禁用所有 NVIDIA 显示适配器，改用集显 |
| 高性能模式 | 重新启用独显 |
| 单个设备启用/禁用 | 右键显卡列表可单独操作 |
| 重启 / 注销 / 睡眠 / 关机 | 注销是"温和版"释放，不用重启 |
| 操作前创建还原点 | iOS 风格开关，一键开启 |

### 🎯 应用 GPU 分配（「应用分配」页）

**这就是 Windows 设置里的「图形性能首选项」，但直接搬进了工具里：**

- 列出所有**正在运行的程序**（原生 API 枚举，毫秒级）
- **一眼看出谁在吃独显**：新增「当前显卡」列，正在使用独显的程序标为 🔴 独显，
  整行红色高亮，并显示显存占用与利用率（与任务管理器同源，走 Windows GPU Engine
  性能计数器）；列表按独显占用从大到小排序
- 顶部汇总条：**共 N 个程序 · 🔴 M 个正在使用独显 · 显存合计 X MB**
- 「只看独显上的」开关：一键过滤掉不占独显的程序
- **支持多选批量操作**：Ctrl / Shift 点选，`Ctrl+A` 全选，或
  **「选中全部占用独显的」** 一键选中所有正在用独显的程序

| 批量按钮 | 作用 |
| --- | --- |
| 🍃 批量改用集显（省电） | 一次性把所选程序全部写给"节能（集显）"，并询问是否立即重启生效 |
| ⚡ 批量改用独显 | 一次性把所选程序全部写给"高性能（独显）" |
| 恢复默认 | 清除所选程序的规则，交回 Windows 决定 |
| 🔄 重启程序（生效） | 结束并重新打开所选程序——**这才是让新分配真正生效的动作** |
| ⚡ 一键释放独显 | 把**所有正在用独显**的程序全部设为集显并立即重启，独显立刻空出来 |
| 结束进程 | 批量 `taskkill` 所选程序 |

- 支持**搜索**（即时过滤，不重新扫描）、**手动添加程序**（选择 exe）、
  **删除规则**、**查看已保存规则**
- 系统关键进程（`explorer.exe` / `dwm.exe` / `svchost.exe` …）会被**自动跳过**，
  不会误重启导致桌面崩溃

原理：写入 Windows 原生的
`HKCU\Software\Microsoft\DirectX\UserGpuPreferences`
（`GpuPreference=1;` 集显 / `GpuPreference=2;` 独显），
与系统设置页完全等效。**设置后需重启该程序才生效。**

### 📊 其他

| 功能 | 说明 |
| --- | --- |
| 实时监控 | 温度、利用率、显存、功耗，2 秒刷新 |
| 功耗上限 | 调用 `nvidia-smi -pl` 限制 TGP（笔记本可能不支持） |
| 开机自动节能 | 计划任务，登录时自动禁用独显 |
| 系统还原点 | 操作前一键创建，出问题可回滚 |
| 恢复脚本 | 生成 `恢复显卡（双击运行）.bat`，黑屏时双击救回 |
| 清理着色器缓存 | NVIDIA DXCache / D3DSCache / GLCache |
| 生成诊断报告 | 显卡 + `nvidia-smi -q` + GPU 规则 + 系统信息 |

---

## 命令行模式

```bat
GPU切换助手.exe --list                  :: 列出所有显示适配器
GPU切换助手.exe --json                  :: JSON 格式输出
GPU切换助手.exe --apps                  :: 列出运行中的程序与独显占用
GPU切换助手.exe --dpiinfo               :: DPI 感知状态（诊断）
GPU切换助手.exe --apply power           :: 切到节能模式（需管理员）
GPU切换助手.exe --apply performance     :: 切到高性能模式
GPU切换助手.exe --apply power --restart :: 切换后 5 秒重启
```

退出码：`0` 成功 / `1` 部分失败 / `2` 权限不足 / `3` 未检测到 NVIDIA 独显。

---

## 它是怎么工作的？

不做任何驱动层 hack，只调用 Windows 官方接口：

1. **设备枚举**：直接读注册表
   `HKLM\SYSTEM\CurrentControlSet\Enum`，筛出 `Driver` 指向显示类
   `{4d36e968-e325-11ce-bfc1-08002be10318}` 的设备——比 PowerShell 的
   `Get-PnpDevice` 快约 **3000 倍**（5ms vs 15s）
2. **启用/禁用**：`pnputil /enable-device` `/disable-device`（原生、快），
   失败时回退 PowerShell 的 `Enable-PnpDevice` / `Disable-PnpDevice`
3. **应用 GPU 分配**：写注册表 `UserGpuPreferences`
4. **进程 GPU 占用检测**：DXGI 枚举适配器 LUID + 读取 Windows「GPU Engine」
   性能计数器（任务管理器同一数据源，原生 PDH API，毫秒级），按实例名中的
   pid / luid 精确归属到进程，再映射到显卡；`nvidia-smi --query-compute-apps`
   作为补充来源
5. **监控**：`nvidia-smi`

---

## ⚠ 注意事项

1. **必须管理员权限**才能启用/禁用设备，程序会引导你提权。
2. **独显直连（MUX）机型**：禁用独显可能黑屏。程序检测不到集显时会警告；
   出问题时重启进安全模式，或双击生成的 `恢复显卡（双击运行）.bat`。
3. 禁用独显期间 `nvidia-smi` 不可用，监控面板显示"独显不可用"属正常。
4. 联想/华硕/戴尔等厂商管家可能自动重新启用独显。
5. 首次运行被 SmartScreen 拦截时，点"更多信息 → 仍要运行"（未签名开源程序常见）。
6. 完整支持高 DPI（150% / 200% 缩放）：程序声明 Per-Monitor V2 感知，
   所有字体与布局按 DPI 实时缩放，高分屏下清晰不模糊。

---

## 从源码构建

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python build.py     :: 生成图标 → PyInstaller → 复制到桌面
```

直接运行源码（需要带 tkinter 的 Python）：

```bat
python gpu_switcher.py
python gpu_switcher.py --list
```

### 项目结构

```
GpuSwitcher/
├── gpu_switcher.py     # 全部程序代码（单文件：GUI + CLI + 后端）
├── make_icon.py        # 生成 assets/icon.ico
├── build.py            # 一键打包并复制到桌面
├── assets/icon.ico
├── docs/               # README 截图
├── requirements.txt    # 仅构建期依赖
├── LICENSE             # MIT
└── README.md
```

---

## 常见问题

**Q：点了节能模式没反应 / 提示失败？**
A：权限不足（标题栏应显示"● 管理员"），或有进程占用独显。
先去「应用分配」页结束占用进程，或勾选"切换后自动重启"。

**Q：怎么让某个程序用集显、其他程序用独显？**
A：这就是「应用分配」页做的事——不要禁用独显，只给特定程序指定"节能（集显）"。

**Q：切换后屏幕闪一下正常吗？**
A：正常，Windows 重建显示拓扑时会闪屏，1-2 秒恢复。

**Q：功耗上限设置失败？**
A：多数笔记本 GPU 功耗由 EC/固件锁定，`nvidia-smi` 无权修改，属正常限制。

---

## 许可证

MIT，见 [LICENSE](LICENSE)。使用风险自负，作者不对硬件或数据损失负责。
