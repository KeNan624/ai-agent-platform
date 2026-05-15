from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, "/root/ai-agent-platform")

from database import SessionLocal
from services.cos_upload import upload_bytes


BASE = Path("/root/ai-agent-platform/tmp/course-covers/generated")
ADMIN_USER_ID = 5

FILES = {
    "dd": BASE / "dd-cover.png",
    "ss": BASE / "ss-cover.png",
    "dsj": BASE / "dsj-cover.png",
    "dytw": BASE / "dytw-cover.png",
    "gzh": BASE / "gzh-cover.png",
    "jd": BASE / "jd-cover.png",
    "sph": BASE / "sph-cover.png",
}


async def main() -> None:
    uploaded: dict[str, str] = {}
    for slug, path in FILES.items():
        data = path.read_bytes()
        result = await upload_bytes(
            data,
            filename=path.name,
            kind="image",
            user_id=ADMIN_USER_ID,
            content_type="image/png",
        )
        uploaded[slug] = result["url"]
        print(f"uploaded {slug} {result['url']}")

    with SessionLocal() as db:
        for slug, url in uploaded.items():
            db.execute(
                text(
                    """
                    update practice_courses
                    set cover_url = :url,
                        updated_at = now()
                    where slug = :slug
                      and content_kind = 'course'
                    """
                ),
                {"slug": slug, "url": url},
            )
        db.commit()
    print("database updated")


if __name__ == "__main__":
    asyncio.run(main())
