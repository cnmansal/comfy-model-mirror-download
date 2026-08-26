#!/usr/bin/env python3
"""
HF镜像下载器 v1.0 (任务队列版)
自动将 HuggingFace 下载地址转换为国内镜像地址并下载到指定文件夹
UI 采用「添加任务 → 任务队列」模式，多任务并行下载
"""

import os
import json
import time
import threading
import ssl
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

APP_TITLE = "HF镜像下载器 v1.0 (任务队列版)"
MIRROR_DOMAIN = "hf-mirror.com"
HF_DOMAINS = ["huggingface.co", "hf.co"]
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

COMFY_MODELS_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Comfy-Desktop", "ComfyUI-Shared", "models"
)

MAX_RETRIES = 3
CHUNK_SIZE = 1024 * 1024  # 1MB


def scan_model_types(models_dir):
    """扫描 ComfyUI models 目录下的所有子文件夹，作为预置类型"""
    types = []
    if os.path.isdir(models_dir):
        try:
            for name in sorted(os.listdir(models_dir)):
                full = os.path.join(models_dir, name)
                if os.path.isdir(full):
                    types.append(name)
        except Exception:
            pass
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
    #types.append("custom")
    return types


class FileDownloader:
    """单文件下载器，支持断点续传和进度回调"""

    def __init__(self, url, filepath, progress_cb=None, log_cb=None, cancel_event=None):
        self.url = url
        self.filepath = filepath
        self.progress_cb = progress_cb
        self.log_cb = log_cb
        self.cancel_event = cancel_event or threading.Event()

    def download(self):
        existing = os.path.getsize(self.filepath) if os.path.exists(self.filepath) else 0

        req = urllib.request.Request(self.url)
        if existing > 0:
            req.add_header("Range", f"bytes={existing}-")

        try:
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=60, context=ctx)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                if self.log_cb:
                    self.log_cb("  文件已完整，跳过")
                return True, True
            if e.code == 404:
                if self.log_cb:
                    self.log_cb("  404 - 文件不存在")
                return False, False
            raise
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}")

        content_length = resp.getheader("Content-Length") or resp.getheader("content-length")
        total_size = int(content_length) if content_length else 0
        is_resume = (resp.code == 206)
        if is_resume:
            total_size += existing

        downloaded = existing if is_resume else 0
        mode = "ab" if is_resume else "wb"

        t0 = time.time()
        last_cb = t0

        try:
            with open(self.filepath, mode) as f:
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
                    if now - last_cb >= 0.5:
                        last_cb = now
                        pct = (downloaded / total_size * 100) if total_size else 0
                        speed = (downloaded / (now - t0) / 1048576) if (now > t0) else 0
                        if self.progress_cb:
                            self.progress_cb(pct, downloaded, total_size, speed)

            if self.progress_cb:
                self.progress_cb(100, downloaded, total_size, 0)
        finally:
            resp.close()

        return True, False


class TaskQueueApp:
    """任务队列版主应用"""

    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("920x680")
        self.root.minsize(760, 560)

        self.tasks = []            # [{iid, type, urls, status}]
        self.running_tasks = 0
        self.busy = False
        self.cancel_event = threading.Event()
        self.cfg = self._load_config()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.model_types = scan_model_types(COMFY_MODELS_DIR)

        self._build_ui()
        self._restore_config()

    # ------ 配置 ------

    def _load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"base_dir": self.dir_var.get().strip()}, f,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _restore_config(self):
        if self.cfg.get("base_dir"):
            self.dir_var.set(self.cfg["base_dir"])
        elif os.path.isdir(COMFY_MODELS_DIR):
            self.dir_var.set(COMFY_MODELS_DIR)

    # ------ UI ------

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # --- 基础目录 ---
        f_dir = ttk.LabelFrame(main, text="基础目录 (ComfyUI models 路径)", padding=8)
        f_dir.pack(fill=tk.X, pady=(0, 6))
        self.dir_var = tk.StringVar()
        ttk.Entry(f_dir, textvariable=self.dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f_dir, text="浏览…", command=self._browse_dir).pack(side=tk.LEFT, padx=(8, 0))

        # --- 添加任务 ---
        f_add = ttk.LabelFrame(main, text="添加任务", padding=8)
        f_add.pack(fill=tk.X, pady=(0, 6))

        row1 = ttk.Frame(f_add)
        row1.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row1, text="类型:").pack(side=tk.LEFT)
        self.type_var = tk.StringVar()
        self.type_combo = ttk.Combobox(row1, textvariable=self.type_var,
                                       values=self.model_types, width=24)
        self.type_combo.pack(side=tk.LEFT, padx=(6, 12))
        self.type_combo.bind("<KeyRelease>", self._on_type_filter)
        ttk.Label(row1, text="→ 保存到 {基础目录}/{类型}/").pack(side=tk.LEFT)

        row2 = ttk.Frame(f_add)
        row2.pack(fill=tk.X)
        ttk.Label(row2, text="地址:").pack(side=tk.LEFT, anchor=tk.N, pady=(4, 0))
        self.url_text = scrolledtext.ScrolledText(row2, height=4,
                                                  font=("Consolas", 10), wrap=tk.WORD)
        self.url_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        row3 = ttk.Frame(f_add)
        row3.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row3, text="＋ 添加到队列", command=self._add_task).pack(side=tk.LEFT)
        ttk.Button(row3, text="清空输入", command=lambda: self.url_text.delete("1.0", tk.END)).pack(
            side=tk.LEFT, padx=(8, 0))

        # --- 任务队列 ---
        f_queue = ttk.LabelFrame(main, text="任务队列 (任务间并行下载)", padding=8)
        f_queue.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        cols = ("type", "count", "progress", "status")
        self.tree = ttk.Treeview(f_queue, columns=cols, show="headings", height=6)
        self.tree.heading("type", text="类型")
        self.tree.heading("count", text="文件数")
        self.tree.heading("progress", text="当前进度")
        self.tree.heading("status", text="状态")
        self.tree.column("type", width=140, anchor=tk.W)
        self.tree.column("count", width=60, anchor=tk.CENTER)
        self.tree.column("progress", width=380, anchor=tk.W)
        self.tree.column("status", width=120, anchor=tk.W)
        sb = ttk.Scrollbar(f_queue, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)

        row4 = ttk.Frame(main)
        row4.pack(fill=tk.X, pady=(0, 6))
        self.btn_start = ttk.Button(row4, text="▶ 全部开始", command=self._start_all)
        self.btn_start.pack(side=tk.LEFT)
        self.btn_cancel = ttk.Button(row4, text="取消全部", command=self._do_cancel,
                                     state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(row4, text="删除选中任务", command=self._remove_selected).pack(
            side=tk.LEFT, padx=(8, 0))
        ttk.Button(row4, text="清空队列", command=self._clear_queue).pack(side=tk.LEFT)
        ttk.Button(row4, text="清空日志", command=self._clear_log).pack(side=tk.RIGHT)

        # --- 日志 ---
        f_log = ttk.LabelFrame(main, text="日志", padding=8)
        f_log.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(f_log, height=8,
                                                  font=("Consolas", 9), wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

    # ------ 辅助 ------

    def _browse_dir(self):
        d = filedialog.askdirectory(title="选择基础目录")
        if d:
            self.dir_var.set(d)

    def _on_type_filter(self, event):
        if event.keysym in ("Up", "Down", "Return", "Tab", "Escape",
                            "Left", "Right", "Home", "End", "Shift_L", "Shift_R"):
            return
        typed = self.type_var.get().strip().lower()
        if not typed:
            self.type_combo["values"] = self.model_types
            return
        self.type_combo["values"] = [t for t in self.model_types if typed in t.lower()]

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: self._log_ts(f"[{ts}] {msg}"))

    def _log_ts(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _convert_url(self, url):
        for domain in HF_DOMAINS:
            url = url.replace(f"https://{domain}/", f"https://{MIRROR_DOMAIN}/")
            url = url.replace(f"http://{domain}/", f"http://{MIRROR_DOMAIN}/")
        url = url.replace("/blob/main/", "/resolve/main/")
        return url

    def _get_filename(self, url):
        parsed = urllib.parse.urlparse(url)
        name = os.path.basename(urllib.parse.unquote(parsed.path))
        return name if name else "download_file"

    def _set_row(self, iid, progress=None, status=None):
        def upd():
            vals = list(self.tree.item(iid, "values"))
            if progress is not None:
                vals[2] = progress
            if status is not None:
                vals[3] = status
            self.tree.item(iid, values=vals)
        self.root.after(0, upd)

    # ------ 任务管理 ------

    def _add_task(self):
        mtype = self.type_var.get().strip()
        raw = self.url_text.get("1.0", tk.END).strip()
        urls = [l.strip() for l in raw.splitlines() if l.strip()]
        if not mtype:
            messagebox.showwarning("提示", "请选择模型类型")
            return
        if not urls:
            messagebox.showwarning("提示", "请输入至少一个下载地址")
            return
        iid = self.tree.insert("", tk.END, values=(mtype, len(urls), "—", "等待中"))
        self.tasks.append({"iid": iid, "type": mtype, "urls": urls, "status": "等待中"})
        self._log(f"已添加任务 [{mtype}] {len(urls)} 个文件")
        self.url_text.delete("1.0", tk.END)

    def _remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在队列中选中一个任务")
            return
        for iid in sel:
            task = next((t for t in self.tasks if t["iid"] == iid), None)
            if task and task["status"] in ("下载中",):
                messagebox.showwarning("提示", "该任务正在下载，无法删除")
                continue
            self.tree.delete(iid)
            if task:
                self.tasks.remove(task)

    def _clear_queue(self):
        if any(t["status"] == "下载中" for t in self.tasks):
            messagebox.showwarning("提示", "有任务正在下载，请先取消")
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.tasks = []

    # ------ 下载 ------

    def _start_all(self):
        if self.busy:
            return
        base = self.dir_var.get().strip()
        if not base:
            messagebox.showwarning("提示", "请选择基础目录")
            return
        pending = [t for t in self.tasks if t["status"] in ("等待中", "已取消", "失败")]
        if not pending:
            messagebox.showinfo("提示", "没有可下载的任务，请先添加")
            return

        self._save_config()
        self.busy = True
        self.cancel_event.clear()
        self.btn_cancel.config(state=tk.NORMAL)

        self._log(f"开始下载: {len(pending)} 个任务并行")
        self.running_tasks = len(pending)
        for task in pending:
            task["status"] = "下载中"
            self._set_row(task["iid"], status="下载中")
            threading.Thread(target=self._run_task, args=(task, base), daemon=True).start()

    def _do_cancel(self):
        self.cancel_event.set()
        self._log("正在取消全部下载…")

    def _run_task(self, task, base):
        mtype = task["type"]
        urls = task["urls"]
        iid = task["iid"]
        prefix = f"[{mtype}]"
        ok_count = fail_count = 0
        try:
            out_dir = os.path.join(base, mtype)
            if os.path.exists(out_dir):
                self._log(f"{prefix} 文件夹已存在: {out_dir}")
            else:
                os.makedirs(out_dir)
                self._log(f"{prefix} 已创建文件夹: {out_dir}")

            for i, url in enumerate(urls, 1):
                if self.cancel_event.is_set():
                    break
                mirror_url = self._convert_url(url)
                filename = self._get_filename(mirror_url)
                filepath = os.path.join(out_dir, filename)
                self._log(f"{prefix} ({i}/{len(urls)}) {filename}")

                success = False
                for attempt in range(MAX_RETRIES):
                    if self.cancel_event.is_set():
                        break
                    try:
                        def on_prog(p, d, t2, s, fn=filename, idx=i, n=len(urls)):
                            txt = (f"({idx}/{n}) {fn}  {d / 1048576:.1f}/"
                                   f"{(t2 / 1048576 if t2 else 0):.1f}MB "
                                   f"{p:.0f}% {s:.1f}MB/s")
                            self._set_row(iid, progress=txt)

                        dl = FileDownloader(mirror_url, filepath,
                                            progress_cb=on_prog, log_cb=self._log,
                                            cancel_event=self.cancel_event)
                        success, _ = dl.download()
                        if success:
                            break
                    except Exception as e:
                        if attempt < MAX_RETRIES - 1:
                            self._log(f"{prefix}   出错 ({e}), {attempt + 2}/{MAX_RETRIES} 重试…")
                            time.sleep(2)
                        else:
                            self._log(f"{prefix}   最终失败: {e}")

                if success:
                    ok_count += 1
                else:
                    fail_count += 1

        except Exception as e:
            self._log(f"{prefix} 任务异常: {e}")

        if self.cancel_event.is_set():
            task["status"] = "已取消"
            self._set_row(iid, status="已取消")
            self._log(f"{prefix} 已取消 (成功 {ok_count}, 失败 {fail_count})")
        elif fail_count == 0:
            task["status"] = "完成"
            self._set_row(iid, progress="—", status="完成")
            self._log(f"{prefix} 完成: 全部 {ok_count} 个文件成功")
        else:
            task["status"] = "失败"
            self._set_row(iid, status=f"失败(成功{ok_count}/失败{fail_count})")
            self._log(f"{prefix} 结束: 成功 {ok_count}, 失败 {fail_count}")

        self.running_tasks -= 1
        if self.running_tasks <= 0:
            self._log(f"{'=' * 60}\n全部任务结束")
            self.root.after(0, self._finish)

    def _finish(self):
        self.busy = False
        self.btn_cancel.config(state=tk.DISABLED)

    def _on_close(self):
        if self.busy:
            ans = messagebox.askyesno(
                "确认退出",
                "仍有任务在下载，退出会导致文件不完整。\n确定要退出吗？",
            )
            if not ans:
                return
            self.cancel_event.set()
        self._save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("vista")
    except Exception:
        pass
    TaskQueueApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
