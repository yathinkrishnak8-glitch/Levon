from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.route('/')
def home():
    return "NovelBot is alive and running!"

def run():
    # Hugging Face specifically requires traffic on port 7860
    app.run(host='0.0.0.0', port=7860)

def keep_alive():
    t = Thread(target=run)
    t.start()
