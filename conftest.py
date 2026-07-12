"""Корневой conftest — устанавливает переменные окружения до импортов"""
import os

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test_token')
os.environ.setdefault('FINANCE_ADMIN_TELEGRAM_ID', '123456')
