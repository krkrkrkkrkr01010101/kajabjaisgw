#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت تيليجرام لإضافة حقوق مائية (Watermark) على الصور.
مبني بالكامل داخل ملف واحد، يستخدم SQLite للتخزين، ومناسب للتشغيل على Railway.
"""

import os
import io
import re
import sys
import time
import sqlite3
import logging
import tempfile
import threading
import shutil
from collections import deque

from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    ARABIC_SUPPORT = True
except Exception:
    ARABIC_SUPPORT = False

import telebot
from telebot import types


# =========================================================================
# الإعدادات الأساسية من متغيرات البيئة
# =========================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.environ.get("OWNER_ID", "").strip()
FORCE_CHANNEL_ID_RAW = os.environ.get("FORCE_CHANNEL_ID", "").strip()
FORCE_CHANNEL_USERNAME = os.environ.get("FORCE_CHANNEL_USERNAME", "").strip()

if not BOT_TOKEN:
    print("خطأ: يجب ضبط متغير البيئة BOT_TOKEN قبل التشغيل.")
    sys.exit(1)

try:
    OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else 0
except ValueError:
    print("خطأ: قيمة OWNER_ID غير صحيحة، يجب أن تكون رقمًا.")
    sys.exit(1)

try:
    FORCE_CHANNEL_ID = int(FORCE_CHANNEL_ID_RAW) if FORCE_CHANNEL_ID_RAW else 0
except ValueError:
    FORCE_CHANNEL_ID = 0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot_data.db")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
TMP_ROOT = os.path.join(tempfile.gettempdir(), "wm_bot_tmp")
os.makedirs(TMP_ROOT, exist_ok=True)
os.makedirs(FONTS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("wm_bot")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, parse_mode=None)


# =========================================================================
# قاعدة البيانات
# =========================================================================

class Database:
    """طبقة تخزين خفيفة باستخدام SQLite، آمنة بين الخيوط عبر قفل واحد."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()
        self._init_default_settings()

    def _init_schema(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at INTEGER,
                    images_count INTEGER DEFAULT 0,
                    is_banned INTEGER DEFAULT 0,
                    banned_until INTEGER DEFAULT 0,
                    ban_reason TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            self._conn.commit()

    def _init_default_settings(self):
        defaults = {
            "force_sub_enabled": "1",
            "force_channel_id": str(FORCE_CHANNEL_ID) if FORCE_CHANNEL_ID else "0",
            "force_channel_username": FORCE_CHANNEL_USERNAME,
            "max_image_mb": "10",
            "max_image_dimension": "6000",
            "default_opacity": "25",
            "flood_window_seconds": "60",
            "flood_max_requests": "5",
            "flood_ban_seconds": "300",
            "max_concurrent_per_user": "1",
            "max_concurrent_global": "4",
            "maintenance_mode": "0",
        }
        with self._lock:
            cur = self._conn.cursor()
            for k, v in defaults.items():
                cur.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
                )
            self._conn.commit()

    # ---------------- إعدادات ----------------
    def get_setting(self, key, default=None, cast=str):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
        if row is None:
            return default
        try:
            return cast(row[0])
        except (ValueError, TypeError):
            return default

    def set_setting(self, key, value):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    # ---------------- مستخدمون ----------------
    def touch_user(self, user_id, username, first_name):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO users (user_id, username, first_name, joined_at) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, username or "", first_name or "", int(time.time())),
                )
            else:
                cur.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                    (username or "", first_name or "", user_id),
                )
            self._conn.commit()

    def increment_images(self, user_id):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE users SET images_count = images_count + 1 WHERE user_id = ?",
                (user_id,),
            )
            self._conn.commit()

    def ban_user(self, user_id, reason="", until=0):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE users SET is_banned = 1, ban_reason = ?, banned_until = ? "
                "WHERE user_id = ?",
                (reason, until, user_id),
            )
            self._conn.commit()

    def unban_user(self, user_id):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE users SET is_banned = 0, ban_reason = '', banned_until = 0 "
                "WHERE user_id = ?",
                (user_id,),
            )
            self._conn.commit()

    def get_user(self, user_id):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    def is_banned(self, user_id):
        u = self.get_user(user_id)
        if not u or not u["is_banned"]:
            return False
        if u["banned_until"] and u["banned_until"] < int(time.time()):
            self.unban_user(user_id)
            return False
        return True

    def list_banned(self, limit=25):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT user_id, username, ban_reason, banned_until FROM users "
                "WHERE is_banned = 1 LIMIT ?",
                (limit,),
            )
            return cur.fetchall()

    def stats(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
            banned = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(images_count), 0) FROM users")
            images = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM users WHERE joined_at > ?",
                (int(time.time()) - 86400,),
            )
            new_today = cur.fetchone()[0]
        return {
            "total": total,
            "banned": banned,
            "images": images,
            "new_today": new_today,
        }


db = Database(DB_PATH)


# =========================================================================
# إدارة الخطوط
# =========================================================================

class FontManager:
    def __init__(self, folder):
        self.folder = folder
        self.fonts = []
        self.reload()

    def reload(self):
        found = []
        if os.path.isdir(self.folder):
            for name in sorted(os.listdir(self.folder)):
                if name.lower().endswith((".ttf", ".otf")):
                    display = os.path.splitext(name)[0].replace("_", " ").replace("-", " ")
                    found.append((display, os.path.join(self.folder, name)))
        self.fonts = found
        return self.fonts

    def get(self, index):
        if 0 <= index < len(self.fonts):
            return self.fonts[index]
        return None

    def has_fonts(self):
        return len(self.fonts) > 0


font_manager = FontManager(FONTS_DIR)


# =========================================================================
# فحص المحتوى (فحص أولي مبسط، وليس بديلاً عن نظام فحص متكامل)
# =========================================================================

class ContentModerator:
    """
    فحص أولي وخفيف لرفض الصور المشبوهة، اعتمادًا فقط على تحليل الألوان
    داخل الصورة نفسها (بدون أي خدمة خارجية). هذا الفحص تقريبي وليس دقيقًا
    بنسبة 100%، ويُنصح عند الحاجة لدقة أعلى بربط خدمة فحص محتوى متخصصة.
    """

    @staticmethod
    def looks_suspicious(image: Image.Image) -> bool:
        try:
            sample = image.convert("RGB")
            sample.thumbnail((200, 200))
            pixels = sample.load()
            w, h = sample.size
            total = w * h
            if total == 0:
                return False
            skin_count = 0
            for y in range(0, h, 2):
                for x in range(0, w, 2):
                    r, g, b = pixels[x, y]
                    if ContentModerator._is_skin(r, g, b):
                        skin_count += 1
            sampled = (w * h) // 4 if (w * h) // 4 else 1
            ratio = skin_count / sampled
            return ratio > 0.55
        except Exception:
            return False

    @staticmethod
    def _is_skin(r, g, b):
        return (
            r > 95 and g > 40 and b > 20
            and (max(r, g, b) - min(r, g, b)) > 15
            and abs(r - g) > 15
            and r > g and r > b
        )


# =========================================================================
# محرك الحقوق المائية
# =========================================================================

POSITIONS = {
    "tr": "أعلى اليمين",
    "tl": "أعلى اليسار",
    "c": "الوسط",
    "br": "أسفل اليمين",
    "bl": "أسفل اليسار",
    "tile": "توزيع على الصورة",
}

OPACITY_PRESETS = [10, 25, 40, 55, 70]

WATERMARK_ANGLE = -18  # درجة الميلان الافتراضية للحقوق


class WatermarkEngine:
    def __init__(self, fonts: FontManager):
        self.fonts = fonts

    # -------- تجهيز النص للعرض (عربي/إنجليزي) --------
    @staticmethod
    def _prepare_line(line: str) -> str:
        if ARABIC_SUPPORT and re.search(r"[\u0600-\u06FF]", line):
            try:
                reshaped = arabic_reshaper.reshape(line)
                return get_display(reshaped)
            except Exception:
                return line
        return line

    @staticmethod
    def _load_font(font_path, size):
        try:
            if font_path:
                return ImageFont.truetype(font_path, size)
        except Exception:
            pass
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    def _wrap_text(self, draw, text, font, max_width):
        words = text.split(" ")
        lines = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip()
            bbox = draw.textbbox((0, 0), self._prepare_line(candidate), font=font)
            width = bbox[2] - bbox[0]
            if width <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    def _fit_text(self, image_size, text, font_path):
        """يحسب حجم خط مناسب وأسطر ملائمة حسب أبعاد الصورة."""
        w, h = image_size
        max_width = int(w * 0.8)
        size = max(14, int(min(w, h) * 0.07))
        min_size = 12
        dummy = Image.new("RGBA", (10, 10))
        draw = ImageDraw.Draw(dummy)

        while size >= min_size:
            font = self._load_font(font_path, size)
            lines = self._wrap_text(draw, text, font, max_width)
            total_h = 0
            max_line_w = 0
            for ln in lines:
                bbox = draw.textbbox((0, 0), self._prepare_line(ln), font=font)
                lw = bbox[2] - bbox[0]
                lh = bbox[3] - bbox[1]
                total_h += lh + 6
                max_line_w = max(max_line_w, lw)
            if total_h <= h * 0.6 and max_line_w <= max_width:
                return font, lines, size
            size -= 2

        font = self._load_font(font_path, min_size)
        lines = self._wrap_text(draw, text, font, max_width)
        return font, lines, min_size

    def _render_text_block(self, text, font_path, alpha, color=(255, 255, 255)):
        """يرسم كتلة النص على صورة شفافة مستقلة قابلة للتدوير."""
        dummy = Image.new("RGBA", (10, 10))
        draw = ImageDraw.Draw(dummy)
        font, lines, size = self._fit_text((1600, 1600), text, font_path)

        line_sizes = []
        max_w = 0
        total_h = 0
        for ln in lines:
            prepared = self._prepare_line(ln)
            bbox = draw.textbbox((0, 0), prepared, font=font)
            lw = bbox[2] - bbox[0]
            lh = bbox[3] - bbox[1]
            line_sizes.append((prepared, lw, lh))
            max_w = max(max_w, lw)
            total_h += lh + 8

        pad = 14
        block = Image.new("RGBA", (max_w + pad * 2, total_h + pad * 2), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(block)
        y = pad
        for prepared, lw, lh in line_sizes:
            x = (block.width - lw) // 2
            bdraw.text(
                (x, y), prepared, font=font, fill=(color[0], color[1], color[2], alpha)
            )
            y += lh + 8
        return block, font, size

    def _paste_with_position(self, base, block, position, margin=24):
        bw, bh = base.size
        # إذا كانت الكتلة (بعد التدوير) أكبر من المساحة المتاحة، يتم تصغيرها
        # بنفس النسبة حتى تبقى الحقوق كاملة وداخل حدود الصورة.
        max_w = max(10, bw - margin * 2)
        max_h = max(10, bh - margin * 2)
        if block.width > max_w or block.height > max_h:
            scale = min(max_w / block.width, max_h / block.height)
            new_size = (max(1, int(block.width * scale)), max(1, int(block.height * scale)))
            block = block.resize(new_size, Image.LANCZOS)
        tw, th = block.size
        if position == "tr":
            xy = (bw - tw - margin, margin)
        elif position == "tl":
            xy = (margin, margin)
        elif position == "br":
            xy = (bw - tw - margin, bh - th - margin)
        elif position == "bl":
            xy = (margin, bh - th - margin)
        else:  # center
            xy = ((bw - tw) // 2, (bh - th) // 2)
        x = min(max(0, xy[0]), max(0, bw - tw))
        y = min(max(0, xy[1]), max(0, bh - th))
        base.alpha_composite(block, dest=(x, y))

    def _apply_tiled(self, base, text, font_path, alpha):
        bw, bh = base.size
        # حجم أصغر نسبيًا لكل وحدة في وضع التوزيع
        small_size = max(16, int(min(bw, bh) * 0.045))
        font = self._load_font(font_path, small_size)
        dummy_draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        prepared = self._prepare_line(text)
        bbox = dummy_draw.textbbox((0, 0), prepared, font=font)
        tw = bbox[2] - bbox[0] + 30
        th = bbox[3] - bbox[1] + 30

        tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(tile)
        tdraw.text((15, 15), prepared, font=font, fill=(255, 255, 255, alpha))
        tile = tile.rotate(WATERMARK_ANGLE, expand=True, resample=Image.BICUBIC)

        step_x = tile.width + 40
        step_y = tile.height + 40
        row = 0
        y = -tile.height
        while y < bh + tile.height:
            offset = (step_x // 2) if (row % 2) else 0
            x = -tile.width + offset
            while x < bw + tile.width:
                base.alpha_composite(tile, dest=(x, y))
                x += step_x
            y += step_y
            row += 1

    def apply(self, input_path, output_path, text, position, font_path, opacity):
        alpha = max(1, min(255, int(255 * (opacity / 100.0))))
        with Image.open(input_path) as img:
            img = img.convert("RGBA")
            base = img.copy()

            if position == "tile":
                self._apply_tiled(base, text, font_path, alpha)
            else:
                block, _, _ = self._render_text_block(text, font_path, alpha)
                block = block.rotate(WATERMARK_ANGLE, expand=True, resample=Image.BICUBIC)
                self._paste_with_position(base, block, position)

            out = base.convert("RGB")
            out.save(output_path, format="JPEG", quality=92)
        return output_path


watermark_engine = WatermarkEngine(font_manager)


# =========================================================================
# نظام Anti-Flood
# =========================================================================

class FloodControl:
    def __init__(self, db: Database):
        self.db = db
        self.lock = threading.Lock()
        self.request_times = {}  # user_id -> deque[timestamps]
        self.flood_strikes = {}  # user_id -> count
        self.active_per_user = {}  # user_id -> count
        self.active_global = 0

    def _cfg(self):
        window = self.db.get_setting("flood_window_seconds", 60, int)
        max_req = self.db.get_setting("flood_max_requests", 5, int)
        ban_seconds = self.db.get_setting("flood_ban_seconds", 300, int)
        return window, max_req, ban_seconds

    def check_and_register(self, user_id):
        """يتحقق من حد الفلود، ويسجل الطلب الحالي. يرجع (مسموح، رسالة)."""
        window, max_req, ban_seconds = self._cfg()
        now = time.time()
        with self.lock:
            dq = self.request_times.setdefault(user_id, deque())
            while dq and now - dq[0] > window:
                dq.popleft()
            if len(dq) >= max_req:
                strikes = self.flood_strikes.get(user_id, 0) + 1
                self.flood_strikes[user_id] = strikes
                if strikes >= 2:
                    self.db.ban_user(
                        user_id,
                        reason="حظر مؤقت تلقائي بسبب تكرار الإرسال السريع",
                        until=int(now) + ban_seconds,
                    )
                    self.flood_strikes[user_id] = 0
                    return False, (
                        "تم إيقافك مؤقتًا بسبب إرسال عدد كبير من الطلبات خلال "
                        "وقت قصير. حاول مرة أخرى لاحقًا."
                    )
                return False, (
                    "الرجاء الانتظار قليلاً قبل إرسال صورة جديدة، لقد تجاوزت "
                    "الحد المسموح به من الطلبات."
                )
            dq.append(now)
            return True, ""

    def try_acquire_slot(self, user_id):
        max_user = self.db.get_setting("max_concurrent_per_user", 1, int)
        max_global = self.db.get_setting("max_concurrent_global", 4, int)
        with self.lock:
            if self.active_global >= max_global:
                return False, "الخادم مشغول حاليًا بمعالجة صور أخرى، حاول بعد قليل."
            if self.active_per_user.get(user_id, 0) >= max_user:
                return False, "لديك عملية معالجة قيد التنفيذ بالفعل، انتظر حتى تنتهي."
            self.active_global += 1
            self.active_per_user[user_id] = self.active_per_user.get(user_id, 0) + 1
            return True, ""

    def release_slot(self, user_id):
        with self.lock:
            self.active_global = max(0, self.active_global - 1)
            if user_id in self.active_per_user:
                self.active_per_user[user_id] = max(0, self.active_per_user[user_id] - 1)


flood = FloodControl(db)


# =========================================================================
# إدارة حالة المحادثة (Sessions)
# =========================================================================

class SessionManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}   # user_id -> dict
        self.admin_state = {}  # user_id -> awaiting action string

    def start(self, user_id, image_path):
        with self.lock:
            self.sessions[user_id] = {
                "step": "waiting_text",
                "image_path": image_path,
                "text": None,
                "position": None,
                "font_index": None,
            }

    def get(self, user_id):
        with self.lock:
            return self.sessions.get(user_id)

    def update(self, user_id, **kwargs):
        with self.lock:
            if user_id in self.sessions:
                self.sessions[user_id].update(kwargs)

    def clear(self, user_id):
        with self.lock:
            session = self.sessions.pop(user_id, None)
        if session and session.get("image_path"):
            _safe_remove(session["image_path"])

    def set_admin_state(self, user_id, state):
        with self.lock:
            if state is None:
                self.admin_state.pop(user_id, None)
            else:
                self.admin_state[user_id] = state

    def get_admin_state(self, user_id):
        with self.lock:
            return self.admin_state.get(user_id)


sessions = SessionManager()


def _safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _user_temp_dir(user_id):
    d = os.path.join(TMP_ROOT, str(user_id))
    os.makedirs(d, exist_ok=True)
    return d


# =========================================================================
# دوال مساعدة عامة
# =========================================================================

def is_admin(user_id):
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_subscribed(user_id):
    enabled = db.get_setting("force_sub_enabled", "1") == "1"
    channel_id = db.get_setting("force_channel_id", "0", int)
    if not enabled or not channel_id:
        return True
    try:
        member = bot.get_chat_member(channel_id, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def subscribe_keyboard():
    kb = types.InlineKeyboardMarkup()
    username = db.get_setting("force_channel_username", "")
    if username:
        url = "https://t.me/" + username.lstrip("@")
        kb.add(types.InlineKeyboardButton("الاشتراك بالقناة", url=url))
    kb.add(types.InlineKeyboardButton("تحقق من الاشتراك", callback_data="check_sub"))
    return kb


def send_subscribe_prompt(chat_id):
    bot.send_message(
        chat_id,
        "يجب عليك الاشتراك في القناة أولاً لاستخدام هذا البوت.\n"
        "بعد الاشتراك اضغط على زر تحقق من الاشتراك.",
        reply_markup=subscribe_keyboard(),
    )


def position_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(POSITIONS["tr"], callback_data="pos:tr"),
        types.InlineKeyboardButton(POSITIONS["tl"], callback_data="pos:tl"),
    )
    kb.add(
        types.InlineKeyboardButton(POSITIONS["br"], callback_data="pos:br"),
        types.InlineKeyboardButton(POSITIONS["bl"], callback_data="pos:bl"),
    )
    kb.add(types.InlineKeyboardButton(POSITIONS["c"], callback_data="pos:c"))
    kb.add(types.InlineKeyboardButton(POSITIONS["tile"], callback_data="pos:tile"))
    return kb


def font_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    fonts = font_manager.fonts
    if not fonts:
        kb.add(types.InlineKeyboardButton("الخط الافتراضي", callback_data="font:-1"))
        return kb
    for idx, (display, _path) in enumerate(fonts):
        kb.add(types.InlineKeyboardButton(display, callback_data=f"font:{idx}"))
    return kb


def opacity_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=3)
    default_op = db.get_setting("default_opacity", 25, int)
    buttons = []
    for op in OPACITY_PRESETS:
        label = f"{op}%" + ("  (افتراضي)" if op == default_op else "")
        buttons.append(types.InlineKeyboardButton(label, callback_data=f"op:{op}"))
    kb.add(*buttons)
    return kb


# =========================================================================
# أوامر المستخدم
# =========================================================================

@bot.message_handler(commands=["start"])
def handle_start(message):
    try:
        user_id = message.from_user.id
        db.touch_user(user_id, message.from_user.username, message.from_user.first_name)

        if db.is_banned(user_id):
            bot.reply_to(message, "أنت محظور حاليًا من استخدام هذا البوت.")
            return

        if db.get_setting("maintenance_mode", "0") == "1" and not is_admin(user_id):
            bot.reply_to(message, "البوت في وضع الصيانة حاليًا، الرجاء المحاولة لاحقًا.")
            return

        if not is_subscribed(user_id):
            send_subscribe_prompt(message.chat.id)
            return

        bot.reply_to(
            message,
            "مرحبًا بك في بوت الحقوق المائية.\n\n"
            "أرسل الصورة التي تريد إضافة الحقوق عليها، وسيقوم البوت بإرشادك "
            "خطوة بخطوة لاختيار النص والمكان والخط ونسبة الشفافية.",
        )
    except Exception as e:
        log.warning("start error: %s", e)


@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def handle_check_sub(call):
    try:
        user_id = call.from_user.id
        if is_subscribed(user_id):
            bot.answer_callback_query(call.id, "تم التحقق من الاشتراك بنجاح.")
            bot.edit_message_text(
                "تم التحقق من اشتراكك. أرسل الآن الصورة التي تريد إضافة الحقوق عليها.",
                call.message.chat.id,
                call.message.message_id,
            )
        else:
            bot.answer_callback_query(
                call.id, "لم يتم العثور على اشتراكك في القناة بعد.", show_alert=True
            )
    except Exception as e:
        log.warning("check_sub error: %s", e)


@bot.message_handler(content_types=["photo", "document"])
def handle_photo(message):
    user_id = message.from_user.id
    try:
        db.touch_user(user_id, message.from_user.username, message.from_user.first_name)

        if db.is_banned(user_id):
            bot.reply_to(message, "أنت محظور حاليًا من استخدام هذا البوت.")
            return

        if db.get_setting("maintenance_mode", "0") == "1" and not is_admin(user_id):
            bot.reply_to(message, "البوت في وضع الصيانة حاليًا، الرجاء المحاولة لاحقًا.")
            return

        if not is_subscribed(user_id):
            send_subscribe_prompt(message.chat.id)
            return

        allowed, flood_msg = flood.check_and_register(user_id)
        if not allowed:
            bot.reply_to(message, flood_msg)
            return

        # تحديد الملف والتحقق من نوعه
        if message.content_type == "photo":
            file_info_id = message.photo[-1].file_id
            declared_size = message.photo[-1].file_size or 0
        else:
            doc = message.document
            mime = (doc.mime_type or "")
            if not mime.startswith("image/"):
                bot.reply_to(message, "هذا النوع من الملفات غير مدعوم، الرجاء إرسال صورة.")
                return
            file_info_id = doc.file_id
            declared_size = doc.file_size or 0

        max_mb = db.get_setting("max_image_mb", 10, int)
        if declared_size and declared_size > max_mb * 1024 * 1024:
            bot.reply_to(
                message,
                f"حجم الصورة أكبر من الحد المسموح به ({max_mb} ميجابايت).",
            )
            return

        file_info = bot.get_file(file_info_id)
        file_bytes = bot.download_file(file_info.file_path)

        if len(file_bytes) > max_mb * 1024 * 1024:
            bot.reply_to(
                message, f"حجم الصورة أكبر من الحد المسموح به ({max_mb} ميجابايت)."
            )
            return

        try:
            img = Image.open(io.BytesIO(file_bytes))
            img.verify()
            img = Image.open(io.BytesIO(file_bytes))  # verify يغلق الملف، نعيد الفتح
        except Exception:
            bot.reply_to(message, "تعذر التعرف على الصورة، الرجاء إرسال صورة صالحة.")
            return

        max_dim = db.get_setting("max_image_dimension", 6000, int)
        if max(img.size) > max_dim:
            bot.reply_to(
                message,
                "أبعاد الصورة كبيرة جدًا، الرجاء إرسال صورة بأبعاد أصغر.",
            )
            return

        if ContentModerator.looks_suspicious(img):
            bot.reply_to(
                message,
                "تم رفض هذه الصورة لأنها قد تحتوي على محتوى غير مسموح به.",
            )
            return

        user_dir = _user_temp_dir(user_id)
        image_path = os.path.join(user_dir, f"src_{int(time.time())}.png")
        img.convert("RGB").save(image_path, format="PNG")

        sessions.start(user_id, image_path)
        bot.reply_to(
            message,
            "تم استلام الصورة. الرجاء الآن كتابة النص الذي تريد وضعه كحقوق ملكية.",
        )
    except Exception as e:
        log.warning("photo handler error: %s", e)
        try:
            bot.reply_to(message, "حدث خطأ غير متوقع أثناء استقبال الصورة، حاول مرة أخرى.")
        except Exception:
            pass


@bot.message_handler(content_types=["text"])
def handle_text(message):
    user_id = message.from_user.id
    text = message.text.strip()

    # أوامر عامة
    if text == "/admin":
        return handle_admin_entry(message)

    # حالة انتظار إدخال من الأدمن
    admin_state = sessions.get_admin_state(user_id)
    if admin_state and is_admin(user_id):
        return process_admin_text_input(message, admin_state)

    # حالة انتظار نص الحقوق
    session = sessions.get(user_id)
    if not session or session.get("step") != "waiting_text":
        return  # رسالة عادية لا تحتاج رد

    try:
        if not text:
            bot.reply_to(message, "الرجاء إرسال نص غير فارغ.")
            return
        if len(text) > 250:
            bot.reply_to(message, "النص طويل جدًا، الرجاء اختصاره إلى أقل من 250 حرفًا.")
            return

        sessions.update(user_id, text=text, step="waiting_position")
        bot.reply_to(message, "اختر مكان الحقوق على الصورة:", reply_markup=position_keyboard())
    except Exception as e:
        log.warning("text handler error: %s", e)


@bot.callback_query_handler(func=lambda c: c.data.startswith("pos:"))
def handle_position_choice(call):
    user_id = call.from_user.id
    try:
        session = sessions.get(user_id)
        if not session or session.get("step") != "waiting_position":
            bot.answer_callback_query(call.id, "انتهت صلاحية هذه الخطوة، ابدأ من جديد بإرسال صورة.")
            return
        position = call.data.split(":", 1)[1]
        sessions.update(user_id, position=position, step="waiting_font")
        bot.answer_callback_query(call.id)
        font_manager.reload()
        bot.edit_message_text(
            "اختر الخط الذي تريد استخدامه:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=font_keyboard(),
        )
    except Exception as e:
        log.warning("position choice error: %s", e)


@bot.callback_query_handler(func=lambda c: c.data.startswith("font:"))
def handle_font_choice(call):
    user_id = call.from_user.id
    try:
        session = sessions.get(user_id)
        if not session or session.get("step") != "waiting_font":
            bot.answer_callback_query(call.id, "انتهت صلاحية هذه الخطوة، ابدأ من جديد بإرسال صورة.")
            return
        font_index = int(call.data.split(":", 1)[1])
        sessions.update(user_id, font_index=font_index, step="waiting_opacity")
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "اختر نسبة شفافية الحقوق:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=opacity_keyboard(),
        )
    except Exception as e:
        log.warning("font choice error: %s", e)


@bot.callback_query_handler(func=lambda c: c.data.startswith("op:"))
def handle_opacity_choice(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    try:
        session = sessions.get(user_id)
        if not session or session.get("step") != "waiting_opacity":
            bot.answer_callback_query(call.id, "انتهت صلاحية هذه الخطوة، ابدأ من جديد بإرسال صورة.")
            return

        if not is_subscribed(user_id):
            bot.answer_callback_query(call.id)
            send_subscribe_prompt(chat_id)
            return

        opacity = int(call.data.split(":", 1)[1])
        bot.answer_callback_query(call.id, "جاري معالجة الصورة...")

        allowed, msg = flood.try_acquire_slot(user_id)
        if not allowed:
            bot.send_message(chat_id, msg)
            return

        try:
            bot.edit_message_text(
                "جاري معالجة الصورة، الرجاء الانتظار...",
                chat_id,
                call.message.message_id,
            )

            font_entry = font_manager.get(session["font_index"])
            font_path = font_entry[1] if font_entry else None

            input_path = session["image_path"]
            output_path = input_path.replace("src_", "out_").rsplit(".", 1)[0] + ".jpg"

            watermark_engine.apply(
                input_path,
                output_path,
                session["text"],
                session["position"],
                font_path,
                opacity,
            )

            with open(output_path, "rb") as f:
                bot.send_photo(chat_id, f, caption="تم إضافة الحقوق بنجاح.")

            db.increment_images(user_id)
            _safe_remove(output_path)
        finally:
            flood.release_slot(user_id)
            sessions.clear(user_id)
    except Exception as e:
        log.warning("opacity choice error: %s", e)
        try:
            bot.send_message(chat_id, "حدث خطأ أثناء معالجة الصورة، الرجاء المحاولة مرة أخرى.")
        except Exception:
            pass
        sessions.clear(user_id)


# =========================================================================
# لوحة تحكم الأدمن
# =========================================================================

def admin_main_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("الإحصائيات", callback_data="adm:stats"),
        types.InlineKeyboardButton("إدارة المحظورين", callback_data="adm:banned"),
        types.InlineKeyboardButton("الاشتراك الإجباري", callback_data="adm:sub"),
        types.InlineKeyboardButton("إعدادات الحماية من الفلود", callback_data="adm:flood"),
        types.InlineKeyboardButton("إعدادات حجم الصور", callback_data="adm:size"),
        types.InlineKeyboardButton("نسبة الشفافية الافتراضية", callback_data="adm:opacity"),
        types.InlineKeyboardButton("إدارة الخطوط", callback_data="adm:fonts"),
        types.InlineKeyboardButton("وضع الصيانة", callback_data="adm:maint"),
    )
    return kb


def back_button(target="adm:main"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("رجوع", callback_data=target))
    return kb


def handle_admin_entry(message):
    if not is_admin(message.from_user.id):
        return
    bot.reply_to(message, "لوحة تحكم الأدمن:", reply_markup=admin_main_keyboard())


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm:"))
def handle_admin_callbacks(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "غير مسموح لك بهذا الإجراء.", show_alert=True)
        return

    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    try:
        bot.answer_callback_query(call.id)

        if data == "adm:main":
            sessions.set_admin_state(user_id, None)
            bot.edit_message_text("لوحة تحكم الأدمن:", chat_id, msg_id, reply_markup=admin_main_keyboard())

        elif data == "adm:stats":
            s = db.stats()
            text = (
                "إحصائيات البوت:\n\n"
                f"إجمالي المستخدمين: {s['total']}\n"
                f"مستخدمون جدد اليوم: {s['new_today']}\n"
                f"عدد المحظورين: {s['banned']}\n"
                f"إجمالي الصور المعالجة: {s['images']}"
            )
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=back_button())

        elif data == "adm:banned":
            show_banned_list(chat_id, msg_id)

        elif data.startswith("adm:unban:"):
            target = int(data.split(":")[2])
            db.unban_user(target)
            bot.answer_callback_query(call.id, "تم فك الحظر.")
            show_banned_list(chat_id, msg_id)

        elif data == "adm:ban:new":
            sessions.set_admin_state(user_id, "awaiting_ban_id")
            bot.edit_message_text(
                "أرسل رقم آيدي المستخدم الذي تريد حظره:",
                chat_id, msg_id, reply_markup=back_button("adm:banned"),
            )

        elif data == "adm:sub":
            show_sub_menu(chat_id, msg_id)

        elif data == "adm:sub:toggle":
            current = db.get_setting("force_sub_enabled", "1")
            db.set_setting("force_sub_enabled", "0" if current == "1" else "1")
            show_sub_menu(chat_id, msg_id)

        elif data == "adm:sub:setchannel":
            sessions.set_admin_state(user_id, "awaiting_channel_info")
            bot.edit_message_text(
                "أرسل معرف القناة الرقمي ثم اسم المستخدم مفصولين بمسافة.\n"
                "مثال:\n-1001234567890 my_channel",
                chat_id, msg_id, reply_markup=back_button("adm:sub"),
            )

        elif data == "adm:flood":
            show_flood_menu(chat_id, msg_id)

        elif data.startswith("adm:flood:"):
            adjust_flood_setting(data)
            show_flood_menu(chat_id, msg_id)

        elif data == "adm:size":
            show_size_menu(chat_id, msg_id)

        elif data.startswith("adm:size:"):
            adjust_size_setting(data)
            show_size_menu(chat_id, msg_id)

        elif data == "adm:opacity":
            show_opacity_menu(chat_id, msg_id)

        elif data.startswith("adm:opacity:"):
            adjust_opacity_setting(data)
            show_opacity_menu(chat_id, msg_id)

        elif data == "adm:fonts":
            font_manager.reload()
            names = [d for d, _ in font_manager.fonts] or ["لا توجد خطوط مضافة حاليًا"]
            text = "الخطوط المتوفرة حاليًا:\n\n" + "\n".join(names)
            text += (
                "\n\nلإضافة خط جديد، ضع ملف الخط بصيغة ttf أو otf داخل مجلد "
                "fonts في المشروع ثم أعد النشر."
            )
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("إعادة الفحص", callback_data="adm:fonts"))
            kb.add(types.InlineKeyboardButton("رجوع", callback_data="adm:main"))
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)

        elif data == "adm:maint":
            current = db.get_setting("maintenance_mode", "0")
            db.set_setting("maintenance_mode", "0" if current == "1" else "1")
            new_state = db.get_setting("maintenance_mode", "0")
            label = "مفعل" if new_state == "1" else "غير مفعل"
            bot.edit_message_text(
                f"وضع الصيانة حاليًا: {label}",
                chat_id, msg_id, reply_markup=back_button(),
            )

    except Exception as e:
        log.warning("admin callback error: %s", e)


def show_banned_list(chat_id, msg_id):
    rows = db.list_banned()
    kb = types.InlineKeyboardMarkup(row_width=1)
    if not rows:
        text = "لا يوجد مستخدمون محظورون حاليًا."
    else:
        text = "المستخدمون المحظورون:\n"
        for user_id, username, reason, until in rows:
            label = f"فك حظر {username or user_id}"
            kb.add(types.InlineKeyboardButton(label, callback_data=f"adm:unban:{user_id}"))
    kb.add(types.InlineKeyboardButton("حظر مستخدم جديد", callback_data="adm:ban:new"))
    kb.add(types.InlineKeyboardButton("رجوع", callback_data="adm:main"))
    bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)


def show_sub_menu(chat_id, msg_id):
    enabled = db.get_setting("force_sub_enabled", "1") == "1"
    channel_username = db.get_setting("force_channel_username", "") or "غير محدد"
    channel_id = db.get_setting("force_channel_id", "0")
    text = (
        "إعدادات الاشتراك الإجباري:\n\n"
        f"الحالة: {'مفعل' if enabled else 'غير مفعل'}\n"
        f"القناة: {channel_username}\n"
        f"معرف القناة: {channel_id}"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            "تعطيل" if enabled else "تفعيل", callback_data="adm:sub:toggle"
        ),
        types.InlineKeyboardButton("تغيير القناة", callback_data="adm:sub:setchannel"),
        types.InlineKeyboardButton("رجوع", callback_data="adm:main"),
    )
    bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)


def show_flood_menu(chat_id, msg_id):
    window = db.get_setting("flood_window_seconds", 60, int)
    max_req = db.get_setting("flood_max_requests", 5, int)
    ban_seconds = db.get_setting("flood_ban_seconds", 300, int)
    max_user = db.get_setting("max_concurrent_per_user", 1, int)
    max_global = db.get_setting("max_concurrent_global", 4, int)
    text = (
        "إعدادات الحماية من الفلود:\n\n"
        f"عدد الصور المسموح بها كل {window} ثانية: {max_req}\n"
        f"مدة الحظر التلقائي عند تكرار الفلود: {ban_seconds} ثانية\n"
        f"حد المعالجة المتزامنة لكل مستخدم: {max_user}\n"
        f"الحد العالمي للمعالجة المتزامنة: {max_global}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("+ عدد الصور", callback_data="adm:flood:inc_max"),
        types.InlineKeyboardButton("- عدد الصور", callback_data="adm:flood:dec_max"),
        types.InlineKeyboardButton("+ مدة الحظر", callback_data="adm:flood:inc_ban"),
        types.InlineKeyboardButton("- مدة الحظر", callback_data="adm:flood:dec_ban"),
        types.InlineKeyboardButton("+ الحد العالمي", callback_data="adm:flood:inc_global"),
        types.InlineKeyboardButton("- الحد العالمي", callback_data="adm:flood:dec_global"),
    )
    kb.add(types.InlineKeyboardButton("رجوع", callback_data="adm:main"))
    bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)


def adjust_flood_setting(data):
    action = data.split(":")[2]
    if action == "inc_max":
        v = db.get_setting("flood_max_requests", 5, int)
        db.set_setting("flood_max_requests", min(50, v + 1))
    elif action == "dec_max":
        v = db.get_setting("flood_max_requests", 5, int)
        db.set_setting("flood_max_requests", max(1, v - 1))
    elif action == "inc_ban":
        v = db.get_setting("flood_ban_seconds", 300, int)
        db.set_setting("flood_ban_seconds", min(86400, v + 60))
    elif action == "dec_ban":
        v = db.get_setting("flood_ban_seconds", 300, int)
        db.set_setting("flood_ban_seconds", max(30, v - 60))
    elif action == "inc_global":
        v = db.get_setting("max_concurrent_global", 4, int)
        db.set_setting("max_concurrent_global", min(50, v + 1))
    elif action == "dec_global":
        v = db.get_setting("max_concurrent_global", 4, int)
        db.set_setting("max_concurrent_global", max(1, v - 1))


def show_size_menu(chat_id, msg_id):
    max_mb = db.get_setting("max_image_mb", 10, int)
    max_dim = db.get_setting("max_image_dimension", 6000, int)
    text = (
        "إعدادات حجم الصور:\n\n"
        f"أقصى حجم للصورة: {max_mb} ميجابايت\n"
        f"أقصى أبعاد للصورة: {max_dim} بكسل"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("+ الحجم", callback_data="adm:size:inc_mb"),
        types.InlineKeyboardButton("- الحجم", callback_data="adm:size:dec_mb"),
        types.InlineKeyboardButton("+ الأبعاد", callback_data="adm:size:inc_dim"),
        types.InlineKeyboardButton("- الأبعاد", callback_data="adm:size:dec_dim"),
    )
    kb.add(types.InlineKeyboardButton("رجوع", callback_data="adm:main"))
    bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)


def adjust_size_setting(data):
    action = data.split(":")[2]
    if action == "inc_mb":
        v = db.get_setting("max_image_mb", 10, int)
        db.set_setting("max_image_mb", min(50, v + 1))
    elif action == "dec_mb":
        v = db.get_setting("max_image_mb", 10, int)
        db.set_setting("max_image_mb", max(1, v - 1))
    elif action == "inc_dim":
        v = db.get_setting("max_image_dimension", 6000, int)
        db.set_setting("max_image_dimension", min(15000, v + 500))
    elif action == "dec_dim":
        v = db.get_setting("max_image_dimension", 6000, int)
        db.set_setting("max_image_dimension", max(1000, v - 500))


def show_opacity_menu(chat_id, msg_id):
    default_op = db.get_setting("default_opacity", 25, int)
    text = f"نسبة الشفافية الافتراضية الحالية: {default_op}%"
    kb = types.InlineKeyboardMarkup(row_width=3)
    buttons = [
        types.InlineKeyboardButton(f"{op}%", callback_data=f"adm:opacity:set:{op}")
        for op in OPACITY_PRESETS
    ]
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton("رجوع", callback_data="adm:main"))
    bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)


def adjust_opacity_setting(data):
    parts = data.split(":")
    if parts[2] == "set":
        db.set_setting("default_opacity", int(parts[3]))


def process_admin_text_input(message, state):
    user_id = message.from_user.id
    text = message.text.strip()
    try:
        if state == "awaiting_ban_id":
            if not text.isdigit():
                bot.reply_to(message, "الرجاء إرسال رقم آيدي صحيح.")
                return
            target = int(text)
            db.ban_user(target, reason="حظر يدوي من الأدمن", until=0)
            sessions.set_admin_state(user_id, None)
            bot.reply_to(message, f"تم حظر المستخدم {target}.", reply_markup=admin_main_keyboard())

        elif state == "awaiting_channel_info":
            parts = text.split()
            if len(parts) < 2 or not re.match(r"^-?\d+$", parts[0]):
                bot.reply_to(
                    message,
                    "صيغة غير صحيحة. أرسل: معرف القناة الرقمي ثم اسم المستخدم.",
                )
                return
            db.set_setting("force_channel_id", parts[0])
            db.set_setting("force_channel_username", parts[1].lstrip("@"))
            sessions.set_admin_state(user_id, None)
            bot.reply_to(message, "تم تحديث بيانات القناة بنجاح.", reply_markup=admin_main_keyboard())
    except Exception as e:
        log.warning("admin text input error: %s", e)


# =========================================================================
# التشغيل
# =========================================================================

def cleanup_old_temp_files():
    """تنظيف دوري لأي ملفات مؤقتة قديمة قد تبقى بسبب انقطاع مفاجئ."""
    try:
        cutoff = time.time() - 3600
        for root, _dirs, files in os.walk(TMP_ROOT):
            for name in files:
                path = os.path.join(root, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except Exception:
                    pass
    except Exception:
        pass


def cleanup_loop():
    while True:
        cleanup_old_temp_files()
        time.sleep(1800)


def main():
    if not ARABIC_SUPPORT:
        log.warning(
            "مكتبات دعم العربية (arabic_reshaper / python-bidi) غير مثبتة، "
            "سيتم عرض النصوص العربية بدون تشكيل صحيح."
        )
    if not font_manager.has_fonts():
        log.warning(
            "لا توجد خطوط داخل مجلد fonts، الرجاء إضافة ملفات ttf لضمان جودة الحقوق."
        )

    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)
        except Exception as e:
            log.warning("polling crashed, restarting in 5 seconds: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
