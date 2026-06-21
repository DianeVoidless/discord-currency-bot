import discord
from discord import app_commands
from dotenv import load_dotenv
import os
import firebase_admin
from firebase_admin import credentials, firestore
from discord.ext import commands
import uuid
from discord.ui import View, Button
import asyncio

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Firebase connection
cred = credentials.Certificate("currency-bot-19258-firebase-adminsdk-fbsvc-e9b8a6f065.json")
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
    try:
        guild1 = discord.Object(id=1367183962447024158)
        guild2 = discord.Object(id=1312819979384782908)
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

@bot.event
async def on_raw_reaction_add(payload):
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
    emoji = str(payload.emoji)
    role_data = get_reaction_role(payload.message_id, emoji)
    if not role_data:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    role = guild.get_role(role_data["role_id"])
    if member and role:
        await member.remove_roles(role)

def get_or_create_user(member):
    user_id = member.id
    ref = db.collection("users").document(str(user_id))
    doc = ref.get()
    if not doc.exists:
        ref.set({
            "username": member.name,
            "balance": STARTING_BALANCE,
            "body_count": STARTING_BODY_COUNT,
            "status": STARTING_STATUS,
            "house": STARTING_HOUSE,
            "partners": [],
            "strikes": 0,
            "allure_boost_multiplier": None,
            "allure_boost_sessions_left": 0
        })
    else:
        ref.update({"username": member.name})
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

async def end_session_for_user(user_id: int):
    if user_id not in active_sessions:
        return

    session_id = active_sessions[user_id]
    session_members = sessions[session_id]["members"].copy()

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

    sessions.pop(session_id, None)


class SessionCheckView(View):
    def __init__(self, user_id: int, session_id: str):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.session_id = session_id

    @discord.ui.button(label="Yes, still active!", style=discord.ButtonStyle.green)
    async def still_active(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()
        pending_checks.discard(self.session_id)
        await interaction.response.edit_message(content=f"✅ {interaction.user.name} confirmed they're still active!", view=None)

    @discord.ui.button(label="No, end session", style=discord.ButtonStyle.red)
    async def end_session(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        self.stop()
        pending_checks.discard(self.session_id)
        await end_session_for_user(self.user_id)
        await interaction.response.edit_message(content=f"✅ Session ended for {interaction.user.name}!", view=None)

    async def on_timeout(self):
        pending_checks.discard(self.session_id)
        await end_session_for_user(self.user_id)


async def session_check_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        interval = get_setting("session_check_interval", 1800)
        await asyncio.sleep(interval)
        print(f"Running session check... {len(sessions)} active sessions")
        for session_id, session_data in list(sessions.items()):
            if session_id in pending_checks:
                continue
            pending_checks.add(session_id)
            members = session_data["members"].copy()
            channel = bot.get_channel(session_data["channel_id"])
            if not channel:
                continue

            for member_id in members:
                member = channel.guild.get_member(member_id)
                if not member:
                    continue

                view = SessionCheckView(member_id, session_id)
                await channel.send(
                    f"⏰ {member.mention} are you still in your session?",
                    view=view
                )


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


class TransactionConfirmView(View):
    def __init__(self, sender_id: int, receiver_id: int, amount: int):
        super().__init__(timeout=30)
        self.sender_id = sender_id
        self.receiver_id = receiver_id
        self.amount = amount

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
        super().__init__(timeout=30)
        self.requester_id = requester_id
        self.target_id = target_id
        self.amount = amount

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
        super().__init__(timeout=30)
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
            "payer_id": target_id,
            "payee_id": initiator_id,
            "price": self.price
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
        super().__init__(timeout=30)
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

        session_id = str(uuid.uuid4())
        sessions[session_id] = {
            "members": {initiator_id, target_id},
            "channel_id": interaction.channel.id,
            "payer_id": target_id,
            "payee_id": initiator_id,
            "price": self.price
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

@bot.tree.command(name="csetcheckinterval", description="Set session check interval in seconds (Mod/Owner only)")
async def csetcheckinterval(interaction: discord.Interaction, seconds: int):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    set_setting("session_check_interval", seconds)
    await interaction.response.send_message(f"✅ Session check interval set to {seconds/60} minutes!")


@bot.tree.command(name="csetlogchannel", description="Set the channel for transaction logs (Mod/Owner only)")
async def csetlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    allowed_roles = ["Mod", "Owner"]
    user_roles = [role.name for role in interaction.user.roles]

    if not any(role in user_roles for role in allowed_roles):
        await interaction.response.send_message("You don't have permission to use this command!", ephemeral=True)
        return

    set_setting(f"log_channel_id_{interaction.guild_id}", channel.id)
    await interaction.response.send_message(f"✅ Transaction log channel set to {channel.mention}!")

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
                "🔸`/cinterrupt`\n-# Leave the session without updating stats"
            ),
            (
                "📖 Transactions",
                "🔸`/csend @user [amount]`\n-# Send 🪙 to someone\n"
                "🔸`/crequest @user [amount]`\n-# Request 🪙 from someone"
            ),
            (
                "📖 Mod/Owner Only",
                "🔸`/cedit @user [field] [value]`\n-# Edit a user's profile\n"
                "🔸`/creset @user`\n-# Reset a user's stats\n"
                "🔸`/cendall`\n-# Force close all active sessions\n"
                "🔸`/csetcheckinterval [seconds]`\n-# Set session check interval\n"
                "🔸`/csetlogchannel #channel`\n-# Set the transaction log channel\n"
                "🔸`/cstrike @user [reason]`\n-# Add a strike to a user\n"
                "🔸`/cunstrike @user [reason]`\n-# Remove a strike from a user\n"
                "🔸`/cmodview @user`\n-# View a user's full profile including strikes\n"
                "🔸`/csetstrikelogchannel #channel`\n-# Set the strike log channel\n"
                "🔸`/csetreactionrole [message_id] [emoji] [role]`\n-# Set up a reaction role"
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
    def __init__(self, perks: list):
        super().__init__(timeout=None)
        for perk in perks:
            self.add_item(self.make_button(perk))

    def make_button(self, perk):
        button = Button(label=f"{perk['label']} — {perk['price']} 🪙", style=discord.ButtonStyle.green)

        async def callback(interaction: discord.Interaction):
            await self.handle_purchase(interaction, perk)

        button.callback = callback
        return button

    async def handle_purchase(self, interaction: discord.Interaction, perk):
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

        await log_transaction(f"🛍️ Perk Purchase: <@{interaction.user.id}> bought **{perk['label']}** for {perk['price']} 🪙", interaction.guild_id)
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
        perks = [
            {"label": "Buy", "price": 10000, "effect": "virginity_reset"}
        ]

    elif perk.value == "allure_boost":
        description = (
            "✨ **Allure Boost**\n"
            "-# Temporarily increase your session price. Only one Allure Boost can be active at a time.\n\n"
            "**Tier I** — +10% price for 3 sessions\n"
            "**Tier II** — +50% price for 5 sessions\n"
            "**Tier III** — +90% price for 2 sessions"
        )
        perks = [
            {"label": "Tier I", "price": 4000, "effect": "allure_boost", "multiplier": 0.1, "sessions": 3},
            {"label": "Tier II", "price": 5000, "effect": "allure_boost", "multiplier": 0.5, "sessions": 5},
            {"label": "Tier III", "price": 6000, "effect": "allure_boost", "multiplier": 0.9, "sessions": 2}
        ]

    view = PerkBuyView(perks)
    await channel.send(description, view=view)
    await interaction.response.send_message(f"✅ Perk posted to {channel.mention}!", ephemeral=True)

bot.run(TOKEN)