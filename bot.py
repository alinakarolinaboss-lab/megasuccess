#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот для управления аккаунтами Mega.nz (Railway-совместимая версия)
Использует mega.py вместо MegaCMD для работы на Railway
"""

import asyncio
import logging
import os
import json
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery

from mega import Mega
from mega.errors import RequestError

# Конфигурация
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7971684310:AAEjrGvX0NdG_Lq32IqN5rsHDMRGuucU9VA")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7919392701"))
VIDEOS_FOLDER = "./videos"  # Папка с видео для загрузки
ACCOUNTS_FILE = "accounts.json"  # Файл для хранения аккаунтов
SETTINGS_FILE = "settings.json"  # Файл для хранения настроек бота

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальные переменные
upload_tasks: Dict[str, asyncio.Task] = {}
mega_sessions: Dict[str, Mega] = {}
executor = ThreadPoolExecutor(max_workers=3)

class BotStates(StatesGroup):
    waiting_for_initial_folder_name = State()
    waiting_for_new_folder_name = State()
    waiting_for_credentials = State()

class SettingsManager:
    """Менеджер настроек бота"""
    
    @staticmethod
    def load_settings() -> Dict:
        """Загрузка настроек из файла"""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки настроек: {e}")
        return {"folder_name": None, "setup_completed": False}
    
    @staticmethod
    def save_settings(settings: Dict):
        """Сохранение настроек в файл"""
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек: {e}")
    
    @staticmethod
    def get_folder_name() -> Optional[str]:
        """Получить текущее название папки"""
        settings = SettingsManager.load_settings()
        return settings.get("folder_name")
    
    @staticmethod
    def set_folder_name(folder_name: str):
        """Установить название папки"""
        settings = SettingsManager.load_settings()
        settings["folder_name"] = folder_name
        settings["setup_completed"] = True
        SettingsManager.save_settings(settings)
    
    @staticmethod
    def is_setup_completed() -> bool:
        """Проверить, завершена ли первоначальная настройка"""
        settings = SettingsManager.load_settings()
        return settings.get("setup_completed", False) and settings.get("folder_name") is not None

class MegaAPI:
    """Класс для работы с Mega.nz через API"""
    
    @staticmethod
    def get_or_create_session(email: str, password: str) -> Optional[Mega]:
        """Получить или создать сессию Mega"""
        try:
            if email in mega_sessions:
                return mega_sessions[email]
            
            mega = Mega()
            mega.login(email, password)
            mega_sessions[email] = mega
            return mega
        except Exception as e:
            logger.error(f"Ошибка авторизации для {email}: {e}")
            return None
    
    @staticmethod
    def close_session(email: str):
        """Закрыть сессию Mega"""
        if email in mega_sessions:
            del mega_sessions[email]
    
    @staticmethod
    async def login_async(email: str, password: str) -> bool:
        """Асинхронная авторизация в Mega"""
        loop = asyncio.get_event_loop()
        try:
            mega = await loop.run_in_executor(
                executor,
                MegaAPI.get_or_create_session,
                email,
                password
            )
            return mega is not None
        except Exception as e:
            logger.error(f"Ошибка авторизации: {e}")
            return False
    
    @staticmethod
    async def upload_folder_async(email: str, password: str, local_path: str, folder_name: str) -> Tuple[bool, Optional[str]]:
        """Асинхронная загрузка папки на Mega"""
        loop = asyncio.get_event_loop()
        
        try:
            mega = await loop.run_in_executor(
                executor,
                MegaAPI.get_or_create_session,
                email,
                password
            )
            
            if not mega:
                return False, None
            
            logger.info(f"Создаю папку {folder_name} на Mega")
            
            # Получаем корневую папку
            files = await loop.run_in_executor(executor, mega.get_files)
            
            # Ищем существующую папку или создаем новую
            folder_node = None
            for node_id, node_info in files.items():
                if node_info['a'] and node_info['a'].get('n') == folder_name and node_info['t'] == 1:
                    folder_node = node_info
                    logger.info(f"Найдена существующая папка {folder_name}")
                    break
            
            # Если папка не найдена, создаем новую
            if not folder_node:
                root_id = files[0]  # Корневая папка
                folder_node = await loop.run_in_executor(
                    executor,
                    mega.create_folder,
                    folder_name,
                    root_id
                )
                logger.info(f"Создана новая папка {folder_name}")
            
            # Загружаем файлы
            upload_success = True
            files_list = list(Path(local_path).glob('*'))
            uploaded_count = 0
            
            for file_path in files_list:
                if file_path.is_file():
                    logger.info(f"Загрузка {file_path.name}...")
                    try:
                        await loop.run_in_executor(
                            executor,
                            mega.upload,
                            str(file_path),
                            folder_node
                        )
                        uploaded_count += 1
                        logger.info(f"✅ Файл {file_path.name} загружен успешно")
                    except Exception as e:
                        logger.error(f"❌ Ошибка загрузки {file_path.name}: {e}")
                        upload_success = False
            
            logger.info(f"Загружено файлов: {uploaded_count}/{len(files_list)}")
            
            if uploaded_count > 0:
                return True, folder_name
            else:
                return False, None
                
        except Exception as e:
            logger.error(f"Исключение при загрузке папки: {e}")
            return False, None
    
    @staticmethod
    async def get_public_link_async(email: str, password: str, folder_name: str) -> Optional[str]:
        """Получить публичную ссылку на папку"""
        loop = asyncio.get_event_loop()
        
        try:
            mega = await loop.run_in_executor(
                executor,
                MegaAPI.get_or_create_session,
                email,
                password
            )
            
            if not mega:
                return None
            
            # Получаем список файлов
            files = await loop.run_in_executor(executor, mega.get_files)
            
            # Ищем папку
            for node_id, node_info in files.items():
                if node_info['a'] and node_info['a'].get('n') == folder_name and node_info['t'] == 1:
                    # Экспортируем папку
                    link = await loop.run_in_executor(
                        executor,
                        mega.export,
                        node_id
                    )
                    logger.info(f"Получена публичная ссылка для {folder_name}: {link}")
                    return link
            
            logger.warning(f"Папка {folder_name} не найдена")
            return None
            
        except Exception as e:
            logger.error(f"Ошибка получения публичной ссылки: {e}")
            return None
    
    @staticmethod
    async def get_account_info_async(email: str, password: str) -> Dict:
        """Получение информации об аккаунте"""
        loop = asyncio.get_event_loop()
        
        try:
            mega = await loop.run_in_executor(
                executor,
                MegaAPI.get_or_create_session,
                email,
                password
            )
            
            if not mega:
                return {"used": "N/A", "total": "N/A", "percent": "N/A"}
            
            # Получаем информацию о квоте
            quota = await loop.run_in_executor(executor, mega.get_quota)
            
            if quota:
                used_gb = quota / (1024**3)
                total_gb = await loop.run_in_executor(executor, mega.get_storage_space, True) / (1024**3)
                percent = (quota / (total_gb * 1024**3)) * 100 if total_gb > 0 else 0
                
                return {
                    "used": f"{used_gb:.2f} GB",
                    "total": f"{total_gb:.2f} GB",
                    "percent": f"{percent:.1f}%"
                }
            
            return {"used": "N/A", "total": "N/A", "percent": "N/A"}
            
        except Exception as e:
            logger.error(f"Ошибка получения информации об аккаунте: {e}")
            return {"used": "N/A", "total": "N/A", "percent": "N/A"}

class AccountManager:
    """Менеджер для работы с аккаунтами Mega"""
    
    @staticmethod
    def load_accounts() -> Dict:
        """Загрузка аккаунтов из файла"""
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки аккаунтов: {e}")
        return {}
    
    @staticmethod
    def save_accounts(accounts: Dict):
        """Сохранение аккаунтов в файл"""
        try:
            with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Ошибка сохранения аккаунтов: {e}")
    
    @staticmethod
    def add_account(email: str, password: str) -> bool:
        """Добавление нового аккаунта"""
        accounts = AccountManager.load_accounts()
        accounts[email] = {
            "password": password,
            "added_at": datetime.now().isoformat(),
            "status": "active",
            "last_upload": None,
            "public_link": None
        }
        AccountManager.save_accounts(accounts)
        return True
    
    @staticmethod
    def remove_account(email: str) -> bool:
        """Удаление аккаунта"""
        accounts = AccountManager.load_accounts()
        if email in accounts:
            del accounts[email]
            AccountManager.save_accounts(accounts)
            MegaAPI.close_session(email)
            return True
        return False
    
    @staticmethod
    def update_account_status(email: str, status: str, public_link: str = None):
        """Обновление статуса аккаунта"""
        accounts = AccountManager.load_accounts()
        if email in accounts:
            accounts[email]["status"] = status
            if public_link:
                accounts[email]["public_link"] = public_link
            accounts[email]["last_upload"] = datetime.now().isoformat()
            AccountManager.save_accounts(accounts)

def create_initial_setup_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для первоначальной настройки"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 Задать название папки", callback_data="setup_folder_name")]
    ])
    return keyboard

def create_main_keyboard() -> InlineKeyboardMarkup:
    """Главная клавиатура"""
    folder_name = SettingsManager.get_folder_name()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="list_accounts")],
        [InlineKeyboardButton(text=f"📁 Папка: {folder_name}", callback_data="change_folder_name")],
        [InlineKeyboardButton(text="🔄 Перезагрузить файлы", callback_data="reupload_all")],
        [InlineKeyboardButton(text="ℹ️ Информация", callback_data="info")]
    ])
    return keyboard

def create_account_keyboard(email: str) -> InlineKeyboardMarkup:
    """Создание клавиатуры для управления аккаунтом"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Перезагрузить файлы", callback_data=f"reupload:{email}")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"delete:{email}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
    ])
    return keyboard

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ У вас нет доступа к этому боту.")
        return
    
    # Проверяем, завершена ли первоначальная настройка
    if not SettingsManager.is_setup_completed():
        welcome_text = (
            "🤖 <b>Mega.nz Manager Bot (Railway Edition)</b>\n\n"
            "Добро пожаловать! Для начала работы необходимо задать название папки, "
            "которое будет использоваться для загрузки файлов на все аккаунты Mega.\n\n"
            "⚠️ <b>Внимание:</b> Пока не будет задано название папки, "
            "работа с аккаунтами будет недоступна.\n\n"
            "Нажмите кнопку ниже, чтобы начать настройку:"
        )
        await message.reply(
            welcome_text,
            reply_markup=create_initial_setup_keyboard(),
            parse_mode="HTML"
        )
    else:
        folder_name = SettingsManager.get_folder_name()
        welcome_text = (
            "🤖 <b>Mega.nz Manager Bot (Railway Edition)</b>\n\n"
            "Этот бот позволяет управлять аккаунтами Mega.nz "
            "и автоматически загружать файлы.\n\n"
            f"📁 <b>Текущая папка:</b> <code>{folder_name}</code>\n"
            "✅ Бот готов к работе\n\n"
            "Выберите действие:"
        )
        await message.reply(
            welcome_text,
            reply_markup=create_main_keyboard(),
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "setup_folder_name")
async def setup_folder_name_handler(callback: CallbackQuery, state: FSMContext):
    """Обработчик первоначальной настройки названия папки"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    await callback.message.edit_text(
        "📁 <b>Настройка названия папки</b>\n\n"
        "Введите название папки, которое будет использоваться "
        "для загрузки файлов на все аккаунты Mega.nz:\n\n"
        "⚠️ Это название будет применено ко всем загрузкам. "
        "Вы сможете изменить его позже в любой момент.\n\n"
        "Пример: <code>MyVideos</code>, <code>Content_2024</code>, <code>Films</code>",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_initial_folder_name)
    await callback.answer()

@dp.callback_query(F.data == "change_folder_name")
async def change_folder_name_handler(callback: CallbackQuery, state: FSMContext):
    """Обработчик изменения названия папки"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return

    current_folder = SettingsManager.get_folder_name()
    await callback.message.edit_text(
        f"📁 <b>Изменение названия папки</b>\n\n"
        f"Текущее название: <code>{current_folder}</code>\n\n"
        "Введите новое название папки для загрузки файлов:\n\n"
        "Пример: <code>MyVideos</code>, <code>Content_2024</code>, <code>Films</code>\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.waiting_for_new_folder_name)
    await callback.answer()

@dp.message(StateFilter(BotStates.waiting_for_initial_folder_name))
async def process_initial_folder_name(message: types.Message, state: FSMContext):
    """Обработка первоначального названия папки"""
    if message.from_user.id != ADMIN_ID:
        return

    folder_name = message.text.strip()
    
    if not folder_name or len(folder_name) < 1:
        await message.reply("❌ Название папки не может быть пустым. Попробуйте снова:")
        return
    
    # Проверяем на недопустимые символы
    if any(char in folder_name for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']):
        await message.reply(
            "❌ Название папки содержит недопустимые символы.\n"
            "Избегайте: / \\ : * ? \" < > |\n\n"
            "Попробуйте снова:"
        )
        return
    
    # Сохраняем настройки
    SettingsManager.set_folder_name(folder_name)
    
    await message.reply(
        f"✅ <b>Настройка завершена!</b>\n\n"
        f"Название папки установлено: <code>{folder_name}</code>\n\n"
        "Теперь вы можете добавлять аккаунты и загружать файлы. "
        "Все файлы будут загружаться в папку с этим названием.\n\n"
        "🎯 Бот готов к работе!",
        parse_mode="HTML",
        reply_markup=create_main_keyboard()
    )
    await state.clear()

@dp.message(StateFilter(BotStates.waiting_for_new_folder_name))
async def process_new_folder_name(message: types.Message, state: FSMContext):
    """Обработка нового названия папки"""
    if message.from_user.id != ADMIN_ID:
        return

    if message.text == "/cancel":
        await message.reply("❌ Изменение названия папки отменено", reply_markup=create_main_keyboard())
        await state.clear()
        return

    folder_name = message.text.strip()
    
    if not folder_name or len(folder_name) < 1:
        await message.reply("❌ Название папки не может быть пустым. Попробуйте снова:")
        return
    
    # Проверяем на недопустимые символы
    if any(char in folder_name for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']):
        await message.reply(
            "❌ Название папки содержит недопустимые символы.\n"
            "Избегайте: / \\ : * ? \" < > |\n\n"
            "Попробуйте снова:"
        )
        return
    
    # Сохраняем новые настройки
    old_folder = SettingsManager.get_folder_name()
    SettingsManager.set_folder_name(folder_name)
    
    await message.reply(
        f"✅ <b>Название папки изменено!</b>\n\n"
        f"Старое название: <code>{old_folder}</code>\n"
        f"Новое название: <code>{folder_name}</code>\n\n"
        "Все новые загрузки будут использовать это название.",
        parse_mode="HTML",
        reply_markup=create_main_keyboard()
    )
    await state.clear()

@dp.callback_query(F.data == "add_account")
async def add_account_handler(callback: CallbackQuery, state: FSMContext):
    """Начало процесса добавления аккаунта"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    
    # Проверяем настройку
    if not SettingsManager.is_setup_completed():
        await callback.answer("Сначала настройте название папки!", show_alert=True)
        return
    
    await callback.message.edit_text(
        "🔐 <b>Добавление аккаунта Mega.nz</b>\n\n"
        "Отправьте данные аккаунта в формате:\n"
        "<code>email:password</code>\n\n"
        "Например: <code>user@example.com:mypassword123</code>\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    
    await state.set_state(BotStates.waiting_for_credentials)
    await callback.answer()

@dp.message(StateFilter(BotStates.waiting_for_credentials))
async def process_credentials(message: types.Message, state: FSMContext):
    """Обработка введенных данных аккаунта"""
    if message.from_user.id != ADMIN_ID:
        return
    
    if message.text == "/cancel":
        await state.clear()
        await message.reply("❌ Добавление аккаунта отменено", reply_markup=create_main_keyboard())
        return
    
    try:
        # Парсим данные
        if ":" not in message.text:
            await message.reply("❌ Неверный формат. Используйте: email:password")
            return
        
        email, password = message.text.strip().split(":", 1)
        
        # Проверяем, не добавлен ли уже этот аккаунт
        accounts = AccountManager.load_accounts()
        if email in accounts:
            await message.reply(
                "⚠️ Этот аккаунт уже добавлен!",
                reply_markup=create_main_keyboard()
            )
            await state.clear()
            return
        
        # Отправляем сообщение о начале авторизации
        status_msg = await message.reply("🔄 Авторизация в Mega.nz...")
        
        # Пытаемся авторизоваться
        login_success = await MegaAPI.login_async(email, password)
        
        if login_success:
            # Добавляем аккаунт в базу
            AccountManager.add_account(email, password)
            
            await status_msg.edit_text("✅ Авторизация успешна!\n🔄 Начинаю загрузку файлов...")
            
            # Запускаем загрузку файлов
            upload_task = asyncio.create_task(upload_files_for_account(email, password, status_msg))
            upload_tasks[email] = upload_task
            
        else:
            await status_msg.edit_text(
                "❌ Ошибка авторизации!\n"
                "Проверьте правильность email и пароля.",
                reply_markup=create_main_keyboard()
            )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Ошибка при добавлении аккаунта: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        await message.reply(
            f"❌ Произошла ошибка: {str(e)}",
            reply_markup=create_main_keyboard()
        )
        await state.clear()

async def upload_files_for_account(email: str, password: str, status_msg: types.Message = None):
    """Асинхронная загрузка файлов для аккаунта"""
    try:
        logger.info(f"Начинаю загрузку файлов для {email}")
        
        # Проверяем наличие файлов
        if not os.path.exists(VIDEOS_FOLDER):
            os.makedirs(VIDEOS_FOLDER)
        
        files_count = len([f for f in Path(VIDEOS_FOLDER).glob('*') if f.is_file()])
        if files_count == 0:
            raise Exception(f"Папка {VIDEOS_FOLDER} пуста")
        
        # Получаем настроенное название папки
        folder_name = SettingsManager.get_folder_name()
        if not folder_name:
            raise Exception("Название папки не настроено!")
        
        # Обновляем статус
        if status_msg:
            await status_msg.edit_text(
                f"✅ Авторизация успешна!\n"
                f"🔄 Загружаю {files_count} файлов в папку '{folder_name}'..."
            )
        
        logger.info(f"Использую название папки: {folder_name}")
        
        # Загружаем файлы
        success, uploaded_folder = await MegaAPI.upload_folder_async(email, password, VIDEOS_FOLDER, folder_name)
        
        if success and uploaded_folder:
            logger.info(f"✅ Файлы загружены в папку {uploaded_folder}")
            
            # Обновляем статус
            if status_msg:
                await status_msg.edit_text(
                    f"✅ Файлы загружены!\n"
                    f"🔗 Создаю публичную ссылку..."
                )
            
            # Создаем публичную ссылку
            public_link = await MegaAPI.get_public_link_async(email, password, uploaded_folder)
            
            if public_link:
                # Обновляем статус аккаунта
                AccountManager.update_account_status(email, "active", public_link)
                success_text = (
                    f"✅ <b>Загрузка завершена!</b>\n\n"
                    f"📧 Аккаунт: <code>{email}</code>\n"
                    f"📁 Папка: <code>{uploaded_folder}</code>\n"
                    f"📄 Загружено файлов: {files_count}\n"
                    f"🔗 Публичная ссылка:\n{public_link}"
                )
            else:
                AccountManager.update_account_status(email, "warning")
                success_text = (
                    f"✅ <b>Файлы загружены!</b>\n\n"
                    f"📧 Аккаунт: <code>{email}</code>\n"
                    f"📁 Папка: <code>{uploaded_folder}</code>\n"
                    f"📄 Загружено файлов: {files_count}\n"
                    f"⚠️ Не удалось создать публичную ссылку\n"
                    f"💡 Попробуйте создать её вручную в веб-интерфейсе:\n"
                    f"1. Откройте mega.nz\n"
                    f"2. Найдите папку <code>{uploaded_folder}</code>\n"
                    f"3. Нажмите правой кнопкой → Получить ссылку"
                )
            
            if status_msg:
                await status_msg.edit_text(success_text, parse_mode="HTML", reply_markup=create_main_keyboard())
            else:
                await bot.send_message(ADMIN_ID, success_text, parse_mode="HTML")
                
            logger.info(f"✅ Загрузка для {email} завершена успешно")
            
        else:
            raise Exception("Ошибка при загрузке файлов на Mega")
            
    except Exception as e:
        logger.error(f"Ошибка при загрузке для {email}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        AccountManager.update_account_status(email, "error")
        error_text = f"❌ Ошибка загрузки для {email}:\n{str(e)}"
        
        if status_msg:
            await status_msg.edit_text(error_text, reply_markup=create_main_keyboard())
        else:
            await bot.send_message(ADMIN_ID, error_text)

@dp.callback_query(F.data == "list_accounts")
async def list_accounts_handler(callback: CallbackQuery):
    """Показать список всех аккаунтов"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    
    # Проверяем настройку
    if not SettingsManager.is_setup_completed():
        await callback.answer("Сначала настройте название папки!", show_alert=True)
        return
    
    accounts = AccountManager.load_accounts()
    
    if not accounts:
        await callback.message.edit_text(
            "📭 Нет добавленных аккаунтов",
            reply_markup=create_main_keyboard()
        )
        await callback.answer()
        return
    
    text = "📋 <b>Список аккаунтов:</b>\n\n"
    
    for email, info in accounts.items():
        status_emoji = "✅" if info["status"] == "active" else "❌" if info["status"] == "error" else "⚠️"
        text += f"{status_emoji} <code>{email}</code>\n"
        
        if info.get("public_link"):
            text += f"   🔗 <a href='{info['public_link']}'>Открыть папку</a>\n"
        
        if info.get("last_upload"):
            upload_time = datetime.fromisoformat(info["last_upload"])
            text += f"   📅 Последняя загрузка: {upload_time.strftime('%d.%m.%Y %H:%M')}\n"
        
        text += "\n"
    
    # Создаем клавиатуру с аккаунтами
    keyboard_buttons = []
    for email in accounts.keys():
        keyboard_buttons.append([InlineKeyboardButton(text=email, callback_data=f"account:{email}")])
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("account:"))
async def account_details_handler(callback: CallbackQuery):
    """Показать детали конкретного аккаунта"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    
    email = callback.data.split(":", 1)[1]
    accounts = AccountManager.load_accounts()
    
    if email not in accounts:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    
    info = accounts[email]
    folder_name = SettingsManager.get_folder_name()
    
    text = (
        f"📧 <b>Аккаунт: {email}</b>\n\n"
        f"📊 Статус: {'✅ Активен' if info['status'] == 'active' else '❌ Ошибка' if info['status'] == 'error' else '⚠️ Предупреждение'}\n"
        f"📁 Папка: <code>{folder_name}</code>\n"
    )
    
    if info.get("added_at"):
        added_time = datetime.fromisoformat(info["added_at"])
        text += f"📅 Добавлен: {added_time.strftime('%d.%m.%Y %H:%M')}\n"
    
    if info.get("last_upload"):
        upload_time = datetime.fromisoformat(info["last_upload"])
        text += f"📤 Последняя загрузка: {upload_time.strftime('%d.%m.%Y %H:%M')}\n"
    
    if info.get("public_link"):
        text += f"\n🔗 Публичная ссылка:\n{info['public_link']}"
    
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=create_account_keyboard(email),
        disable_web_page_preview=True
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("reupload:"))
async def reupload_account_handler(callback: CallbackQuery):
    """Перезагрузить файлы для конкретного аккаунта"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    
    email = callback.data.split(":", 1)[1]
    accounts = AccountManager.load_accounts()
    
    if email not in accounts:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    
    await callback.message.edit_text("🔄 Начинаю перезагрузку файлов...")
    
    password = accounts[email]["password"]
    
    # Запускаем загрузку
    upload_task = asyncio.create_task(upload_files_for_account(email, password, callback.message))
    upload_tasks[email] = upload_task
    
    await callback.answer()

@dp.callback_query(F.data.startswith("delete:"))
async def delete_account_handler(callback: CallbackQuery):
    """Удалить аккаунт"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    
    email = callback.data.split(":", 1)[1]
    
    # Отменяем задачу загрузки если есть
    if email in upload_tasks:
        upload_tasks[email].cancel()
        del upload_tasks[email]
    
    # Удаляем из базы
    if AccountManager.remove_account(email):
        await callback.message.edit_text(
            f"✅ Аккаунт {email} удален",
            reply_markup=create_main_keyboard()
        )
    else:
        await callback.answer("Ошибка при удалении аккаунта", show_alert=True)
    
    await callback.answer()

@dp.callback_query(F.data == "reupload_all")
async def reupload_all_handler(callback: CallbackQuery):
    """Перезагрузить файлы на все аккаунты"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    
    # Проверяем настройку
    if not SettingsManager.is_setup_completed():
        await callback.answer("Сначала настройте название папки!", show_alert=True)
        return
    
    accounts = AccountManager.load_accounts()
    
    if not accounts:
        await callback.answer("Нет аккаунтов для загрузки", show_alert=True)
        return
    
    await callback.message.edit_text("🔄 Начинаю массовую перезагрузку файлов...")
    
    # Запускаем загрузку для каждого аккаунта последовательно
    for email, info in accounts.items():
        try:
            password = info["password"]
            await upload_files_for_account(email, password)
            await asyncio.sleep(2)  # Небольшая задержка между аккаунтами
        except Exception as e:
            logger.error(f"Ошибка при перезагрузке для {email}: {e}")
            await bot.send_message(ADMIN_ID, f"❌ Ошибка загрузки для {email}: {str(e)}")
    
    await callback.message.edit_text(
        "✅ Массовая перезагрузка завершена!",
        reply_markup=create_main_keyboard()
    )
    
    await callback.answer()

@dp.callback_query(F.data == "info")
async def info_handler(callback: CallbackQuery):
    """Показать информацию о боте"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    
    accounts = AccountManager.load_accounts()
    active_uploads = len([t for t in upload_tasks.values() if not t.done()])
    folder_name = SettingsManager.get_folder_name()
    
    # Проверяем существование папки с видео
    videos_count = 0
    total_size = 0
    
    if os.path.exists(VIDEOS_FOLDER):
        for file_path in Path(VIDEOS_FOLDER).rglob('*'):
            if file_path.is_file():
                videos_count += 1
                total_size += file_path.stat().st_size
    
    total_size_mb = total_size / (1024 * 1024)
    
    info_text = (
        "ℹ️ <b>Информация о боте</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Аккаунтов: {len(accounts)}\n"
        f"• Активных загрузок: {active_uploads}\n"
        f"• Активных сессий: {len(mega_sessions)}\n\n"
        f"📁 <b>Настройки:</b>\n"
        f"• Название папки: <code>{folder_name or 'Не настроено'}</code>\n"
        f"• Настройка завершена: {'✅ Да' if SettingsManager.is_setup_completed() else '❌ Нет'}\n\n"
        f"📄 <b>Папка с видео:</b>\n"
        f"• Путь: <code>{VIDEOS_FOLDER}</code>\n"
        f"• Файлов: {videos_count}\n"
        f"• Размер: {total_size_mb:.2f} MB\n\n"
        f"🤖 <b>Версия бота:</b> 2.0.0 (Railway Edition)\n"
        f"⚙️ <b>Backend:</b> mega.py API\n"
        f"🔧 <b>Платформа:</b> Railway-compatible\n\n"
        f"📝 <b>Особенности Railway версии:</b>\n"
        f"• Использует mega.py вместо MegaCMD\n"
        f"• Полностью асинхронная работа\n"
        f"• Поддержка множественных сессий\n"
        f"• Работает в контейнерах без CLI зависимостей"
    )
    
    await callback.message.edit_text(
        info_text,
        parse_mode="HTML",
        reply_markup=create_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_main")
async def back_to_main_handler(callback: CallbackQuery):
    """Вернуться в главное меню"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен", show_alert=True)
        return
    
    if not SettingsManager.is_setup_completed():
        await callback.message.edit_text(
            "🤖 <b>Mega.nz Manager Bot (Railway Edition)</b>\n\n"
            "Для начала работы необходимо задать название папки.",
            reply_markup=create_initial_setup_keyboard(),
            parse_mode="HTML"
        )
    else:
        folder_name = SettingsManager.get_folder_name()
        welcome_text = (
            "🤖 <b>Mega.nz Manager Bot (Railway Edition)</b>\n\n"
            "Этот бот позволяет управлять аккаунтами Mega.nz "
            "и автоматически загружать файлы.\n\n"
            f"📁 <b>Текущая папка:</b> <code>{folder_name}</code>\n\n"
            "Выберите действие:"
        )
        await callback.message.edit_text(
            welcome_text,
            reply_markup=create_main_keyboard(),
            parse_mode="HTML"
        )
    await callback.answer()

@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    """Отмена текущего действия"""
    if message.from_user.id != ADMIN_ID:
        return
    
    await state.clear()
    await message.reply(
        "❌ Действие отменено",
        reply_markup=create_main_keyboard() if SettingsManager.is_setup_completed() else create_initial_setup_keyboard()
    )

@dp.message(Command("reset"))
async def reset_handler(message: types.Message):
    """Сброс настроек бота"""
    if message.from_user.id != ADMIN_ID:
        return
    
    try:
        # Удаляем файл настроек
        if os.path.exists(SETTINGS_FILE):
            os.remove(SETTINGS_FILE)
        
        await message.reply(
            "🔄 <b>Настройки сброшены!</b>\n\n"
            "Все настройки бота удалены. "
            "Используйте /start для повторной настройки.\n\n"
            "⚠️ Аккаунты остались без изменений.",
            parse_mode="HTML"
        )
        
    except Exception as e:
        await message.reply(f"❌ Ошибка при сбросе: {str(e)}")

async def main():
    """Основная функция запуска бота"""
    # Создаем папку для видео если её нет
    os.makedirs(VIDEOS_FOLDER, exist_ok=True)
    
    # Уведомляем админа о запуске
    try:
        setup_status = "✅ Завершена" if SettingsManager.is_setup_completed() else "❌ Требуется настройка"
        folder_name = SettingsManager.get_folder_name()
        
        await bot.send_message(
            ADMIN_ID,
            "✅ Бот запущен и готов к работе!\n"
            "🤖 Версия: 2.0.0 (Railway Edition)\n"
            f"📁 Настройка папки: {setup_status}\n"
            f"📂 Текущая папка: {folder_name or 'Не настроена'}\n"
            "⚙️ Backend: mega.py API\n"
            "Используйте /start для начала работы."
        )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение админу: {e}")
    
    # Запускаем бота
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")