"""
Mini App API — лёгкий веб-сервер, отдающий данные игрока для Telegram Mini App.

Работает параллельно с ботом (в отдельном потоке).
Безопасность: проверяет подпись Telegram initData, поэтому игрок может
получить ТОЛЬКО свои данные, а не чужие.
"""
import hashlib
import hmac
import json
import logging
import os
import threading
from urllib.parse import parse_qsl

from aiohttp import web

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")


def _verify_init_data(init_data: str) -> dict | None:
    """Проверяет подпись Telegram initData. Возвращает данные пользователя или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        # Строка для проверки: все поля кроме hash, отсортированы, через \n
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        # Секретный ключ = HMAC-SHA256("WebAppData", bot_token)
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        user_json = parsed.get("user")
        if not user_json:
            return None
        return json.loads(user_json)
    except Exception as e:
        logger.warning("initData verify failed: %s", e)
        return None


# Заголовки CORS (разрешаем запросы со страницы GitHub Pages)
def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def handle_options(request):
    return _cors(web.Response(text=""))


async def handle_health(request):
    """Проверка живости — открой этот адрес в браузере, должно быть OK."""
    return _cors(web.json_response({"status": "ok", "service": "hogwarts-miniapp-api"}))


async def handle_profile(request):
    """Главный эндпоинт — отдаёт профиль игрока по проверенному initData."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))

    init_data = body.get("initData", "")
    tg_user = _verify_init_data(init_data)
    if not tg_user:
        return _cors(web.json_response({"error": "unauthorized"}, status=401))

    user_id = tg_user.get("id")
    if not user_id:
        return _cors(web.json_response({"error": "no user id"}, status=400))

    # Импорт здесь, чтобы избежать циклических импортов при старте
    from database import get_user
    from utils.helpers import get_rank, xp_needed_for_level

    user = get_user(int(user_id))
    if not user:
        return _cors(web.json_response({"registered": False}))

    house_names = {
        "gryffindor": "Гриффиндор", "slytherin": "Слизерин",
        "ravenclaw":  "Когтевран",  "hufflepuff": "Пуффендуй",
    }
    house_emojis = {
        "gryffindor": "🦁", "slytherin": "🐍",
        "ravenclaw":  "🦅", "hufflepuff": "🦡",
    }
    house = user.get("house", "gryffindor")

    try:
        xp_need = xp_needed_for_level(user.get("level", 1))
    except Exception:
        xp_need = 150
    try:
        rank = get_rank(user.get("level", 1))
    except Exception:
        rank = "🐣 Первокурсник"

    data = {
        "registered": True,
        "name":       user.get("wizard_name", "Волшебник"),
        "house":      house_names.get(house, "Хогвартс"),
        "houseEmoji": house_emojis.get(house, "🏰"),
        "rank":       rank,
        "level":      user.get("level", 1),
        "hp":         user.get("hp", 100),
        "maxHp":      user.get("max_hp", 100),
        "mana":       user.get("mana", 50),
        "maxMana":    user.get("max_mana", 50),
        "xp":         user.get("xp", 0),
        "maxXp":      xp_need,
        "atk":        user.get("attack", 10),
        "def":        user.get("defense", 5),
        "spd":        user.get("speed", 10),
        "luck":       user.get("luck", 5),
        "gold":       user.get("gold", 0),
        "id":         user_id,
    }

    # Титул
    data["title"] = user.get("title") or ""

    # Винрейт дуэлей
    try:
        from database import get_conn as _gc, fetchrow as _fr
        with _gc() as conn:
            st = _fr(conn, "SELECT pvp_wins, pvp_losses FROM user_stats WHERE user_id=%s", int(user_id))
        wins = (st or {}).get("pvp_wins", 0) or 0
        losses = (st or {}).get("pvp_losses", 0) or 0
        total = wins + losses
        data["pvpWins"] = wins
        data["pvpLosses"] = losses
        data["winrate"] = (str(round(wins / total * 100)) + "%") if total else "—"
    except Exception:
        data["pvpWins"] = 0; data["pvpLosses"] = 0; data["winrate"] = "—"

    # Ранг в дуэльной лиге (ELO + дивизион)
    try:
        from handlers.duel_league import _get_rating, _get_division
        r = _get_rating(int(user_id))
        elo = r.get("elo", 1000)
        div_name, _ = _get_division(elo)
        data["elo"] = elo
        data["division"] = div_name
    except Exception:
        data["elo"] = 0; data["division"] = ""

    # Питомец
    try:
        from handlers.pets import _get_pet, PETS, _get_stage
        pet = _get_pet(int(user_id))
        if pet:
            pinfo = PETS.get(pet.get("pet_id"), {})
            stage = _get_stage(pet.get("level", 1))
            stages = pinfo.get("stages", [])
            pemoji = stages[stage]["emoji"] if stage < len(stages) else pinfo.get("emoji", "🐾")
            pname = stages[stage]["name"] if stage < len(stages) else pinfo.get("name", "Питомец")
            data["pet"] = {"emoji": pemoji, "name": pname, "level": pet.get("level", 1)}
        else:
            data["pet"] = None
    except Exception:
        data["pet"] = None

    # Снаряжение (надетые предметы по слотам)
    try:
        from database import get_conn as _gc2, fetchall as _fa2
        from game.items import ITEMS as _IT, item_display_name as _idn
        with _gc2() as conn:
            eq_rows = _fa2(conn, "SELECT slot, item_id, bonus FROM equipped_items WHERE user_id=%s", int(user_id))
        slot_names = {"weapon": "Оружие", "armor": "Броня", "accessory": "Аксессуар",
                      "helmet": "Шлем", "boots": "Обувь", "cloak": "Мантия"}
        equipment = []
        for r in eq_rows:
            idata = _IT.get(r["item_id"], {})
            equipment.append({
                "slot": slot_names.get(r["slot"], r["slot"]),
                "name": _idn(idata, "ru") if idata else r["item_id"],
                "emoji": idata.get("emoji", "🔲"),
                "bonus": r.get("bonus", 0),
            })
        data["equipment"] = equipment
    except Exception:
        data["equipment"] = []

    # Палочка
    try:
        wood = user.get("wand_wood"); core = user.get("wand_core")
        if wood or core:
            data["wand"] = {"wood": wood or "—", "core": core or "—"}
        else:
            data["wand"] = None
    except Exception:
        data["wand"] = None

    # Статистика
    try:
        from database import get_user_stats
        s = get_user_stats(int(user_id)) or {}
        data["stats"] = {
            "pveKills":   s.get("pve_kills", 0) or 0,
            "bossKills":  s.get("boss_kills", 0) or 0,
            "worldBoss":  s.get("world_boss_kills", 0) or 0,
            "potions":    s.get("potions_brewed", 0) or 0,
            "combos":     s.get("combo_used", 0) or 0,
            "goldEarned": s.get("gold_earned", 0) or 0,
        }
    except Exception:
        data["stats"] = {}

    return _cors(web.json_response(data))


async def handle_leaderboard(request):
    """Топ игроков. Категория через ?cat=level|gold|pvp. Публичный."""
    cat = request.query.get("cat", "level")
    if cat not in ("level", "gold", "pvp"):
        cat = "level"
    try:
        from database import get_leaderboard
        rows = get_leaderboard(cat, 15)
    except Exception as e:
        logger.warning("leaderboard: %s", e)
        rows = []
    house_emojis = {
        "gryffindor": "🦁", "slytherin": "🐍",
        "ravenclaw":  "🦅", "hufflepuff": "🦡",
    }
    top = []
    for i, r in enumerate(rows, 1):
        if cat == "gold":
            metric = str(r.get("gold", 0)) + " 💰"
        elif cat == "pvp":
            metric = str(r.get("pvp_wins", 0)) + " 🏆"
        else:
            metric = "ур. " + str(r.get("level", 1))
        top.append({
            "place": i,
            "name":  r.get("wizard_name", "—"),
            "house": house_emojis.get(r.get("house"), "🏰"),
            "metric": metric,
        })
    return _cors(web.json_response({"top": top, "cat": cat}))


async def handle_inventory(request):
    """Инвентарь игрока — требует авторизации (initData)."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))

    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))

    user_id = int(tg_user["id"])
    from database import get_conn, fetchall
    from game.items import ITEMS, item_display_name

    try:
        with get_conn() as conn:
            rows = fetchall(conn,
                "SELECT item_id, quantity FROM inventory WHERE user_id=%s ORDER BY acquired_at DESC",
                user_id)
    except Exception as e:
        logger.warning("inventory: %s", e)
        rows = []

    rarity_emoji = {
        "common": "⚪", "uncommon": "🟢", "rare": "🔵", "very_rare": "🟣",
        "epic": "🟠", "legendary": "🔴", "mythical": "🌟", "abyssal": "⚫",
    }
    items = []
    for r in rows:
        iid = r.get("item_id")
        item = ITEMS.get(iid, {})
        try:
            nm = item_display_name(item, "ru") if item else iid
        except Exception:
            nm = iid
        rarity = item.get("rarity", "common")
        items.append({
            "name":   nm,
            "emoji":  item.get("emoji", "📦"),
            "rarity": rarity_emoji.get(rarity, "⚪"),
            "qty":    r.get("quantity", 1),
        })
    return _cors(web.json_response({"items": items}))


async def handle_housecup(request):
    """Очки факультетов (Кубок). Публичный."""
    try:
        from database import get_house_points
        rows = get_house_points()
    except Exception as e:
        logger.warning("housecup: %s", e)
        rows = []
    house_names = {
        "gryffindor": "Гриффиндор", "slytherin": "Слизерин",
        "ravenclaw":  "Когтевран",  "hufflepuff": "Пуффендуй",
    }
    house_emojis = {
        "gryffindor": "🦁", "slytherin": "🐍",
        "ravenclaw":  "🦅", "hufflepuff": "🦡",
    }
    houses = []
    for r in rows:
        h = r.get("house")
        houses.append({
            "name":   house_names.get(h, h),
            "emoji":  house_emojis.get(h, "🏰"),
            "points": r.get("points", 0),
        })
    return _cors(web.json_response({"houses": houses}))


async def handle_feed_pet(request):
    """Покормить питомца. Требует авторизации."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))

    user_id = int(tg_user["id"])
    from datetime import datetime, timezone
    from database import get_conn, fetchrow, execute

    try:
        with get_conn() as conn:
            pet = fetchrow(conn, "SELECT * FROM user_pets WHERE user_id=%s", user_id)
        if not pet:
            return _cors(web.json_response({"ok": False, "msg": "У тебя нет питомца"}))
        fed = pet.get("fed_at")
        if fed:
            if fed.tzinfo is None:
                fed = fed.replace(tzinfo=timezone.utc)
            hours = (datetime.now(timezone.utc) - fed).total_seconds() / 3600
            if hours < 6:
                left = int(6 - hours) + 1
                return _cors(web.json_response({"ok": False, "msg": f"Питомец не голоден. Покорми через ~{left} ч."}))
        new_h = min(100, (pet.get("happiness", 50) or 50) + 30)
        with get_conn() as conn:
            execute(conn, "UPDATE user_pets SET happiness=%s, fed_at=NOW() WHERE user_id=%s", new_h, user_id)
        # немного опыта питомцу
        try:
            from handlers.pets import add_pet_xp
            add_pet_xp(user_id, 15)
        except Exception:
            pass
        return _cors(web.json_response({"ok": True, "msg": f"Питомец накормлен! Счастье: {new_h}/100"}))
    except Exception as e:
        logger.warning("feed_pet: %s", e)
        return _cors(web.json_response({"ok": False, "msg": "Ошибка"}))


async def handle_claim_daily(request):
    """Забрать ежедневный бонус. Требует авторизации."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))

    user_id = int(tg_user["id"])
    from datetime import datetime, timezone, timedelta
    from database import get_conn, execute, add_gold, add_xp, add_item_to_inventory
    try:
        from handlers.daily_bonus import _get_login_streak, _get_login_reward, _ensure_tables
        _ensure_tables()
        today = datetime.now(timezone.utc).date()
        streak_row = _get_login_streak(user_id)
        last_login = streak_row.get("last_login")
        if last_login == today:
            return _cors(web.json_response({"ok": False, "msg": "Бонус за сегодня уже получен!"}))
        yesterday = today - timedelta(days=1)
        new_streak = (streak_row.get("streak", 0) + 1) if last_login == yesterday else 1
        reward = _get_login_reward(new_streak)
        if reward.get("gold"): add_gold(user_id, reward["gold"])
        if reward.get("xp"):   add_xp(user_id, reward["xp"])
        if reward.get("item"): add_item_to_inventory(user_id, reward["item"], 1)
        with get_conn() as conn:
            execute(conn, """
                INSERT INTO login_streaks (user_id, streak, last_login, total_logins)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (user_id) DO UPDATE
                SET streak=EXCLUDED.streak, last_login=EXCLUDED.last_login,
                    total_logins=login_streaks.total_logins+1
            """, user_id, new_streak, today)
        return _cors(web.json_response({"ok": True, "msg": f"🎁 Получено: {reward['label']} (серия: {new_streak})"}))
    except Exception as e:
        logger.warning("claim_daily: %s", e)
        return _cors(web.json_response({"ok": False, "msg": "Ошибка"}))


async def handle_equip_best(request):
    """Надеть лучшее снаряжение. Требует авторизации."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    try:
        from handlers.inventory import auto_equip_best
        changes = auto_equip_best(user_id)
        if not changes:
            return _cors(web.json_response({"ok": False, "msg": "Лучшее снаряжение уже надето (или его нет)"}))
        return _cors(web.json_response({"ok": True, "msg": f"⚡ Надето лучшее в {len(changes)} слот(ов)!"}))
    except Exception as e:
        logger.warning("equip_best: %s", e)
        return _cors(web.json_response({"ok": False, "msg": "Ошибка"}))


async def handle_battle(request):
    """PvE-бой: action = start|cast|state|flee."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "state")

    import webapp_battle as wb
    try:
        if action == "start":
            zone = body.get("zone") or None
            boss = bool(body.get("boss", False))
            return _cors(web.json_response(wb.start_battle(user_id, zone, boss)))
        elif action == "zones":
            return _cors(web.json_response(wb.list_zones(user_id)))
        elif action == "cast":
            spell_id = body.get("spell", "")
            return _cors(web.json_response(wb.cast(user_id, spell_id)))
        elif action == "flee":
            return _cors(web.json_response(wb.flee(user_id)))
        else:
            return _cors(web.json_response(wb.get_state(user_id)))
    except Exception as e:
        logger.warning("battle %s: %s", action, e)
        return _cors(web.json_response({"active": False, "error": "server"}))


async def handle_pet(request):
    """Инфо о питомце или тренировка. action = info|train|feed."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "info")

    from database import get_user, get_conn, fetchrow, execute
    try:
        from handlers.pets import (
            _get_pet, PETS, _get_stage, _pet_xp_needed, _bonus_desc,
            _add_pet_xp, PET_MAX_LEVEL, EVOLVE_LEVELS,
        )
    except Exception as e:
        logger.warning("pet import: %s", e)
        return _cors(web.json_response({"hasPet": False}))

    def _pet_payload(msg=None, ok=True):
        pet = _get_pet(user_id)
        if not pet:
            return {"hasPet": False, "ok": ok, "msg": msg}
        pid = pet.get("pet_id")
        pinfo = PETS.get(pid, {})
        level = pet.get("level", 1)
        stage = _get_stage(level)
        stages = pinfo.get("stages", [])
        if stage < len(stages):
            pemoji = stages[stage].get("emoji", pinfo.get("emoji", "🐾"))
            pname  = stages[stage].get("name", pinfo.get("name", "Питомец"))
        else:
            pemoji = pinfo.get("emoji", "🐾"); pname = pinfo.get("name", "Питомец")
        xp = pet.get("xp", 0)
        xp_need = _pet_xp_needed(level)
        try:
            bonus = _bonus_desc(pid, level)
        except Exception:
            bonus = ""
        # След. эволюция
        next_evo = None
        for lvl in EVOLVE_LEVELS:
            if level < lvl:
                next_evo = lvl; break
        return {
            "hasPet": True, "ok": ok, "msg": msg,
            "emoji": pemoji, "name": pname,
            "level": level, "maxLevel": PET_MAX_LEVEL,
            "xp": xp, "maxXp": xp_need,
            "happiness": pet.get("happiness", 100),
            "stage": stage + 1,
            "bonus": bonus,
            "nextEvo": next_evo,
        }

    try:
        if action == "train":
            pet = _get_pet(user_id)
            if not pet:
                return _cors(web.json_response({"hasPet": False, "ok": False, "msg": "Нет питомца"}))
            user = get_user(user_id)
            if user["gold"] < 50:
                return _cors(web.json_response(_pet_payload("❌ Нужно 50 золота", ok=False)))
            if pet.get("level", 1) >= PET_MAX_LEVEL:
                return _cors(web.json_response(_pet_payload("Питомец уже максимального уровня!", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE users SET gold=gold-50 WHERE user_id=%s", user_id)
            leveled, evolved, new_level = _add_pet_xp(user_id, 40)
            if evolved:   msg = f"🎉 Эволюция! Питомец достиг {new_level} уровня!"
            elif leveled: msg = f"⬆️ Питомец вырос до {new_level} уровня!"
            else:         msg = "✅ +40 опыта питомцу!"
            return _cors(web.json_response(_pet_payload(msg, ok=True)))
        else:
            return _cors(web.json_response(_pet_payload()))
    except Exception as e:
        logger.warning("pet action: %s", e)
        return _cors(web.json_response({"hasPet": False, "ok": False, "msg": "Ошибка"}))


async def handle_potions(request):
    """Зелья: action = list|brew|collect. Требует авторизации."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "list")

    from datetime import datetime, timezone
    try:
        from handlers.potion_system import (
            RECIPES, _can_brew, _spend_ingredients, _get_inventory_item_count,
            _unlock_starter_recipes,
        )
        from config import POTION_BREW_TIME_MINUTES
        from database import (
            get_user_recipes, get_brewing_queue, add_item_to_inventory,
            get_conn, execute, fetchall,
        )
        from game.items import ITEMS, item_display_name
    except Exception as e:
        logger.warning("potions import: %s", e)
        return _cors(web.json_response({"recipes": [], "queue": []}))

    try:
        _unlock_starter_recipes(user_id)
    except Exception:
        pass

    def _queue_payload():
        try:
            q = get_brewing_queue(user_id)
        except Exception:
            q = []
        now = datetime.now(timezone.utc)
        out = []
        for item in q:
            ra = item["ready_at"]
            if ra.tzinfo is None:
                ra = ra.replace(tzinfo=timezone.utc)
            rid = item["recipe_id"]
            rc = RECIPES.get(rid, {})
            remaining = int((ra - now).total_seconds())
            out.append({
                "recipe": rid,
                "name": rc.get("name", rid),
                "emoji": rc.get("emoji", "🧪"),
                "ready": remaining <= 0,
                "remaining": max(0, remaining),
            })
        return out

    def _recipes_payload():
        try:
            known = {r["recipe_id"] for r in get_user_recipes(user_id)}
        except Exception:
            known = set()
        out = []
        for rid, rc in RECIPES.items():
            if rid not in known and rc.get("unlock") != "start":
                continue
            ings = []
            can = True
            for iid, need in rc["ingredients"].items():
                have = _get_inventory_item_count(user_id, iid)
                if have < need: can = False
                idata = ITEMS.get(iid, {})
                ings.append({
                    "name": item_display_name(idata, "ru") if idata else iid,
                    "have": have, "need": need,
                })
            out.append({
                "id": rid, "name": rc.get("name", rid), "emoji": rc.get("emoji", "🧪"),
                "time": POTION_BREW_TIME_MINUTES.get(rc.get("rarity"), 5),
                "ingredients": ings, "canBrew": can,
            })
        return out

    try:
        if action == "brew":
            rid = body.get("recipe", "")
            rc = RECIPES.get(rid)
            if not rc:
                return _cors(web.json_response({"ok": False, "msg": "Рецепт не найден"}))
            ok, missing = _can_brew(user_id, rc)
            if not ok:
                return _cors(web.json_response({"ok": False, "msg": "Не хватает ингредиентов"}))
            from datetime import timedelta
            brew_time = POTION_BREW_TIME_MINUTES.get(rc.get("rarity"), 5)
            ready_at = datetime.now(timezone.utc) + timedelta(minutes=brew_time)
            _spend_ingredients(user_id, rc)
            with get_conn() as conn:
                execute(conn, "INSERT INTO brewing_queue (user_id, recipe_id, ready_at) VALUES (%s,%s,%s)",
                        user_id, rid, ready_at)
            return _cors(web.json_response({"ok": True, "msg": f"🔥 Варка началась! Готово через {brew_time} мин.",
                                            "recipes": _recipes_payload(), "queue": _queue_payload()}))
        elif action == "collect":
            now = datetime.now(timezone.utc)
            collected = []
            q = get_brewing_queue(user_id)
            for item in q:
                ra = item["ready_at"]
                if ra.tzinfo is None: ra = ra.replace(tzinfo=timezone.utc)
                if now >= ra:
                    rc = RECIPES.get(item["recipe_id"], {})
                    result_item = rc.get("result_item")
                    if result_item:
                        add_item_to_inventory(user_id, result_item, 1)
                        collected.append(rc.get("name", item["recipe_id"]))
                    with get_conn() as conn:
                        execute(conn, "DELETE FROM brewing_queue WHERE id=%s", item["id"])
            msg = ("✅ Собрано: " + ", ".join(collected)) if collected else "Пока нечего собирать"
            return _cors(web.json_response({"ok": bool(collected), "msg": msg,
                                            "recipes": _recipes_payload(), "queue": _queue_payload()}))
        else:
            return _cors(web.json_response({"recipes": _recipes_payload(), "queue": _queue_payload()}))
    except Exception as e:
        logger.warning("potions action: %s", e)
        return _cors(web.json_response({"recipes": [], "queue": [], "ok": False, "msg": "Ошибка"}))


async def handle_shop(request):
    """Магазин: action = list|buy. Требует авторизации."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "list")

    from database import get_user, get_conn, fetchrow, fetchall, execute, add_item_to_inventory
    from game.items import ITEMS, item_display_name
    try:
        from handlers.shop import _ensure_daily_shop
    except Exception as e:
        logger.warning("shop import: %s", e)
        return _cors(web.json_response({"items": [], "gold": 0}))

    rarity_emoji = {
        "common": "⚪", "uncommon": "🟢", "rare": "🔵", "very_rare": "🟣",
        "epic": "🟠", "legendary": "🔴", "mythical": "🌟", "abyssal": "⚫",
    }

    def _shop_payload(msg=None, ok=True):
        try:
            rows = _ensure_daily_shop()
        except Exception:
            rows = []
        user = get_user(user_id)
        out = []
        for r in rows:
            item = ITEMS.get(r["item_id"], {})
            if not item:
                continue
            out.append({
                "rowId": r["id"],
                "name": item_display_name(item, "ru"),
                "emoji": item.get("emoji", "📦"),
                "rarity": rarity_emoji.get(item.get("rarity", "common"), "⚪"),
                "price": r["price_gold"],
                "stock": r["stock"],
                "afford": user["gold"] >= r["price_gold"],
            })
        return {"items": out, "gold": user["gold"], "ok": ok, "msg": msg}

    try:
        if action == "buy":
            row_id = int(body.get("rowId", 0))
            with get_conn() as conn:
                row = fetchrow(conn, "SELECT * FROM shop_items WHERE id=%s AND available_until::date >= CURRENT_DATE", row_id)
            if not row:
                return _cors(web.json_response(_shop_payload("Товар недоступен", ok=False)))
            item = ITEMS.get(row["item_id"])
            user = get_user(user_id)
            if row["stock"] == 0:
                return _cors(web.json_response(_shop_payload("Распродано", ok=False)))
            if user["gold"] < row["price_gold"]:
                return _cors(web.json_response(_shop_payload("Не хватает золота", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE users SET gold=gold-%s WHERE user_id=%s", row["price_gold"], user_id)
                if row["stock"] > 0:
                    execute(conn, "UPDATE shop_items SET stock=stock-1 WHERE id=%s", row_id)
            add_item_to_inventory(user_id, row["item_id"], 1)
            nm = item_display_name(item, "ru") if item else row["item_id"]
            return _cors(web.json_response(_shop_payload(f"✅ Куплено: {nm}", ok=True)))
        else:
            return _cors(web.json_response(_shop_payload()))
    except Exception as e:
        logger.warning("shop action: %s", e)
        return _cors(web.json_response({"items": [], "gold": 0, "ok": False, "msg": "Ошибка"}))


async def handle_achievements(request):
    """Достижения с прогрессом. Требует авторизации."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])

    from database import get_user_stats, get_user_achievements
    try:
        from handlers.achievements import ACHIEVEMENTS
    except Exception as e:
        logger.warning("ach import: %s", e)
        return _cors(web.json_response({"achievements": []}))

    try:
        stats = get_user_stats(user_id) or {}
    except Exception:
        stats = {}
    try:
        earned = {r["achievement"]: r["tier"] for r in get_user_achievements(user_id)}
    except Exception:
        earned = {}

    out = []
    for aid, a in ACHIEVEMENTS.items():
        stat_key = a.get("stat")
        cur = stats.get(stat_key, 0) if stat_key else 0
        tiers = a.get("tiers", [])
        earned_tier = earned.get(aid, 0)
        # Следующая цель
        next_goal = None
        for t in tiers:
            if cur < t:
                next_goal = t; break
        max_goal = tiers[-1] if tiers else 1
        done = earned_tier >= len(tiers) or (next_goal is None)
        out.append({
            "name": a.get("name", aid),
            "emoji": a.get("emoji", "🏅"),
            "desc": a.get("desc", "").replace("{n}", str(next_goal or max_goal)),
            "cur": cur,
            "goal": next_goal or max_goal,
            "tier": earned_tier,
            "maxTier": len(tiers),
            "done": done,
        })
    # Сначала незавершённые с прогрессом
    out.sort(key=lambda x: (x["done"], -min(1.0, x["cur"]/x["goal"] if x["goal"] else 0)))
    return _cors(web.json_response({"achievements": out}))


async def handle_pvp(request):
    """Асинхронный PvP: action = opponents|fight."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "opponents")
    import webapp_pvp as wp
    try:
        if action == "fight":
            opp_id = int(body.get("opponent", 0))
            if not opp_id:
                return _cors(web.json_response({"ok": False, "error": "no_opponent"}))
            return _cors(web.json_response(wp.simulate(user_id, opp_id)))
        else:
            return _cors(web.json_response(wp.list_opponents(user_id)))
    except Exception as e:
        logger.warning("pvp %s: %s", action, e)
        return _cors(web.json_response({"ok": False, "error": "server"}))


async def handle_liveduel(request):
    """Живой PvP — Шаг 1: приглашения и матчинг.
    action = challenge_id | challenge_random | incoming | respond |
             check_status | toggle_block | block_status
    """
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "incoming")
    import webapp_duel as wd
    try:
        if action == "challenge_id":
            target = int(body.get("targetId", 0))
            return _cors(web.json_response(wd.challenge_by_id(user_id, target)))
        elif action == "challenge_random":
            return _cors(web.json_response(wd.challenge_random(user_id)))
        elif action == "incoming":
            return _cors(web.json_response(wd.get_incoming(user_id)))
        elif action == "respond":
            iid = body.get("inviteId", "")
            accept = bool(body.get("accept", False))
            return _cors(web.json_response(wd.respond_invite(user_id, iid, accept)))
        elif action == "check_status":
            iid = body.get("inviteId", "")
            return _cors(web.json_response(wd.check_invite_status(user_id, iid)))
        elif action == "battle_state":
            rid = body.get("roomId", "")
            return _cors(web.json_response(wd.get_battle_state(user_id, rid)))
        elif action == "battle_cast":
            rid = body.get("roomId", "")
            spell = body.get("spell", "")
            return _cors(web.json_response(wd.battle_cast(user_id, rid, spell)))
        elif action == "battle_flee":
            rid = body.get("roomId", "")
            return _cors(web.json_response(wd.battle_flee(user_id, rid)))
        elif action == "toggle_block":
            return _cors(web.json_response(wd.toggle_block(user_id)))
        elif action == "block_status":
            return _cors(web.json_response({"blocked": wd.get_block_status(user_id)}))
        else:
            return _cors(web.json_response({"error": "unknown_action"}))
    except Exception as e:
        logger.warning("liveduel %s: %s", action, e)
        return _cors(web.json_response({"ok": False, "error": "server"}))


async def handle_league(request):
    """Дуэльная лига: рейтинг игрока + топ дивизиона."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    from database import get_conn, fetchall, fetchrow
    try:
        from handlers.duel_league import _get_rating, _get_division, _next_division, DIVISIONS
    except Exception as e:
        logger.warning("league import: %s", e)
        return _cors(web.json_response({"me": None, "top": []}))
    try:
        r = _get_rating(user_id)
        elo = r.get("elo", 1000)
        div_name, div_id = _get_division(elo)
        nxt = _next_division(elo)
        # Моё место
        try:
            with get_conn() as conn:
                rank_row = fetchrow(conn, "SELECT COUNT(*)+1 AS rank FROM duel_league WHERE elo > %s", elo)
                top_rows = fetchall(conn, """
                    SELECT d.user_id, d.elo, d.wins, d.losses, u.wizard_name, u.house
                    FROM duel_league d JOIN users u ON u.user_id = d.user_id
                    ORDER BY d.elo DESC LIMIT 15
                """)
        except Exception:
            rank_row = {"rank": "—"}; top_rows = []
        house_emojis = {"gryffindor": "🦁", "slytherin": "🐍", "ravenclaw": "🦅", "hufflepuff": "🦡"}
        top = []
        for i, t in enumerate(top_rows, 1):
            dn, _ = _get_division(t["elo"])
            top.append({
                "place": i, "name": t["wizard_name"],
                "house": house_emojis.get(t["house"], "🏰"),
                "elo": t["elo"], "wins": t.get("wins", 0), "losses": t.get("losses", 0),
                "division": dn.split()[0] if dn else "",
                "isMe": t["user_id"] == user_id,
            })
        return _cors(web.json_response({
            "me": {
                "elo": elo, "division": div_name, "divisionId": div_id,
                "rank": rank_row.get("rank", "—"),
                "wins": r.get("wins", 0), "losses": r.get("losses", 0),
                "peak": r.get("peak_elo", elo),
                "nextDivision": nxt[0] if nxt else None,
                "nextElo": nxt[1] if nxt else None,
            },
            "top": top,
        }))
    except Exception as e:
        logger.warning("league: %s", e)
        return _cors(web.json_response({"me": None, "top": []}))


async def handle_quests(request):
    """Ежедневные квесты с прогрессом."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    from database import get_user_stats
    try:
        from handlers.quests import _get_today_daily_quests
    except Exception as e:
        logger.warning("quests import: %s", e)
        return _cors(web.json_response({"quests": []}))
    try:
        dailies = _get_today_daily_quests(user_id)
        stats = get_user_stats(user_id) or {}
        out = []
        for q in dailies:
            obj = q.get("objective", {})
            otype = obj.get("type", "")
            need = obj.get("count", 1)
            # Прогресс по типу цели (приблизительно, из статистики)
            cur = 0
            if otype == "kill_monster":
                cur = stats.get("pve_kills", 0) or 0
            elif otype == "win_duel":
                cur = stats.get("pvp_wins", 0) or 0
            elif otype == "brew_potion":
                cur = stats.get("potions_brewed", 0) or 0
            elif otype == "kill_boss":
                cur = stats.get("boss_kills", 0) or 0
            reward = q.get("reward", {})
            out.append({
                "name": q["name"].get("ru") if isinstance(q.get("name"), dict) else q.get("name", ""),
                "type": otype,
                "cur": min(cur, need), "need": need,
                "done": cur >= need,
                "rewardXp": reward.get("xp", 0), "rewardGold": reward.get("gold", 0),
            })
        return _cors(web.json_response({"quests": out}))
    except Exception as e:
        logger.warning("quests: %s", e)
        return _cors(web.json_response({"quests": []}))


# Сессии уроков в памяти API
_lesson_api_sessions: dict = {}

async def handle_lessons(request):
    """Уроки-викторина: action = subjects | question | answer."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "subjects")
    import random as _rnd
    try:
        from handlers.lessons import SUBJECTS_INFO, QUESTIONS
        from database import add_xp, add_gold
    except Exception as e:
        logger.warning("lessons import: %s", e)
        return _cors(web.json_response({"subjects": []}))
    try:
        if action == "subjects":
            subs = [{"id": sid, "name": info["name"], "teacher": info["teacher"], "emoji": info["emoji"]}
                    for sid, info in SUBJECTS_INFO.items() if QUESTIONS.get(sid)]
            return _cors(web.json_response({"subjects": subs}))
        elif action == "question":
            subject = body.get("subject", "")
            pool = QUESTIONS.get(subject, [])
            if not pool:
                return _cors(web.json_response({"error": "no_questions"}))
            idx = _rnd.randrange(len(pool))
            q = pool[idx]
            _lesson_api_sessions[user_id] = {"subject": subject, "idx": idx, "answer": q["answer"]}
            return _cors(web.json_response({
                "question": q["q"], "options": q["options"],
                "subject": SUBJECTS_INFO.get(subject, {}).get("name", ""),
            }))
        elif action == "answer":
            choice = int(body.get("choice", -1))
            sess = _lesson_api_sessions.get(user_id)
            if not sess:
                return _cors(web.json_response({"error": "no_session"}))
            correct = (choice == sess["answer"])
            pool = QUESTIONS.get(sess["subject"], [])
            q = pool[sess["idx"]] if sess["idx"] < len(pool) else {}
            xp = gold = 0
            if correct:
                xp, gold = _rnd.randint(15, 30), _rnd.randint(5, 15)
                try: add_xp(user_id, xp); add_gold(user_id, gold)
                except Exception: pass
            _lesson_api_sessions.pop(user_id, None)
            return _cors(web.json_response({
                "correct": correct, "correctIndex": sess["answer"],
                "hint": q.get("hint", ""), "xp": xp, "gold": gold,
            }))
        else:
            return _cors(web.json_response({"error": "unknown"}))
    except Exception as e:
        logger.warning("lessons %s: %s", action, e)
        return _cors(web.json_response({"error": "server"}))


async def handle_worldboss(request):
    """Мировой босс: action = status | attack."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "status")
    import random as _rnd
    from database import (get_active_world_boss, get_world_boss_top, get_user,
                          get_user_spells, get_daily_limit, increment_daily,
                          record_world_boss_damage)
    try:
        from handlers.world_bosses import WORLD_BOSSES, _boss_name
        from game.spells import SPELLS, spell_display_name
        from config import DAILY_LIMITS
    except Exception as e:
        logger.warning("wb import: %s", e)
        return _cors(web.json_response({"active": False}))

    def _wb_payload(extra=None):
        wb = get_active_world_boss()
        if not wb:
            return {"active": False}
        boss = WORLD_BOSSES.get(wb["boss_id"], {})
        top = get_world_boss_top(wb["id"])
        user = get_user(user_id)
        my_dmg = 0
        top_list = []
        for i, t in enumerate(top, 1):
            is_me = t.get("wizard_name") == (user["wizard_name"] if user else "")
            if is_me: my_dmg = t.get("damage", 0)
            top_list.append({"place": i, "name": t.get("wizard_name", "?"),
                             "damage": t.get("damage", 0), "isMe": is_me})
        used = get_daily_limit(user_id, "world_boss")
        limit = DAILY_LIMITS.get("world_boss", 1)
        payload = {
            "active": True,
            "id": wb["id"],
            "name": _boss_name(boss, "ru") if boss else wb["boss_id"],
            "emoji": boss.get("emoji", "🐲"),
            "hp": wb["current_hp"], "maxHp": wb["max_hp"],
            "weakness": boss.get("weakness", ""),
            "myDamage": my_dmg,
            "attacksLeft": max(0, limit - used),
            "top": top_list[:10],
        }
        if extra: payload.update(extra)
        return payload

    try:
        if action == "attack":
            wb = get_active_world_boss()
            if not wb or wb["current_hp"] <= 0:
                return _cors(web.json_response({"active": False, "msg": "Босс уже повержен!"}))
            used = get_daily_limit(user_id, "world_boss")
            if used >= DAILY_LIMITS.get("world_boss", 1):
                return _cors(web.json_response(_wb_payload({"msg": "⚔️ Ты уже атаковал сегодня!", "ok": False})))
            user = get_user(user_id)
            boss = WORLD_BOSSES.get(wb["boss_id"], {})
            spell_id = body.get("spell", "")
            spell = SPELLS.get(spell_id)
            if not spell:
                # берём первое доступное атакующее
                srows = [r["spell_id"] for r in (get_user_spells(user_id) or [])]
                spell_id = next((s for s in srows if SPELLS.get(s, {}).get("damage", 0) > 0), "expelliarmus")
                spell = SPELLS.get(spell_id, {"damage": 10})
            base = spell.get("damage", 10)
            atk_mult = 1 + (user["attack"] - 10) * 0.02
            crit = _rnd.random() < (0.05 + user.get("luck", 5) * 0.005)
            dmg = max(1, int(base * atk_mult * (1.5 if crit else 1.0)))
            record_world_boss_damage(wb["id"], user_id, dmg)
            increment_daily(user_id, "world_boss")
            wb2 = get_active_world_boss()
            killed = (not wb2) or wb2["current_hp"] <= 0
            return _cors(web.json_response(_wb_payload({
                "ok": True, "dmg": dmg, "crit": crit, "killed": killed,
                "spellName": spell_display_name(spell_id, "ru"),
                "msg": f"⚔️ {dmg} урона!" + (" 💥 КРИТ!" if crit else ""),
            })))
        else:
            return _cors(web.json_response(_wb_payload()))
    except Exception as e:
        logger.warning("worldboss %s: %s", action, e)
        return _cors(web.json_response({"active": False, "ok": False}))


async def handle_tournament(request):
    """Турнир: action = status | register."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "status")
    from database import get_user, get_conn, execute
    try:
        import handlers.tournament as T
        from config import TOURNAMENT_ENTRY_FEE
    except Exception as e:
        logger.warning("tour import: %s", e)
        return _cors(web.json_response({"open": False}))

    def _tour_payload(msg=None, ok=True):
        user = get_user(user_id)
        return {
            "open": T._registration_open,
            "registered": user_id in T._registrants,
            "count": len(T._registrants),
            "max": T.TOURNAMENT_MAX_PLAYERS,
            "fee": TOURNAMENT_ENTRY_FEE,
            "gold": user["gold"] if user else 0,
            "rewards": [
                {"place": "🥇 1 место", "xp": T.TOURNAMENT_REWARDS[1]["xp"], "gold": T.TOURNAMENT_REWARDS[1]["gold"]},
                {"place": "🥈 2 место", "xp": T.TOURNAMENT_REWARDS[2]["xp"], "gold": T.TOURNAMENT_REWARDS[2]["gold"]},
                {"place": "🥉 3 место", "xp": T.TOURNAMENT_REWARDS[3]["xp"], "gold": T.TOURNAMENT_REWARDS[3]["gold"]},
            ],
            "ok": ok, "msg": msg,
        }

    try:
        if action == "register":
            if not T._registration_open:
                return _cors(web.json_response(_tour_payload("❌ Регистрация закрыта", ok=False)))
            if user_id in T._registrants:
                return _cors(web.json_response(_tour_payload("✅ Ты уже зарегистрирован", ok=False)))
            user = get_user(user_id)
            if user["gold"] < TOURNAMENT_ENTRY_FEE:
                return _cors(web.json_response(_tour_payload(f"❌ Нужно {TOURNAMENT_ENTRY_FEE} 💰", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE users SET gold=gold-%s WHERE user_id=%s", TOURNAMENT_ENTRY_FEE, user_id)
            T._registrants.append(user_id)
            return _cors(web.json_response(_tour_payload("✅ Ты зарегистрирован на турнир!", ok=True)))
        else:
            return _cors(web.json_response(_tour_payload()))
    except Exception as e:
        logger.warning("tournament %s: %s", action, e)
        return _cors(web.json_response({"open": False, "ok": False}))


async def handle_teambattle(request):
    """Командный бой 3×3: action = status | find | fight."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "status")
    import random as _rnd
    from database import (get_user, get_squad, get_squad_members, get_conn,
                          fetchall, add_xp, add_gold)
    try:
        from config import TEAM_SIZE as _TS
    except Exception:
        _TS = 3
    house_emojis = {"gryffindor": "🦁", "slytherin": "🐍", "ravenclaw": "🦅", "hufflepuff": "🦡"}

    def _power(members):
        total = 0
        for m in members:
            u = get_user(m["user_id"])
            if u: total += u["attack"]*2 + u["defense"] + u["max_hp"]//5 + u["level"]*3
        return total
    def _synergy(members):
        houses = [get_user(m["user_id"]).get("house") for m in members if get_user(m["user_id"])]
        bonus = 0
        for h in set(houses):
            c = houses.count(h)
            if c >= 2: bonus += c*15
        return bonus
    def _roster(members):
        out = []
        for m in members[:_TS]:
            u = get_user(m["user_id"])
            if u: out.append({"name": u["wizard_name"], "house": house_emojis.get(u["house"],"🏰"),
                              "level": u["level"], "attack": u["attack"]})
        return out

    user = get_user(user_id)
    squad_id = user.get("squad_id") if user else None

    try:
        if not squad_id:
            return _cors(web.json_response({"hasSquad": False}))
        my_squad = get_squad(squad_id)
        my_members = get_squad_members(squad_id)
        if len(my_members) < _TS:
            return _cors(web.json_response({"hasSquad": True, "enoughMembers": False,
                "squadName": my_squad["name"] if my_squad else "",
                "memberCount": len(my_members), "needed": _TS}))

        if action == "find":
            with get_conn() as conn:
                squads = fetchall(conn, """
                    SELECT s.id, s.name, COUNT(u.user_id) as cnt
                    FROM squads s JOIN users u ON u.squad_id = s.id
                    WHERE s.id != %s GROUP BY s.id, s.name
                    HAVING COUNT(u.user_id) >= %s ORDER BY RANDOM() LIMIT 5
                """, squad_id, _TS)
            return _cors(web.json_response({"hasSquad": True, "enoughMembers": True,
                "enemies": [{"id": s["id"], "name": s["name"], "count": s["cnt"]} for s in squads]}))

        elif action == "fight":
            enemy_id = int(body.get("enemyId", 0))
            enemy_squad = get_squad(enemy_id)
            enemy_members = get_squad_members(enemy_id)
            if not enemy_squad or len(enemy_members) < _TS:
                return _cors(web.json_response({"hasSquad": True, "ok": False, "msg": "Отряд недоступен"}))
            pa, pb = _power(my_members), _power(enemy_members)
            sa, sb = _synergy(my_members), _synergy(enemy_members)
            ta = pa + sa + _rnd.randint(0,50)
            tb = pb + sb + _rnd.randint(0,50)
            iWon = ta >= tb
            # Поединки бойцов
            duels = []
            for i in range(_TS):
                a = get_user(my_members[i]["user_id"]) if i < len(my_members) else None
                b = get_user(enemy_members[i]["user_id"]) if i < len(enemy_members) else None
                if a and b:
                    ar = a["attack"] + a["level"]*2 + _rnd.randint(0,30)
                    br = b["attack"] + b["level"]*2 + _rnd.randint(0,30)
                    aWin = ar >= br
                    duels.append({
                        "a": a["wizard_name"], "aHouse": house_emojis.get(a["house"],"🏰"),
                        "b": b["wizard_name"], "bHouse": house_emojis.get(b["house"],"🏰"),
                        "aWon": aWin,
                    })
            # Награды только участнику (упрощённо — игроку)
            if iWon: add_xp(user_id, 150); add_gold(user_id, 80)
            else: add_xp(user_id, 40); add_gold(user_id, 15)
            return _cors(web.json_response({
                "hasSquad": True, "ok": True, "iWon": iWon,
                "myName": my_squad["name"], "enemyName": enemy_squad["name"],
                "myPower": pa, "mySynergy": sa, "enemyPower": pb, "enemySynergy": sb,
                "duels": duels,
                "reward": {"xp": 150 if iWon else 40, "gold": 80 if iWon else 15},
            }))
        else:
            return _cors(web.json_response({"hasSquad": True, "enoughMembers": True,
                "squadName": my_squad["name"], "power": _power(my_members),
                "synergy": _synergy(my_members), "roster": _roster(my_members)}))
    except Exception as e:
        logger.warning("teambattle %s: %s", action, e)
        return _cors(web.json_response({"hasSquad": False, "ok": False}))


async def handle_events(request):
    """События: статус активного события + лидерборд."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    from datetime import datetime, timezone
    try:
        from handlers.events import _get_active_event, _get_event_leaderboard
    except Exception as e:
        logger.warning("events import: %s", e)
        return _cors(web.json_response({"active": False}))
    try:
        ev = _get_active_event()
        if not ev:
            return _cors(web.json_response({"active": False}))
        house_emojis = {"gryffindor": "🦁", "slytherin": "🐍", "ravenclaw": "🦅", "hufflepuff": "🦡"}
        top = []
        try:
            lb = _get_event_leaderboard(ev["id"])
            for i, r in enumerate(lb, 1):
                top.append({"place": i, "name": r.get("wizard_name", "?"),
                            "house": house_emojis.get(r.get("house"), "🏰"),
                            "damage": int(r.get("total_dmg") or 0)})
        except Exception:
            top = []
        # Время до конца
        ends = ev.get("ends_at")
        time_left = ""
        if ends:
            if ends.tzinfo is None: ends = ends.replace(tzinfo=timezone.utc)
            secs = int((ends - datetime.now(timezone.utc)).total_seconds())
            if secs > 0:
                d, rem = divmod(secs, 86400); h, _ = divmod(rem, 3600)
                time_left = (f"{d}д {h}ч" if d else f"{h}ч")
        data = ev.get("data") or {}
        if isinstance(data, str):
            import json as _j
            try: data = _j.loads(data)
            except Exception: data = {}
        return _cors(web.json_response({
            "active": True,
            "type": ev.get("event_type", ""),
            "title": data.get("title_ru") or data.get("title") or "Событие",
            "desc": data.get("desc_ru") or data.get("desc") or "",
            "emoji": data.get("emoji", "🎉"),
            "timeLeft": time_left,
            "top": top,
            "hint": "Сражайся с событийным боссом в боте командой /events",
        }))
    except Exception as e:
        logger.warning("events: %s", e)
        return _cors(web.json_response({"active": False}))


async def handle_squad(request):
    """Отряды: action = status | create | browse | join | leave | disband."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "status")
    from database import (get_user, get_squad, get_squad_members, create_squad,
                          get_conn, execute, fetchall)
    try:
        from config import SQUAD_MAX_MEMBERS, SQUAD_CREATE_COST
    except Exception:
        SQUAD_MAX_MEMBERS, SQUAD_CREATE_COST = 5, 500
    house_emojis = {"gryffindor": "🦁", "slytherin": "🐍", "ravenclaw": "🦅", "hufflepuff": "🦡"}

    def _status_payload(msg=None, ok=True):
        user = get_user(user_id)
        sid = user.get("squad_id") if user else None
        if sid:
            sq = get_squad(sid)
            members = get_squad_members(sid)
            roster = []
            for m in members:
                mu = get_user(m["user_id"])
                roster.append({"name": m["wizard_name"], "level": m["level"],
                               "house": house_emojis.get(mu["house"],"🏰") if mu else "🏰",
                               "isLeader": sq and m["user_id"] == sq["leader_id"]})
            return {"inSquad": True, "ok": ok, "msg": msg,
                    "squadName": sq["name"] if sq else "",
                    "isLeader": sq and sq["leader_id"] == user_id,
                    "memberCount": len(members), "maxMembers": SQUAD_MAX_MEMBERS,
                    "roster": roster}
        return {"inSquad": False, "ok": ok, "msg": msg,
                "gold": user["gold"] if user else 0, "createCost": SQUAD_CREATE_COST,
                "maxMembers": SQUAD_MAX_MEMBERS}

    try:
        if action == "create":
            user = get_user(user_id)
            if user.get("squad_id"):
                return _cors(web.json_response(_status_payload("Ты уже в отряде", ok=False)))
            if user["gold"] < SQUAD_CREATE_COST:
                return _cors(web.json_response(_status_payload(f"Нужно {SQUAD_CREATE_COST} 💰", ok=False)))
            name = (body.get("name") or "").strip()[:24]
            if len(name) < 2:
                return _cors(web.json_response(_status_payload("Название 2-24 символа", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE users SET gold=gold-%s WHERE user_id=%s", SQUAD_CREATE_COST, user_id)
            create_squad(name, user_id)
            return _cors(web.json_response(_status_payload(f"✅ Отряд «{name}» создан!", ok=True)))
        elif action == "browse":
            with get_conn() as conn:
                squads = fetchall(conn, """
                    SELECT s.id, s.name, COUNT(u.user_id) as cnt
                    FROM squads s LEFT JOIN users u ON u.squad_id = s.id
                    GROUP BY s.id, s.name HAVING COUNT(u.user_id) < %s
                    ORDER BY cnt DESC LIMIT 20
                """, SQUAD_MAX_MEMBERS)
            return _cors(web.json_response({"squads": [
                {"id": s["id"], "name": s["name"], "count": s["cnt"], "max": SQUAD_MAX_MEMBERS}
                for s in squads]}))
        elif action == "join":
            user = get_user(user_id)
            if user.get("squad_id"):
                return _cors(web.json_response(_status_payload("Ты уже в отряде", ok=False)))
            sid = int(body.get("squadId", 0))
            sq = get_squad(sid)
            if not sq:
                return _cors(web.json_response(_status_payload("Отряд не найден", ok=False)))
            members = get_squad_members(sid)
            if len(members) >= SQUAD_MAX_MEMBERS:
                return _cors(web.json_response(_status_payload("Отряд заполнен", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE users SET squad_id=%s WHERE user_id=%s", sid, user_id)
            return _cors(web.json_response(_status_payload(f"✅ Ты вступил в «{sq['name']}»!", ok=True)))
        elif action == "leave":
            user = get_user(user_id)
            sid = user.get("squad_id")
            if not sid:
                return _cors(web.json_response(_status_payload("Ты не в отряде", ok=False)))
            sq = get_squad(sid)
            if sq and sq["leader_id"] == user_id:
                return _cors(web.json_response(_status_payload("Командир не может выйти — распусти отряд", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE users SET squad_id=NULL WHERE user_id=%s", user_id)
            return _cors(web.json_response(_status_payload("Ты покинул отряд", ok=True)))
        elif action == "disband":
            user = get_user(user_id)
            sid = user.get("squad_id")
            if not sid:
                return _cors(web.json_response(_status_payload("Ты не в отряде", ok=False)))
            sq = get_squad(sid)
            if not sq or sq["leader_id"] != user_id:
                return _cors(web.json_response(_status_payload("Только командир может распустить", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE users SET squad_id=NULL WHERE squad_id=%s", sid)
                execute(conn, "DELETE FROM squads WHERE id=%s", sid)
            return _cors(web.json_response(_status_payload("Отряд распущен", ok=True)))
        else:
            return _cors(web.json_response(_status_payload()))
    except Exception as e:
        logger.warning("squad %s: %s", action, e)
        return _cors(web.json_response({"inSquad": False, "ok": False, "msg": "Ошибка"}))


async def handle_trade(request):
    """Торговля золотом: action = send (перевод по ID)."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    from database import get_user, transfer_gold
    try:
        from config import TRADE_MIN_AMOUNT, TRADE_MAX_AMOUNT, TRADE_TAX_PERCENT
    except Exception:
        TRADE_MIN_AMOUNT, TRADE_MAX_AMOUNT, TRADE_TAX_PERCENT = 10, 100000, 5
    action = body.get("action", "info")
    try:
        if action == "send":
            target_id = int(body.get("targetId", 0))
            amount = int(body.get("amount", 0))
            if target_id == user_id:
                return _cors(web.json_response({"ok": False, "msg": "Нельзя перевести самому себе"}))
            target = get_user(target_id)
            if not target:
                return _cors(web.json_response({"ok": False, "msg": "Получатель с таким ID не найден"}))
            if amount < TRADE_MIN_AMOUNT or amount > TRADE_MAX_AMOUNT:
                return _cors(web.json_response({"ok": False, "msg": f"Сумма от {TRADE_MIN_AMOUNT} до {TRADE_MAX_AMOUNT}"}))
            tax = max(1, int(amount * TRADE_TAX_PERCENT / 100))
            sender = get_user(user_id)
            if sender["gold"] < amount + tax:
                return _cors(web.json_response({"ok": False, "msg": f"Не хватает золота (нужно {amount + tax} с учётом комиссии)"}))
            transfer_gold(user_id, target_id, amount, tax)
            return _cors(web.json_response({"ok": True,
                "msg": f"✅ Переведено {amount} 💰 игроку {target['wizard_name']} (комиссия {tax})",
                "gold": get_user(user_id)["gold"]}))
        else:
            user = get_user(user_id)
            return _cors(web.json_response({"gold": user["gold"] if user else 0,
                "minAmount": TRADE_MIN_AMOUNT, "maxAmount": TRADE_MAX_AMOUNT, "taxPercent": TRADE_TAX_PERCENT}))
    except Exception as e:
        logger.warning("trade %s: %s", action, e)
        return _cors(web.json_response({"ok": False, "msg": "Ошибка"}))


async def handle_wandcraft(request):
    """Крафт палочки: action = info | craft."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "info")
    import random as _rnd
    from database import get_user, get_conn, execute, fetchall
    try:
        from handlers.wandcraft import (CORES, CRAFT_COST_GOLD, WAND_WOODS,
                                        _get_inventory_ids, _available_ingredients, _ensure_table)
        from game.items import ITEMS
    except Exception as e:
        logger.warning("wandcraft import: %s", e)
        return _cors(web.json_response({"cores": []}))
    try:
        _ensure_table()
        inv = _get_inventory_ids(user_id)
        user = get_user(user_id)
        if action == "craft":
            core_id = body.get("core", "")
            core = CORES.get(core_id)
            if not core:
                return _cors(web.json_response({"ok": False, "msg": "Сердцевина не найдена"}))
            if inv.get(core_id, 0) < 1:
                return _cors(web.json_response({"ok": False, "msg": "У тебя нет этой сердцевины"}))
            if user["gold"] < CRAFT_COST_GOLD:
                return _cors(web.json_response({"ok": False, "msg": f"Нужно {CRAFT_COST_GOLD} 💰"}))
            ings = _available_ingredients(inv)
            if sum(q for _, _, q in ings) < 2:
                return _cors(web.json_response({"ok": False, "msg": "Нужно минимум 2 ингредиента"}))
            to_consume = []; need = 2
            for iid, item, qty in ings:
                take = min(qty, need); to_consume.append((iid, take)); need -= take
                if need <= 0: break
            bonus = core["attack"]
            for iid, take in to_consume:
                item = ITEMS.get(iid, {})
                rb = {"common":2,"uncommon":4,"rare":7,"very_rare":10,"epic":15}.get(item.get("rarity","common"),2)
                bonus += rb * take
            bonus += _rnd.randint(-3, 8); bonus = max(10, bonus)
            wood = _rnd.choice(WAND_WOODS)
            wand_name = f"{wood} палочка ({core['name']})"
            with get_conn() as conn:
                execute(conn, "UPDATE users SET gold=gold-%s WHERE user_id=%s", CRAFT_COST_GOLD, user_id)
                execute(conn, "UPDATE inventory SET quantity=quantity-1 WHERE user_id=%s AND item_id=%s", user_id, core_id)
                for iid, take in to_consume:
                    execute(conn, "UPDATE inventory SET quantity=quantity-%s WHERE user_id=%s AND item_id=%s", take, user_id, iid)
                execute(conn, "UPDATE users SET wand_wood=%s, wand_core=%s, attack=attack+%s WHERE user_id=%s",
                        wood, core["name"], bonus, user_id)
            return _cors(web.json_response({"ok": True,
                "msg": f"🪄 Создана {wand_name}! +{bonus} к атаке"}))
        else:
            cores = []
            for cid, c in CORES.items():
                cores.append({"id": cid, "name": c["name"], "attack": c["attack"],
                              "have": inv.get(cid, 0) > 0})
            ings = _available_ingredients(inv)
            ing_count = sum(q for _, _, q in ings)
            return _cors(web.json_response({
                "cores": cores, "cost": CRAFT_COST_GOLD, "gold": user["gold"] if user else 0,
                "ingredients": ing_count, "canCraft": ing_count >= 2,
            }))
    except Exception as e:
        logger.warning("wandcraft %s: %s", action, e)
        return _cors(web.json_response({"cores": [], "ok": False, "msg": "Ошибка"}))


async def handle_bank(request):
    """Гринготтс: action = info | deposit | withdraw | collect."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "info")
    from database import get_user, get_conn, execute
    try:
        from handlers.gringotts import (_get_account, _apply_interest, _calc_interest,
                                        INTEREST_RATE, INTEREST_CAP, _ensure_tables)
    except Exception as e:
        logger.warning("bank import: %s", e)
        return _cors(web.json_response({"balance": 0}))
    try:
        _ensure_tables()
        # Начисляем накопленные проценты при любом обращении
        acc = _get_account(user_id)
        _apply_interest(user_id, acc)
        acc = _get_account(user_id)
        user = get_user(user_id)
        balance = acc.get("balance", 0)

        def _info(msg=None, ok=True):
            nxt = min(int(balance * INTEREST_RATE), INTEREST_CAP)
            return {"balance": balance, "gold": user["gold"] if user else 0,
                    "rate": int(INTEREST_RATE*100), "cap": INTEREST_CAP,
                    "nextInterest": nxt if balance > 0 else 0, "ok": ok, "msg": msg}

        if action == "deposit":
            amount = int(body.get("amount", 0))
            if amount <= 0 or amount > user["gold"]:
                return _cors(web.json_response(_info("Некорректная сумма", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE users SET gold=gold-%s WHERE user_id=%s", amount, user_id)
                execute(conn, """INSERT INTO gringotts (user_id, balance, last_interest)
                                 VALUES (%s,%s,NOW()) ON CONFLICT (user_id)
                                 DO UPDATE SET balance=gringotts.balance+%s""", user_id, amount, amount)
            user = get_user(user_id); acc = _get_account(user_id); balance = acc.get("balance", 0)
            return _cors(web.json_response(_info(f"✅ Внесено {amount} 💰 в банк", ok=True)))
        elif action == "withdraw":
            amount = int(body.get("amount", 0))
            if amount <= 0 or amount > balance:
                return _cors(web.json_response(_info("Некорректная сумма", ok=False)))
            with get_conn() as conn:
                execute(conn, "UPDATE gringotts SET balance=balance-%s WHERE user_id=%s", amount, user_id)
                execute(conn, "UPDATE users SET gold=gold+%s WHERE user_id=%s", amount, user_id)
            user = get_user(user_id); acc = _get_account(user_id); balance = acc.get("balance", 0)
            return _cors(web.json_response(_info(f"✅ Снято {amount} 💰 из банка", ok=True)))
        else:
            return _cors(web.json_response(_info()))
    except Exception as e:
        logger.warning("bank %s: %s", action, e)
        return _cors(web.json_response({"balance": 0, "ok": False, "msg": "Ошибка"}))


async def handle_explore(request):
    """Исследование локаций: action = zones | event | choice."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "zones")
    import random as _rnd
    from database import get_user, add_xp, add_gold, add_item_to_inventory
    try:
        from handlers.locations import LOCATION_EVENTS, _get_progress, _increment_visits
        from game.monsters import ZONES
        from game.items import ITEMS, item_display_name
    except Exception as e:
        logger.warning("explore import: %s", e)
        return _cors(web.json_response({"zones": []}))
    try:
        user = get_user(user_id)
        lvl = user.get("level", 1) if user else 1
        if action == "event":
            zone_id = body.get("zone", "")
            events = LOCATION_EVENTS.get(zone_id)
            if not events:
                return _cors(web.json_response({"hasEvent": False, "msg": "В этой локации нет событий"}))
            _increment_visits(user_id, zone_id)
            ev = _rnd.choice(events)
            # индекс события для последующего выбора
            idx = events.index(ev)
            return _cors(web.json_response({
                "hasEvent": True, "zone": zone_id, "eventIdx": idx,
                "title": ev["title"], "desc": ev["desc"], "options": ev["options"],
            }))
        elif action == "choice":
            zone_id = body.get("zone", "")
            ev_idx = int(body.get("eventIdx", -1))
            choice = int(body.get("choice", -1))
            events = LOCATION_EVENTS.get(zone_id, [])
            if ev_idx < 0 or ev_idx >= len(events):
                return _cors(web.json_response({"ok": False, "msg": "Событие не найдено"}))
            ev = events[ev_idx]
            outcomes = ev.get("outcomes", [])
            if choice < 0 or choice >= len(outcomes):
                return _cors(web.json_response({"ok": False, "msg": "Неверный выбор"}))
            out = outcomes[choice]
            xp = out.get("xp", 0); gold = out.get("gold", 0); item = out.get("item")
            if xp: add_xp(user_id, xp)
            if gold: add_gold(user_id, gold)
            item_txt = ""
            if item:
                add_item_to_inventory(user_id, item, 1)
                idata = ITEMS.get(item, {})
                item_txt = (idata.get("emoji","📦") + " " + item_display_name(idata, "ru")) if idata else item
            return _cors(web.json_response({
                "ok": True, "msg": out.get("msg", ""),
                "xp": xp, "gold": gold, "item": item_txt,
            }))
        else:
            zones = []
            for zid, z in ZONES.items():
                if zid not in LOCATION_EVENTS:
                    continue
                visits = _get_progress(user_id, zid)
                zones.append({
                    "id": zid,
                    "name": z["name"].get("ru") if isinstance(z.get("name"), dict) else z.get("name", zid),
                    "emoji": z.get("emoji", "🗺️"),
                    "desc": z.get("desc_ru", ""),
                    "visits": visits,
                    "locked": lvl < z.get("min_level", 1),
                    "minLevel": z.get("min_level", 1),
                })
            return _cors(web.json_response({"zones": zones}))
    except Exception as e:
        logger.warning("explore %s: %s", action, e)
        return _cors(web.json_response({"zones": [], "ok": False, "msg": "Ошибка"}))


async def handle_collections(request):
    """Коллекции: action = list | claim."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "list")
    from database import get_user, get_conn, execute, add_gold, add_xp, add_item_to_inventory
    try:
        from handlers.collections import (COLLECTIONS, _get_owned_items, _is_claimed,
                                          _collection_progress, _ensure_table)
        from handlers.titles import grant_title
    except Exception as e:
        logger.warning("coll import: %s", e)
        return _cors(web.json_response({"collections": []}))
    try:
        _ensure_table()
        owned = _get_owned_items(user_id)
        if action == "claim":
            cid = body.get("collection", "")
            coll = COLLECTIONS.get(cid)
            if not coll:
                return _cors(web.json_response({"ok": False, "msg": "Коллекция не найдена"}))
            have, total = _collection_progress(owned, coll)
            if have < total:
                return _cors(web.json_response({"ok": False, "msg": f"Собрано {have}/{total} — ещё не готово"}))
            if _is_claimed(user_id, cid):
                return _cors(web.json_response({"ok": False, "msg": "Награда уже получена"}))
            reward = coll["reward"]
            if reward.get("xp"): add_xp(user_id, reward["xp"])
            if reward.get("gold"): add_gold(user_id, reward["gold"])
            if reward.get("item"): add_item_to_inventory(user_id, reward["item"], 1)
            if reward.get("title"):
                try: grant_title(user_id, reward["title"])
                except Exception: pass
            with get_conn() as conn:
                execute(conn, """INSERT INTO collection_claims (user_id, collection_id)
                                 VALUES (%s,%s) ON CONFLICT DO NOTHING""", user_id, cid)
            return _cors(web.json_response({"ok": True,
                "msg": f"🎉 Награда получена: +{reward.get('xp',0)} XP, +{reward.get('gold',0)} 💰"}))
        else:
            out = []
            for cid, coll in COLLECTIONS.items():
                have, total = _collection_progress(owned, coll)
                out.append({
                    "id": cid, "title": coll["title"], "desc": coll.get("desc", ""),
                    "have": have, "total": total, "done": have >= total,
                    "claimed": _is_claimed(user_id, cid),
                    "rewardXp": coll["reward"].get("xp", 0),
                    "rewardGold": coll["reward"].get("gold", 0),
                    "rewardTitle": coll["reward"].get("title"),
                })
            return _cors(web.json_response({"collections": out}))
    except Exception as e:
        logger.warning("collections %s: %s", action, e)
        return _cors(web.json_response({"collections": [], "ok": False, "msg": "Ошибка"}))


async def handle_titles(request):
    """Титулы: action = list | set."""
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad request"}, status=400))
    tg_user = _verify_init_data(body.get("initData", ""))
    if not tg_user or not tg_user.get("id"):
        return _cors(web.json_response({"error": "unauthorized"}, status=401))
    user_id = int(tg_user["id"])
    action = body.get("action", "list")
    from database import get_user, get_conn, execute, fetchall
    try:
        from handlers.titles import ALL_TITLES, RARITY_EMOJI
    except Exception as e:
        logger.warning("titles import: %s", e)
        return _cors(web.json_response({"titles": []}))
    try:
        user = get_user(user_id)
        with get_conn() as conn:
            rows = fetchall(conn, "SELECT title_id FROM user_titles WHERE user_id=%s ORDER BY earned_at DESC", user_id)
        owned = [r["title_id"] for r in rows]
        active = user.get("title", "") if user else ""
        if action == "set":
            tid = body.get("titleId", "")
            if tid not in owned and tid != "":
                return _cors(web.json_response({"ok": False, "msg": "Титул не получен"}))
            info = ALL_TITLES.get(tid, {})
            title_text = (info.get("emoji","") + " " + info.get("name", tid)).strip() if tid else ""
            with get_conn() as conn:
                execute(conn, "UPDATE users SET title=%s WHERE user_id=%s", title_text, user_id)
            return _cors(web.json_response({"ok": True, "msg": "✅ Титул установлен!", "active": title_text}))
        else:
            out = []
            for tid in owned:
                info = ALL_TITLES.get(tid, {})
                title_text = (info.get("emoji","") + " " + info.get("name", tid)).strip()
                out.append({
                    "id": tid, "name": info.get("name", tid), "emoji": info.get("emoji", "🎭"),
                    "rarity": RARITY_EMOJI.get(info.get("rarity","common"), ""),
                    "active": title_text == active,
                })
            return _cors(web.json_response({"titles": out, "active": active}))
    except Exception as e:
        logger.warning("titles %s: %s", action, e)
        return _cors(web.json_response({"titles": [], "ok": False, "msg": "Ошибка"}))


def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/profile", handle_profile)
    app.router.add_options("/api/profile", handle_options)
    app.router.add_get("/api/leaderboard", handle_leaderboard)
    app.router.add_options("/api/leaderboard", handle_options)
    app.router.add_post("/api/inventory", handle_inventory)
    app.router.add_options("/api/inventory", handle_options)
    app.router.add_get("/api/housecup", handle_housecup)
    app.router.add_options("/api/housecup", handle_options)
    app.router.add_post("/api/feedpet", handle_feed_pet)
    app.router.add_options("/api/feedpet", handle_options)
    app.router.add_post("/api/claimdaily", handle_claim_daily)
    app.router.add_options("/api/claimdaily", handle_options)
    app.router.add_post("/api/equipbest", handle_equip_best)
    app.router.add_options("/api/equipbest", handle_options)
    app.router.add_post("/api/battle", handle_battle)
    app.router.add_options("/api/battle", handle_options)
    app.router.add_post("/api/pet", handle_pet)
    app.router.add_options("/api/pet", handle_options)
    app.router.add_post("/api/potions", handle_potions)
    app.router.add_options("/api/potions", handle_options)
    app.router.add_post("/api/shop", handle_shop)
    app.router.add_options("/api/shop", handle_options)
    app.router.add_post("/api/achievements", handle_achievements)
    app.router.add_options("/api/achievements", handle_options)
    app.router.add_post("/api/pvp", handle_pvp)
    app.router.add_options("/api/pvp", handle_options)
    app.router.add_post("/api/liveduel", handle_liveduel)
    app.router.add_options("/api/liveduel", handle_options)
    app.router.add_post("/api/league", handle_league)
    app.router.add_options("/api/league", handle_options)
    app.router.add_post("/api/quests", handle_quests)
    app.router.add_options("/api/quests", handle_options)
    app.router.add_post("/api/lessons", handle_lessons)
    app.router.add_options("/api/lessons", handle_options)
    app.router.add_post("/api/worldboss", handle_worldboss)
    app.router.add_options("/api/worldboss", handle_options)
    app.router.add_post("/api/tournament", handle_tournament)
    app.router.add_options("/api/tournament", handle_options)
    app.router.add_post("/api/teambattle", handle_teambattle)
    app.router.add_options("/api/teambattle", handle_options)
    app.router.add_post("/api/events", handle_events)
    app.router.add_options("/api/events", handle_options)
    app.router.add_post("/api/squad", handle_squad)
    app.router.add_options("/api/squad", handle_options)
    app.router.add_post("/api/trade", handle_trade)
    app.router.add_options("/api/trade", handle_options)
    app.router.add_post("/api/wandcraft", handle_wandcraft)
    app.router.add_options("/api/wandcraft", handle_options)
    app.router.add_post("/api/bank", handle_bank)
    app.router.add_options("/api/bank", handle_options)
    app.router.add_post("/api/explore", handle_explore)
    app.router.add_options("/api/explore", handle_options)
    app.router.add_post("/api/collections", handle_collections)
    app.router.add_options("/api/collections", handle_options)
    app.router.add_post("/api/titles", handle_titles)
    app.router.add_options("/api/titles", handle_options)
    return app


def run_api_server():
    """Запускает API-сервер в отдельном потоке (со своим event loop)."""
    import asyncio
    port = int(os.environ.get("PORT", "8080"))

    def _serve():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(_build_app())
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", port)
        loop.run_until_complete(site.start())
        logger.info("Mini App API запущен на порту %s", port)
        loop.run_forever()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
