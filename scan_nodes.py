#!/usr/bin/env python3
"""全量节点扫描：逐个切换+实测出口，找出当前可用节点"""
import urllib.request, json, time, ssl, sys
from urllib.parse import quote

MIHOMO_API = "http://127.0.0.1:9090"
PROXY = "http://127.0.0.1:7890"
GROUP = "交易专用"
FULL_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BLOCKED = {"CN","US","HK","KP","IR","CU","SY","RU","MM","VE","CF"}


def get_opener():
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    ph = urllib.request.ProxyHandler({"http":PROXY,"https":PROXY})
    hh = urllib.request.HTTPSHandler(context=ctx)
    return urllib.request.build_opener(ph, hh)


def check_exit(opener):
    apis = [
        ("https://ipwho.is/", lambda d: (d.get("country_code",""), d.get("ip",""))),
        ("https://ipapi.co/json/", lambda d: (d.get("country_code",""), d.get("ip",""))),
        ("https://ipinfo.io/json", lambda d: (d.get("country",""), d.get("ip",""))),
        ("http://ip-api.com/json/", lambda d: (d.get("countryCode",""), d.get("query",""))),
    ]
    for url, parse in apis:
        try:
            req = urllib.request.Request(url, headers={"User-Agent":FULL_UA})
            r = opener.open(req, timeout=5)
            d = json.loads(r.read())
            cc, ip = parse(d)
            if cc and ip:
                return cc, ip
        except Exception:
            continue
    return None, None


def probe_clob(opener):
    try:
        req = urllib.request.Request("https://clob.polymarket.com/", headers={"User-Agent":FULL_UA})
        r = opener.open(req, timeout=12)
        return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTPError {e.code}"
    except Exception as e:
        return False, repr(e)[:60]


def get_nodes():
    try:
        d = json.loads(urllib.request.urlopen(MIHOMO_API + "/proxies", timeout=5).read())
        proxies = d.get("proxies", {})
        groups = {"Selector","URLTest","Fallback","Direct","Reject","Compatible","Pass","RejectDrop"}
        return [k for k, v in proxies.items() if v.get("type") not in groups]
    except Exception:
        return []


def main():
    nodes = get_nodes()
    print(f"节点池: {len(nodes)} 个，逐个扫描中...")
    opener = get_opener()
    good = []
    for i, node in enumerate(nodes):
        req = urllib.request.Request(MIHOMO_API + "/proxies/" + quote(GROUP, safe=""), method="PUT",
                                     data=json.dumps({"name": node}).encode())
        req.add_header("Content-Type", "application/json")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            if r.status != 204:
                continue
        except Exception:
            continue
        time.sleep(1.2)
        cc, ip = check_exit(opener)
        if not cc:
            print(f"[{i+1:3d}] {node[:40]:42s} 出口查询失败")
            continue
        okc, detc = probe_clob(opener)
        tag = "OK" if (cc not in BLOCKED and okc) else "FAIL"
        print(f"[{i+1:3d}] {node[:40]:42s} {cc:2s} {ip:16s} clob={detc} {tag}")
        if cc not in BLOCKED and okc:
            good.append((node, cc, ip))
    print()
    print(f"=== 可用节点 {len(good)} 个 ===")
    for node, cc, ip in good:
        print(f"  {cc} {ip}  {node}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
