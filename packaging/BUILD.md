# OrderBot Windows 安装构建

运行源码入口 `app/launcher.py`，稳定桌面入口 `OrderBot.exe`，程序版本在 `releases/<version>`；客户数据只在安装根目录 `data/`，安装包从不复制开发 data。向导首次保存默认 target=test、schedule_master_on=0；升级/卸载保留已有数据。缺配置时先启动独立首次设置页，不启动主业务线程。

开发端需要可信 Python 3.12 x64、已锁定 site-packages、.NET Framework csc、官方 Inno Setup。客户无需安装 Python/pip/Git/Node/Codex 或独立 MCP。WebView2使用微软签名bootstrap，缺失时需要联网下载安装；VC运行库也验微软签名，缺失则安装并复核。组件安装失败不切当前版本，保留客户data。账号授权/充值/可信IP仍由账号所有人完成。

1. 首次固定运行依赖：开发 Python 运行 `scripts/verify_release.py --lock packaging/runtime-lock.json --site-packages <可信site-packages>`，审阅锁文件再用于发布。后续构建仅消费该锁，任何包文件变化拒绝。
2. 编译 `packaging/OrderBotLauncher.cs`：系统 csc `/target:winexe /optimize+ /reference:System.Windows.Forms.dll /out:<OrderBot.exe> <源码绝对路径>`。构建受限启动器用 `agent-tools/launcher/build.ps1 -OutputPath <AppContainerLauncher.exe>`；将这两份固定二进制输入传给构建脚本。
3. 从官方来源准备 Inno Setup、微软 WebView2/VC 安装材料。实际构建入口：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_release.ps1 -Version <版本> -OutputPath <新输出目录> -RuntimeRoot <可信完整Python根> -SitePackages <site-packages> -Launcher <OrderBot.exe> -SandboxLauncher <AppContainerLauncher.exe> -Prerequisites <微软签名安装材料目录> -InnoCompiler <ISCC.exe>`。BuildPython可显式传入；默认开发venv，仅构建端使用。
4. 脚本按清单复制、hash核包、生成standalone主runtime和仅stdlib的worker-runtime、运行 `-I -B`真实导入/SDK加载，再复核清单，最后编译安装器及sha256。输出目录已存在即拒绝，避免删错目录。用 `verify_release.py --verify <stage>`可独立复核，不需要读取开发data。
5. R0在稳定集成与全量测试后重新构建最终版本；开发中产出的exe是候选，不能替代干净Windows安装、账号配置、升级与回退验收。

无账号首启验收：使用stage自身`runtime/python.exe -I -B packaging/probe_customer_runtime.py <stage>`；它将release代码加入路径，在全新TEMP客户目录用真实store/registry/versions调用start_worker_runtime，验证baseline安装与后台隔离启动，不启动主程序、不读取现有data、不调用企微/飞书/DeepSeek账号接口。

Sandbox必须显式传 `runtime_root=<install>/worker-runtime`、`launcher_path=<install>/agent-tools/launcher/AppContainerLauncher.exe`，客户默认不允许现场csc/复制全局Python。应用启动设 `ORDER_BOT_DATA_DIR=<install>/data`、cwd安装根；SDK跟随release/vendor路径，rule_loader应使用源码相对模板fallback。

当前指针由安装器成功完成文件与前置组件后通过MoveFileEx原子替换，并先保留previous.json；不执行运行中强杀。程序回退须验证上一版release后切指针，不删除或还原客户data。旧发布版本保留以供维护者回退，未实现自动盲回退或后台自动升级。

安装/卸载运行保护按目标目录的`launcher.running`独占文件句柄判断，桌面loader在整个子进程生命周期持有；其它目录有运行实例不阻断新目录安装。业务main的既有全局单实例保护仍由main维持。`/DValidationBuild=<唯一ID>`仅用于隔离验收：使用专用卸载AppId且不创建桌面/菜单快捷方式，避免覆盖真实安装注册；正式构建不传此定义。WebView2存在性按微软文档的F3017226-FE2A-4295-8BDF-00C3A9A7E4C5注册键及非空非0版本判断。

官方来源：Python https://www.python.org/downloads/windows/；Inno https://jrsoftware.org/isdl.php（本次开发编译6.4.3，官方旧版不可变release）；WebView2 https://developer.microsoft.com/microsoft-edge/webview2/；VC https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist 。运行依赖许可证随各distribution metadata复制，OpenSSL/curl原始许可证在packaging/licenses；SDK来源及使用条款指针写入THIRD-PARTY-NOTICES，不冒充SDK额外授权。
