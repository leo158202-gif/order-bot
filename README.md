# Order Bot

面向 Windows 的企业微信订单整理原型：会话存档 → DeepSeek 整理 → 规则校验 → 飞书电子表格 → 群汇总。提供本地管理界面、诊断助手、失败检查点与受控维护工具。

**状态：测试阶段，尚未完成生产验收。** 本仓库是 2026-10-31 的脱敏交接快照，不包含原仓库历史、客户数据或任何可用账号凭据。公开副本的测试结果见 `docs/VALIDATION.md`；原环境历史的通过数量不能代替本副本验证。

## 开发入口

Windows / Python 3.12。安装依赖后先运行离线测试：

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m app.main --version
```

启动真实服务需要自行申请企业微信会话存档、飞书应用、DeepSeek 账号，并获取官方 Windows SDK。不要直接用示例配置启动定时发送。详见 [部署与配置](docs/DEPLOYMENT.md)。

## 交接阅读顺序

1. [交接状态与剩余工作](docs/HANDOFF.md)
2. [需求与规则](docs/REQUIREMENTS.md)
3. [代码架构](docs/ARCHITECTURE.md)
4. [部署与配置](docs/DEPLOYMENT.md)
5. [安全与发布边界](SECURITY.md)
6. [验证结果](docs/VALIDATION.md)

`app/` 为主程序，`rules/` 为规则模板，`tests/` 为回归测试，`scripts/` 为开发与维护工具，`packaging/` 为安装器构建材料，`agent-tools/` 和 `worker-releases/` 为受限诊断执行组件。

旧脚本中的日期、订单编号和固定测试标识保留用于回归定位；账号标识及收件信息已替换。历史修复脚本不能直接对新用户数据库运行。

原创代码采用 MIT；第三方依赖与企业微信 SDK 分别遵守各自条款。SDK 二进制不随仓库发布。
