"""astr基座 插件 Pages 支持 (复刻 AstrBot 插件 UI 页面)。

AstrBot 插件可在插件目录 ``pages/<页面名>/index.html`` 下自带 Web UI, 页面脚本通过
``window.AstrBotPluginPage`` bridge 调用插件用 ``context.register_web_api()`` 注册的
后端 API。此模块提供:

- ``/page``     静态页面服务 (相对资源改写为带 token 的 ``/page`` 链接, index.html 注入 bridge);
- ``/page-api`` 后端分发: 按 AstrBot 路由规则匹配已登记的 Web API, 在 quart 请求上下文中调用
  (插件 handler 普遍用 ``from quart import request, jsonify``)。
"""

from __future__ import annotations

import inspect
import json
import mimetypes
import os
import posixpath
import re
from urllib.parse import quote

from core.base.logger import PLUGIN, get_logger

from ..runtime import state

log = get_logger(PLUGIN, "astr基座")

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_WEB_DIR)
_APPS_DIR = os.path.join(_PLUGIN_DIR, "apps")

_PAGES_DIR_NAME = "pages"
_ENTRY_FILE = "index.html"


# ==================================================================== #
#  页面发现
# ==================================================================== #


def list_app_pages(app_dir: str) -> list[str]:
    """插件目录下 pages/ 的一级子目录 (须含 index.html) = 该插件的 UI 页面列表。"""
    root = os.path.join(app_dir, _PAGES_DIR_NAME)
    out: list[str] = []
    if not os.path.isdir(root):
        return out
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in (".", ".."):
            continue
        if os.path.isfile(os.path.join(entry.path, _ENTRY_FILE)):
            out.append(entry.name)
    return out


def _safe_name(name: str) -> str:
    """页面/插件目录名白名单校验 (防路径穿越), 非法返回空。"""
    name = str(name or "").strip()
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return ""
    return name


def _safe_join(base: str, rel: str) -> str | None:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    path = os.path.normpath(os.path.join(base, rel))
    base = os.path.normpath(base)
    if path != base and not path.startswith(base + os.sep):
        return None
    return path


# ==================================================================== #
#  相对资源改写 (relative src/href/url() → 带 token 的 /page 链接)
# ==================================================================== #

_HTML_ATTR_RE = re.compile(
    r"(?P<attr>src|href)=(?P<q>[\"'])(?P<url>[^\"']*)(?P=q)", re.IGNORECASE
)
_CSS_URL_RE = re.compile(
    r"url\(\s*(?P<q>[\"']?)(?P<url>[^\"')]+)(?P=q)\s*\)", re.IGNORECASE
)
_ABS_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _is_relative(url: str) -> bool:
    u = (url or "").strip()
    if not u or u.startswith(("#", "/", "//")):
        return False
    return not _ABS_URL_RE.match(u)


def _resolve_rel(cur_rel: str, url: str) -> str:
    """按当前文件相对路径解析引用 (assets/a.css 里的 ./b.png → assets/b.png)。"""
    clean = url.split("#", 1)[0].split("?", 1)[0]
    base = posixpath.dirname(cur_rel.replace("\\", "/"))
    return posixpath.normpath(posixpath.join(base, clean)).lstrip("/")


def _asset_url(app: str, page: str, rel: str, token: str) -> str:
    # 相对链接 "page?..." 从 /api/ext/astrbot_base/page 解析仍落在同一路由上
    return (
        f"page?app={quote(app)}&page={quote(page)}"
        f"&path={quote(rel)}&token={quote(token)}"
    )


def _rewrite_html(html: str, app: str, page: str, cur_rel: str, token: str) -> str:
    def repl(m):
        url = m.group("url")
        if not _is_relative(url):
            return m.group(0)
        rel = _resolve_rel(cur_rel, url)
        return f'{m.group("attr")}={m.group("q")}{_asset_url(app, page, rel, token)}{m.group("q")}'

    return _HTML_ATTR_RE.sub(repl, html)


def _rewrite_css(css: str, app: str, page: str, cur_rel: str, token: str) -> str:
    def repl(m):
        url = m.group("url")
        if not _is_relative(url):
            return m.group(0)
        rel = _resolve_rel(cur_rel, url)
        return f'url({m.group("q")}{_asset_url(app, page, rel, token)}{m.group("q")})'

    return _CSS_URL_RE.sub(repl, css)


# ==================================================================== #
#  bridge SDK (注入 index.html, 提供 window.AstrBotPluginPage)
# ==================================================================== #

_BRIDGE_JS = r"""
(function(){
  var APP=__APP__, PAGE=__PAGE__, TOKEN=__TOKEN__;
  var CTX={pluginName:APP,displayName:APP,pageName:PAGE,pageTitle:PAGE,locale:'zh-CN',i18n:{},isDark:false};
  var API=location.pathname.replace(/\/page$/,'');
  var SSE={};
  function apiUrl(ep,params){
    var e=String(ep||'');
    var sub=e.charAt(0)==='/'?e.slice(1):(APP+'/'+e);
    var u=new URL(API+'/page-api',location.origin);
    u.searchParams.set('path',sub);
    u.searchParams.set('token',TOKEN);
    if(params)Object.keys(params).forEach(function(k){u.searchParams.set(k,String(params[k]));});
    return u.toString();
  }
  function unwrap(d){
    if(d&&typeof d==='object'&&d.status==='error')throw new Error(d.message||'请求失败');
    return (d&&typeof d==='object'&&d.data!==undefined&&d.status!==undefined)?d.data:d;
  }
  async function jfetch(url,opts){
    var r=await fetch(url,opts);
    var ct=r.headers.get('content-type')||'';
    var d=ct.indexOf('json')>=0?await r.json():await r.text();
    if(!r.ok)throw new Error((d&&d.message)||('HTTP '+r.status));
    return unwrap(d);
  }
  window.AstrBotPluginPage={
    ready:function(){return Promise.resolve(CTX);},
    getContext:function(){return CTX;},
    getLocale:function(){return CTX.locale;},
    getI18n:function(){return {};},
    t:function(k,fb){return fb||'';},
    onContext:function(h){if(typeof h==='function'){try{h(CTX);}catch(e){}}return function(){};},
    __setInitialContext:function(){},
    apiGet:function(ep,params){return jfetch(apiUrl(ep,params));},
    apiPost:function(ep,body){return jfetch(apiUrl(ep),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});},
    upload:async function(ep,file){
      var fd=new FormData();
      fd.append('file',file,(file&&file.name)||'upload.bin');
      return jfetch(apiUrl(ep),{method:'POST',body:fd});
    },
    download:async function(ep,params,filename){
      var r=await fetch(apiUrl(ep,params));
      if(!r.ok)throw new Error('HTTP '+r.status);
      var blob=await r.blob();
      var a=document.createElement('a');
      a.href=URL.createObjectURL(blob);
      a.download=filename||'download';
      document.body.appendChild(a);a.click();a.remove();
      setTimeout(function(){URL.revokeObjectURL(a.href);},0);
      return {filename:a.download};
    },
    subscribeSSE:async function(ep,handlers,params){
      handlers=handlers||{};
      var id='sse_'+Math.random().toString(36).slice(2);
      var ac=new AbortController();
      SSE[id]=ac;
      (async function(){
        try{
          var r=await fetch(apiUrl(ep,params),{signal:ac.signal});
          if(!r.ok||!r.body)throw new Error('HTTP '+r.status);
          if(handlers.onOpen)handlers.onOpen();
          var reader=r.body.getReader(),dec=new TextDecoder(),buf='';
          for(;;){
            var c=await reader.read();
            if(c.done)break;
            buf+=dec.decode(c.value,{stream:true});
            var blocks=buf.replace(/\r\n/g,'\n').replace(/\r/g,'\n').split('\n\n');
            buf=blocks.pop();
            blocks.forEach(function(b){
              var data=b.split('\n').filter(function(l){return l.indexOf('data:')===0;})
                .map(function(l){return l.slice(5).replace(/^ /,'');}).join('\n');
              if(!data)return;
              var parsed;try{parsed=JSON.parse(data);}catch(e){parsed=data;}
              if(handlers.onMessage)handlers.onMessage({raw:data,parsed:parsed,eventType:'message',lastEventId:''});
            });
          }
        }catch(e){if(!ac.signal.aborted&&handlers.onError)handlers.onError();}
        finally{delete SSE[id];}
      })();
      return id;
    },
    unsubscribeSSE:function(id){if(SSE[id]){SSE[id].abort();delete SSE[id];}return Promise.resolve();}
  };
})();
"""

_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)


def _inject_bridge(html: str, app: str, page: str, token: str) -> str:
    js = (
        _BRIDGE_JS.replace("__APP__", json.dumps(app))
        .replace("__PAGE__", json.dumps(page))
        .replace("__TOKEN__", json.dumps(token))
    )
    tag = f"<script>{js}</script>"
    m = _HEAD_RE.search(html)
    if m:
        return html[: m.end()] + tag + html[m.end():]
    return tag + html


# ==================================================================== #
#  /page — 静态页面服务
# ==================================================================== #


async def handle_page(request):
    """GET /page?app=<插件>&page=<页面>[&path=<资源>] — 插件页静态文件。"""
    from aiohttp import web

    app_name = _safe_name(request.query.get("app", ""))
    page = _safe_name(request.query.get("page", ""))
    token = request.query.get("token", "")
    if not app_name or not page:
        return web.json_response({"success": False, "message": "缺少 app / page 参数"}, status=400)
    base = os.path.join(_APPS_DIR, app_name, _PAGES_DIR_NAME, page)
    if not os.path.isdir(base):
        return web.Response(text="插件页面不存在", status=404, content_type="text/plain", charset="utf-8")
    rel = (request.query.get("path", "") or _ENTRY_FILE).replace("\\", "/").lstrip("/")
    path = _safe_join(base, rel)
    if not path or not os.path.isfile(path):
        return web.Response(text="文件不存在", status=404, content_type="text/plain", charset="utf-8")
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError as e:
        return web.Response(text=f"读取失败: {e}", status=500, content_type="text/plain", charset="utf-8")

    ext = os.path.splitext(path)[1].lower()
    if ext in (".html", ".htm"):
        text = content.decode("utf-8", errors="replace")
        text = _rewrite_html(text, app_name, page, rel, token)
        text = _inject_bridge(text, app_name, page, token)
        return web.Response(text=text, content_type="text/html", charset="utf-8")
    if ext == ".css":
        text = content.decode("utf-8", errors="replace")
        text = _rewrite_css(text, app_name, page, rel, token)
        return web.Response(text=text, content_type="text/css", charset="utf-8")
    return web.Response(body=content, headers={"Content-Type": ctype, "Cache-Control": "no-cache"})


# ==================================================================== #
#  /page-api — 插件后端 Web API 分发 (quart 请求上下文兼容)
# ==================================================================== #

_ROUTE_PARAM_RE = re.compile(r"<(?:(path):)?([A-Za-z_][A-Za-z0-9_]*)>")


def _route_pattern(route: str) -> str:
    """AstrBot 路由 (<param> / <path:param>) → 正则 (同 AstrBot 本体规则)。"""
    normalized = route if route.startswith("/") else "/" + route
    chunks = []
    pos = 0
    for m in _ROUTE_PARAM_RE.finditer(normalized):
        chunks.append(re.escape(normalized[pos:m.start()]))
        name = m.group(2)
        chunks.append(f"(?P<{name}>.*)" if m.group(1) else f"(?P<{name}>[^/]+)")
        pos = m.end()
    chunks.append(re.escape(normalized[pos:]))
    return "".join(chunks)


def _match_web_api(subpath: str, method: str):
    request_path = "/" + subpath.lstrip("/")
    method = method.upper()
    for route, handler, methods, _desc in state.WEB_APIS:
        if method not in methods:
            continue
        m = re.fullmatch(_route_pattern(route), request_path)
        if m:
            return handler, m.groupdict()
    return None


_quart_app = None


def _get_quart_app():
    global _quart_app
    if _quart_app is None:
        from quart import Quart

        _quart_app = Quart("astrbase_pages")
    return _quart_app


async def _call_web_api(handler, path_params: dict, *, method: str, path: str,
                        headers: list, body: bytes):
    """在 quart 请求上下文里调用插件 handler (插件普遍用 quart 的 request/jsonify)。"""
    app = _get_quart_app()
    async with app.test_request_context(path, method=method, headers=headers, data=body):
        rv = handler(**path_params)
        if inspect.isawaitable(rv):
            rv = await rv
        resp = await app.make_response(rv)
        payload = await resp.get_data()
        return resp.status_code, resp.headers.get("Content-Type", "application/octet-stream"), payload


async def handle_page_api(request):
    """GET/POST /page-api?path=<插件名/端点> — 分发到插件注册的 Web API。"""
    from aiohttp import web

    subpath = request.query.get("path", "").strip()
    if not subpath:
        return web.json_response({"status": "error", "message": "缺少 path 参数", "data": {}})
    matched = _match_web_api(subpath, request.method)
    if not matched:
        return web.json_response({"status": "error", "message": "未找到该路由", "data": {}})
    handler, path_params = matched

    # 透传除 path/token 外的查询参数; body/头原样进 quart 上下文 (multipart 上传可解析)
    extra = [(k, v) for k, v in request.query.items() if k not in ("path", "token")]
    qs = "&".join(f"{quote(k)}={quote(v)}" for k, v in extra)
    quart_path = "/" + subpath.lstrip("/") + (f"?{qs}" if qs else "")
    body = await request.read()
    headers = [
        (k, v) for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization")
    ]
    try:
        status, ctype, payload = await _call_web_api(
            handler, path_params, method=request.method,
            path=quart_path, headers=headers, body=body,
        )
    except ModuleNotFoundError as e:
        log.warning(f"[astr基座] 插件页 API 缺少依赖: {e}")
        return web.json_response(
            {"status": "error", "message": f"缺少依赖 {e.name}, 请在框架环境安装后重试", "data": {}}
        )
    except Exception as e:
        log.error(f"[astr基座] 插件页 API 调用失败 [{subpath}]: {e}", exc_info=True)
        return web.json_response({"status": "error", "message": f"插件 API 调用失败: {e}", "data": {}})
    return web.Response(body=payload, status=status, headers={"Content-Type": ctype})
