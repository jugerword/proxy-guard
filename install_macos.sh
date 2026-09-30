#!/usr/bin/env bash
# ============================================================
# proxy-guard macOS 一键部署脚本
# 适用于: macOS (Apple Silicon / Intel) + Homebrew
# 用法:   bash install_macos.sh [部署目录]
#         默认部署目录: $HOME/proxy-guard
# 说明:   macOS 无 systemd，使用 LaunchAgents plist（登录自动加载）
#         + guard.py 直接进程管理（mihomo 崩溃由 guard 拉起）
# ============================================================
set -euo pipefail

DEPLOY_DIR="${1:-$HOME/proxy-guard}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DEPLOY_DIR/.env"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
GUARD_PLIST="$LAUNCH_AGENTS_DIR/com.proxyguard.guard.plist"
MIHOMO_CONF_DIR="$HOME/.config/mihomo"
MIHOMO_CONF="$MIHOMO_CONF_DIR/config.yaml"

echo "========================================"
echo "  proxy-guard macOS 部署到: $DEPLOY_DIR"
echo "========================================"

# ---------- 1. 环境检查 ----------
echo ""
echo "[1/7] 环境检查..."
if [[ "$(uname)" != "Darwin" ]]; then
  echo "错误: 本脚本仅支持 macOS" >&2; exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误: 需要 python3" >&2; exit 1
fi
echo "  ✓ macOS $(sw_vers -productVersion) / $(uname -m)"

# ---------- 2. 安装 mihomo ----------
echo ""
echo "[2/7] 检查 mihomo..."
if command -v mihomo >/dev/null 2>&1; then
  echo "  ✓ mihomo 已安装: $(mihomo -v | head -1)"
else
  echo "  未安装 mihomo，尝试 brew 安装（若 ghcr.io 下载失败会自动换清华镜像）..."
  if ! command -v brew >/dev/null 2>&1; then
    echo "错误: 需要 Homebrew (https://brew.sh)" >&2; exit 1
  fi
  if ! HOMEBREW_NO_AUTO_UPDATE=1 brew install mihomo 2>/tmp/brew_mihomo.err; then
    echo "  ghcr.io 下载失败，改用清华镜像重试..."
    HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
      HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles \
      brew install mihomo
  fi
  echo "  ✓ mihomo 安装完成: $(mihomo -v | head -1)"
fi

# ---------- 3. 复制文件 ----------
echo ""
echo "[3/7] 复制文件到 $DEPLOY_DIR ..."
mkdir -p "$DEPLOY_DIR"
cp "$SCRIPT_DIR/guard.py" "$SCRIPT_DIR/switch_node.py" "$SCRIPT_DIR/notify.py" "$SCRIPT_DIR/scan_nodes.py" "$DEPLOY_DIR/"
chmod +x "$DEPLOY_DIR/"*.py
echo "  ✓ 核心脚本已复制"

# ---------- 4. 生成配置 (.env) ----------
echo ""
echo "[4/7] 配置告警参数..."
if [[ -f "$ENV_FILE" ]]; then
  echo "  .env 已存在，保留现有配置: $ENV_FILE"
else
  echo "# proxy-guard 环境配置 (macOS)" > "$ENV_FILE"
  echo "# Telegram 告警（可选，不填则静默跳过）" >> "$ENV_FILE"
  read -rp "  TELEGRAM_BOT_TOKEN（从 @BotFather 获取，可回车跳过）: " TOK
  read -rp "  TELEGRAM_CHAT_ID（给 @userinfobot 发消息获取，可回车跳过）: " CID
  echo "TELEGRAM_BOT_TOKEN=${TOK:-}" >> "$ENV_FILE"
  echo "TELEGRAM_CHAT_ID=${CID:-}" >> "$ENV_FILE"
  echo "# 出口组名（与 mihomo config.yaml 的组名一致）" >> "$ENV_FILE"
  echo "GUARD_GROUP=交易专用" >> "$ENV_FILE"
  echo "# mihomo 内核路径与配置目录（默认值即可）" >> "$ENV_FILE"
  echo "MIHOMO_BIN=/opt/homebrew/opt/mihomo/bin/mihomo" >> "$ENV_FILE"
  echo "MIHOMO_CONF_DIR=$MIHOMO_CONF_DIR" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "  ✓ 已生成 $ENV_FILE (权限 600)"
fi

# ---------- 5. mihomo 配置 ----------
echo ""
echo "[5/7] mihomo 配置..."
if [[ -f "$MIHOMO_CONF" ]]; then
  cp "$MIHOMO_CONF" "$MIHOMO_CONF.bak.$(date +%Y%m%d%H%M%S)"
  echo "  原配置已备份"
fi
mkdir -p "$MIHOMO_CONF_DIR"
cp "$SCRIPT_DIR/config.yaml.example" "$MIHOMO_CONF"
echo "  ✓ 已写入 $MIHOMO_CONF （4 个免费订阅源 + fallback 架构）"

# ---------- 6. LaunchAgents 定时巡检 ----------
echo ""
echo "[6/7] 安装 LaunchAgents（登录自动加载，每分钟巡检）..."
mkdir -p "$LAUNCH_AGENTS_DIR"
sed "s|__DEPLOY_DIR__|$DEPLOY_DIR|g; s|__USER__|$USER|g" "$SCRIPT_DIR/macos/com.proxyguard.guard.plist" > /tmp/com.proxyguard.guard.plist
cp /tmp/com.proxyguard.guard.plist "$GUARD_PLIST"
echo "  ✓ 已写入 $GUARD_PLIST （下次登录自动生效）"
echo "  立即加载（可选，当前会话生效）: launchctl bootstrap gui/$(id -u) $GUARD_PLIST"

# ---------- 7. 启动 mihomo + 首轮巡检 ----------
echo ""
echo "[7/7] 启动 mihomo 并首轮巡检..."
if ! pgrep -f "mihomo -d" >/dev/null 2>&1; then
  nohup "$(command -v mihomo)" -d "$MIHOMO_CONF_DIR" >> "$DEPLOY_DIR/mihomo.run.log" 2>&1 &
  echo "  mihomo 已后台启动，等待拉取订阅 (30s)..."
  sleep 30
fi
cd "$DEPLOY_DIR" && python3 guard.py 2>&1 | tail -3

echo ""
echo "========================================"
echo "  部署完成！"
echo "  部署目录: $DEPLOY_DIR"
echo "  状态:      cat $DEPLOY_DIR/status.json"
echo "  手动切节点: python3 $DEPLOY_DIR/switch_node.py"
echo "  全量扫描:   python3 $DEPLOY_DIR/scan_nodes.py"
echo ""
echo "  浏览器上网（必须开启系统代理）:"
echo "    sudo networksetup -setwebproxy \"Wi-Fi\" 127.0.0.1 7890"
echo "    sudo networksetup -setsecurewebproxy \"Wi-Fi\" 127.0.0.1 7890"
echo "    sudo networksetup -setsocksfirewallproxy \"Wi-Fi\" 127.0.0.1 7890"
echo "  或: 系统设置 → 网络 → Wi-Fi → 详细信息 → 代理 → 手动"
echo "     HTTP/HTTPS/SOCKS 服务器均填 127.0.0.1，端口 7890"
echo "========================================"
