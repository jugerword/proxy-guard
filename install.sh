#!/usr/bin/env bash
# ============================================================
# proxy-guard 一键部署脚本
# 适用于: Linux (systemd) + mihomo 已安装
# 用法:   bash install.sh [部署目录]
#         默认部署目录: $HOME/proxy-guard
# ============================================================
set -euo pipefail

DEPLOY_DIR="${1:-$HOME/proxy-guard}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIHOMO_CONF_DIR="$HOME/.config/mihomo"
MIHOMO_CONF="$MIHOMO_CONF_DIR/config.yaml"
ENV_FILE="$DEPLOY_DIR/.env"

echo "======================================"
echo "  proxy-guard 部署到: $DEPLOY_DIR"
echo "======================================"

# ---------- 1. 环境检查 ----------
echo ""
echo "[1/6] 环境检查..."
if [[ "$(uname)" != "Linux" ]]; then
  echo "错误: 本脚本仅支持 Linux" >&2; exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误: 需要 python3" >&2; exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "错误: 需要 systemd" >&2; exit 1
fi
echo "  ✓ python3: $(python3 --version)"
echo "  ✓ systemd 可用"

# ---------- 2. mihomo 检查/安装 ----------
echo ""
echo "[2/6] mihomo 检查..."
PKG_MIHOMO="$SCRIPT_DIR/mihomo"
if command -v mihomo >/dev/null 2>&1 || pgrep -f mihomo >/dev/null 2>&1; then
  echo "  ✓ mihomo 已存在（进程运行中或命令可用）"
elif [[ -x "$PKG_MIHOMO" ]]; then
  echo "  发现 Release 包自带 mihomo，安装到 /usr/local/bin/mihomo ..."
  sudo install -m 755 "$PKG_MIHOMO" /usr/local/bin/mihomo
  if [[ ! -f /etc/systemd/system/mihomo.service ]]; then
    sudo tee /etc/systemd/system/mihomo.service >/dev/null << EOF
[Unit]
Description=mihomo (Clash Meta)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
ExecStart=/usr/local/bin/mihomo -d $HOME/.config/mihomo
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now mihomo
    echo "  ✓ mihomo 已安装并创建 systemd 服务"
  fi
else
  echo "  ! mihomo 未安装。需要先安装 mihomo（Clash Meta）："
  echo "    下载: https://github.com/MetaCubeX/mihomo/releases"
  echo "    安装示例:"
  echo "      sudo install -m 755 mihomo-linux-amd64 /usr/local/bin/mihomo"
  echo "    然后创建服务:"
  echo "      sudo systemctl enable --now mihomo"
  echo ""
  echo "    你也可以先跳过（部署完成后手动安装 mihomo 再启用定时器）。"
  read -rp "    继续安装 proxy-guard？（y/n）: " ans
  [[ "$ans" == "y" ]] || exit 1
fi

# ---------- 3. 复制文件 ----------
echo ""
echo "[3/6] 复制文件到 $DEPLOY_DIR ..."
mkdir -p "$DEPLOY_DIR"
cp "$SCRIPT_DIR/guard.py" "$SCRIPT_DIR/switch_node.py" "$SCRIPT_DIR/notify.py" "$SCRIPT_DIR/scan_nodes.py" "$DEPLOY_DIR/"
chmod +x "$DEPLOY_DIR/"*.py
echo "  ✓ 核心脚本已复制"

# ---------- 4. 生成配置 (.env) ----------
echo ""
echo "[4/6] 配置告警参数..."
if [[ -f "$ENV_FILE" ]]; then
  echo "  .env 已存在，保留现有配置: $ENV_FILE"
else
  echo "# proxy-guard 环境配置（请按需修改）" > "$ENV_FILE"
  echo "# Telegram 告警（可选，不填则静默跳过）" >> "$ENV_FILE"
  read -rp "  TELEGRAM_BOT_TOKEN（从 @BotFather 获取，可回车跳过）: " TOK
  read -rp "  TELEGRAM_CHAT_ID（给 @userinfobot 发消息获取，可回车跳过）: " CID
  echo "TELEGRAM_BOT_TOKEN=${TOK:-}" >> "$ENV_FILE"
  echo "TELEGRAM_CHAT_ID=${CID:-}" >> "$ENV_FILE"
  echo "# 出口组名（与 mihomo config.yaml 的组名一致）" >> "$ENV_FILE"
  echo "GUARD_GROUP=交易专用" >> "$ENV_FILE"
  echo "# sudo 密码（guard 重启 mihomo 时需要；留空则用 NOPASSWD sudo）" >> "$ENV_FILE"
  read -rp "  系统 sudo 密码（可回车跳过，跳过则需配置 NOPASSWD sudo）: " SUDO
  echo "SUDO_PASSWORD=${SUDO:-}" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "  ✓ 已生成 $ENV_FILE (权限 600)"
fi

# ---------- 5. mihomo 配置 ----------
echo ""
echo "[5/6] mihomo 配置..."
if [[ -f "$MIHOMO_CONF" ]]; then
  cp "$MIHOMO_CONF" "$MIHOMO_CONF.bak.$(date +%Y%m%d%H%M%S)"
  echo "  原配置已备份"
fi
mkdir -p "$MIHOMO_CONF_DIR"
if [[ -f "$SCRIPT_DIR/config.yaml.example" ]]; then
  cp "$SCRIPT_DIR/config.yaml.example" "$MIHOMO_CONF"
  echo "  ✓ 已写入 $MIHOMO_CONF （免费订阅源配置）"
  echo "  如需修改订阅源/组名，编辑此文件后执行: sudo systemctl restart mihomo"
else
  echo "  ! 未找到 config.yaml.example，跳过（请手动配置 mihomo）"
fi

# ---------- 6. systemd 服务 + 定时器 ----------
echo ""
echo "[6/6] 安装 systemd 服务..."
# 将部署目录写入 service 模板
sed "s|__DEPLOY_DIR__|$DEPLOY_DIR|g; s|__USER__|$USER|g" "$SCRIPT_DIR/systemd/proxy-guard.service" > /tmp/proxy-guard.service
sed "s|__DEPLOY_DIR__|$DEPLOY_DIR|g; s|__USER__|$USER|g" "$SCRIPT_DIR/systemd/proxy-guard.timer" > /tmp/proxy-guard.timer
sudo cp /tmp/proxy-guard.timer /etc/systemd/system/proxy-guard.timer
sudo cp /tmp/proxy-guard.service /etc/systemd/system/proxy-guard.service
sudo systemctl daemon-reload
sudo systemctl enable --now proxy-guard.timer
echo "  ✓ 定时器已启用（每分钟自动巡检，开机 30 秒后首跑）"

echo ""
echo "======================================"
echo "  部署完成！"
echo "  部署目录: $DEPLOY_DIR"
echo "  立即手动跑一轮:  cd $DEPLOY_DIR && python3 guard.py"
echo "  查看状态:        cat $DEPLOY_DIR/status.json"
echo "  手动切节点:      python3 $DEPLOY_DIR/switch_node.py"
echo "  全量扫描节点:    python3 $DEPLOY_DIR/scan_nodes.py"
echo "  告警日志:        tail -f $DEPLOY_DIR/guard.log"
echo "======================================"
