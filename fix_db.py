"""一次性修复：给 conversations 表加 project_id 列"""
from database import engine
from sqlalchemy import text

with engine.connect() as conn:
    # 检查列是否存在
    result = conn.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name='conversations' AND column_name='project_id'
    """)).fetchone()

    if result:
        print("✅ project_id 列已存在，无需修改")
    else:
        print("📝 正在添加 project_id 列...")
        conn.execute(text("ALTER TABLE conversations ADD COLUMN project_id BIGINT"))
        conn.commit()
        print("✅ 列已添加")

    # 添加索引
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_conversations_project_id ON conversations(project_id)"))
    conn.commit()
    print("✅ 索引已添加")

    # 添加外键（可能会失败如果已存在，没关系）
    try:
        conn.execute(text("""
            ALTER TABLE conversations
            ADD CONSTRAINT fk_conversations_project
            FOREIGN KEY (project_id) REFERENCES projects(id)
        """))
        conn.commit()
        print("✅ 外键已添加")
    except Exception as e:
        print(f"⚠️  外键添加跳过（可能已存在）: {e}")

    print("\n🎉 修复完成！现在可以重启后端了")
