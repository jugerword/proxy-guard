#!/usr/bin/env python3
"""
switch_node.py - 手动切换节点工具（可复用版）

遍历节点池，优先切换 Polymarket 允许区域（JP/SG/DE...），每个节点切换后实测 clob。
用法：
  python3 switch_node.py            # 自动遍历切换（默认出口组）
  python3 switch_node.py "节点名"   # 手动指定节点切换

配置（环境变量）：
  GUARD_GROUP   出口组名（默认 交易专用；与 guard.py 保持一致）
  MIHOMO_API    mihomo API（默认 http://127.0.0.1:9090）
"""
import urllib.request, json, time, ssl, sys, os
from urllib.parse import quote

MIHOMO_API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090")
PROXY = os.environ.get("PROXY", "http://127.0.0.1:7890")
GROUP_NAME = os.environ.get("GUARD_GROUP", "交易专用")

# Polymarket 允许区域优先级（日本最优，其次新加坡/德国等非 US/CN 区域）
PRIORITY = ["JP", "SG", "DE", "GB", "FR", "NL", "KR", "TW", "LT", "LV"]
FULL_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def api(method, path, body=None):
    req = urllib.request.Request(MIHOMO_API + path, method=method)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body else None
    try:
        r = urllib.request.urlopen(req, data=data, timeout=5)
        return r.status, r.read().decode()
    except Exception as e:
        return None, str(e)


def get_proxies():
    try:
        d = json.loads(urllib.request.urlopen(MIHOMO_API + "/proxies", timeout=5).read())
        proxies = d.get("proxies", {})
        groups = {"Selector", "URLTest", "Fallback", "Direct", "Reject", "Compatible", "Pass", "RejectDrop"}
        nodes = [k for k, v in proxies.items() if v.get("type") not in groups]
        return nodes
    except Exception:
        return []


def test_clob():
    """通过代理测试 clob 是否可达"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    proxy_handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    https_handler = urllib.request.HTTPSHandler(context=ctx)
    opener = urllib.request.build_opener(proxy_handler, https_handler)
    try:
        req = urllib.request.Request("https://clob.polymarket.com/", headers={"User-Agent": FULL_UA})
        r = opener.open(req, timeout=12)
        return True
    except urllib.error.HTTPError as e:
        # 403 说明网络通但被风控；其他 HTTP 错误说明可达
        if e.code in (200, 301, 302, 307, 403, 404, 429):
            return True
        return False
    except Exception:
        return False


def get_current():
    try:
        d = json.loads(urllib.request.urlopen(MIHOMO_API + "/proxies", timeout=5).read())
        g = d.get("proxies", {}).get(GROUP_NAME, {})
        return g.get("now", "")
    except Exception:
        return ""


def main():
    if len(sys.argv) > 1:
        # 手动指定节点
        node = sys.argv[1]
        s, resp = api("PUT", "/proxies/" + quote(GROUP_NAME, safe=""), {"name": node})
        if s == 204:
            print(f"已切换至: {node}")
            time.sleep(3)
            ok = test_clob()
            print("clob 测试:", "OK" if ok else "FAIL")
            return 0 if ok else 1
        print(f"切换失败: {resp[:100]}")
        return 1

    nodes = get_proxies()
    if not nodes:
        print("无法获取节点列表（mihomo API 未就绪？）")
        return 1
    print(f"出口组: {GROUP_NAME} | 当前节点: {get_current()}")
    print(f"节点池: {len(nodes)} 个，逐个扫描中...")

    def node_priority(name):
        n = name.upper()
        for i, cc in enumerate(PRIORITY):
            if cc in n:
                return i
        return 99

    ranked = sorted(nodes, key=node_priority)

    for i, node in enumerate(ranked[:15]):
        s, resp = api("PUT", "/proxies/" + quote(GROUP_NAME, safe=""), {"name": node})
        if s != 204:
            continue
        time.sleep(3)
        ok = test_clob()
        status = "OK" if ok else "FAIL"
        print(f"  [{i+1}] {status} {node[:45]}")
        if ok:
            print(f"成功切换至: {node}")
            return 0

    print("前15个节点均不可用，请用 python3 scan_nodes.py 全量扫描")
    return 1


if __name__ == "__main__":
    sys.exit(main())
