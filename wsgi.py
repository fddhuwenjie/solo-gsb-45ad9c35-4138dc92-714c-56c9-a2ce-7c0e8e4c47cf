"""gunicorn 入口：先初始化数据库再启动应用。"""
from app import server, storage

storage.init_db()
app = server.app
