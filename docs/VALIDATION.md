# 公开快照验证

验证日期：2026-10-31；Windows，Python 3.12，本机既有虚拟环境。

- `python -m pytest -q --disable-warnings`：1395 passed，4 skipped，257 warnings，142.08秒。跳过包括未分发的官方SDK、未公开的历史QA脚本及已有条件跳过；不算通过。
- `python -m app.main --version`：order-bot 0.1.0。
- `python -m compileall -q app scripts`：通过。
- 暂存内容逐文件检查：没有本机已知凭据、私钥块或被禁止的运行文件。

仅为公开源码与合成样本验证。没有在新账号调用企业微信、DeepSeek或飞书；没有新环境真实订单验收、SDK打包或干净Windows安装验收。未改变原环境通知暂停状态。公开副本调整了脱敏样本与外部依赖跳过条件；原项目客户回归证据仍保留私密，不将本结果折算为原项目98%准确率。
