# v26.1 (2026-04-29) · scraper 体验修复
# - 加 og:title / meta description 兜底(修微信"微信公众平台"标题问题)
# - 加 try/except + 失败 return 而非 raise(修知乎抓不到没卡片问题)
# - 加 User-Agent 模拟真实浏览器(提升抓取成功率)
# - 失败时也返回 {url, title:"", content:"", error:"..."} · 让 emit 链路完整

from playwright.async_api import async_playwright


# 模拟一个真实的桌面 Chrome User-Agent · 防止简单的反爬
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


async def scrape(url: str, timeout_ms: int = 30000) -> dict:
    """Scrape a webpage and return its text content.

    返回结构(永远 return · 不 raise):
      成功: {url, title, content, error: None}
      失败: {url, title: "", content: "", error: "..."}
            前端通过 error 字段判断 · 显示失败卡片
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                # 用真实 User-Agent + 中文偏好 · 防简单反爬
                context = await browser.new_context(
                    user_agent=DEFAULT_UA,
                    locale="zh-CN",
                    extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                )
                page = await context.new_page()
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

                # 抓标题 · 加 og:title / meta description 兜底
                # 这样修了微信公众号文章 <title> 显示"微信公众平台"的问题
                title_data = await page.evaluate("""() => {
                    const og = document.querySelector('meta[property="og:title"]');
                    const tw = document.querySelector('meta[name="twitter:title"]');
                    const h1 = document.querySelector('h1');
                    return {
                        page_title: document.title || '',
                        og_title: og ? og.getAttribute('content') : '',
                        tw_title: tw ? tw.getAttribute('content') : '',
                        h1_text: h1 ? (h1.innerText || h1.textContent || '').trim() : '',
                    };
                }""")
                # 优先级:og:title > twitter:title > <h1> > <title>
                # 同时过滤掉太短(< 4 字)和明显的垃圾标题
                bad_titles = ("微信公众平台", "微信公众号", "公众号", "知乎", "首页", "Loading...", "Untitled")
                candidates = [
                    title_data.get("og_title", "").strip(),
                    title_data.get("tw_title", "").strip(),
                    title_data.get("h1_text", "").strip(),
                    title_data.get("page_title", "").strip(),
                ]
                title = ""
                for c in candidates:
                    if c and len(c) >= 4 and c not in bad_titles:
                        title = c
                        break
                # 全都被过滤掉 · 退而用 page_title (即使是垃圾)
                if not title:
                    title = title_data.get("page_title", "") or url

                # 抓正文 · 去除 script/style/nav/footer/header/aside
                content = await page.evaluate("""() => {
                    const remove = document.querySelectorAll('script, style, nav, footer, header, aside, .header, .footer, .nav, .sidebar');
                    remove.forEach(el => el.remove());
                    return document.body?.innerText || '';
                }""")

                # 清理空白
                content = "\n".join(
                    line.strip() for line in content.splitlines() if line.strip()
                )

                # 内容太短 · 大概率是反爬登录墙 · 给 error 提示
                if len(content) < 80:
                    return {
                        "url": url,
                        "title": title,
                        "content": content,
                        "error": f"页面正文太短({len(content)} 字符)· 可能需要登录或被反爬拦截",
                    }

                return {
                    "url": url,
                    "title": title,
                    "content": content[:8000],
                    "error": None,
                }
            finally:
                await browser.close()

    except Exception as e:
        # 任何异常(超时 / DNS 失败 / Playwright 崩溃 / SSL / 反爬挂死等)
        # 都 return · 不 raise · 让前端能渲染失败卡片
        err_msg = str(e)[:200]
        print(f"[SCRAPE] ⚠️ 抓取失败 url={url[:80]!r} err={err_msg!r}", flush=True)
        return {
            "url": url,
            "title": "",
            "content": "",
            "error": err_msg or "unknown scrape error",
        }


# Tool definition for Anthropic Tool Use API
TOOL_DEFINITION = {
    "name": "scrape_webpage",
    "description": (
        "Fetch and extract the text content of a webpage. Use this to read the full "
        "content of a specific URL when a search result snippet is not enough. "
        "Common use case: user pastes a URL (微信公众号 / 知乎 / 博客 / 新闻) and asks for summary or analysis. "
        "Returns {url, title, content, error}. If error is non-null, tell the user the page couldn't be read "
        "(usually due to login wall or anti-scraping)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL of the webpage to scrape",
            },
        },
        "required": ["url"],
    },
}
