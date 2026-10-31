# 部署、测试与配置

优先在 Windows / Python 3.12 的新目录创建环境，安装 requirements.txt。先运行 pytest，随后 --version。不要直接复制原客户data目录。

## 账号与SDK

自行配置企业微信会话存档权限、合法企业出口IP、RSA密钥及飞书自建应用。SDK从企业微信官方取得，将Windows DLL及依赖按 app/wecom/sdk.py 的路径放在 vendor/wecom_sdk/bin。仓库不提供或转授这些二进制。

DeepSeek API key只保存在接手方本地配置；不要在聊天、Issue、PR、截图或日志中提交。模板为 config.example.yaml，复制到自己的运行目录后填入自己的凭据。模板没有可用凭据，不能直接运行成功。

## 测试隔离

app/test_runtime.py包含开发期固定测试表边界。公开副本使用占位符，必须在独立分支配置你自己的测试目标和manifest，并保持检查一致；不得删掉安全检查后直连生产库。scripts/prepare_test_runtime.py需先读实现与 --help，仅针对自己的环境建立隔离副本。历史repair/replay/resume脚本依赖旧测试证据，不能盲跑。

原工作环境的飞书告警与定时均已暂停。新部署也应先关闭alerts.enabled，并用本地总开关暂停定时；只有获明确授权、测试目标确认且验证完成后启用。

## 打包

详细构建入口见 packaging/BUILD.md；构建需自行准备可信Python运行时、微软运行库/WebView2、Inno Setup及SDK。构建通过不能替代干净Windows实际安装/升级/回退验收。保留客户data，禁止运行中强杀或覆盖；程序回退和数据恢复分开处理。

## 故障

301042/10009须用实际接口错误确认可信IP拒绝；若后台称第三方IP，重复填写无法解除，需管理员核实企业实际出口并向平台核查。重启软件不能修正平台许可。恢复后须检查自然拉取、游标与真实交付，不能仅凭工具成功宣布恢复。
