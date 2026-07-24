import uuid
import time
import config
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from dotenv import load_dotenv
import os
import firebase_admin
from firebase_admin import credentials, firestore
from discord.ext import commands
from discord.ui import View, Button
import asyncio


import logging
logging.basicConfig(level=logging.INFO)


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Firebase connection
cred = credentials.Certificate("currency-bot-19258-firebase-adminsdk-fbsvc-74ead12719.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Default user profile values
STARTING_BALANCE = 3050
STARTING_BODY_COUNT = 0
STARTING_STATUS = "virgin"
STARTING_HOUSE = None

SESSION_CHECK_INTERVAL = None  # 30 minutes in seconds

DECAY_RATE = 0.2
SESSION_PRICE_FLOOR = 10

pending_checks = set()  # session_ids currently waiting for a response

# Active sessions {user_id: session_id}
active_sessions = {}
# Session data {session_id: set of user_ids}
sessions = {}

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(name="/chelp for commands"))

    bot.add_view(JobRoleSelectView())
    for floor_name in config.FLOOR_JOBS:
        bot.add_view(JobButtonView(floor_name))
    print("Registered persistent job views")
    bot.add_view(PerkBuyView(["virginity_reset"]))
    bot.add_view(PerkBuyView(["allure_tier1", "allure_tier2", "allure_tier3"]))
    print("Registered persistent perk views")

    bot.add_dynamic_items(DailyClaimButton)
    print("Registered dynamic daily claim button")
    
    reschedule_count = 0
    for doc in db.collection("users").stream():
        data = doc.to_dict()
        job_cooldowns = data.get("job_cooldowns", {})
        if not job_cooldowns:
            continue

        user_id = int(doc.id)
        for cooldown_key, last_claim in job_cooldowns.items():
            parts = cooldown_key.rsplit("_", 1)
            if len(parts) != 2:
                continue
            job_name, floor_name = parts
            if job_name not in config.JOB_PAY_INFO:
                continue

            _, _, cooldown_seconds = config.JOB_PAY_INFO[job_name]
            ready_at = last_claim + cooldown_seconds

            if ready_at > time.time():
                bot.loop.create_task(schedule_job_ready_dm(user_id, job_name, floor_name, ready_at))
                reschedule_count += 1

    print(f"Rescheduled {reschedule_count} pending job-ready DM(s)")

    try:
        guild1 = discord.Object(id=1367183962447024158) # velvet
        guild2 = discord.Object(id=1312819979384782908) # yellow
        bot.tree.copy_global_to(guild=guild1)
        bot.tree.copy_global_to(guild=guild2)
        synced1 = await bot.tree.sync(guild=guild1)
        synced2 = await bot.tree.sync(guild=guild2)
        print(f"Synced {len(synced1)} commands to server 1")
        print(f"Synced {len(synced2)} commands to server 2")
    except Exception as e:
        print(e)
    print(f"Logged in as {bot.user}")
    bot.loop.create_task(session_check_loop())
    bot.loop.create_task(leaderboard_loop())
    bot.loop.create_task(daily_eligibility_loop())
    await announce_status("✅ Currency Bot is now **online**!")

@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id != config.ALLOWED_GUILD_ID:
        return
    if payload.member is None or payload.member.bot:
        return

    emoji = str(payload.emoji)
    role_data = get_reaction_role(payload.message_id, emoji)
    if not role_data:
        return

    guild = bot.get_guild(payload.guild_id)
    role = guild.get_role(role_data["role_id"])
    if role:
        await payload.member.add_roles(role)

@bot.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id != config.ALLOWED_GUILD_ID:
        return

    emoji = str(payload.emoji)
    role_data = get_reaction_role(payload.message_id, emoji)
    if not role_data:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    role = guild.get_role(role_data["role_id"])
    if member and role:
        await member.remove_roles(role)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild is None:
        return
    if message.guild.id != config.ALLOWED_GUILD_ID:
        return
    if message.channel.id in config.EXCLUDED_ACTIVITY_CHANNELS:
        return

    get_or_create_user(message.author)
    increment_activity_count(message.guild.id, message.author.id)
    mark_message_sent_today(message.author.id)

    await bot.process_commands(message)

def get_or_create_user(member):
    user_id = member.id
    ref = db.collection("users").document(str(user_id))
    doc = ref.get()

    default_fields = {
        "username": member.name,
        "balance": STARTING_BALANCE,
        "body_count": STARTING_BODY_COUNT,
        "status": STARTING_STATUS,
        "house": STARTING_HOUSE,
        "partners": [],
        "strikes": 0,
        "allure_boost_multiplier": None,
        "allure_boost_sessions_left": 0,
        "current_job_role": None,
        "job_quit_lockout_until": None,
        "job_cooldowns": {}
    }

    if not doc.exists:
        ref.set(default_fields)
        return ref.get().to_dict()

    data = doc.to_dict()
    missing = {k: v for k, v in default_fields.items() if k not in data}

    update_payload = {"username": member.name}
    if missing:
        update_payload.update(missing)

    ref.update(update_payload)

    if missing:
        print(f"[get_or_create_user] Repaired profile for {member.name} ({user_id}): added missing fields {list(missing.keys())}")

    return ref.get().to_dict()

def calculate_session_price(body_count: int, multiplier: float = None) -> int:
    base_price = max(SESSION_PRICE_FLOOR, round(1500 / (1 + body_count * DECAY_RATE)))
    if multiplier:
        base_price = round(base_price * (1 + multiplier))
    return base_price

def get_setting(key, default):
    ref = db.collection("settings").document(key)
    doc = ref.get()
    if not doc.exists:
        ref.set({"value": default})
        return default
    return doc.to_dict()["value"]

def set_setting(key, value):
    db.collection("settings").document(key).set({"value": value})
    
def get_current_week_start():
    now = datetime.now(ZoneInfo("Europe/Bucharest"))
    days_since_friday = (now.weekday() - 4) % 7  # Monday=0 ... Friday=4, Sunday=6
    week_start = now - timedelta(days=days_since_friday)
    week_start = week_start.replace(hour=14, minute=0, second=0, microsecond=0)

    if week_start > now:
        week_start -= timedelta(days=7)

    return week_start

def get_leaderboard_top3(collection_name, guild_id, week_start, order_field):
    from google.cloud.firestore_v1.base_query import FieldFilter
    query = (
        db.collection(collection_name)
        .where(filter=FieldFilter("guild_id", "==", guild_id))
        .where(filter=FieldFilter("week_start", "==", week_start.isoformat()))
        .order_by(order_field, direction=firestore.Query.DESCENDING)
        .limit(10)
    )
    all_results = list(query.stream())

    filtered = [doc for doc in all_results if doc.to_dict()["user_id"] not in config.EXCLUDED_DAILY_USERS]
    return filtered[:3]

def increment_activity_count(guild_id: int, user_id: int):
    if user_id in config.EXCLUDED_DAILY_USERS:
        return
    current_week = get_current_week_start()
    doc_id = f"{guild_id}_{user_id}"
    ref = db.collection("activity").document(doc_id)
    doc = ref.get()

    if doc.exists:
        data = doc.to_dict()
        if data.get("week_start") == current_week.isoformat():
            count = data.get("count", 0) + 1
        else:
            count = 1  # new week, start over
    else:
        count = 1

    ref.set({
        "guild_id": guild_id,
        "user_id": user_id,
        "count": count,
        "week_start": current_week.isoformat()
    })
    
def increment_session_count(guild_id: int, user_id: int):
    if user_id in config.EXCLUDED_DAILY_USERS:
        return
    current_week = get_current_week_start()
    doc_id = f"{guild_id}_{user_id}"
    ref = db.collection("session_activity").document(doc_id)
    doc = ref.get()

    if doc.exists:
        data = doc.to_dict()
        if data.get("week_start") == current_week.isoformat():
            count = data.get("count", 0) + 1
        else:
            count = 1  # new week, start over
    else:
        count = 1

    ref.set({
        "guild_id": guild_id,
        "user_id": user_id,
        "count": count,
        "week_start": current_week.isoformat()
    })
    
def increment_coins_received(guild_id: int, user_id: int, amount: int):
    if user_id in config.EXCLUDED_DAILY_USERS:
        return
    current_week = get_current_week_start()
    doc_id = f"{guild_id}_{user_id}"
    ref = db.collection("coins_received").document(doc_id)
    doc = ref.get()

    if doc.exists:
        data = doc.to_dict()
        if data.get("week_start") == current_week.isoformat():
            total = data.get("total", 0) + amount
        else:
            total = amount  # new week, start over
    else:
        total = amount

    ref.set({
        "guild_id": guild_id,
        "user_id": user_id,
        "total": total,
        "week_start": current_week.isoformat()
    })
    
async def pay_leaderboard_winners(guild_id, results, rewards):
    print(f"[PAYOUT DEBUG] Called with guild_id={guild_id}, {len(results)} result(s), rewards={rewards}")
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    rank = 0
    for doc in results:
        if rank >= len(rewards):
            break
        data = doc.to_dict()
        user_id = data["user_id"]
        if user_id in config.EXCLUDED_DAILY_USERS:
            continue
        reward = rewards[rank]
        i = rank
        rank += 1

        ref = db.collection("users").document(str(user_id))
        user_doc = ref.get()
        print(f"[PAYOUT DEBUG] rank {i}: user_id={user_id}, exists={user_doc.exists}")
        if not user_doc.exists:
            continue
        user_data = user_doc.to_dict()
        current_balance = user_data.get("balance", STARTING_BALANCE)

        ref.set({"balance": current_balance + reward}, merge=True)
        lines.append(f"{medals[i]} <@{user_id}> received **{reward} 🪙**!")

    print(f"[PAYOUT DEBUG] lines built: {lines}")

    if not lines:
        print("[PAYOUT DEBUG] No lines, returning early")
        return

    log_channel_id = get_setting(f"log_channel_id_{guild_id}", None)
    print(f"[PAYOUT DEBUG] log_channel_id={log_channel_id}")
    if not log_channel_id:
        print("[PAYOUT DEBUG] No log_channel_id, returning early")
        return
    log_channel = bot.get_channel(log_channel_id)
    print(f"[PAYOUT DEBUG] log_channel object={log_channel}")
    if not log_channel:
        print("[PAYOUT DEBUG] log_channel not found, returning early")
        return

    await log_channel.send("**💰 Weekly Winner Rewards**\n\n" + "\n".join(lines))
    print("[PAYOUT DEBUG] Sent successfully")
    
async def assign_weekly_vip(guild, activity_results, session_results, received_results):
    vip_role_id = get_setting(f"vip_role_id_{guild.id}", None)
    if not vip_role_id:
        return
    vip_role = guild.get_role(vip_role_id)
    if not vip_role:
        return

    # Strip VIP from everyone who currently has it
    for member in vip_role.members:
        try:
            await member.remove_roles(vip_role, reason="Weekly VIP reset")
        except Exception as e:
            print(f"[VIP ERROR] Failed to remove role from {member.id}: {e}")

    assigned_user_ids = set()
    winners = []

    for board_name, results in [
        ("Activity", activity_results),
        ("Sessions", session_results),
        ("Coins Received", received_results)
    ]:
        for doc in results:
            data = doc.to_dict()
            user_id = data["user_id"]
            if user_id in config.EXCLUDED_DAILY_USERS:
                continue
            if user_id in assigned_user_ids:
                continue  # already claimed VIP from a higher-priority board this week

            member = guild.get_member(user_id)
            if not member:
                continue

            try:
                await member.add_roles(vip_role, reason=f"Weekly VIP winner ({board_name} leaderboard)")
                assigned_user_ids.add(user_id)
                winners.append((member, board_name))
            except Exception as e:
                print(f"[VIP ERROR] Failed to add role to {user_id}: {e}")
            break  # move to next leaderboard once someone from this one is assigned

    if winners:
        leaderboard_channel_id = get_setting(f"leaderboard_channel_id_{guild.id}", None)
        if leaderboard_channel_id:
            leaderboard_channel = bot.get_channel(leaderboard_channel_id)
            if leaderboard_channel:
                lines = [f"👑 {member.mention} — VIP for topping the **{board_name}** leaderboard!" for member, board_name in winners]
                await leaderboard_channel.send("**👑 Weekly VIP Winners**\n\n" + "\n".join(lines))
    
def mark_message_sent_today(user_id: int):
    today = datetime.now(ZoneInfo("Europe/Bucharest")).date().isoformat()
    db.collection("users").document(str(user_id)).set({"last_message_date": today}, merge=True)

def add_reaction_role(message_id, emoji, role_id):
    db.collection("reaction_roles").document(f"{message_id}_{emoji}").set({
        "message_id": str(message_id),
        "emoji": emoji,
        "role_id": role_id
    })

def get_reaction_role(message_id, emoji):
    doc = db.collection("reaction_roles").document(f"{message_id}_{emoji}").get()
    if doc.exists:
        return doc.to_dict()
    return None
    
async def log_transaction(message: str, guild_id: int):
    channel_id = get_setting(f"log_channel_id_{guild_id}", None)
    if not channel_id:
        return
    try:
        channel = await bot.fetch_channel(channel_id)
        await channel.send(message)
    except Exception as e:
        print(f"Log transaction error: {e}")

async def log_strike(message: str, guild_id: int):
    channel_id = get_setting(f"strike_log_channel_id_{guild_id}", None)
    if not channel_id:
        return
    try:
        channel = await bot.fetch_channel(channel_id)
        await channel.send(message)
    except Exception as e:
        print(f"Log strike error: {e}")

async def announce_status(message: str):
    for guild in bot.guilds:
        if guild.id != config.ALLOWED_GUILD_ID:
            continue
        channel_id = get_setting(f"announce_channel_id_{guild.id}", None)
        if not channel_id:
            continue
        try:
            channel = await bot.fetch_channel(channel_id)
            await channel.send(message)
        except Exception as e:
            print(f"Announce error: {e}")

async def end_session_for_user(user_id: int):
    if user_id not in active_sessions:
        return

    session_id = active_sessions[user_id]
    session_members = sessions[session_id]["members"].copy()
    guild_id = sessions[session_id].get("guild_id")

    for member_id in session_members:
        ref = db.collection("users").document(str(member_id))
        user_data = ref.get().to_dict()
        partners = user_data.get("partners", [])
        
        new_partners = [str(m) for m in session_members if m != member_id and str(m) not in partners]
        new_body_count = user_data["body_count"] + len(new_partners)
        
        updated_partners = partners + new_partners
        
        update_data = {"partners": updated_partners}
        if new_body_count > user_data["body_count"]:
            update_data["body_count"] = new_body_count
        if user_data["status"] == "virgin" and new_body_count > 0:
            update_data["status"] = "non-virgin"
            
        ref.update(update_data)
        active_sessions.pop(member_id, None)

        if guild_id:
            increment_session_count(guild_id, member_id)

    sessions.pop(session_id, None)

async def end_session_for_afk_user(user_id: int):
    if user_id not in active_sessions:
        return

    session_id = active_sessions[user_id]
    session_data = sessions.get(session_id)
    if not session_data:
        active_sessions.pop(user_id, None)
        return

    session_members = session_data["members"]
    guild_id = session_data.get("guild_id")

    # Update stats for the AFK user leaving, same as a normal /cend
    ref = db.collection("users").document(str(user_id))
    user_data = ref.get().to_dict()
    partners = user_data.get("partners", [])

    new_partners = [str(m) for m in session_members if m != user_id and str(m) not in partners]
    new_body_count = user_data["body_count"] + len(new_partners)
    updated_partners = partners + new_partners

    update_data = {"partners": updated_partners}
    if new_body_count > user_data["body_count"]:
        update_data["body_count"] = new_body_count
    if user_data["status"] == "virgin" and new_body_count > 0:
        update_data["status"] = "non-virgin"

    ref.update(update_data)

    if guild_id:
        increment_session_count(guild_id, user_id)

    # Remove just this one user from the session
    session_members.discard(user_id)
    active_sessions.pop(user_id, None)
    session_data.get("awaiting_confirmation", set()).discard(user_id)

    if len(session_members) < 2:
        # Not enough people left to continue, end it for whoever remains
        for member_id in session_members.copy():
            active_sessions.pop(member_id, None)
        sessions.pop(session_id, None)
        pending_checks.discard(session_id)
    elif not session_data.get("awaiting_confirmation"):
        # Session survives, and everyone else already confirmed — round is over
        pending_checks.discard(session_id)
        session_data["last_active"] = time.time()

class SessionCheckView(View):
    def __init__(self, user_id: int, session_id: str):
        super().__init__(timeout=config.CONFIRMATION_TIMEOUT)
        self.user_id = user_id
        self.session_id = session_id
        self.message = None

    @discord.ui.button(label="Yes, still active!", style=discord.ButtonStyle.green)
    async def still_active(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()

        session_data = sessions.get(self.session_id)
        if session_data:
            session_data.get("awaiting_confirmation", set()).discard(self.user_id)

            afk_checks_passed = session_data.setdefault("afk_checks_passed", {})
            afk_checks_passed[self.user_id] = afk_checks_passed.get(self.user_id, 0) + 1

            if not session_data.get("awaiting_confirmation"):
                # everyone has confirmed, restart the timer
                pending_checks.discard(self.session_id)
                session_data["last_active"] = time.time()

        await interaction.response.edit_message(content=f"✅ {interaction.user.name} confirmed they're still active!", view=None)
    @discord.ui.button(label="No, end session", style=discord.ButtonStyle.red)
    async def end_session(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()
        pending_checks.discard(self.session_id)
        await end_session_for_user(self.user_id)
        await interaction.response.edit_message(content=f"⚠️ Session ended for {interaction.user.name}!", view=None)

    async def on_timeout(self):
        session_data = sessions.get(self.session_id)
        channel_id = session_data["channel_id"] if session_data else None
        other_members = (session_data["members"] - {self.user_id}) if session_data else set()

        await end_session_for_afk_user(self.user_id)

        for child in self.children:
            child.disabled = True
        if self.message:
            await self.message.edit(content="⏰ You didn't respond in time — you've been removed from the session.", view=self)

        session_ended = self.session_id not in sessions
        if session_ended and channel_id and other_members:
            channel = bot.get_channel(channel_id)
            if channel:
                mentions = ", ".join(f"<@{m}>" for m in other_members)
                await channel.send(f"⛔ Session ended — <@{self.user_id}> didn't respond to the AFK check in time. {mentions}, your session has ended too.")


async def session_check_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        interval = config.SESSION_CHECK_INTERVAL
        await asyncio.sleep(interval)
        now = time.time()

        for session_id, session_data in list(sessions.items()):
            if session_id in pending_checks:
                continue

            last_active = session_data.get("last_active", now)
            if now - last_active < interval:
                continue  # not due yet

            pending_checks.add(session_id)
            members = session_data["members"].copy()
            session_data["awaiting_confirmation"] = set(members)
            channel = bot.get_channel(session_data["channel_id"])
            if not channel:
                continue

            print(f"Session {session_id} due for check (inactive {int(now - last_active)}s)")

            for member_id in members:
                if session_id not in sessions:
                    break  # session ended mid-round, stop sending stale checks

                member = channel.guild.get_member(member_id)
                if not member:
                    continue

                view = SessionCheckView(member_id, session_id)
                sent_message = await channel.send(
                    f"⏰ {member.mention} are you still in your session?",
                    view=view
                )
                view.message = sent_message

async def post_weekly_leaderboard(guild, week_start):
    channel_id = get_setting(f"leaderboard_channel_id_{guild.id}", None)
    print(f"[LEADERBOARD DEBUG] guild={guild.id}, channel_id={channel_id}")
    if not channel_id:
        print("[LEADERBOARD DEBUG] No channel_id found, returning early")
        return []
    channel = bot.get_channel(channel_id)
    print(f"[LEADERBOARD DEBUG] channel object = {channel}")
    if not channel:
        print("[LEADERBOARD DEBUG] bot.get_channel returned None, returning early")
        return []

    try:
        print(f"[LEADERBOARD DEBUG] Querying with week_start={week_start.isoformat()}")
        results = get_leaderboard_top3("activity", guild.id, week_start, "count")
        print(f"[LEADERBOARD DEBUG] Got {len(results)} result(s)")

        if not results:
            print("[LEADERBOARD DEBUG] No results, sending 'no activity' message")
            await channel.send("📊 **Weekly Leaderboard** — no activity recorded this week!")
            return []

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, doc in enumerate(results):
            data = doc.to_dict()
            lines.append(f"{medals[i]} <@{data['user_id']}> — {data['count']} messages")

        print("[LEADERBOARD DEBUG] Sending results message")
        await channel.send("**📊 Weekly Leaderboard Results**\n\n" + "\n".join(lines))
        print("[LEADERBOARD DEBUG] Message sent successfully")

        await pay_leaderboard_winners(guild.id, results, config.ACTIVITY_LEADERBOARD_REWARDS)
        return results
    except Exception as e:
        print(f"[LEADERBOARD ERROR] {type(e).__name__}: {e}")
        return []

async def post_weekly_received_leaderboard(guild, week_start):
    channel_id = get_setting(f"leaderboard_channel_id_{guild.id}", None)
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        results = get_leaderboard_top3("coins_received", guild.id, week_start, "total")

        if not results:
            await channel.send("🪙 **Weekly Top Earners** — no coins received this week!")
            return []

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, doc in enumerate(results):
            data = doc.to_dict()
            lines.append(f"{medals[i]} <@{data['user_id']}> — {data['total']} 🪙")

        await channel.send("**🪙 Weekly Top Earners**\n\n" + "\n".join(lines))

        await pay_leaderboard_winners(guild.id, results, config.RECEIVED_LEADERBOARD_REWARDS)
        return results
    except Exception as e:
        print(f"[RECEIVED LEADERBOARD ERROR] {type(e).__name__}: {e}")
        return []

async def post_weekly_session_leaderboard(guild, week_start):
    channel_id = get_setting(f"leaderboard_channel_id_{guild.id}", None)
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    try:
        results = get_leaderboard_top3("session_activity", guild.id, week_start, "count")

        if not results:
            await channel.send("🔥 **Weekly Session Leaderboard** — no completed sessions this week!")
            return []

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, doc in enumerate(results):
            data = doc.to_dict()
            lines.append(f"{medals[i]} <@{data['user_id']}> — {data['count']} session(s)")

        await channel.send("**🔥 Weekly Session Leaderboard Results**\n\n" + "\n".join(lines))

        await pay_leaderboard_winners(guild.id, results, config.SESSION_LEADERBOARD_REWARDS)
        return results
    except Exception as e:
        print(f"[SESSION LEADERBOARD ERROR] {type(e).__name__}: {e}")
        return []

async def daily_eligibility_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(config.DAILY_ELIGIBILITY_CHECK_INTERVAL)
        today = datetime.now(ZoneInfo("Europe/Bucharest")).date().isoformat()
        now = time.time()

        for guild in bot.guilds:
            if guild.id != config.ALLOWED_GUILD_ID:
                continue
            channel_id = get_setting(f"daily_channel_id_{guild.id}", None)
            if not channel_id:
                continue
            channel = bot.get_channel(channel_id)
            if not channel:
                continue

            for member in guild.members:
                if member.bot:
                    continue
                if member.id in config.EXCLUDED_DAILY_USERS:
                    continue

                ref = db.collection("users").document(str(member.id))
                doc = ref.get()
                if not doc.exists:
                    continue
                data = doc.to_dict()

                if data.get("last_message_date") != today:
                    continue

                last_claim = data.get("last_daily_claim")
                if last_claim is not None and (now - last_claim) < config.DAILY_CLAIM_COOLDOWN:
                    continue

                if data.get("daily_reminder_sent"):
                    continue

                view = View(timeout=None)
                view.add_item(DailyClaimButton(member.id))
                await channel.send(
                    f"🪙 {member.mention}, your daily reward is ready! Click below to claim it.",
                    view=view
                )
                ref.update({"daily_reminder_sent": True})

class DailyClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"daily_claim:(?P<user_id>[0-9]+)"):
    def __init__(self, user_id: int):
        super().__init__(
            discord.ui.Button(
                label="Claim Daily Reward",
                style=discord.ButtonStyle.green,
                emoji="🪙",
                custom_id=f"daily_claim:{user_id}"
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        user_id = int(match["user_id"])
        return cls(user_id)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your claim!", ephemeral=True)
            return

        user_data = get_or_create_user(interaction.user)
        now = time.time()
        last_claim = user_data.get("last_daily_claim")
        current_streak = user_data.get("daily_streak", 0)

        if last_claim is not None and (now - last_claim) < config.DAILY_CLAIM_COOLDOWN:
            await interaction.response.send_message("❌ You've already claimed your daily reward recently!", ephemeral=True)
            return

        if last_claim is not None and (now - last_claim) <= config.DAILY_STREAK_GRACE:
            new_streak = current_streak + 1
        else:
            new_streak = 1

        capped_streak = min(new_streak, config.DAILY_STREAK_CAP)
        reward = config.DAILY_REWARD_BASE + (capped_streak - 1) * config.DAILY_STREAK_BONUS_PER_DAY

        ref = db.collection("users").document(str(interaction.user.id))
        ref.update({
            "balance": user_data["balance"] + reward,
            "last_daily_claim": now,
            "daily_streak": new_streak,
            "daily_reminder_sent": False
        })
        increment_coins_received(interaction.guild_id, interaction.user.id, reward)

        self.item.disabled = True
        await interaction.response.edit_message(view=self.view)

        streak_text = f"{new_streak} day" if new_streak == 1 else f"{new_streak} days"
        await interaction.followup.send(
            f"✅ You claimed **{reward} 🪙**! (Streak: {streak_text})",
            ephemeral=True
        )

STYLE_MAP = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

class JobRoleSelectView(View):
    def __init__(self):
        super().__init__(timeout=None)  # permanent board, always usable
        for role_number, role_info in config.JOB_ROLE_GROUPS.items():
            self.add_item(self.make_join_button(role_number, role_info))
        self.add_item(self.make_quit_button())

    def make_join_button(self, role_number, role_info):
        button = Button(
            label=f"Join ({role_info['name']})",
            style=STYLE_MAP[role_info["style"]],
            custom_id=f"job_role_join_{role_number}"
        )

        async def callback(interaction: discord.Interaction):
            await self.handle_join(interaction, role_number)

        button.callback = callback
        return button

    def make_quit_button(self):
        button = Button(label="Quit Current Job", style=discord.ButtonStyle.secondary, row=1, custom_id="job_role_quit")

        async def callback(interaction: discord.Interaction):
            await self.handle_quit(interaction)

        button.callback = callback
        return button

    async def handle_join(self, interaction: discord.Interaction, role_number: int):
        user_data = get_or_create_user(interaction.user)
        now = time.time()

        if user_data.get("current_job_role"):
            await interaction.response.send_message(
                "❌ You already have a job! Quit first before picking a new one.",
                ephemeral=True
            )
            return

        lockout_until = user_data.get("job_quit_lockout_until")
        if lockout_until and now < lockout_until:
            remaining = int(lockout_until - now)
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await interaction.response.send_message(
                f"❌ You quit recently! You can join a new job in {hours}h {minutes}m.",
                ephemeral=True
            )
            return

        role_id = get_setting(f"job_role_{role_number}_id_{interaction.guild_id}", None)
        if not role_id:
            await interaction.response.send_message(
                f"❌ Job Role {role_number} hasn't been set up yet! Ask a Mod/Owner to run /csetjobrole.",
                ephemeral=True
            )
            return

        role = interaction.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message("❌ Couldn't find that role on the server!", ephemeral=True)
            return

        try:
            await interaction.user.add_roles(role, reason="Joined job role")
        except Exception as e:
            await interaction.response.send_message(f"❌ Couldn't assign the role: {e}", ephemeral=True)
            return

        db.collection("users").document(str(interaction.user.id)).update({"current_job_role": role_number})

        role_name = config.JOB_ROLE_GROUPS[role_number]["name"]
        await interaction.response.send_message(
            f"✅ You're now working as **Job Role {role_number} ({role_name})**!",
            ephemeral=True
        )

    async def handle_quit(self, interaction: discord.Interaction):
        user_data = get_or_create_user(interaction.user)
        current_role_number = user_data.get("current_job_role")

        if not current_role_number:
            await interaction.response.send_message("❌ You don't currently have a job!", ephemeral=True)
            return

        role_id = get_setting(f"job_role_{current_role_number}_id_{interaction.guild_id}", None)
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await interaction.user.remove_roles(role, reason="Quit job role")
                except Exception as e:
                    print(f"[JOB ERROR] Failed to remove role from {interaction.user.id}: {e}")

        lockout_until = time.time() + config.JOB_QUIT_LOCKOUT
        db.collection("users").document(str(interaction.user.id)).update({
            "current_job_role": None,
            "job_quit_lockout_until": lockout_until
        })

        await interaction.response.send_message(
            "✅ You've quit your job. You can join a new one in 2 days.",
            ephemeral=True
        )

def get_job_role_number(job_name):
    for role_number, role_info in config.JOB_ROLE_GROUPS.items():
        if job_name in role_info["jobs"]:
            return role_number
    return None

async def schedule_job_ready_dm(user_id: int, job_name: str, floor_name: str, ready_at: float):
    delay = ready_at - time.time()
    if delay > 0:
        await asyncio.sleep(delay)

    user = bot.get_user(user_id)
    if not user:
        try:
            user = await bot.fetch_user(user_id)
        except Exception as e:
            print(f"[JOB DM ERROR] Couldn't fetch user {user_id}: {e}")
            return

    job_label = job_name.replace("_", " ").title()
    floor_label = floor_name.replace("_", " ").title()
    try:
        await user.send(f"🪙 Your **{job_label}** job on the **{floor_label}** floor is ready again — go earn some coins!")
    except Exception as e:
        print(f"[JOB DM ERROR] Couldn't DM {user_id}: {e}")

class JobButtonView(View):
    def __init__(self, floor_name):
        super().__init__(timeout=None)  # permanent, always usable
        self.floor_name = floor_name
        for job_name in config.FLOOR_JOBS[floor_name]:
            self.add_item(self.make_job_button(job_name))

    def make_job_button(self, job_name):
        role_number = get_job_role_number(job_name)
        style = STYLE_MAP[config.JOB_ROLE_GROUPS[role_number]["style"]]
        label = job_name.replace("_", " ").title()

        button = Button(label=label, style=style, custom_id=f"job_button_{job_name}_{self.floor_name}")

        async def callback(interaction: discord.Interaction):
            await self.handle_job_click(interaction, job_name)

        button.callback = callback
        return button

    async def handle_job_click(self, interaction: discord.Interaction, job_name: str):
        user_data = get_or_create_user(interaction.user)
        required_role_number = get_job_role_number(job_name)

        if user_data.get("current_job_role") != required_role_number:
            await interaction.response.send_message(
                f"❌ You need to be working **Job Role {required_role_number}** to do this job!",
                ephemeral=True
            )
            return

        cooldown_key = f"{job_name}_{self.floor_name}"
        job_cooldowns = user_data.get("job_cooldowns", {})
        last_claim = job_cooldowns.get(cooldown_key)

        min_pay, max_pay, cooldown_seconds = config.JOB_PAY_INFO[job_name]
        now = time.time()

        if last_claim is not None and (now - last_claim) < cooldown_seconds:
            remaining = int(cooldown_seconds - (now - last_claim))
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            await interaction.response.send_message(
                f"❌ You're still on cooldown for this job here! Try again in {hours}h {minutes}m.",
                ephemeral=True
            )
            return

        import random
        reward = random.randint(min_pay, max_pay)

        job_cooldowns[cooldown_key] = now
        ref = db.collection("users").document(str(interaction.user.id))
        ref.update({
            "balance": user_data["balance"] + reward,
            "job_cooldowns": job_cooldowns
        })
        increment_coins_received(interaction.guild_id, interaction.user.id, reward)

        ready_at = now + cooldown_seconds
        bot.loop.create_task(schedule_job_ready_dm(interaction.user.id, job_name, self.floor_name, ready_at))

        job_label = job_name.replace("_", " ").title()
        await log_transaction(f"💼 Job: <@{interaction.user.id}> earned {reward} 🪙 working as a **{job_label}**", interaction.guild_id)
        await interaction.response.send_message(
            f"✅ You worked as a **{job_label}** and earned **{reward} 🪙**!",
            ephemeral=True
        )

async def leaderboard_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(ZoneInfo("Europe/Bucharest"))
        current_week = get_current_week_start()
        next_week = current_week + timedelta(days=7)
        seconds_until_next = (next_week - now).total_seconds()

        print(f"Leaderboard loop sleeping {int(seconds_until_next)}s until next Saturday midnight (Bucharest)")
        await asyncio.sleep(seconds_until_next)

        for guild in bot.guilds:
            if guild.id != config.ALLOWED_GUILD_ID:
                continue
            activity_results = await post_weekly_leaderboard(guild, current_week)
            session_results = await post_weekly_session_leaderboard(guild, current_week)
            received_results = await post_weekly_received_leaderboard(guild, current_week)
            await assign_weekly_vip(guild, activity_results, session_results, received_results)

@bot.tree.command(name="cprofile", description="View your profile")
async def profile(interaction: discord.Interaction):
    user = get_or_create_user(interaction.user)
    await interaction.response.send_message(
        f"**{interaction.user.name}'s Profile**\n"
        f"💰 Balance: {user['balance']} 🪙\n"
        f"🌸 Status: {user['status']}\n"
        f"🔢 Body Count: {user['body_count']}\n"
        f"🏠 House: {user['house'] or 'None'}"
    )
    
@bot.tree.command(name="cbalance", description="Check your current credit balance")
async def cbalance(interaction: discord.Interaction):
    user = get_or_create_user(interaction.user)
    await interaction.response.send_message(f"💰 Your current balance is **{user['balance']} 🪙**!", ephemeral=True)


@bot.tree.command(name="cview", description="View another user's profile")
async def cview(interaction: discord.Interaction, user: discord.Member):
    profile = get_or_create_user(user)
    await interaction.response.send_message(
        f"**{user.name}'s Profile**\n"
        f"💰 Balance: {profile['balance']} 🪙\n"
        f"🌸 Status: {profile['status']}\n"
        f"🔢 Body Count: {profile['body_count']}\n"
        f"🏠 House: {profile['house'] or 'None'}"
    )

@bot.tree.command(name="cleaderboard", description="Show the top 3 most active users this week")
async def cleaderboard(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        current_week = get_current_week_start()
        results = get_leaderboard_top3("activity", interaction.guild_id, current_week, "count")

        if not results:
            await interaction.followup.send("No activity recorded yet this week!")
            return

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, doc in enumerate(results):
            data = doc.to_dict()
            user_id = data["user_id"]
            count = data["count"]
            lines.append(f"{medals[i]} <@{user_id}> — {count} messages")

        await interaction.followup.send(
            "**📊 This Week's Most Active Users**\n\n" + "\n".join(lines)
        )
    except Exception as e:
        print(f"[LEADERBOARD ERROR] {type(e).__name__}: {e}")
        await interaction.followup.send(f"❌ Something went wrong: {e}")

@bot.tree.command(name="csessionleaderboard", description="Show the top 3 users with the most completed sessions this week")
async def csessionleaderboard(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        current_week = get_current_week_start()
        results = get_leaderboard_top3("session_activity", interaction.guild_id, current_week, "count")

        if not results:
            await interaction.followup.send("No completed sessions recorded yet this week!")
            return

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, doc in enumerate(results):
            data = doc.to_dict()
            user_id = data["user_id"]
            count = data["count"]
            lines.append(f"{medals[i]} <@{user_id}> — {count} session(s)")

        await interaction.followup.send(
            "**🔥 This Week's Most Active Session Users**\n\n" + "\n".join(lines)
        )
    except Exception as e:
        print(f"[SESSION LEADERBOARD ERROR] {type(e).__name__}: {e}")
        await interaction.followup.send(f"❌ Something went wrong: {e}")

@bot.tree.command(name="creceivedleaderboard", description="Show the top 3 users who received the most coins this week")
async def creceivedleaderboard(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        current_week = get_current_week_start()
        results = get_leaderboard_top3("coins_received", interaction.guild_id, current_week, "total")

        if not results:
            await interaction.followup.send("No coins received yet this week!")
            return

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, doc in enumerate(results):
            data = doc.to_dict()
            user_id = data["user_id"]
            total = data["total"]
            lines.append(f"{medals[i]} <@{user_id}> — {total} 🪙")

        await interaction.followup.send(
            "**🪙 This Week's Top Earners**\n\n" + "\n".join(lines)
        )
    except Exception as e:
        print(f"[RECEIVED LEADERBOARD ERROR] {type(e).__name__}: {e}")
        await interaction.followup.send(f"❌ Something went wrong: {e}")

class TransactionConfirmView(View):
    def __init__(self, sender_id: int, receiver_id: int, amount: int):
        super().__init__(timeout=config.CONFIRMATION_TIMEOUT)
        self.sender_id = sender_id
        self.receiver_id = receiver_id
        self.amount = amount
        self.message = None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True

        sender_ref = db.collection("users").document(str(self.sender_id))
        receiver_ref = db.collection("users").document(str(self.receiver_id))

        sender_data = sender_ref.get().to_dict()

        if sender_data["balance"] < self.amount:
            await interaction.response.edit_message(content="❌ The sender no longer has enough 🪙!", view=None)
            return

        sender_ref.update({"balance": sender_data["balance"] - self.amount})
        receiver_data = receiver_ref.get().to_dict()
        receiver_ref.update({"balance": receiver_data["balance"] + self.amount})
        increment_coins_received(interaction.guild_id, self.receiver_id, self.amount)

        await log_transaction(f"💸 Transaction: <@{self.sender_id}> sent {self.amount} 🪙 to <@{self.receiver_id}>", interaction.guild_id)
        await interaction.response.edit_message(content=f"✅ Transaction complete! {self.amount} 🪙 transferred.", view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Transaction declined.", view=None)
        
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            await self.message.edit(content="⏰ This transaction request has expired.", view=self)


@bot.tree.command(name="csend", description="Send 🪙 to another user")
async def csend(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Amount must be greater than 0!", ephemeral=True)
        return

    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't send 🪙 to yourself!", ephemeral=True)
        return
    
    if not get_setting(f"log_channel_id_{interaction.guild_id}", None):
        await interaction.response.send_message("❌ Transactions are disabled until a log channel is set by a Mod/Owner!", ephemeral=True)
        return

    get_or_create_user(user)
    view = TransactionConfirmView(interaction.user.id, user.id, amount)
    await interaction.response.send_message(f"💸 {interaction.user.name} wants to send {amount} 🪙 to {user.mention}. Do you accept?", view=view)
    view.message = await interaction.original_response()

class RequestConfirmView(View):
    def __init__(self, requester_id: int, target_id: int, amount: int):
        super().__init__(timeout=config.CONFIRMATION_TIMEOUT)
        self.requester_id = requester_id
        self.target_id = target_id
        self.amount = amount
        self.message = None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True

        target_ref = db.collection("users").document(str(self.target_id))
        requester_ref = db.collection("users").document(str(self.requester_id))

        target_data = target_ref.get().to_dict()

        if target_data["balance"] < self.amount:
            await interaction.response.edit_message(content="❌ You don't have enough 🪙!", view=None)
            return

        target_ref.update({"balance": target_data["balance"] - self.amount})
        requester_data = requester_ref.get().to_dict()
        requester_ref.update({"balance": requester_data["balance"] + self.amount})
        increment_coins_received(interaction.guild_id, self.requester_id, self.amount)

        await log_transaction(f"💰 Request: <@{self.target_id}> sent {self.amount} 🪙 to <@{self.requester_id}>", interaction.guild_id)
        await interaction.response.edit_message(content=f"✅ Request accepted! {self.amount} 🪙 transferred.", view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Request declined.", view=None)
        
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            await self.message.edit(content="⏰ This request has expired.", view=self)


@bot.tree.command(name="crequest", description="Request 🪙 from another user")
async def crequest(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Amount must be greater than 0!", ephemeral=True)
        return

    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't request 🪙 from yourself!", ephemeral=True)
        return
    
    if not get_setting(f"log_channel_id_{interaction.guild_id}", None):
        await interaction.response.send_message("❌ Transactions are disabled until a log channel is set by a Mod/Owner!", ephemeral=True)
        return

    get_or_create_user(interaction.user)
    get_or_create_user(user)

    view = RequestConfirmView(interaction.user.id, user.id, amount)
    await interaction.response.send_message(f"💰 {interaction.user.name} is requesting {amount} 🪙 from {user.mention}. Do you accept?", view=view)
    view.message = await interaction.original_response()


class SessionConfirmView(View):
    def __init__(self, initiator: discord.Member, target: discord.Member, price: int):
        super().__init__(timeout=config.CONFIRMATION_TIMEOUT)
        self.initiator = initiator
        self.target = target
        self.price = price
        self.message = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            await self.message.edit(content="⏰ This session request has expired.", view=self)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This confirmation isn't for you!", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True

        initiator_id = self.initiator.id
        target_id = self.target.id

        if initiator_id in active_sessions:
            await interaction.response.send_message("The initiator is already in a session!", ephemeral=True)
            return

        if target_id in active_sessions:
            await interaction.response.send_message("You're already in a session!", ephemeral=True)
            return

        session_id = str(uuid.uuid4())
        sessions[session_id] = {
            "members": {initiator_id, target_id},
            "channel_id": interaction.channel.id,
            "guild_id": interaction.guild_id,
            "payer_id": target_id,
            "payee_id": initiator_id,
            "price": self.price,
            "last_active": time.time(),
            "started_at": time.time(),
            "afk_checks_passed": {}
        }
        active_sessions[initiator_id] = session_id
        active_sessions[target_id] = session_id

        initiator_data = get_or_create_user(self.initiator)
        get_or_create_user(self.target)

        if initiator_data["balance"] < self.price:
            await interaction.response.edit_message(content=f"❌ {self.initiator.name} can't afford this session! Price: {self.price} 🪙.", view=None)
            return

        db.collection("users").document(str(initiator_id)).update({"balance": initiator_data["balance"] - self.price})
        target_data = db.collection("users").document(str(target_id)).get().to_dict()

        new_target_balance = target_data["balance"] + self.price
        update_data = {"balance": new_target_balance}

        if target_data.get("allure_boost_multiplier"):
            sessions_left = target_data.get("allure_boost_sessions_left", 0) - 1
            if sessions_left <= 0:
                update_data["allure_boost_multiplier"] = None
                update_data["allure_boost_sessions_left"] = 0
            else:
                update_data["allure_boost_sessions_left"] = sessions_left

        db.collection("users").document(str(target_id)).update(update_data)
        increment_coins_received(interaction.guild_id, target_id, self.price)

        await log_transaction(f"💋 Session: <@{initiator_id}> paid {self.price} 🪙 to <@{target_id}>", interaction.guild_id)
        await interaction.response.edit_message(content=f"🔥 {self.initiator.name} and {self.target.name} are now in a session! {self.price} 🪙 transferred.", view=None)
    
    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This confirmation isn't for you!", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True

        self.stop()
        await interaction.response.edit_message(content=f"❌ {self.target.name} declined the session request.", view=None)


@bot.tree.command(name="csexbuy", description="Buy someone's services and start a session")
async def csex(interaction: discord.Interaction, user: discord.Member):
    initiator_id = interaction.user.id
    target_id = user.id

    if initiator_id == target_id:
        await interaction.response.send_message("You can't start a session with yourself!", ephemeral=True)
        return

    if initiator_id in active_sessions:
        await interaction.response.send_message("You're already in a session!", ephemeral=True)
        return

    if target_id in active_sessions:
        await interaction.response.send_message(f"{user.name} is already in a session!", ephemeral=True)
        return

    target_data = get_or_create_user(user)
    price = calculate_session_price(target_data["body_count"], target_data.get("allure_boost_multiplier"))

    view = SessionConfirmView(initiator=interaction.user, target=user, price=price)
    await interaction.response.send_message(
        f"💌 {interaction.user.name} wants to start a session with {user.mention}.\n"
        f"💰 Session price: **{price} 🪙**. Do you accept?",
        view=view
    )
    view.message = await interaction.original_response()
    
class SessionSellConfirmView(View):
    def __init__(self, initiator: discord.Member, target: discord.Member, price: int):
        super().__init__(timeout=config.CONFIRMATION_TIMEOUT)
        self.initiator = initiator
        self.target = target
        self.price = price
        self.message = None

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This confirmation isn't for you!", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True

        initiator_id = self.initiator.id
        target_id = self.target.id

        if initiator_id in active_sessions:
            await interaction.response.edit_message(content="The initiator is already in a session!", view=None)
            return

        if target_id in active_sessions:
            await interaction.response.edit_message(content="You're already in a session!", view=None)
            return

        target_data = get_or_create_user(self.target)
        get_or_create_user(self.initiator)

        if target_data["balance"] < self.price:
            await interaction.response.edit_message(content=f"❌ You can't afford this session! Price: {self.price} 🪙.", view=None)
            return

        db.collection("users").document(str(target_id)).update({"balance": target_data["balance"] - self.price})
        initiator_data = db.collection("users").document(str(initiator_id)).get().to_dict()

        new_initiator_balance = initiator_data["balance"] + self.price
        update_data = {"balance": new_initiator_balance}

        if initiator_data.get("allure_boost_multiplier"):
            sessions_left = initiator_data.get("allure_boost_sessions_left", 0) - 1
            if sessions_left <= 0:
                update_data["allure_boost_multiplier"] = None
                update_data["allure_boost_sessions_left"] = 0
            else:
                update_data["allure_boost_sessions_left"] = sessions_left

        db.collection("users").document(str(initiator_id)).update(update_data)
        increment_coins_received(interaction.guild_id, initiator_id, self.price)

        session_id = str(uuid.uuid4())
        sessions[session_id] = {
            "members": {initiator_id, target_id},
            "channel_id": interaction.channel.id,
            "guild_id": interaction.guild_id,
            "payer_id": target_id,
            "payee_id": initiator_id,
            "price": self.price,
            "last_active": time.time(),
            "started_at": time.time(),
            "afk_checks_passed": {}
        }
        active_sessions[initiator_id] = session_id
        active_sessions[target_id] = session_id

        await log_transaction(f"💋 Session: <@{target_id}> paid {self.price} 🪙 to <@{initiator_id}>", interaction.guild_id)
        await interaction.response.edit_message(content=f"🔥 {self.initiator.name} and {self.target.name} are now in a session! {self.price} 🪙 transferred.", view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This confirmation isn't for you!", ephemeral=True)
            return
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"❌ {self.target.name} declined the session request.", view=None)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            await self.message.edit(content="⏰ This session request has expired.", view=self)
    
@bot.tree.command(name="csexsell", description="Offer your services and start a session")
async def csexsell(interaction: discord.Interaction, user: discord.Member):
    initiator_id = interaction.user.id
    target_id = user.id

    if initiator_id == target_id:
        await interaction.response.send_message("You can't start a session with yourself!", ephemeral=True)
        return

    if initiator_id in active_sessions:
        await interaction.response.send_message("You're already in a session!", ephemeral=True)
        return

    if target_id in active_sessions:
        await interaction.response.send_message(f"{user.name} is already in a session!", ephemeral=True)
        return

    initiator_data = get_or_create_user(interaction.user)
    price = calculate_session_price(initiator_data["body_count"], initiator_data.get("allure_boost_multiplier"))

    view = SessionSellConfirmView(initiator=interaction.user, target=user, price=price)
    await interaction.response.send_message(
        f"💌 {interaction.user.name} is offering their services to {user.mention}.\n"
        f"💰 Session price: **{price} 🪙**. Do you accept?",
        view=view
    )
    view.message = await interaction.original_response()    
    
    
@bot.tree.command(name="cend", description="End your current session")
async def cend(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in active_sessions:
        await interaction.response.send_message("You're not in a session!", ephemeral=True)
        return

    session_id = active_sessions[user_id]
    session_members = sessions[session_id]["members"].copy()
    guild_id = sessions[session_id].get("guild_id")

    # Update everyone in the session
    for member_id in session_members:
        ref = db.collection("users").document(str(member_id))
        user_data = ref.get().to_dict()
        partners = user_data.get("partners", [])
        
        new_partners = [str(m) for m in session_members if m != member_id and str(m) not in partners]
        new_body_count = user_data["body_count"] + len(new_partners)
        
        updated_partners = partners + new_partners
        
        update_data = {"partners": updated_partners}
        if new_body_count > user_data["body_count"]:
            update_data["body_count"] = new_body_count
        if user_data["status"] == "virgin" and new_body_count > 0:
            update_data["status"] = "non-virgin"
            
        ref.update(update_data)
        active_sessions.pop(member_id, None)

        if guild_id:
            increment_session_count(guild_id, member_id)

    sessions.pop(session_id, None)

    await interaction.response.send_message(f"✅ Session ended! Everyone's stats have been updated.")


@bot.tree.command(name="cinterrupt", description="Leave your current session without updating stats")
async def cinterrupt(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id not in active_sessions:
        await interaction.response.send_message("You're not in a session!", ephemeral=True)
        return

    session_id = active_sessions[user_id]
    session_data = sessions[session_id]
    session_data["members"].remove(user_id)
    active_sessions.pop(user_id, None)

    remaining = session_data["members"]

    if len(remaining) < 2:
        # End session for everyone, no updates, refund the payer
        for member_id in remaining:
            active_sessions.pop(member_id, None)

        payer_id = session_data.get("payer_id")
        payee_id = session_data.get("payee_id")
        price = session_data.get("price")

        if payer_id and payee_id and price:
            payer_ref = db.collection("users").document(str(payer_id))
            payee_ref = db.collection("users").document(str(payee_id))
            payer_data = payer_ref.get().to_dict()
            payee_data = payee_ref.get().to_dict()

            print(f"DEBUG: payer_id={payer_id}, payee_id={payee_id}, price={price}")
            print(f"DEBUG: payer balance before={payer_data['balance']}, payee balance before={payee_data['balance']}")

            payer_ref.update({"balance": payer_data["balance"] + price})
            payee_ref.update({"balance": payee_data["balance"] - price})

            print(f"DEBUG: refund complete")

            await log_transaction(f"↩️ Refund: <@{payee_id}> refunded {price} 🪙 to <@{payer_id}> (session interrupted)", interaction.guild_id)

        sessions.pop(session_id, None)
        await interaction.response.send_message(f"⛔ {interaction.user.name} left the session. Not enough participants, session ended for everyone. No stats updated, payment refunded.")
    else:
        await interaction.response.send_message(f"⛔ {interaction.user.name} left the session. Others are still going!")


@bot.tree.command(name="cedit", description="Edit a user's profile (Mod/Owner only)")
@app_commands.describe(
    user="The user to edit",
    field="Field to edit (balance, value, body_count, status, house)",
    value="New value to set"
)
async def cedit(interaction: discord.Interaction, user: discord.Member, field: str, value: str):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    valid_fields = ["balance", "body_count", "status", "house"]
    if field not in valid_fields:
        await interaction.response.send_message(f"Invalid field! Choose from: {', '.join(valid_fields)}", ephemeral=True)
        return

    # Convert to int if needed
    if field in ["balance", "body_count"]:
        try:
            value = int(value)
        except ValueError:
            await interaction.response.send_message("That field requires a number!", ephemeral=True)
            return

    ref = db.collection("users").document(str(user.id))
    if not ref.get().exists:
        await interaction.response.send_message("That user doesn't have a profile yet!", ephemeral=True)
        return

    ref.update({field: value})
    await interaction.response.send_message(f"✅ Updated {user.name}'s {field} to {value}!")


@bot.tree.command(name="creset", description="Reset a user's status and body count (Mod/Owner only)")
async def creset(interaction: discord.Interaction, user: discord.Member):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    ref = db.collection("users").document(str(user.id))
    if not ref.get().exists:
        await interaction.response.send_message("That user doesn't have a profile yet!", ephemeral=True)
        return

    ref.update({
        "body_count": STARTING_BODY_COUNT,
        "status": STARTING_STATUS
    })
    await interaction.response.send_message(f"✅ {user.name}'s profile has been reset!")

@bot.tree.command(name="cstrike", description="Add a strike to a user (Mod/Owner only)")
async def cstrike(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return
    
    if not get_setting(f"strike_log_channel_id_{interaction.guild_id}", None):
        await interaction.response.send_message("❌ Strikes are disabled until a strike log channel is set!", ephemeral=True)
        return

    user_data = get_or_create_user(user)
    new_strikes = user_data.get("strikes", 0) + 1

    db.collection("users").document(str(user.id)).update({"strikes": new_strikes})

    await log_strike(f"⚠️ **Strike Added**\nUser: {user.mention}\nNew Strike Count: {new_strikes}\nIssued by: {interaction.user.mention}\nReason: {reason}", interaction.guild_id)
    await interaction.response.send_message(f"⚠️ {user.mention} now has **{new_strikes} strike(s)**.", ephemeral=True)

@bot.tree.command(name="cunstrike", description="Remove a strike from a user (Mod/Owner only)")
async def cunstrike(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return
    
    if not get_setting(f"strike_log_channel_id_{interaction.guild_id}", None):
        await interaction.response.send_message("❌ Strikes are disabled until a strike log channel is set!", ephemeral=True)
        return

    user_data = get_or_create_user(user)
    current_strikes = user_data.get("strikes", 0)

    if current_strikes <= 0:
        await interaction.response.send_message(f"{user.mention} has no strikes to remove!", ephemeral=True)
        return

    new_strikes = current_strikes - 1
    db.collection("users").document(str(user.id)).update({"strikes": new_strikes})

    await log_strike(f"✅ **Strike Removed**\nUser: {user.mention}\nNew Strike Count: {new_strikes}\nIssued by: {interaction.user.mention}\nReason: {reason}", interaction.guild_id)
    await interaction.response.send_message(f"✅ Removed a strike from {user.mention}. They now have **{new_strikes} strike(s)**.", ephemeral=True)

@bot.tree.command(name="cmodview", description="View a user's full profile including strikes (Mod/Owner only)")
async def cmodview(interaction: discord.Interaction, user: discord.Member):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    user_data = get_or_create_user(user)
    await interaction.response.send_message(
        f"**{user.name}'s Full Profile** 🔍\n"
        f"💰 Balance: {user_data['balance']} 🪙\n"
        f"🌸 Status: {user_data['status']}\n"
        f"🔢 Body Count: {user_data['body_count']}\n"
        f"🏠 House: {user_data['house'] or 'None'}\n"
        f"⚠️ Strikes: {user_data.get('strikes', 0)}",
        ephemeral=True
    )

@bot.tree.command(name="csetlogchannel", description="Set the channel for transaction logs (Mod/Owner only)")
async def csetlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    set_setting(f"log_channel_id_{interaction.guild_id}", channel.id)
    await interaction.response.send_message(f"✅ Transaction log channel set to {channel.mention}!")

@bot.tree.command(name="csetannouncechannel", description="Set the channel for bot status announcements (Mod/Owner only)")
async def csetannouncechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    set_setting(f"announce_channel_id_{interaction.guild_id}", channel.id)
    await interaction.response.send_message(f"✅ Announcement channel set to {channel.mention}!")

@bot.tree.command(name="csetleaderboardchannel", description="Set the channel for the automatic weekly leaderboard (Mod/Owner only)")
async def csetleaderboardchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    set_setting(f"leaderboard_channel_id_{interaction.guild_id}", channel.id)
    await interaction.response.send_message(f"✅ Leaderboard channel set to {channel.mention}!")

@bot.tree.command(name="csetdailychannel", description="Set the channel for daily reward claim pings (Mod/Owner only)")
async def csetdailychannel(interaction: discord.Interaction, channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    set_setting(f"daily_channel_id_{interaction.guild_id}", channel.id)
    await interaction.response.send_message(f"✅ Daily reward channel set to {channel.mention}!")

@bot.tree.command(name="csetviprole", description="Set the role granted to weekly leaderboard winners (Mod/Owner only)")
async def csetviprole(interaction: discord.Interaction, role: discord.Role):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [r.name for r in interaction.user.roles]

    if not any(r in user_roles for r in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    set_setting(f"vip_role_id_{interaction.guild_id}", role.id)
    await interaction.response.send_message(f"✅ VIP role set to {role.mention}!")
    
@bot.tree.command(name="cclearjoblockout", description="Clear a user's job-quit lockout, letting them join a job role early (Mod/Owner only)")
async def cclearjoblockout(interaction: discord.Interaction, user: discord.Member):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [r.name for r in interaction.user.roles]

    if not any(r in user_roles for r in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    try:
        get_or_create_user(user)
        db.collection("users").document(str(user.id)).update({"job_quit_lockout_until": None})
        await interaction.response.send_message(f"✅ Cleared {user.mention}'s job-quit lockout. They can join a new job role now.", ephemeral=True)
    except Exception as e:
        print(f"[JOB LOCKOUT CLEAR ERROR] {type(e).__name__}: {e}")
        await interaction.response.send_message(f"❌ Something went wrong: {e}", ephemeral=True)

@bot.tree.command(name="cresetjobcooldowns", description="Reset all of a user's job cooldowns, letting them work every job again immediately (Mod/Owner only)")
async def cresetjobcooldowns(interaction: discord.Interaction, user: discord.Member):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [r.name for r in interaction.user.roles]

    if not any(r in user_roles for r in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    try:
        user_data = get_or_create_user(user)
        job_cooldowns = user_data.get("job_cooldowns", {})

        for cooldown_key, last_claim in job_cooldowns.items():
            parts = cooldown_key.rsplit("_", 1)
            if len(parts) != 2:
                continue
            job_name, floor_name = parts
            if job_name not in config.JOB_PAY_INFO:
                continue

            _, _, cooldown_seconds = config.JOB_PAY_INFO[job_name]
            ready_at = last_claim + cooldown_seconds

            if ready_at > time.time():
                bot.loop.create_task(schedule_job_ready_dm(user.id, job_name, floor_name, time.time()))

        db.collection("users").document(str(user.id)).update({"job_cooldowns": {}})
        await interaction.response.send_message(f"✅ Cleared all job cooldowns for {user.mention}. They can work any job immediately.", ephemeral=True)
    except Exception as e:
        print(f"[JOB COOLDOWN RESET ERROR] {type(e).__name__}: {e}")
        await interaction.response.send_message(f"❌ Something went wrong: {e}", ephemeral=True)

@bot.tree.command(name="csetjobrole", description="Link a Discord role to one of the 4 Job Roles (Mod/Owner only)")
@app_commands.describe(job_role_number="Which Job Role this is (1-4)", role="The Discord role to link")
async def csetjobrole(interaction: discord.Interaction, job_role_number: int, role: discord.Role):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [r.name for r in interaction.user.roles]

    if not any(r in user_roles for r in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    if job_role_number not in [1, 2, 3, 4]:
        await interaction.response.send_message("❌ job_role_number must be 1, 2, 3, or 4!", ephemeral=True)
        return

    try:
        set_setting(f"job_role_{job_role_number}_id_{interaction.guild_id}", role.id)
        await interaction.response.send_message(f"✅ Job Role {job_role_number} linked to {role.mention}!")
    except Exception as e:
        print(f"[JOB ROLE SET ERROR] {type(e).__name__}: {e}")
        await interaction.response.send_message(f"❌ Something went wrong: {e}", ephemeral=True)

@bot.tree.command(name="cpostjobselect", description="Post the job role select board to a channel (Mod/Owner only)")
async def cpostjobselect(interaction: discord.Interaction, channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [r.name for r in interaction.user.roles]

    if not any(r in user_roles for r in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    try:
        view = JobRoleSelectView()
        await channel.send(
            "**# 💼 Choose Your Job Role**\n\n"
            "Pick a Job Role below to start working!\n- You can only hold one job role at a time.\n"
            "**- Quitting locks you out of joining a new one for `2` days**"" - After you select your profession, go through https://discordapp.com/channels/1367183962447024158/1525894786446393395, https://discordapp.com/channels/1367183962447024158/1525894810811240670, https://discordapp.com/channels/1367183962447024158/1525894841639370753, https://discordapp.com/channels/1367183962447024158/1525894858739290173 and see what tasks you can complete.\n"
            "- Each profession has a differently color-coded button.\n",
            view=view
        )
        await interaction.response.send_message(f"✅ Job select board posted to {channel.mention}!", ephemeral=True)
    except Exception as e:
        print(f"[JOB SELECT ERROR] {type(e).__name__}: {e}")
        await interaction.response.send_message(f"❌ Something went wrong: {e}", ephemeral=True)

@bot.tree.command(name="cpostjobboard", description="Post job buttons for a specific floor to a channel (Mod/Owner only)")
@app_commands.describe(floor="Which floor's jobs to post")
@app_commands.choices(floor=[
    app_commands.Choice(name="Ground Floor", value="ground"),
    app_commands.Choice(name="Exhibition Floor", value="exhibition"),
    app_commands.Choice(name="Specialty Floor", value="specialty"),
    app_commands.Choice(name="VIP Level", value="vip"),
])
async def cpostjobboard(interaction: discord.Interaction, floor: app_commands.Choice[str], channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [r.name for r in interaction.user.roles]

    if not any(r in user_roles for r in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    try:
        view = JobButtonView(floor.value)
        job_names = ", ".join(j.replace("_", " ").title() for j in config.FLOOR_JOBS[floor.value])
        await channel.send(
            f"**# 💼 {floor.name} Jobs**\n\n"
            f"Available jobs here: **{job_names}**\n"
            f"You must hold the correct Job Role to work any of these.\n",
            view=view
        )
        await interaction.response.send_message(f"✅ {floor.name} job board posted to {channel.mention}!", ephemeral=True)
    except Exception as e:
        print(f"[JOB BOARD ERROR] {type(e).__name__}: {e}")
        await interaction.response.send_message(f"❌ Something went wrong: {e}", ephemeral=True)

@bot.tree.command(name="ctriggerdaily", description="Manually trigger a daily reward ping for a specific user (Mod/Owner only)")
async def ctriggerdaily(interaction: discord.Interaction, user: discord.Member):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    channel_id = get_setting(f"daily_channel_id_{interaction.guild_id}", None)
    if not channel_id:
        await interaction.response.send_message("❌ No daily channel set! Use /csetdailychannel first.", ephemeral=True)
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        await interaction.response.send_message("❌ Couldn't find that channel!", ephemeral=True)
        return

    get_or_create_user(user)
    db.collection("users").document(str(user.id)).update({"daily_reminder_sent": False})

    view = View(timeout=None)
    view.add_item(DailyClaimButton(user.id))
    await channel.send(
        f"🪙 {user.mention}, your daily reward is ready! Click below to claim it.",
        view=view
    )
    await interaction.response.send_message(f"✅ Daily reward ping sent for {user.mention}!", ephemeral=True)

@bot.tree.command(name="ctestdailyping", description="TEMP: manually trigger a daily reward ping for yourself (Mod/Owner only)")
async def ctestdailyping(interaction: discord.Interaction):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    channel_id = get_setting(f"daily_channel_id_{interaction.guild_id}", None)
    if not channel_id:
        await interaction.response.send_message("❌ No daily channel set! Use /csetdailychannel first.", ephemeral=True)
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        await interaction.response.send_message("❌ Couldn't find that channel!", ephemeral=True)
        return

    get_or_create_user(interaction.user)
    mark_message_sent_today(interaction.user.id)
    db.collection("users").document(str(interaction.user.id)).update({"daily_reminder_sent": False})

    view = View(timeout=None)
    view.add_item(DailyClaimButton(interaction.user.id))
    await channel.send(
        f"🪙 {interaction.user.mention}, your daily reward is ready! Click below to claim it.",
        view=view
    )
    await interaction.response.send_message("✅ Test ping sent!", ephemeral=True)









@bot.tree.command(name="ctestleaderboardpost", description="TEMP: manually trigger all weekly leaderboard posts + payouts (Mod/Owner only)")
async def ctestleaderboardpost(interaction: discord.Interaction):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    await interaction.response.send_message("✅ Triggering test post...", ephemeral=True)
    current_week = get_current_week_start()
    activity_results = await post_weekly_leaderboard(interaction.guild, current_week)
    session_results = await post_weekly_session_leaderboard(interaction.guild, current_week)
    received_results = await post_weekly_received_leaderboard(interaction.guild, current_week)
    await assign_weekly_vip(interaction.guild, activity_results, session_results, received_results)













@bot.tree.command(name="cfixprofiles", description="Backfill any missing fields on incomplete user profiles (Mod/Owner only)")
async def cfixprofiles(interaction: discord.Interaction):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    defaults = {
        "balance": STARTING_BALANCE,
        "body_count": STARTING_BODY_COUNT,
        "status": STARTING_STATUS,
        "house": STARTING_HOUSE,
        "partners": [],
        "strikes": 0,
        "allure_boost_multiplier": None,
        "allure_boost_sessions_left": 0
    }

    docs = db.collection("users").stream()
    fixed_count = 0

    for doc in docs:
        data = doc.to_dict()
        missing = {key: value for key, value in defaults.items() if key not in data}
        if missing:
            doc.reference.set(missing, merge=True)
            fixed_count += 1

    await interaction.followup.send(f"✅ Checked all profiles. Fixed **{fixed_count}** incomplete profile(s).", ephemeral=True)

class HelpView(View):
    def __init__(self):
        super().__init__(timeout=60)
        self.page = 0
        self.pages = [
            (
                "📖 Profile & Sessions",
                "**Profile**\n"
                "🔸`/cprofile`\n-# View your profile\n"
                "🔸`/cview @user`\n-# View someone else's profile\n"
                "🔸`/cbalance`\n-# Check your current credit balance\n\n"
                "**Sessions**\n"
                "🔸`/csexbuy @user`\n-# Buy someone's services, they get paid based on their body count\n"
                "🔸`/csexsell @user`\n-# Offer your services, you get paid based on your body count\n"
                "🔸`/cend`\n-# End your session and update everyone's stats\n"
                "🔸`/cinterrupt`\n-# Leave the session without updating stats\n"
                "**Others**\n"
                "🔸`/cpsst @user [message]`\n-# Send a private whisper only they can see"
            ),
            (
                "📖 Transactions",
                "🔸`/csend @user [amount]`\n-# Send 🪙 to someone\n"
                "🔸`/crequest @user [amount]`\n-# Request 🪙 from someone"
            ),
            (
                "📖 Activity & Rewards",
                "🔸`/cleaderboard`\n-# Show the top 3 most active users this week\n"
                "🔸`/csessionleaderboard`\n-# Show the top 3 users with the most completed sessions this week\n"
                "🔸`/creceivedleaderboard`\n-# Show the top 3 users who received the most coins this week\n"
                "-# All three leaderboards also post automatically every  Friday at 11:00 AM UTC, with top 3 winners on each getting paid coins and a chance at VIP\n\n"
                "**Daily Reward**\n"
                "-# Send at least one message each day to become eligible. Once eligible, the bot will ping you in the daily reward channel with a claim button.\n"
                "-# Claiming builds a streak — the longer your streak, the bigger the reward (up to a cap)."
            ),
            (
                "📖 Jobs",
                "**How it works**\n"
                "-# Pick a Job Role on the job select board — you can only hold one at a time. Each role gives you access to a few specific jobs.\n"
                "-# Job buttons are posted in each floor's job channel. You must hold the matching Job Role to work a job button.\n"
                "-# Each job button pays out a random amount and goes on its own cooldown — the same job on different floors has separate cooldowns.\n"
                "-# Quitting your Job Role is instant and free, but locks you out of joining a new one for 2 days.\n\n"
                "**Mod/Owner Only**\n"
                "🔸`/csetjobrole [1-4] @role`\n-# Link a Discord role to one of the 4 Job Roles\n"
                "🔸`/cpostjobselect #channel`\n-# Post the job role select board\n"
                "🔸`/cpostjobboard [floor] #channel`\n-# Post job buttons for a specific floor\n"
                "🔸`/cclearjoblockout @user`\n-# Clear a user's job-quit lockout early"
            ),
            (
                "📖 Mod/Owner Only",
                "🔸`/cedit @user [field] [value]`\n-# Edit a user's profile\n"
                "🔸`/creset @user`\n-# Reset a user's stats\n"
                "🔸`/cendall`\n-# Force close all active sessions\n"
                "🔸`/csetlogchannel #channel`\n-# Set the transaction log channel\n"
                "🔸`/csetannouncechannel #channel`\n-# Set the bot status announcement channel\n"
                "🔸`/csetleaderboardchannel #channel`\n-# Set the channel for the automatic weekly leaderboards\n"
                "🔸`/csetdailychannel #channel`\n-# Set the channel for daily reward claim pings\n"
                "🔸`/csetviprole @role`\n-# Set the role granted to weekly leaderboard winners\n"
                "🔸`/cstrike @user [reason]`\n-# Add a strike to a user\n"
                "🔸`/cunstrike @user [reason]`\n-# Remove a strike from a user\n"
                "🔸`/cmodview @user`\n-# View a user's full profile including strikes\n"
                "🔸`/csetstrikelogchannel #channel`\n-# Set the strike log channel\n"
                "🔸`/csetreactionrole [message_id] [emoji] [role]`\n-# Set up a reaction role\n"
                "🔸`/cfixprofiles`\n-# Backfill any missing fields on incomplete user profiles\n"
                "🔸`/cpostperk [perk] #channel`\n-# Post a perk (Virginity Reset or Allure Boost) to a channel\n"
                "🔸`/csessionoverview`\n-# Show all ongoing sessions with members, start time, and AFK check history"
            ),
            (
                "📖 Trigger / Test Commands (Mod/Owner only)",
                "-# These manually trigger things that would normally happen automatically — useful for testing or forcing an early run.\n\n"
                "🔸`/ctriggerdaily @user`\n-# Manually send a daily reward ping to a specific user\n"
                "🔸`/ctestdailyping`\n-# Manually send yourself a daily reward ping\n"
                "🔸`/ctestleaderboardpost`\n-# Manually trigger all 3 weekly leaderboard posts, payouts, and VIP assignment"
                "🔸`/cresetjobcooldowns @user`\n-# Reset all of a user's job cooldowns, letting them work every job again, immediately"
            )
        ]

    def get_embed_content(self):
        title, content = self.pages[self.page]
        return f"**{title}** (Page {self.page + 1}/{len(self.pages)})\n\n{content}"

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.gray)
    async def previous(self, interaction: discord.Interaction, button: Button):
        self.page = (self.page - 1) % len(self.pages)
        await interaction.response.edit_message(content=self.get_embed_content(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: Button):
        self.page = (self.page + 1) % len(self.pages)
        await interaction.response.edit_message(content=self.get_embed_content(), view=self)


@bot.tree.command(name="chelp", description="Show all available commands")
async def chelp(interaction: discord.Interaction):
    view = HelpView()
    await interaction.response.send_message(view.get_embed_content(), view=view, ephemeral=True)

@bot.tree.command(name="cendall", description="Force close all active sessions (Mod/Owner only)")
async def cendall(interaction: discord.Interaction):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    if not sessions:
        await interaction.response.send_message("There are no active sessions!", ephemeral=True)
        return

    count = len(sessions)
    sessions.clear()
    active_sessions.clear()

    await interaction.response.send_message(f"✅ Force closed {count} active session(s). No stats were updated.")

class WhisperModal(discord.ui.Modal, title="Write Your Whisper"):
    message_input = discord.ui.TextInput(
        label="Your private message",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True
    )

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        view = WhisperView(interaction.user.id, self.target.id, self.message_input.value)
        await interaction.channel.send(
            f"🤫 {self.target.mention}, {interaction.user.mention} sent you a whisper — click below to read it.",
            view=view
        )
        await interaction.response.send_message("✅ Whisper sent!", ephemeral=True)

class WhisperView(View):
    def __init__(self, sender_id: int, target_id: int, message: str):
        super().__init__(timeout=None)  # stays clickable indefinitely, no expiry
        self.sender_id = sender_id
        self.target_id = target_id
        self.message = message

    @discord.ui.button(label="Reveal Whisper", style=discord.ButtonStyle.primary, emoji="🤫")
    async def reveal(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("This whisper isn't for you!", ephemeral=True)
            return

        await interaction.response.send_message(
            f"🤫 Whisper from <@{self.sender_id}>:\n\n-# {self.message}",
            ephemeral=True
        )

@bot.tree.command(name="cpsst", description="Send a private whisper to someone, visible only to them")
@app_commands.describe(user="Who to whisper to")
async def cpsst(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        await interaction.response.send_message("You can't whisper to yourself!", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You can't whisper to a bot!", ephemeral=True)
        return

    await interaction.response.send_modal(WhisperModal(user))

@bot.tree.command(name="csessionoverview", description="Show all ongoing sessions with members, start time, and AFK check history (Mod/Owner only)")
async def csessionoverview(interaction: discord.Interaction):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    if not sessions:
        await interaction.response.send_message("There are no active sessions!", ephemeral=True)
        return

    now = time.time()
    blocks = []

    for session_id, session_data in sessions.items():
        started_at = session_data.get("started_at")
        duration_str = "unknown"
        if started_at:
            elapsed = int(now - started_at)
            hours = elapsed // 3600
            minutes = (elapsed % 3600) // 60
            duration_str = f"{hours}h {minutes}m ago"

        afk_checks_passed = session_data.get("afk_checks_passed", {})
        member_lines = []
        for member_id in session_data["members"]:
            passed = afk_checks_passed.get(member_id, 0)
            member_lines.append(f"  • <@{member_id}> — {passed} successful AFK check(s)")

        block = (
            f"**Session `{session_id[:8]}`**\n"
            f"Started: {duration_str}\n"
            + "\n".join(member_lines)
        )
        blocks.append(block)

    full_text = "**🔍 Active Sessions Overview**\n\n" + "\n\n".join(blocks)

    if len(full_text) > 1900:
        await interaction.response.send_message("There are too many active sessions to display at once — consider trimming with /cendall.", ephemeral=True)
        return

    await interaction.response.send_message(full_text, ephemeral=True)

@bot.tree.command(name="csetstrikelogchannel", description="Set the channel for strike logs (Mod/Owner only)")
async def csetstrikelogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    set_setting(f"strike_log_channel_id_{interaction.guild_id}", channel.id)
    await interaction.response.send_message(f"✅ Strike log channel set to {channel.mention}!")

@bot.tree.command(name="csetreactionrole", description="Set up a reaction role on a message (Mod/Owner only)")
async def csetreactionrole(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [r.name for r in interaction.user.roles]

    if not any(r in user_roles for r in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    try:
        message = await interaction.channel.fetch_message(int(message_id))
    except Exception:
        await interaction.response.send_message("❌ Couldn't find that message in this channel!", ephemeral=True)
        return

    await message.add_reaction(emoji)
    add_reaction_role(message_id, emoji, role.id)

    await interaction.response.send_message(f"✅ Reaction role set! React with {emoji} on that message to get {role.mention}.", ephemeral=True)

class PerkBuyView(View):
    def __init__(self, perk_keys: list):
        super().__init__(timeout=None)
        for perk_key in perk_keys:
            self.add_item(self.make_button(perk_key))

    def make_button(self, perk_key):
        perk = config.PERKS[perk_key]
        button = Button(
            label=f"{perk['label']} — {perk['price']} 🪙",
            style=discord.ButtonStyle.green,
            custom_id=f"perk_buy_{perk_key}"
        )

        async def callback(interaction: discord.Interaction):
            await self.handle_purchase(interaction, perk_key)

        button.callback = callback
        return button

    async def handle_purchase(self, interaction: discord.Interaction, perk_key: str):
        perk = config.PERKS[perk_key]
        user_data = get_or_create_user(interaction.user)

        if user_data["balance"] < perk["price"]:
            await interaction.response.send_message(f"❌ You don't have enough 🪙! This perk costs {perk['price']} 🪙.", ephemeral=True)
            return

        ref = db.collection("users").document(str(interaction.user.id))

        if perk["effect"] == "virginity_reset":
            ref.update({
                "balance": user_data["balance"] - perk["price"],
                "status": STARTING_STATUS,
                "body_count": STARTING_BODY_COUNT,
                "partners": []
            })

        elif perk["effect"] == "allure_boost":
            if user_data.get("allure_boost_multiplier"):
                await interaction.response.send_message("❌ You already have an active Allure Boost! Wait for it to expire first.", ephemeral=True)
                return

            ref.update({
                "balance": user_data["balance"] - perk["price"],
                "allure_boost_multiplier": perk["multiplier"],
                "allure_boost_sessions_left": perk["sessions"]
            })

        display_label = "Virginity Perk" if perk["label"] == "Buy" else perk["label"]
        await log_transaction(f"🛍️ Perk Purchase: <@{interaction.user.id}> bought **{display_label}** for {perk['price']} 🪙", interaction.guild_id)
        await interaction.response.send_message(f"✅ You bought **{perk['label']}**! Your stats have been updated.", ephemeral=True)
        
@bot.tree.command(name="cpostperk", description="Post a perk to a channel (Mod/Owner only)")
@app_commands.describe(perk="Which perk to post", channel="Where to post it")
@app_commands.choices(perk=[
    app_commands.Choice(name="Virginity Reset", value="virginity_reset"),
    app_commands.Choice(name="Allure Boost", value="allure_boost")
])
async def cpostperk(interaction: discord.Interaction, perk: app_commands.Choice[str], channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    if perk.value == "virginity_reset":
        description = "🌸 **Virginity Perk**\n-# Reclaim your innocence. Resets your status back to virgin, your body count to 0, and clears your partner history."
        perk_keys = ["virginity_reset"]

    elif perk.value == "allure_boost":
        description = (
            "✨ **Allure Boost**\n"
            "-# Temporarily increase your session price. Only one Allure Boost can be active at a time.\n\n"
            "**Tier I** — +10% price for 3 sessions\n"
            "**Tier II** — +50% price for 5 sessions\n"
            "**Tier III** — +90% price for 2 sessions"
        )
        perk_keys = ["allure_tier1", "allure_tier2", "allure_tier3"]

    view = PerkBuyView(perk_keys)
    await channel.send(description, view=view)
    await interaction.response.send_message(f"✅ Perk posted to {channel.mention}!", ephemeral=True)

async def main():
    try:
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        pass
    finally:
        await announce_status("⛔ Currency Bot is now **offline**!")
        await bot.close()

asyncio.run(main())

bot.run(TOKEN)