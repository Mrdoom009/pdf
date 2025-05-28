#!/bin/bash
# Start the bot and Flask app in parallel

# For Linux/Mac
python bot.py &
python flask_app.py
