"""本地开发入口：初始化 SQLite 后运行 Flask 开发服务器。"""
from app import server, storage

if __name__ == "__main__":
    storage.init_db()
    server.app.run(host="0.0.0.0", port=8000, debug=False)
