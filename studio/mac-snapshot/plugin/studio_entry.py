from urllib.parse import urlsplit

from aiohttp import web


ENTRY_HTML = '''<!doctype html>
<html lang="zh-CN" data-zyb-studio="standalone">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>庄严宝刹 · 设计工作台</title>
<link rel="stylesheet" data-zyb-studio-style="1" href="/extensions/zyb_obsidian_design/studio.css?v=20.23">
<style>
body{margin:0;background:#f5f5f7;color:#1d1d1f;font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
#zyb-studio-loading{max-width:420px;margin:15vh auto;padding:24px}
#zyb-studio-loading a{color:#274b9f}
</style>
<script type="module" src="/extensions/zyb_obsidian_design/studio.js?v=20.50"></script>
</head>
<body>
<main id="zyb-studio-loading" role="status">
<h1>庄严宝刹 · 设计工作台</h1>
<p>正在连接工作台…</p>
<p>若页面没有打开，可<a href="">重新加载</a>，或联系管理员。</p>
<noscript><p>请启用浏览器的 JavaScript 后重新加载。已保存的设计不受影响。</p></noscript>
</main>
</body>
</html>'''


def require_local(request):
    if request.remote not in ('127.0.0.1', '::1'):
        raise ValueError('仅允许本机访问')
    if urlsplit('http://' + request.host).hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('主机名无效')


@web.middleware
async def studio_entry(request, handler):
    if request.method in ('GET', 'HEAD') and request.path == '/' and request.query.get('zyb') == 'studio':
        try:
            require_local(request)
        except ValueError as error:
            return web.json_response({'error': str(error)}, status=400, headers={'Cache-Control': 'no-store'})
        return web.Response(text=ENTRY_HTML, content_type='text/html', headers={
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
        })
    return await handler(request)
