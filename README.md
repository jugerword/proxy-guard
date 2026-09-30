# proxy-guard

**Polymarket 交易链路自愈守护系统** —— 保证你的 Polymarket 交易系统 24 小时网络不断线，节点故障自动切换、自动告警。

> **支持平台**：Linux（systemd）✅ / macOS（LaunchAgents）✅ —— guard.py 为跨平台版本，自动适配两套机制。

## 为什么需要它

免费代理节点的本质是**不稳定**：存活率 <5%、活节点 20-60 分钟就死、Telegram 频道日抛节点泛滥。如果交易系统直接依赖单条代理链路，会出现：

- 代理悄悄挂掉 → 交易请求直连国内 IP → 被 Polymarket **geoblock 拒绝**（`Trading restricted in your region`）
- 全部订单静默失败，你事后才发现
- 手动切节点 1-3 分钟，正好错过 15 分钟周期的下单窗口

proxy-guard 解决的就是这个问题——**把"断网发现+自愈+告警"全部自动化**。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                   三层自愈防线                            │
├─────────────────────────────────────────────────────────┤
│ ① mihomo fallback 组（核心，内建）                      │
│    - 每 60 秒健康检查（直接测 clob.polymarket.com）      │
│    - 当前节点死亡 → 自动切换到下一健康节点（实测 16 秒） │
│    - 拒绝把自己切到不可用状态（防御性）                  │
│                                                         │
│ ② guard.py 每分钟巡检（兜底）                           │
│    - 实测 gamma-api 真实交易层（200+非空行情才算通）     │
│    - fallback 全死 → 刷新 4 个订阅源让 fallback 重选     │
│    - 仍不通 → known-good 缓存优先逐个手动切换            │
│    - 再不通 → 重启 mihomo + 更新订阅再试一轮             │
│                                                         │
│ ③ 进程守护 + 告警                                       │
│    - mihomo 崩溃自动拉起                                 │
│    - Telegram 告警（故障/切换/恢复，仅在状态变化时通知） │
└─────────────────────────────────────────────────────────┘
        状态输出: status.json（可接入你的看板）
```

**实测自愈速度：16 秒**（人为切断 → fallback 自动切到健康节点 → gamma 恢复 200）。

## 快速部署

### 前置条件

- Linux 服务器（systemd）
- Python 3
- [mihomo](https://github.com/MetaCubeX/mihomo/releases)（Clash Meta 内核，已安装并监听 7890/9090）

### 一键安装

```bash
git clone https://github.com/<你的用户名>/proxy-guard.git
cd proxy-guard
bash install.sh
```

脚本会：
1. 检查环境（Linux / python3 / systemd / mihomo）
2. 复制核心脚本到 `~/proxy-guard`
3. 交互式配置 `.env`（Telegram 告警 token / chat_id / sudo 密码，可跳过）
4. 安装 mihomo 配置（`~/.config/mihomo/config.yaml`，原配置自动备份）
5. 安装 systemd 定时器（**每分钟自动巡检**，开机 30 秒后首跑）

### 手动部署（不想用脚本）

```bash
# 1. 复制脚本
mkdir -p ~/proxy-guard && cp guard.py switch_node.py notify.py scan_nodes.py ~/proxy-guard/

# 2. 配置环境变量（写入 ~/proxy-guard/.env）
cat > ~/proxy-guard/.env << 'EOF'
TELEGRAM_BOT_TOKEN=你的bot_token        # 可选
TELEGRAM_CHAT_ID=你的chat_id            # 可选
GUARD_GROUP=交易专用
SUDO_PASSWORD=你的sudo密码              # 可选
EOF
chmod 600 ~/proxy-guard/.env

# 3. 安装 mihomo 配置
mkdir -p ~/.config/mihomo
cp config.yaml.example ~/.config/mihomo/config.yaml
sudo systemctl restart mihomo

# 4. 安装定时器
sudo cp systemd/proxy-guard.service systemd/proxy-guard.timer /etc/systemd/system/
sudo sed -i "s|__DEPLOY_DIR__|$HOME/proxy-guard|g; s|__USER__|$USER|g" /etc/systemd/system/proxy-guard.service
sudo systemctl daemon-reload
sudo systemctl enable --now proxy-guard.timer
```

## macOS 部署

macOS 没有 systemd，改用 **LaunchAgents**（登录自动加载）+ guard.py 直接进程管理（mihomo 崩溃由 guard 拉起）。

### 一键安装（macOS）

```bash
git clone https://github.com/<你的用户名>/proxy-guard.git
cd proxy-guard
bash install_macos.sh
```

脚本会：
1. 检查环境（macOS / python3 / brew）
2. 安装 mihomo（brew，ghcr 下载失败自动换清华镜像）
3. 复制核心脚本到 `~/proxy-guard`
4. 交互式配置 `.env`（Telegram 告警可跳过）
5. 安装 mihomo 配置（`~/.config/mihomo/config.yaml`，原配置自动备份）
6. 安装 LaunchAgents plist（**登录自动加载，每分钟巡检**）
7. 启动 mihomo + 首轮巡检

### 手动部署（macOS）

```bash
# 1. 安装 mihomo（ghcr 失败用清华镜像）
brew install mihomo
# 或: HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles brew install mihomo

# 2. 复制脚本
mkdir -p ~/proxy-guard && cp guard.py switch_node.py notify.py scan_nodes.py ~/proxy-guard/

# 3. 配置 .env
cat > ~/proxy-guard/.env << 'EOF'
TELEGRAM_BOT_TOKEN=你的bot_token        # 可选
TELEGRAM_CHAT_ID=你的chat_id            # 可选
GUARD_GROUP=交易专用
MIHOMO_BIN=/opt/homebrew/opt/mihomo/bin/mihomo
MIHOMO_CONF_DIR=$HOME/.config/mihomo
EOF
chmod 600 ~/proxy-guard/.env

# 4. 安装 mihomo 配置
mkdir -p ~/.config/mihomo && cp config.yaml.example ~/.config/mihomo/config.yaml

# 5. 安装 LaunchAgents（下次登录自动生效；当前会话可手动 bootstrap）
mkdir -p ~/Library/LaunchAgents
sed "s|__DEPLOY_DIR__|$HOME/proxy-guard|g; s|__USER__|$USER|g" \
  macos/com.proxyguard.guard.plist > ~/Library/LaunchAgents/com.proxyguard.guard.plist

# 6. 启动 mihomo + 首轮巡检
nohup "$(command -v mihomo)" -d ~/.config/mihomo >> ~/proxy-guard/mihomo.run.log 2>&1 &
sleep 30 && python3 ~/proxy-guard/guard.py

# 7. 开启系统代理（浏览器上网必需）
sudo networksetup -setwebproxy "Wi-Fi" 127.0.0.1 7890
sudo networksetup -setsecurewebproxy "Wi-Fi" 127.0.0.1 7890
sudo networksetup -setsocksfirewallproxy "Wi-Fi" 127.0.0.1 7890
# 或: 系统设置 → 网络 → Wi-Fi → 详细信息 → 代理 → 手动（HTTP/HTTPS/SOCKS 均 127.0.0.1:7890）
```

> macOS 注意事项：
> - guard.py 检测 mihomo 未运行时会自动拉起（LaunchAgents 每分钟巡检兜底）
> - 如果 launchctl 被系统权限限制无法手动 bootstrap，**重启/重新登录后 LaunchAgents 会自动加载**，无需手动
> - 系统代理开启后：国内网站直连（GEOIP,CN,DIRECT），外网走节点；mihomo 崩溃时浏览器会短暂断网（最多 1 分钟），guard 自动恢复

## 配置说明

### mihomo 配置（config.yaml）

核心是 **fallback 出口组**（自动故障转移）：

```yaml
proxy-groups:
  - name: 交易专用            # 出口组（guard 操作的组）
    type: fallback            # 关键：fallback = 自动故障转移
    proxies: [自动选择, DIRECT]
    use: [free1, free2, free3, free4]
    url: https://clob.polymarket.com/   # 健康检查直接测 Polymarket
    interval: 60                        # 每 60 秒健康检查
    tolerance: 2
    fallback-filter:
      geoip: true
      geoip-code: CN           # 排除国内节点
      ipcidr: [240.0.0.0/4, 0.0.0.0/32]
  - name: 自动选择
    type: url-test
    use: [free1, free2, free3, free4]
    url: https://clob.polymarket.com/
    interval: 300
rules:
  - GEOIP,CN,DIRECT
  - MATCH,交易专用
```

**内置 4 个免费订阅源**（约 300+ 真实节点）：

| 源 | URL | 说明 |
|---|---|---|
| free1 | `raw.githubusercontent.com/peasoft/NoMoreWalls/master/list.txt` | 约 276 节点 |
| free2 | `raw.githubusercontent.com/mahdibland/V2RayAggregator/master/sub/sub_merge.txt` | 约 200 节点 |
| free3 | `raw.githubusercontent.com/mahdibland/ShadowsocksAggregator/master/Eternity` | 约 200 节点 |
| free4 | `cdn.jsdelivr.net/gh/freefq/free@master/v2` | 约 15 节点（CDN 国内可达） |

> 可自行增删订阅源。**节点名不可信**（写 JP 的实际可能出口美国），一切以实测为准。

### 环境变量（.env）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 空 | Telegram bot token（可选，不配则跳过告警） |
| `TELEGRAM_CHAT_ID` | 空 | 告警接收 chat_id（可选） |
| `GUARD_GROUP` | `交易专用` | mihomo 出口组名 |
| `SUDO_PASSWORD` | 空 | sudo 密码（重启 mihomo 用；或配置 NOPASSWD sudo） |
| `MIHOMO_API` | `http://127.0.0.1:9090` | mihomo 外部控制器 |
| `PROXY` | `http://127.0.0.1:7890` | 本地代理端口 |
| `MAX_SWITCH` | `15` | 每轮最多切换测试节点数 |

## 日常使用

```bash
# 手动跑一轮巡检（立即执行一次完整检测+自愈逻辑）
python3 ~/proxy-guard/guard.py

# 查看当前状态
cat ~/proxy-guard/status.json

# 查看运行日志
tail -f ~/proxy-guard/guard.log

# 手动切换节点（自动遍历，优先 JP/SG/DE 区域）
python3 ~/proxy-guard/switch_node.py

# 手动指定节点
python3 ~/proxy-guard/switch_node.py "🇯🇵JP_3|1.6MB/s"

# 全量扫描节点池（逐个切换+实测出口，找出所有可用节点）
python3 ~/proxy-guard/scan_nodes.py

# 控制定时器
systemctl status proxy-guard.timer
sudo systemctl stop proxy-guard.timer    # 暂停自动巡检
sudo systemctl start proxy-guard.timer   # 恢复
```

## 接入看板

guard 每分钟把状态写入 `status.json`：

```json
{
  "timestamp": "2026-09-30 08:11:43",
  "health": "ok",                // ok / fail
  "action": "",                  // 最近自愈动作
  "mihomo_alive": true,
  "port7890": true,
  "exit_country": "FR",          // 出口国家（IP API 限流时为空，不影响判定）
  "exit_ip": "51.15.248.62",
  "clob_ok": true,
  "clob_detail": "HTTP 200",
  "gamma_ok": true,              // 真实交易层
  "gamma_detail": "HTTP 200 (行情数据正常)",
  "binance_ok": true
}
```

FastAPI 接入示例：

```python
@app.get("/api/proxy")
def proxy_status():
    result = {"status": "ok"}
    try:
        with open("/home/zz/proxy-guard/status.json") as f:
            result["guard"] = json.load(f)
    except Exception as e:
        result["guard"] = {"health": "unknown", "error": str(e)}
    return result
```

## 判定逻辑（为什么这样设计）

1. **以 gamma-api 为准，不以 clob 根路径为准**：clob 根路径任何区域都返回 200，不够；gamma-api 带行情数据，被 geoblock 时返回 403 `Trading restricted in your region`。**gamma 200 + 非空数据 = 交易层真实可用**。
2. **区域查询降级为辅助**：免费 IP 归属地 API 有速率限制（实测 200+ 次/半小时被限流）。gamma 已证明交易层可用时，不再依赖 IP API 判定。
3. **IP 归属地 API 限流规避**：多 API fallback（ipwho.is / ipapi.co / ipinfo.io / ip-api.com），全部失败不影响 health 判定。
4. **区域白名单**：`BLOCKED = {CN, US, KP, IR, CU, SY, RU, MM, VE, CF}`——CN 实测 geoblock，US 官方禁止，香港实测放行。
5. **known-good 缓存**：切换成功的节点自动记录，下次优先复用，避免反复试死节点。

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `exit_country` 为空 | IP API 被限流 | 正常现象，gamma_ok=true 即为链路健康 |
| Telegram 告警不生效 | 未配置 token / 或 Telegram API 需走代理 | 配置 `.env`，notify.py 默认走 127.0.0.1:7890 |
| 重启 mihomo 失败 | sudo 需要密码 | 在 `.env` 配 `SUDO_PASSWORD`，或配置 NOPASSWD sudo |
| 节点全死 | 免费源整体失效 | `python3 scan_nodes.py` 找可用节点；更换/增加订阅源 |
| 订阅拉取失败 | GitHub raw 被墙/超时 | 增加 CDN 镜像源（如 free4 的 jsDelivr） |

## 免责声明

- 本项目仅用于技术学习与个人网络可用性保障。
- 免费节点来源为公开聚合源，节点随时可能失效、含有风险，使用前请自行评估。
- 请遵守所在地法律法规及目标平台（Polymarket 等）的服务条款。
- 交易有风险，本项目不构成任何投资建议。

## License

MIT
