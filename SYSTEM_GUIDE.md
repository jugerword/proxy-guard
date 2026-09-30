# Polymarket 交易系统全栈说明文档

> **给新 Agent 的阅读须知（30 秒版）**
> 1. 这套系统是**自愈的**——网络断了会自动修，配置被改坏了会自动恢复。**不要手动乱改底层配置**，遇到问题先看 `status.json` 和 `guard.log`。
> 2. 一切判定以**实测**为准，不要凭"感觉不通"就改代码或配置。
> 3. 本文档末尾有**红线清单**（改了就出事的东西）和**故障排查 SOP**（遇到问题该走哪条路）。
> 4. 你负责的业务系统（采集、训练、交易）运行在 `/home/zz/data/polymarket15BTC/`，**不要碰 `/home/zz/polymarket15BTC/`**（那是旧副本残留，改错了会误导所有人）。

---

## 1. 系统全景架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        用户设备（Mac / 手机 / iPad）                  │
│         通过 Tailscale / 局域网 访问 Linux 上的看板与 API            │
└───────────────┬─────────────────────────────────────────────────────┘
                │ SSH / HTTP
┌───────────────▼─────────────────────────────────────────────────────┐
│                 Linux 生产机（192.168.0.110, Ubuntu）                │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ ① 网络层：mihomo（Clash Meta 内核 v1.19.31）                   │  │
│  │    4 个免费订阅池（4829 节点）→ 交易专用 fallback → 分流规则    │  │
│  │    监听 :7890（HTTP/SOCKS 代理） :9090（控制 API）              │  │
│  └──────────────────────────┬─────────────────────────────────────┘  │
│                             │ 自愈（每分钟 cron）                     │
│  ┌──────────────────────────▼─────────────────────────────────────┐  │
│  │ ② 守护层：proxy-guard v6（guard.py）                            │  │
│  │    实测出口区域 + Polymarket 行情 + Binance → 四级自愈          │  │
│  │    输出 status.json（看板 / 其他 Agent 读这个）                  │  │
│  └──────────────────────────┬─────────────────────────────────────┘  │
│                             │ 防篡改（每分钟 cron）                   │
│  ┌──────────────────────────▼─────────────────────────────────────┐  │
│  │ ③ 防篡改层：config-check.sh                                     │  │
│  │    校验 config.yaml 完整性，被改写→恢复 v6 模板 + 重启 mihomo   │  │
│  └──────────────────────────┬─────────────────────────────────────┘  │
│                             │ 流量走 127.0.0.1:7890（环境变量注入）   │
│  ┌──────────────────────────▼─────────────────────────────────────┐  │
│  │ ④ 交易系统层：xiaos.service（systemd, Restart=always）          │  │
│  │    运行目录 /home/zz/data/polymarket15BTC                       │  │
│  │    Node 18 + evo/server.mjs --portfolio（下单/撤单/持仓）       │  │
│  │    + laya_sidecar_linux.py（Python 陪跑）                       │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  其他常驻系统：openclaw 工作区（聚合/报告/推送）、训练任务           │
└─────────────────────────────────────────────────────────────────────┘
```

**分层职责一句话**：① 提供通道 → ② 保证通道永远健康 → ③ 防止有人改坏通道 → ④ 在健康通道上跑交易。

---

## 2. 网络层：mihomo

### 2.1 角色与版本

| 项 | 值 |
|---|---|
| 内核 | `/usr/local/bin/mihomo`（Mihomo Meta **v1.19.31** linux amd64） |
| 旧内核备份 | `/usr/local/bin/mihomo.1.18.bak`（v1.18 不支持 anytls，**勿再用**） |
| 配置文件 | `/home/zz/.config/mihomo/config.yaml` |
| 恢复基线模板 | `/home/zz/.config/mihomo/config.yaml.v6.template` |
| systemd 服务 | `mihomo.service`（随开机自启） |
| 监听端口 | `7890` HTTP/SOCKS 代理、`7891` SOCKS、`9090` 控制 API（127.0.0.1） |

> ⚠️ **v1.19.31 的组类型变化**：`selector` 已改名为 `select`。写配置时只能使用 `fallback` / `url-test` / `select` / `load-balance`。**当前 v6 配置只用了 `fallback` 和 `url-test`**，不要改成别的。

### 2.2 免费订阅池（proxy-providers）

| provider | 来源 | 节点数（实测） |
|---|---|---|
| free1 | peasoft/NoMoreWalls（GitHub raw） | 276 |
| free2 | mahdibland/V2RayAggregator sub_merge | 4338 |
| free3 | mahdibland/ShadowsocksAggregator Eternity | 200 |
| free4 | freefq（jsdelivr CDN） | 15 |

- 全部带 `health-check: https://clob.polymarket.com/`（每 60 秒测一次，自动剔除死节点）。
- 订阅每 3600 秒自动刷新（机场节点会换 IP，免费源也一样）。

### 2.3 组与分流规则

```
proxy-groups:
  - 交易专用    type: fallback   # 出口组，按健康度自动选最优
      proxies: [自动选择, DIRECT]
      use: [free1, free2, free3, free4]
      url: https://clob.polymarket.com/   # 健康检查目标（60s）
      fallback-filter: geoip CN + 私网段   # 中国 IP 节点直接排除
  - 自动选择    type: url-test   # 延迟测速组（300s 一轮）
      use: [free1..4]

rules:
  - GEOIP,CN,DIRECT      # 国内流量直连（豆包、国内网站不受影响）
  - MATCH,交易专用        # 其余全部走代理出口
dns:
  fake-ip 模式；nameserver=223.5.5.5/119.29.29.29；fallback=8.8.8.8/1.1.1.1
```

**关键认知**：`GEOIP,CN,DIRECT` 保证**国内直连**——这就是为什么开代理时豆包/国内服务不受影响；`MATCH,交易专用` 让 Polymarket、Binance、Google 等全部走代理出口。

### 2.4 已弃用的东西（别再引入）

| 已弃用 | 原因 |
|---|---|
| 付费机场 付费机场A（订阅 URL `机场域名[已脱敏]/...`，uuid `REDACTED-...`） | 港/日/台/澳节点 IP 被 GFW 封死（TCP 不通，实测），只剩美国节点（Polymarket/Binance 禁区） |
| 旧配置 `config.yaml.backup`（9-28） | 实为机场订阅 base64，不是配置文件 |
| 机场 provider 文件 `subs/jichang.yaml` | 已移至 `subs/backup/jichang.20260930.yaml` 备份 |
| WireGuard 全隧道方案 | 一开全流量走隧道，豆包/本地服务全断（被用户否决） |
| AWS 自建节点 | 日本 IP 段被 GFW 重点封控，换 IP 也很快被封 |

---

## 3. 守护层：proxy-guard v6

### 3.1 文件与配置

| 文件 | 路径 |
|---|---|
| 主脚本 | `/home/zz/proxy-guard/guard.py`（v6 跨平台版） |
| 环境配置 | `/home/zz/proxy-guard/.env` |
| 状态输出 | `/home/zz/proxy-guard/status.json`（**看板和其他 Agent 的唯一权威状态源**） |
| 日志 | `/home/zz/proxy-guard/guard.log` |
| 好节点缓存 | `/home/zz/proxy-guard/known_good.json` |
| 告警脚本 | `/home/zz/proxy-guard/notify.py`（Telegram） |

`.env` 关键项：`MIHOMO_API=http://127.0.0.1:9090`、`PROXY=http://127.0.0.1:7890`、`GUARD_GROUP=交易专用`、`SUDO_PASSWORD=zz`、`MIHOMO_BIN=/usr/local/bin/mihomo`、`MIHOMO_CONF_DIR=/home/zz/.config/mihomo`。

### 3.2 健康判定标准（health=ok 必须全满足）

1. mihomo 进程存活 + 7890 端口监听
2. **实测**（走代理）`gamma-api.polymarket.com` 返回 200 + 非空行情数据（真实交易层）
3. **实测** `api.binance.com/api/v3/ping` 通（行情源）
4. **实测出口 IP 归属地**不在封锁列表 `BLOCKED = {CN, US, KP, IR, CU, SY, RU, MM, VE, CF}`（CN 实测 geoblock / US 官方禁止；**HK 实测放行**）

> **核心原则：节点名不可信，一切以实测为准。** 出口 IP 用 ipwho.is / ipapi.co / ipinfo.io / ip-api.com 多源探测。

### 3.3 v6.2 增强（2026-09-30 实测补丁）

| 增强 | 解决的问题 | 机制 |
|---|---|---|
| **出口纠偏** | mihomo 自动选路只认连通性，可能选中**封锁区域节点**（实测遇到过缅甸 MM：clob 能访问但交易被拒） | 状态写入前**强制实测出口**，若在 BLOCKED 立即切换合法节点（两轮：直接切 → 重启+刷新后再切） |
| **节点黑名单** | 切换时反复撞上封锁出口节点，浪费时间 | `blacklist.json` 记录实测封锁出口的节点，后续排序时排到最后 |
| **v1.19 provider API** | mihomo v1.19 把 provider 节点从顶层 `/proxies` 移除，导致切换候选池取空、自愈失效 | `get_nodes()` 改为从 `/providers/proxies/{name}` 逐池拉取（4687 节点全可用） |
| **切换探测延长** | 免费节点握手慢，3 秒探测误判失败 | 每节点切换后等待 5 秒再实测 |
| **候选池扩容** | 死节点多，15 个候选不够 | MAX_SWITCH 默认 15 → 30 |

> **重要认知**：mihomo 的 fallback 健康检查（HTTP 200）**无法区分"能访问"和"允许交易"**——缅甸/中国节点访问 clob 也是 200。所以**必须由 guard 实测出口区域并强制纠偏**，这是这套系统稳定性的核心保障。

### 3.4 四级自愈（链路故障时的完整动作链）

```
第一层  刷新订阅源 → mihomo fallback 组自动重选健康节点（60s 内自愈）
第二层  遍历候选节点逐个切换（known_good 缓存优先），每个实测 gamma
第三层  重启 mihomo + 更新订阅 + 再试一轮
兜底    全部失败 → 维持现状 + Telegram 告警
限频    120 秒内最多触发一轮修复（.switch_limit 文件控制）
```

切换成功的好节点写入 `known_good.json`，下次优先使用；实测封锁出口的节点写入 `blacklist.json` 排到最后。

### 3.5 调度方式

```
crontab（用户 zz，Linux）：
  * * * * *  /usr/bin/python3 /home/zz/proxy-guard/guard.py >>guard.log 2>&1   # 每分钟

launchd（用户 zz，macOS）：
  ~/Library/LaunchAgents/com.proxyguard.guard.plist
  StartInterval=60 + RunAtLoad（每分钟 + 开机自启）
```

> Linux 之前用 systemd `proxy-guard.timer` 出现过不排程的异常，已改为 crontab（与 health-check.sh 等一致，稳定可靠）。**不要再改回 systemd timer。**

---

## 4. 防篡改层：config-check.sh

### 4.1 为什么需要

历史上有 Agent 误改 `config.yaml`（改坏组类型导致 mihomo 崩溃循环、或把配置换成无效结构）。为防止"不知情的人/Agent 改坏网络"，增加每分钟自检。

### 4.2 机制

```
脚本：/home/zz/.local/bin/config-check.sh（crontab 每分钟）
校验：config.yaml 必须同时满足
  - free1~free4 四个 provider 都在（≥4 处 "  free[0-9]:"）
  - ≥2 处 clob.polymarket.com 健康检查
  - 含 "MATCH,交易专用" 规则
不满足 → 记录 /tmp/config-check.log → 用 v6 模板覆盖 → 重启 mihomo
```

**这就是恢复基线**：`config.yaml.v6.template` 是永远正确的 v6 结构。**任何 Agent 都不需要手动改 config.yaml——改坏了会被自动还原。**

---

## 5. 交易系统层：xiaos.service

### 5.1 服务定义（systemd）

```
/etc/systemd/system/xiaos.service
[Service]
Type=simple, User=zz, Restart=always, RestartSec=5
WorkingDirectory=/home/zz/data/polymarket15BTC          # ← 真正的工作目录！
EnvironmentFile=/home/zz/data/polymarket15BTC/.env
Environment=PATH=/home/zz/data/node18/bin:/usr/local/bin:/usr/bin:/bin
Environment=HTTPS_PROXY=http://127.0.0.1:7890           # ← 走代理！
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=LAYA_PYTHON=python3
Environment=LAYA_SCRIPT=evo/laya_sidecar_linux.py
ExecStart=/home/zz/data/node18/bin/node evo/server.mjs --portfolio
StandardOutput/Error=append:/home/zz/data/polymarket15BTC/logs/server.log
```

### 5.2 ⚠️ 两个目录千万别搞混

| 路径 | 角色 |
|---|---|
| `/home/zz/data/polymarket15BTC/` | **服务真实运行目录**（evo/server.mjs、mlpModel.mjs、laya_sidecar_linux.py 都在这里） |
| `/home/zz/polymarket15BTC/` | **旧副本/残留**（里面有旧崩溃日志，看它会被误导；**不要改它，不要拿它当真相**） |

### 5.3 交易能力现状

- 历史累计 **37 次 FILLED 成交**（真实下单、真实成交，日志含 orderId 和交易哈希）。
- 下单走 Polymarket CLOB（REST）→ 代理出口（非封锁区域）→ 成交。
- WebSocket（ws-live-v2）偶发断线（免费节点波动），**系统自带自动重连**（3-4 秒），不影响下单执行。
- 日志中 `Trading restricted in your region` = 出口 IP 在封锁区（CN/US），**遇到它先看 status.json 确认出口，不要改代码**。

### 5.4 为什么交易系统必须依赖网络层

`HTTPS_PROXY=http://127.0.0.1:7890` 是注入的环境变量——**所有交易流量都走 mihomo**。mihomo 挂了 → 直连中国 IP → Polymarket geoblock 拒单。所以"网络守护"是交易系统的生命线，**改网络配置前必须想清楚影响面**。

---

## 6. 双机架构：Mac 与 Linux 各自独立

| 项 | Mac（本地） | Linux（生产机 192.168.0.110） |
|---|---|---|
| mihomo 配置 | `/opt/homebrew/etc/mihomo/config.yaml` | `/home/zz/.config/mihomo/config.yaml` |
| guard 目录 | `~/proxy-guard/`（v6） | `/home/zz/proxy-guard/`（v6） |
| 自愈 | 独立 cron/launchd | crontab 每分钟 |
| 交易系统 | 无（曾运行，已迁移） | xiaos.service（生产） |
| 看板/API | 消费端 | 提供端 |

**设计原则：两台机器互不依赖。** Mac 关机/断网不影响 Linux 上的交易系统继续跑；Linux 出问题也不影响 Mac 的代理通道。任何"让两台互相依赖"的改造都是错误的。

连接方式：
- 局域网：`ssh zz@192.168.0.110`（密码 `zz`，sudo 密码同）
- 跨地域：Tailscale（两台都装，100.x.x.x 虚拟网段）

---

## 7. 其他常驻系统（crontab 全景）

```
30 7 * * *  openclaw free_job_aggregator + send_daily_jobs      # 每日聚合推送
0 * * * *   openclaw hourly_report / telegram_push               # 每小时报告
* * * * *   health-check.sh                                     # 系统健康检查
0 */6 * * * update-clash-nodes.sh                               # 每6h更新节点（只写 ~/.openclaw/clash-work/，不碰 config.yaml）
0 */2 * * * polymarket15BTC klines_trainer.py                   # 每2h训练
15 * * * *  polymarket15BTC minimax_optimizer.py                # 每h15分优化
* * * * *   config-check.sh                                     # config 防篡改
* * * * *   guard.py                                            # 网络自愈
```

> **update-clash-nodes.sh 不会修改 `/home/zz/.config/mihomo/config.yaml`**——它只更新 openclaw 自己的节点文件。如果你看到 config.yaml 被改动，那是 guard 自动恢复动作或外部误改，**不要再去"修"它**。

---

## 8. 关键文件路径速查表

| 路径 | 用途 |
|---|---|
| `/home/zz/.config/mihomo/config.yaml` | mihomo 主配置（v6） |
| `/home/zz/.config/mihomo/config.yaml.v6.template` | **恢复基线**（config-check 用它还原） |
| `/usr/local/bin/mihomo` | 内核 v1.19.31 |
| `/usr/local/bin/mihomo.1.18.bak` | 旧内核（勿用） |
| `/home/zz/proxy-guard/guard.py` | 守护主脚本 |
| `/home/zz/proxy-guard/status.json` | **状态权威源**（health/exit/clob/gamma/binance） |
| `/home/zz/proxy-guard/guard.log` | 守护日志 |
| `/home/zz/.local/bin/config-check.sh` | 防篡改脚本 |
| `/home/zz/data/polymarket15BTC/` | **交易系统工作目录**（别搞混） |
| `/home/zz/data/polymarket15BTC/logs/server.log` | 交易系统日志（**看这个**） |
| `/home/zz/polymarket15BTC/` | 旧副本残留（**别看、别改**） |
| `/etc/systemd/system/xiaos.service` | 交易服务定义 |
| `/etc/systemd/system/mihomo.service` | mihomo 服务定义 |

---

## 9. 红线清单（改了必出事）

1. ❌ **不要手动改 `/home/zz/.config/mihomo/config.yaml`**——改坏了 config-check.sh 会自动还原，但会造成 mihomo 重启抖动；非要改，先备份并同步更新 `.v6.template`。
2. ❌ **不要把组类型改成 `selector`/`manual`/`relay`**——v1.19.31 不支持（会启动失败崩溃循环）。
3. ❌ **不要删 `/home/zz/data/polymarket15BTC/evo/` 下任何文件**——`server.mjs`、`mlpModel.mjs`、`laya_sidecar_linux.py` 都是 xiaos.service 的依赖，缺一个服务就崩。
4. ❌ **不要改 xiaos.service 的环境变量**（尤其 `HTTPS_PROXY=127.0.0.1:7890` 和工作目录）——改了就直连中国 IP，Polymarket 全拒单。
5. ❌ **不要动 `/home/zz/polymarket15BTC/`（旧副本）**——它不是真相来源，改它纯属浪费且会误导别人。
6. ❌ **不要再引入付费机场 付费机场A**——节点已全灭（港日台 IP 被 GFW 封），唯一活着的美国节点是交易禁区。
7. ❌ **不要把 guard 调度从 crontab 改回 systemd timer**——timer 曾出现不排程的故障。
8. ❌ **不要"看到网络不通就直连测试"**——直连出口必然是中国 IP，Polymarket 必然拒单；测试必须走 `127.0.0.1:7890` 代理。
9. ❌ **不要删 `status.json` / `known_good.json`**——守护系统的状态和缓存依赖它们。

---

## 10. 故障排查 SOP（按顺序走）

**症状：Polymarket 下单失败 / geoblock / 行情断**

```
第1步  ssh zz@192.168.0.110
第2步  cat /home/zz/proxy-guard/status.json
       ├─ health=ok          → 网络健康，问题在别处（看第5步）
       ├─ health=fail        → 守护正在自愈，等 1-2 分钟再看 status.json
       └─ exit_country=CN/US → 出口在封锁区，guard 会自动切换，等 2 分钟
第3步  若 2 分钟后仍 fail：tail -30 /home/zz/proxy-guard/guard.log  看自愈动作
第4步  手动验证链路：
       python3 -c "import urllib.request;print(urllib.request.urlopen(urllib.request.Request('https://ipwho.is/',headers={'User-Agent':'Mozilla/5.0'}),timeout=8).read())"
       （必须不带任何代理直连 127.0.0.1:9090 之外的东西；对外探测要走代理）
第5步  tail -20 /home/zz/data/polymarket15BTC/logs/server.log  看交易层日志
第6步  只有确认「status=ok 但交易仍异常」才允许查代码——且先怀疑业务逻辑，不是网络
```

**症状：mihomo 挂了 / 7890 连不上**

```
systemctl status mihomo        # 看是否 active
journalctl -u mihomo -n 50     # 看启动错误
systemctl restart mihomo       # 重启
cat /home/zz/proxy-guard/status.json  # 确认恢复
```

**症状：怀疑配置被改**

```
cat /tmp/config-check.log      # 有记录 = 被改过且已自动还原
ls -la /home/zz/.config/mihomo/config.yaml   # 对比 mtime
```

---

## 11. 历史演进（为什么是现在的样子）

1. **免费 VPN / 公共节点** → 不可靠、速度差、随时失效。
2. **AWS 自建（日本）** → GFW 重点封控日本 IP 段，换 IP 也很快被封。
3. **付费机场 付费机场A（anytls）** → 港/日/台节点 IP 被 GFW 全封（TCP 实测不通），美国节点存活但为交易禁区；mihomo v1.18 还不支持 anytls 协议。→ **弃用**。
4. **WireGuard 全隧道** → 一开，豆包/国内服务全走隧道失联，被用户否决。
5. **最终方案：mihomo v1.19.31 + 免费订阅池 + proxy-guard v6 四级自愈 + config 防篡改** → 在 Mac 验证稳定（出口印度/荷兰，Polymarket+Binance 双通）后完整部署到 Linux，现为生产基线。

**核心经验**：免费节点会死、机场会被封、IP 会被识别——**唯一可靠的是"自动发现 + 自动切换 + 自动恢复"的自愈机制**，而不是任何一个固定节点。

---

## 12. 常用命令速查

```bash
# 状态
cat /home/zz/proxy-guard/status.json            # 网络健康权威状态
systemctl is-active xiaos mihomo                # 服务状态
tail -20 /home/zz/data/polymarket15BTC/logs/server.log   # 交易日志

# 手动跑一轮守护（自愈）
python3 /home/zz/proxy-guard/guard.py

# 重启网络
systemctl restart mihomo

# 控制口查询（必须绕代理直连）
python3 -c "import urllib.request,json;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));print(json.loads(op.open('http://127.0.0.1:9090/proxies').read())['proxies']['交易专用']['now'])"

# 对外实测（走代理）
python3 -c "import urllib.request;op=urllib.request.build_opener(urllib.request.ProxyHandler({'http':'http://127.0.0.1:7890','https':'http://127.0.0.1:7890'}));print(op.open('https://ipwho.is/',timeout=8).read())"
```

---

*文档版本：v1.0（2026-09-30）· 覆盖：网络层 / 守护层 / 防篡改层 / 交易系统层 / 双机架构 · 事实以 Linux 192.168.0.110 现场为准，修改前请先核对 status.json。*
