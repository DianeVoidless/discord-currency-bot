# config.py
# Constant values used across the bot.
# Keeping these here (instead of scattered in bot.py) makes them easy to find and tweak.

ALLOWED_GUILD_ID = 1367183962447024158  # the only server this bot instance is allowed to operate in ("Velvet")
# ALLOWED_GUILD_ID = 1312819979384782908  # the only server this bot instance is allowed to operate in ("Yellow")

CONFIRMATION_TIMEOUT = 300  # seconds a user has to respond to any check before being timing out

CONFIRMATION_TIMEOUT = 300  # seconds a user has to respond to any check before being timing out
SESSION_CHECK_INTERVAL = 1800  # seconds of inactivity before a session gets an AFK check (1800 = 30 minutes)

DAILY_REWARD_BASE = 75  # base coins for claiming /cdaily
DAILY_STREAK_BONUS_PER_DAY = 15  # extra coins added per consecutive day claimed
DAILY_STREAK_CAP = 10  # streak bonus stops growing after this many days
DAILY_CLAIM_COOLDOWN = 24 * 60 * 60  # seconds before you can claim again (24 hours)
DAILY_STREAK_GRACE = 48 * 60 * 60  # if you wait longer than this since your last claim, streak resets (48 hours)

EXCLUDED_ACTIVITY_CHANNELS = [1372557351105597482, 1515324370434789567, 1522874551258714212, 1500861891231350814, 1367473737569402900, 1367474072648417280, 1367917151847186472, 1367221726303223887]  # channel IDs that don't count toward the activity leaderboard (e.g. bot-command or mod channels)
EXCLUDED_DAILY_USERS = [1365421859142631514, 1164970747077337088, 1516167293409689640, 155149108183695360, 159985870458322944, 431544605209788416, 302050872383242240]  # user IDs that should never get daily reward pings (e.g. bots, alt accounts, staff test accounts)

DAILY_ELIGIBILITY_CHECK_INTERVAL = 300  # seconds between scans for eligible daily-reward claimers (300 = 5 minutes)

# Weekly leaderboard winner rewards (1st, 2nd, 3rd place)
ACTIVITY_LEADERBOARD_REWARDS = [500, 300, 150]
SESSION_LEADERBOARD_REWARDS = [750, 450, 250]
RECEIVED_LEADERBOARD_REWARDS = [400, 250, 125]