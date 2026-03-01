import os
import json
from pyrogram import Client, filters

AUTH_FILE = "authorized_users.json"

def load_auth_users():
    if not os.path.exists(AUTH_FILE):
        return []
    try:
        with open(AUTH_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_auth_users(users):
    with open(AUTH_FILE, "w") as f:
        json.dump(users, f)

# Helper function to check if a user is the master admin (defined in .env)
def is_master_admin(user_id):
    allowed_admins_str = os.getenv("ALLOWED_ADMIN_IDS", "")
    if not allowed_admins_str:
        return False
    try:
        master_admins = [int(x.strip()) for x in allowed_admins_str.split(",") if x.strip()]
        return user_id in master_admins
    except ValueError:
        return False

@Client.on_message(filters.command("auth"))
async def auth_user(client, message):
    if not is_master_admin(message.from_user.id):
        return await message.reply("⛔ You must be a Master Admin to use this command.")

    if len(message.command) < 2:
        return await message.reply("Usage: /auth <user_id>")

    try:
        new_user = int(message.command[1])
    except ValueError:
        return await message.reply("Invalid user ID. It must be a number.")

    users = load_auth_users()
    if new_user in users:
        return await message.reply(f"User `{new_user}` is already authorized.")

    users.append(new_user)
    save_auth_users(users)
    await message.reply(f"✅ User `{new_user}` has been authorized to use the bot.")


@Client.on_message(filters.command("del"))
async def del_user(client, message):
    if not is_master_admin(message.from_user.id):
        return await message.reply("⛔ You must be a Master Admin to use this command.")

    if len(message.command) < 2:
        return await message.reply("Usage: /del <user_id>")

    try:
        del_user = int(message.command[1])
    except ValueError:
        return await message.reply("Invalid user ID. It must be a number.")

    users = load_auth_users()
    if del_user not in users:
        return await message.reply(f"User `{del_user}` is not in the authorized list.")

    users.remove(del_user)
    save_auth_users(users)
    await message.reply(f"❌ User `{del_user}` has been removed from authorization.")
