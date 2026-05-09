"""
API 返回结构探查脚本
放在项目根目录，跑：python3 probe_apis.py

作用：
- 真实向 ePhone 提交一个简单图片任务，完整打印最终 JSON
- 真实向火山 Seedance 提交一个 3 秒视频任务，完整打印最终 JSON
- 这些完整 JSON 用来让 Claude 知道真实字段名，就能修好 media.py 的字段提取
"""
import os
import sys
import json
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import httpx
except ImportError:
    print("请先 pip3 install httpx")
    sys.exit(1)

EPHONE_KEY = os.getenv("EPHONE_API_KEY", "").strip()
ARK_KEY = os.getenv("ARK_API_KEY", "").strip()

if not EPHONE_KEY or not ARK_KEY:
    print("❌ .env 里 EPHONE_API_KEY 或 ARK_API_KEY 没配")
    sys.exit(1)


def probe_ephone():
    print("\n" + "=" * 70)
    print("【1/2】探查 ePhone 图片 API 真实返回结构")
    print("=" * 70)

    r = httpx.post(
        "https://api.ephone.ai/v1/task/submit",
        headers={"Authorization": f"Bearer {EPHONE_KEY}", "Content-Type": "application/json"},
        json={
            "model": "gemini-2.5-flash-image",
            "input": {"prompt": "a simple red apple on white background, studio lighting"},
        },
        timeout=30.0,
    )
    if r.status_code != 200:
        print(f"❌ submit 失败 HTTP {r.status_code}: {r.text[:500]}")
        return
    submit_data = r.json()
    task_id = submit_data.get("id") or submit_data.get("task_id")
    print(f"✅ submit 成功 task_id={task_id}")
    print("提交响应 JSON:")
    print(json.dumps(submit_data, ensure_ascii=False, indent=2))

    print("\n开始轮询（最多 60 秒，每 3 秒一次）...")
    for i in range(20):
        time.sleep(3)
        r = httpx.get(
            f"https://api.ephone.ai/v1/task/{task_id}",
            headers={"Authorization": f"Bearer {EPHONE_KEY}"},
            timeout=30.0,
        )
        data = r.json()
        status = str(data.get("status", "")).lower()
        progress = data.get("progress", "?")
        print(f"  #{i+1}: HTTP {r.status_code}  status={status}  progress={progress}")
        if status in ("succeeded", "success", "completed", "done", "failed", "error", "canceled", "cancelled"):
            print("\n" + "-" * 70)
            print(f"✅✅✅ 终态 status={status}  这就是我需要的最终 JSON ✅✅✅")
            print("-" * 70)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            print("-" * 70)
            return
    print("⚠️  60 秒仍未出结果，最后一次响应：")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def probe_ark():
    print("\n" + "=" * 70)
    print("【2/2】探查 火山方舟 Seedance API 真实返回结构")
    print("=" * 70)

    # 提交一个最短 3 秒视频
    body = {
        "model": "doubao-seedance-2-0-fast-260128",
        "content": [{"type": "text", "text": "a red apple on a white table, camera slowly zooms in"}],
        "ratio": "16:9",
        "duration": 3,
        "generate_audio": False,  # 探查阶段不生成音频，更快
        "watermark": False,
    }

    r = httpx.post(
        "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
        headers={"Authorization": f"Bearer {ARK_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=30.0,
    )
    if r.status_code != 200:
        print(f"❌ submit 失败 HTTP {r.status_code}: {r.text[:800]}")
        return
    submit_data = r.json()
    task_id = submit_data.get("id") or submit_data.get("task_id")
    print(f"✅ submit 成功 task_id={task_id}")
    print("提交响应 JSON:")
    print(json.dumps(submit_data, ensure_ascii=False, indent=2))

    print("\n开始轮询（最多 5 分钟，每 5 秒一次）...")
    for i in range(60):
        time.sleep(5)
        r = httpx.get(
            f"https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{task_id}",
            headers={"Authorization": f"Bearer {ARK_KEY}"},
            timeout=30.0,
        )
        data = r.json()
        status = str(data.get("status", "")).lower()
        print(f"  #{i+1}: HTTP {r.status_code}  status={status}")
        if status in ("succeeded", "success", "completed", "done", "failed", "error", "cancelled", "canceled", "expired"):
            print("\n" + "-" * 70)
            print(f"✅✅✅ 终态 status={status}  这就是我需要的最终 JSON ✅✅✅")
            print("-" * 70)
            print(json.dumps(data, ensure_ascii=False, indent=2))
            print("-" * 70)
            return
    print("⚠️  5 分钟仍未出结果，最后一次响应：")
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    probe_ephone()
    probe_ark()
    print("\n" + "=" * 70)
    print("✅ 探查完成 · 把上面两段 '✅✅✅ 终态' 的 JSON 贴给 Claude")
    print("=" * 70)
