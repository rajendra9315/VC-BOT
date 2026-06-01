import os, asyncio, json, shutil, secrets
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ── ENV CONFIG ────────────────────────────────────────────────────────────────
API_ID       = int(os.environ["API_ID"])
API_HASH     = os.environ["API_HASH"]
SESSION      = os.environ["SESSION_STRING"]
CHANNEL      = os.environ["CHANNEL"]          # @username or -100xxxxxxx
WEB_PASS     = os.environ["WEB_PASSWORD"]
WEB_PORT     = int(os.getenv("WEB_PORT", "8000"))
MAX_VOICES   = int(os.getenv("MAX_VOICES", "100"))
AUDIO_DIR    = Path(os.getenv("AUDIO_DIR", "audio"))
AUDIO_DIR.mkdir(exist_ok=True)

# ── IMPORTS ───────────────────────────────────────────────────────────────────
from pyrogram import Client
from pyrogram.raw.functions.phone import CreateGroupCall, LeaveGroupCall
from pyrogram.raw.types import InputGroupCall
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioPiped, AudioVideoPiped
from pytgcalls.types.input_stream import AudioParameters
from pytgcalls.exceptions import NoActiveGroupCall, AlreadyJoinedError

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── STATE ─────────────────────────────────────────────────────────────────────
queue: list[str] = []          # ordered list of filenames
current_index: int = 0
is_playing: bool = False
vc_active: bool = False

app_client = Client("vc_session", api_id=API_ID, api_hash=API_HASH, session_string=SESSION)
calls = PyTgCalls(app_client)

# ── HELPERS ───────────────────────────────────────────────────────────────────
async def get_channel_peer():
    return await app_client.resolve_peer(CHANNEL)

async def play_next():
    global current_index, is_playing, vc_active
    if not queue or current_index >= len(queue):
        current_index = 0
        is_playing = False
        return
    fname = queue[current_index]
    fpath = AUDIO_DIR / fname
    if not fpath.exists():
        current_index += 1
        await play_next()
        return
    try:
        peer = await get_channel_peer()
        stream = AudioPiped(str(fpath), AudioParameters.from_quality("high"))
        if not vc_active:
            await calls.join_group_call(CHANNEL, stream, stream_type=stream)
            vc_active = True
        else:
            await calls.change_stream(CHANNEL, stream)
        is_playing = True
    except AlreadyJoinedError:
        peer = await get_channel_peer()
        stream = AudioPiped(str(fpath), AudioParameters.from_quality("high"))
        await calls.change_stream(CHANNEL, stream)
        is_playing = True
    except Exception as e:
        print(f"[play_next error] {e}")
        is_playing = False

@calls.on_stream_end()
async def on_end(_, __):
    global current_index
    current_index += 1
    if current_index >= len(queue):
        current_index = 0          # loop back
    await play_next()

# ── WEB AUTH ──────────────────────────────────────────────────────────────────
security = HTTPBasic()

def require_auth(creds: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest(creds.password.encode(), WEB_PASS.encode())
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Wrong password",
                            headers={"WWW-Authenticate": "Basic"})
    return creds.username

# ── FASTAPI APP ───────────────────────────────────────────────────────────────
web = FastAPI(title="TG VC Controller")
web.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── UI ───────────────────────────────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TG VC Controller</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f0f0f;color:#e8e8e8;min-height:100vh;padding:24px}
  h1{font-size:20px;font-weight:600;margin-bottom:4px;color:#fff}
  .sub{font-size:13px;color:#666;margin-bottom:28px}
  .card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:20px;margin-bottom:16px}
  .card h2{font-size:13px;font-weight:500;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  button{padding:9px 18px;border-radius:8px;border:none;font-size:14px;font-weight:500;cursor:pointer;transition:opacity .15s}
  button:hover{opacity:.82} button:disabled{opacity:.35;cursor:not-allowed}
  .btn-green{background:#22c55e;color:#000}
  .btn-red{background:#ef4444;color:#fff}
  .btn-blue{background:#3b82f6;color:#fff}
  .btn-gray{background:#2a2a2a;color:#e8e8e8;border:1px solid #3a3a3a}
  .status-dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px}
  .dot-on{background:#22c55e;box-shadow:0 0 6px #22c55e}
  .dot-off{background:#555}
  .status-bar{display:flex;align-items:center;font-size:14px;margin-bottom:14px}
  .queue-list{list-style:none;display:flex;flex-direction:column;gap:6px;max-height:340px;overflow-y:auto}
  .queue-item{display:flex;align-items:center;gap:10px;background:#222;border:1px solid #2e2e2e;border-radius:8px;padding:9px 12px;font-size:13px}
  .queue-item.playing{border-color:#3b82f6;background:#1a2540}
  .q-num{color:#555;font-size:12px;min-width:22px}
  .q-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .q-del{background:none;border:none;color:#555;font-size:17px;cursor:pointer;padding:0 4px;line-height:1}
  .q-del:hover{color:#ef4444}
  input[type=file]{display:none}
  .upload-label{display:inline-flex;align-items:center;gap:7px;padding:9px 18px;background:#2a2a2a;border:1px solid #3a3a3a;border-radius:8px;font-size:14px;font-weight:500;cursor:pointer;transition:opacity .15s}
  .upload-label:hover{opacity:.8}
  .msg{font-size:13px;color:#888;margin-top:10px;min-height:18px}
  .channel-badge{background:#1e2d3d;color:#60a5fa;font-size:12px;padding:4px 10px;border-radius:6px;font-family:monospace}
  .progress-bar{height:3px;background:#2a2a2a;border-radius:2px;margin-top:14px}
  .progress-fill{height:3px;background:#3b82f6;border-radius:2px;transition:width .4s}
  .skip-btns{display:flex;gap:8px}
  label.upload-label input{display:none}
</style>
</head>
<body>
<h1>📡 TG Voice Chat</h1>
<p class="sub">Channel: <span class="channel-badge" id="chan">—</span></p>

<div class="card">
  <h2>Status</h2>
  <div class="status-bar">
    <span class="status-dot" id="dot"></span>
    <span id="status-text">Loading…</span>
  </div>
  <div id="now-playing" style="font-size:13px;color:#888;margin-bottom:14px"></div>
  <div class="row">
    <button class="btn-green" id="btn-start" onclick="startVC()">▶ Start VC</button>
    <button class="btn-red"   id="btn-stop"  onclick="stopVC()">■ Stop VC</button>
    <div class="skip-btns">
      <button class="btn-gray" onclick="skip(-1)">⏮ Prev</button>
      <button class="btn-gray" onclick="skip(1)">⏭ Next</button>
    </div>
  </div>
  <div class="progress-bar"><div class="progress-fill" id="pbar" style="width:0%"></div></div>
</div>

<div class="card">
  <h2>Queue <span id="q-count" style="color:#555;font-weight:400"></span></h2>
  <ul class="queue-list" id="q-list"></ul>
</div>

<div class="card">
  <h2>Upload Voice</h2>
  <div class="row">
    <label class="upload-label">
      📂 Choose Files
      <input type="file" id="file-input" accept="audio/*" multiple onchange="uploadFiles()"/>
    </label>
    <span id="upload-msg" class="msg"></span>
  </div>
</div>

<script>
const api = '';
let state = {};

function b64(p){return btoa('user:'+p)}
function getPass(){
  let p=sessionStorage.getItem('vc_pass');
  if(!p){p=prompt('Password:');sessionStorage.setItem('vc_pass',p);}
  return p;
}

async function req(path,opts={}){
  const pass=getPass();
  const headers={'Authorization':'Basic '+b64(pass),...(opts.headers||{})};
  const r=await fetch(api+path,{...opts,headers});
  if(r.status===401){sessionStorage.removeItem('vc_pass');location.reload();}
  return r;
}

async function fetchState(){
  try{
    const r=await req('/state');
    state=await r.json();
    render();
  }catch(e){}
}

function render(){
  document.getElementById('chan').textContent=state.channel||'—';
  const on=state.vc_active;
  document.getElementById('dot').className='status-dot '+(on?'dot-on':'dot-off');
  document.getElementById('status-text').textContent=on?(state.is_playing?'Playing':'Connected, idle'):'Disconnected';
  document.getElementById('btn-start').disabled=on;
  document.getElementById('btn-stop').disabled=!on;

  const np=state.current_file||'';
  document.getElementById('now-playing').textContent=np?'▶ '+np:'';

  const pct=state.queue_length>0?((state.current_index+1)/state.queue_length*100):0;
  document.getElementById('pbar').style.width=Math.min(pct,100)+'%';

  const q=state.queue||[];
  document.getElementById('q-count').textContent='('+q.length+'/'+state.max_voices+')';
  const ul=document.getElementById('q-list');
  ul.innerHTML='';
  q.forEach((f,i)=>{
    const li=document.createElement('li');
    li.className='queue-item'+(i===state.current_index&&state.is_playing?' playing':'');
    li.innerHTML=`<span class="q-num">${i+1}</span><span class="q-name">${f}</span><button class="q-del" onclick="removeTrack('${encodeURIComponent(f)}')" title="Remove">✕</button>`;
    ul.appendChild(li);
  });
}

async function startVC(){
  await req('/start',{method:'POST'});
  setTimeout(fetchState,800);
}
async function stopVC(){
  await req('/stop',{method:'POST'});
  setTimeout(fetchState,800);
}
async function skip(dir){
  await req('/skip?dir='+dir,{method:'POST'});
  setTimeout(fetchState,600);
}
async function removeTrack(name){
  await req('/queue/'+name,{method:'DELETE'});
  fetchState();
}

async function uploadFiles(){
  const input=document.getElementById('file-input');
  const msg=document.getElementById('upload-msg');
  if(!input.files.length)return;
  msg.textContent='Uploading…';
  let ok=0,fail=0;
  for(const f of input.files){
    const fd=new FormData();
    fd.append('file',f);
    const r=await req('/upload',{method:'POST',body:fd});
    if(r.ok)ok++;else fail++;
  }
  msg.textContent=`✓ ${ok} uploaded${fail?' · '+fail+' failed':''}`;
  input.value='';
  fetchState();
  setTimeout(()=>{msg.textContent=''},4000);
}

fetchState();
setInterval(fetchState,3000);
</script>
</body>
</html>"""

@web.get("/", response_class=HTMLResponse)
async def ui():
    return HTML_PAGE

@web.get("/state")
async def get_state(_=Depends(require_auth)):
    cur = queue[current_index] if queue and current_index < len(queue) else None
    return {
        "vc_active": vc_active,
        "is_playing": is_playing,
        "channel": CHANNEL,
        "queue": queue,
        "queue_length": len(queue),
        "current_index": current_index,
        "current_file": cur,
        "max_voices": MAX_VOICES,
    }

@web.post("/start")
async def start_vc(_=Depends(require_auth)):
    global vc_active
    if vc_active:
        return {"ok": True, "msg": "already active"}
    try:
        if queue:
            await play_next()
        else:
            # join silently with no stream
            peer = await get_channel_peer()
            await calls.join_group_call(CHANNEL,
                AudioPiped("", AudioParameters.from_quality("high")),
            )
            vc_active = True
    except NoActiveGroupCall:
        raise HTTPException(400, "No active voice chat in channel. Start one from Telegram first.")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True}

@web.post("/stop")
async def stop_vc(_=Depends(require_auth)):
    global vc_active, is_playing
    try:
        await calls.leave_group_call(CHANNEL)
    except Exception:
        pass
    vc_active = False
    is_playing = False
    return {"ok": True}

@web.post("/skip")
async def skip_track(dir: int = 1, _=Depends(require_auth)):
    global current_index
    current_index = max(0, current_index + dir)
    if current_index >= len(queue):
        current_index = 0
    if vc_active:
        await play_next()
    return {"ok": True, "index": current_index}

@web.post("/upload")
async def upload_voice(file: UploadFile = File(...), _=Depends(require_auth)):
    if len(queue) >= MAX_VOICES:
        raise HTTPException(400, f"Queue full ({MAX_VOICES} max). Remove tracks first.")
    ext = Path(file.filename).suffix.lower()
    if ext not in {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus"}:
        raise HTTPException(400, "Unsupported format. Use mp3/ogg/wav/flac/m4a/opus.")
    # safe filename
    safe = "".join(c for c in file.filename if c.isalnum() or c in "._- ").strip()
    dest = AUDIO_DIR / safe
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    if safe not in queue:
        queue.append(safe)
    return {"ok": True, "file": safe, "queue_length": len(queue)}

@web.delete("/queue/{filename}")
async def remove_from_queue(filename: str, _=Depends(require_auth)):
    global current_index
    fname = filename  # already decoded by FastAPI
    if fname in queue:
        idx = queue.index(fname)
        queue.remove(fname)
        try:
            (AUDIO_DIR / fname).unlink(missing_ok=True)
        except Exception:
            pass
        if current_index >= idx and current_index > 0:
            current_index -= 1
    return {"ok": True, "queue": queue}

# ── ENTRYPOINT ────────────────────────────────────────────────────────────────
async def main():
    print(f"[TG-VC] Starting userbot…")
    await app_client.start()
    await calls.start()
    print(f"[TG-VC] Userbot ready. Channel: {CHANNEL}")
    print(f"[TG-VC] Web dashboard → http://0.0.0.0:{WEB_PORT}")
    config = uvicorn.Config(web, host="0.0.0.0", port=WEB_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
