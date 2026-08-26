"""离线验证 main.py 中 OAuth 请求逻辑（不启动 GUI，不涉及真实账号）"""
import json
import urllib.request
import urllib.error
import urllib.parse

MIRROR_DOMAIN = "hf-mirror.com"
HF_OAUTH_CLIENT_ID = "26be6b09-91c5-47da-9861-d2d2bb7a7e36"
OAUTH_DEVICE_URL = f"https://{MIRROR_DOMAIN}/oauth/device"
OAUTH_TOKEN_URL = f"https://{MIRROR_DOMAIN}/oauth/token"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


def oauth_post_json(url, data):
    """与 main.py _oauth_post_json 相同的实现（含浏览器 UA，绕过 Cloudflare 对 Python UA 的封禁）"""
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("User-Agent",
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise


# 1. 请求设备码
info = oauth_post_json(OAUTH_DEVICE_URL, {"client_id": HF_OAUTH_CLIENT_ID})
print("device 响应字段:", sorted(info.keys()))
assert info.get("device_code") and info.get("user_code"), "缺少 device_code/user_code"
print(f"user_code={info['user_code']}  verification_uri={info.get('verification_uri')}  expires_in={info.get('expires_in')}")

# 2. 轮询一次（预期 authorization_pending，HTTP 400 → HTTPError 分支解析 JSON）
data = oauth_post_json(OAUTH_TOKEN_URL, {
    "grant_type": DEVICE_GRANT_TYPE,
    "device_code": info["device_code"],
    "client_id": HF_OAUTH_CLIENT_ID,
})
print("token 轮询响应:", data)
assert data.get("error") == "authorization_pending", "预期 pending"
print("全部通过: 设备码获取 + 400 状态下 JSON 解析 + pending 判定")
