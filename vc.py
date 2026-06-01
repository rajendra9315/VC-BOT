import os, asyncio, json, base64, io, shutil
from pathlib import Path
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

from pyrogram import Client as PyroClient
from pyrogram.errors import SessionPasswordNeeded as PyroPasswordNeeded
from tgcaller import TgCaller
from tgcaller.types import AudioConfig

import qrcode

load_dotenv()

# ---------- CONFIG ----------
PORT = int(os.getenv("PORT", 8000))
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
PHONE = os.getenv("PHONE_NUMBER")
TWO_STEP_PASSWORD = os.getenv("TWO_STEP_PASSWORD")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID"))
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin")

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
TELE_SESSION_FILE = "telethon.session"
PYRO_SESSION_FILE = "pyrogram.session"

# ---------- GLOBAL STATE ----------
tele_client: TelegramClient = None
pyro_client: PyroClient = None
caller: TgCaller = None
playlist: list[str] = []
current_index = 0
is_playing = False
login_state = {"status": "disconnected", "qr_data": None}

# ---------- FASTAPI APP ----------
app = FastAPI()
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- TELEGRAM CLIENT (Telethon for Messaging) ----------
async def get_tele_client():
    global tele_client
    if tele_client is None:
        session_str = None
        if os.path.exists(TELE_SESSION_FILE):
            with open(TELE_SESSION_FILE, "r") as f:
                session_str = f.read()
        tele_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await tele_client.connect()
    return tele_client

async def save_tele_session():
    if tele_client:
        session_str = tele_client.session.save()
        with open(TELE_SESSION_FILE, "w") as f:
            f.write(session_str)

async def complete_tele_login(code: str = None, password: str = None):
    global login_state
    client = await get_tele_client()
    try:
        if code:
            await client.sign_in(PHONE, code)
        elif password:
            await client.sign_in(password=password)
        else:
            return {"error": "Missing credentials"}
        login_state["status"] = "logged_in"
        await save_tele_session()
        await init_pyrogram_and_caller()
        return {"status": "success"}
    except SessionPasswordNeededError:
        login_state["status"] = "wait_password"
        return {"status": "2fa_needed"}
    except Exception as e:
        login_state["status"] = "error"
        return {"error": str(e)}

# ---------- PYROGRAM + TgCaller (for Voice) ----------
async def init_pyrogram_and_caller():
    global pyro_client, caller
    if pyro_client is not None:
        return
    session_str = None
    if os.path.exists(PYRO_SESSION_FILE):
        with open(PYRO_SESSION_FILE, "r") as f:
            session_str = f.read()
    pyro_client = PyroClient(
        "voice_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_str,
        in_memory=False
    )
    await pyro_client.start()
    if not await pyro_client.is_user_authorized():
        try:
            await pyro_client.send_code(PHONE)
            raise Exception("Pyrogram not authorized. Use /api/pyro_login first.")
        except Exception as e:
            if TWO_STEP_PASSWORD:
                try:
                    await pyro_client.sign_in(PHONE, code=input("Pyrogram code? "))
                except:
                    pass
            raise
    session_str = await pyro_client.export_session_string()
    with open(PYRO_SESSION_FILE, "w") as f:
        f.write(session_str)

    # Initialize TgCaller
    caller = TgCaller(pyro_client)
    await caller.start()

    @caller.on_stream_end
    async def on_stream_end(client, update):
        await play_next()

async def start_voice_chat():
    global is_playing, current_index
    if not playlist:
        raise Exception("No audio files uploaded")
    await init_pyrogram_and_caller()
    if not caller.is_connected(CHANNEL_ID):
        await caller.join_call(CHANNEL_ID)
    await caller.play(CHANNEL_ID, playlist[0])
    is_playing = True
    current_index = 1

async def stop_voice_chat():
    global is_playing
    try:
        await caller.leave_call(CHANNEL_ID)
        is_playing = False
    except Exception as e:
        print(f"Error leaving call: {e}")
        is_playing = False

async def play_next():
    global current_index, is_playing, playlist
    if not is_playing or not playlist:
        return
    if current_index >= len(playlist):
        current_index = 0
    file_path = playlist[current_index]
    try:
        await caller.play(CHANNEL_ID, file_path)
        current_index += 1
    except Exception as e:
        print(f"Play error: {e}")

# ---------- API ENDPOINTS ----------
@app.post("/api/login")
async def api_login(username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        return {"success": True}
    raise HTTPException(401, "Invalid credentials")

@app.get("/api/status")
async def api_status():
    tele = await get_tele_client()
    auth = await tele.is_user_authorized()
    pyro_ok = pyro_client is not None and await pyro_client.is_user_authorized()
    return {
        "authorized": auth,
        "pyro_authorized": pyro_ok,
        "login_state": login_state["status"],
        "qr_data": login_state.get("qr_data"),
        "vc_active": is_playing,
        "playlist": [Path(p).name for p in playlist],
        "current_track": Path(playlist[current_index-1]).name if is_playing and current_index>0 else None
    }

@app.post("/api/send_code")
async def api_send_code():
    global login_state
    client = await get_tele_client()
    if PHONE:
        try:
            await client.send_code_request(PHONE)
            login_state["status"] = "wait_code"
            await save_tele_session()
            return {"status": "code_sent"}
        except Exception as e:
            login_state["status"] = "error"
            return JSONResponse({"error": str(e)}, status_code=400)
    else:
        qr_login = await client.qr_login()
        img = qrcode.make(qr_login.url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        login_state["status"] = "wait_qr"
        login_state["qr_data"] = f"data:image/png;base64,{b64}"
        login_state["qr_obj"] = qr_login
        return {"status": "qr_ready", "qr_image": login_state["qr_data"]}

@app.post("/api/submit_code")
async def submit_code(code: str = Form(...)):
    return await complete_tele_login(code=code)

@app.post("/api/submit_password")
async def submit_password(password: str = Form(...)):
    return await complete_tele_login(password=password)

@app.post("/api/pyro_login")
async def pyro_login_step(code: str = Form(None), password: str = Form(None)):
    global pyro_client, caller
    try:
        if code:
            await pyro_client.sign_in(PHONE, code=code)
        elif password:
            await pyro_client.sign_in(password=password)
        else:
            await pyro_client.send_code(PHONE)
            return {"status": "code_sent"}
        session_str = await pyro_client.export_session_string()
        with open(PYRO_SESSION_FILE, "w") as f:
            f.write(session_str)
        caller = TgCaller(pyro_client)
        await caller.start()
        @caller.on_stream_end
        async def on_stream_end(client, update):
            await play_next()
        return {"status": "success"}
    except PyroPasswordNeeded:
        return {"status": "2fa_needed"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/start_vc")
async def start_vc():
    try:
        await start_voice_chat()
        return {"status": "started"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/stop_vc")
async def stop_vc():
    try:
        await stop_voice_chat()
        return {"status": "stopped"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(('.mp3', '.ogg', '.m4a')):
        raise HTTPException(400, "Only MP3/OGG/M4A allowed")
    file_path = UPLOAD_DIR / file.filename
    with open(file_path, "wb") as f:
        f.write(await file.read())
    playlist.append(str(file_path))
    return {"success": True, "filename": file.filename, "playlist": [p.name for p in playlist]}

@app.post("/api/reorder")
async def reorder(data: dict):
    global playlist
    order = data.get("order", [])
    full_new = []
    for fname in order:
        path = UPLOAD_DIR / fname
        if path.exists():
            full_new.append(str(path))
    playlist = full_new
    return {"status": "reordered"}

@app.delete("/api/delete/{filename}")
async def delete_file(filename: str):
    global playlist
    file_path = UPLOAD_DIR / filename
    if file_path.exists():
        file_path.unlink()
    playlist = [p for p in playlist if os.path.basename(p) != filename]
    return {"status": "deleted"}

@app.post("/api/post_from_source")
async def post_from_source(message_id: int = Form(...)):
    client = await get_tele_client()
    try:
        msg = await client.get_messages(SOURCE_CHANNEL_ID, ids=message_id)
        if not msg:
            raise HTTPException(404, "Message not found")
        await client.send_message(
            CHANNEL_ID,
            message=msg.text,
            file=msg.media,
            formatting_entities=msg.entities,
            link_preview=False
        )
        return {"status": "posted"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

@app.post("/api/post_custom")
async def post_custom(text: str = Form(""), file: UploadFile = File(None)):
    client = await get_tele_client()
    try:
        media = None
        if file:
            tmp_path = UPLOAD_DIR / f"temp_{file.filename}"
            with open(tmp_path, "wb") as f:
                f.write(await file.read())
            media = tmp_path
        await client.send_message(CHANNEL_ID, message=text, file=media)
        if media:
            os.remove(media)
        return {"status": "posted"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

# ---------- EMBEDDED WEB UI (Full Admin Panel) ----------
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VC Admin Panel</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:#0a0a14;color:#fff;min-height:100vh}
#particles{position:fixed;top:0;left:0;width:100%;height:100%;z-index:-1}
.glass{background:rgba(20,20,40,0.7);backdrop-filter:blur(16px);border:1px solid rgba(255,255,255,0.08);border-radius:24px;padding:2rem}
.login-container,.dashboard{max-width:800px;margin:2rem auto;animation:fadeIn .5s}
.hidden{display:none}
input,textarea,button,.file-label{width:100%;padding:12px 16px;margin:8px 0;background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.1);border-radius:14px;color:#fff;font-size:1rem;transition:.3s}
button{background:linear-gradient(135deg,#7f00ff,#e100ff);cursor:pointer;font-weight:bold;border:none}
button:hover{transform:scale(1.02);opacity:0.9}
.tabs{display:flex;gap:12px;margin-bottom:2rem}
.tab{padding:12px 24px;border-radius:12px;background:rgba(255,255,255,0.05);cursor:pointer;font-weight:bold;transition:.3s}
.tab.active{background:linear-gradient(135deg,#7f00ff,#e100ff)}
.qr-image{width:180px;display:block;margin:1rem auto}
.playlist-item{display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.05);padding:12px;margin:6px 0;border-radius:12px}
.upload-zone{border:2px dashed rgba(255,255,255,0.2);border-radius:20px;padding:2rem;text-align:center;margin:1rem 0;transition:.3s}
.upload-zone.dragover{border-color:#7f00ff;background:rgba(127,0,255,0.1)}
.toast{position:fixed;bottom:20px;right:20px;background:#2a2a4a;padding:15px 25px;border-radius:12px;z-index:100;animation:slideUp .3s}
@keyframes fadeIn{from{opacity:0;transform:translateY(20px)}}
@keyframes slideUp{from{transform:translateY(80px);opacity:0}}
</style>
</head>
<body>
<canvas id="particles"></canvas>

<div id="loginPage" class="login-container glass">
  <h2 style="text-align:center;margin-bottom:2rem;">🔐 Admin Login</h2>
  <input id="username" placeholder="Username">
  <input type="password" id="password" placeholder="Password">
  <button onclick="login()">Login</button>
  <p id="loginError" style="color:#f55;text-align:center;margin-top:12px"></p>
</div>

<div id="dashboard" class="dashboard glass hidden">
  <div class="tabs">
    <div class="tab active" onclick="switchTab('vc')">🎤 VC Control</div>
    <div class="tab" onclick="switchTab('sounds')">🎵 Sound Manager</div>
    <div class="tab" onclick="switchTab('messages')">💬 Message Reposter</div>
  </div>

  <div id="tab-vc">
    <div style="margin:1.5rem 0;padding:1rem;background:rgba(255,255,255,0.04);border-radius:16px">
      <p>Telethon Bot: <span id="botStatus">Checking...</span></p>
      <p>Pyrogram (VC): <span id="pyroStatus">Unknown</span></p>
      <p>VC: <span id="vcStatus">Stopped</span></p>
      <p id="currentTrack"></p>
    </div>
    <div style="display:flex;gap:1rem">
      <button onclick="startVC()">▶️ Start VC</button>
      <button onclick="stopVC()" style="background:linear-gradient(135deg,#ff416c,#ff4b2b)">⏹️ Stop VC</button>
    </div>
    <div id="loginSection" class="hidden" style="margin-top:2rem">
      <h3>Telegram Login (Telethon)</h3>
      <button onclick="sendCode()">📱 Send Code / QR</button>
      <div id="qrContainer" class="hidden"><img id="qrImage" class="qr-image"><p>Scan with Telegram</p></div>
      <div id="codeContainer" class="hidden">
        <input id="codeInput" placeholder="Enter code">
        <button onclick="submitCode()">Verify</button>
      </div>
      <div id="passwordContainer" class="hidden">
        <input type="password" id="password2FA" placeholder="2FA Password">
        <button onclick="submitPassword()">Verify</button>
      </div>
    </div>
    <div id="pyroSection" style="margin-top:2rem">
      <h3>Pyrogram Voice Login (if needed)</h3>
      <button onclick="pyroSendCode()">📱 Init Pyrogram</button>
      <div id="pyroCodeContainer" class="hidden">
        <input id="pyroCodeInput" placeholder="Enter code">
        <button onclick="pyroSubmitCode()">Verify Pyrogram</button>
      </div>
      <div id="pyroPasswordContainer" class="hidden">
        <input type="password" id="pyroPassword2FA" placeholder="2FA for Pyrogram">
        <button onclick="pyroSubmitPassword()">Verify 2FA</button>
      </div>
    </div>
  </div>

  <div id="tab-sounds" class="hidden">
    <h3>Upload Audio Files</h3>
    <div class="upload-zone" id="dropZone">
      <p>Drop MP3/OGG/M4A files here</p>
      <label for="fileInput" class="file-label" style="display:block;text-align:center;background:#7f00ff;border:none;margin-top:1rem">Browse</label>
      <input type="file" id="fileInput" multiple accept=".mp3,.ogg,.m4a" style="display:none">
    </div>
    <div id="playlistContainer"></div>
  </div>

  <div id="tab-messages" class="hidden">
    <h3>Repost from Source Channel</h3>
    <input type="number" id="sourceMsgId" placeholder="Source message ID">
    <button onclick="postFromSource()">📨 Repost to VC Channel</button>
    <hr style="margin:2rem 0;border-color:rgba(255,255,255,0.1)">
    <h3>Send Custom Message</h3>
    <textarea id="customText" placeholder="Your message..." rows="3"></textarea>
    <input type="file" id="customFile" accept="*">
    <button onclick="postCustom()">✉️ Send to VC Channel</button>
  </div>
</div>

<script>
// Particles effect
const canvas=document.getElementById('particles');
const ctx=canvas.getContext('2d');
canvas.width=innerWidth;canvas.height=innerHeight;
let particles=[];
class Particle{constructor(){this.reset()}reset(){this.x=Math.random()*canvas.width;this.y=Math.random()*canvas.height;this.size=Math.random()*2+1;this.speedX=(Math.random()-0.5)*0.5;this.speedY=(Math.random()-0.5)*0.5;this.opacity=Math.random()*0.5+0.2}update(){this.x+=this.speedX;this.y+=this.speedY;if(this.x<0||this.x>canvas.width||this.y<0||this.y>canvas.height)this.reset()}draw(){ctx.beginPath();ctx.arc(this.x,this.y,this.size,0,Math.PI*2);ctx.fillStyle=`rgba(127,0,255,${this.opacity})`;ctx.fill()}}
for(let i=0;i<80;i++)particles.push(new Particle());
function anim(){ctx.clearRect(0,0,canvas.width,canvas.height);particles.forEach(p=>{p.update();p.draw()});requestAnimationFrame(anim)}anim();
addEventListener('resize',()=>{canvas.width=innerWidth;canvas.height=innerHeight});

function toast(msg){let t=document.createElement('div');t.className='toast';t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),3000)}

function switchTab(tab){
  ['vc','sounds','messages'].forEach(t=>document.getElementById('tab-'+t).classList.add('hidden'));
  document.getElementById('tab-'+tab).classList.remove('hidden');
  document.querySelectorAll('.tab').forEach((el,i)=>{el.classList.toggle('active', i===['vc','sounds','messages'].indexOf(tab))});
}

async function login(){
  let u=document.getElementById('username').value,p=document.getElementById('password').value;
  let r=await fetch('/api/login',{method:'POST',body:new URLSearchParams({username:u,password:p})});
  if(r.ok){localStorage.setItem('logged','1');document.getElementById('loginPage').classList.add('hidden');document.getElementById('dashboard').classList.remove('hidden');loadStatus();setInterval(loadStatus,5000)}
  else document.getElementById('loginError').textContent='Invalid credentials';
}

async function loadStatus(){
  let r=await fetch('/api/status'),d=await r.json();
  document.getElementById('botStatus').textContent=d.authorized?'Connected ✅':'Disconnected ❌';
  document.getElementById('pyroStatus').textContent=d.pyro_authorized?'Ready ✅':'Not logged in';
  document.getElementById('vcStatus').textContent=d.vc_active?'Playing 🔊':'Stopped';
  document.getElementById('currentTrack').textContent=d.current_track?'Now: '+d.current_track:'';
  if(!d.authorized)document.getElementById('loginSection').classList.remove('hidden');
  else document.getElementById('loginSection').classList.add('hidden');
  renderPlaylist(d.playlist);
}
function renderPlaylist(list){
  let c=document.getElementById('playlistContainer');
  c.innerHTML=list.map((n,i)=>`<div class="playlist-item"><span>🎵 ${n}</span><div><button onclick="delFile('${n}')">🗑️</button></div></div>`).join('');
}
async function delFile(name){await fetch('/api/delete/'+name,{method:'DELETE'});loadStatus()}

async function sendCode(){
  let r=await fetch('/api/send_code',{method:'POST'}),d=await r.json();
  if(d.status=='code_sent'){document.getElementById('codeContainer').classList.remove('hidden')}
  else if(d.qr_image){document.getElementById('qrImage').src=d.qr_image;document.getElementById('qrContainer').classList.remove('hidden')}
}
async function submitCode(){
  let code=document.getElementById('codeInput').value;
  let r=await fetch('/api/submit_code',{method:'POST',body:new URLSearchParams({code})});
  let d=await r.json();
  if(d.status=='2fa_needed')document.getElementById('passwordContainer').classList.remove('hidden');
  else if(r.ok){toast('Logged in');loadStatus()}
}
async function submitPassword(){
  let p=document.getElementById('password2FA').value;
  let r=await fetch('/api/submit_password',{method:'POST',body:new URLSearchParams({password:p})});
  if(r.ok){toast('2FA verified');loadStatus()}
}
async function startVC(){
  let r=await fetch('/api/start_vc',{method:'POST'});
  if(r.ok)toast('VC started');else{let e=await r.json();toast('Error: '+e.error)}
  loadStatus();
}
async function stopVC(){
  let r=await fetch('/api/stop_vc',{method:'POST'});
  if(r.ok)toast('VC stopped');
  loadStatus();
}
// Pyrogram login steps
async function pyroSendCode(){
  let r=await fetch('/api/pyro_login',{method:'POST'});
  let d=await r.json();
  if(d.status=='code_sent') document.getElementById('pyroCodeContainer').classList.remove('hidden');
  else toast('Error: '+d.error);
}
async function pyroSubmitCode(){
  let code=document.getElementById('pyroCodeInput').value;
  let r=await fetch('/api/pyro_login',{method:'POST',body:new URLSearchParams({code})});
  let d=await r.json();
  if(d.status=='2fa_needed') document.getElementById('pyroPasswordContainer').classList.remove('hidden');
  else if(r.ok){toast('Pyrogram ready');loadStatus();}
}
async function pyroSubmitPassword(){
  let p=document.getElementById('pyroPassword2FA').value;
  let r=await fetch('/api/pyro_login',{method:'POST',body:new URLSearchParams({password:p})});
  if(r.ok){toast('Pyrogram ready');loadStatus();}
}
// Uploads
let dz=document.getElementById('dropZone');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('dragover')});
dz.addEventListener('dragleave',()=>dz.classList.remove('dragover'));
dz.addEventListener('drop',async e=>{e.preventDefault();dz.classList.remove('dragover');for(let f of e.dataTransfer.files)await upload(f)});
document.getElementById('fileInput').addEventListener('change',async e=>{for(let f of e.target.files)await upload(f)});
async function upload(file){
  let fd=new FormData();fd.append('file',file);
  let r=await fetch('/api/upload',{method:'POST',body:fd});
  if(r.ok){toast('Uploaded: '+file.name);loadStatus()}else toast('Upload failed');
}
// Message reposting
async function postFromSource(){
  let id=document.getElementById('sourceMsgId').value;
  let r=await fetch('/api/post_from_source',{method:'POST',body:new URLSearchParams({message_id:id})});
  if(r.ok)toast('Message reposted!');else{let e=await r.json();toast('Error: '+e.error)}
}
async function postCustom(){
  let text=document.getElementById('customText').value;
  let file=document.getElementById('customFile').files[0];
  let fd=new FormData();fd.append('text',text);if(file)fd.append('file',file);
  let r=await fetch('/api/post_custom',{method:'POST',body:fd});
  if(r.ok)toast('Message sent!');else{let e=await r.json();toast('Error: '+e.error)}
}

window.onload=()=>{if(localStorage.getItem('logged')){document.getElementById('loginPage').classList.add('hidden');document.getElementById('dashboard').classList.remove('hidden');loadStatus();setInterval(loadStatus,5000)}}
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML

# ---------- STARTUP ----------
@app.on_event("startup")
async def startup():
    await get_tele_client()
    if await tele_client.is_user_authorized():
        login_state["status"] = "logged_in"
        await init_pyrogram_and_caller()

def run():
    uvicorn.run(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    run()
