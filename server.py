from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
import psycopg2
import json
import hashlib
from datetime import datetime
import os

# Şifreyi sildik! Artık linki Render'ın gizli kasasından (Environment Variables) otomatik çekiyor.
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
        
        # 1. Mesajlar Tablosu (PostgreSQL uyumlu)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                username TEXT,
                text TEXT,
                time TEXT,
                channel TEXT
            )
        ''')
        
        # 2. Kullanıcılar Tablosu (Şifreli giriş için)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                password_hash TEXT,
                role TEXT
            )
        ''')
        
        # Varsayılan Admin Hesabını Oluşturma (Eğer yoksa)
        cursor.execute("SELECT * FROM users WHERE username = 'admin'")
        if not cursor.fetchone():
            # Siber güvenlik standardı: Şifreler düz metin tutulmaz, Hashlenir!
            hashed_pw = hashlib.sha256("admin123".encode()).hexdigest()
            cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)", 
                           ("admin", hashed_pw, "admin"))
            
        conn.commit()
        conn.close()
        print("Veritabanı bağlantısı ve tablolar hazır.")
    except Exception as e:
        print(f"Veritabanı başlatılırken hata oluştu: {e}")

# Sunucu başlarken veritabanını hazırla
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
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            packet = json.loads(data)
            
            # 1. GİRİŞ YAPMA KONTROLÜ
            if packet.get("type") == "login":
                username = packet.get("username")
                password = packet.get("password")
                
                # Gelen şifreyi siber güvenlik standartlarına göre hashle
                hashed_pw = hashlib.sha256(password.encode()).hexdigest()
                
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT role FROM users WHERE username = %s AND password_hash = %s", (username, hashed_pw))
                user = cursor.fetchone()
                conn.close()
                
                if user:
                    # Şifre doğruysa onay gönder ve rolünü (admin/user) belirt
                    await websocket.send_text(json.dumps({"type": "login_response", "success": True, "role": user[0]}))
                else:
                    # Şifre yanlışsa hata gönder
                    await websocket.send_text(json.dumps({"type": "login_response", "success": False}))

            # 2. GEÇMİŞİ YÜKLEME
            elif packet.get("type") == "load_history":
                channel_name = packet.get("channel")
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT username, text, time FROM messages WHERE channel = %s ORDER BY id ASC", (channel_name,))
                rows = cursor.fetchall()
                conn.close()
                
                history_list = [{"username": r[0], "text": r[1], "time": r[2]} for r in rows]
                await websocket.send_text(json.dumps({"type": "history", "messages": history_list}))
            
            # 3. YENİ MESAJ GÖNDERME
            elif packet.get("type") == "message":
                now = datetime.now()
                time_string = now.strftime("%H:%M")
                
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("INSERT INTO messages (username, text, time, channel) VALUES (%s, %s, %s, %s)", 
                               (packet['username'], packet['text'], time_string, packet['channel']))
                conn.commit()
                conn.close()
                
                packet['time'] = time_string
                await manager.broadcast(json.dumps(packet))
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"Beklenmeyen bağlantı hatası: {e}")
        manager.disconnect(websocket)

if __name__ == "__main__":
    print("PostgreSQL destekli sunucu başlatıldı!")
    uvicorn.run(app, host="0.0.0.0", port=8000)
