import os
from pyrogram import Client, filters
from shared_state import active_batches

def is_master_admin(user_id):
    allowed_admins_str = os.getenv("ALLOWED_ADMIN_IDS", "")
    if not allowed_admins_str: return False
    try:
        master_admins = [int(x.strip()) for x in allowed_admins_str.split(",") if x.strip()]
        return user_id in master_admins
    except ValueError:
        return False

@Client.on_message(filters.command("status"))
async def batch_status(client, message):
    user_id = message.from_user.id
    if user_id not in active_batches:
        # Note: If an authorized user asks status but hasn't started a batch, we just say none active.
        return await message.reply("No batch download currently running.")
        
    b = active_batches[user_id]
    await message.reply(
        f"🔄 **Batch Status:**\n"
        f"• **Episode:** {b['current']}/{b['total']}\n"
        f"• **State:** {b['status']}"
    )

@Client.on_message(filters.command("cancel"))
async def cancel_batch(client, message):
    user_id = message.from_user.id
    if user_id not in active_batches:
        return await message.reply("No batch download currently running.")
        
    b = active_batches[user_id]
    try:
        b["task"].cancel()
    except Exception as e:
        print(f"Error cancelling task: {e}")
        
    del active_batches[user_id]
    await message.reply("🛑 Active batch download cancelled. (If an episode is currently uploading, it may finish first).")
