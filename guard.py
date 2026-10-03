#!/usr/bin/env python3
"""
proxy-guard v6 - Polymarket 交易链路守护（跨平台：Linux / macOS）

核心原则：节点名不可信，一切以【实测】为准（gamma-api 真实交易层）。
判定标准（health=ok 必须满足）：
  1. mihomo 进程 + 7890 端口存活
  2. 通过代理实测 gamma-api.polymarket.com 返回 200 + 非空行情数据（真实交易层）
  3. Binance ping 通（行情源）
  4. 出口区域不在 BLOCKED 列表（CN 实测 geoblock / US 官方禁止）
  5. v6.4: 敏感域名(google/github)任一通（链路未被 GFW 盯）

自愈动作（四级递进）：
  第一层: 刷新订阅源 -> 让 mihomo fallback 组自动重选健康节点（内建秒级自愈）
  第二层: 遍历候选节点池逐个切换（known-good 缓存优先），每个实测 gamma + 敏感域名
  第三层: 重启 mihomo -> 更新订阅 -> 再试一轮
  兜底:  全部失败 -> 维持现状 + Telegram 告警

平台适配：
  Linux  -> systemctl / ss
  macOS  -> launchd (kickstart -k) / lsof

配置（环境变量，或同目录 .env 文件）：
  PROXY_GUARD_DIR      脚本/状态文件目录（默认脚本所在目录）
  MIHOMO_API           mihomo 外部控制器地址（默认 http://127.0.0.1:9090）
  PROXY                本地代理端口（默认 http://127.0.0.1:7890）
  GUARD_GROUP          mihomo 出口组名（默认 交易专用）
  SUDO_PASSWORD        系统用户 sudo 密码（Linux restart 需要；留空则用无密码 sudo）
  MIHOMO_LABEL         macOS launchd 服务标签（默认 com.proxyguard.mihomo）
  MIHOMO_PLIST         macOS launchd plist 路径（默认 ~/Library/LaunchAgents/com.proxyguard.mihomo.plist）
  MAX_SWITCH           每轮最多切换测试的节点数（默认 15）

状态输出: <PROXY_GUARD_DIR>/status.json （可被看板 /api/proxy 读取）
"""
import urllib.request, json, time, ssl, subprocess, os, sys, platform
from urllib.parse import quote

IS_DARWIN = platform.system() == "Darwin"

BASE_DIR = os.environ.get("PROXY_GUARD_DIR", os.path.dirname(os.path.abspath(__file__)))


def load_env():
    """读取同目录 .env（不覆盖已有环境变量），Linux systemd / macOS launchd 均可配合"""
    try:
        with open(os.path.join(BASE_DIR, ".env")) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


load_env()

MIHOMO_API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090")
PROXY = os.environ.get("PROXY", "http://127.0.0.1:7890")
GROUP_NAME = os.environ.get("GUARD_GROUP", "交易专用")
SUDO_PASSWORD = os.environ.get("SUDO_PASSWORD", "")
MIHOMO_BIN = os.environ.get("MIHOMO_BIN", "/opt/homebrew/opt/mihomo/bin/mihomo")
MIHOMO_CONF_DIR = os.environ.get("MIHOMO_CONF_DIR", "/opt/homebrew/etc/mihomo")

STATUS_FILE = os.path.join(BASE_DIR, "status.json")
LOG_FILE = os.path.join(BASE_DIR, "guard.log")
LIMIT_FILE = os.path.join(BASE_DIR, ".switch_limit")
LAST_STATE_FILE = os.path.join(BASE_DIR, ".last_state")
KNOWN_GOOD_FILE = os.path.join(BASE_DIR, "known_good.json")
BLACKLIST_FILE = os.path.join(BASE_DIR, "blacklist.json")
NOTIFY_SCRIPT = os.path.join(BASE_DIR, "notify.py")

MAX_SWITCH = int(os.environ.get("MAX_SWITCH", "30"))
SWITCH_LIMIT_SEC = 120  # 限频：120 秒内最多触发一轮修复

FULL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Polymarket 允许区域（交易政策允许的区域；其余视为可用候选）
ALLOWED = {"JP","SG","DE","GB","FR","NL","KR","TW","LT","LV","CA","AU","SE","CH","IT","ES","FI","NO","DK","PL","CZ","EE","IE","BE","AT","LU","PT","GR","RO","BG","HR","SK","SI"}
# BLOCKED：仅保留实测/官方明确禁止的区域（CN 实测 geoblock，US 官方禁止。HK 实测 gamma 200，放行）
BLOCKED = {"CN","US","KP","IR","CU","SY","RU","MM","VE","CF"}

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


def _direct_opener():
    """本地 API 直连 opener（绕过环境代理，避免被代理规则/沙箱代理劫持）"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(method, path, body=None):
    req = urllib.request.Request(MIHOMO_API + path, method=method)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode() if body else None
    try:
        r = _direct_opener().open(req, data=data, timeout=5)
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


def port_open(port):
    """检测端口监听：macOS 用 lsof，Linux 用 ss"""
    if IS_DARWIN:
        try:
            out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout
            return "LISTEN" in out
        except Exception:
            return False
    else:
        try:
            out = subprocess.run(["ss", "-tln"], capture_output=True, text=True).stdout
            return ":%d " % port in out
        except Exception:
            return False


def _kill_mihomo():
    """精确结束 mihomo 进程（按 -d 参数匹配）"""
    try:
        out = subprocess.run(["pgrep", "-f", "mihomo -d /opt/homebrew/etc/mihomo"], capture_output=True, text=True).stdout
        for pid in out.split():
            subprocess.run(["kill", pid], capture_output=True)
    except Exception:
        pass


def _start_mihomo():
    """macOS 直接后台启动 mihomo（launchd 在受限环境可能不可用）"""
    try:
        logf = open(os.path.join(BASE_DIR, "mihomo.run.log"), "ab")
        subprocess.Popen(
            [MIHOMO_BIN, "-d", MIHOMO_CONF_DIR],
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        log(f"启动 mihomo 失败: {e}")


def mihomo_ctl(action):
    """启动/重启 mihomo：macOS 直接进程管理，Linux 用 systemctl"""
    if IS_DARWIN:
        if action in ("start", "restart"):
            _kill_mihomo()
            time.sleep(1)
            _start_mihomo()
            time.sleep(5)
    else:
        if SUDO_PASSWORD:
            cmd = "echo %s | sudo -S systemctl %s mihomo" % (SUDO_PASSWORD, action)
            subprocess.run(["bash", "-c", cmd], capture_output=True)
        else:
            subprocess.run(["systemctl", action, "mihomo"], capture_output=True)


def check_mihomo():
    """返回 (mihomo_alive, port_alive)"""
    alive = False
    try:
        subprocess.run(["pgrep", "-f", "mihomo -d /opt/homebrew/etc/mihomo"], check=True, capture_output=True)
        alive = True
    except Exception:
        pass
    return alive, port_open(7890)


def ensure_system_proxy_darwin():
    """macOS：检测系统代理是否指向 127.0.0.1:7890，未开启则自动开启并返回修复结果。

    背景：mihomo 在跑但系统代理被关（如 Clash GUI 退出/重启重置），
    浏览器/应用流量不走代理 → 表现为"VPN 明明在跑却上不了外网"。
    Linux 不需要此逻辑（依赖进程环境变量/TUN），返回 skip。
    """
    if not IS_DARWIN:
        return True, "skip(linux)"
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=8).stdout
        http_on = "HTTPEnable : 1" in out
        https_on = "HTTPSEnable : 1" in out
        http_ok = http_on and "HTTPPort : 7890" in out
        https_ok = https_on and "HTTPSPort : 7890" in out
        if http_ok and https_ok:
            return True, "系统代理正常(7890)"
        # 系统代理缺失/指向错误 → 找可用网络服务接口并开启
        svcs = subprocess.run(["networksetup", "-listallnetworkservices"],
                              capture_output=True, text=True, timeout=8).stdout
        iface = None
        for line in svcs.splitlines():
            l = line.strip().lstrip("*").strip()
            if not l:
                continue
            if l.lower() in ("wi-fi", "wifi"):
                iface = l
                break
        if not iface:
            # 无 Wi-Fi：取第一个普通接口（排除 Thunderbolt/DEMO/AX 等）
            for line in svcs.splitlines():
                l = line.strip().lstrip("*").strip()
                if l and all(k not in l for k in ("Thunderbolt", "DEMO", "Boardband", "Bridge")):
                    iface = l
                    break
        if not iface:
            return False, "未找到可用网络服务接口"
        subprocess.run(["networksetup", "-setwebproxy", iface, "127.0.0.1", "7890"],
                       capture_output=True, timeout=10)
        subprocess.run(["networksetup", "-setsecurewebproxy", iface, "127.0.0.1", "7890"],
                       capture_output=True, timeout=10)
        subprocess.run(["networksetup", "-setsocksfirewallproxy", iface, "127.0.0.1", "7890"],
                       capture_output=True, timeout=10)
        time.sleep(1)
        return True, f"系统代理已开启({iface} -> 127.0.0.1:7890)"
    except Exception as e:
        return False, f"系统代理设置失败: {e}"


def get_nodes():
    """v1.19 兼容：provider 节点不再展开到顶层 /proxies，需从 providers API 逐池拉取"""
    nodes = []
    try:
        d = json.loads(_direct_opener().open(MIHOMO_API + "/providers/proxies", timeout=5).read())
        for name in d.get("providers", {}):
            if name in ("default",):
                continue
            try:
                dd = json.loads(_direct_opener().open(
                    MIHOMO_API + "/providers/proxies/" + quote(name, safe=""), timeout=5).read())
                plist = dd.get("proxies", [])
                for p in plist:
                    if isinstance(p, dict) and p.get("name"):
                        nodes.append(p["name"])
                    elif isinstance(p, str):
                        nodes.append(p)
            except Exception:
                continue
    except Exception:
        pass
    return nodes


def switch_to(node):
    return api("PUT", "/proxies/" + quote(GROUP_NAME, safe=""), {"name": node})


def node_rank(name):
    n = name.upper()
    for i, cc in enumerate(NODE_PRIORITY):
        if cc in n:
            return i
    return 99


def get_auto_current():
    try:
        d = json.loads(_direct_opener().open(MIHOMO_API + "/proxies", timeout=5).read())
        for name, p in d.get("proxies", {}).items():
            if p.get("type") == "URLTest" and p.get("now"):
                return p["now"]
    except Exception:
        pass
    return None


def ranked_nodes():
    nodes = get_nodes()
    auto = get_auto_current()
    try:
        kg = json.load(open(KNOWN_GOOD_FILE))
        kg_names = [x["node"] for x in kg]
    except Exception:
        kg_names = []
    try:
        bl = json.load(open(BLACKLIST_FILE))
        bl_names = [x["node"] for x in bl]
    except Exception:
        bl_names = []
    ranked = sorted(nodes, key=lambda n: (
        0 if n in kg_names else 1,
        0 if n == auto else 1,
        node_rank(n)
    ))
    # 黑名单节点排到最后（避免选中已被实测封锁出口的节点）
    ranked = [n for n in ranked if n not in bl_names] + [n for n in ranked if n in bl_names]
    return auto, ranked


SENSITIVE_URLS = [
    "https://www.google.com",
    "https://github.com",
]

def probe_sensitive(opener, timeout=10):
    """敏感域名探测：任一通即视为链路未被盯（本次事件中美国节点 google/github 全被掐、gamma 却通）"""
    for url in SENSITIVE_URLS:
        ok, det = probe(opener, url, timeout=timeout)
        if ok:
            return True, f"{url.split('/')[2]}={det}"
    return False, "; ".join(f"{u.split('/')[2]}={probe(opener, u, timeout=timeout)[1]}" for u in SENSITIVE_URLS)


def full_test():
    """完整链路测试 v6.4：gamma 通(交易可用) + 敏感域名任一通(链路未被 GFW 盯) = health ok。
    此前只测 gamma，导致"美国节点 gamma 通但 google/github 全被掐"被误判健康。
    """
    opener = get_opener()
    okg, detg = probe_gamma(opener)
    if not okg:
        cc, ip = check_exit(opener)
        if cc in BLOCKED:
            return False, f"gamma不通且出口被封锁区域: {cc} ({ip})"
        return False, f"gamma: {detg}"
    cc, ip = check_exit(opener)
    if cc in BLOCKED:
        return False, f"gamma通但出口在封锁区域: {cc} ({ip})"
    oks, dets = probe_sensitive(opener)
    if not oks:
        return False, f"敏感域名被掐({dets}) 出口={cc}({ip}) 链路可能被盯"
    return True, f"{cc or '?'} ({ip or '?'}) gamma={detg} {dets}"


def restart_mihomo():
    log("重启 mihomo...")
    mihomo_ctl("restart")
    time.sleep(8)


def update_sub():
    log("更新订阅源(触发 mihomo provider 刷新)...")
    try:
        d = json.loads(_direct_opener().open(MIHOMO_API + "/providers/proxies", timeout=5).read())
        for name in d.get("providers", {}):
            try:
                api("PUT", "/providers/proxies/" + quote(name, safe=""))
            except Exception:
                pass
    except Exception as e:
        log(f"触发 provider 刷新失败: {e}")
    time.sleep(8)


def main():
    m_alive, p_alive = check_mihomo()
    if not m_alive:
        log("mihomo 未运行，尝试启动")
        mihomo_ctl("start")
        time.sleep(5)
        m_alive, p_alive = check_mihomo()
    if m_alive and not p_alive:
        log("7890 端口未监听，重启 mihomo")
        restart_mihomo()
        m_alive, p_alive = check_mihomo()

    # 【v6.3 系统代理自愈】macOS：检测系统代理是否指向 7890，未开启自动修复
    # （浏览器/应用依赖系统代理走 mihomo；系统代理被关会表现为"VPN 在跑却上不了外网"）
    if IS_DARWIN:
        sp_ok, sp_det = ensure_system_proxy_darwin()
        log(f"系统代理: {sp_det}" + ("(已修复)" if sp_ok and "已开启" in sp_det else ""))

    ok, detail = full_test() if p_alive else (False, "7890端口不可用")

    action = ""
    if ok:
        health = "ok"
        log(f"链路正常: {detail}")
    else:
        health = "fail"
        log(f"链路异常: {detail}")

        now = time.time()
        can = True
        try:
            last = float(open(LIMIT_FILE).read().strip())
            can = (now - last) > SWITCH_LIMIT_SEC
        except Exception:
            can = True

        if can:
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
                for attempt in range(2):
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
                        time.sleep(5)
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
                            # v6.4: gamma 通但整体失败 => 敏感域名被掐（链路被盯），记黑名单防下轮回切
                            okg2, _ = probe_gamma(opener)
                            if okg2:
                                try:
                                    bl = json.load(open(BLACKLIST_FILE))
                                except Exception:
                                    bl = []
                                if not any(x["node"] == node for x in bl):
                                    bl.insert(0, {"node": node, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": "敏感域名探测失败(链路被盯)"})
                                    json.dump(bl[:100], open(BLACKLIST_FILE, "w"), ensure_ascii=False, indent=2)
                                log(f"  [v6.4] {node} gamma通但敏感域名失败，已记黑名单")
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

    opener = get_opener()
    cc_now, ip_now = check_exit(opener)
    # 【v6.1 出口纠偏】fallback 自动选路可能选中封锁区节点(如 MM/CN/US)，
    # 状态写入前强制拉回合法区域；mihomo 的 clob health-check 无法识别区域封锁，
    # 必须由 guard 实测出口并强制切换。切换后 60s 内 fallback 若回弹，下一轮会再拉回。
    if cc_now in BLOCKED:
        log(f"出口在封锁区域 {cc_now}({ip_now})，强制切换合法节点...")
        corrected = False
        for attempt in range(2):
            if attempt == 1:
                log("纠偏第一轮失败，重启 mihomo + 刷新订阅后重试")
                restart_mihomo()
                update_sub()
            _auto, nodes = ranked_nodes()
            for node in nodes[:MAX_SWITCH]:
                s, resp = switch_to(node)
                if s != 204:
                    continue
                time.sleep(5)
                cc2, ip2 = check_exit(opener)
                if cc2 and cc2 not in BLOCKED:
                    log(f"出口纠偏成功: {cc2}({ip2}) via {node}")
                    cc_now, ip_now = cc2, ip2
                    health = "ok"
                    action = "出口纠偏"
                    corrected = True
                    try:
                        kg = json.load(open(KNOWN_GOOD_FILE))
                    except Exception:
                        kg = []
                    kg = [x for x in kg if x["node"] != node][:30]
                    kg.insert(0, {"node": node, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "detail": f"{cc2}({ip2})"})
                    json.dump(kg, open(KNOWN_GOOD_FILE, "w"), ensure_ascii=False, indent=2)
                    break
                elif cc2 in BLOCKED:
                    # 该节点出口也在封锁区，记入黑名单避免下次再试
                    try:
                        bl = json.load(open(BLACKLIST_FILE))
                    except Exception:
                        bl = []
                    if not any(x["node"] == node for x in bl):
                        bl.insert(0, {"node": node, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "cc": cc2})
                        json.dump(bl[:100], open(BLACKLIST_FILE, "w"), ensure_ascii=False, indent=2)
                        log(f"黑名单记录封锁出口节点: {node} ({cc2})")
            if corrected:
                break
        if not corrected:
            health = "fail"
            action = "出口纠偏失败"
            log("出口纠偏失败：候选节点均无法脱离封锁区，维持现状")
    okc, detc = probe(opener, "https://clob.polymarket.com/")
    okg, detg = probe_gamma(opener)
    okb, detb = probe(opener, "https://api.binance.com/api/v3/ping", timeout=8)
    oks, dets = probe_sensitive(opener)

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
        "sensitive_ok": bool(oks),
        "sensitive_detail": dets,
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log(f"状态写入: health={health} exit={cc_now}({ip_now}) clob={detc} binance={detb} action={action}")

    try:
        with open(LAST_STATE_FILE) as f:
            last_state = f.read().strip()
    except Exception:
        last_state = ""
    cur_state = f"{health}|{cc_now}|{ip_now}"

    notify_reason = None
    if cur_state != last_state:
        if health == "fail":
            notify_reason = f"⚠️ *Polymarket 网络链路故障！*\n出口: {cc_now}({ip_now})\nclob: {detc} gamma: {detg} 敏感域名: {dets}\n动作: {action}"
        elif action and "切换" in action:
            notify_reason = f"🔄 *Polymarket 节点已自动切换*\n出口: {cc_now}({ip_now}) health={health} 敏感域名: {dets}"
        elif health == "ok" and last_state and "ok" not in last_state:
            notify_reason = f"✅ *Polymarket 网络已恢复*\n出口: {cc_now}({ip_now}) clob: {detc}"

    if notify_reason:
        try:
            subprocess.run(["python3", NOTIFY_SCRIPT, notify_reason], timeout=25, capture_output=True)
            log(f"已发送 Telegram 告警: {notify_reason[:50]}...")
        except Exception as e:
            log(f"Telegram 告警发送失败: {e}")
    with open(LAST_STATE_FILE, "w") as f:
        f.write(cur_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
