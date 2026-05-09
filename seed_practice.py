"""
seed_practice.py · 实战区数据初始化（v3 · 全标签覆盖版）
=============================================================
一次性脚本 · 幂等可重复跑。

灌入：
  1. 三层标签字典（82 个）
  2. ~29 个实战项目（3 个营期 + 26 个玩法库）
     每个标签至少被 1 个项目挂上，点任何标签都能看到卡片
  3. 项目 ↔ 标签的关联关系

用法（在项目根目录下）：
    python3 seed_practice.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import Base, SessionLocal, engine
from models import PracticeProject, PracticeProjectTag, PracticeTag  # noqa: F401


# ════════════════════════════════════════════════════════════
# 标签字典 · 三层（"全部"不入库，前端写死）
# ════════════════════════════════════════════════════════════

PLATFORMS = [
    "小红书", "视频号", "公众号", "抖音", "微信", "闲鱼", "YouTube", "TikTok",
    "X", "Reddit", "B站", "快手", "线下实体", "YouTube Shorts", "Amazon", "SHEIN",
    "淘宝", "知乎", "小绿书", "得物",
]

CATEGORIES = [
    "自媒体", "电商", "出海", "IP", "AI产品开发", "小程序", "企业服务",
    "垂直小号", "一人公司", "虚拟产品", "AI自媒体", "垂直小店", "B站好物",
    "抖音自然流CPS", "出海工具", "知识付费", "跨境电商", "小红书买手",
    "外卖推客", "公众号爆文", "定制红包封面", "微信问一问", "AI短剧",
    "AI小说", "AI代写",
]

SKILLS = [
    "AI", "OpenClaw", "Seedance2.0", "AI编程", "AI工作流", "AI视频", "AI生图",
    "AI写作", "投放推广", "电商选品", "销售成交", "私域运营", "直播带货",
    "生财认知", "社交链接", "团队管理", "优势发掘", "RPA", "SEO", "Google SEO",
    "Claude Code", "Codex", "Claude Skills", "Gemini", "Cursor", "MCP", "GEO",
    "飞书多维表格", "n8n", "Coze", "Nano Banana", "Sora", "即梦", "AI数字人",
    "ChatGPT", "DeepSeek", "豆包",
]


# ════════════════════════════════════════════════════════════
# 项目数据 · 覆盖全部 82 个标签
# ════════════════════════════════════════════════════════════
# 标签覆盖策略：
#   - 每个平台标签至少被 1 个项目覆盖（20 个平台 → 至少 20 次覆盖）
#   - 每个项目类型至少被 1 个项目覆盖（25 个 → 至少 25 次）
#   - 每个技能至少被 1 个项目覆盖（37 个 → 至少 37 次）
#   - 每个项目挂 1 平台 + 1 类型 + 2-4 技能 = 4-6 标签
#   - 为了全覆盖 82 标签，至少需要 ~25 个项目

SEED_PROJECTS = [
    # ───────── 3 个营期项目 ─────────
    {
        "slug": "gzh-cold-start",
        "title": "公众号冷启动 · 第一期",
        "description": "0 粉丝起步，如何在 30 天内写出第一篇 10 万+，阿川亲自带你跑通全流程",
        "project_type": "camp",
        "status": "upcoming", "cover_emoji": "✍️", "cover_color": "1",
        "md_filename": "gzh-cold-start.md",
        "sort_order": 10, "start_date": "待定", "is_published": True,
        "tag_names": ["公众号", "自媒体", "公众号爆文", "AI写作", "SEO"],
    },
    {
        "slug": "ai-monetization",
        "title": "AI 变现实战 · 第一期",
        "description": "普通人用 AI 做副业的第一桶金路径，从选品到变现的完整 SOP",
        "project_type": "camp",
        "status": "upcoming", "cover_emoji": "🤖", "cover_color": "2",
        "md_filename": "ai-monetization.md",
        "sort_order": 20, "start_date": "待定", "is_published": True,
        "tag_names": ["公众号", "AI自媒体", "一人公司", "AI", "投放推广", "销售成交"],
    },
    {
        "slug": "xhs-startup",
        "title": "小红书起号 · 第一期",
        "description": "从 0 到千粉的流量密码，真实号养成路径 + 爆款选题拆解",
        "project_type": "camp",
        "status": "waitlist", "cover_emoji": "📱", "cover_color": "3",
        "md_filename": "xhs-startup.md",
        "sort_order": 30, "start_date": "待定", "is_published": True,
        "tag_names": ["小红书", "自媒体", "AI写作", "私域运营"],
    },

    # ───────── 26 个玩法库项目 ─────────
    {
        "slug": "douyin-ai-digital-human",
        "title": "抖音 AI 数字人带货",
        "description": "用 AI 数字人做无人直播带货，0 露脸 0 出镜，单场 GMV 5000-30000 的实操拆解",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🎬", "cover_color": "4",
        "md_filename": "douyin-ai-digital-human.md",
        "sort_order": 100, "start_date": None, "is_published": True,
        "tag_names": ["抖音", "电商", "AI数字人", "直播带货", "投放推广"],
    },
    {
        "slug": "shipinhao-family",
        "title": "视频号中老年流量赛道",
        "description": "视频号的银发经济红利未见顶，靠情感语录号 + 健康号拿自然流量",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "👴", "cover_color": "1",
        "md_filename": None,
        "sort_order": 110, "is_published": True,
        "tag_names": ["视频号", "垂直小号", "AI视频", "即梦"],
    },
    {
        "slug": "weixin-wenyiwen",
        "title": "微信问一问掘金",
        "description": "微信新出的问答流量口子，用 AI 批量写高赞回答，蹭搜索流量",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "❓", "cover_color": "5",
        "md_filename": None,
        "sort_order": 120, "is_published": True,
        "tag_names": ["微信", "微信问一问", "AI写作", "豆包", "DeepSeek"],
    },
    {
        "slug": "xianyu-no-source",
        "title": "闲鱼无货源电商",
        "description": "0 库存 0 囤货，闲鱼选品 + 一件代发，月入 5000-20000 的复刻路径",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🐟", "cover_color": "2",
        "md_filename": None,
        "sort_order": 130, "is_published": True,
        "tag_names": ["闲鱼", "电商", "电商选品", "销售成交"],
    },
    {
        "slug": "youtube-faceless",
        "title": "YouTube 无脸频道",
        "description": "英文 YouTube 纯素材 + AI 配音，靠 AdSense 分成月入 $1000+",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "📺", "cover_color": "3",
        "md_filename": None,
        "sort_order": 140, "is_published": True,
        "tag_names": ["YouTube", "出海", "AI视频", "AI写作", "ChatGPT"],
    },
    {
        "slug": "tiktok-affiliate",
        "title": "TikTok Shop 联盟带货",
        "description": "美区 TikTok 达人带货，挂小黄车走美金佣金，适合有短视频基础的",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🎵", "cover_color": "4",
        "md_filename": None,
        "sort_order": 150, "is_published": True,
        "tag_names": ["TikTok", "跨境电商", "直播带货", "投放推广"],
    },
    {
        "slug": "x-twitter-influencer",
        "title": "X/Twitter 英文 IP",
        "description": "用中文优势内容翻译成英文在 X 上建 IP，接广告单 + 知识付费",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🐦", "cover_color": "1",
        "md_filename": None,
        "sort_order": 160, "is_published": True,
        "tag_names": ["X", "IP", "AI写作", "Claude Code", "Gemini"],
    },
    {
        "slug": "reddit-traffic",
        "title": "Reddit 导流独立站",
        "description": "在垂直 subreddit 发软文，导流到 Shopify 独立站成交，转化率极高",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "👽", "cover_color": "2",
        "md_filename": None,
        "sort_order": 170, "is_published": True,
        "tag_names": ["Reddit", "出海", "Google SEO", "GEO", "AI写作"],
    },
    {
        "slug": "bzhan-cps",
        "title": "B 站好物 CPS",
        "description": "B 站挂链接带货，一条中长视频挂 10 个链接，躺赚佣金分成",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "📺", "cover_color": "3",
        "md_filename": None,
        "sort_order": 180, "is_published": True,
        "tag_names": ["B站", "B站好物", "AI视频", "Sora"],
    },
    {
        "slug": "kuaishou-local",
        "title": "快手本地生活团购",
        "description": "下沉市场的机会，快手上做本地餐饮团购，佣金 8-15%",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "⚡", "cover_color": "4",
        "md_filename": None,
        "sort_order": 190, "is_published": True,
        "tag_names": ["快手", "外卖推客", "直播带货", "电商选品"],
    },
    {
        "slug": "offline-service",
        "title": "线下技能服务变现",
        "description": "线下实体服务（家政/维修/摄影）挂靠线上获客，客单价 200-2000",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🏪", "cover_color": "5",
        "md_filename": None,
        "sort_order": 200, "is_published": True,
        "tag_names": ["线下实体", "企业服务", "私域运营", "社交链接"],
    },
    {
        "slug": "youtube-shorts-growth",
        "title": "YouTube Shorts 快速起号",
        "description": "Shorts 算法红利期，用 AI 批量剪辑 60 秒视频，1 个月破 1 万粉",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "📲", "cover_color": "1",
        "md_filename": None,
        "sort_order": 210, "is_published": True,
        "tag_names": ["YouTube Shorts", "AI自媒体", "AI视频", "Seedance2.0"],
    },
    {
        "slug": "amazon-fba",
        "title": "Amazon FBA 选品",
        "description": "用工具 + 生财认知做美亚选品，首批货 5 万成本月赚 3 万的路径",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "📦", "cover_color": "2",
        "md_filename": None,
        "sort_order": 220, "is_published": True,
        "tag_names": ["Amazon", "跨境电商", "电商选品", "出海工具", "生财认知"],
    },
    {
        "slug": "shein-supplier",
        "title": "SHEIN 供应商分销",
        "description": "SHEIN 平台女装分销，0 库存，月销 5 万单的真实玩法",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "👗", "cover_color": "3",
        "md_filename": None,
        "sort_order": 230, "is_published": True,
        "tag_names": ["SHEIN", "跨境电商", "电商选品", "团队管理"],
    },
    {
        "slug": "taobao-vertical-shop",
        "title": "淘宝垂直小店",
        "description": "淘宝单品爆款路径，1 个 SKU 做到类目前 10 的操作拆解",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🛍️", "cover_color": "4",
        "md_filename": None,
        "sort_order": 240, "is_published": True,
        "tag_names": ["淘宝", "垂直小店", "电商选品", "投放推广"],
    },
    {
        "slug": "zhihu-knowledge",
        "title": "知乎知识付费",
        "description": "知乎写专业长回答导流到私域，卖 ¥99-¥499 知识产品",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "📚", "cover_color": "5",
        "md_filename": None,
        "sort_order": 250, "is_published": True,
        "tag_names": ["知乎", "知识付费", "虚拟产品", "AI写作", "SEO"],
    },
    {
        "slug": "xiaolvshu-niche",
        "title": "小绿书垂类起号",
        "description": "小绿书（微信读书圈子）新平台红利，垂直领域 0 竞争起号",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "📗", "cover_color": "1",
        "md_filename": None,
        "sort_order": 260, "is_published": True,
        "tag_names": ["小绿书", "垂直小号", "AI写作", "ChatGPT"],
    },
    {
        "slug": "dewu-resell",
        "title": "得物转卖套利",
        "description": "得物球鞋/盲盒转卖套利，每单 50-500，月入 5000 的低门槛副业",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "👟", "cover_color": "2",
        "md_filename": None,
        "sort_order": 270, "is_published": True,
        "tag_names": ["得物", "电商", "电商选品", "私域运营"],
    },
    {
        "slug": "xhs-buyer-cps",
        "title": "小红书买手 CPS",
        "description": "小红书买手身份分销，0 库存做电商，佣金 10-30%",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "💼", "cover_color": "3",
        "md_filename": None,
        "sort_order": 280, "is_published": True,
        "tag_names": ["小红书", "小红书买手", "销售成交", "优势发掘"],
    },
    {
        "slug": "douyin-natural-cps",
        "title": "抖音自然流 CPS 玩法",
        "description": "不投千川只靠自然流，抖音图文带货 CPS 分成拆解",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🔥", "cover_color": "4",
        "md_filename": None,
        "sort_order": 290, "is_published": True,
        "tag_names": ["抖音", "抖音自然流CPS", "Nano Banana", "AI生图"],
    },
    {
        "slug": "redpacket-cover",
        "title": "定制红包封面变现",
        "description": "春节红利项目，定制红包封面 + 企业定制单，年入 10 万+",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🧧", "cover_color": "5",
        "md_filename": None,
        "sort_order": 300, "is_published": True,
        "tag_names": ["微信", "定制红包封面", "虚拟产品", "AI生图", "即梦"],
    },
    {
        "slug": "ai-short-drama",
        "title": "AI 短剧分销",
        "description": "AI 生成短剧 + 分销小程序，单剧月流水 10 万+",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🎭", "cover_color": "1",
        "md_filename": None,
        "sort_order": 310, "is_published": True,
        "tag_names": ["抖音", "AI短剧", "AI视频", "Sora", "Seedance2.0"],
    },
    {
        "slug": "ai-novel-writing",
        "title": "AI 小说上架番茄",
        "description": "用 AI 批量写网文上架番茄/七猫，单本月均 3000-8000",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "📖", "cover_color": "2",
        "md_filename": None,
        "sort_order": 320, "is_published": True,
        "tag_names": ["微信", "AI小说", "AI代写", "AI写作", "DeepSeek", "Claude Skills"],
    },
    {
        "slug": "miniprogram-tool",
        "title": "小程序工具变现",
        "description": "用 AI 编程快速开发工具类小程序，广告 + 内购月入过万",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🔧", "cover_color": "3",
        "md_filename": None,
        "sort_order": 330, "is_published": True,
        "tag_names": ["微信", "小程序", "AI产品开发", "AI编程", "Cursor", "Codex"],
    },
    {
        "slug": "automation-workflow",
        "title": "AI 工作流代做",
        "description": "给企业做 n8n / Coze 自动化工作流，单项目 5000-30000",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "⚙️", "cover_color": "4",
        "md_filename": None,
        "sort_order": 340, "is_published": True,
        "tag_names": ["公众号", "AI产品开发", "AI工作流", "n8n", "Coze", "RPA", "MCP"],
    },
    {
        "slug": "enterprise-ai-service",
        "title": "企业 AI 培训服务",
        "description": "给中小企业做 AI 落地培训，单场 3000-20000，高客单",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🏢", "cover_color": "5",
        "md_filename": None,
        "sort_order": 350, "is_published": True,
        "tag_names": ["线下实体", "企业服务", "团队管理", "飞书多维表格", "OpenClaw"],
    },
    {
        "slug": "personal-ip-one-person-company",
        "title": "一人公司 IP 打造",
        "description": "打造个人 IP → 一人公司矩阵，阿川自己走通的路径拆解",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🦸", "cover_color": "1",
        "md_filename": None,
        "sort_order": 360, "is_published": True,
        "tag_names": ["公众号", "IP", "一人公司", "生财认知", "社交链接", "团队管理"],
    },
    {
        "slug": "faceless-video-matrix",
        "title": "视频号矩阵变现",
        "description": "50 个视频号矩阵，情感/段子/正能量赛道，月入 3-10 万",
        "project_type": "playbook",
        "status": "running", "cover_emoji": "🎯", "cover_color": "2",
        "md_filename": None,
        "sort_order": 370, "is_published": True,
        "tag_names": ["视频号", "垂直小号", "AI视频", "即梦"],
    },
]


def seed():
    print("\n[seed] ════════════════════════════════════════")
    print("[seed]  实战区数据初始化 · v3 全标签覆盖版")
    print("[seed] ════════════════════════════════════════\n")

    # 1. 自动建表
    print("[seed] ① 检查/创建表结构...")
    Base.metadata.create_all(bind=engine)
    print("[seed]    ✓ 完成\n")

    db = SessionLocal()
    try:
        # 2. 灌标签字典
        print("[seed] ② 灌入标签字典...")
        existing_tags = {(t.name, t.tag_type): t for t in db.query(PracticeTag).all()}

        def upsert_tag(name: str, tag_type: str, sort_order: int) -> PracticeTag:
            key = (name, tag_type)
            if key in existing_tags:
                return existing_tags[key]
            t = PracticeTag(name=name, tag_type=tag_type, sort_order=sort_order)
            db.add(t)
            db.flush()
            existing_tags[key] = t
            return t

        added_tags = 0
        for i, name in enumerate(PLATFORMS):
            if (name, "platform") not in existing_tags:
                upsert_tag(name, "platform", i * 10); added_tags += 1
        for i, name in enumerate(CATEGORIES):
            if (name, "category") not in existing_tags:
                upsert_tag(name, "category", i * 10); added_tags += 1
        for i, name in enumerate(SKILLS):
            if (name, "skill") not in existing_tags:
                upsert_tag(name, "skill", i * 10); added_tags += 1
        db.commit()
        print(f"[seed]    ✓ 平台 {len(PLATFORMS)} · 项目类型 {len(CATEGORIES)} · 技能 {len(SKILLS)}")
        print(f"[seed]    新增 {added_tags} 个标签（其余已存在）\n")

        # 3. 灌项目数据
        print("[seed] ③ 灌入项目数据...")
        all_tags = db.query(PracticeTag).all()
        tag_by_name: dict[str, PracticeTag] = {}
        for t in all_tags:
            tag_by_name[t.name] = t

        added_p, skipped_p, missing_tags_log = 0, 0, []
        coverage = set()  # 统计覆盖了哪些标签

        for data in SEED_PROJECTS:
            data = dict(data)
            tag_names = data.pop("tag_names", [])

            existing = db.query(PracticeProject).filter(PracticeProject.slug == data["slug"]).first()
            if existing:
                print(f"[seed]    ⊙ 已存在: {data['slug']:32s} ({data['title']})")
                existing_tag_ids = {t.id for t in existing.tags}
                for tname in tag_names:
                    tag = tag_by_name.get(tname)
                    if tag:
                        coverage.add(tname)
                        if tag.id not in existing_tag_ids:
                            existing.tags.append(tag)
                            print(f"[seed]      + 补关联标签: {tname}")
                skipped_p += 1
                continue

            p = PracticeProject(**data)
            attached = []
            for tname in tag_names:
                tag = tag_by_name.get(tname)
                if tag:
                    p.tags.append(tag)
                    attached.append(tname)
                    coverage.add(tname)
                else:
                    missing_tags_log.append((data["slug"], tname))
            db.add(p)
            added_p += 1
            print(f"[seed]    + 新增: {data['slug']:32s} → {' '.join(f'#{x}' for x in attached)}")

        db.commit()
        print(f"\n[seed]    ✓ 项目 · 新增 {added_p} · 已存在 {skipped_p}")

        # 4. 覆盖度报告
        print(f"\n[seed] ④ 标签覆盖度检查...")
        all_dict_tags = set(PLATFORMS) | set(CATEGORIES) | set(SKILLS)
        uncovered = all_dict_tags - coverage
        print(f"[seed]    已覆盖 {len(coverage)}/{len(all_dict_tags)} 个标签")
        if uncovered:
            print(f"[seed]    ⚠️  未覆盖标签（这些标签点下去是空的）:")
            for name in sorted(uncovered):
                # 找出它是哪个维度
                dim = "platform" if name in PLATFORMS else ("category" if name in CATEGORIES else "skill")
                print(f"[seed]       [{dim}] {name}")
        else:
            print(f"[seed]    🎉 所有标签都有项目！")

        if missing_tags_log:
            print(f"\n[seed]    ⚠️  以下标签未在字典中找到（已忽略）:")
            for slug, tname in missing_tags_log:
                print(f"[seed]       {slug} → {tname}")

    finally:
        db.close()

    # 5. md 文件
    md_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "practice_md")
    print(f"\n[seed] ⑤ 检查 Markdown 文件: {md_dir}")
    if not os.path.isdir(md_dir):
        print(f"[seed]    ⚠️  目录不存在！请创建: mkdir -p {md_dir}")
    else:
        existing_md = set(os.listdir(md_dir))
        has_md = 0
        for data in SEED_PROJECTS:
            fname = data.get("md_filename")
            if fname:
                mark = "✓" if fname in existing_md else "✗ 缺失"
                print(f"[seed]    {mark} {fname}")
                if fname in existing_md: has_md += 1
        print(f"[seed]    {has_md} 个 md 文件就位（其余项目详情页会显示'内容待完善'）")

    print("\n[seed] ✅ 全部完成\n")


if __name__ == "__main__":
    seed()
