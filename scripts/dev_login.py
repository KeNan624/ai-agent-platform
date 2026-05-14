"""
开发用快速登录脚本：
- 在数据库里找/创建指定手机号的用户
- 给该用户签发一个有效 JWT
- 打印 token 和一行可直接粘贴到浏览器 DevTools Console 的命令

用法：
    python3 scripts/dev_login.py [phone]

不传 phone 默认用 13260018535（CLAUDE.md 里保留的测试号）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本可以从仓库根目录导入 database / models / auth
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import SessionLocal  # noqa: E402
from models import User  # noqa: E402
from auth import create_access_token  # noqa: E402


def main():
    phone = sys.argv[1] if len(sys.argv) > 1 else "13260018535"

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.phone == phone).first()
        if not user:
            # SQLite + BigInteger 不自动递增,显式分配 id
            from sqlalchemy import func
            max_id = db.query(func.max(User.id)).scalar() or 0
            user = User(
                id=max_id + 1,
                phone=phone,
                nickname=f"测试用户-{phone[-4:]}",
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            created = True
        else:
            created = False

        token = create_access_token(user.id)
    finally:
        db.close()

    print("\n" + "=" * 64)
    print(f"✓ 用户 {'已创建' if created else '已存在'}: id={user.id} phone={phone}")
    print("=" * 64)
    print("\n【方式一】浏览器打开 http://localhost:8000/login.html")
    print("         按 F12 → Console 粘贴下面这行回车，自动跳主页：\n")
    print(f"  localStorage.setItem('token','{token}'); location.href='/index.html';")
    print("\n【方式二】只复制 token：\n")
    print(f"  {token}\n")


if __name__ == "__main__":
    main()
