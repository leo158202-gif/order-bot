"""查询本机公网 IP：同时查 3 个服务，取多数一致的结果。

用法：.venv\\Scripts\\python.exe scripts\\ip_probe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.net.public_ip import get_public_ip


def main() -> None:
    result = get_public_ip()
    print("各服务返回：")
    for url, ip in result["results"].items():
        print(f"  {url} -> {ip or '(失败)'}")
    print()
    if not result["network_ok"]:
        print("结论：网络断开（DNS 全部解析失败），请检查网络连接")
        sys.exit(2)
    if result["ip"]:
        print(f"当前公网 IP：{result['ip']}")
        if result["error"]:
            print(f"注意：{result['error']}")
        print()
        print("请和浏览器打开 https://www.ip138.com 显示的 IP 对照，应一致。")
    else:
        print("结论：所有查询服务都失败了（但网络正常），请稍后再试或在后台更换服务地址")
        sys.exit(1)


if __name__ == "__main__":
    main()
