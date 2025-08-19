from flask import Flask, request, send_from_directory
from datetime import datetime
import os

app = Flask(__name__)

# 定义存放 IP 和 User-Agent 信息的文本文件
LOG_FILE = 'visitor_logs.txt'

# 根路由，用于加载 index.html
@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

# 记录访问信息的路由
@app.route('/record-ip', methods=['GET'])
def record_ip():
    # 获取用户的真实公网 IP
    if 'X-Forwarded-For' in request.headers:
        ip_address = request.headers['X-Forwarded-For'].split(',')[0].strip()
    else:
        ip_address = request.remote_addr

    # 获取 User-Agent 信息
    user_agent = request.headers.get('User-Agent')

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    log_entry = f"时间: {timestamp} | IP: {ip_address} | User-Agent: {user_agent}\n"

    try:
        # 将信息写入文件
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)

        return "Info recorded", 200
    except IOError:
        return "Failed to write info to file", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=50450)