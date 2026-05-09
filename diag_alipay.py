"""
支付宝密钥格式诊断 V2 —— 同时测试两种格式。
用法：
  cd ~/Projects/ai-agent-platform
  python3 diag_alipay.py
"""
import os
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

app_id = os.getenv("ALIPAY_APP_ID", "")
priv_file = os.path.expanduser(os.getenv("ALIPAY_PRIVATE_KEY_FILE", ""))
pub_file = os.path.expanduser(os.getenv("ALIPAY_PUBLIC_KEY_FILE", ""))

print("=" * 60)
print(f"APP_ID       : {app_id}")
print(f"PRIVATE KEY  : {priv_file}  (exists: {Path(priv_file).exists()})")
print(f"PUBLIC KEY   : {pub_file}  (exists: {Path(pub_file).exists()})")
print("=" * 60)

priv_pem = Path(priv_file).read_text()  # full PEM with BEGIN/END
pub_pem = Path(pub_file).read_text()


def clean(raw):
    lines = [ln.strip() for ln in raw.splitlines()
             if ln.strip() and "BEGIN" not in ln and "END" not in ln]
    return "".join(lines)


priv_b64 = clean(priv_pem)
pub_b64 = clean(pub_pem)

print(f"\n私钥: PEM 格式 {len(priv_pem)} 字符  /  base64 {len(priv_b64)} 字符")
print(f"公钥: PEM 格式 {len(pub_pem)} 字符  /  base64 {len(pub_b64)} 字符")


from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient
from alipay.aop.api.domain.AlipayTradePrecreateModel import AlipayTradePrecreateModel
from alipay.aop.api.request.AlipayTradePrecreateRequest import AlipayTradePrecreateRequest


def try_call(priv, pub, label):
    print(f"\n{'─' * 60}")
    print(f"🧪 测试：{label}")
    print(f"{'─' * 60}")
    try:
        cfg = AlipayClientConfig()
        cfg.server_url = "https://openapi.alipay.com/gateway.do"
        cfg.app_id = app_id
        cfg.app_private_key = priv
        cfg.alipay_public_key = pub
        cfg.charset = "utf-8"
        cfg.sign_type = "RSA2"

        client = DefaultAlipayClient(alipay_client_config=cfg)

        model = AlipayTradePrecreateModel()
        model.out_trade_no = "DIAG" + uuid.uuid4().hex[:8].upper()
        model.total_amount = "0.01"
        model.subject = "诊断"

        req = AlipayTradePrecreateRequest(biz_model=model)
        resp = client.execute(req)
        print(f"✅ 成功")
        print(f"返回: {resp[:200]}")
        return True
    except Exception as e:
        print(f"❌ 失败: {type(e).__name__}")
        print(f"  {str(e)[:400]}")
        return False


# 方式 A：裸 base64（去掉 BEGIN/END）
try_call(priv_b64, pub_b64, "A) 裸 base64（单行，无 BEGIN/END）")

# 方式 B：完整 PEM（带 BEGIN/END）
try_call(priv_pem, pub_pem, "B) 完整 PEM（带 BEGIN/END）")

# 方式 C：私钥 PEM + 公钥 base64
try_call(priv_pem, pub_b64, "C) 私钥 PEM + 公钥 base64")

# 方式 D：私钥 base64 + 公钥 PEM
try_call(priv_b64, pub_pem, "D) 私钥 base64 + 公钥 PEM")

print("\n" + "=" * 60)
print("完成。请把上面输出截图给 Claude。")
