from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse  # BUNU YENİ EKLEDİK
import uvicorn
import sqlite3
import json
from datetime import datetime

app = FastAPI()

# BUNU DA YENİ EKLEDİK: Biri sitemize girdiğinde ona index.html'i gönder!
@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(html_content)


def init_db():
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    # Tabloya 'channel' (kanal) bilgisini de ekledik
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            text TEXT,
            time TEXT,
            channel TEXT
        )
    ''')
    conn.commit()
    conn.close()

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
            
            # Eğer kullanıcı bir kanala tıklayıp geçmişi istiyorsa:
            if packet.get("type") == "load_history":
                channel_name = packet.get("channel")
                conn = sqlite3.connect("chat.db")
                cursor = conn.cursor()
                # Sadece o kanala ait mesajları getir
                cursor.execute("SELECT username, text, time FROM messages WHERE channel = ?", (channel_name,))
                rows = cursor.fetchall()
                conn.close()
                
                history_list = [{"username": r[0], "text": r[1], "time": r[2]} for r in rows]
                # Geçmişi sadece isteyen kişiye özel olarak gönder
                await websocket.send_text(json.dumps({"type": "history", "messages": history_list}))
            
            # Eğer kullanıcı normal bir mesaj gönderiyorsa:
            elif packet.get("type") == "message":
                now = datetime.now()
                time_string = now.strftime("%H:%M")
                
                # Mesajı veritabanına kanal bilgisiyle kaydet
                conn = sqlite3.connect("chat.db")
                cursor = conn.cursor()
                cursor.execute("INSERT INTO messages (username, text, time, channel) VALUES (?, ?, ?, ?)", 
                               (packet['username'], packet['text'], time_string, packet['channel']))
                conn.commit()
                conn.close()
                
                packet['time'] = time_string
                await manager.broadcast(json.dumps(packet))
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    print("Sunucu başlatıldı! Arka planda çalışıyor...")
    uvicorn.run(app, host="127.0.0.1", port=8000)