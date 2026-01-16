#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
水墨屏网页浏览器（地址栏 + 自动识别网址/搜索 + HTTP/HTTPS 探测 + 水墨遮罩点穿）
要求实现：
1) 输入网址：优先 https；如果 https 连不上就自动尝试 http（用 fetch no-cors 做网络可达性探测）
2) 输入不是网址：自动用 Bing 搜索并加载搜索结果
3) 水墨屏遮罩一直存在，且可“点穿”点击 iframe 内页面；页面内部怎么跳转都不影响遮罩和功能

注意（客观限制）：
- 有些网站通过 X-Frame-Options / CSP 禁止被 iframe 嵌入，iframe 会显示空白/报错页。
  这种不是遮罩问题，是对方站点策略。
"""

import http.server
import socketserver
import threading
import webbrowser
from pathlib import Path


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>水墨屏网页浏览器</title>

  <style>
    :root{
      --paper: #f3efe6;
      --ink: #111;
      --panel: #efe9dc;
      --border: rgba(0,0,0,.35);
    }

    html, body{
      height: 100%;
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font: 14px "Microsoft YaHei", system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }

    /* 顶部地址栏 */
    .bar{
      position: sticky;
      top: 0;
      z-index: 50;
      display: flex;
      gap: 8px;
      align-items: center;
      padding: 10px;
      background: var(--panel);
      border-bottom: 1px solid var(--border);
    }

    .btn{
      border: 1px solid var(--border);
      background: var(--paper);
      color: var(--ink);
      padding: 6px 10px;
      border-radius: 8px;
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
    }
    .btn:active{ transform: translateY(1px); }

    .url{
      flex: 1;
      min-width: 120px;
      border: 1px solid var(--border);
      background: var(--paper);
      color: var(--ink);
      padding: 8px 10px;
      border-radius: 10px;
      outline: none;
    }

    .viewer{
      position: relative;
      height: calc(100% - 58px);
      min-height: 320px;
      overflow: hidden;
      background: var(--paper);
    }

    /* iframe：用 CSS filter 做墨水化（不要 SVG filter，避免 iframe 不渲染） */
    #frame{
      width: 100%;
      height: 100%;
      border: 0;
      display: block;
      background: var(--paper);

      filter: grayscale(1) contrast(1.35) brightness(0.98);
    }

    /* 遮罩：点穿 */
    .mask{
      position: absolute;
      inset: 0;
      pointer-events: none; /* 允许点穿点击网页 */
      z-index: 10;
      mix-blend-mode: multiply;
      opacity: 1;
    }

    .mask::before{
      content:"";
      position:absolute;
      inset:0;
      background:
        radial-gradient(circle at 20% 10%, rgba(0,0,0,.040), transparent 46%),
        radial-gradient(circle at 80% 30%, rgba(0,0,0,.032), transparent 50%),
        radial-gradient(circle at 30% 85%, rgba(0,0,0,.030), transparent 55%),
        linear-gradient(180deg, rgba(255,255,255,.28), rgba(0,0,0,.03));
      opacity: .95;
    }

    .mask::after{
      content:"";
      position:absolute;
      inset:0;
      opacity: .12;
      background-image:
        repeating-radial-gradient(circle at 0 0,
          rgba(0,0,0,.22) 0 1px,
          rgba(0,0,0,0) 1px 3px);
    }

    .toast{
      position: absolute;
      left: 10px;
      bottom: 10px;
      z-index: 20;
      background: rgba(243,239,230,.88);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 8px 10px;
      max-width: min(720px, calc(100% - 20px));
      font-size: 12px;
      backdrop-filter: blur(4px);
      line-height: 1.35;
    }

    .toast code{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 11px;
    }
  </style>
</head>

<body>
  <div class="bar">
    <button class="btn" id="backBtn" title="后退">←</button>
    <button class="btn" id="forwardBtn" title="前进">→</button>
    <button class="btn" id="reloadBtn" title="刷新">⟳</button>

    <input class="url" id="urlInput" placeholder="输入网址（example.com）或关键词（比如 航空航天 新闻）" />
    <button class="btn" id="goBtn">访问/搜索</button>
    <button class="btn" id="newTabBtn" title="新标签打开">↗</button>
  </div>

  <div class="viewer">
    <iframe id="frame" src="http://spaceaero.space/ink.html"></iframe>
    <div class="mask" aria-hidden="true"></div>

    <div class="toast" id="toast">
      规则：<br>
      1) 看起来像网址：优先 <code>https</code>，如果网络连不上自动试 <code>http</code>。<br>
      2) 不像网址：自动用 Bing 搜索。<br>
      注意：部分网站禁止 iframe（X-Frame-Options / CSP），会空白或报错页——不是遮罩问题。
    </div>
  </div>

  <script>
    const frame = document.getElementById('frame');
    const urlInput = document.getElementById('urlInput');
    const toast = document.getElementById('toast');

    const backBtn = document.getElementById('backBtn');
    const forwardBtn = document.getElementById('forwardBtn');
    const reloadBtn = document.getElementById('reloadBtn');
    const goBtn = document.getElementById('goBtn');
    const newTabBtn = document.getElementById('newTabBtn');

    function showToast(msg, ms=3500) {
      toast.textContent = msg;
      toast.style.display = 'block';
      clearTimeout(showToast._t);
      showToast._t = setTimeout(() => {
        toast.style.display = 'none';
      }, ms);
    }

    function enc(s){ return encodeURIComponent(s); }

    // 判断是不是“像网址”
    // 规则（够用且稳）：
    // - 有协议（http/https/about/data/blob）
    // - 或者是 localhost / IP
    // - 或者包含点号且不含空格（example.com / a.b / xxx.cn）
    function looksLikeUrl(input) {
      const s = (input || '').trim();
      if (!s) return false;

      if (/^(about:|data:|blob:)/i.test(s)) return true;
      if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(s)) return true;

      if (/\s/.test(s)) return false;

      if (/^localhost(:\d+)?(\/|$)/i.test(s)) return true;

      // IPv4
      if (/^\d{1,3}(\.\d{1,3}){3}(:\d+)?(\/|$)/.test(s)) return true;

      // 形如 example.com / a.b / abc.xyz/path
      if (s.includes('.')) return true;

      return false;
    }

    // 生成 Bing 搜索 URL
    function bingSearchUrl(q) {
      return `https://www.bing.com/search?q=${enc(q.trim())}`;
    }

    // 规范化 URL（如果无协议先暂不补，交给探测函数选择 http/https）
    function normalizeMaybeUrl(raw) {
      let u = (raw || '').trim();
      if (!u) return '';

      if (/^(about:|data:|blob:)/i.test(u)) return u;
      if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(u)) return u;

      // 没协议：先返回原样（例如 example.com/path）
      return u;
    }

    // 网络可达性探测：用 fetch(mode:no-cors) 判断“网络层是否能连上”
    // - 成功 resolve：说明网络连接没报错（即便状态码未知）
    // - reject：说明网络错误（DNS/握手失败/被阻断等），可用来决定换 http
    async function probeReachable(url) {
      try {
        // no-cors：跨域也不会被 CORS 拦截读取，但能区分网络错误
        await fetch(url, { mode: 'no-cors', cache: 'no-store' });
        return true;
      } catch (_) {
        return false;
      }
    }

    // 对用户输入执行“访问/搜索”
    async function go() {
      const raw = (urlInput.value || '').trim();
      if (!raw) return;

      // 不是网址：直接 Bing 搜索
      if (!looksLikeUrl(raw)) {
        const sUrl = bingSearchUrl(raw);
        frame.src = sUrl;
        showToast('使用 Bing 搜索：' + raw);
        return;
      }

      // 是网址
      const norm = normalizeMaybeUrl(raw);

      // 已带协议：直接尝试；若是 https 且网络错误，再尝试 http
      if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(norm)) {
        // about/data/blob 直接走
        if (/^(about:|data:|blob:)/i.test(norm)) {
          frame.src = norm;
          showToast('打开：' + norm);
          return;
        }

        // http/https
        if (/^https:\/\//i.test(norm)) {
          frame.src = norm;
          showToast('打开（https）：' + norm);

          // 轻量探测：若 https 网络层不可达，则自动试 http
          const ok = await probeReachable(norm);
          if (!ok) {
            const httpUrl = norm.replace(/^https:\/\//i, 'http://');
            const ok2 = await probeReachable(httpUrl);
            if (ok2) {
              frame.src = httpUrl;
              showToast('https 连接失败，已自动改用 http：' + httpUrl, 4500);
            } else {
              showToast('https / http 都无法连通（可能 DNS/网络问题）：' + norm, 4500);
            }
          }
          return;
        }

        // 直接是 http 或其它协议：直接打开
        frame.src = norm;
        showToast('打开：' + norm);
        return;
      }

      // 无协议：先试 https，网络不可达再试 http
      const httpsUrl = 'https://' + norm;
      const httpUrl  = 'http://' + norm;

      frame.src = httpsUrl;
      showToast('尝试 https：' + httpsUrl);

      const okHttps = await probeReachable(httpsUrl);
      if (okHttps) return;

      const okHttp = await probeReachable(httpUrl);
      if (okHttp) {
        frame.src = httpUrl;
        showToast('https 不可达，已自动改用 http：' + httpUrl, 4500);
      } else {
        showToast('https / http 都无法连通（可能输入有误或网络问题）：' + norm, 4500);
      }
    }

    // 按钮：后退/前进/刷新（跨域时可能受限，但不影响遮罩）
    backBtn.addEventListener('click', () => {
      try { frame.contentWindow.history.back(); }
      catch (e) { showToast('无法后退（可能被浏览器限制或跨域）', 3000); }
    });

    forwardBtn.addEventListener('click', () => {
      try { frame.contentWindow.history.forward(); }
      catch (e) { showToast('无法前进（可能被浏览器限制或跨域）', 3000); }
    });

    reloadBtn.addEventListener('click', () => {
      try { frame.contentWindow.location.reload(); }
      catch (e) { frame.src = frame.src; }
    });

    goBtn.addEventListener('click', go);
    urlInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') go();
    });

    newTabBtn.addEventListener('click', () => {
      const raw = (urlInput.value || '').trim();
      if (!raw) return;

      if (!looksLikeUrl(raw)) {
        window.open(bingSearchUrl(raw), '_blank');
        return;
      }

      const norm = normalizeMaybeUrl(raw);
      const u = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(norm) ? norm : ('https://' + norm);
      window.open(u, '_blank');
    });

    // 尝试（同源才行）同步地址栏；跨域无法读取真实地址时不强求
    function syncUrlBar() {
      try {
        urlInput.value = frame.contentWindow.location.href;
      } catch (_) {
        // 跨域：保留用户输入
        if (!urlInput.value) urlInput.value = frame.src;
      }
    }

    frame.addEventListener('load', () => {
      syncUrlBar();
      // 这里无法可靠判断“是否被禁止 iframe”，只给一个通用提示
      showToast('加载完成（若空白，多半是该网站禁止 iframe）', 2500);
    });

    // 初始化
    urlInput.value = frame.src;
    setTimeout(() => { toast.style.display = 'none'; }, 5500);
  </script>
</body>
</html>
"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass


def main():
    out = Path("ink_browser.html")
    out.write_text(HTML, encoding="utf-8")

    port = 8000
    socketserver.TCPServer.allow_reuse_address = True

    with socketserver.TCPServer(("127.0.0.1", port), QuietHandler) as httpd:
        url = f"http://127.0.0.1:{port}/{out.name}"

        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        print("已启动本地服务：", url)
        print("按 Ctrl+C 退出。")

        try:
            webbrowser.open(url, new=1, autoraise=True)
        except Exception:
            pass

        try:
            t.join()
        except KeyboardInterrupt:
            print("\n正在退出...")
            httpd.shutdown()


if __name__ == "__main__":
    main()
