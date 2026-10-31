"""Versioned local instructions remain available with no working model/network."""


GUIDES = [
    {"id": "recharge", "title": "DeepSeek余额与充值", "steps": [
        "先检查实际余额或HTTP错误；超时不能直接判为欠费。",
        "由账号所有人打开DeepSeek官方平台，登录与软件API Key对应的账号。",
        "在余额/充值页面完成支付；不要在聊天中发送密钥、密码或支付信息。",
        "完成后输入“已充值，检查一次”，本次请求只检查一次。"],
     "action": {"label": "打开DeepSeek官方平台", "url": "https://platform.deepseek.com/"}},
    {"id": "trusted_ip", "title": "企业微信可信IP", "steps": [
        "只有企微301042/10009白名单拒绝证据才走可信IP处理；公网IP变化不是过期证据。",
        "到管理后台的IP白名单页查看并复制本次实际出口IP；没有查到时不要猜。",
        "由账号管理员进入企业微信后台：管理工具 → 会话内容存档 → 可信IP，添加并保存。",
        "回到IP白名单页点“我已更新可信IP”，只试拉一次；网络不通先恢复本机网络或原有代理。"],
     "action": {"label": "打开IP白名单页", "url": "/admin/ip"}},
    {"id": "usage", "title": "软件基本操作", "steps": [
        "临时发单：主页点击“现在输出临时订单”，每次点击是独立发送要求。",
        "定时：在管理后台设置时刻；忙时会持久排队。",
        "每月一本表、每日一个日期sheet；无订单不会另建空月表。",
        "四个特殊群只收原文写“本地”的订单；其它启用群照常。",
        "指出错误时提供表、日期sheet、物理行列及正确写法；冲突需确认以哪个为准。",
        "同一待核项及同一有效证据最多三轮，缺证会保留备注并停止重复核查。"],
     "action": {"label": "打开运行状态", "url": "/admin/status"}},
]


def classify_failure(*, http_status=None, wecom_code=None, network_error=None):
    if wecom_code in {301042, 10009}:
        return {"category": "trusted_ip", "message": "企业微信返回可信IP白名单拒绝，请按本机指引更新后检查一次。", "guide_id": "trusted_ip"}
    if http_status == 402:
        return {"category": "balance", "message": "模型接口返回HTTP402，请由账号所有人核查余额并充值。", "guide_id": "recharge"}
    if http_status in {401, 403}:
        return {"category": "authentication", "message": "接口鉴权或权限被拒绝，请核对当前账号配置和权限。", "guide_id": None}
    if http_status == 429:
        return {"category": "rate_limit", "message": "接口限流，本次已按预算停止或有限重试。", "guide_id": None}
    if network_error:
        return {"category": "network", "message": "网络或代理连接未完成，不能据此判断欠费或IP白名单状态。", "guide_id": None}
    return {"category": "service", "message": "接口未完成请求，请查看实际状态和错误证据。", "guide_id": None}


def local_answer(message):
    selected = [GUIDES[0]] if "充值" in message or "余额" in message else [GUIDES[1]] if "IP" in message.upper() else [GUIDES[2]]
    return "模型当前不可用，以下是本机说明：\n" + "\n".join(
        guide["title"] + "：\n" + "\n".join(guide["steps"]) for guide in selected)
