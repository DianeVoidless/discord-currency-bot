# config.py
# Constant values used across the bot.
# Keeping these here (instead of scattered in bot.py) makes them easy to find and tweak.

CONFIRMATION_TIMEOUT = 5  # seconds a user has to respond to an AFK check before being removed
SESSION_CHECK_INTERVAL = 10  # seconds of inactivity before a session gets an AFK check (1800 = 30 minutes)