# Global state storage for the bot
# Format: { chat_id: { "url": str, "res": int or None, "langs": list, "ep_data": dict, ... } }
user_sessions = {}

# Active batch download queues
# Format: { chat_id: { "task": asyncio.Task, "current": int, "total": int, "status": str } }
active_batches = {}
