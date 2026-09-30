#!/usr/bin/env python3
"""
notify.py - Telegram 告警发送（可复用版）
配置（环境变量，未配置则静默跳过，不影响 guard 主流程）：
  TELEGRAM_BOT_TOKEN   机器人 token（从 @BotFather 获取）
  TELEGRAM_CHAT_ID     接收告警的 chat_id（给 @userinfobot 发送任意消息获取）
  NOTIFY_PROXY         代理地址（默认 http://127.0.0.1:7890，Telegram API 在国内需走代理）

用法：
  python3 notify.py "告警内容"
"""
import urllib.request, json, sys, os, ssl

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PROXY = os.environ.get("NOTIFY_PROXY", "http://127.0.0.1:7890")


def send(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，跳过告警", file=sys.stderr)
        return False
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ph = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    hh = urllib.request.HTTPSHandler(context=ctx)
    opener = urllib.request.build_opener(ph, hh)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        r = opener.open(req, timeout=15)
        d = json.loads(r.read())
        return d.get("ok", False)
    except Exception as e:
        print(f"发送失败: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "test"
    ok = send(msg)
    print("发送:", "成功" if ok else "失败")
    sys.exit(0 if ok else 1)
