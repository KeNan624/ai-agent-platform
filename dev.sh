#!/bin/bash
# 阿川 AI 超级助手 · 一键部署 + 启动脚本
# 用法:把新代码丢到 ~/Downloads · 然后跑这个脚本
# sh ~/Projects/ai-agent-platform/dev.sh

set -e

PROJECT_ROOT="$HOME/Projects/ai-agent-platform"
DOWNLOADS="$HOME/Downloads"
NOW=$(date +%H%M%S)

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${YELLOW}===== 阿川 AI · 一键部署脚本 =====${NC}"

# 1. 检测 Downloads 里有没有新代码
HAS_UPDATE=0
NEW_FILES=""

if [ -f "$DOWNLOADS/main.py" ]; then
    HAS_UPDATE=1
    NEW_FILES="$NEW_FILES main.py"
fi

if [ -f "$DOWNLOADS/core.py" ]; then
    HAS_UPDATE=1
    NEW_FILES="$NEW_FILES core.py"
fi

# 找最新的 index_vXX.html
LATEST_HTML=$(ls -t "$DOWNLOADS"/index_v*.html 2>/dev/null | head -1)
if [ -n "$LATEST_HTML" ]; then
    HAS_UPDATE=1
    NEW_FILES="$NEW_FILES $(basename $LATEST_HTML)"
fi

# 2. 如果有更新 · 备份并部署
if [ $HAS_UPDATE -eq 1 ]; then
    echo -e "${GREEN}[1/4] 检测到新代码:${NEW_FILES}${NC}"
    echo "      备份 + 部署中..."
    
    # 备份
    [ -f "$PROJECT_ROOT/main.py" ] && cp "$PROJECT_ROOT/main.py" "$PROJECT_ROOT/main_backup_${NOW}.py"
    [ -f "$PROJECT_ROOT/agent/core.py" ] && cp "$PROJECT_ROOT/agent/core.py" "$PROJECT_ROOT/agent/core_backup_${NOW}.py"
    [ -f "$PROJECT_ROOT/frontend/index.html" ] && cp "$PROJECT_ROOT/frontend/index.html" "$PROJECT_ROOT/frontend/index_backup_${NOW}.html"
    
    # 部署
    if [ -f "$DOWNLOADS/main.py" ]; then
        cp "$DOWNLOADS/main.py" "$PROJECT_ROOT/main.py"
        echo "      ✓ main.py 已部署"
        # 部署完移走 · 避免下次重复部署
        mv "$DOWNLOADS/main.py" "$DOWNLOADS/main_deployed_${NOW}.py"
    fi
    
    if [ -f "$DOWNLOADS/core.py" ]; then
        cp "$DOWNLOADS/core.py" "$PROJECT_ROOT/agent/core.py"
        echo "      ✓ core.py 已部署"
        mv "$DOWNLOADS/core.py" "$DOWNLOADS/core_deployed_${NOW}.py"
    fi
    
    if [ -n "$LATEST_HTML" ]; then
        cp "$LATEST_HTML" "$PROJECT_ROOT/frontend/index.html"
        echo "      ✓ $(basename $LATEST_HTML) 已部署到 index.html"
        mv "$LATEST_HTML" "${LATEST_HTML%.html}_deployed_${NOW}.html"
    fi
else
    echo -e "${YELLOW}[1/4] Downloads 里没有新代码 · 跳过部署${NC}"
fi

# 3. 杀旧后端
echo -e "${GREEN}[2/4] 杀掉旧后端...${NC}"
lsof -ti :8000 | xargs kill -9 2>/dev/null || echo "      (没有正在运行的后端 · 跳过)"
sleep 1

# 4. 关闭无痕窗口(后面会重开)
echo -e "${GREEN}[3/4] 关闭旧 Chrome 无痕窗口...${NC}"
osascript -e 'tell application "Google Chrome" to close (every window whose mode is "incognito")' 2>/dev/null || true

# 5. 提示用户启动后端
echo ""
echo -e "${GREEN}[4/4] 准备就绪 · 现在做这两件事:${NC}"
echo ""
echo -e "${YELLOW}① 在【这个终端】跑后端(会一直占用) · 复制下面这行:${NC}"
echo ""
echo "    cd ~/Projects/ai-agent-platform && python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000"
echo ""
echo -e "${YELLOW}② 等后端起来后 · 【新开一个终端 tab(Cmd+T)】 · 跑下面这行打开浏览器:${NC}"
echo ""
echo "    open -na \"Google Chrome\" --args --incognito \"http://localhost:8000/index.html\""
echo ""
echo -e "${GREEN}===== 脚本结束 · 祝顺利 =====${NC}"
