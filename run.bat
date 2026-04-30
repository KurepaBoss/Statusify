@echo off
pip install -r requirements.txt --quiet 2>nul
start "" pythonw main.py
