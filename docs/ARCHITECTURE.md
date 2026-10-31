# 架构与代码导航

Python 3.12 / FastAPI / SQLAlchemy / APScheduler / pywebview。SQLite保存消息、订单、批次、发送事件与运行状态；文件保存规则、提示版本和检查点相关资料。

| 模块 | 职责 |
|---|---|
| app/main.py | 本地启动、拉取线程、健康检查、窗口及正常退出 |
| app/wecom/ | ctypes SDK、增量拉取与解密 |
| app/pipeline/ | 分段、模型整理、校验、规则、来源保护、改单及批次 |
| app/feishu/ | API客户端、电子表格写出、汇总通知 |
| app/scheduler.py / send_events.py | 定时身份、持久队列、重试与总开关 |
| app/net/ | 网络就绪、IP与拉取状态、告警 |
| app/web/ | 本地UI、管理路由、诊断入口 |
| app/diagnostician/ | 对话、工具注册、诊断预算、金标、规则/代码版本及受限执行 |
| app/maintenance.py | 协作维护协议；不允许强杀替代安全退出 |
| scripts/resident_* | 观察、维护准备、正常退出/单次启动、独立版本核验 |
| packaging/ | 安装包、依赖清单、启动器与升级回退材料 |

数据流：会话存档 → 本地Message → 模型/规则 → 来源审计 → Order与Batch检查点 → 飞书表 → 批次汇总。游标提交只代表拉取成功，不能当成订单处理或交付完成。

修改后须验证受影响模块；维护失败应保留停点，不强杀/reload或反复抢租约。诊断执行组件不能访问宿主密钥，真实API调用只允许显式配置的测试目标。
