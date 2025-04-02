import os
import json
import re
import time
import asyncio
from PIL import Image
from urllib.parse import quote

# Telethon / Telegram
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeAnimated
from telethon.utils import get_peer_id
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.functions.messages import ExportChatInviteRequest

# Web3 for BSC on-chain logic
from web3 import Web3
from eth_account import Account

# OpenAI (translation)
from openai import OpenAI

# =============================================================================
#                      CRITICAL BLOCKCHAIN CONSTANTS
# =============================================================================
BSC_RPC = "https://bsc-dataseed1.binance.org/"
web3 = Web3(Web3.HTTPProvider(BSC_RPC))

USDC_CONTRACT_ADDRESS = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"
USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]
usdc_contract = web3.eth.contract(address=USDC_CONTRACT_ADDRESS, abi=USDC_ABI)

MAIN_WALLET_ADDRESS = "0x6370345bA940b087828dC5db428959fa01A6C4d3"
MAIN_WALLET_PRIVATE_KEY = "184e2a03b0fc26d9b9871c556eb04b1dcab8f6eeba27f407e54c1a2cd61e2744" #Don't laste time :D

GAS_PRICE_GWEI = 5
GAS_LIMIT = 200000

PRICE_1_MONTH = 150
PRICE_1_YEAR  = 1500

try:
    token_decimals = usdc_contract.functions.decimals().call()
except:
    token_decimals = 18

def to_smallest_unit(amount):
    return int(amount * (10 ** token_decimals))

# =============================================================================
#                              TELEGRAM + BOT
# =============================================================================
OPENAI_API_KEY = ""
openai_client = OpenAI(api_key=OPENAI_API_KEY)

api_id = 12345678
api_hash = ""
phone = "+"
BOT_TOKEN = ""
BOT_USERNAME = ""
ALLOWED_USERS = {
}

def user_is_authorized(user_id):
    return user_id in ALLOWED_USERS

SESSION_FORWARDER = "./sessionfiles/session_forwarder.session"
SESSION_BOT = "./sessionfiles/session_menu_bot.session"

SOURCE_GROUPS_FILE = "./groupfiles/source_groups.json"
OFFENSIVE_WORDS_FILE = "./groupfiles/offensive_words.json"
MAPPINGS_FILE = "./groupfiles/message_mappings.json"
BOT_SETTINGS_FILE = "./groupfiles/bot_settings.json"

DEFAULT_SOURCE_GROUPS = {
    "set_1": [],
    "set_2": [],
    "set_3": [],
    "test": []
}
DEFAULT_BOT_SETTINGS = {
    "translation_enabled": False
}

# Destination groups for forwarding
destination_group_chat_id_1 = -
destination_group_chat_id_2 = -
destination_group_chat_id_3 = -
test_destination_group_chat_id = -

client_telegram = TelegramClient(SESSION_FORWARDER, api_id, api_hash)
bot = TelegramClient(SESSION_BOT, api_id, api_hash)

# =============================================================================
#       REFERRAL DATA + Payment Tracking
# =============================================================================
REFERRAL_DATA_FILE = "referral_data.json"

def load_referral_data():
    if not os.path.exists(REFERRAL_DATA_FILE):
        with open(REFERRAL_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": {}}, f, indent=4)
    try:
        with open(REFERRAL_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"users": {}}

def save_referral_data(data):
    with open(REFERRAL_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

referral_data = load_referral_data()

def get_user_data(user_id):
    """
    Ensures user data entry is created if missing. 
    Also sets joined_at, purchase_history if not present.
    """
    uid_str = str(user_id)
    if "users" not in referral_data:
        referral_data["users"] = {}
    if uid_str not in referral_data["users"]:
        now_ts = int(time.time())
        referral_data["users"][uid_str] = {
            "balance": 0,
            "referrals": 0,
            "earned": 0,
            "referred_by": None,
            "purchases_count": 0,
            "non_purchases_count": 0,
            "purchases_redeemed": 0,
            "free_referrals_redeemed": 0,
            "group_access_until": 0,
            "deposit_address": None,
            "deposit_privkey": None,
            "deposit_deadline": 0,
            "deposit_amount_owed": 0,
            "has_paid": False,
            "payment_message_id": None,
            "payment_chat_id": None,
            "joined_at": now_ts,        # track when user first started
            "purchase_history": []      # track each paid subscription
        }
    # If missing these fields for older users, add them
    if "joined_at" not in referral_data["users"][uid_str]:
        referral_data["users"][uid_str]["joined_at"] = int(time.time())
    if "purchase_history" not in referral_data["users"][uid_str]:
        referral_data["users"][uid_str]["purchase_history"] = []
    return referral_data["users"][uid_str]

# =============================================================================
#   UTILS: Generating a deposit address
# =============================================================================
def generate_new_address_for_user():
    acct = Account.create()
    priv_key = acct.key.hex()
    address = acct.address
    return address, priv_key

def store_deposit_info(user_id, amount_usdc):
    ud = get_user_data(user_id)
    address, privkey = generate_new_address_for_user()
    ud["deposit_address"] = address
    ud["deposit_privkey"] = privkey
    ud["deposit_deadline"] = int(time.time()) + 3600
    ud["deposit_amount_owed"] = amount_usdc
    ud["has_paid"] = False
    ud["payment_message_id"] = None
    ud["payment_chat_id"] = None
    save_referral_data(referral_data)
    return address

# =============================================================================
#   BLOCKCHAIN OPERATIONS
# =============================================================================
def get_usdc_balance(addr):
    return usdc_contract.functions.balanceOf(addr).call()

def send_bnb(from_privkey, to_addr, amount_bnb):
    acct = Account.from_key(from_privkey)
    from_addr = acct.address
    nonce = web3.eth.get_transaction_count(from_addr)
    tx = {
        'nonce': nonce,
        'to': to_addr,
        'value': web3.to_wei(amount_bnb, 'ether'),
        'gas': 21000,
        'gasPrice': web3.to_wei(GAS_PRICE_GWEI, 'gwei'),
        'chainId': 56
    }
    signed = web3.eth.account.sign_transaction(tx, private_key=from_privkey)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)

    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt.status == 1

def transfer_usdc(from_privkey, to_addr, amount_smallest):
    acct = Account.from_key(from_privkey)
    from_addr = acct.address
    nonce = web3.eth.get_transaction_count(from_addr)
    tx = usdc_contract.functions.transfer(to_addr, amount_smallest).build_transaction({
        'chainId': 56,
        'gas': GAS_LIMIT,
        'gasPrice': web3.to_wei(GAS_PRICE_GWEI, 'gwei'),
        'nonce': nonce
    })
    signed = web3.eth.account.sign_transaction(tx, private_key=from_privkey)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)

    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt.status == 1

def sweep_bnb_leftover(from_privkey, to_addr):
    acct = Account.from_key(from_privkey)
    from_addr = acct.address
    balance = web3.eth.get_balance(from_addr)
    if balance <= 0:
        return

    gas_price = web3.to_wei(GAS_PRICE_GWEI, 'gwei')
    gas_cost = 21000 * gas_price
    if balance <= gas_cost:
        return

    amount_to_send = balance - gas_cost
    nonce = web3.eth.get_transaction_count(from_addr)
    tx = {
        'nonce': nonce,
        'to': to_addr,
        'value': amount_to_send,
        'gas': 21000,
        'gasPrice': gas_price,
        'chainId': 56
    }
    signed = web3.eth.account.sign_transaction(tx, private_key=from_privkey)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)

    receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    if receipt.status == 1:
        print(f"[SweepBNB] Swept leftover ~{web3.from_wei(amount_to_send, 'ether')} BNB from {from_addr} to {to_addr}.")

# =============================================================================
#   WATCHER TASK: Check deposit addresses
# =============================================================================
VIP_GROUP_CHAT_ID = -1002499818049

async def watch_deposits():
    while True:
        now = int(time.time())
        changed = False
        for uid_str, user_obj in referral_data["users"].items():
            if not user_obj.get("deposit_address"):
                continue
            if user_obj.get("has_paid"):
                continue

            deposit_deadline = user_obj.get("deposit_deadline", 0)
            if deposit_deadline == 0:
                continue

            deposit_addr = user_obj["deposit_address"]
            deposit_key  = user_obj["deposit_privkey"]
            amount_owed  = user_obj["deposit_amount_owed"]
            if now > deposit_deadline:
                print(f"[Watcher] Deposit for user {uid_str} expired. Removing deposit info.")
                user_obj["deposit_address"] = None
                user_obj["deposit_privkey"] = None
                user_obj["deposit_deadline"] = 0
                user_obj["deposit_amount_owed"] = 0
                user_obj["payment_message_id"] = None
                user_obj["payment_chat_id"] = None
                changed = True
                continue

            balance_smallest = get_usdc_balance(deposit_addr)
            required_smallest = to_smallest_unit(amount_owed)
            if balance_smallest >= required_smallest:
                print(f"[Watcher] Payment detected for user {uid_str} on address {deposit_addr}")

                # Mark user as paid (record purchase history if needed)
                user_obj["has_paid"] = True
                # (Do NOT increment user_obj["purchases_count"] here)

                # Credit the referrer (if any)
                referrer_id = user_obj.get("referred_by")
                if referrer_id:
                    ref_data = get_user_data(referrer_id)
                    ref_data["purchases_count"] += 1
                    save_referral_data(referral_data)

                deposit_balance_bnb = web3.eth.get_balance(deposit_addr)
                if deposit_balance_bnb < web3.to_wei(0.0005, 'ether'):
                    print(f"[Watcher] Topping up deposit address for user {uid_str} with 0.001 BNB gas.")
                    send_bnb(MAIN_WALLET_PRIVATE_KEY, deposit_addr, 0.001)

                success = transfer_usdc(deposit_key, MAIN_WALLET_ADDRESS, balance_smallest)
                if success:
                    print(f"[Watcher] USDC swept to main wallet from user {uid_str}.")
                    await asyncio.sleep(3)
                    sweep_bnb_leftover(deposit_key, MAIN_WALLET_ADDRESS)
                else:
                    print(f"[Watcher] Sweep USDC failed for user {uid_str} address {deposit_addr}.")

                # Decide membership length
                if amount_owed == PRICE_1_MONTH:
                    extension_days = 30
                else:
                    extension_days = 365

                old_expiry = user_obj["group_access_until"]
                if old_expiry < now:
                    old_expiry = now
                new_expiry = old_expiry + (extension_days * 86400)
                user_obj["group_access_until"] = new_expiry

                # Generate a unique invite link for the VIP group
                try:
                    invite_result = await client_telegram(ExportChatInviteRequest(
                        peer=VIP_GROUP_CHAT_ID,
                        expire_date=int(time.time()) + (extension_days * 86400),
                        usage_limit=1
                    ))
                    vip_invite_link = invite_result.link

                    # Send the invite link to the user via the bot
                    await bot.send_message(
                        entity=int(uid_str),
                        message=(
                            f"✅ **Payment Confirmed**\n\n"
                            f"Received {amount_owed} USDC. "
                            f"Your VIP access is valid until {time.ctime(new_expiry)}.\n"
                            f"Here is your unique link to join VIP: [Join Now]({vip_invite_link})\n\n"
                            f"⚠ *Link expires in {extension_days} days, single-use only!*"
                        ),
                        parse_mode='md'
                    )
                    print(f"[Watcher] Sent unique VIP invite link to user {uid_str}")
                except Exception as e:
                    print(f"Could not generate/send VIP invite link to user {uid_str}: {e}")

                # Edit the original payment message
                payment_msg_id = user_obj.get("payment_message_id")
                payment_chat_id = user_obj.get("payment_chat_id")
                if payment_msg_id and payment_chat_id:
                    try:
                        await bot.edit_message(
                            entity=payment_chat_id,
                            message=payment_msg_id,
                            text=(
                                f"✅ **Payment Confirmed**\n\n"
                                f"Received {amount_owed} USDC. "
                                f"Your VIP access is valid until {time.ctime(new_expiry)}.\n"
                                f"Thank you!"
                            )
                        )
                    except Exception as e:
                        print(f"[Watcher] Could not edit payment msg for user {uid_str}: {e}")
                else:
                    print(f"[Watcher] No payment_message_id/chat_id for user {uid_str}; cannot edit deposit message.")

                # Clear deposit info
                user_obj["deposit_address"] = None
                user_obj["deposit_privkey"] = None
                user_obj["deposit_deadline"] = 0
                user_obj["deposit_amount_owed"] = 0
                user_obj["payment_message_id"] = None
                user_obj["payment_chat_id"] = None

                changed = True

        if changed:
            save_referral_data(referral_data)
        await asyncio.sleep(30)

# =============================================================================
#   BACKGROUND TASK: REMOVE EXPIRED GROUP ACCESS
# =============================================================================
CHECK_GROUP_ACCESS_INTERVAL = 600

FREE_GROUP_CHAT_ID = -1002320333381

async def remove_expired_access():
    while True:
        now = int(time.time())
        updated = False
        for uid_str, user_obj in referral_data.get("users", {}).items():
            ga_until = user_obj.get("group_access_until", 0)
            if ga_until > 0 and ga_until < now:
                real_uid = int(uid_str)
                print(f"[GroupAccess] Removing expired user {real_uid} from free groupVIP group {VIP_GROUP_CHAT_ID}")
                # Remove from VIP
                try:
                    await client_telegram.edit_permissions(
                        VIP_GROUP_CHAT_ID,
                        real_uid,
                        view_messages=False
                    )
                except Exception as e:
                    print(f"Could not remove user {real_uid} from VIP: {e}")

                user_obj["group_access_until"] = 0
                updated = True

        if updated:
            save_referral_data(referral_data)
        await asyncio.sleep(CHECK_GROUP_ACCESS_INTERVAL)

# =============================================================================
#   LOAD/SAVE MAPPINGS, OFFENSIVE_WORDS, BOT SETTINGS
# =============================================================================
def load_source_groups(file_path):
    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SOURCE_GROUPS, f, indent=4)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_SOURCE_GROUPS

def save_source_groups(file_path, data):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def load_offensive_words(file_path):
    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump([], f, indent=4)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_offensive_words(file_path, words_set):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(sorted(words_set), f, indent=4)

def load_bot_settings(file_path):
    if not os.path.exists(file_path):
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_BOT_SETTINGS, f, indent=4)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_BOT_SETTINGS

def save_bot_settings(file_path, settings_dict):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(settings_dict, f, indent=4)

source_groups = load_source_groups(SOURCE_GROUPS_FILE)
OFFENSIVE_WORDS = load_offensive_words(OFFENSIVE_WORDS_FILE)
bot_settings = load_bot_settings(BOT_SETTINGS_FILE)

processed_messages = set()
message_mappings = {}
reply_mappings = {}
try:
    with open(MAPPINGS_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)
        message_mappings = data.get("message_mappings", {})
        reply_mappings = data.get("reply_mappings", {})
except (FileNotFoundError, json.JSONDecodeError):
    pass

async def save_mappings():
    with open(MAPPINGS_FILE, "w", encoding="utf-8") as file:
        json.dump({
            "message_mappings": message_mappings,
            "reply_mappings": reply_mappings
        }, file, ensure_ascii=False)

# =============================================================================
#   FORWARDING & TRANSLATION
# =============================================================================
def contains_offensive_words(message, offensive_words):
    if not message:
        return False
    message_lower = message.lower()
    for word in offensive_words:
        if re.search(rf"\b{re.escape(word.lower())}\b", message_lower):
            return True
    return False

async def translate_message(message):
    if not bot_settings.get("translation_enabled", True):
        return None
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "Translate the following English sentence into Bulgarian."
                },
                {"role": "user", "content": message}
            ],
            temperature=0.7,
            max_tokens=256,
            top_p=1
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Translation Error: {e}")
        return None

async def forward_message(event, destination_chat_id, source_chat_id):
    global processed_messages

    # Use a tuple (chat_id, message_id) as the unique identifier.
    msg_key = (event.chat_id, event.id)
    if msg_key in processed_messages:
        return
    processed_messages.add(msg_key)

    msg_text = event.message.message or ""
    if contains_offensive_words(msg_text, OFFENSIVE_WORDS):
        try:
            await event.reply("Your message contains forbidden words and will not be forwarded.")
        except:
            pass
        return

    reply_to_msg_id = None
    if event.is_reply:
        reply_to = await event.get_reply_message()
        if reply_to:
            srep_id = reply_to.id
            map_data = message_mappings.get(str(source_chat_id), {}).get(str(srep_id), {})
            reply_to_msg_id = map_data.get(str(destination_chat_id))

    # Handling media messages
    if (
        event.photo or 
        (event.document and any(isinstance(attr, DocumentAttributeAnimated) for attr in event.document.attributes))
    ):
        file_path = await event.download_media()
        caption = event.message.message or None

        file_to_send = file_path
        try:
            ext = os.path.splitext(file_path)[1].lower()
            if ext in [".jpg", ".jpeg", ".png"]:
                with Image.open(file_path) as img:
                    w, h = img.size
                    crop_h = int(h * 0.25)
                    new_h = h - crop_h
                    cropped_img = img.crop((0, 0, w, new_h))
                    cropped_file_path = file_path.replace(ext, f"_cropped{ext}")
                    cropped_img.save(cropped_file_path)
                    file_to_send = cropped_file_path
        except Exception as e:
            print("Error processing image:", e)

        sent = await client_telegram.send_file(destination_chat_id, file_to_send, caption=caption, reply_to=reply_to_msg_id)
        if str(source_chat_id) not in message_mappings:
            message_mappings[str(source_chat_id)] = {}
        message_mappings[str(source_chat_id)][str(event.id)] = {str(destination_chat_id): sent.id}
        await save_mappings()

        if caption:
            translated = await translate_message(caption)
            if translated:
                await client_telegram.send_message(destination_chat_id, translated, reply_to=sent.id)

        if os.path.exists(file_path):
            os.remove(file_path)
    else:
        sent = await client_telegram.send_message(destination_chat_id, msg_text, reply_to=reply_to_msg_id)
        if str(source_chat_id) not in message_mappings:
            message_mappings[str(source_chat_id)] = {}
        message_mappings[str(source_chat_id)][str(event.id)] = {str(destination_chat_id): sent.id}
        if reply_to_msg_id:
            if str(source_chat_id) not in reply_mappings:
                reply_mappings[str(source_chat_id)] = {}
            reply_mappings[str(source_chat_id)][str(event.id)] = {str(destination_chat_id): reply_to_msg_id}
        await save_mappings()

        translated = await translate_message(msg_text)
        if translated:
            await client_telegram.send_message(destination_chat_id, translated, reply_to=sent.id)
# =============================================================================
#  HANDLERS
# =============================================================================
handler_set_1_ref = None
handler_set_2_ref = None
handler_set_3_ref = None
handler_test_ref = None

def register_handlers():
    global handler_set_1_ref, handler_set_2_ref, handler_set_3_ref, handler_test_ref

    if handler_set_1_ref:
        client_telegram.remove_event_handler(handler_set_1_ref)
    if handler_set_2_ref:
        client_telegram.remove_event_handler(handler_set_2_ref)
    if handler_set_3_ref:
        client_telegram.remove_event_handler(handler_set_3_ref)
    if handler_test_ref:
        client_telegram.remove_event_handler(handler_test_ref)

    @client_telegram.on(events.NewMessage(chats=source_groups["set_1"]))
    async def handler_set_1(event):
        await forward_message(event, destination_group_chat_id_1, event.chat_id)

    @client_telegram.on(events.NewMessage(chats=source_groups["set_2"]))
    async def handler_set_2(event):
        await forward_message(event, destination_group_chat_id_2, event.chat_id)

    @client_telegram.on(events.NewMessage(chats=source_groups["set_3"]))
    async def handler_set_3(event):
        await forward_message(event, destination_group_chat_id_3, event.chat_id)

    @client_telegram.on(events.NewMessage(chats=source_groups["test"]))
    async def handler_test(event):
        await forward_message(event, test_destination_group_chat_id, event.chat_id)

    handler_set_1_ref = handler_set_1
    handler_set_2_ref = handler_set_2
    handler_set_3_ref = handler_set_3
    handler_test_ref = handler_test

    print("[register_handlers] Updated forwarding handlers.")

register_handlers()

# =============================================================================
#   MENUS ETC.
# =============================================================================
SET_DISPLAY_NAMES = {
    "set_1": "Crypto Lion VIP",
    "set_2": "Crypto Lion",
    "set_3": "Crypto Lion VIP 1",
    "test":  "Тест"
}

def main_menu_text():
    txt = "📢 **Меню за настройка (ADMIN)** 📢\n\n"
    txt += f"**Превод**: {'ВКЛ.' if bot_settings.get('translation_enabled', True) else 'ИЗКЛ.'}\n"
    txt += f"**Забранени думи**: {len(OFFENSIVE_WORDS)} броя.\n\n"
    txt += "Изберете бутон отдолу:\n\n"
    txt += "- Редактиране на изходните групи\n"
    txt += "- Включване/Изключване на превод\n"
    txt += "- Управление на забранени думи\n"
    txt += "- Статистики\n"
    return txt

def main_menu_buttons():
    return [
        [Button.inline("🔧 Редактиране на групи", b"edit_groups")],
        [Button.inline("🔀 Превключване на превода", b"toggle_translation")],
        [Button.inline("⚠️ Забранени думи", b"manage_offensive")],
        [Button.inline("📊 Статистики", b"admin_stats")],
    ]

def group_menu_text(dialog_map):
    lines = []
    for key in ["set_1", "set_2", "set_3", "test"]:
        display_name = SET_DISPLAY_NAMES[key]
        group_ids = source_groups[key]
        if not group_ids:
            lines.append(f"**{display_name}**: (Няма зададени източници)")
        else:
            names_list = []
            for gid_or_user in group_ids:
                if isinstance(gid_or_user, int):
                    name = dialog_map.get(gid_or_user, f"ID: {gid_or_user}")
                else:
                    name = f"@{gid_or_user}"
                names_list.append(name)
            lines.append(f"**{display_name}**: {', '.join(names_list)}")
    lines.append("")
    lines.append("Изберете кой сет искате да редактирате:")
    return "\n".join(lines)

def group_menu_buttons():
    return [
        [Button.inline("Crypto Lion VIP", b"edit_set_1")],
        [Button.inline("Crypto Lion", b"edit_set_2")],
        [Button.inline("Crypto Lion VIP 1", b"edit_set_3")],
        [Button.inline("Тест", b"edit_test")],
        [Button.inline("🔙 Назад", b"back_main")]
    ]

def set_menu_text(set_name, dialog_map):
    display_name = SET_DISPLAY_NAMES[set_name]
    groups = source_groups[set_name]
    if not groups:
        return (
            f"🔄 **Редактиране: {display_name}** 🔄\n"
            "Няма групи в този списък.\n\n"
            "Натиснете „Добавяне“, за да зададете нова група."
        )
    lines = [f"🔄 **Редактиране: {display_name}** 🔄", "Текущи източници:"]
    for i, gid_or_user in enumerate(groups, start=1):
        if isinstance(gid_or_user, int):
            display_val = dialog_map.get(gid_or_user, f"ID: {gid_or_user}")
        else:
            display_val = f"@{gid_or_user}"
        lines.append(f"{i}) {display_val}")
    lines.append("\nНатиснете бутони за промяна/премахване или добавете нова група.")
    return "\n".join(lines)

def set_menu_buttons(set_name):
    btns = []
    groups = source_groups[set_name]
    if not groups:
        data = f"choose_src|{set_name}|0"
        btns.append([Button.inline("➕ Добавяне на първа група", data.encode("utf-8"))])
    else:
        for i, _ in enumerate(groups):
            data_change = f"choose_src|{set_name}|{i}"
            data_remove = f"remove_src|{set_name}|{i}"
            label_change = f"Промяна #{i+1}"
            label_remove = f"Премахване #{i+1}"
            btns.append([
                Button.inline(label_change, data=data_change.encode("utf-8")),
                Button.inline(label_remove, data=data_remove.encode("utf-8"))
            ])
        new_index = len(groups)
        data_append = f"choose_src|{set_name}|{new_index}"
        btns.append([Button.inline("➕ Добавяне на нова група", data_append.encode("utf-8"))])
    btns.append([Button.inline("🔙 Назад", f"edit_groups".encode("utf-8"))])
    return btns

def referral_menu_text(user_id):
    ud = get_user_data(user_id)
    balance = ud.get("balance", 0)
    refs = ud.get("referrals", 0)
    earned = ud.get("earned", 0)
    return (
        f"**Welcome to Crypto Lion Referral Bot!**\n\n"
        f"**Your Balance**: ${balance}\n"
        f"**Your Referrals**: {refs}\n"
        f"**Earned from Referrals**: ${earned}\n\n"
        "Please select an option below:"
    )

def referral_menu_buttons():
    return [
        [Button.inline("Refer", b"ref_refer")],
        [Button.inline("Buy", b"ref_buy")],
        [Button.inline("Redeem", b"ref_redeem")]
    ]

def buy_menu_text():
    return (
        "**Choose a subscription:**\n\n"
        "1) Crypto Lion VIP (1 Month) - $150\n"
        "2) Crypto Lion VIP (1 Year) - $1500\n"
        "3) Crypto Lion Free (Join group now)\n\n"
        "Select an option below:"
    )

def buy_menu_buttons():
    return [
        [Button.inline("VIP 1 Month - $150", b"subscribe_1_month")],
        [Button.inline("VIP 1 Year - $1500", b"subscribe_1_year")],
        [Button.inline("Crypto Lion Free Group", b"subscribe_free")],
        [Button.inline("<< Back", b"referral_back")]
    ]

pending_offensive_word_add = {}

@bot.on(events.NewMessage(pattern=r'^/start(?:\s+(.*))?'))
async def on_start_command(event):
    user_id = event.sender_id
    input_str = event.pattern_match.group(1)
    referred_by_id = None
    if input_str and input_str.startswith("ref_"):
        try:
            referred_by_id = int(input_str.replace("ref_", "").strip())
        except ValueError:
            referred_by_id = None

    ud = get_user_data(user_id)
    if referred_by_id and ud["referred_by"] is None and referred_by_id != user_id:
        ud["referred_by"] = referred_by_id
        ref_data = get_user_data(referred_by_id)
        ref_data["referrals"] += 1
        save_referral_data(referral_data)

    if user_is_authorized(user_id):
        txt = main_menu_text()
        btns = main_menu_buttons()
        await event.respond(txt, buttons=btns)
    else:
        txt = referral_menu_text(user_id)
        btns = referral_menu_buttons()
        await event.respond(txt, buttons=btns)

@bot.on(events.NewMessage)
async def on_any_message(event):
    user_id = event.sender_id
    if user_id in pending_offensive_word_add and user_is_authorized(user_id):
        new_word = event.raw_text.strip().lower()
        if new_word in ("/cancel", "cancel"):
            del pending_offensive_word_add[user_id]
            await event.respond("Canceled adding new forbidden word.")
            return
        OFFENSIVE_WORDS.add(new_word)
        save_offensive_words(OFFENSIVE_WORDS_FILE, OFFENSIVE_WORDS)
        del pending_offensive_word_add[user_id]
        await event.respond(f"✅ Added forbidden word: **{new_word}**")

@bot.on(events.CallbackQuery)
async def on_callback_query(event):
    user_id = event.sender_id
    data = event.data.decode("utf-8")

    if user_is_authorized(user_id):
        await handle_admin_callback_query(event, data)
    else:
        await handle_referral_callback_query(event, data)

# REFERRAL MENU
async def handle_referral_callback_query(event, data):
    user_id = event.sender_id
    ud = get_user_data(user_id)

    if data == "ref_refer":
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        link_encoded = quote(link, safe='')
        share_text = quote("Hey, join me via this referral link:")
        share_url = f"https://t.me/share/url?url={link_encoded}&text={share_text}"

        await event.edit(
            f"**Your Referral Link**:\n{link}\n\nShare it!",
            buttons=[
                [Button.url("Send to Telegram", share_url)],
                [Button.inline("<< Back", b"referral_back")]
            ]
        )

    elif data == "ref_buy":
        await event.edit(buy_menu_text(), buttons=buy_menu_buttons())

    elif data == "ref_redeem":
        purchased_total = ud["purchases_count"]
        purchased_redeemed = ud["purchases_redeemed"]
        unclaimed_purchases = purchased_total - purchased_redeemed
        if unclaimed_purchases < 0:
            unclaimed_purchases = 0
        total_refs = ud["referrals"]
        non_purchased = total_refs - purchased_total
        if non_purchased < 0:
            non_purchased = 0
        free_batches_total = non_purchased // 10
        free_batches_redeemed = ud["free_referrals_redeemed"]
        unclaimed_batches = free_batches_total - free_batches_redeemed
        if unclaimed_batches < 0:
            unclaimed_batches = 0

        msg = (
            f"**Redeem Menu**\n\n"
            f"**Total Referrals**: {total_refs}\n"
            f"**Purchased**: {purchased_total}\n"
            f"**Non-Purchasing**: {non_purchased}\n\n"
            f"**Unclaimed $**: {unclaimed_purchases * 25} $\n"
            f"**Unclaimed 1-Week Access**: {unclaimed_batches}\n\n"
            "Tap **Claim Rewards** to claim now."
        )
        await event.edit(
            msg,
            buttons=[
                [Button.inline("Claim Rewards", b"claim_rewards")],
                [Button.inline("<< Back", b"referral_back")]
            ]
        )

    elif data == "claim_rewards":
        purchased_total = ud["purchases_count"]
        purchased_redeemed = ud["purchases_redeemed"]
        unclaimed_purchases = purchased_total - purchased_redeemed
        if unclaimed_purchases < 0:
            unclaimed_purchases = 0
        usdc_reward = unclaimed_purchases * 25

        total_refs = ud["referrals"]
        non_purchased = total_refs - purchased_total
        if non_purchased < 0:
            non_purchased = 0
        free_batches_total = non_purchased // 10
        free_batches_redeemed = ud["free_referrals_redeemed"]
        unclaimed_batches = free_batches_total - free_batches_redeemed
        if unclaimed_batches < 0:
            unclaimed_batches = 0
        weeks_to_add = unclaimed_batches

        ud["purchases_redeemed"] += unclaimed_purchases
        ud["free_referrals_redeemed"] += unclaimed_batches
        if usdc_reward > 0:
            ud["balance"] += usdc_reward
            ud["earned"] += usdc_reward

        if weeks_to_add > 0:
            nowt = int(time.time())
            expiry = ud["group_access_until"]
            if expiry < nowt:
                expiry = nowt
            expiry += weeks_to_add * 7 * 86400
            ud["group_access_until"] = expiry
            try:
                await client_telegram.edit_permissions(
                    FREE_GROUP_CHAT_ID,
                    user_id,
                    send_messages=True,
                    view_messages=True
                )
            except Exception as e:
                print(f"Could not invite user {user_id} to group: {e}")

        save_referral_data(referral_data)
        notes = []
        if usdc_reward > 0:
            notes.append(f"+${usdc_reward} to balance")
        if weeks_to_add > 0:
            notes.append(f"+{weeks_to_add} weeks group access")
        if not notes:
            notes_str = "No new rewards."
        else:
            notes_str = ", ".join(notes)
        await event.edit(
            f"**Rewards Claimed**:\n{notes_str}\n\n"
            f"**New balance**: ${ud['balance']}",
            buttons=[[Button.inline("<< Back", b"referral_back")]]
        )

    elif data == "referral_back":
        txt = referral_menu_text(user_id)
        btns = referral_menu_buttons()
        await event.edit(txt, buttons=btns)

    elif data.startswith("subscribe_"):
        if data == "subscribe_1_month":
            address = store_deposit_info(user_id, PRICE_1_MONTH)
            await event.answer("1 Month purchase initiated! You have 1 hour to pay.", alert=True)

            msg_text = (
                f"**VIP 1 Month**\n\n"
                f"Please send **{PRICE_1_MONTH} USDC** to:\n"
                f"`{address}`\n\n"
                "Once we detect payment on-chain, you'll get 1 month VIP."
            )
            sent = await event.respond(
                msg_text,
                buttons=[[Button.inline("<< Back", b"referral_back")]]
            )
            ud["payment_message_id"] = sent.id
            ud["payment_chat_id"] = event.chat_id
            save_referral_data(referral_data)

        elif data == "subscribe_1_year":
            address = store_deposit_info(user_id, PRICE_1_YEAR)
            await event.answer("1 Year purchase initiated! You have 1 hour to pay.", alert=True)

            msg_text = (
                f"**VIP 1 Year**\n\n"
                f"Please send **{PRICE_1_YEAR} USDC** to:\n"
                f"`{address}`\n\n"
                "Once we detect payment on-chain, you'll get 1 year VIP."
            )
            sent = await event.respond(
                msg_text,
                buttons=[[Button.inline("<< Back", b"referral_back")]]
            )
            ud["payment_message_id"] = sent.id
            ud["payment_chat_id"] = event.chat_id
            save_referral_data(referral_data)

        elif data == "subscribe_free":
            free_group_link = "https://t.me/+HhwCZpxtjjhkM2E8"
            await event.answer("Here is your free group access link!", alert=False)
            await event.edit(
                "Click below to join our free group:",
                buttons=[
                    [Button.url("Join Free Group", free_group_link)],
                    [Button.inline("<< Back", b"referral_back")]
                ]
            )

# =============================================================================
#  ADMIN CALLBACKS
# =============================================================================

# We'll hold a small pagination cache for active subscriptions
stats_pagination_cache = {}

async def get_dialogs_map():
    dialogs = await client_telegram.get_dialogs(limit=None)
    return {get_peer_id(d.entity): (d.name or "Untitled") for d in dialogs}

def chunk_list(lst, chunk_size=20):
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i+chunk_size]

async def fetch_user_name(user_id):
    """
    Attempt to get a Telegram display name for the user.
    Fallback to just user_id as string if error.
    """
    try:
        entity = await client_telegram.get_entity(user_id)
        if entity.first_name or entity.last_name:
            return (entity.first_name or "") + " " + (entity.last_name or "")
        if entity.username:
            return f"@{entity.username}"
        return str(user_id)
    except:
        return str(user_id)

async def handle_admin_callback_query(event, data):
    user_id = event.sender_id
    if data == "back_main":
        txt = main_menu_text()
        btns = main_menu_buttons()
        await event.edit(txt, buttons=btns)
        return

    if data == "edit_groups":
        dialog_map = await get_dialogs_map()
        txt = group_menu_text(dialog_map)
        btns = group_menu_buttons()
        await event.edit(txt, buttons=btns)
        return

    if data == "toggle_translation":
        curr = bot_settings.get("translation_enabled", True)
        bot_settings["translation_enabled"] = not curr
        save_bot_settings(BOT_SETTINGS_FILE, bot_settings)
        status = "ВКЛ." if bot_settings["translation_enabled"] else "ИЗКЛ."
        await event.answer(f"Преводът вече е {status}", alert=True)
        txt = main_menu_text()
        btns = main_menu_buttons()
        await event.edit(txt, buttons=btns)
        return

    if data == "manage_offensive":
        words_sorted = sorted(OFFENSIVE_WORDS)
        if not words_sorted:
            txt = "⚠️ Няма въведени забранени думи."
        else:
            txt = "⚠️ **Текущи забранени думи** ⚠️\n"
            for w in words_sorted:
                txt += f" - {w}\n"
        txt += "\nИзберете действие отдолу."
        btns = []
        for w in words_sorted:
            cb_data = f"offensive_remove|{w}"
            btns.append([Button.inline(f"Премахни '{w}'", cb_data.encode("utf-8"))])
        btns.append([Button.inline("➕ Добавяне на нова дума", b"offensive_add")])
        btns.append([Button.inline("🔙 Назад", b"back_main")])
        await event.edit(txt, buttons=btns)
        return

    if data.startswith("offensive_remove|"):
        _, word = data.split("|", 1)
        word = word.strip().lower()
        if word in OFFENSIVE_WORDS:
            OFFENSIVE_WORDS.remove(word)
            save_offensive_words(OFFENSIVE_WORDS_FILE, OFFENSIVE_WORDS)
            await event.answer(f"Removed word: {word}", alert=True)
        else:
            await event.answer("Word not found.", alert=True)
        words_sorted = sorted(OFFENSIVE_WORDS)
        if not words_sorted:
            txt = "⚠️ Няма въведени забранени думи."
        else:
            txt = "⚠️ **Текущи забранени думи** ⚠️\n"
            for w in words_sorted:
                txt += f" - {w}\n"
        txt += "\nИзберете действие отдолу."
        btns = []
        for w in words_sorted:
            cb_data = f"offensive_remove|{w}"
            btns.append([Button.inline(f"Премахни '{w}'", cb_data.encode("utf-8"))])
        btns.append([Button.inline("➕ Добавяне на нова дума", b"offensive_add")])
        btns.append([Button.inline("🔙 Назад", b"back_main")])
        await event.edit(txt, buttons=btns)
        return

    if data == "offensive_add":
        pending_offensive_word_add[user_id] = True
        await event.answer("Моля, въведете новата забранена дума като обикновено съобщение.")
        await event.edit("Напишете новата забранена дума сега.\nИли въведете /cancel за отказ.")
        return

    if data.startswith("edit_set_") or data == "edit_test":
        set_name = data.replace("edit_", "", 1)  # "edit_set_1" becomes "set_1"
        dialog_map = await get_dialogs_map()
        txt = set_menu_text(set_name, dialog_map)
        btns = set_menu_buttons(set_name)
        await event.edit(txt, buttons=btns)
        return


    if data == "edit_groups":
        dialog_map = await get_dialogs_map()
        txt = group_menu_text(dialog_map)
        btns = group_menu_buttons()
        await event.edit(txt, buttons=btns)
        return

    if data.startswith("choose_src"):
        _, set_name, idx_str = data.split("|")
        idx = int(idx_str)
        dialogs = await client_telegram.get_dialogs(limit=None)
        items = []
        for d in dialogs:
            if d.is_user:
                continue
            pid = get_peer_id(d.entity)
            title = d.name or "Untitled"
            items.append((pid, title))
        items.sort(key=lambda x: x[1].lower())
        txt = f"Изберете нова група/чат за позиция {idx+1} в {SET_DISPLAY_NAMES[set_name]}."
        btns = []
        for (pid, title) in items:
            short_t = (title[:30] + "...") if len(title) > 30 else title
            data_str = f"replace_src|{set_name}|{idx}|{pid}"
            btns.append([Button.inline(short_t, data_str.encode("utf-8"))])
        btns.append([Button.inline("🔙 Назад", f"edit_{set_name}".encode("utf-8"))])
        await event.edit(txt, buttons=btns)
        return

    if data.startswith("remove_src"):
        _, set_name, idx_str = data.split("|")
        idx = int(idx_str)
        groups_list = source_groups.get(set_name, [])
        if 0 <= idx < len(groups_list):
            removed = groups_list.pop(idx)
            save_source_groups(SOURCE_GROUPS_FILE, source_groups)
            register_handlers()
            await event.answer(f"Премахната група: {removed}", alert=True)
            dialog_map = await get_dialogs_map()
            txt = set_menu_text(set_name, dialog_map)
            btns = set_menu_buttons(set_name)
            await event.edit(txt, buttons=btns)
        else:
            await event.answer("Невалиден индекс!", alert=True)
        return

    if data.startswith("replace_src"):
        _, set_name, idx_str, chat_id_str = data.split("|")
        idx = int(idx_str)
        new_peer_id = int(chat_id_str)
        try:
            entity = await client_telegram.get_entity(new_peer_id)
        except Exception as e:
            print(f"Error get_entity({new_peer_id}): {e}")
            await event.answer("Не може да бъде достъпна тази група/канал.", alert=True)
            return

        # Automatically determine if the group is public or private:
        if getattr(entity, 'username', None):
            # For public groups, store the username.
            stored_value = entity.username
        else:
            # For private groups, store the unique numeric peer ID.
            stored_value = get_peer_id(entity)

        # Make sure the list is long enough.
        while len(source_groups[set_name]) <= idx:
            source_groups[set_name].append(None)
        source_groups[set_name][idx] = stored_value
        save_source_groups(SOURCE_GROUPS_FILE, source_groups)
        register_handlers()

        # Optionally, update your display names for clarity.
        dialog_map = await get_dialogs_map()
        updated_names = []
        for item in source_groups[set_name]:
            if isinstance(item, int):
                updated_names.append(dialog_map.get(item, str(item)))
            else:
                updated_names.append(item)
        txt = (
            f"✅ Обновен списък за **{SET_DISPLAY_NAMES[set_name]}**:\n"
            f"{updated_names}\n\n"
            "Връщаме се в основното меню..."
        )
        btns = main_menu_buttons()
        await event.edit(txt, buttons=btns)


    # -------------------------------------------------------------------------
    # STATS MENU
    # -------------------------------------------------------------------------
    if data == "admin_stats":
        now_ts = int(time.time())
        users = referral_data["users"]

        # new users in last X
        last24 = now_ts - 86400
        last7d = now_ts - 7*86400
        last30d = now_ts - 30*86400

        new_users_24h = 0
        new_users_7d = 0
        new_users_30d = 0

        # new subscriptions count
        new_subs_24h = 0
        new_subs_7d = 0
        new_subs_30d = 0

        # total sales and total paid to referers
        total_sales = 0
        total_paid_to_ref = 0

        # top earners
        earners_list = []

        # count how many active subscriptions right now
        active_subs_count = 0

        for uid_str, user_obj in users.items():
            # joined_at check
            joined_at = user_obj.get("joined_at", 0)
            if joined_at >= last24:
                new_users_24h += 1
            if joined_at >= last7d:
                new_users_7d += 1
            if joined_at >= last30d:
                new_users_30d += 1

            # purchase_history for new subs counts
            purchase_history = user_obj.get("purchase_history", [])
            for ph in purchase_history:
                pht = ph["timestamp"]
                amount_paid = ph["amount_paid"]
                total_sales += amount_paid
                if pht >= last24:
                    new_subs_24h += 1
                if pht >= last7d:
                    new_subs_7d += 1
                if pht >= last30d:
                    new_subs_30d += 1

            # check active subscription
            if user_obj.get("group_access_until", 0) > now_ts:
                active_subs_count += 1

            # track earners
            earn = user_obj.get("earned", 0)
            total_paid_to_ref += earn
            if earn > 0:
                earners_list.append((uid_str, earn))

        # sort earners descending
        earners_list.sort(key=lambda x: x[1], reverse=True)
        top_5_earners = earners_list[:5]

        # Format output
        stats_text = (
            "**[Статистика]**\n\n"
            f"**Нови потребители**:\n"
            f" - Последни 24ч: {new_users_24h}\n"
            f" - Последни 7д: {new_users_7d}\n"
            f" - Последни 30д: {new_users_30d}\n\n"

            f"**Нови абонаменти**:\n"
            f" - Последни 24ч: {new_subs_24h}\n"
            f" - Последни 7д: {new_subs_7d}\n"
            f" - Последни 30д: {new_subs_30d}\n\n"

            f"**Текущо активни абонаменти**: {active_subs_count}\n"
            f"*(Натиснете бутона долу, за да видите списък)*\n\n"

            f"**Общо приходи (sales)**: {total_sales} USDC\n"
            f"**Общо изплатени на рефери**: {total_paid_to_ref} USDC\n\n"
            "**Топ 5 рефера**:\n"
        )
        for (uid_str, earn_val) in top_5_earners:
            stats_text += f"- User {uid_str} => {earn_val} USDC\n"

        btns = [
            [Button.inline("👀 Активни абонаменти", b"stats_active_subs|0")],
            [Button.inline("🔙 Назад", b"back_main")]
        ]
        await event.edit(stats_text, buttons=btns)
        return

    if data.startswith("stats_active_subs"):
        # parse page
        parts = data.split("|")
        page_num = int(parts[1]) if len(parts) > 1 else 0
        now_ts = int(time.time())

        # collect all user IDs with active sub
        active_users = []
        for uid_str, user_obj in referral_data["users"].items():
            if user_obj.get("group_access_until", 0) > now_ts:
                active_users.append(int(uid_str))
        active_users.sort()

        # chunk
        chunks = list(chunk_list(active_users, 20))
        total_pages = len(chunks)
        if total_pages == 0:
            # no active subs
            await event.answer("Няма активни абонаменти в момента!", alert=True)
            return

        page_num = max(0, min(page_num, total_pages-1))
        page_users = chunks[page_num]
        lines = [f"**Активни абонаменти - Страница {page_num+1}/{total_pages}**\n"]

        # gather info about each user
        for uid in page_users:
            ud = get_user_data(uid)
            expiry = ud.get("group_access_until", 0)
            timestr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry))
            lines.append(f"- User {uid}, Expires: {timestr}")

        txt = "\n".join(lines)
        btns = []
        nav_btns = []
        if page_num > 0:
            prev_data = f"stats_active_subs|{page_num-1}"
            nav_btns.append(Button.inline("⬅️", data=prev_data.encode("utf-8")))
        if page_num < total_pages-1:
            next_data = f"stats_active_subs|{page_num+1}"
            nav_btns.append(Button.inline("➡️", data=next_data.encode("utf-8")))
        if nav_btns:
            btns.append(nav_btns)
        btns.append([Button.inline("🔙 Назад", b"admin_stats")])

        await event.edit(txt, buttons=btns)
        return

# =============================================================================
#  MAIN
# =============================================================================
def main():
    client_telegram.start(phone=phone)
    bot.start(bot_token=BOT_TOKEN)

    loop = asyncio.get_event_loop()
    loop.create_task(remove_expired_access())
    loop.create_task(watch_deposits())

    print("Forwarder is running. Bot is running.")
    print("Admins use /start => admin menu. Others => referral menu.")

    loop.run_until_complete(
        asyncio.gather(
            client_telegram.run_until_disconnected(),
            bot.run_until_disconnected()
        )
    )

if __name__ == "__main__":
    main()
