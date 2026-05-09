"""
fix_preset_models.py
====================
一次性数据修复脚本：把数据库里所有预设项目（is_preset=True）的 model 字段
统一刷成 claude-sonnet-4-6。

为什么要这个脚本？
  seeds.py 是幂等的 — 只在第一次启动时插入预设项目。如果你之前启动时
  seeds.py 里 model 字段写的是 claude-opus-4-6，后来改成 sonnet，
  数据库里已存在的记录不会被刷新。所以要用这个脚本手工 ALTER 一下。

怎么跑：
  cd ~/Projects/ai-agent-platform
  python3 fix_preset_models.py

会打印每个项目改前改后的 model，确认无误即可。
"""

from database import SessionLocal
from models import Project

TARGET_MODEL = "claude-sonnet-4-6"

def main():
    db = SessionLocal()
    try:
        presets = db.query(Project).filter(
            Project.is_preset.is_(True),
            Project.user_id.is_(None),
        ).all()

        print(f"共找到 {len(presets)} 个预设项目：\n")
        changed = 0
        for p in presets:
            old = p.model
            print(f"  [{p.id:>2}] {p.emoji or '❓':>2}  {p.name:<12} model: {old}", end="")
            if old != TARGET_MODEL:
                p.model = TARGET_MODEL
                print(f"  →  {TARGET_MODEL}  ✅")
                changed += 1
            else:
                print(f"  (已经是目标值，跳过)")

        if changed > 0:
            db.commit()
            print(f"\n✅ 成功更新 {changed} 个项目的 model")
        else:
            print(f"\n✨ 所有项目 model 已是 {TARGET_MODEL}，无需改动")

    except Exception as e:
        db.rollback()
        print(f"❌ 出错：{e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
