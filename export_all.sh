#!/bin/bash
set -e

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
EXPORT_NAME="achuanai-export-$TIMESTAMP"
EXPORT_DIR="$HOME/Desktop/$EXPORT_NAME"
mkdir -p "$EXPORT_DIR"

echo "==> 1/5 导出 PostgreSQL 数据..."
pg_dump -U postgres -d ai_agent_platform -F c -f "$EXPORT_DIR/db.dump"
echo "    DB dump 大小: $(du -h "$EXPORT_DIR/db.dump" | cut -f1)"

echo "==> 2/5 打包代码为 zip（排除 .venv / __pycache__ / .git）..."
cd "$HOME/Projects"
zip -rq "$EXPORT_DIR/code.zip" ai-agent-platform \
  -x "ai-agent-platform/.venv/*" \
  -x "ai-agent-platform/venv/*" \
  -x "ai-agent-platform/__pycache__/*" \
  -x "ai-agent-platform/*/__pycache__/*" \
  -x "ai-agent-platform/*/*/__pycache__/*" \
  -x "ai-agent-platform/.git/*" \
  -x "ai-agent-platform/node_modules/*" \
  -x "ai-agent-platform/.DS_Store" \
  -x "ai-agent-platform/*/.DS_Store"
echo "    代码 zip 大小: $(du -h "$EXPORT_DIR/code.zip" | cut -f1)"

echo "==> 3/5 复制 requirements.txt..."
cd "$HOME/Projects/ai-agent-platform"
if [ -f requirements.txt ]; then
  cp requirements.txt "$EXPORT_DIR/requirements.txt"
fi

echo "==> 4/5 生成 Windows 导入指南..."
cat > "$EXPORT_DIR/IMPORT-IN-WINDOWS.txt" << 'TXT_END'
========================================
阿川 AI 超级助手 · Windows 导入指南
========================================

前置安装（一次性，手动装）：
─────────────────────────────────────
1. Python 3.9+
   下载: https://www.python.org/downloads/windows/
   安装时必须勾选 "Add Python to PATH"

2. PostgreSQL 14+
   下载: https://www.postgresql.org/download/windows/
   安装时记住你设的 postgres 用户密码

安装验证（Windows PowerShell 里跑）：
─────────────────────────────────────
python --version         # 应该显示 3.9+
psql --version           # 应该显示 14+

导入数据（按顺序跑）：
─────────────────────────────────────

第 1 步 · 解压 code.zip
  双击 code.zip 解压到任意位置（比如 D:\ai-agent-platform）

第 2 步 · 安装 Python 依赖
  在解压目录打开 PowerShell（Shift + 右键 → "在此处打开 PowerShell"）:
  pip install -r requirements.txt

  或者用刚才 export 包里的 requirements.txt:
  pip install -r D:\path-to-zip-folder\requirements.txt

第 3 步 · 创建数据库
  psql -U postgres
  # 输入密码后进入 psql 命令行:
  CREATE DATABASE ai_agent_platform;
  \q

第 4 步 · 恢复数据（PowerShell 里）
  # 进入 db.dump 所在的文件夹
  pg_restore -U postgres -d ai_agent_platform db.dump

第 5 步 · 改 .env 密码（重要！）
  打开 D:\ai-agent-platform\.env
  找到 DATABASE_URL，把密码改成你 Windows 的 postgres 密码:
  DATABASE_URL=postgresql://postgres:你的新密码@localhost:5432/ai_agent_platform

第 6 步 · 启动
  cd D:\ai-agent-platform
  python main.py

  看到 "Application startup complete" 就成了
  浏览器打开: http://localhost:8000/practice.html

========================================
有报错 → 截图发到网页版 Claude
========================================
TXT_END

echo "==> 5/5 整个文件夹打包成 1 个 zip（方便传输）..."
cd "$HOME/Desktop"
zip -rq "$EXPORT_NAME.zip" "$EXPORT_NAME"
echo "    最终 zip 大小: $(du -h "$EXPORT_NAME.zip" | cut -f1)"

echo ""
echo "╔════════════════════════════════════════╗"
echo "║  ✅ 打包完成！                          ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "📦 单文件 zip（推荐传输）:"
echo "   $HOME/Desktop/$EXPORT_NAME.zip"
echo ""
echo "📂 原始文件夹（如果想直接拷贝）:"
echo "   $EXPORT_DIR"
echo ""
echo "下一步："
echo "  1. 把 $EXPORT_NAME.zip 传到 Windows"
echo "  2. 在 Windows 上双击解压 → 看 IMPORT-IN-WINDOWS.txt"
