#!/usr/bin/env python3
"""
HF镜像下载器 - 自动将 HuggingFace 下载地址转换为国内镜像地址并下载到指定文件夹
适用于 ComfyUI 工作流模型下载场景
"""

import os
import sys
import re
import json
import time
import threading
import ssl
import webbrowser
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import collections

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

# ============================================================
# 常量
# ============================================================

APP_TITLE = "ComfyUI HF 镜像下载器 v1.0 (标签分组版)"
MIRROR_DOMAIN = "hf-mirror.com"
# HF 官方域名 → 镜像域名（仅 HF 走镜像；CivitAI 直连官方，见 CIVITAI_HOSTS 注释）
MIRROR_DOMAIN_MAP = {
    "huggingface.co": MIRROR_DOMAIN,
    "hf.co": MIRROR_DOMAIN,
}
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 浏览器 UA：镜像站 / CivitAI 的 Cloudflare 会封禁 Python-urllib 默认 UA（错误码 1010）
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# CivitAI 域名（官方 + 旧镜像，仅用于识别站点类型）
CIVITAI_HOSTS = ("civitai.com", "www.civitai.com", "civitai.red", "www.civitai.red")

def is_civitai_url(url):
    """判断是否 CivitAI 域名（官方或镜像）"""
    return (urllib.parse.urlparse(url).hostname or "").lower() in CIVITAI_HOSTS


def parse_content_disposition(headers):
    """从 Content-Disposition 头解析文件名；解析失败返回 None
    用于 CivitAI 等下载端点：原始 URL 无文件名，真实名字在重定向后的响应头里"""
    cd = headers.get("Content-Disposition") or headers.get("content-disposition")
    if not cd:
        return None
    # attachment; filename="blindbox_v1_mix.safetensors"
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    return m.group(1) if m else None

# ComfyUI 默认 models 路径
COMFY_MODELS_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Comfy-Desktop", "ComfyUI-Shared", "models"
)

MAX_RETRIES = 3
CHUNK_SIZE = 1024 * 1024  # 1MB
MAX_PARALLEL = 2  # 最多同时下载的类型数

# 启动时预填的常用标签（在标签下一行直接粘贴链接即可）
PRESET_TAGS = ["checkpoints", "diffusion_models", "loras", "vae", "text_encoders"]

# HF 设备码登录（RFC 8628，与 huggingface-cli login 同一流程；端点走镜像站）
HF_OAUTH_CLIENT_ID = "26be6b09-91c5-47da-9861-d2d2bb7a7e36"  # huggingface_hub 官方公开 client_id
OAUTH_DEVICE_URL = f"https://{MIRROR_DOMAIN}/oauth/device"
OAUTH_TOKEN_URL = f"https://{MIRROR_DOMAIN}/oauth/token"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


def scan_model_types(models_dir):
    """扫描指定目录下的所有子文件夹，作为类型下拉的预置候选
    （ComfyUI 官方类型是预定义的，直接扫当前本地基础目录即可）
    扫描失败或目录为空时，退回常用类型列表兜底"""
    types = []
    if os.path.isdir(models_dir):
        try:
            for name in sorted(os.listdir(models_dir)):
                full = os.path.join(models_dir, name)
                if os.path.isdir(full):
                    types.append(name)
        except Exception:
            pass
    # 兜底：如果扫描失败，使用默认列表
    if not types:
        types = [
            "audio_encoders",
            "background_removal",
            "checkpoints",
            "clip",
            "clip_vision",
            "configs",
            "controlnet",
            "detection",
            "diffusers",
            "diffusion_models",
            "embeddings",
            "frame_interpolation",
            "geometry_estimation",
            "gligen",
            "hypernetworks",
            "latent_upscale_models",
            "loras",
            "model_patches",
            "optical_flow",
            "photomaker",
            "style_models",
            "text_encoders",
            "unet",
            "upscale_models",
            "vae",
            "vae_approx",
        ]
    # types.append("custom")
    return types


# ============================================================
# 下载核心逻辑
# ============================================================

class NonRetryableError(RuntimeError):
    """不可重试的错误（如 403 无权限、404 不存在），重试也不会成功"""
    pass


class FileDownloader:
    """单文件下载器，支持断点续传和进度回调
    下载写入 .part 临时文件，完成后改名为最终文件（最终文件存在 = 下载完整）"""

    def __init__(self, url, filepath, progress_cb=None, log_cb=None, cancel_event=None, token=None):
        self.url = url
        self.filepath = filepath
        self.part_path = filepath + ".part"
        self.progress_cb = progress_cb
        self.log_cb = log_cb
        self.cancel_event = cancel_event or threading.Event()
        self.token = (token or "").strip()
        self.is_civitai = is_civitai_url(url)  # 两站点鉴权方式不同，严格分流
        self.resolved_filename = None  # Content-Disposition 解析出的真实文件名（CivitAI 等）

    def download(self):
        """执行下载，返回 (success: bool, skipped: bool)"""
        existing = os.path.getsize(self.part_path) if os.path.exists(self.part_path) else 0

        req = urllib.request.Request(self.url)
        req.add_header("User-Agent", BROWSER_UA)  # 过 Cloudflare 对 Python-urllib 的封禁(1010)
        if existing > 0:
            req.add_header("Range", f"bytes={existing}-")
        # 鉴权分流：HF 用 Bearer Token 头；CivitAI 用链接 ?token= 参数
        # （HF Token 绝不发给 CivitAI 镜像站，避免凭证泄漏且对方也不识别）
        if self.token and not self.is_civitai:
            req.add_header("Authorization", f"Bearer {self.token}")

        try:
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=60, context=ctx)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                # .part 已达远端大小 → 完整，改名为最终文件
                try:
                    os.replace(self.part_path, self.filepath)
                except OSError:
                    pass
                if self.log_cb:
                    self.log_cb("  文件已完整，跳过")
                return True, True
            if e.code in (401, 403):
                # 两站点分流提示：HF 为 gated 仓库；CivitAI 为需登录下载
                if self.log_cb:
                    if self.is_civitai:
                        self.log_cb(f"  {e.code} - CivitAI 拒绝访问（需登录的模型请在链接末尾追加 &token=你的APIKEY）")
                    else:
                        hint = "" if self.token else "（无权限：gated 模型先在模型页同意协议并配置 Token）"
                        self.log_cb(f"  {e.code} - 无权限访问 {hint}")
                raise NonRetryableError(f"{e.code} 无权限访问")
            if e.code == 404:
                if self.log_cb:
                    self.log_cb("  404 - 文件不存在")
                raise NonRetryableError("404 文件不存在")
            raise
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}")

        # 从重定向后的响应头解析真实文件名（CivitAI 等下载端点：原始 URL 无文件名）
        cd_name = parse_content_disposition(resp.headers)
        if cd_name:
            cd_name = os.path.basename(cd_name)  # 防路径穿越
            cur_name = os.path.basename(self.filepath)
            if cd_name and cd_name != cur_name:
                new_fp = os.path.join(os.path.dirname(self.filepath), cd_name)
                new_part = new_fp + ".part"
                # 若旧 .part 有残留续传数据，随同改名以保留断点
                if existing > 0 and os.path.exists(self.part_path):
                    try:
                        os.replace(self.part_path, new_part)
                    except OSError:
                        pass
                self.filepath = new_fp
                self.part_path = new_part
                self.resolved_filename = cd_name
                if self.log_cb:
                    self.log_cb(f"  真实文件名: {cd_name}")

        content_length = resp.getheader("Content-Length") or resp.getheader("content-length")
        total_size = int(content_length) if content_length else 0
        is_resume = (resp.code == 206)
        if is_resume:
            total_size += existing

        downloaded = existing if is_resume else 0
        mode = "ab" if is_resume else "wb"

        if self.log_cb:
            tag = "续传" if is_resume else "下载"
            size_mb = total_size / 1048576
            extra = f" (已有 {existing / 1048576:.1f} MB)" if is_resume else ""
            self.log_cb(f"  {tag} {size_mb:.1f} MB{extra}")

        t0 = time.time()
        last_log = t0

        try:
            with open(self.part_path, mode) as f:
                while True:
                    if self.cancel_event.is_set():
                        f.flush()
                        if self.log_cb:
                            self.log_cb(f"  已取消 (已下载 {downloaded / 1048576:.1f} MB)")
                        return False, False

                    data = resp.read(CHUNK_SIZE)
                    if not data:
                        break
                    f.write(data)
                    downloaded += len(data)

                    now = time.time()
                    if now - last_log >= 0.5:
                        last_log = now
                        pct = (downloaded / total_size * 100) if total_size else 0
                        speed = (downloaded / (now - t0) / 1048576) if (now > t0) else 0
                        if self.progress_cb:
                            self.progress_cb(pct, downloaded, total_size, speed)

            # 最终进度
            if self.progress_cb:
                self.progress_cb(100, downloaded, total_size, 0)

        finally:
            resp.close()

        # 下载完整 → .part 改名为最终文件
        try:
            os.replace(self.part_path, self.filepath)
        except OSError:
            pass

        if self.log_cb:
            self.log_cb("  下载完成!")
        return True, False


# ============================================================
# 主应用
# ============================================================

class HFMirrorApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("880x780")
        self.root.minsize(700, 600)

        self.busy = False
        self.cancel_event = threading.Event()
        self.cfg = self._load_config()

        # 追加下载相关状态
        self.seen_urls = set()          # 已入队的 URL（去重用）
        self.download_queue = collections.deque()  # 全局下载队列（调度器与追加共享）
        self._total_tasks = 0            # 动态任务总数
        self._scheduler_running = False  # 调度器是否在运行

        # 窗口关闭时若正在下载，先确认
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 预置类型在 _restore_config 之后按当前基础目录扫描（见 _refresh_model_types）
        self.model_types = []

        self._build_ui()
        self._restore_config()
        self._refresh_model_types()

    # ------ 配置 ------

    def _load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            data = {
                "base_dir": self.dir_var.get().strip(),
                "model_type": self.type_var.get().strip(),
                "hf_token": self.token_var.get().strip(),
            }
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _toggle_token_show(self):
        """切换 Token 明文/密文显示"""
        self._token_shown = not self._token_shown
        self.token_entry.config(show="" if self._token_shown else "*")

    def _restore_config(self):
        if self.cfg.get("base_dir"):
            self.dir_var.set(self.cfg["base_dir"])
        elif os.path.isdir(COMFY_MODELS_DIR):
            self.dir_var.set(COMFY_MODELS_DIR)
        if self.cfg.get("model_type"):
            self.type_var.set(self.cfg["model_type"])
        # Token 优先级: config.json > 环境变量 HF_TOKEN
        token = self.cfg.get("hf_token") or os.environ.get("HF_TOKEN", "")
        if token:
            self.token_var.set(token)

    # ------ HF 设备码登录（RFC 8628，与 huggingface-cli login 同一流程，走镜像站） ------

    def _hf_login(self):
        """启动设备码登录（后台线程）：浏览器授权后自动获取 Token"""
        self.btn_login.config(state=tk.DISABLED)
        self.login_status.config(text="登录中…", foreground="#888888")
        threading.Thread(target=self._do_device_login, daemon=True).start()

    def _oauth_post_json(self, url, data, _depth=0):
        """POST form 数据并返回 JSON；4xx 时解析响应体中的 JSON（OAuth pending 以 400 返回）
        - 镜像站 Cloudflare 封禁 Python-urllib UA，必须伪装浏览器 UA
        - 镜像站可能把 OAuth 端点 308 重定向到官方站，urllib 对 POST 的 308 不会自动跟随，需手动处理"""
        if _depth > 3:
            raise RuntimeError("重定向次数过多")
        payload = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("User-Agent", BROWSER_UA)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # POST 的 308/30x urllib 可能不自动跟随 → 按 Location 手动重发
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and loc:
                self._log(f"OAuth: {url} 重定向 → {loc}")
                return self._oauth_post_json(loc, data, _depth + 1)
            body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            snippet = body[:120].strip() if body.strip() else "(空响应)"
            raise RuntimeError(f"响应不是 JSON: {snippet}")

    def _do_device_login(self):
        """设备码登录主流程：请求设备码 → 开浏览器授权 → 轮询 Token"""
        cancel = threading.Event()
        try:
            info = self._oauth_post_json(OAUTH_DEVICE_URL, {"client_id": HF_OAUTH_CLIENT_ID})
        except Exception as e:
            self.root.after(0, self._login_failed, f"无法获取设备码: {e}")
            return
        device_code = info.get("device_code", "")
        user_code = info.get("user_code", "")
        verify_uri = info.get("verification_uri", "https://hf.co/oauth/device")
        expires_in = int(info.get("expires_in") or 300)
        interval = int(info.get("interval") or 5)
        if not device_code or not user_code:
            self.root.after(0, self._login_failed, f"设备码响应异常: {info}")
            return

        self._log("HF 登录: 已获取设备码，正在打开浏览器授权页…")
        try:
            webbrowser.open(verify_uri)
        except Exception:
            pass
        self.root.after(0, self._show_login_window, user_code, verify_uri, cancel)

        deadline = time.time() + expires_in
        while time.time() < deadline:
            if cancel.is_set():
                self._log("HF 登录已取消")
                self.root.after(0, self._login_cancelled)
                return
            try:
                data = self._oauth_post_json(OAUTH_TOKEN_URL, {
                    "grant_type": DEVICE_GRANT_TYPE,
                    "device_code": device_code,
                    "client_id": HF_OAUTH_CLIENT_ID,
                })
            except Exception as e:
                self._log(f"HF 登录: 网络错误 ({e})，继续等待…")
                time.sleep(interval)
                continue
            if "access_token" in data:
                self.root.after(0, self._login_success, data["access_token"])
                return
            err = data.get("error", "")
            if err == "authorization_pending":
                time.sleep(interval)
                continue
            if err == "slow_down":
                interval += 5
                time.sleep(interval)
                continue
            if err == "expired_token":
                self.root.after(0, self._login_failed, "设备码已过期（超时），请重试")
            elif err == "access_denied":
                self.root.after(0, self._login_failed, "授权被拒绝，请重试")
            else:
                self.root.after(0, self._login_failed,
                                f"OAuth 错误: {err} {data.get('error_description', '')}")
            return
        self.root.after(0, self._login_failed, "设备码已过期（超时），请重试")

    def _show_login_window(self, user_code, verify_uri, cancel):
        """弹出登录窗口：显示验证码与等待状态"""
        win = tk.Toplevel(self.root)
        win.title("HuggingFace 登录")
        win.geometry("440x250")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: cancel.set())
        self._login_win = win

        frm = ttk.Frame(win, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text="1. 已在浏览器打开 HuggingFace 授权页面").pack(anchor=tk.W)
        link = ttk.Label(frm, text=f"（未打开？点此访问: {verify_uri}）",
                         foreground="#185FA5", cursor="hand2")
        link.pack(anchor=tk.W, pady=(0, 6))
        link.bind("<Button-1>", lambda e: webbrowser.open(verify_uri))
        ttk.Label(frm, text="2. 登录后，在页面输入以下验证码：").pack(anchor=tk.W)
        ttk.Label(frm, text=user_code, font=("Consolas", 20, "bold"),
                  foreground="#534AB7").pack(pady=4)
        ttk.Label(frm, text="3. 点击页面上的 Authorize 完成授权",
                  foreground="#5F5E5A").pack(anchor=tk.W)
        self._login_state = ttk.Label(frm, text="等待授权中…", foreground="#888888")
        self._login_state.pack(pady=6)
        ttk.Button(frm, text="取消", command=lambda: cancel.set()).pack(pady=2)

    def _close_login_window(self):
        win = getattr(self, "_login_win", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        self._login_win = None

    def _login_success(self, token):
        """登录成功：Token 自动填入并保存，关闭弹窗"""
        self.token_var.set(token)
        self._save_config()
        self._close_login_window()
        self.login_status.config(text="✓ 已登录", foreground="#0a7d32")
        self.btn_login.config(state=tk.NORMAL)
        self._log("HF 登录成功，Token 已自动填入并保存")

    def _login_failed(self, msg):
        self._close_login_window()
        self.login_status.config(text="登录失败", foreground="#A32D2D")
        self.btn_login.config(state=tk.NORMAL)
        self._log(f"HF 登录失败: {msg}")

    def _login_cancelled(self):
        self._close_login_window()
        self.login_status.config(text="", foreground="#888888")
        self.btn_login.config(state=tk.NORMAL)

    # ------ UI ------

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # --- 基础目录 ---
        f_dir = ttk.LabelFrame(main, text="基础目录 (模型保存根目录；子文件夹自动作为类型候选)", padding=8)
        f_dir.pack(fill=tk.X, pady=(0, 6))
        self.dir_var = tk.StringVar()
        self.dir_entry = ttk.Entry(f_dir, textvariable=self.dir_var)
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 手动改完路径失焦后重扫子文件夹，刷新类型候选
        self.dir_entry.bind("<FocusOut>", lambda e: self._refresh_model_types())
        ttk.Button(f_dir, text="浏览…", command=self._browse_dir).pack(side=tk.LEFT, padx=(8, 0))

        # --- HF Token（gated 模型需要，如 Lightricks/LTX-2.5）---
        f_token = ttk.LabelFrame(main, text="HF Token (下载 gated 模型需要；推荐点「HF 登录」自动获取，也可手动粘贴)", padding=8)
        f_token.pack(fill=tk.X, pady=(0, 6))
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(f_token, textvariable=self.token_var, show="*")
        self.token_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 明文/密文切换，方便核对
        self._token_shown = False
        ttk.Button(f_token, text="显示", width=6, command=self._toggle_token_show).pack(side=tk.LEFT, padx=(8, 0))
        # 设备码登录：浏览器授权后自动获取 token
        self.btn_login = ttk.Button(f_token, text="HF 登录…", command=self._hf_login)
        self.btn_login.pack(side=tk.LEFT, padx=(8, 0))
        self.login_status = ttk.Label(f_token, text="", foreground="#0a7d32")
        self.login_status.pack(side=tk.LEFT, padx=(8, 0))
        # 启动时若已有 token，显示已登录
        if self.cfg.get("hf_token") or os.environ.get("HF_TOKEN"):
            self.login_status.config(text="✓ 已有 Token")

        # --- 模型类型：默认类型 + 一键插入为标签 ---
        f_type = ttk.LabelFrame(main, text="默认类型 ([*] 段使用此类型；输入三个字母筛选，Backspace 清空)", padding=8)
        f_type.pack(fill=tk.X, pady=(0, 6))
        self.type_var = tk.StringVar()
        self.type_combo = ttk.Combobox(f_type, textvariable=self.type_var, values=self.model_types)
        self.type_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 一键把当前 dropdown 值插入为 [标签] 到 URL 框光标处
        ttk.Button(f_type, text="+ 插入当前为标签", command=self._insert_current_as_tag).pack(side=tk.LEFT, padx=(8, 0))
        # 实时显示当前默认类型
        self.default_label = ttk.Label(f_type, text="")
        self.default_label.pack(side=tk.LEFT, padx=(8, 0))
        self._refresh_default_label()
        self.type_var.trace_add("write", lambda *_: self._refresh_default_label())

        # 输入时自动筛选下拉列表
        self.type_combo.bind("<KeyRelease>", self._on_type_filter)
        # 键盘操作：回车确认、Esc 关闭、上下键导航、Backspace 清空
        self.type_combo.bind("<Return>", self._on_type_return)
        self.type_combo.bind("<Escape>", self._on_type_escape)
        self.type_combo.bind("<Down>", lambda e: self._on_type_nav(1))
        self.type_combo.bind("<Up>", lambda e: self._on_type_nav(-1))
        self.type_combo.bind("<BackSpace>", self._on_type_backspace)
        # 获取焦点时不自动弹下拉（与"输入满三个字母才弹出"规则一致）
        self.type_combo.bind("<FocusIn>", self._on_type_focusin)
        # 点击列表项选中后，恢复完整类型列表
        self.type_combo.bind("<<ComboboxSelected>>", self._on_type_selected)

        # --- URL 输入 ---
        f_url = ttk.LabelFrame(main, text="HuggingFace/CivitAI 下载地址 ([*] = 默认段；[类型] = 分组；下载中可追加新链接，自动去重)", padding=8)
        f_url.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        self.url_text = scrolledtext.ScrolledText(f_url, height=8, font=("Consolas", 10), wrap=tk.WORD)
        self.url_text.pack(fill=tk.BOTH, expand=True)
        # 启动时预填常用标签模板
        if not self.url_text.get("1.0", tk.END).strip():
            self._insert_tag_template()

        # --- 按钮 ---
        f_btn = ttk.Frame(main)
        f_btn.pack(fill=tk.X, pady=(0, 6))
        self.btn_preview = ttk.Button(f_btn, text="预览地址", command=self._preview)
        self.btn_preview.pack(side=tk.LEFT)
        self.btn_download = ttk.Button(f_btn, text="开始下载", command=self._start_download)
        self.btn_download.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_append = ttk.Button(f_btn, text="追加下载", command=self._append_download, state=tk.DISABLED)
        self.btn_append.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_cancel = ttk.Button(f_btn, text="取消下载", command=self._do_cancel, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT, padx=(8, 0))
        # 清空任务随时可用：进行中/排队中/已失败的任务全部清掉
        self.btn_clear = ttk.Button(f_btn, text="清空任务", command=self._clear_tasks)
        self.btn_clear.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(f_btn, text="清空日志", command=self._clear_log).pack(side=tk.RIGHT)
        ttk.Button(f_btn, text="清空已完成", command=self._clear_done_bars).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(f_btn, text="清空下载链接", command=self._clear_url_links).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(f_btn, text="初始化标签组", command=self._reset_tag_template).pack(side=tk.RIGHT, padx=(0, 6))

        # --- 进度区（每个文件一个进度条，全局队列 2 并行） ---
        f_prog = ttk.LabelFrame(main, text="下载进度 (双任务并行，按顺序下载)", padding=8)
        f_prog.pack(fill=tk.X, pady=(0, 6))
        self.prog_area = f_prog
        self.prog_bars = {}  # mtype -> (progressbar, label)
        self._done_bars = set()  # 已到终态（完成/失败/取消/已存在）的 bar_key

        # --- 日志（高度固定，不随窗口拉伸） ---
        f_log = ttk.LabelFrame(main, text="日志", padding=8)
        f_log.pack(fill=tk.X)
        self.log_text = scrolledtext.ScrolledText(f_log, height=10, font=("Consolas", 9), wrap=tk.WORD)
        self.log_text.pack(fill=tk.X)
        self.log_text.config(state=tk.DISABLED)

    # ------ 辅助方法 ------

    def _browse_dir(self):
        d = filedialog.askdirectory(title="选择基础目录")
        if d:
            self.dir_var.set(d)
            # 目录变了 → 重扫子文件夹，刷新类型下拉候选
            self._refresh_model_types()

    def _refresh_model_types(self):
        """扫描当前基础目录下所有子文件夹作为类型下拉的全量预置"""
        base = self.dir_var.get().strip()
        self.model_types = scan_model_types(base)
        if hasattr(self, "type_combo"):
            self.type_combo["values"] = self.model_types

    def _on_type_filter(self, event):
        """输入字母时自动筛选并弹出下拉列表"""
        # 忽略导航键（已单独绑定处理）
        if event.keysym in ("Up", "Down", "Return", "Tab", "Escape",
                            "Shift_L", "Shift_R", "Control_L", "Control_R",
                            "Left", "Right", "Home", "End"):
            return
        typed = self.type_var.get().strip().lower()
        if not typed:
            # 输入为空时恢复完整列表并收起下拉
            self.type_combo["values"] = self.model_types
            self._unpost_dropdown()
            return
        # 模糊匹配：包含输入子串的类型
        filtered = [t for t in self.model_types if typed in t.lower()]
        self.type_combo["values"] = filtered
        # 输入满三个字母才弹出下拉（弹出会抢焦点，先静默筛选）
        if filtered and len(typed) >= 3:
            # 延迟到当前按键事件处理完毕后再弹出，避免焦点竞争
            self.type_combo.after(0, self._post_and_focus)
        else:
            self._unpost_dropdown(keep_filter=True)

    def _popdown_listbox(self):
        """获取下拉弹出列表的 listbox 路径（用于高亮/导航）"""
        popdown = self.type_combo.tk.call(
            "ttk::combobox::PopdownWindow", self.type_combo)
        return str(popdown) + ".f.l"

    def _post_and_focus(self, active_idx=0):
        """弹出下拉列表，高亮匹配项，并把焦点强制还给输入框"""
        try:
            self.type_combo.tk.call("ttk::combobox::Post", self.type_combo)
            lb = self._popdown_listbox()
            values = self.type_combo["values"]
            if values:
                idx = max(0, min(int(active_idx), len(values) - 1))
                self.type_combo.tk.call(lb, "selection", "clear", 0, "end")
                self.type_combo.tk.call(lb, "activate", idx)
                self.type_combo.tk.call(lb, "selection", "set", idx)
                self.type_combo.tk.call(lb, "see", idx)
        except Exception:
            try:
                self.type_combo.event_generate("<Down>")
            except Exception:
                pass
        # 关键：弹出动作会把焦点转给下拉列表，必须用 focus_force 夺回
        self.type_combo.focus_force()
        self.type_combo.icursor(tk.END)

    def _unpost_dropdown(self, keep_filter=False):
        """收起下拉列表，焦点回到输入框（不改动当前筛选结果）"""
        try:
            self.type_combo.tk.call("ttk::combobox::Unpost", self.type_combo)
        except Exception:
            pass
        self.type_combo.focus_set()

    def _on_type_nav(self, delta):
        """上下键在下拉列表中移动高亮，焦点保持在输入框"""
        values = self.type_combo["values"]
        if not values:
            return "break"
        cur_raw = ""
        try:
            lb = self._popdown_listbox()
            cur_raw = str(self.type_combo.tk.call(lb, "index", "active"))
        except Exception:
            pass
        if cur_raw == "" or cur_raw == "-1":
            idx = 0
        else:
            idx = max(0, min(int(cur_raw) + delta, len(values) - 1))
        self._post_and_focus(idx)
        return "break"  # 阻止默认行为（默认 Down 会重新弹出并抢走焦点）

    def _on_type_return(self, event):
        """回车：选中下拉中高亮的项（或第一项）"""
        values = self.type_combo["values"]
        if values:
            idx = 0
            try:
                lb = self._popdown_listbox()
                cur = str(self.type_combo.tk.call(lb, "index", "active"))
                if cur not in ("", "-1") and int(cur) < len(values):
                    idx = int(cur)
            except Exception:
                pass
            self.type_var.set(values[idx])
        self._unpost_dropdown()
        self.type_combo["values"] = self.model_types
        return "break"

    def _on_type_backspace(self, event):
        """Backspace：一次性清空输入框并收起下拉"""
        self.type_var.set("")
        self._unpost_dropdown()
        self.type_combo["values"] = self.model_types
        return "break"

    def _on_type_focusin(self, event):
        """获取焦点时不自动弹出下拉（除非已输入满三个字母的筛选词）"""
        typed = self.type_var.get().strip()
        if len(typed) < 3:
            # 延迟收起，压过主题默认的焦点弹出行为
            self.type_combo.after(10, self._unpost_dropdown)

    def _on_type_escape(self, event):
        """Esc：收起下拉"""
        self._unpost_dropdown()
        return "break"

    def _on_type_selected(self, event=None):
        """点击列表项选中后，恢复完整类型列表"""
        self.type_combo["values"] = self.model_types

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: self._log_threadsafe(f"[{ts}] {msg}"))

    def _log_threadsafe(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _insert_tag_template(self):
        """向 URL 输入框插入常用标签模板（[*] 默认段 + 常用类型标签，标签之间空两行）"""
        parts = ["[*]"]
        parts += [f"[{t}]" for t in PRESET_TAGS]
        template = "\n\n\n".join(parts) + "\n"
        self.url_text.delete("1.0", tk.END)
        self.url_text.insert("1.0", template)

    def _reset_tag_template(self):
        """清空输入框并重新插入标签模板"""
        self._insert_tag_template()
        self._log("已重置标签模板")

    def _clear_url_links(self):
        """清空下载地址框中手动粘贴的链接，仅保留 [标签] 行（如 [loras]）
        与 _parse_groups 的标签识别规则一致：标签须独占一行"""
        raw = self.url_text.get("1.0", tk.END)
        tags = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]") and len(line) > 2:
                tags.append(line)
        # 保留标签行，标签之间空两行（与标签模板格式一致）
        content = ("\n\n\n".join(tags) + "\n") if tags else ""
        self.url_text.delete("1.0", tk.END)
        self.url_text.insert("1.0", content)
        self._log(f"已清空下载链接，保留 {len(tags)} 个标签")

    def _refresh_default_label(self):
        """实时刷新「当前默认类型」标签"""
        t = self.type_var.get().strip() or "(未选)"
        self.default_label.config(text=f"→ 当前默认: [{t}]")

    def _insert_current_as_tag(self):
        """把 dropdown 当前值作为 [标签] 插入到 URL 输入框光标处"""
        t = self.type_var.get().strip()
        if not t:
            messagebox.showwarning("提示", "请先在下拉框选择或输入一个类型")
            return
        # 恢复完整类型列表（防止下拉列表被筛选过）
        self.type_combo["values"] = self.model_types
        # 在光标处插入标签，前后各空两行，保证与相邻标签之间有两行间隔
        self.url_text.insert(tk.INSERT, f"\n\n[{t}]\n\n\n")
        # 收起可能弹出的下拉
        try:
            self.type_combo.tk.call("ttk::combobox::Unpost", self.type_combo)
        except Exception:
            pass
        self.url_text.focus_set()
        self._log(f"已插入 [{t}] 标签到光标处（前后各空两行）")

    def _parse_groups(self):
        """解析 URL 文本：
        - [类型] 标签分组：归入对应类型
        - [*] 重置标记：current 重置为下拉框默认类型
        - 未分组的链接：跟随最近的 current（首次使用下拉框默认）
        """
        default_type = self.type_var.get().strip() or "custom"
        groups = {}
        order = []
        current = default_type
        raw = self.url_text.get("1.0", tk.END)
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]") and len(line) > 2:
                t = line[1:-1].strip()
                # [*] 重置标记：把 current 重置为下拉框默认类型
                if t == "*":
                    current = default_type
                    continue
                if t:
                    current = t
                    if t not in groups:
                        groups[t] = []
                        order.append(t)
                continue
            if current not in groups:
                groups[current] = []
                order.append(current)
            groups[current].append(line)
        return [(t, groups[t]) for t in order if groups[t]]

    def _normalize_civitai_url(self, url):
        """CivitAI 专用：模型页 URL → API 下载端点
        页面地址（/models/93152）不是下载链接，直接请求会被拒绝；
        真实下载端点是 /api/download/models/{modelId}，规则：
        https://civitai.com/models/93152?...       → /api/download/models/93152?...
        https://civitai.com/models/93152/98765?... → /api/download/models/93152?versionId=98765&...
        已是 /api/download/ 开头的地址原样返回，不做任何改动"""
        m = re.match(
            r"(https?://(?:www\.)?civitai\.(?:com|red))/models/(\d+)(?:/(\d+))?/?(\\?.*)?$",
            url,
        )
        if not m:
            return url
        base, mid, vid, q = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        params = urllib.parse.parse_qsl(q.lstrip("?"))
        if vid:
            params = [("versionId", vid)] + [(k, v) for k, v in params if k != "versionId"]
        qs = urllib.parse.urlencode(params)
        return f"{base}/api/download/models/{mid}" + (f"?{qs}" if qs else "")

    def _convert_civitai_url(self, url):
        """CivitAI 专用链路：仅做页面地址 → /api/download 下载端点的转换
        不做镜像转换（镜像的 307 仍会跳回 civitai.com 官方 CDN，需直连）；
        与 HF 逻辑无任何耦合；链接上的 ?token= 等参数原样透传"""
        return self._normalize_civitai_url(url)

    def _convert_hf_url(self, url):
        """HF 专用链路：官方域名 → 镜像域名；浏览页 /blob/ → 下载地址 /resolve/"""
        for domain, mirror in MIRROR_DOMAIN_MAP.items():
            url = url.replace(f"https://{domain}/", f"https://{mirror}/")
            url = url.replace(f"http://{domain}/", f"http://{mirror}/")
        url = url.replace("/blob/main/", "/resolve/main/")
        return url

    def _convert_url(self, url):
        """URL 转换总入口：按站点分流，CivitAI 与 HuggingFace 互不混同"""
        if is_civitai_url(url):
            return self._convert_civitai_url(url)
        return self._convert_hf_url(url)

    def _get_filename(self, url):
        parsed = urllib.parse.urlparse(url)
        name = os.path.basename(urllib.parse.unquote(parsed.path))
        return name if name else "download_file"

    def _set_group_progress(self, mtype, pct, downloaded, total, speed):
        """更新某一分组的进度条"""
        entry = self.prog_bars.get(mtype)
        if not entry:
            return
        bar, lbl = entry
        bar["value"] = pct
        dl_mb = downloaded / 1048576
        total_mb = total / 1048576 if total else 0
        if total:
            lbl.config(text=f"{dl_mb:.1f}/{total_mb:.1f}MB {pct:.0f}% {speed:.1f}MB/s")
        else:
            lbl.config(text=f"{dl_mb:.1f}MB {speed:.1f}MB/s")

    def _make_group_bar(self, mtype):
        """为分组创建一行进度条（标签固定宽，进度条撑满剩余空间）"""
        row = ttk.Frame(self.prog_area)
        row.pack(fill=tk.X, pady=2)
        # 标签固定 44 字符宽，显示完整类型/文件名
        ttk.Label(row, text=mtype, width=44, anchor=tk.W).pack(side=tk.LEFT)
        # 进度条撑满标签和状态文字之间的剩余空间
        bar = ttk.Progressbar(row)
        bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        # 状态文字固定 36 字符宽
        lbl = ttk.Label(row, text="等待中", width=36, anchor=tk.W)
        lbl.pack(side=tk.LEFT)
        self.prog_bars[mtype] = (bar, lbl)

    def _mark_bar_done(self, bar_key, text, full=True):
        """把某进度条标记为终态（完成/已存在 → 100%；失败/取消 → 保持当前进度）"""
        self._done_bars.add(bar_key)
        entry = self.prog_bars.get(bar_key)
        if not entry:
            return
        bar, lbl = entry
        if full:
            bar["value"] = 100
        lbl.config(text=text)

    def _clear_done_bars(self):
        """清除已到终态（完成/失败/取消/已存在）的进度条行"""
        if not self._done_bars:
            return
        for bar_key in list(self._done_bars):
            entry = self.prog_bars.get(bar_key)
            if not entry:
                self._done_bars.discard(bar_key)
                continue
            bar, _ = entry
            bar.master.destroy()  # 销毁整行
            del self.prog_bars[bar_key]
            self._done_bars.discard(bar_key)

    def _clear_group_bars(self):
        """清空所有分组进度条"""
        for w in self.prog_area.winfo_children():
            w.destroy()
        self.prog_bars = {}
        self._done_bars = set()

    # ------ 预览 ------

    def _preview(self):
        groups = self._parse_groups()
        if not groups:
            messagebox.showwarning("提示", "请输入至少一个下载地址")
            return
        base = self.dir_var.get().strip()
        total = sum(len(u) for _, u in groups)
        self._log(f"=== 预览: {len(groups)} 组 / {total} 个文件 ===")
        for t, urls in groups:
            out = os.path.join(base, t) if base else "(未设置目录)"
            self._log(f"[{t}] {len(urls)} 个文件 → {out}")
            for i, u in enumerate(urls, 1):
                mirror = self._convert_url(u)
                self._log(f"  {i}. {self._get_filename(mirror)}  ←  {mirror}")
        self._log("=" * 60)

    # ------ 下载 ------

    def _start_download(self):
        if self.busy:
            return
        base = self.dir_var.get().strip()
        groups = self._parse_groups()

        if not base:
            messagebox.showwarning("提示", "请选择基础目录")
            return
        if not groups:
            messagebox.showwarning("提示", "请输入至少一个下载地址（可用 [类型] 标签分组）")
            return

        self._save_config()
        self.busy = True
        self.cancel_event.clear()
        self.btn_download.config(state=tk.DISABLED)
        self.btn_preview.config(state=tk.DISABLED)
        self.btn_append.config(state=tk.NORMAL)
        self.btn_cancel.config(state=tk.NORMAL)

        # 初始化去重集合和任务计数（同 URL 只入队一次，避免重复进度条 / 重复下载）
        self.seen_urls = set()

        # 创建目录 + 进度条；本地已存在的文件不入队，进度条直接显示"已存在，跳过"
        skipped = 0
        filtered_groups = []
        self._clear_group_bars()
        for mtype, urls in groups:
            out_dir = os.path.join(base, mtype)
            if os.path.exists(out_dir):
                self._log(f"[{mtype}] 文件夹已存在: {out_dir}")
            else:
                os.makedirs(out_dir)
                self._log(f"[{mtype}] 已创建文件夹: {out_dir}")
            kept = []
            for url in urls:
                if url in self.seen_urls:
                    continue  # 同一 URL 在更早的组里已出现，跳过
                self.seen_urls.add(url)
                mirror = self._convert_url(url)
                fname = self._get_filename(mirror)
                bar_key = f"{mtype}/{fname}"
                # 同 mtype 内同 URL 多次粘贴 → 共用同一个进度条行，不重复创建
                if bar_key not in self.prog_bars:
                    self._make_group_bar(bar_key)
                if os.path.exists(os.path.join(out_dir, fname)):
                    self._mark_bar_done(bar_key, "已存在，跳过")
                    skipped += 1
                else:
                    kept.append(url)
            if kept:
                filtered_groups.append((mtype, kept))

        self._total_tasks = sum(len(u) for _, u in filtered_groups)

        if not filtered_groups:
            self._log(f"全部 {skipped} 个文件均已存在，无需下载")
            self.busy = False
            self.btn_download.config(state=tk.NORMAL)
            self.btn_preview.config(state=tk.NORMAL)
            self.btn_append.config(state=tk.DISABLED)
            self.btn_cancel.config(state=tk.DISABLED)
            return

        total = self._total_tasks
        self._log(f"开始下载: {len(filtered_groups)} 组, 共 {total} 个文件"
                  + (f", 跳过 {skipped} 个已存在" if skipped else "")
                  + ", 最多 2 个并行")

        # 启动调度线程
        threading.Thread(target=self._run_scheduler, args=(filtered_groups, base), daemon=True).start()

    def _do_cancel(self):
        self.cancel_event.set()
        self._log("正在取消全部下载…")

    def _clear_tasks(self):
        """清空任务队列：随时可用，无论任务是排队中、下载中还是已失败
        - 进行中的下载被中止（已写入的 .part 保留，重新下载可续传）
        - 等待队列、任务计数、去重集合、进度条全部清空
        - 调度器随后自行收尾并恢复按钮状态"""
        if self.busy:
            ans = messagebox.askyesno(
                "确认清空",
                "进行中的任务将被中止（已下载部分保留 .part，可续传），\n"
                "等待中的任务将被丢弃。确定清空全部任务吗？",
            )
            if not ans:
                return
            self.cancel_event.set()  # 停止调度器循环 + 中止进行中的下载
        pending = len(self.download_queue)
        self.download_queue.clear()
        self.seen_urls.clear()
        self._total_tasks = 0
        self._clear_group_bars()  # 清掉所有进度条（含失败/取消的）
        msg = "任务已全部清空"
        if pending:
            msg += f"（丢弃 {pending} 个等待中的任务）"
        if self.busy:
            msg += "，正在中止进行中的下载…"
        self._log(msg)

    def _append_download(self):
        """追加下载：解析文本框，对已入队的 URL 去重，仅追加新链接到调度队列"""
        if not self.busy or not self._scheduler_running:
            self._log("下载已结束，请点击「开始下载」重新启动")
            return

        base = self.dir_var.get().strip()
        groups = self._parse_groups()
        if not groups:
            self._log("没有可追加的链接")
            return

        new_count = 0
        skipped = 0
        for mtype, urls in groups:
            out_dir = os.path.join(base, mtype)
            for url in urls:
                if url in self.seen_urls:
                    continue
                self.seen_urls.add(url)
                mirror = self._convert_url(url)
                fname = self._get_filename(mirror)
                bar_key = f"{mtype}/{fname}"
                # 本地已存在 → 进度条直接显示完成，不入队
                if os.path.exists(os.path.join(out_dir, fname)):
                    if bar_key not in self.prog_bars:
                        self._make_group_bar(bar_key)
                    self._mark_bar_done(bar_key, "已存在，跳过")
                    self._log(f"[{mtype}] 已存在，跳过: {fname}")
                    skipped += 1
                    continue
                # 追加到全局下载队列（deque 线程安全）
                self.download_queue.append((url, out_dir, mtype, bar_key))
                # 创建目录（如不存在）
                if not os.path.exists(out_dir):
                    os.makedirs(out_dir)
                    self._log(f"[{mtype}] 已创建文件夹: {out_dir}")
                # 创建进度条（同名 bar 共用一行，不重复创建）
                if bar_key not in self.prog_bars:
                    self._make_group_bar(bar_key)
                new_count += 1

        self._total_tasks += new_count
        if new_count:
            msg = f"追加 {new_count} 个文件到下载队列 (总计 {self._total_tasks})"
            if skipped:
                msg += f", 跳过 {skipped} 个已存在"
            self._log(msg)
        elif skipped:
            self._log(f"追加的链接均已存在本地，跳过 {skipped} 个")
        else:
            self._log("没有新链接（已去重）")

    # ---- 单文件下载（被调度器调用） ----

    def _repo_page_url(self, url):
        """从文件下载 URL 提取仓库页面地址（gated 模型打开 Agree 协议页用）"""
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2:
            return f"https://huggingface.co/{parts[0]}/{parts[1]}"
        return None

    def _handle_gated_403(self, url, mirror_url):
        """403 无权限处理（按站点分流，互不混同）
        CivitAI → 提示补 API Token；HF gated → 打开协议页让用户点 Agree"""
        if is_civitai_url(mirror_url):
            # CivitAI 403：多为需要登录才能下载的模型
            self._log("  → CivitAI 403：该模型可能需要登录后才能下载")
            self._log("     到 civitai.com → 头像 → Account Settings → API Keys 生成 Key，")
            self._log("     在链接末尾追加 &token=你的Key，再点「追加下载」重试")
            self.seen_urls.discard(url)
            return
        token = self.token_var.get().strip()
        if not token:
            self._log("  → 这是 gated 模型，请点「HF 登录…」完成登录后重试")
            return
        page = self._repo_page_url(mirror_url)
        if page:
            self._log("  → 正在打开仓库页面，点击 Agree 同意协议后，点「追加下载」重新下载")
            try:
                webbrowser.open(page)
            except Exception:
                self._log(f"  → 请手动打开: {page}")
        # 从去重集合移除：用户同意协议后，「追加下载」可重新入队该链接
        self.seen_urls.discard(url)

    def _download_one_file(self, url, out_dir, mtype, bar_key):
        """下载单个文件，返回 (success, filename)"""
        mirror_url = self._convert_url(url)
        filename = self._get_filename(mirror_url)
        filepath = os.path.join(out_dir, filename)
        prefix = f"[{mtype}]"
        self._log(f"{prefix} 开始: {filename}")

        success = False
        civitai = is_civitai_url(mirror_url)
        for attempt in range(MAX_RETRIES):
            if self.cancel_event.is_set():
                break
            try:
                downloader = FileDownloader(
                    mirror_url, filepath,
                    progress_cb=lambda p, d, t2, s, bk=bar_key: self.root.after(
                        0, self._set_group_progress, bk, p, d, t2, s),
                    log_cb=self._log,
                    cancel_event=self.cancel_event,
                    # HF Token 只用于 HF 链路；CivitAI 鉴权走链接 ?token= 参数
                    token=None if civitai else self.token_var.get().strip(),
                )
                success, _ = downloader.download()
                # CivitAI 等端点的真实文件名在下载时才解析得到
                if downloader.resolved_filename:
                    filename = downloader.resolved_filename
                if success:
                    break
            except NonRetryableError as e:
                # 401/403/404 等错误重试也不会成功，直接失败
                self._log(f"{prefix}   失败: {e}")
                if "401" in str(e) or "403" in str(e):
                    self._handle_gated_403(url, mirror_url)
                break
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    self._log(f"{prefix}   出错 ({e}), {attempt + 2}/{MAX_RETRIES} 重试…")
                    time.sleep(2)
                else:
                    self._log(f"{prefix}   最终失败: {e}")
        return success, filename

    # ---- 统一调度器：全局队列始终 2 并行 + 支持动态追加 ----

    def _run_scheduler(self, groups, base):
        """调度线程：始终保持最多 2 个文件同时下载，任务按输入顺序从全局队列依次取
        支持运行中通过 _append_download 追加新任务到 self.download_queue"""
        self._scheduler_running = True

        # 构建全局下载队列（存为实例属性，追加下载共享访问），按文本框出现顺序入队
        self.download_queue = collections.deque()
        for mtype, urls in groups:
            out_dir = os.path.join(base, mtype)
            for url in urls:
                mirror = self._convert_url(url)
                fname = self._get_filename(mirror)
                bar_key = f"{mtype}/{fname}"
                self.download_queue.append((url, out_dir, mtype, bar_key))

        pool = ThreadPoolExecutor(max_workers=2)
        futures = {}  # future -> (mtype, bar_key)
        completed = 0

        def pick_next():
            """取下一个任务：从全局队列头部取
            deque.popleft 线程安全；用 try-except 防 race"""
            try:
                return self.download_queue.popleft()
            except IndexError:
                return None

        # 初始填充：最多 2 个
        for _ in range(min(2, self._total_tasks)):
            task = pick_next()
            if not task:
                break
            url, out_dir, mt, bar_key = task
            fut = pool.submit(self._download_one_file, url, out_dir, mt, bar_key)
            futures[fut] = (mt, bar_key)

        # 事件循环：futures 有任务 或 队列有追加任务 → 继续跑
        while futures or self.download_queue:
            if self.cancel_event.is_set():
                break

            # 队列有任务但无运行中的 future（追加场景：调度器恰好空窗）
            if not futures:
                task = pick_next()
                if task:
                    url, out_dir, mt2, bar_key = task
                    fut = pool.submit(self._download_one_file, url, out_dir, mt2, bar_key)
                    futures[fut] = (mt2, bar_key)
                else:
                    time.sleep(0.1)  # race：队列瞬时空，短暂等待
                continue

            done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED, timeout=0.5)
            for fut in done:
                mt, bar_key = futures.pop(fut)
                try:
                    success, fname = fut.result()
                    completed += 1
                    if self.cancel_event.is_set():
                        self._log(f"[{mt}] 已取消: {fname}")
                        self.root.after(0, self._mark_bar_done, bar_key, "已取消", False)
                    elif success:
                        self._log(f"[{mt}] 完成: {fname}")
                        self.root.after(0, self._mark_bar_done, bar_key, "完成")
                    else:
                        self._log(f"[{mt}] 失败: {fname}")
                        self.root.after(0, self._mark_bar_done, bar_key, "失败", False)
                except Exception as e:
                    self._log(f"[{mt}] 异常: {e}")
                    completed += 1
                    self.root.after(0, self._mark_bar_done, bar_key, "异常", False)

                # 取下一个任务（全局队列，按输入顺序）
                if not self.cancel_event.is_set():
                    task = pick_next()
                    if task:
                        url, out_dir, mt2, bar_key = task
                        fut = pool.submit(self._download_one_file, url, out_dir, mt2, bar_key)
                        futures[fut] = (mt2, bar_key)

        self._scheduler_running = False
        pool.shutdown(wait=True)
        self._log(f"\n{'=' * 60}")
        self._log(f"全部结束: {completed}/{self._total_tasks} 完成")
        self.root.after(0, self._finish_download)

    def _finish_download(self):
        self.busy = False
        self.btn_download.config(state=tk.NORMAL)
        self.btn_preview.config(state=tk.NORMAL)
        self.btn_append.config(state=tk.DISABLED)
        self.btn_cancel.config(state=tk.DISABLED)

    def _on_close(self):
        """关闭窗口：正在下载时弹确认，避免文件半截"""
        if self.busy:
            ans = messagebox.askyesno(
                "确认退出",
                "正在下载中，退出会导致文件不完整。\n确定要退出吗？",
            )
            if not ans:
                return
            self.cancel_event.set()
        self._save_config()
        self.root.destroy()


# ============================================================
# 入口
# ============================================================

def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass
    HFMirrorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
