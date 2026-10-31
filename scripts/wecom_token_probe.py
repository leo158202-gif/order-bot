"""用 corpid + 会话存档 secret 调 gettoken，验证可信 IP 是否生效。

用法：.venv\\Scripts\\python.exe scripts\\wecom_token_probe.py
返回 token -> 通过；返回 60020 -> 可信 IP 没填对或未生效。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import yaml

ERRCODE_HINTS = {
    60020: "IP 不在企业微信可信 IP 名单里（或刚保存还没生效，等 1~2 分钟再试）",
    40001: "secret 错误，请核对 data/config.yaml 里的 wecom.archive_secret",
    40013: "corpid 错误，请核对 data/config.yaml 里的 wecom.corpid",
}


def main() -> None:
    cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
    corpid = cfg["wecom"]["corpid"]
    secret = cfg["wecom"]["archive_secret"]
    if not corpid or not secret:
        print("请先在 data/config.yaml 填 wecom.corpid 和 wecom.archive_secret")
        sys.exit(1)

    resp = httpx.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": corpid, "corpsecret": secret},
        timeout=10,
    )
    data = resp.json()
    # 不打印 token 本体，只打印结论
    errcode = data.get("errcode", -1)
    if errcode == 0:
        token = data["access_token"]
        print(f"成功！拿到 access_token（末4位 …{token[-4:]}，有效期 {data.get('expires_in')} 秒）")
        print("可信 IP 已生效。")
    else:
        hint = ERRCODE_HINTS.get(errcode, "见企业微信官方文档错误码表")
        safe = {k: v for k, v in data.items() if k != "access_token"}
        print(f"失败：errcode={errcode} {data.get('errmsg')}")
        print(f"提示：{hint}")
        print(f"原始返回：{json.dumps(safe, ensure_ascii=False)}")
        sys.exit(2)


if __name__ == "__main__":
    main()
