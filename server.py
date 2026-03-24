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
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(html_content)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Mesajlar Tablosu
        cursor.execute('''CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, username TEXT, text TEXT, time TEXT, channel TEXT)''')
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_data TEXT")
            cursor.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_type TEXT")
        except: pass
            
        # 2. Kullanıcılar Tablosu ve Ban Sistemi
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, role TEXT)''')
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE")
        except: pass
        
        # 3. YENİ: Dinamik Kanallar Tablosu
        cursor.execute('''CREATE TABLE IF NOT EXISTS channels (id SERIAL PRIMARY KEY, name TEXT UNIQUE, is_locked BOOLEAN DEFAULT FALSE, password TEXT)''')
        
        # Varsayılan Admini Ekle
        cursor.execute("SELECT * FROM users WHERE username = 'admin'")
        if not cursor.fetchone():
            hashed_pw = hashlib.sha256("admin123".encode()).hexdigest()
            cursor.execute("INSERT INTO users (username, password_hash, role, is_banned) VALUES (%s, %s, %s, FALSE)", ("admin", hashed_pw, "admin"))
            
        # Varsayılan Kanalları Ekle (Eğer hiç kanal yoksa)
        cursor.execute("SELECT COUNT(*) FROM channels")
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO channels (name) VALUES ('genel-sohbet'), ('valorant-tayfa'), ('siber-guvenlik')")
            
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Hata: {e}")

init_db()

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try: await connection.send_text(message)
            except: pass

manager = ConnectionManager()

# Kanalları herkese gönderen yardımcı fonksiyon
async def broadcast_channels():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name, is_locked FROM channels ORDER BY id ASC")
    ch_list = [{"name": r[0], "is_locked": r[1]} for r in cursor.fetchall()]
    conn.close()
    await manager.broadcast(json.dumps({"type": "channel_list", "channels": ch_list}))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            packet = json.loads(data)
            
            # GİRİŞ SİSTEMİ (BAN KONTROLÜ İLE)
            if packet.get("type") == "login":
                username = packet.get("username")
                password = packet.get("password")
                hashed_pw = hashlib.sha256(password.encode()).hexdigest()
                
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT role, is_banned FROM users WHERE username = %s AND password_hash = %s", (username, hashed_pw))
                user = cursor.fetchone()
                
                if user:
                    if user[1]: # Eğer is_banned True ise
                        await websocket.send_text(json.dumps({"type": "login_response", "success": False, "error_msg": "Hesabınız sunucudan BANLANDI!"}))
                    else:
                        await websocket.send_text(json.dumps({"type": "login_response", "success": True, "role": user[0]}))
                        # Giriş yapana anında güncel kanal listesini yolla
                        cursor.execute("SELECT name, is_locked FROM channels ORDER BY id ASC")
                        ch_list = [{"name": r[0], "is_locked": r[1]} for r in cursor.fetchall()]
                        await websocket.send_text(json.dumps({"type": "channel_list", "channels": ch_list}))
                else:
                    await websocket.send_text(json.dumps({"type": "login_response", "success": False, "error_msg": "Hatalı şifre!"}))
                conn.close()

            # GEÇMİŞİ YÜKLE VEYA ŞİFRELİ KANALA GİRİŞ YAP
            elif packet.get("type") == "load_history":
                ch_name = packet.get("channel")
                pwd_attempt = packet.get("password", "")
                
                conn = get_db_connection()
                cursor = conn.cursor()
                
                # Önce kanal şifreli mi kontrol et
                cursor.execute("SELECT is_locked, password FROM channels WHERE name = %s", (ch_name,))
                ch_info = cursor.fetchone()
                
                if ch_info and ch_info[0]: # Kanal kilitliyse
                    if ch_info[1] != pwd_attempt:
                        await websocket.send_text(json.dumps({"type": "system_error", "message": "Yanlış kanal şifresi!"}))
                        conn.close()
                        continue # Şifre yanlışsa geçmişi gönderme!
                
                cursor.execute("SELECT username, text, time, media_data, media_type FROM messages WHERE channel = %s ORDER BY id ASC", (ch_name,))
                rows = cursor.fetchall()
                conn.close()
                
                history_list = [{"username": r[0], "text": r[1], "time": r[2], "media_data": r[3], "media_type": r[4]} for r in rows]
                await websocket.send_text(json.dumps({"type": "history", "messages": history_list, "channel": ch_name}))
            
            # MESAJ GÖNDERME
            elif packet.get("type") == "message":
                time_string = datetime.now().strftime("%H:%M")
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("INSERT INTO messages (username, text, time, channel, media_data, media_type) VALUES (%s, %s, %s, %s, %s, %s)", 
                               (packet['username'], packet['text'], time_string, packet['channel'], packet.get('media_data'), packet.get('media_type')))
                conn.commit()
                conn.close()
                packet['time'] = time_string
                await manager.broadcast(json.dumps(packet))
                
            # ADMİN YETKİLERİ (Kullanıcı İşlemleri)
            elif packet.get("type") == "admin_user_action":
                action = packet.get("action")
                target = packet.get("target")
                val = packet.get("value")
                
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    if action == "create":
                        hpw = hashlib.sha256(val.encode()).hexdigest()
                        cursor.execute("INSERT INTO users (username, password_hash, role, is_banned) VALUES (%s, %s, %s, FALSE)", (target, hpw, "user"))
                    elif action == "ban":
                        cursor.execute("UPDATE users SET is_banned = TRUE WHERE username = %s", (target,))
                    elif action == "delete":
                        cursor.execute("DELETE FROM users WHERE username = %s", (target,))
                    conn.commit()
                    await websocket.send_text(json.dumps({"type": "system_msg", "message": f"Kullanıcı işlemi ({action}) başarılı!"}))
                except Exception as e:
                    await websocket.send_text(json.dumps({"type": "system_error", "message": "Hata oluştu veya kullanıcı zaten var."}))
                finally:
                    conn.close()

            # ADMİN YETKİLERİ (Kanal İşlemleri)
            elif packet.get("type") == "admin_channel_action":
                action = packet.get("action")
                target = packet.get("target") # Eski kanal adı
                val = packet.get("value") # Yeni kanal adı veya şifre
                
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    if action == "create":
                        cursor.execute("INSERT INTO channels (name) VALUES (%s)", (target,))
                    elif action == "delete":
                        cursor.execute("DELETE FROM channels WHERE name = %s", (target,))
                        cursor.execute("DELETE FROM messages WHERE channel = %s", (target,))
                    elif action == "rename":
                        cursor.execute("UPDATE channels SET name = %s WHERE name = %s", (val, target))
                        cursor.execute("UPDATE messages SET channel = %s WHERE channel = %s", (val, target))
                    elif action == "lock":
                        cursor.execute("UPDATE channels SET is_locked = TRUE, password = %s WHERE name = %s", (val, target))
                    elif action == "unlock":
                        cursor.execute("UPDATE channels SET is_locked = FALSE, password = NULL WHERE name = %s", (target,))
                    
                    conn.commit()
                    await websocket.send_text(json.dumps({"type": "system_msg", "message": f"Kanal işlemi ({action}) başarılı!"}))
                except Exception as e:
                    await websocket.send_text(json.dumps({"type": "system_error", "message": "Kanal işleminde hata!"}))
                finally:
                    conn.close()
                
                # Kanal değiştiyse herkese haber ver ki sol menüleri güncellensin!
                await broadcast_channels()
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
