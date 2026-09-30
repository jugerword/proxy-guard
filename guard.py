#!/usr/bin/env python3
"""
proxy-guard.py - Polymarket 交易链路守护（可复用版 v5）

核心原则：节点名不可信，一切以【实测】为准（gamma-api 真实交易层）。
判定标准（health=ok 必须满足）：
  1. mihomo 进程 + 7890 端口存活
  2. 通过代理实测 gamma-api.polymarket.com 返回 200 + 非空行情数据（真实交易层）
  3. Binance ping 通（行情源）
  4. 出口区域不在 BLOCKED 列表（CN 实测 geoblock / US 官方禁止）

自愈动作（四级递进）：
  第一层: 刷新订阅源 -> 让 mihomo fallback 组自动重选健康节点（内建秒级自愈）
  第二层: 遍历候选节点池逐个切换（known-good 缓存优先），每个实测 gamma
  第三层: 重启 mihomo -> 更新订阅 -> 再试一轮
  兜底:  全部失败 -> 维持现状 + Telegram 告警

配置方式（优先级：环境变量 > 本文件默认值）：
  PROXY_GUARD_DIR      脚本/状态文件目录（默认脚本所在目录）
  MIHOMO_API           mihomo 外部控制器地址（默认 http://127.0.0.1:9090）
  PROXY                本地代理端口（默认 http://127.0.0.1:7890）
  GUARD_GROUP          mihomo 出口组名（默认 交易专用）
  SUDO_PASSWORD        系统用户 sudo 密码（restart mihomo 需要；留空则用无密码 sudo）
  MAX_SWITCH           每轮最多切换测试的节点数（默认 15）

状态输出: <PROXY_GUARD_DIR>/status.json （可被看板 /api/proxy 读取）
"""
import urllib.request, json, time, ssl, subprocess, os, sys
from urllib.parse import quote

# ---------- 可配置项（环境变量优先） ----------
BASE_DIR = os.environ.get("PROXY_GUARD_DIR", os.path.dirname(os.path.abspath(__file__)))
MIHOMO_API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090")
PROXY = os.environ.get("PROXY", "http://127.0.0.1:7890")
GROUP_NAME = os.environ.get("GUARD_GROUP", "交易专用")
SUDO_PASSWORD = os.environ.get("SUDO_PASSWORD", "")

STATUS_FILE = os.path.join(BASE_DIR, "status.json")
LOG_FILE = os.path.join(BASE_DIR, "guard.log")
LIMIT_FILE = os.path.join(BASE_DIR, ".switch_limit")
LAST_STATE_FILE = os.path.join(BASE_DIR, ".last_state")
KNOWN_GOOD_FILE = os.path.join(BASE_DIR, "known_good.json")
NOTIFY_SCRIPT = os.path.join(BASE_DIR, "notify.py")

MAX_SWITCH = int(os.environ.get("MAX_SWITCH", "15"))
SWITCH_LIMIT_SEC = 120  # 限频：120 秒内最多触发一轮修复

FULL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Polymarket 允许区域（交易政策允许的区域；其余视为可用候选）
ALLOWED = {"JP","SG","DE","GB","FR","NL","KR","TW","LT","LV","CA","AU","SE","CH","IT","ES","FI","NO","DK","PL","CZ","EE","IE","BE","AT","LU","PT","GR","RO","BG","HR","SK","SI"}
# BLOCKED：仅保留实测/官方明确禁止的区域（CN 实测 geoblock，US 官方禁止。HK 实测 gamma 200，放行）
BLOCKED = {"CN","US","KP","IR","CU","SY","RU","MM","VE","CF"}

# 节点名称区域优先级（仅影响手动切换排序；fallback 组以健康检查实测为准）
NODE_PRIORITY = ["JP","SG","DE","GB","FR","NL","KR","TW","LT","LV","CA","AU","SE","CH"]


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


def api(method, path, body=None):
    req = urllib.request.Request(MIHOMO_API + path, method=method)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body else None
    try:
        r = urllib.request.urlopen(req, data=data, timeout=5)
        return r.status, r.read().decode()
    except Exception as e:
        return None, str(e)


def get_opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ph = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    hh = urllib.request.HTTPSHandler(context=ctx)
    return urllib.request.build_opener(ph, hh)


def probe(opener, url, timeout=12):
    """返回 (ok, detail)"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": FULL_UA})
        r = opener.open(req, timeout=timeout)
        return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTPError {e.code}"
    except Exception as e:
        return False, repr(e)[:120]


GAMMA_URL = "https://gamma-api.polymarket.com/markets?limit=1"
def probe_gamma(opener):
    """实测 gamma-api（真实交易层）：200 + 非空 JSON 才算通"""
    try:
        req = urllib.request.Request(GAMMA_URL, headers={"User-Agent": FULL_UA})
        r = opener.open(req, timeout=15)
        body = r.read()
        if r.status == 200 and body and body.strip() not in (b"", b"[]", b"null", b"{}"):
            return True, f"HTTP {r.status} (行情数据正常)"
        return False, f"HTTP {r.status} 空响应"
    except urllib.error.HTTPError as e:
        return False, f"HTTPError {e.code}"
    except Exception as e:
        return False, repr(e)[:100]


def check_exit(opener):
    """实测出口 IP 归属地（多 API fallback），返回 (country_code, ip)"""
    apis = [
        ("https://ipwho.is/", lambda d: (d.get("country_code", ""), d.get("ip", ""))),
        ("https://ipapi.co/json/", lambda d: (d.get("country_code", ""), d.get("ip", ""))),
        ("https://ipinfo.io/json", lambda d: (d.get("country", ""), d.get("ip", ""))),
        ("http://ip-api.com/json/", lambda d: (d.get("countryCode", ""), d.get("query", ""))),
    ]
    for url, parse in apis:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": FULL_UA})
            r = opener.open(req, timeout=8)
            d = json.loads(r.read())
            cc, ip = parse(d)
            if cc and ip:
                return cc, ip
        except Exception:
            continue
    return None, None


def check_mihomo():
    """返回 (mihomo_alive, port_alive)"""
    alive = False
    try:
        subprocess.run(["pgrep", "-f", "mihomo"], check=True, capture_output=True)
        alive = True
    except Exception:
        pass
    port = False
    try:
        out = subprocess.run(["ss", "-tln"], capture_output=True, text=True).stdout
        port = ":7890 " in out
    except Exception:
        pass
    return alive, port


def get_nodes():
    try:
        d = json.loads(urllib.request.urlopen(MIHOMO_API + "/proxies", timeout=5).read())
        proxies = d.get("proxies", {})
        groups = {"Selector","URLTest","Fallback","Direct","Reject","Compatible","Pass","RejectDrop"}
        return [k for k, v in proxies.items() if v.get("type") not in groups]
    except Exception:
        return []


def switch_to(node):
    return api("PUT", "/proxies/" + quote(GROUP_NAME, safe=""), {"name": node})


def node_rank(name):
    n = name.upper()
    for i, cc in enumerate(NODE_PRIORITY):
        if cc in n:
            return i
    return 99


def get_auto_current():
    """获取 URLTest 组(自动选择)当前选中的节点，作为第一候选"""
    try:
        d = json.loads(urllib.request.urlopen(MIHOMO_API + "/proxies", timeout=5).read())
        for name, p in d.get("proxies", {}).items():
            if p.get("type") == "URLTest" and p.get("now"):
                return p["now"]
    except Exception:
        pass
    return None


def ranked_nodes():
    nodes = get_nodes()
    auto = get_auto_current()
    # known-good 缓存（最近成功过的）优先
    try:
        kg = json.load(open(KNOWN_GOOD_FILE))
        kg_names = [x["node"] for x in kg]
    except Exception:
        kg_names = []
    # 排序：known-good > URLTest当前 > 区域优先级 > 其余
    ranked = sorted(nodes, key=lambda n: (
        0 if n in kg_names else 1,
        0 if n == auto else 1,
        node_rank(n)
    ))
    return auto, ranked


def full_test():
    """完整链路测试：gamma-api 通 = 交易可用 = health ok。
    区域查询为辅助（IP API 可能限流，gamma 已证明交易层真实可达）。"""
    opener = get_opener()
    okg, detg = probe_gamma(opener)
    if not okg:
        # gamma 不通：尽力查出口，若是 BLOCKED 区域则明确拒绝
        cc, ip = check_exit(opener)
        if cc in BLOCKED:
            return False, f"gamma不通且出口被封锁区域: {cc} ({ip})"
        return False, f"gamma: {detg}"
    # gamma 通 = 交易层可用；区域仅作日志展示
    cc, ip = check_exit(opener)
    if cc in BLOCKED:
        return False, f"gamma通但出口在封锁区域: {cc} ({ip})"
    return True, f"{cc or '?'} ({ip or '?'}) gamma={detg}"


def restart_mihomo():
    log("重启 mihomo...")
    if SUDO_PASSWORD:
        cmd = f"echo {SUDO_PASSWORD} | sudo -S systemctl restart mihomo"
    else:
        cmd = "sudo -n systemctl restart mihomo"
    subprocess.run(["bash", "-c", cmd], capture_output=True)
    time.sleep(8)


def update_sub():
    log("更新订阅源(触发 mihomo provider 刷新)...")
    try:
        d = json.loads(urllib.request.urlopen(MIHOMO_API + "/providers/proxies", timeout=5).read())
        for name in d.get("providers", {}):
            try:
                api("PUT", "/providers/proxies/" + quote(name, safe=""))
            except Exception:
                pass
    except Exception as e:
        log(f"触发 provider 刷新失败: {e}")
    time.sleep(8)


def main():
    # 1. mihomo 进程/端口
    m_alive, p_alive = check_mihomo()
    if not m_alive:
        log("mihomo 未运行，尝试启动")
        subprocess.run(["systemctl", "start", "mihomo"], capture_output=True)
        time.sleep(5)
        m_alive, p_alive = check_mihomo()
    if m_alive and not p_alive:
        log("7890 端口未监听，重启 mihomo")
        restart_mihomo()
        m_alive, p_alive = check_mihomo()

    # 2. 完整链路测试
    ok, detail = full_test() if p_alive else (False, "7890端口不可用")

    action = ""
    if ok:
        health = "ok"
        log(f"链路正常: {detail}")
    else:
        health = "fail"
        log(f"链路异常: {detail}")

        # 限频：120 秒内最多触发一轮修复
        now = time.time()
        can = True
        try:
            last = float(open(LIMIT_FILE).read().strip())
            can = (now - last) > SWITCH_LIMIT_SEC
        except Exception:
            can = True

        if can:
            # 第一层修复：刷新订阅源，让 fallback 组自动重选（mihomo 内建自愈）
            action = "刷新订阅+fallback自选"
            log("第一层修复：刷新订阅源，等待 fallback 自动重选...")
            update_sub()
            ok, detail = full_test()
            if ok:
                log(f"fallback 自愈成功: {detail}")
                health = "ok"
                action = "fallback自愈成功"
            else:
                log(f"刷新后仍不通({detail})，进入手动切换兜底")
                action = "自动切换节点"
                found = False
                for attempt in range(2):  # 最多两轮：正常切换 -> 重启后重试
                    if attempt == 1:
                        log("第一轮失败，重启 mihomo + 更新订阅后重试")
                        restart_mihomo()
                        update_sub()
                    auto, nodes = ranked_nodes()
                    log(f"切换轮次 {attempt+1}: 候选池 {len(nodes)} 个，URLTest当前={auto}")
                    for i, node in enumerate(nodes[:MAX_SWITCH]):
                        s, resp = switch_to(node)
                        if s != 204:
                            continue
                        time.sleep(3)
                        ok2, det2 = full_test()
                        if ok2:
                            log(f"  切换成功 [{i+1}] {node} -> {det2}")
                            try:
                                kg = json.load(open(KNOWN_GOOD_FILE))
                            except Exception:
                                kg = []
                            kg = [x for x in kg if x["node"] != node][:30]
                            kg.insert(0, {"node": node, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "detail": det2})
                            json.dump(kg, open(KNOWN_GOOD_FILE, "w"), ensure_ascii=False, indent=2)
                            found = True
                            break
                        else:
                            log(f"  切换失败 [{i+1}] {node}: {det2}")
                    if found:
                        break
                    if not found:
                        log("两轮切换均失败，维持现状")
                        ok, detail = full_test()
                        health = "ok" if ok else "fail"
                    else:
                        ok = True
                        health = "ok"
                    try:
                        open(LIMIT_FILE, "w").write(str(int(time.time())))
                    except Exception:
                        pass
        else:
            action = f"{SWITCH_LIMIT_SEC}秒内已切换过，本轮跳过"
            log(action)

    # 3. 当前出口信息（供看板）
    opener = get_opener()
    cc_now, ip_now = check_exit(opener)
    # clob 状态
    okc, detc = probe(opener, "https://clob.polymarket.com/")
    # gamma 状态（真实交易层）
    okg, detg = probe_gamma(opener)
    # binance
    okb, detb = probe(opener, "https://api.binance.com/api/v3/ping", timeout=8)

    state = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "health": health,
        "action": action,
        "mihomo_alive": bool(m_alive),
        "port7890": bool(p_alive),
        "exit_country": cc_now or "",
        "exit_ip": ip_now or "",
        "clob_ok": bool(okc),
        "clob_detail": detc,
        "gamma_ok": bool(okg),
        "gamma_detail": detg,
        "binance_ok": bool(okb),
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log(f"状态写入: health={health} exit={cc_now}({ip_now}) clob={detc} binance={detb} action={action}")

    # ---- Telegram 告警：仅在状态变化时通知 ----
    try:
        with open(LAST_STATE_FILE) as f:
            last_state = f.read().strip()
    except Exception:
        last_state = ""
    cur_state = f"{health}|{cc_now}|{ip_now}"

    notify_reason = None
    if cur_state != last_state:
        if health == "fail":
            notify_reason = f"⚠️ *Polymarket 网络链路故障！*\n出口: {cc_now}({ip_now})\nclob: {detc} gamma: {detg} binance: {detb}\n动作: {action}"
        elif action and "切换" in action:
            notify_reason = f"🔄 *Polymarket 节点已自动切换*\n出口: {cc_now}({ip_now}) health={health} clob: {detc}"
        elif health == "ok" and last_state and "ok" not in last_state:
            notify_reason = f"✅ *Polymarket 网络已恢复*\n出口: {cc_now}({ip_now}) clob: {detc}"

    if notify_reason:
        try:
            _sp = subprocess
            _sp.run(["python3", NOTIFY_SCRIPT, notify_reason], timeout=25, capture_output=True)
            log(f"已发送 Telegram 告警: {notify_reason[:50]}...")
        except Exception as e:
            log(f"Telegram 告警发送失败: {e}")
    with open(LAST_STATE_FILE, "w") as f:
        f.write(cur_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
