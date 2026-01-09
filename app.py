from flask import Flask, jsonify, request, Response
import qbittorrentapi
from transmission_rpc import Client as TransmissionClient
import requests
import os
import logging
import json
import threading
import time
import copy
import hashlib
import secrets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ================= 1. 环境变量 =================
def get_env(key, default=None, is_int=False):
    val = os.getenv(key, default)
    if is_int and val: return int(val)
    return val

# 原有配置
QB_HOST = get_env("QB_HOST"); QB_PORT = get_env("QB_PORT", 8080, True); QB_USER = get_env("QB_USER"); QB_PASS = get_env("QB_PASS")
TR_HOST = get_env("TR_HOST"); TR_PORT = get_env("TR_PORT", 9091, True); TR_USER = get_env("TR_USER"); TR_PASS = get_env("TR_PASS")
EMBY_HOST = get_env("EMBY_HOST"); EMBY_KEY  = get_env("EMBY_KEY")
ABS_HOST  = get_env("ABS_HOST"); ABS_KEY   = get_env("ABS_KEY")
MP_HOST = get_env("MP_HOST"); MP_USER = get_env("MP_USER"); MP_PASS = get_env("MP_PASS")
NAVI_HOST = get_env("NAVI_HOST"); NAVI_USER = get_env("NAVI_USER"); NAVI_PASS = get_env("NAVI_PASS")

# HA 配置
HASS_HOST  = get_env("HASS_HOST")
HASS_TOKEN = get_env("HASS_TOKEN")
HASS_ID_TODAY_DL = get_env("HASS_ID_TODAY_DL")
HASS_ID_TODAY_UL = get_env("HASS_ID_TODAY_UL")
HASS_ID_MONTH_DL = get_env("HASS_ID_MONTH_DL")
HASS_ID_MONTH_UL = get_env("HASS_ID_MONTH_UL")
HASS_UNIT_FIX = get_env("HASS_UNIT_FIX")

# ================= 2. 工具类 =================
def get_subsonic_auth():
    if not NAVI_PASS: return {}
    salt = secrets.token_hex(6)
    token = hashlib.md5((NAVI_PASS + salt).encode('utf-8')).hexdigest()
    return {"u": NAVI_USER, "t": token, "s": salt, "v": "1.16.1", "c": "HomeLab", "f": "json"}

def smart_format(state, unit):
    if state in [None, "unavailable", "unknown"]: return "-"
    try: val = float(state)
    except: return str(state)
    if HASS_UNIT_FIX: return f"{val:.2f} {HASS_UNIT_FIX}"
    if unit in ['GB', 'GiB', 'TB', 'TiB', 'MB', 'MiB']: return f"{val:.2f} {unit}"
    power = 2**10; n = val; power_labels = {0 : '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}; n_step = 0
    while n > power and n_step < 4: n /= power; n_step += 1
    unit_suffix = "B" if unit == "B" or not unit else unit
    final_unit = f"{power_labels[n_step]}B" if n_step > 0 else unit_suffix
    return f"{n:.2f} {final_unit}"

class TokenClient:
    def __init__(self, name, host, user, pwd, login_paths):
        self.name = name
        if host and not host.startswith("http"): self.host = f"http://{host}".rstrip('/')
        else: self.host = host.rstrip('/') if host else ""
        self.user = user; self.pwd = pwd
        self.login_paths = login_paths if isinstance(login_paths, list) else [login_paths]
        self.token = None
        self.headers = {"User-Agent": "HomeLab/1.0", "Accept": "application/json"}
    def login(self):
        if not self.host or not self.user or not self.pwd: return False
        for path in self.login_paths:
            try:
                url = f"{self.host}{path}"
                try: res = requests.post(url, json={"username": self.user, "password": self.pwd}, headers=self.headers, timeout=5, verify=False)
                except: res = None
                if not res or res.status_code >= 400:
                    res = requests.post(url, data={"username": self.user, "password": self.pwd}, headers=self.headers, timeout=5, verify=False)
                if res and res.status_code == 200:
                    data = res.json()
                    if "access_token" in data: self.token = data["access_token"]; self.headers["Authorization"] = f"Bearer {self.token}"; return True
            except: pass
        return False
    def get(self, endpoint):
        if not self.token and not self.login(): return None
        try:
            url = f"{self.host}{endpoint}"; res = requests.get(url, headers=self.headers, timeout=8, verify=False)
            if res.status_code == 401: 
                if self.login(): res = requests.get(url, headers=self.headers, timeout=8, verify=False)
            if res.status_code == 200: return res.json()
        except: pass
        return None

mp_client = TokenClient("MoviePilot", MP_HOST, MP_USER, MP_PASS, ["/api/v1/login/access-token"])

# ================= 3. 数据获取 =================
def get_qb_data():
    try:
        if not QB_HOST: return {"status": False, "msg": "未配置"}
        qb = qbittorrentapi.Client(host=QB_HOST, port=QB_PORT, username=QB_USER, password=QB_PASS)
        qb.auth_log_in()
        t = qb.transfer_info(); tor = qb.torrents_info()
        act = sum(1 for x in tor if x.state not in ['pausedDL','pausedUP','completed','error','unknown'])
        return {"status": True, "dl": f"{round(t.dl_info_speed/1048576,1)} MB/s", "ul": f"{round(t.up_info_speed/1048576,1)} MB/s", "val1": act, "val2": sum(1 for x in tor if x.progress==1), "val3": len(tor), "error": sum(1 for x in tor if x.state in ['error','missingFiles'])}
    except: return {"status": False, "msg": "连接失败"}

def get_tr_data():
    try:
        if not TR_HOST: return {"status": False, "msg": "未配置"}
        tr = TransmissionClient(host=TR_HOST, port=TR_PORT, username=TR_USER, password=TR_PASS, timeout=5)
        s = tr.session_stats(); tor = tr.get_torrents()
        return {"status": True, "dl": f"{round(s.download_speed/1048576,1)} MB/s", "ul": f"{round(s.upload_speed/1048576,1)} MB/s", "val1": sum(1 for x in tor if x.status not in ['stopped'] and x.error==0), "val2": sum(1 for x in tor if x.percent_done==1), "val3": len(tor), "error": sum(1 for x in tor if x.error!=0)}
    except: return {"status": False, "msg": "连接失败"}

def get_emby_data():
    try:
        if not EMBY_HOST: return {"status": False, "msg": "未配置"}
        h = {"X-Emby-Token": EMBY_KEY}
        c = requests.get(f"{EMBY_HOST}/Items/Counts", headers=h, timeout=5).json()
        s = requests.get(f"{EMBY_HOST}/Sessions", headers=h, timeout=5).json()
        return {"status": True, "title_extra": f"播放: {len([x for x in s if x.get('NowPlayingItem')])}", "val1_label": "电影", "val1": c.get('MovieCount',0), "val2_label": "剧集", "val2": c.get('SeriesCount',0), "val3_label": "单集", "val3": c.get('EpisodeCount',0), "error": 0}
    except: return {"status": False, "msg": "连接失败"}

def get_abs_data():
    try:
        if not ABS_HOST: return {"status": False, "msg": "未配置"}
        h = {"Authorization": f"Bearer {ABS_KEY}"}
        libs = requests.get(f"{ABS_HOST}/api/libraries", headers=h, timeout=5).json().get('libraries', [])
        a = 0; p = 0
        for lib in libs:
            try:
                s = requests.get(f"{ABS_HOST}/api/libraries/{lib['id']}/stats", headers=h, timeout=2).json()
                cnt = s.get('totalItems', 0)
                if lib.get('mediaType') == 'podcast': p += cnt
                else: a += cnt
            except: pass
        act = len(requests.get(f"{ABS_HOST}/api/sessions", headers=h, timeout=3).json())
        return {"status": True, "title_extra": f"听书: {act}", "val1_label": "有声书", "val1": a, "val2_label": "播客", "val2": p, "val3_label": "库数量", "val3": len(libs), "error": 0}
    except: return {"status": False, "msg": "连接失败"}

def get_mp_subs_data():
    d = mp_client.get("/api/v1/subscribe")
    if not d: return {"status": False, "msg": "连接断开"}
    total = 0; items = []
    if isinstance(d, dict): total = d.get('total', 0); items = d.get('data', []) or d.get('items', [])
    elif isinstance(d, list): items = d; total = len(items)
    movie = 0; tv = 0
    for x in items:
        t = str(x.get('type', '')).lower()
        if t in ['movie', '电影']: movie += 1
        elif t in ['tv', 'series', 'show', '剧集', '电视剧']: tv += 1
    if total > 0 and movie == 0 and tv == 0:
        return {"status": True, "title_extra": "", "val1_label": "总订阅", "val1": total, "val2_label": "待分类", "val2": total, "val3_label": "其他", "val3": 0, "error": 0}
    return {"status": True, "title_extra": "", "val1_label": "总订阅", "val1": total, "val2_label": "电影", "val2": movie, "val3_label": "剧集", "val3": tv, "error": 0}

def get_mp_site_data():
    d = mp_client.get(f"/api/v1/site?_t={int(time.time())}")
    if not d: return {"status": False, "msg": "连接断开"}
    items = d if isinstance(d, list) else d.get('data', [])
    items = [x for x in items if isinstance(x, dict)]
    ok = sum(1 for s in items if s.get('cookie') and (s.get('is_active', True) or s.get('enable', True)))
    return {"status": True, "title_extra": "API模式", "val1_label": "配置站点", "val1": len(items), "val2_label": "Cookie在线", "val2": ok, "val3_label": "掉线/未配", "val3": len(items)-ok, "error": 0}

def get_navi_stats():
    try:
        if not NAVI_HOST: return {"status": False, "msg": "未配置"}
        p = get_subsonic_auth()
        d = requests.get(f"{NAVI_HOST.rstrip('/')}/rest/getScanStatus", params=p, timeout=5, verify=False).json()
        stats = d.get('subsonic-response', {}).get('scanStatus', {})
        song_count = stats.get('count', 0); album_count = stats.get('albumCount', 0); artist_count = stats.get('artistCount', 0)
        if artist_count == 0 or album_count == 0:
            try:
                idx_res = requests.get(f"{NAVI_HOST.rstrip('/')}/rest/getArtists", params=p, timeout=5, verify=False).json()
                indexes = idx_res.get('subsonic-response', {}).get('artists', {}).get('index', [])
                ra = 0; ral = 0
                for idx in indexes:
                    for art in idx.get('artist', []):
                        ra += 1; ral += art.get('albumCount', 0)
                if artist_count == 0: artist_count = ra
                if album_count == 0: album_count = ral
            except: pass
        return {"status": True, "title_extra": "", "val1_label": "歌曲", "val1": song_count, "val2_label": "专辑", "val2": album_count, "val3_label": "艺术家", "val3": artist_count, "error": 0}
    except: return {"status": False, "msg": "连接失败"}

def get_hass_data():
    if not HASS_HOST or not HASS_TOKEN: return {"status": False, "msg": "未配置"}
    headers = {"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"}
    def get_formatted_state(entity_id):
        if not entity_id or "example" in entity_id: return "N/A"
        try:
            url = f"{HASS_HOST.rstrip('/')}/api/states/{entity_id}"
            res = requests.get(url, headers=headers, timeout=3)
            if res.status_code == 200:
                data = res.json()
                return smart_format(data.get('state'), data.get('attributes', {}).get('unit_of_measurement'))
        except: pass
        return "-"
    return {
        "status": True, "title_extra": "OpenWrt",
        "val1_label": "今日下行", "val1": get_formatted_state(HASS_ID_TODAY_DL),
        "val2_label": "今日上行", "val2": get_formatted_state(HASS_ID_TODAY_UL),
        "val3_label": "本月下行", "val3": get_formatted_state(HASS_ID_MONTH_DL),
        "val4_label": "本月上行", "val4": get_formatted_state(HASS_ID_MONTH_UL),
        "error": 0
    }

# ================= 4. 轮询 =================
CACHE = {}
def loop():
    global CACHE
    while True:
        try:
            CACHE = {
                "qb": get_qb_data(), "tr": get_tr_data(), "emby": get_emby_data(), "abs": get_abs_data(),
                "mp_sub": get_mp_subs_data(), "mp_site": get_mp_site_data(), "navi": get_navi_stats(), "hass": get_hass_data()
            }
        except: pass
        time.sleep(15)
threading.Thread(target=loop, daemon=True).start()

# ================= 5. API =================
@app.route('/api/data')
def api_data(): return jsonify(CACHE)

@app.route('/api/proxy/stream')
def proxy_stream():
    if not NAVI_HOST: return "No Config", 404
    song_id = request.args.get('id')
    try:
        p = get_subsonic_auth()
        url = f"{NAVI_HOST.rstrip('/')}/rest/stream?id={song_id}&format=mp3&maxBitRate=320"
        req = requests.get(url, params=p, stream=True, timeout=10, verify=False)
        return Response(req.iter_content(chunk_size=1024*64), content_type=req.headers.get('Content-Type', 'audio/mpeg'))
    except Exception as e: return str(e), 500

@app.route('/api/proxy/cover')
def proxy_cover():
    if not NAVI_HOST: return "No Config", 404
    cover_id = request.args.get('id')
    try:
        p = get_subsonic_auth()
        url = f"{NAVI_HOST.rstrip('/')}/rest/getCoverArt?id={cover_id}"
        req = requests.get(url, params=p, stream=True, timeout=5, verify=False)
        return Response(req.iter_content(chunk_size=1024*64), content_type=req.headers.get('Content-Type', 'image/jpeg'))
    except: return "", 404

# [修改] 增加状态返回：是否喜欢(starred) 和 用户评分(rating)
@app.route('/api/navi/random')
def api_navi_random():
    if not NAVI_HOST: return jsonify({"error": "No Config"})
    try:
        p = get_subsonic_auth(); p['size'] = 1
        res = requests.get(f"{NAVI_HOST.rstrip('/')}/rest/getRandomSongs", params=p, timeout=5, verify=False)
        song = res.json()['subsonic-response']['randomSongs']['song'][0]
        return jsonify({
            "id": song['id'], 
            "title": song['title'], 
            "artist": song.get('artist',''),
            "starred": "starred" in song, # 返回布尔值
            "rating": song.get("userRating", 0), # 返回评分(0-5)
            "src": f"/api/proxy/stream?id={song['id']}&_t={int(time.time())}",
            "cover": f"/api/proxy/cover?id={song.get('coverArt','')}"
        })
    except Exception as e: return jsonify({"error": str(e)})

# [修改] 操作接口：支持 star, unstar, setRating
@app.route('/api/navi/rate', methods=['POST'])
def api_navi_rate():
    try:
        d = request.json
        p = get_subsonic_auth()
        action = d.get('action') # 'star', 'unstar', 'rate'
        p['id'] = d.get('id')
        
        endpoint = ''
        if action == 'star': 
            endpoint = 'star'
        elif action == 'unstar': 
            endpoint = 'unstar'
        elif action == 'rate': 
            endpoint = 'setRating'
            p['rating'] = d.get('rating', 0)
            
        if endpoint:
            requests.get(f"{NAVI_HOST.rstrip('/')}/rest/{endpoint}", params=p, timeout=5, verify=False)
            return jsonify({"success": True})
        return jsonify({"error": "Invalid action"})
    except: return jsonify({"error": "Failed"})

# ================= 6. HTML =================
@app.route('/')
def index():
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { width: 100%; height: 100vh; overflow: hidden; font-family: sans-serif; background: transparent; }
    body { display: flex; flex-direction: column; justify-content: space-between; padding: 2px 0; }
    .row { display: flex; gap: 6px; width: 100%; height: 24%; padding: 0 4px; }
    .card { flex: 1; background: rgba(30, 32, 40, 0.60); backdrop-filter: blur(8px); border-radius: 8px; padding: 0 10px; color: white; border: 1px solid rgba(255,255,255,0.08); display: flex; flex-direction: column; justify-content: center; min-width: 0; overflow: hidden; }
    .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 2px; margin-bottom: 2px; height: 22px; flex-shrink: 0; }
    .title-group { display: flex; align-items: center; gap: 6px; }
    .dot { width: 6px; height: 6px; border-radius: 50%; }
    .app-name { font-size: 12px; font-weight: bold; color: #eee; white-space: nowrap; }
    .info-right { font-size: 10px; color: #ccc; font-family: monospace; white-space: nowrap; }
    .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 2px; text-align: center; height: 100%; align-content: center; }
    .stat-item { display: flex; flex-direction: column; justify-content: center; overflow: hidden; }
    .label { font-size: 9px; color: #888; transform: scale(0.9); white-space: nowrap; }
    .value { font-size: 11px; font-weight: 600; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; width: 100%; }
    .dot-qb { background: #007bff; box-shadow: 0 0 4px #007bff; }
    .dot-tr { background: #dc3545; box-shadow: 0 0 4px #dc3545; }
    .dot-emby { background: #52B54B; box-shadow: 0 0 4px #52B54B; }
    .dot-abs { background: #d99e0b; box-shadow: 0 0 4px #d99e0b; }
    .dot-mp { background: #6f42c1; box-shadow: 0 0 4px #6f42c1; }
    .dot-navi { background: #e91e63; box-shadow: 0 0 4px #e91e63; }
    .dot-hass { background: #00bcd4; box-shadow: 0 0 4px #00bcd4; }
    .offline { color: #ff5555; font-size: 10px; }
    @media screen and (max-width: 600px) { html, body { height: auto !important; overflow-y: auto !important; display: block !important; padding: 4px 6px !important; } .row { display: block !important; height: auto !important; width: 100% !important; margin: 0 !important; padding: 0 !important; } .card { width: 100% !important; height: auto !important; min-height: 85px !important; margin-bottom: 8px !important; padding: 8px 12px !important; display: flex !important; flex-direction: column !important; } .header { margin-bottom: 6px !important; } .stats-grid { height: auto !important; padding-bottom: 2px; } }
    </style></head><body>
    <div class="row"><div class="card" id="c-qb"><div class="header"><div class="title-group"><div class="dot dot-qb"></div><span class="app-name">qBittorrent</span></div><div class="info-right" id="i-qb"></div></div><div class="stats-grid" id="s-qb"></div></div><div class="card" id="c-tr"><div class="header"><div class="title-group"><div class="dot dot-tr"></div><span class="app-name">Transmission</span></div><div class="info-right" id="i-tr"></div></div><div class="stats-grid" id="s-tr"></div></div></div>
    <div class="row"><div class="card" id="c-emby"><div class="header"><div class="title-group"><div class="dot dot-emby"></div><span class="app-name">Emby</span></div><div class="info-right" id="i-emby"></div></div><div class="stats-grid" id="s-emby"></div></div><div class="card" id="c-abs"><div class="header"><div class="title-group"><div class="dot dot-abs"></div><span class="app-name">Audiobooks</span></div><div class="info-right" id="i-abs"></div></div><div class="stats-grid" id="s-abs"></div></div></div>
    <div class="row"><div class="card" id="c-mp_sub"><div class="header"><div class="title-group"><div class="dot dot-mp"></div><span class="app-name">MP 订阅</span></div><div class="info-right" id="i-mp_sub"></div></div><div class="stats-grid" id="s-mp_sub"></div></div><div class="card" id="c-mp_site"><div class="header"><div class="title-group"><div class="dot dot-mp"></div><span class="app-name">MP 站点</span></div><div class="info-right" id="i-mp_site"></div></div><div class="stats-grid" id="s-mp_site"></div></div></div>
    <div class="row"><div class="card" id="c-navi"><div class="header"><div class="title-group"><div class="dot dot-navi"></div><span class="app-name">Navidrome</span></div><div class="info-right" id="i-navi"></div></div><div class="stats-grid" id="s-navi"></div></div><div class="card" id="c-hass"><div class="header"><div class="title-group"><div class="dot dot-hass"></div><span class="app-name">OpenWrt</span></div><div class="info-right" id="i-hass"></div></div><div class="stats-grid" id="s-hass"></div></div></div>
    <script>
    function r(d,t){if(!d||!d.status)return `<span class="offline">${d?d.msg:'loading'}</span>`;let h='';if(t=='pt'){h+=`<div class="stat-item"><span class="label">运行</span><span class="value">${d.val1}</span></div><div class="stat-item"><span class="label">完成</span><span class="value">${d.val2}</span></div><div class="stat-item"><span class="label">错误</span><span class="value" style="color:${d.error>0?'#f00':'#fff'}">${d.error}</span></div><div class="stat-item"><span class="label">总计</span><span class="value">${d.val3}</span></div>`;}else if(d.val4_label){h+=`<div class="stat-item"><span class="label">${d.val1_label}</span><span class="value">${d.val1}</span></div><div class="stat-item"><span class="label">${d.val2_label}</span><span class="value">${d.val2}</span></div><div class="stat-item"><span class="label">${d.val3_label}</span><span class="value">${d.val3}</span></div><div class="stat-item"><span class="label">${d.val4_label}</span><span class="value">${d.val4}</span></div>`;}else{h+=`<div class="stat-item"><span class="label">${d.val1_label}</span><span class="value">${d.val1}</span></div><div class="stat-item"><span class="label">${d.val2_label}</span><span class="value">${d.val2}</span></div><div class="stat-item"><span class="label">${d.val3_label}</span><span class="value">${d.val3}</span></div><div class="stat-item"><span class="label">状态</span><span class="value" style="color:#52B54B">OK</span></div>`;}return h;}
    function u(){fetch('/api/data').then(r=>r.json()).then(d=>{['qb','tr','emby','abs','mp_sub','mp_site','navi','hass'].forEach(k=>{let el=document.getElementById('s-'+k);if(el)el.innerHTML=r(d[k],(k=='qb'||k=='tr')?'pt':'media');let ei=document.getElementById('i-'+k);if(ei&&d[k]&&d[k].status){if(k=='qb'||k=='tr')ei.innerText=`⬇${d[k].dl.replace(' MB/s','M')} ⬆${d[k].ul.replace(' MB/s','M')}`;else ei.innerText=d[k].title_extra||'';}});});}
    u();setInterval(u,5000);
    </script></body></html>"""

@app.route('/player')
def player():
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><style>
    * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; -webkit-tap-highlight-color: transparent; }
    html, body { width: 100%; height: 100%; overflow: hidden; font-family: sans-serif; }
    body { background: #000; color: white; display: flex; flex-direction: column; position: relative; }
    #bg { position: absolute; top:0; left:0; width:100%; height:100%; background-size: cover; background-position: center; filter: blur(6px) brightness(0.7); z-index: 0; transition: background-image 0.5s; }
    .container { z-index: 1; flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 15px; text-align: center; width: 100%; }
    .cover-box { width: 100px; height: 100px; border-radius: 12px; overflow: hidden; margin-bottom: 12px; box-shadow: 0 6px 15px rgba(0,0,0,0.6); position: relative; background: #333; }
    .cover-box img { width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity 0.5s; }
    .info { width: 100%; margin-bottom: 12px; text-shadow: 0 1px 3px rgba(0,0,0,0.8); }
    .title { font-size: 16px; font-weight: bold; margin-bottom: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .artist { font-size: 13px; color: #eee; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .progress-container { width: 100%; height: 4px; background: rgba(255,255,255,0.3); border-radius: 2px; margin-bottom: 20px; position: relative; cursor: pointer; }
    .progress-bar { width: 0%; height: 100%; background: #e91e63; border-radius: 2px; transition: width 0.1s linear; box-shadow: 0 0 5px rgba(233, 30, 99, 0.5); }
    .controls { display: flex; align-items: center; justify-content: center; gap: 15px; width: 100%; }
    .btn { background: transparent; border: none; color: white; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: 0.2s; filter: drop-shadow(0 2px 2px rgba(0,0,0,0.5)); }
    .btn:active { transform: scale(0.9); }
    .btn-play { width: 50px; height: 50px; background: white; border-radius: 50%; color: #e91e63; box-shadow: 0 4px 10px rgba(0,0,0,0.4); }
    .btn-play svg { fill: #e91e63; width: 20px; height: 20px; margin-left: 2px; }
    .btn-play.playing svg { margin-left: 0; }
    .btn-func { width: 36px; height: 36px; border-radius: 50%; background: rgba(0,0,0,0.2); }
    .btn-func:hover { background: rgba(0,0,0,0.4); }
    .btn-rate.active svg { fill: #ff4081; }
    #autoplay-overlay { position: absolute; top:0; left:0; width:100%; height:100%; background: rgba(0,0,0,0.7); z-index: 10; display: flex; align-items: center; justify-content: center; flex-direction: column; cursor: pointer; backdrop-filter: blur(5px); display: none; }
    #autoplay-overlay span { margin-top:10px; font-size:12px; color:#ccc;}
    </style></head><body>
    <div id="bg"></div><div id="autoplay-overlay" onclick="enableAudio()"><svg width="40" height="40" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg><span>点击开始播放</span></div>
    <div class="container"><div class="cover-box"><img id="cover"></div><div class="info"><div class="title" id="title">Navidrome</div><div class="artist" id="artist">Player</div></div><div class="progress-container" onclick="seek(event)"><div class="progress-bar" id="bar"></div></div><div class="controls">
    <button class="btn btn-func btn-rate" id="btn-dislike" onclick="toggleDislike()"><svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"></path><path d="m12 5 3 3-3 3 3 3-3 3"></path></svg></button>
    <button class="btn btn-func" onclick="next()"><svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M6 6h2v12H6zm3.5 6l8.5 6V6z"/></svg></button><button class="btn btn-play" id="btn-play" onclick="togglePlay()"><svg id="icon-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></button><button class="btn btn-func" onclick="next()"><svg width="20" height="20" viewBox="0 0 24 24" fill="white"><path d="M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z"/></svg></button><button class="btn btn-func btn-rate" id="btn-like" onclick="toggleLike()"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg></button></div></div>
    <audio id="audio" onended="next()" ontimeupdate="upTime()" crossorigin="anonymous"></audio>
    <script>
    let currentId=null; let isStarred=false; let userRating=0;
    const audio=document.getElementById('audio');const playBtn=document.getElementById('btn-play');
    const btnLike=document.getElementById('btn-like');const btnDislike=document.getElementById('btn-dislike');
    const svgPlay='<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>';const svgPause='<svg viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>';
    
    function next(){
        document.getElementById('title').style.opacity=0.5;
        // 重置按钮 UI
        btnLike.querySelector('svg').style.fill='none'; btnLike.querySelector('svg').style.stroke='white';
        btnDislike.querySelector('svg').style.stroke='white';
        
        fetch('/api/navi/random').then(r=>r.json()).then(d=>{
            if(d.error){document.getElementById('title').innerText="Error";return;}
            currentId=d.id;
            isStarred=d.starred; 
            userRating=d.rating;
            
            document.getElementById('title').innerText=d.title;
            document.getElementById('title').style.opacity=1;
            document.getElementById('artist').innerText=d.artist||'Unknown';
            const img=document.getElementById('cover');img.style.opacity=0;img.src=d.cover;img.onload=()=>img.style.opacity=1;
            document.getElementById('bg').style.backgroundImage=`url('${d.cover}')`;
            
            // 初始化 UI 状态
            updateUI();
            
            audio.src=d.src;
            var p=audio.play();
            if(p!==undefined){p.then(_=>{updatePlayBtn(true);document.getElementById('autoplay-overlay').style.display='none';}).catch(e=>{updatePlayBtn(false);document.getElementById('autoplay-overlay').style.display='flex';});}
        });
    }
    
    function updateUI() {
        // 更新 Like 按钮
        if(isStarred) {
            btnLike.querySelector('svg').style.fill='#ff4081'; 
            btnLike.querySelector('svg').style.stroke='#ff4081';
        } else {
            btnLike.querySelector('svg').style.fill='none'; 
            btnLike.querySelector('svg').style.stroke='white';
        }
        
        // 更新 Dislike 按钮 (假设评分1星为不喜欢)
        if(userRating === 1) {
            btnDislike.querySelector('svg').style.stroke='#ff4081';
        } else {
            btnDislike.querySelector('svg').style.stroke='white';
        }
    }

    function toggleLike() {
        if(!currentId) return;
        const action = isStarred ? 'unstar' : 'star';
        isStarred = !isStarred; // 乐观更新状态
        updateUI();
        
        fetch('/api/navi/rate', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({id:currentId, action:action})
        });
    }

    function toggleDislike() {
        if(!currentId) return;
        // 如果当前是 1 星，则改为 0 星（取消）；否则设为 1 星
        const newRating = (userRating === 1) ? 0 : 1;
        userRating = newRating; // 乐观更新
        updateUI();
        
        fetch('/api/navi/rate', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({id:currentId, action:'rate', rating:newRating})
        });
    }

    function enableAudio(){document.getElementById('autoplay-overlay').style.display='none';audio.play();updatePlayBtn(true);}
    function togglePlay(){if(audio.paused){audio.play();updatePlayBtn(true);}else{audio.pause();updatePlayBtn(false);}}
    function updatePlayBtn(p){playBtn.innerHTML=p?svgPause:svgPlay;playBtn.className=p?"btn btn-play playing":"btn btn-play";}
    function upTime(){if(audio.duration){document.getElementById('bar').style.width=(audio.currentTime/audio.duration*100)+"%";}}
    function seek(e){if(!audio.duration)return;const r=e.currentTarget.getBoundingClientRect();audio.currentTime=((e.clientX-r.left)/r.width)*audio.duration;}
    
    next();
    </script></body></html>"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8501)