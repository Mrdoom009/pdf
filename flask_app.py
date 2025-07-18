from flask import Flask
app = Flask(__name__)

@app.route('/')
def index():
    return "Telegram PDF Bot is running!"

@app.route('/health')
def health_check():
    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8000)
