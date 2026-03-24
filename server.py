from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
import psycopg2 # YENİ: PostgreSQL kütüphanemiz
import json
import hashlib # YENİ: Şifreleme (Siber Güvenlik) için
from datetime import datetime

# Neon'dan aldığın bağlantı adresini buraya yapıştır:
DATABASE_URL = "postgresql://neondb_owner:npg_Q9obn1qiVCdS@ep-autumn-water-an7r9ha8-pooler.c-6.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

app = FastAPI()

@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(html_content)

# YENİ: Veritabanına bağlanma fonksiyonu
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
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

# Sunucu başlarken veritabanını hazırla
init_db()

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            packet = json.loads(data)
            
            if packet.get("type") == "load_history":
                channel_name = packet.get("channel")
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT username, text, time FROM messages WHERE channel = %s ORDER BY id ASC", (channel_name,))
                rows = cursor.fetchall()
                conn.close()
                
                history_list = [{"username": r[0], "text": r[1], "time": r[2]} for r in rows]
                await websocket.send_text(json.dumps({"type": "history", "messages": history_list}))
            
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

if __name__ == "__main__":
    print("PostgreSQL destekli sunucu başlatıldı! http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
