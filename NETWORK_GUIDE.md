# proxy-guard 网络使用说明（AI Agent 交接文档）

> **读者：所有在这台机器上运行的 AI Agent / 程序**
> 阅读本文需要 2 分钟，可避免你误判网络、乱改底层代码、破坏交易链路。

---

## 一、一句话规则（最重要）

本机所有**外网请求必须走本地代理 `127.0.0.1:7890`**（HTTP/HTTPS/SOCKS5 均可）。

- ❌ **禁止直连外网**（尤其 Binance / Polymarket / Google）
- ❌ **禁止修改** mihomo 配置（`~/.config/mihomo/config.yaml`）、`~/proxy-guard/` 下任何文件、网络路由/DNS
- ❌ **禁止 kill** mihomo / guard 进程
- ✅ 网络是**自愈**的：遇到不通先看状态、等自愈，不要自己动手改

## 二、网络架构（本机是怎么上网的）

```
你的程序 ──> http://127.0.0.1:7890 ──> mihomo 内核 ──> 出口组「交易专用」(fallback)
                                          │              │
                                          │        每 60 秒健康检查 clob.polymarket.com
                                          │        当前节点死亡 → 16 秒内自动切下一健康节点
                                          │
                    国内网站 (GEOIP,CN) ──直连，不绕路
```

- **mihomo**：本地代理内核，监听 `7890`（代理端口）+ `9090`（控制 API）
- **出口组「交易专用」**：fallback 类型，自动故障转移，内置 4 个免费订阅源（数千节点），健康检查直接测 Polymarket
- **guard.py**：每分钟巡检一次，实测 **gamma-api（Polymarket 真实交易层）+ Binance + CLOB**，异常时自动执行四级修复（刷新订阅 → 自动切节点 → 重启 mihomo → 兜底），并把状态写入 `status.json`

## 三、区域约束（为什么出口不能随便选）

| 目标 | 约束 | 表现 |
|---|---|---|
| **Polymarket**（gamma-api / clob） | 出口 **≠ CN、≠ US** | CN/US 出口返回 `403 Trading restricted in your region` |
| **Binance**（api.binance.com） | 出口 **≠ US** | US 出口返回 `451`；CN 出口多数超时 |
| Google / GitHub / 其他外网 | 出口 ≠ CN | CN 出口被墙/超时 |
| 国内站点 | 直连 | GEOIP,CN,DIRECT，不占节点 |

proxy-guard 内置区域黑名单 `BLOCKED = {CN, US, ...}`，出口自动避开 → **一条通道同时保证 Polymarket 和 Binance 可用**。你不需要自己处理区域问题。

## 四、给 Agent 的硬性规则

1. **外网请求统一走代理**：
   - curl：`curl -x http://127.0.0.1:7890 https://...`
   - Python：`proxies = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}`
   - Node/其他：配置 HTTP_PROXY/HTTPS_PROXY=`http://127.0.0.1:7890`
2. **判断网络是否可用，只看两个文件**：
   - `cat ~/proxy-guard/status.json` → `health=ok` 即可用
   - `tail -20 ~/proxy-guard/guard.log` → 最近的修复动作
3. **status 为 fail 时**：先等 1~2 分钟（fallback 16 秒自动切 + guard 每分钟兜底），仍 fail 再手动：
   `python3 ~/proxy-guard/switch_node.py`
4. 你观察到"某网站短暂不通"，大概率是**节点切换窗口（≤1 分钟）**，重试即可，不是故障，更不是需要你修的问题。

## 五、status.json 字段说明

| 字段 | 含义 |
|---|---|
| `health` | `ok`=链路健康 / `fail`=异常 |
| `exit_country` / `exit_ip` | 当前出口归属地（判断是否被区域限制） |
| `gamma_ok` / `gamma_detail` | **Polymarket 真实交易层是否可用（最权威）** |
| `clob_ok` / `clob_detail` | Polymarket CLOB 是否可达 |
| `binance_ok` | Binance API 是否可达 |
| `action` | 最近一次自愈动作 |

## 六、常见误判（你可能会遇到，别中招）

| 现象 | 真相 | 正确做法 |
|---|---|---|
| "Binance 行情拿不到" | 直连了，或出口是美国节点 | 走 7890；若 `exit_country=US` 等 guard 自动切换或手动 `switch_node.py` |
| "Polymarket 403 Trading restricted" | 出口是 CN/US | 同上 |
| "代理端口连不上" | mihomo 正在自愈重启（≤1 分钟） | 等 1 分钟重试 |
| "节点换了 IP" | 免费节点存活 20-60 分钟，属正常 | 无需处理，16 秒自动切 |
| "网络很慢" | 免费节点带宽有限 | 可接受；重要操作避开高峰期 |

## 七、常用命令

```bash
cat ~/proxy-guard/status.json            # 当前状态（第一优先级）
tail -20 ~/proxy-guard/guard.log         # 自愈日志
python3 ~/proxy-guard/guard.py           # 立即执行一轮巡检
python3 ~/proxy-guard/switch_node.py     # 手动切到健康节点
python3 ~/proxy-guard/scan_nodes.py      # 全量扫描可用节点
```

---

*本文件属于 proxy-guard 项目（github.com/jugerword/proxy-guard）。如需了解更多，见仓库 README.md。*
