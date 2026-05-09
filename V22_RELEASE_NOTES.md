# v22 修复说明 · 2026-04-26 晚

## 修了什么

阿川亲测 v21 联网搜索 · 后端报 Error code: 400 The use of the web search tool is not supported

定位 · ePhone 中转专门拦截了 web_search 这个工具名(可能跟 Anthropic 官方 web_search_20250305 撞名 · 中转方做了拦截策略)

修法 · 把工具名全局从 web_search 改为 internet_lookup · 涉及 7 处:
- tools/search.py · TOOL_DEFINITION 的 name 字段(1 处)
- agent/core.py · 99/106/481/483/721/723 行(6 处)

## 验收结果

阿川新对话问 "今天 iPhone 16 Pro 多少钱" · 全部通过:
- 后端日志 [v21 SIMPLE 模式] 第 1 轮 stop_reason=tool_use tool_uses=1
- 第 2 轮还自动追加搜索 · 第 3 轮 end_turn
- [v21 SIMPLE] emit citations: 10 条
- 前端「引用来源 · 10」卡片正常显示
- 中文回答 · 价格表 markdown 完美渲染
- 不复述 "I'll search for"
- 自媒体语气("我帮你分析~")

## 给下个 Claude 的注意

- 不要再把工具名改回 web_search · ePhone 会拒
- 如果以后换中转商 · 可以测一下能不能用回 web_search · 不能继续用 internet_lookup
- 工具名只是字符串 · Tavily 调用本身不受影响
