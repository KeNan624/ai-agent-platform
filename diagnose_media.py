"""
媒体接入诊断脚本 · 放在项目根目录跑：python3 diagnose_media.py

这个脚本会：
1. 检查 .env 是否被读到
2. 测试 ePhone 图片 API 是否通（提交一个 'test' 任务立刻看响应）
3. 测试火山 Seedance API 是否通（不提交，只试 list 接口验证 key）
"""
import os
import sys

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✅ dotenv 已加载 .env")
except Exception as e:
    print(f"❌ 加载 .env 失败: {e}")
    print("   运行: pip3 install python-dotenv --break-system-packages")
    sys.exit(1)

# 检查 key
ephone_key = os.getenv("EPHONE_API_KEY", "").strip()
ark_key = os.getenv("ARK_API_KEY", "").strip()

print(f"\n========== 环境变量 ==========")
print(f"EPHONE_API_KEY: {'✅ ' + ephone_key[:10] + '...' + ephone_key[-4:] + f' (len={len(ephone_key)})' if ephone_key else '❌ 未设置'}")
print(f"ARK_API_KEY:    {'✅ ' + ark_key[:10] + '...' + ark_key[-4:] + f' (len={len(ark_key)})' if ark_key else '❌ 未设置'}")

if not ephone_key and not ark_key:
    print("\n❌ 两个 key 都没读到。可能原因：")
    print("   1. .env 里没加 EPHONE_API_KEY= 和 ARK_API_KEY= 两行")
    print("   2. .env 文件位置不对（应该在 ~/Projects/ai-agent-platform/.env）")
    print("   请跑: cd ~/Projects/ai-agent-platform && grep 'EPHONE\\|ARK' .env")
    sys.exit(1)

# 测试 ePhone
if ephone_key:
    print(f"\n========== 测试 ePhone API ==========")
    import httpx
    try:
        r = httpx.post(
            "https://api.ephone.ai/v1/task/submit",
            headers={
                "Authorization": f"Bearer {ephone_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gemini-2.5-flash-image",
                "input": {"prompt": "a simple test: red apple"},
            },
            timeout=30.0,
        )
        print(f"HTTP 状态: {r.status_code}")
        print(f"响应前 500 字符: {r.text[:500]}")
        if r.status_code == 200:
            data = r.json()
            task_id = data.get("id") or data.get("task_id")
            print(f"✅ 提交成功! task_id = {task_id}")
            if task_id:
                print(f"   (可以用 GET https://api.ephone.ai/v1/task/{task_id} 查结果)")
        elif r.status_code == 401:
            print("❌ 401 未授权 → key 错了或过期")
        elif r.status_code == 404:
            print("❌ 404 → 模型名不对，ePhone 后台看下这个模型实际叫什么")
        else:
            print(f"❌ 其他错误")
    except Exception as e:
        print(f"❌ 请求失败: {e}")

# 测试火山
if ark_key:
    print(f"\n========== 测试火山方舟 API ==========")
    import httpx
    try:
        # 用列表接口验证 key（不消耗 token）
        r = httpx.get(
            "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks?page_num=1&page_size=1",
            headers={"Authorization": f"Bearer {ark_key}"},
            timeout=30.0,
        )
        print(f"HTTP 状态: {r.status_code}")
        print(f"响应前 500 字符: {r.text[:500]}")
        if r.status_code == 200:
            print("✅ 火山 API key 有效")
        elif r.status_code == 401:
            print("❌ 401 未授权 → key 错了")
        elif r.status_code == 403:
            print("❌ 403 禁止 → 模型未开通权限，去火山后台 STEP 2 点'开通模型'")
        else:
            print(f"❌ 其他错误")
    except Exception as e:
        print(f"❌ 请求失败: {e}")

print("\n========== 诊断完成 ==========")
print("把上面的输出截图发给 Claude。")
