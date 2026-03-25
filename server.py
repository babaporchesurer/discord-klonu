from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
import psycopg2
import json
import hashlib
from datetime import datetime
import os

DATABASE_URL = os.environ.get("DATABASE_URL")
app = FastAPI()

@app.get("/")
@app.head("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(html_content)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    conn.autocommit = True # Hata almadan tabloları güncelleyebilmek için
    cursor = conn.cursor()
    
    # 1. Tabloları Oluştur (Eğer yoklarsa)
    try: cursor.execute('''CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, username TEXT, text TEXT, time TEXT, channel TEXT, media_data TEXT, media_type TEXT, server_name TEXT DEFAULT 'Ana Sunucu')''')
    except: pass
    try: cursor.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, role TEXT, is_banned BOOLEAN DEFAULT FALSE, display_name TEXT, profile_pic TEXT)''')
    except: pass
    try: cursor.execute('''CREATE TABLE IF NOT EXISTS servers (id SERIAL PRIMARY KEY, name TEXT UNIQUE, icon_data TEXT, owner TEXT)''')
    except: pass
    try: cursor.execute('''CREATE TABLE IF NOT EXISTS channels (id SERIAL PRIMARY KEY, name TEXT, is_locked BOOLEAN DEFAULT FALSE, password TEXT, server_name TEXT DEFAULT 'Ana Sunucu')''')
    except: pass
    try: cursor.execute('''CREATE TABLE IF NOT EXISTS reports (id SERIAL PRIMARY KEY, reporter TEXT, reported_user TEXT, reason TEXT, time TEXT)''')
    except: pass

    # 2. Eski veritabanını Yeni Multi-Server mantığına güncelle (Hata verirse geçer)
    try: cursor.execute("ALTER TABLE messages ADD COLUMN server_name TEXT DEFAULT 'Ana Sunucu'")
    except: pass
    try: cursor.execute("ALTER TABLE channels ADD COLUMN server_name TEXT DEFAULT 'Ana Sunucu'")
    except: pass
    
    # 3. Varsayılan (İlk) verileri oluştur
    try: 
        cursor.execute("SELECT * FROM users WHERE username = 'admin'")
        if not cursor.fetchone(): cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)", ("admin", hashlib.sha256("admin123".encode()).hexdigest(), "admin"))
    except: pass
    
    try:
        cursor.execute("SELECT COUNT(*) FROM servers")
        if cursor.fetchone()[0] == 0: cursor.execute("INSERT INTO servers (name, icon_data, owner) VALUES ('Ana Sunucu', '', 'admin')")
    except: pass

    try:
        cursor.execute("SELECT COUNT(*) FROM channels")
        if cursor.fetchone()[0] == 0: cursor.execute("INSERT INTO channels (name, server_name) VALUES ('genel-sohbet', 'Ana Sunucu'), ('valorant-tayfa', 'Ana Sunucu')")
    except: pass

    conn.close()

init_db()

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[WebSocket, str] = {} 
        self.voice_users: dict[str, dict] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[websocket] = None

    async def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            uname = self.active_connections[websocket]
            del self.active_connections[websocket]
            await self.broadcast_online_users()
            if uname in self.voice_users:
                del self.voice_users[uname]
                await self.broadcast_voice_users()

    async def broadcast(self, message: str):
        for connection in list(self.active_connections.keys()):
            try: await connection.send_text(message)
            except: pass
            
    async def send_to_user(self, target_username: str, message: str):
        for ws, uname in list(self.active_connections.items()):
            if uname == target_username:
                try: await ws.send_text(message)
                except: pass

    async def broadcast_online_users(self):
        online_users = list(set([user for user in self.active_connections.values() if user]))
        await self.broadcast(json.dumps({"type": "online_list", "users": online_users}))
        
    async def broadcast_voice_users(self):
        v_users = [{"username": k, "display_name": v["display_name"], "profile_pic": v["profile_pic"], "server_name": v.get("server_name")} for k, v in self.voice_users.items()]
        await self.broadcast(json.dumps({"type": "voice_list", "users": v_users}))

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            packet = json.loads(data)
            
            if packet.get("type") == "register":
                username = packet.get("username"); password = packet.get("password")
                hashed_pw = hashlib.sha256(password.encode()).hexdigest()
                conn = get_db_connection(); cursor = conn.cursor()
                try:
                    cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
                    if cursor.fetchone(): await websocket.send_text(json.dumps({"type": "register_response", "success": False, "error_msg": "Bu kullanıcı adı alınmış!"}))
                    else:
                        cursor.execute("INSERT INTO users (username, password_hash, role, is_banned) VALUES (%s, %s, %s, FALSE)", (username, hashed_pw, "user"))
                        conn.commit(); await websocket.send_text(json.dumps({"type": "register_response", "success": True}))
                except Exception: await websocket.send_text(json.dumps({"type": "register_response", "success": False, "error_msg": "Hata!"}))
                finally: conn.close()

            elif packet.get("type") == "login":
                username = packet.get("username"); password = packet.get("password")
                hashed_pw = hashlib.sha256(password.encode()).hexdigest()
                conn = get_db_connection(); cursor = conn.cursor()
                cursor.execute("SELECT role, is_banned, display_name, profile_pic FROM users WHERE username = %s AND password_hash = %s", (username, hashed_pw))
                user = cursor.fetchone()
                if user:
                    if user[1]: await websocket.send_text(json.dumps({"type": "login_response", "success": False, "error_msg": "BANLANDINIZ!"}))
                    else:
                        manager.active_connections[websocket] = username
                        await websocket.send_text(json.dumps({"type": "login_response", "success": True, "role": user[0], "display_name": user[2] or username, "profile_pic": user[3] or ""}))
                        
                        # YENİ: Giriş yapınca tüm sunucu listesini gönder
                        cursor.execute("SELECT name, icon_data FROM servers ORDER BY id ASC")
                        s_list = [{"name": r[0], "icon": r[1]} for r in cursor.fetchall()]
                        await websocket.send_text(json.dumps({"type": "server_list", "servers": s_list}))
                        
                        await manager.broadcast_online_users()
                        await websocket.send_text(json.dumps({"type": "voice_list", "users": [{"username": k, "display_name": v["display_name"], "profile_pic": v["profile_pic"], "server_name": v.get("server_name")} for k, v in manager.voice_users.items()]}))
                else: await websocket.send_text(json.dumps({"type": "login_response", "success": False, "error_msg": "Hatalı şifre!"}))
                conn.close()

            # YENİ: Bir Sunucuya Tıklandığında Kanallarını Yükle
            elif packet.get("type") == "load_server":
                s_name = packet.get("server_name")
                conn = get_db_connection(); cursor = conn.cursor()
                cursor.execute("SELECT name, is_locked FROM channels WHERE server_name = %s ORDER BY id ASC", (s_name,))
                ch_list = [{"name": r[0], "is_locked": r[1]} for r in cursor.fetchall()]
                conn.close()
                await websocket.send_text(json.dumps({"type": "channel_list", "channels": ch_list, "server_name": s_name}))

            elif packet.get("type") == "load_history":
                ch_name, pwd_attempt, s_name = packet.get("channel"), packet.get("password", ""), packet.get("server_name")
                my_user = manager.active_connections.get(websocket)
                conn = get_db_connection(); cursor = conn.cursor()
                
                if ch_name.startswith("@"):
                    target_user = ch_name[1:]
                    cursor.execute("SELECT m.username, m.text, m.time, m.media_data, m.media_type, u.display_name, u.profile_pic FROM messages m LEFT JOIN users u ON m.username = u.username WHERE (m.channel = %s AND m.username = %s) OR (m.channel = %s AND m.username = %s) ORDER BY m.id ASC", (f"@{target_user}", my_user, f"@{my_user}", target_user))
                else:
                    cursor.execute("SELECT is_locked, password FROM channels WHERE name = %s AND server_name = %s", (ch_name, s_name))
                    ch_info = cursor.fetchone()
                    if ch_info and ch_info[0] and ch_info[1] != pwd_attempt:
                        await websocket.send_text(json.dumps({"type": "system_error", "message": "Yanlış kanal şifresi!"}))
                        conn.close(); continue
                    cursor.execute("SELECT m.username, m.text, m.time, m.media_data, m.media_type, u.display_name, u.profile_pic FROM messages m LEFT JOIN users u ON m.username = u.username WHERE m.channel = %s AND m.server_name = %s ORDER BY m.id ASC", (ch_name, s_name))
                
                history_list = [{"username": r[0], "text": r[1], "time": r[2], "media_data": r[3], "media_type": r[4], "display_name": r[5] or r[0], "profile_pic": r[6] or ""} for r in cursor.fetchall()]
                conn.close()
                await websocket.send_text(json.dumps({"type": "history", "messages": history_list, "channel": ch_name, "server_name": s_name}))
            
            elif packet.get("type") == "message":
                time_string = datetime.now().strftime("%H:%M")
                conn = get_db_connection(); cursor = conn.cursor()
                cursor.execute("INSERT INTO messages (username, text, time, channel, media_data, media_type, server_name) VALUES (%s, %s, %s, %s, %s, %s, %s)", (packet['username'], packet['text'], time_string, packet['channel'], packet.get('media_data'), packet.get('media_type'), packet.get('server_name')))
                conn.commit(); conn.close()
                packet['time'] = time_string
                await manager.broadcast(json.dumps(packet))
                
            # YENİ: SUNUCU OLUŞTURMA
            elif packet.get("type") == "create_server":
                s_name, s_icon = packet.get("server_name"), packet.get("server_icon")
                my_user = manager.active_connections.get(websocket)
                conn = get_db_connection(); cursor = conn.cursor()
                try:
                    cursor.execute("INSERT INTO servers (name, icon_data, owner) VALUES (%s, %s, %s)", (s_name, s_icon, my_user))
                    cursor.execute("INSERT INTO channels (name, server_name) VALUES ('genel', %s)", (s_name,))
                    conn.commit()
                    cursor.execute("SELECT name, icon_data FROM servers ORDER BY id ASC")
                    s_list = [{"name": r[0], "icon": r[1]} for r in cursor.fetchall()]
                    await manager.broadcast(json.dumps({"type": "server_list", "servers": s_list}))
                    await websocket.send_text(json.dumps({"type": "system_msg", "message": "Sunucu Kuruldu!"}))
                except Exception: await websocket.send_text(json.dumps({"type": "system_error", "message": "Sunucu adı kullanılıyor!"}))
                finally: conn.close()

            elif packet.get("type") == "join_voice":
                my_user = manager.active_connections.get(websocket)
                manager.voice_users[my_user] = {"display_name": packet.get("display_name"), "profile_pic": packet.get("profile_pic"), "server_name": packet.get("server_name")}
                await manager.broadcast_voice_users()
                for ws, uname in list(manager.active_connections.items()):
                    if uname and uname != my_user: await ws.send_text(json.dumps({"type": "user_joined_voice", "username": my_user}))
                        
            elif packet.get("type") == "leave_voice":
                my_user = manager.active_connections.get(websocket)
                if my_user in manager.voice_users: del manager.voice_users[my_user]
                await manager.broadcast_voice_users()
            
            elif packet.get("type") in ["webrtc_offer", "webrtc_answer", "webrtc_ice"]:
                target = packet.get("target")
                await manager.send_to_user(target, json.dumps(packet))

            elif packet.get("type") == "report_user":
                conn = get_db_connection(); cursor = conn.cursor()
                cursor.execute("INSERT INTO reports (reporter, reported_user, reason, time) VALUES (%s, %s, %s, %s)", (packet['reporter'], packet['reported'], packet['reason'], datetime.now().strftime("%d-%m-%Y %H:%M")))
                conn.commit(); conn.close()
                await websocket.send_text(json.dumps({"type": "system_msg", "message": "Şikayet iletildi."}))

            elif packet.get("type") == "get_reports":
                conn = get_db_connection(); cursor = conn.cursor()
                cursor.execute("SELECT reporter, reported_user, reason, time FROM reports ORDER BY id DESC")
                reps = [{"reporter": r[0], "reported": r[1], "reason": r[2], "time": r[3]} for r in cursor.fetchall()]
                conn.close()
                await websocket.send_text(json.dumps({"type": "report_list", "reports": reps}))

            elif packet.get("type") == "update_profile":
                conn = get_db_connection(); cursor = conn.cursor()
                cursor.execute("UPDATE users SET display_name = %s, profile_pic = %s WHERE username = %s", (packet['display_name'], packet['profile_pic'], packet['username']))
                conn.commit(); conn.close()
                if packet['username'] in manager.voice_users:
                    manager.voice_users[packet['username']]['display_name'] = packet['display_name']
                    manager.voice_users[packet['username']]['profile_pic'] = packet['profile_pic']
                    await manager.broadcast_voice_users()
                await websocket.send_text(json.dumps({"type": "profile_updated", "display_name": packet['display_name'], "profile_pic": packet['profile_pic']}))

            elif packet.get("type") == "admin_user_action":
                action, target = packet.get("action"), packet.get("target")
                conn = get_db_connection(); cursor = conn.cursor()
                try:
                    if action == "ban": 
                        cursor.execute("UPDATE users SET is_banned = TRUE WHERE username = %s", (target,))
                        for ws, uname in list(manager.active_connections.items()):
                            if uname == target:
                                await ws.send_text(json.dumps({"type": "system_error", "message": "BANLANDINIZ!"}))
                                await ws.close() 
                    elif action == "unban": cursor.execute("UPDATE users SET is_banned = FALSE WHERE username = %s", (target,))
                    elif action == "delete": 
                        cursor.execute("DELETE FROM users WHERE username = %s", (target,))
                        for ws, uname in list(manager.active_connections.items()):
                            if uname == target: await ws.close()
                    conn.commit()
                    await websocket.send_text(json.dumps({"type": "system_msg", "message": f"Kullanıcı işlemi başarılı!"}))
                except Exception: await websocket.send_text(json.dumps({"type": "system_error", "message": "Hata!"}))
                finally: conn.close()

            # YENİ: KANAL VE SUNUCU SİLME İŞLEMLERİ
            elif packet.get("type") == "admin_channel_action":
                action, target, val, s_name = packet.get("action"), packet.get("target"), packet.get("value"), packet.get("server_name")
                conn = get_db_connection(); cursor = conn.cursor()
                try:
                    if action == "create": cursor.execute("INSERT INTO channels (name, server_name) VALUES (%s, %s)", (target, s_name))
                    elif action == "delete": cursor.execute("DELETE FROM channels WHERE name = %s AND server_name = %s", (target, s_name)); cursor.execute("DELETE FROM messages WHERE channel = %s AND server_name = %s", (target, s_name))
                    elif action == "rename": cursor.execute("UPDATE channels SET name = %s WHERE name = %s AND server_name = %s", (val, target, s_name)); cursor.execute("UPDATE messages SET channel = %s WHERE channel = %s AND server_name = %s", (val, target, s_name))
                    elif action == "lock": cursor.execute("UPDATE channels SET is_locked = TRUE, password = %s WHERE name = %s AND server_name = %s", (val, target, s_name))
                    elif action == "unlock": cursor.execute("UPDATE channels SET is_locked = FALSE, password = NULL WHERE name = %s AND server_name = %s", (target, s_name))
                    conn.commit()
                    await websocket.send_text(json.dumps({"type": "system_msg", "message": "Kanal işlemi başarılı!"}))
                    cursor.execute("SELECT name, is_locked FROM channels WHERE server_name = %s ORDER BY id ASC", (s_name,))
                    ch_list = [{"name": r[0], "is_locked": r[1]} for r in cursor.fetchall()]
                    await manager.broadcast(json.dumps({"type": "channel_list", "channels": ch_list, "server_name": s_name}))
                except Exception: await websocket.send_text(json.dumps({"type": "system_error", "message": "Hata!"}))
                finally: conn.close()
                
            elif packet.get("type") == "admin_server_delete":
                s_name = packet.get("server_name")
                conn = get_db_connection(); cursor = conn.cursor()
                try:
                    cursor.execute("DELETE FROM servers WHERE name = %s", (s_name,))
                    cursor.execute("DELETE FROM channels WHERE server_name = %s", (s_name,))
                    cursor.execute("DELETE FROM messages WHERE server_name = %s", (s_name,))
                    conn.commit()
                    cursor.execute("SELECT name, icon_data FROM servers ORDER BY id ASC")
                    s_list = [{"name": r[0], "icon": r[1]} for r in cursor.fetchall()]
                    await manager.broadcast(json.dumps({"type": "server_list", "servers": s_list}))
                    await websocket.send_text(json.dumps({"type": "system_msg", "message": "Sunucu kalıcı olarak silindi!"}))
                except Exception: pass
                finally: conn.close()
                
    except WebSocketDisconnect: await manager.disconnect(websocket)
    except Exception: await manager.disconnect(websocket)
