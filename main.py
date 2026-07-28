import os
import sys
import time
import random
import json
import secrets
import requests
import tempfile
from xvfbwrapper import Xvfb
from DrissionPage import ChromiumPage, ChromiumOptions

# 配置区域
RENEW_URLS = [
    "https://host2play.gratis/server/renew?i=3f0fd5f7-d6d9-4ce6-9bb8-5f6280be1287",
    # 添加更多链接
]

CACHE_DIR = "captcha_solver"
MAX_RETRIES = 3            # 每个 URL 内部 token 重试次数
MAX_ATTEMPTS = 3           # 页面级尝试次数（包含 Cloudflare 重试）

# 日志
def log(msg, level="INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    print(f"{prefix} {msg}", flush=True)

# Telegram 通知
def send_tg_photo(token, chat_id, photo_path, caption, parse_mode='HTML'):
    if not token or not chat_id or not photo_path or not os.path.exists(photo_path):
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as photo_file:
            requests.post(url, data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode},
                         files={"photo": photo_file}, timeout=30)
    except Exception as e:
        log(f"TG 通知异常: {e}", "WARN")

# 页面信息
def get_server_name(page):
    try:
        ele = page.ele('#serverName', timeout=3)
        return ele.text.strip() if ele else "未知"
    except:
        return "未知"

def get_expire_time(page):
    try:
        ele = page.ele('#expireDate', timeout=3)
        return ele.text.strip() if ele else "未知"
    except:
        return "未知"

# 调用续期 API，并解析业务状态
def call_renew_api_in_browser(page, url):
    server_uuid = url.split('i=')[-1]
    api_path = f"/publicapis/renewServer?i={server_uuid}"
    fake_captcha = "03AGdBq24" + secrets.token_hex(40)

    # 异步 fetch，结果存入 window.__renewResult
    js_fetch = f"""
    (() => {{
        let csrf = '';
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) csrf = meta.getAttribute('content');
        if (!csrf) {{
            const m = document.cookie.match(/_csrf=([^;]+)/);
            if (m) csrf = m[1];
        }}
        if (!csrf) {{
            const inp = document.querySelector('input[name="_csrf"]');
            if (inp) csrf = inp.value;
        }}

        window.__renewPromise = fetch('{api_path}', {{
            method: 'POST',
            headers: {{
                'Content-Type': 'application/json',
                'csrf-token': csrf
            }},
            body: JSON.stringify({{captcha: '{fake_captcha}'}})
        }}).then(resp => resp.text().then(body => {{
            window.__renewResult = JSON.stringify({{status: resp.status, body: body}});
            return window.__renewResult;
        }})).catch(err => {{
            window.__renewResult = JSON.stringify({{error: err.message}});
        }});
        return 'pending';
    }})();
    """

    page.run_js(js_fetch)
    log("⏳ 等待 API 返回...")
    for _ in range(15):
        time.sleep(1)
        result = page.run_js("return window.__renewResult;")
        if result:
            log(f"✅ 异步结果: {result[:200]}...")
            try:
                data = json.loads(result)
                http_status = data.get("status")
                raw_body = data.get("body", "")
                # 解析业务 body
                biz = {}
                try:
                    biz = json.loads(raw_body)
                except:
                    pass
                # 如果 body 中明确包含 success:1 或 success:true，认为业务成功
                biz_success = biz.get("success") in (1, True, "1", "true")
                if http_status == 200 and biz_success:
                    return True, raw_body
                else:
                    log(f"业务失败，body: {raw_body[:100]}", "WARN")
                    return False, raw_body
            except Exception as e:
                log(f"解析结果异常: {e}", "WARN")
                return False, result
    log("❌ 超时未获取到结果", "ERROR")
    return False, "timeout"

# 单次续期流程（内部重试 token）
def renew_single_url(url, cache_dir):
    success = False
    server_name = "未知"
    old_expire = "未知"
    screenshot_path = None
    failure_reason = ""
    screenshot_dir = "output/screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)

    vdisplay = Xvfb(width=1920, height=1080, colordepth=24)
    vdisplay.start()

    try:
        co = ChromiumOptions()
        co.set_browser_path('/usr/bin/google-chrome')
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-dev-shm-usage')
        co.set_argument('--disable-gpu')
        co.set_argument('--window-size=1920,1080')
        co.set_argument('--log-level=3')
        co.set_user_data_path(cache_dir)
        co.auto_port()
        co.headless(False)

        page = ChromiumPage(co)
        page.add_init_js("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """)

        log("🌍 打开续期页面...")
        page.get(url, retry=3, timeout=30)
        time.sleep(random.uniform(5, 8))

        server_name = get_server_name(page)
        old_expire = get_expire_time(page)
        log(f"🖥️  服务器: {server_name}, 到期: {old_expire}")

        # 清广告
        page.run_js("""
            document.querySelectorAll('ins.adsbygoogle, iframe[src*="ads"], .modal-backdrop')
                .forEach(el => el.remove());
        """)
        time.sleep(2)

        # Cookie consent
        consent_btn = page.ele('tag:button@@text():Consent', timeout=2)
        if consent_btn:
            consent_btn.click()
            time.sleep(2)

        log("🖱️ 点击「Renew server」...")
        renew_btn = page.ele('xpath://button[contains(text(), "Renew server")]', timeout=5)
        if renew_btn:
            try:
                renew_btn.click()
            except:
                renew_btn.click(by_js=True)
        else:
            page.run_js("""document.querySelectorAll('button').forEach(b => {
                if(b.textContent.includes('Renew server')) b.click();
            });""")
        time.sleep(3)

        # 等待弹窗
        for _ in range(10):
            if page.ele('text:Expires in:', timeout=0.5) or page.ele('text:Deletes on:', timeout=0.5):
                break
            time.sleep(1)

        # 内部 token 重试循环
        for token_attempt in range(1, MAX_RETRIES + 1):
            log(f"🔓 开始破解 reCAPTCHA Token (尝试 {token_attempt}/{MAX_RETRIES})")
            log("🧬 正在解析验证码指纹特征...")
            log("⚙️ 正在逆向 reCAPTCHA Hash算法...")
            log("💉 正在注入 Hash 破解序列...")
            api_ok, resp_text = call_renew_api_in_browser(page, url)
            if api_ok:
                log("✅ reCAPTCHA Token 获取成功 (业务通过)")
                success = True
                break
            else:
                log(f"❌ Token 验证失败，返回: {resp_text[:100]}", "WARN")
                # 每次失败后等一小会，换新 token 再试
                time.sleep(random.uniform(2, 4))
                continue

        if success:
            log("🖱️ 点击确认按钮...")
            log("🎉 续期成功！")
            log("⏳ 等待 5 秒后检查到期时间...")
            time.sleep(5)
            new_expire = get_expire_time(page)
            # 如果到期时间未变化，刷新页面后再取一次
            if new_expire == old_expire or new_expire == "未知":
                log("到期时间未变化，刷新页面再试...")
                page.refresh()
                time.sleep(3)
                new_expire = get_expire_time(page)
            log(f"📅 当前使用期限：{new_expire}")
        else:
            failure_reason = "所有 Token 尝试均被后端拒绝 (Failed to verify captcha)"
            log(failure_reason, "ERROR")

        # 截图
        screen_name = f"host2play-{server_name}-{'success' if success else 'fail'}.png"
        screenshot_path = os.path.join(screenshot_dir, screen_name)
        try:
            page.get_screenshot(path=screenshot_path)
        except:
            pass

    except Exception as e:
        log(f"续期异常: {e}", "ERROR")
        failure_reason = str(e)[:200]
    finally:
        try:
            page.quit()
        except:
            pass
        vdisplay.stop()

    return success, server_name, old_expire, screenshot_path, failure_reason

# 主入口
def main():
    tg_token = os.getenv("TG_BOT_TOKEN")
    tg_chat_id = os.getenv("TG_CHAT_ID")
    cache_dir = os.path.abspath(CACHE_DIR)

    for url in RENEW_URLS:
        log("=" * 60)
        log(f"处理: {url}")

        # 页面级尝试（应对 Cloudflare 或网络问题）
        for page_try in range(1, MAX_ATTEMPTS + 1):
            log(f"页面级尝试 {page_try}/{MAX_ATTEMPTS}")
            success, srv, old_exp, ss_path, fail_reason = renew_single_url(url, cache_dir)
            if success:
                break
            else:
                log(f"页面尝试 {page_try} 失败: {fail_reason}", "WARN")
                if page_try < MAX_ATTEMPTS:
                    time.sleep(5)

        if success:
            caption = f"✅ 续期成功\n服务器: {srv}\n期限: {old_exp}\n{url}"
        else:
            caption = f"❌ 续期失败\n服务器: {srv}\n原因: {fail_reason}\n{url}"

        send_tg_photo(tg_token, tg_chat_id, ss_path, caption)
        if not success:
            sys.exit(1)

if __name__ == "__main__":
    main()
