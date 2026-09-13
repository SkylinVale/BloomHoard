import os
import re
import discord
from discord import app_commands
from supabase import create_client, Client
from dotenv import load_dotenv
import asyncio
from datetime import datetime, timezone, timedelta

load_dotenv()

# ════════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════════

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

RARITIES = ["Green", "Blue", "Purple", "Gold", "Red"]

RARITY_DISPLAY = {
    "Green":  "Ordinary",
    "Blue":   "Common",
    "Purple": "Exceptional",
    "Gold":   "Splendid",
    "Red":    "Celestial",
}

RARITY_ORDER = {r: i for i, r in enumerate(["Red", "Gold", "Purple", "Blue", "Green"])}

POINT_TIERS    = [30, 28, 25, 23, 21, 14, 9]
ALWAYS_INCLUDE = {30, 28, 25}
MIN_FLOWERS    = 10

TIER_FALLBACK = {30: "🔴", 28: "🟠", 25: "🟡", 23: "🟢", 21: "🔵", 14: "🟣", 9: "⚪"}

VASE_SLOTS = [
    "primary_1",   "primary_2",   "primary_3",
    "secondary_1", "secondary_2", "secondary_3",
    "tertiary_1",  "tertiary_2",  "tertiary_3",
]

STAFF_ROLES = {"Vice President", "Steward"}
PINK        = discord.Color.from_rgb(255, 182, 193)
PAGE_SIZE   = 25
MOUNTAIN_TZ = timezone(timedelta(hours=-7))

# ════════════════════════════════════════════════════════════════════════════════
# BOT SETUP
# ════════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


@client.event
async def on_ready():
    MY_GUILD = discord.Object(id=int(os.environ["GUILD_ID"]))
    tree.copy_global_to(guild=MY_GUILD)
    await tree.sync(guild=MY_GUILD)
    print(f"Logged in as {client.user} — slash commands synced!")
    client.loop.create_task(weekly_cleardone())


# ════════════════════════════════════════════════════════════════════════════════
# WEEKLY AUTO-CLEAR DONE MARKERS
# ════════════════════════════════════════════════════════════════════════════════

async def weekly_cleardone():
    await client.wait_until_ready()
    while not client.is_closed():
        now = datetime.now(MOUNTAIN_TZ)
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 22:
            days_until_sunday = 7
        next_sunday = now.replace(hour=22, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
        await asyncio.sleep((next_sunday - now).total_seconds())
        supabase.table("players").update({"done": False}).eq("done", True).execute()
        print("Auto-cleared all done markers (Sunday 10pm Mountain)")


# ════════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.name in STAFF_ROLES for role in interaction.user.roles)

def log_change(
    interaction: discord.Interaction,
    category: str,
    change_type: str,
    name: str,
    description: str,
    old_name: str | None = None,
):
    supabase.table("changelog").insert({
        "category": category,
        "change_type": change_type,
        "name": name,
        "old_name": old_name,
        "description": description,
        "created_by": interaction.user.name,
    }).execute()

def log_bot_change(
    interaction: discord.Interaction,
    description: str,
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=42)

    existing = (
        supabase.table("changelog")
        .select("id")
        .eq("category", "bot")
        .eq("description", description)
        .gte("changed_at", cutoff.isoformat())
        .execute()
    )

    if existing.data:
        return

    log_change(
        interaction,
        "bot",
        "updated",
        "BlossomHoard",
        description,
    )
    
def get_config_icon(key: str) -> str | None:
    res = supabase.table("config").select("icon_url").eq("key", key).execute()
    return res.data[0]["icon_url"] if res.data else None


def rarity_icon(rarity: str) -> str:
    return get_config_icon(f"rarity_{rarity.lower()}") or ""


def bonus_icon(bonus: int) -> str:
    return get_config_icon(f"bonus_{bonus}") or ""


def tier_icon(pts: int) -> str:
    return get_config_icon(f"tier_{pts}") or TIER_FALLBACK.get(pts, "•")


def sort_key_rarity_points_alpha(b: dict) -> tuple:
    return (
        RARITY_ORDER.get(b.get("rarity", "Green"), 99),
        -b.get("points", 0),
        b.get("name", ""),
    )


def build_hoard_lines(rows: list, bl_map: dict) -> list[str]:
    """Build display lines for a hoard list."""
    lines = []
    for row in rows:
        b    = bl_map.get(row["blossom"], {})
        rico = rarity_icon(b.get("rarity", "")) if b.get("rarity") else ""
        line = f"{rico} **{row['blossom']}** — {b.get('points', '?')} pts"
        if row.get("bonus"):
            line += f"  {bonus_icon(row['bonus'])} +{row['bonus']}"
        lines.append(line)
    return lines


def build_whitelist_lines(kept: list) -> list[str]:
    """Build display lines for the whitelist."""
    lines = []
    for b in kept:
        ico = rarity_icon(b["rarity"]) if b.get("rarity") else ""
        lines.append(f"{ico} **{b['name']}** — {b['points']} pts")
    return lines

async def ocr_image(image_path: str) -> str:
    """Run OCR with image preprocessing to improve text recognition."""

    from PIL import Image, ImageEnhance, ImageFilter
    import pytesseract

    image = Image.open(image_path).convert("RGB")

    # Enlarge the image so small/stylized text is easier for Tesseract.
    scale = 2
    image = image.resize(
        (image.width * scale, image.height * scale),
        Image.Resampling.LANCZOS
    )

    grayscale = image.convert("L")

    contrast = ImageEnhance.Contrast(grayscale).enhance(2.0)

    sharpened = contrast.filter(
        ImageFilter.SHARPEN
    )

    # Use the sharpened version as the primary OCR input.
    result = pytesseract.image_to_string(
        sharpened,
        config="--psm 6"
    )

    return result.strip()

async def ocr_image_data(image_path: str) -> list[dict]:
    """Run OCR and return recognized words with position and confidence."""
    from PIL import Image
    import pytesseract

    image = Image.open(image_path)

    data = pytesseract.image_to_data(
        image,
        config="--psm 6",
        output_type=pytesseract.Output.DICT,
    )

    results = []

    for i, text in enumerate(data["text"]):
        text = text.strip()

        if not text:
            continue

        try:
            confidence = float(data["conf"][i])
        except (ValueError, TypeError):
            confidence = -1

        results.append({
            "text": text,
            "confidence": confidence,
            "x": data["left"][i],
            "y": data["top"][i],
            "width": data["width"][i],
            "height": data["height"][i],
            "block_num": data["block_num"][i],
            "par_num": data["par_num"][i],
            "line_num": data["line_num"][i],
            "word_num": data["word_num"][i],
        })

    return results

def parse_game_identity(text: str):
    """
    Extract (server_number, game_name) pairs from OCR text.

    Supports:
    - s5.Miraea
    - s102.DittoWasHere
    - s5
      Miraea
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    identities = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Format: s5.Miraea
        match = re.fullmatch(r"s(\d+)\.(.+)", line, re.IGNORECASE)
        if match:
            identities.append((int(match.group(1)), match.group(2).strip()))
            i += 1
            continue

        # Format:
        # s5
        # Miraea
        match = re.fullmatch(r"s(\d+)", line, re.IGNORECASE)
        if match and i + 1 < len(lines):
            next_line = lines[i + 1].strip()

            # Don't accept another server line as the name
            if not re.fullmatch(r"s\d+", next_line, re.IGNORECASE):
                identities.append((int(match.group(1)), next_line))
                i += 2
                continue

        i += 1

    return identities

def parse_task_logs(
    text: str,
    player_aliases: dict | None = None,
) -> list[dict]:
    """
    Extract flower/task entries from OCR text.

    OCR is assumed to be unreliable.

    Supported:
        s29.Metp has completed Advanced Task 63: Harvest 560 Orange Poppy
        s25.Rosie has completed Task 29: Harvest 280 White Ixia
        s39.Re Nichole has completed Advanced Task 7: Harvest 560 Brunfelsia pauciflora
        s16.Hill Ynez has completed Task 24: Harvest 280 Orange Oxalis
        s4.Lily spent Ingots to upgrade Task 60: Harvest 560 Taro Purple Gladiolus!!

    Handles OCR damage such as:
        s16.Hill Ynez has completed Task \
        24: Harvest 280 Orange Oxalis

        s61.Lexie has completed Task 20: {
        Harvest 280 Golden Lycoris

        s15.Starla has completed
        Advanced Task 7: Harvested 600
        Golden Scales in Flight

        . $38.44= spent Ingots to upgrade
        , Task 12: Harvest 560 Pink Astilbe!!

        pes eiuey has completed Advanced
        Task 32: Harvest 600 Pomegranate Moon

        OO 99.01 20:33

    Deleted tasks are intentionally ignored.
    Non-flower upgrades are intentionally ignored.
    """

    print("\n" + "=" * 70)
    print("DEBUG PARSER START")
    print("=" * 70)

    print("DEBUG RAW INPUT:")
    print(repr(text))

    # ---------------------------------------------------------
    # Alias normalization
    # ---------------------------------------------------------

    aliases = {}

    if player_aliases:
        aliases = {
            str(k).strip().lower(): str(v).strip()
            for k, v in player_aliases.items()
            if str(k).strip() and str(v).strip()
        }

    print("\nDEBUG PLAYER ALIASES:")
    print(repr(aliases))

    # ---------------------------------------------------------
    # Basic OCR helpers
    # ---------------------------------------------------------

    def normalize_spaces(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def contains_timestamp(value: str) -> bool:
        """
        Detect a timestamp anywhere inside an OCR-damaged line.

        Examples:
            09.06 13:19
            09.06 13:19 f
            OO 99.01 20:33

        The last example is intentionally accepted because OCR may
        corrupt the characters immediately before the timestamp.
        """

        return bool(
            re.search(
                r"\d{2}[.,]\d{2}\s+\d{1,2}:\d{2}",
                value,
            )
        )

    def is_timestamp(value: str) -> bool:
        """
        True when the line is essentially just a timestamp,
        possibly surrounded by OCR garbage.
        """

        return bool(
            re.match(
                r"^[^A-Za-z0-9]*"
                r"\d{2}[.,]\d{2}"
                r"\s+"
                r"\d{1,2}:\d{2}"
                r"[^A-Za-z0-9]*$",
                value,
            )
        )

    def is_footer(value: str) -> bool:
        return bool(
            re.match(
                r"^[^A-Za-z]*"
                r"(?:keep|feep)"
                r"\s+only\s+"
                r"(?:the\s+)?latest\s+100\s+logs",
                value,
                re.IGNORECASE,
            )
        )

    def clean_player_name(value: str) -> str:
        """
        Remove OCR punctuation surrounding a player name.
        """

        value = normalize_spaces(value)

        value = value.strip(
            " \t\r\n.,;:'\"!?|\\/_=+-»>«<()[]{}"
        )

        return value

    def clean_flower_name(value: str) -> str:
        """
        Clean OCR punctuation from a flower/task name.
        """

        value = normalize_spaces(value)

        value = re.sub(
            r"[|\\»>_{}[\]()]",
            " ",
            value,
        )

        value = re.split(
            r",?\s*earning\b",
            value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]

        value = re.split(
            r"\bCompetition\s+Points\b",
            value,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]

        value = normalize_spaces(value)

        value = value.strip(
            " \t\r\n.,;:'\"!?|\\/_=+-»>«<()[]{}"
        )

        return value

    def apply_alias(name: str) -> str:
        cleaned = clean_player_name(name)

        alias = aliases.get(cleaned.lower())

        if alias:
            print(
                "DEBUG PLAYER ALIAS MATCH: "
                f"{cleaned!r} -> {alias!r}"
            )
            return alias

        return cleaned

    # ---------------------------------------------------------
    # Player header detection
    # ---------------------------------------------------------

    combined_player_pattern = re.compile(
        r"^[^A-Za-z0-9]*"
        r"[s$]"
        r"(\d{1,3})"
        r"\s*[.=]\s*"
        r"(.+?)"
        r"\s+"
        r"(?="
        r"has\s+completed\b"
        r"|spent\s+Ingots\s+to\b"
        r"|deleted\s+Task\b"
        r")",
        re.IGNORECASE,
    )

    server_only_pattern = re.compile(
        r"^[^A-Za-z0-9]*"
        r"s(\d{1,3})"
        r"[^A-Za-z0-9]*$",
        re.IGNORECASE,
    )

    def detect_combined_player(line: str):
        match = combined_player_pattern.match(line)

        if not match:
            return None

        server = int(match.group(1))
        name = apply_alias(match.group(2))

        # Avoid treating extremely short OCR garbage as a useful
        # player name unless it has been explicitly aliased.
        if len(name) < 2:
            print(
                "DEBUG PLAYER REJECTED - NAME TOO SHORT: "
                f"server={server}, name={name!r}, "
                f"raw_line={line!r}"
            )
            return None

        print(
            "DEBUG PLAYER DETECTED - COMBINED: "
            f"server={server}, name={name!r}, "
            f"raw_line={line!r}"
        )

        return {
            "server_number": server,
            "game_name": name,
            "header_match": match,
        }

    # ---------------------------------------------------------
    # Build cleaned OCR lines
    # ---------------------------------------------------------

    raw_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    print("\nDEBUG CLEANED LINES:")

    for index, line in enumerate(raw_lines):
        print(f"  [{index}] {repr(line)}")

    print(
        f"\nDEBUG TOTAL CLEANED LINES: {len(raw_lines)}"
    )

    # ---------------------------------------------------------
    # Build logical blocks
    # ---------------------------------------------------------
    #
    # Important:
    #
    # Timestamp lines separate cards.
    #
    # We also split on recognizable player headers because OCR
    # occasionally destroys a timestamp completely.
    #
    # NEW:
    #
    # A timestamp embedded inside an OCR-damaged line is also
    # treated as a boundary.
    #
    # Example:
    #
    #     OO 99.01 20:33
    #
    # becomes a timestamp boundary instead of being swallowed
    # into the previous task.
    # ---------------------------------------------------------

    blocks = []
    current_block = []

    def flush_block():
        nonlocal current_block

        if current_block:
            blocks.append(current_block)
            current_block = []

    i = 0

    while i < len(raw_lines):

        line = raw_lines[i]

        # -----------------------------------------------------
        # Footer
        # -----------------------------------------------------

        if is_footer(line):
            print(
                "DEBUG BLOCK SPLIT: footer "
                f"{line!r}"
            )
            flush_block()
            break

        # -----------------------------------------------------
        # Timestamp line
        # -----------------------------------------------------

        if is_timestamp(line):
            print(
                "DEBUG BLOCK SPLIT: timestamp "
                f"{line!r}"
            )
            flush_block()
            i += 1
            continue

        # -----------------------------------------------------
        # Timestamp embedded in OCR garbage
        #
        # Example:
        #     OO 99.01 20:33
        #
        # If there is no meaningful task text before it, simply
        # treat the entire line as a timestamp.
        #
        # If there IS text before it, preserve that text as part
        # of the current block, then split.
        # -----------------------------------------------------

        timestamp_match = re.search(
            r"\d{2}[.,]\d{2}\s+\d{1,2}:\d{2}",
            line,
        )

        if timestamp_match:

            before_timestamp = line[
                :timestamp_match.start()
            ].strip()

            after_timestamp = line[
                timestamp_match.end():
            ].strip()

            # Pure OCR garbage + timestamp.
            if (
                not before_timestamp
                or not re.search(
                    r"[A-Za-z]{3,}",
                    before_timestamp,
                )
            ):
                print(
                    "DEBUG BLOCK SPLIT: embedded timestamp "
                    f"{line!r}"
                )

                flush_block()

                # Anything after the timestamp is potentially
                # the beginning of the next entry.
                if after_timestamp:
                    current_block.append(after_timestamp)

                i += 1
                continue

        # -----------------------------------------------------
        # Recognizable combined player header
        # -----------------------------------------------------

        player = detect_combined_player(line)

        if player:

            if current_block:
                print(
                    "DEBUG BLOCK SPLIT: new player header "
                    f"{line!r}"
                )
                flush_block()

            current_block = [line]
            i += 1
            continue

        # -----------------------------------------------------
        # Split-format server/name:
        #
        # s29
        # Metp
        # -----------------------------------------------------

        server_only = server_only_pattern.match(line)

        if server_only and i + 1 < len(raw_lines):

            possible_name = raw_lines[i + 1]

            if (
                not is_timestamp(possible_name)
                and not is_footer(possible_name)
                and not re.search(
                    r"\bTask\s+\d+\s*:",
                    possible_name,
                    re.IGNORECASE,
                )
            ):

                if current_block:
                    flush_block()

                server = int(server_only.group(1))
                name = apply_alias(possible_name)

                print(
                    "DEBUG PLAYER DETECTED - SPLIT: "
                    f"server={server}, name={name!r}"
                )

                current_block = [
                    line,
                    possible_name,
                ]

                i += 2
                continue

        current_block.append(line)
        i += 1

    flush_block()

    print("\nDEBUG LOGICAL BLOCKS:")

    for index, block in enumerate(blocks):
        print(
            f"  BLOCK {index}: "
            f"{repr(' '.join(block))}"
        )

    # ---------------------------------------------------------
    # Task regexes
    # ---------------------------------------------------------

    completed_pattern = re.compile(
        r"has\s+completed\b"
        r".*?"
        r"\bTask\s*"
        r"[^0-9]{0,8}"
        r"(\d+)"
        r"\s*:\s*"
        r"[^A-Za-z]{0,12}"
        r"Harvest(?:ed)?"
        r"\s+"
        r"(.+?)"
        r"(?="
        r"\s*,?\s*earning\b"
        r"|\s*Competition\s+Points\b"
        r"|$"
        r")",
        re.IGNORECASE,
    )

    orphan_completed_pattern = re.compile(
        r"\bTask\s*"
        r"[^0-9]{0,8}"
        r"(\d+)"
        r"\s*:\s*"
        r"[^A-Za-z]{0,12}"
        r"Harvest(?:ed)?"
        r"\s+"
        r"(.+?)"
        r"(?="
        r"\s*,?\s*earning\b"
        r"|\s*Competition\s+Points\b"
        r"|$"
        r")",
        re.IGNORECASE,
    )

    # ---------------------------------------------------------
    # UPGRADE PATTERN
    #
    # OCR can insert punctuation, stray characters, or spaces
    # between any of these pieces:
    #
    #     spent Ingots to upgrade Task 60
    #     spent Ingots to upgrade , Task 12
    #     spent Ingots to L upgrade | Task 7
    #     . $38.44= spent Ingots to upgrade , Task 12
    #
    # We only care that the block contains the recognizable
    # sequence:
    #
    #     spent Ingots to ... upgrade ... Task <number>:
    #
    # ---------------------------------------------------------

    upgrade_pattern = re.compile(
        r"spent\s+Ingots\s+to"
        r".*?"
        r"\bupgrade\b"
        r"[\s\W_]*"
        r"\bTask\s*"
        r"[^0-9]{0,8}"
        r"(\d+)"
        r"\s*:\s*"
        r"(.+?)"
        r"(?=$|\s{2,})",
        re.IGNORECASE,
    )
    
    def extract_flower_from_upgrade(task_content: str):
        """
        Determine whether an upgrade task is a flower task.

        Flower:
            Harvest 560 Pink Snapdragon

        Non-flower:
            Upgrade any flower 5 times
        """

        match = re.search(
            r"\bHarvest(?:ed)?\s+"
            r"(.+?)"
            r"[!.|\\»>_]*$",
            task_content.strip(),
            re.IGNORECASE,
        )

        if not match:
            return None

        flower = clean_flower_name(
            match.group(1)
        )

        return flower if flower else None

    # ---------------------------------------------------------
    # Parse logical blocks
    # ---------------------------------------------------------

    entries = []

    for block_index, block in enumerate(blocks):

        print("\n" + "-" * 70)
        print(
            f"DEBUG PARSING BLOCK {block_index}"
        )

        block_text = normalize_spaces(
            " ".join(block)
        )

        print(
            "DEBUG BLOCK TEXT:",
            repr(block_text),
        )

        server_number = None
        game_name = None
        ocr_player = None

        # -----------------------------------------------------
        # Identify player
        # -----------------------------------------------------

        combined_player = detect_combined_player(
            block[0]
        )

        if combined_player:

            server_number = combined_player[
                "server_number"
            ]

            game_name = combined_player[
                "game_name"
            ]

            ocr_player = clean_player_name(
                combined_player[
                    "header_match"
                ].group(2)
            )

        # -----------------------------------------------------
        # Split-format player
        # -----------------------------------------------------

        elif (
            len(block) >= 2
            and server_only_pattern.match(block[0])
        ):

            server_match = server_only_pattern.match(
                block[0]
            )

            server_number = int(
                server_match.group(1)
            )

            ocr_player = clean_player_name(
                block[1]
            )

            game_name = apply_alias(
                ocr_player
            )

        # -----------------------------------------------------
        # Deleted tasks
        #
        # THIS MUST HAPPEN BEFORE THE ORPHAN COMPLETED FALLBACK.
        #
        # Otherwise:
        #
        #     deleted Task 7: Harvest 280 Hoary Stock!!
        #
        # gets mistaken for a completion because it still
        # contains "Task 7: Harvest ..."
        # -----------------------------------------------------

        deleted_match = re.search(
            r"\bdeleted\s+Task\b",
            block_text,
            re.IGNORECASE,
        )

        if deleted_match:

            print(
                "DEBUG DELETED TASK: YES"
            )

            print(
                "DEBUG DECISION: "
                "IGNORING DELETED TASK"
            )

            continue

        # -----------------------------------------------------
        # If player cannot be confidently identified, preserve
        # OCR player text.
        # -----------------------------------------------------

        if game_name is None:

            possible_orphan_player = re.search(
                r"^(.+?)\s+"
                r"(?="
                r"has\s+completed\b"
                r"|spent\s+Ingots\s+to\b"
                r"|deleted\s+Task\b"
                r")",
                block_text,
                re.IGNORECASE,
            )

            if possible_orphan_player:

                ocr_player = clean_player_name(
                    possible_orphan_player.group(1)
                )

                aliased_player = apply_alias(
                    ocr_player
                )

                if aliased_player != ocr_player:

                    game_name = aliased_player

                    print(
                        "DEBUG ORPHAN PLAYER ALIAS RESOLVED: "
                        f"{ocr_player!r} -> "
                        f"{game_name!r}"
                    )

                else:

                    game_name = "<unknown player>"

                    print(
                        "DEBUG ORPHAN PLAYER: "
                        f"{ocr_player!r}"
                    )

            else:

                game_name = "<unknown player>"

                print(
                    "DEBUG ORPHAN PLAYER: "
                    "no usable OCR player text"
                )

        # -----------------------------------------------------
        # COMPLETED FLOWER TASK
        # -----------------------------------------------------

        completed = completed_pattern.search(
            block_text
        )

        if completed:

            task_number = int(
                completed.group(1)
            )

            flower = clean_flower_name(
                completed.group(2)
            )

            print(
                "DEBUG COMPLETED MATCH: YES"
            )

            print(
                "DEBUG COMPLETED TASK:",
                task_number,
            )

            print(
                "DEBUG COMPLETED FLOWER:",
                repr(flower),
            )

            entry = {
                "server_number": server_number,
                "game_name": game_name,
                "action": "completed",
                "task_number": task_number,
                "task_text": flower,
                "competition_points": None,
                "competition_tokens": None,
                "is_flower": True,
            }

            if ocr_player:
                entry["ocr_player"] = ocr_player

            entries.append(entry)

            print(
                "DEBUG DECISION: "
                "ADDING COMPLETED FLOWER ENTRY"
            )

            continue

        print(
            "DEBUG COMPLETED MATCH: NO"
        )

        # -----------------------------------------------------
        # ORPHAN COMPLETED FLOWER
        # -----------------------------------------------------

        orphan_completed = (
            orphan_completed_pattern.search(
                block_text
            )
        )

        if (
            orphan_completed
            and game_name == "<unknown player>"
        ):

            task_number = int(
                orphan_completed.group(1)
            )

            flower = clean_flower_name(
                orphan_completed.group(2)
            )

            print(
                "DEBUG ORPHAN COMPLETED MATCH: YES"
            )

            print(
                "DEBUG ORPHAN TASK:",
                task_number,
            )

            print(
                "DEBUG ORPHAN FLOWER:",
                repr(flower),
            )

            entry = {
                "server_number": None,
                "game_name": "<unknown player>",
                "action": "completed",
                "task_number": task_number,
                "task_text": flower,
                "competition_points": None,
                "competition_tokens": None,
                "is_flower": True,
            }

            if ocr_player:
                entry["ocr_player"] = ocr_player

            entries.append(entry)

            print(
                "DEBUG DECISION: "
                "ADDING UNKNOWN-PLAYER "
                "FLOWER COMPLETED ENTRY"
            )

            continue

        # -----------------------------------------------------
        # ANY UPGRADE
        # -----------------------------------------------------

        upgrade = upgrade_pattern.search(
            block_text
        )

        if upgrade:

            task_number = int(
                upgrade.group(1)
            )

            task_content = normalize_spaces(
                upgrade.group(2)
            )

            print(
                "DEBUG ANY UPGRADE MATCH: YES"
            )

            print(
                "DEBUG UPGRADE TASK:",
                task_number,
            )

            print(
                "DEBUG UPGRADE CONTENT:",
                repr(task_content),
            )

            flower = extract_flower_from_upgrade(
                task_content
            )

            if flower:

                print(
                    "DEBUG UPGRADE CATEGORY: 🌸 FLOWER"
                )

                print(
                    "DEBUG UPGRADED FLOWER:",
                    repr(flower),
                )

                entry = {
                    "server_number": server_number,
                    "game_name": game_name,
                    "action": "upgraded",
                    "task_number": task_number,
                    "task_text": flower,
                    "competition_points": None,
                    "competition_tokens": None,
                    "is_flower": True,
                }

                if ocr_player:
                    entry["ocr_player"] = ocr_player

                entries.append(entry)

                print(
                    "DEBUG DECISION: "
                    "ADDING FLOWER UPGRADE ENTRY"
                )

            else:

                print(
                    "DEBUG UPGRADE CATEGORY: "
                    "🚫 NOT A FLOWER"
                )

                print(
                    "DEBUG DECISION: "
                    "IGNORING NON-FLOWER UPGRADE"
                )

            continue

        print(
            "DEBUG ANY UPGRADE MATCH: NO"
        )

        print(
            "DEBUG DECISION: "
            "BLOCK DID NOT MATCH COMPLETED "
            "OR FLOWER UPGRADE"
        )

    # ---------------------------------------------------------
    # Deduplication
    # ---------------------------------------------------------

    print("\n" + "=" * 70)
    print("DEBUG DEDUPLICATION")
    print("=" * 70)

    print(
        f"DEBUG ENTRIES BEFORE DEDUP: "
        f"{len(entries)}"
    )

    unique_entries = []
    seen = set()

    for entry in entries:

        key = (
            entry["server_number"],
            entry["game_name"].lower(),
            entry["action"],
            entry["task_number"],
            entry["task_text"].lower(),
        )

        print(
            "DEBUG DEDUPE KEY:",
            repr(key),
        )

        if key not in seen:

            seen.add(key)
            unique_entries.append(entry)

            print(
                "DEBUG DEDUPE RESULT: KEEP"
            )

        else:

            print(
                "DEBUG DEDUPE RESULT: "
                "REMOVE DUPLICATE"
            )

    print(
        f"DEBUG ENTRIES AFTER DEDUP: "
        f"{len(unique_entries)}"
    )

    # ---------------------------------------------------------
    # Final debug output
    # ---------------------------------------------------------

    print("\nDEBUG FINAL ENTRIES:")

    for entry in unique_entries:

        print(
            f"  {entry['server_number']}."
            f"{entry['game_name']} | "
            f"{entry['action']} | "
            f"Task {entry['task_number']} | "
            f"{entry['task_text']} | "
            f"is_flower={entry.get('is_flower')}"
        )

        if entry.get("ocr_player"):
            print(
                "      OCR PLAYER:",
                repr(entry["ocr_player"])
            )

    print("\n" + "=" * 70)
    print("DEBUG PARSER END")
    print("=" * 70 + "\n")

    return unique_entries

def group_ocr_lines(words: list[dict]) -> list[str]:
    """Group OCR words into readable lines using their Y coordinates."""

    if not words:
        return []

    # Sort primarily by vertical position, then horizontal position
    words = sorted(words, key=lambda w: (w["y"], w["x"]))

    lines = []
    current_line = []
    current_y = None

    # Words within this many pixels vertically are considered
    # part of the same text line.
    Y_TOLERANCE = 12

    for word in words:
        if current_y is None:
            current_y = word["y"]
            current_line = [word]
            continue

        if abs(word["y"] - current_y) <= Y_TOLERANCE:
            current_line.append(word)
        else:
            current_line.sort(key=lambda w: w["x"])
            lines.append(" ".join(w["text"] for w in current_line))

            current_line = [word]
            current_y = word["y"]

    if current_line:
        current_line.sort(key=lambda w: w["x"])
        lines.append(" ".join(w["text"] for w in current_line))

    return lines

async def resolve_game_identity(server_number: int, game_name: str):
    game_name = game_name.strip()

    link = (
        supabase.table("player_aliases")
        .select("player_id")
        .eq("server_number", server_number)
        .eq("game_name", game_name)
        .execute()
        .data
    )

    if not link:
        return None

    player_id = link[0]["player_id"]

    player = (
        supabase.table("players")
        .select("gamename")
        .eq("id", player_id)
        .execute()
        .data
    )

    if not player:
        return None

    return player[0]["gamename"]

# ════════════════════════════════════════════════════════════════════════════════
# PLAYER ALIAS / IDENTITY HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def load_player_aliases():
    """
    Load all player aliases from the player_aliases table.

    A fresh Supabase client is used for this read because the
    shared client's HTTP connection can occasionally terminate
    between consecutive synchronous requests.
    """

    alias_supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )

    return (
        alias_supabase
        .table("player_aliases")
        .select(
            "player_id, game_name, server_number"
        )
        .execute()
        .data
        or []
    )


def resolve_player_alias(game_name, server_number=None, aliases=None):
    """
    Resolve an OCR game name to a player ID using player_aliases.

    If server_number is available, it is used to make the match more specific.

    Returns:
        {
            "player_id": int,
            "game_name": str,
            "server_number": int | None,
            "match_type": "exact" | "alias"
        }

    Returns None when:
        - no matching alias exists
        - the name is ambiguous without a server number
    """
    if not game_name:
        return None

    # Use a supplied alias list when available so multiple entries
    # from the same screenshot do not require repeated database calls.
    if aliases is None:
        aliases = load_player_aliases()

    normalized_name = " ".join(str(game_name).strip().split()).casefold()

    matches = []

    for alias in aliases:
        alias_name = alias.get("game_name")

        if not alias_name:
            continue

        normalized_alias = " ".join(
            str(alias_name).strip().split()
        ).casefold()

        if normalized_alias != normalized_name:
            continue

        alias_server = alias.get("server_number")

        # If we know the server, require the alias to match it.
        if server_number is not None:
            if alias_server != server_number:
                continue

        matches.append(alias)

    # No match.
    if not matches:
        return None

    # If there are multiple matches and we don't have a server number,
    # don't guess which player it belongs to.
    player_ids = {row["player_id"] for row in matches}

    if len(player_ids) > 1:
        return None

    match = matches[0]

    # Determine whether this was the canonical name or an alternate alias.
    match_type = (
        "exact"
        if str(match["game_name"]).strip().casefold() == normalized_name
        else "alias"
    )

    return {
        "player_id": match["player_id"],
        "game_name": match["game_name"],
        "server_number": match.get("server_number"),
        "match_type": match_type,
    }


def save_player_alias(player_id, game_name, server_number=None):
    """
    Save a newly discovered player game name/OCR alias.

    Returns:
        "created"             - a new alias was saved
        "already_same_player" - this exact identity already belongs to this player
        "conflict"            - this exact identity belongs to a different player
    """
    if not game_name:
        return None

    cleaned_name = " ".join(str(game_name).strip().split())

    alias_supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )

    # Check whether this exact game name/server identity already exists,
    # regardless of which player it currently belongs to.
    query = (
        alias_supabase
        .table("player_aliases")
        .select("id, player_id, game_name, server_number")
        .eq("game_name", cleaned_name)
    )

    if server_number is None:
        query = query.is_("server_number", "null")
    else:
        query = query.eq("server_number", server_number)

    existing = query.limit(1).execute().data or []

    if existing:
        existing_player_id = existing[0]["player_id"]

        if existing_player_id == player_id:
            return "already_same_player"

        return "conflict"

    # No existing identity was found, so create the alias.
    result = (
        alias_supabase
        .table("player_aliases")
        .insert({
            "player_id": player_id,
            "game_name": cleaned_name,
            "server_number": server_number,
        })
        .execute()
    )

    if result.data:
        return "created"

    return None

# ════════════════════════════════════════════════════════════════════════════════
# PLAYER / OWNERSHIP HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def get_current_player_gamename(player_id):
    """
    Get the player's current canonical gamename from the players table.

    The player_id is the stable identity. The gamename is what the
    existing ownership table uses as its foreign-key value.
    """

    player_supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )

    rows = (
        player_supabase
        .table("players")
        .select("id, gamename")
        .eq("id", player_id)
        .limit(1)
        .execute()
        .data
        or []
    )

    if not rows:
        return None

    return rows[0].get("gamename")


def load_owned_blossoms(gamename):
    """
    Load the blossoms currently owned by a florist.

    Returns a set so duplicate checks are fast.
    """

    ownership_supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )

    rows = (
        ownership_supabase
        .table("ownership")
        .select("blossom")
        .eq("gamename", gamename)
        .execute()
        .data
        or []
    )

    return {
        row["blossom"]
        for row in rows
        if row.get("blossom")
    }

# ════════════════════════════════════════════════════════════════════════════════
# BLOSSOM RESOLUTION HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def load_blossom_names():
    """
    Load all canonical blossom names from the blossoms table.

    A fresh Supabase client is used for this read because the
    shared client's HTTP connection can occasionally terminate
    between consecutive synchronous requests.
    """

    blossom_supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )

    return [
        row["name"]
        for row in (
            blossom_supabase
            .table("blossoms")
            .select("name")
            .execute()
            .data
            or []
        )
        if row.get("name")
    ]

def normalize_blossom_name(value: str) -> str:
    """
    Normalize blossom text for comparison.

    Task-log entries may begin with the harvest quantity:

        280 Peach Blossom Jacquelyn
        560 Taro Purple Gladiolus

    The quantity is not part of the blossom name, so remove it
    before comparing against the canonical blossoms table.

    This does NOT modify the parser output or the canonical
    database name. It only creates a comparison-friendly version.
    """

    if not value:
        return ""

    value = str(value).strip().lower()

    # Remove the harvest quantity at the beginning.
    #
    # Examples:
    #     "280 Peach Blossom Jacquelyn"
    #     "560 Taro Purple Gladiolus"
    #
    # Both become:
    #     "Peach Blossom Jacquelyn"
    value = re.sub(
        r"^\d+\s*[:\-]?\s*",
        "",
        value,
    )

    # Treat punctuation/OCR separators as spaces.
    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    # Collapse repeated whitespace.
    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


def resolve_blossom(blossom_text, blossom_names=None):
    """
    Resolve OCR flower text against the canonical blossoms table.

    Exact normalized matches are accepted immediately.

    If there is no exact match, compare the OCR text against every
    canonical blossom using character-sequence similarity.

    Returns:
        {
            "blossom": str,
            "score": float,
            "match_type": "exact" | "fuzzy",
            "needs_review": bool,
        }

    Returns None when no blossom names are available.

    Confidence rules are intentionally conservative:
        >= 0.90  -> confident fuzzy match
        0.80-0.89 -> possible match; staff review required
        < 0.80 -> unresolved
    """

    from difflib import SequenceMatcher

    if not blossom_text:
        return None

    if blossom_names is None:
        blossom_names = load_blossom_names()

    if not blossom_names:
        return None

    normalized_input = normalize_blossom_name(
        blossom_text
    )

    if not normalized_input:
        return None

    # ---------------------------------------------------------
    # Exact normalized match
    # ---------------------------------------------------------

    exact_matches = [
        name
        for name in blossom_names
        if normalize_blossom_name(name) == normalized_input
    ]

    if len(exact_matches) == 1:
        return {
            "blossom": exact_matches[0],
            "score": 1.0,
            "match_type": "exact",
            "needs_review": False,
        }

    # ---------------------------------------------------------
    # Fuzzy comparison
    # ---------------------------------------------------------

    scored = []

    for name in blossom_names:

        normalized_candidate = normalize_blossom_name(
            name
        )

        if not normalized_candidate:
            continue

        score = SequenceMatcher(
            None,
            normalized_input,
            normalized_candidate,
        ).ratio()

        scored.append(
            (score, name)
        )

    if not scored:
        return None

    # Highest score first.
    scored.sort(
        key=lambda item: item[0],
        reverse=True
    )

    best_score, best_name = scored[0]

    # ---------------------------------------------------------
    # No sufficiently plausible match
    # ---------------------------------------------------------

    if best_score < 0.80:
        return {
            "blossom": None,
            "score": best_score,
            "match_type": "unresolved",
            "needs_review": True,
        }

    # ---------------------------------------------------------
    # Determine whether the best match is sufficiently ahead
    # of the second-best candidate.
    #
    # We don't want:
    #
    #     Candidate A = 86%
    #     Candidate B = 85%
    #
    # to be silently accepted.
    # ---------------------------------------------------------

    second_score = (
        scored[1][0]
        if len(scored) > 1
        else 0.0
    )

    score_gap = best_score - second_score

    needs_review = (
        best_score < 0.90
        or score_gap < 0.05
    )

    return {
        "blossom": best_name,
        "score": best_score,
        "match_type": "fuzzy",
        "needs_review": needs_review,
    }

# ════════════════════════════════════════════════════════════════════════════════
# TASK-LOG PLAYER REVIEW
# ════════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════════
# TASK-LOG PLAYER REVIEW
# ════════════════════════════════════════════════════════════════════════════════

def load_current_players():
    """
    Load current florists for player identification.

    Returns player records using the stable player ID.
    """

    player_supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )

    return (
        player_supabase
        .table("players")
        .select("id, gamename")
        .order("gamename")
        .execute()
        .data
        or []
    )


def save_player_alias(
    player_id,
    game_name,
    server_number
):
    """
    Save an OCR-discovered game identity as an alias.

    If the exact game_name/server combination already exists,
    do not create a duplicate.
    """

    alias_supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )

    existing = (
        alias_supabase
        .table("player_aliases")
        .select("id, player_id")
        .eq("game_name", game_name)
        .eq("server_number", server_number)
        .limit(1)
        .execute()
        .data
        or []
    )

    if existing:
        return False

    (
        alias_supabase
        .table("player_aliases")
        .insert({
            "player_id": player_id,
            "game_name": game_name,
            "server_number": server_number
        })
        .execute()
    )

    return True

class TaskBlossomSearchModal(discord.ui.Modal):

    def __init__(
        self,
        review_view,
        unknown_entry
    ):
        super().__init__(
            title="Identify Flower"
        )

        self.review_view = review_view
        self.unknown_entry = unknown_entry

        lookup_name = (
            unknown_entry.get("task_text")
            or "Unknown flower"
        )

        self.flower_name = discord.ui.TextInput(
            label="Flower needing review",
            placeholder=(
                "Enter part of the flower name"
            ),
            required=True,
            max_length=100
        )

        self.add_item(
            self.flower_name
        )

    async def on_submit(
        self,
        interaction: discord.Interaction
    ):
        search_text = str(
            self.flower_name.value
        ).strip().lower()

        if not search_text:
            await interaction.response.send_message(
                "❌ Please enter a flower name or part of a flower name.",
                ephemeral=True
            )
            return

        matches = [
            blossom
            for blossom in self.review_view.blossom_names
            if search_text in blossom.lower()
        ]

        if not matches:
            await interaction.response.send_message(
                f"❌ No flowers found matching "
                f"`{self.flower_name.value}`.\n\n"
                "Try entering a different part of the flower's name.",
                ephemeral=True
            )
            return

        if len(matches) > 3:
            await interaction.response.send_message(
                f"🔎 That search found **{len(matches)} flowers**.\n\n"
                "Please type more letters to narrow the results.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🔍 **Flowers matching "
            f"`{self.flower_name.value}`:**\n\n"
            "Choose the correct flower:",
            view=TaskBlossomMatchView(
                self.review_view,
                self.unknown_entry,
                matches
            ),
            ephemeral=True
        )

class TaskBlossomMatchView(discord.ui.View):

    def __init__(
        self,
        review_view,
        unknown_entry,
        matches
    ):
        super().__init__(timeout=600)

        self.review_view = review_view
        self.unknown_entry = unknown_entry
        self.matches = matches

        for blossom in matches:

            button = discord.ui.Button(
                label=blossom[:80],
                style=discord.ButtonStyle.primary
            )

            async def callback(
                interaction,
                blossom_name=blossom
            ):
                await self.choose_blossom(
                    interaction,
                    blossom_name
                )

            button.callback = callback

            self.add_item(button)

    async def choose_blossom(
        self,
        interaction: discord.Interaction,
        selected_blossom
    ):
        await interaction.response.defer(
            ephemeral=True
        )

        task_text = (
            self.unknown_entry.get("task_text")
            or ""
        )

        resolution_key = normalize_blossom_name(
            task_text
        ).lower()

        self.review_view.session.pending_blossom_resolutions[
            resolution_key
        ] = selected_blossom

        self.review_view.pending_blossom_resolutions[
            resolution_key
        ] = selected_blossom

        await interaction.followup.send(
            f"🌸 **Got it!** "
            f"`{task_text}` → **{selected_blossom}**\n\n"
            "The flower will be treated as **"
            f"{selected_blossom}** for this import only.",
            ephemeral=True
        )

        await interaction.followup.send(
            "🔄 **Resuming the import preview...**",
            ephemeral=True
        )

        await run_importtasklog(
            interaction,
            self.review_view.session
        )

class TaskBlossomReviewView(discord.ui.View):
    """
    Queue of unresolved blossoms.

    Only one unresolved flower is handled at a time.

    Manual blossom selections are kept temporarily in the
    import session and are never written to Supabase.
    """

    def __init__(
        self,
        blossom_names,
        unknown_entries,
        session
    ):
        super().__init__(timeout=600)

        self.blossom_names = blossom_names
        self.unknown_entries = list(
            unknown_entries
        )
        self.session = session

        self.pending_blossom_resolutions = (
            session.pending_blossom_resolutions
        )

        self.identify_button = discord.ui.Button(
            label="Identify Flower",
            emoji="🌸",
            style=discord.ButtonStyle.primary
        )

        self.identify_button.callback = (
            self.open_search
        )

        self.add_item(
            self.identify_button
        )

    async def open_search(
        self,
        interaction: discord.Interaction
    ):
        if not self.unknown_entries:

            await interaction.response.send_message(
                "✅ All flowers have already been identified.",
                ephemeral=True
            )
            return

        unknown_entry = (
            self.unknown_entries[0]
        )

        await interaction.response.send_modal(
            TaskBlossomSearchModal(
                self,
                unknown_entry
            )
        )

class TaskPlayerSearchModal(discord.ui.Modal):

    def __init__(self, review_view, unknown_entry):
        super().__init__(title="Identify Player")

        self.review_view = review_view
        self.unknown_entry = unknown_entry

        lookup_name = (
            unknown_entry.get("ocr_player")
            or unknown_entry.get("game_name")
            or "Unknown"
        )

        server_number = unknown_entry.get("server_number")

        self.name_display = discord.ui.TextInput(
            label=f"Name needing review: {lookup_name}"[:45],
            placeholder=(
                f"Identify `{lookup_name}` / s{server_number}"
                if server_number is not None
                else f"Identify `{lookup_name}`"
            ),
            required=False,
            max_length=100
        )

        self.florist_name = self.name_display

        self.add_item(self.florist_name)

    async def on_submit(self, interaction):

        search_text = str(
            self.florist_name.value
        ).strip().lower()

        if not search_text:
            await interaction.response.send_message(
                "❌ Please enter a florist name or part of a florist name.",
                ephemeral=True
            )
            return

        matches = [
            player
            for player in self.review_view.players
            if search_text in player["gamename"].lower()
        ]

        if not matches:

            await interaction.response.send_message(
                f"❌ No florists found matching `{self.florist_name.value}`.\n\n"
                "Try entering a different part of the florist's name.",
                ephemeral=True
            )
            return

        if len(matches) > 25:

            await interaction.response.send_message(
                f"🔎 That search found **{len(matches)} florists**.\n\n"
                "Please type more letters to narrow the results.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🔍 **Florists matching `{self.florist_name.value}`:**\n\n"
            "Choose the correct florist:",
            view=TaskPlayerMatchView(
                self.review_view,
                matches
            ),
            ephemeral=True
        )

class TaskPlayerMatchView(discord.ui.View):
    """
    Shows the florist search results as buttons.

    This view is intentionally limited to 25 matches because
    Discord limits a row of selectable options/buttons.
    """

    def __init__(
        self,
        review_view,
        matches
    ):
        super().__init__(timeout=300)

        self.review_view = review_view
        self.matches = matches

        for player in matches:
            button = discord.ui.Button(
                label=player["gamename"][:80],
                style=discord.ButtonStyle.primary
            )

            async def select_player(
                interaction,
                player_id=player["id"]
            ):
                await self.choose_player(
                    interaction,
                    player_id
                )

            button.callback = select_player
            self.add_item(button)

    async def choose_player(
        self,
        interaction: discord.Interaction,
        player_id
    ):
        await interaction.response.defer(
            ephemeral=True
        )
    
        selected_player = next(
            (
                player
                for player in self.matches
                if player["id"] == player_id
            ),
            None
        )
    
        if not selected_player:
            await interaction.followup.send(
                "❌ Could not find that florist.",
                ephemeral=True
            )
            return
    
        if not self.review_view.unknown_entries:
            await interaction.followup.send(
                "❌ There are no unresolved players remaining.",
                ephemeral=True
            )
            return
    
        # Resolve the FIRST unresolved player in the queue.
        unknown_entry = (
            self.review_view.unknown_entries[0]
        )
    
        lookup_name = (
            unknown_entry.get("ocr_player")
            or unknown_entry.get("game_name")
        )
    
        server_number = unknown_entry.get(
            "server_number"
        )
    
        # -----------------------------------------------------
        # Check whether this exact alias already exists in the
        # aliases loaded for this import session.
        #
        # IMPORTANT:
        # We do NOT write anything to Supabase here.
        # -----------------------------------------------------
    
        existing_alias = next(
            (
                alias
                for alias in self.review_view.player_aliases
                if alias.get("game_name") == lookup_name
                and alias.get("server_number") == server_number
            ),
            None
        )
    
        if existing_alias:
            if existing_alias["player_id"] != selected_player["id"]:
                await interaction.followup.send(
                    f"🚨 **Alias conflict!**\n\n"
                    f"`{lookup_name}` / s{server_number} "
                    "is already associated with a different florist.\n\n"
                    "No alias was changed. Please investigate this identity "
                    "before continuing.",
                    ephemeral=True
                )
                return
    
            alias_message = (
                f"✅ `{lookup_name}` / s{server_number} "
                f"is already known as an alias for "
                f"**{selected_player['gamename']}**."
            )
    
        else:
            # -------------------------------------------------
            # This is a NEW alias for this import session.
            #
            # Keep it in memory only.
            # -------------------------------------------------
    
            pending_alias = {
                "player_id": selected_player["id"],
                "game_name": lookup_name,
                "server_number": server_number
            }
    
            self.review_view.pending_aliases.append(
                pending_alias
            )
    
            # Add it to the in-memory resolver data so that
            # the resumed import can immediately use it.
            self.review_view.player_aliases.append(
                pending_alias
            )
    
            alias_message = (
                f"📝 Temporarily mapped `{lookup_name}` / s{server_number} "
                f"to **{selected_player['gamename']}**.\n"
                f"*(This alias will be saved only if the import is confirmed.)*"
            )
    
        # Remove this entry from the unresolved queue.
        self.review_view.unknown_entries.pop(0)
    
        # -----------------------------------------------------
        # More unresolved players remain.
        # -----------------------------------------------------
    
        if self.review_view.unknown_entries:
    
            next_entry = (
                self.review_view.unknown_entries[0]
            )
    
            next_name = (
                next_entry.get("ocr_player")
                or next_entry.get("game_name")
            )
    
            next_server = next_entry.get(
                "server_number"
            )
    
            await interaction.followup.send(
                (
                    f"{alias_message}\n\n"
                    f"🔍 **Next unresolved player:**\n"
                    f"`{next_name}` / s{next_server}\n\n"
                    f"Click **Identify Player** to continue."
                ),
                view=self.review_view,
                ephemeral=True
            )
    
            return
    
        # -----------------------------------------------------
        # All unknown players have now been resolved.
        # -----------------------------------------------------
    
        if (
            self.review_view.resume_command == "importtasklog"
            and self.review_view.session is not None
        ):
    
            await interaction.followup.send(
                f"{alias_message}\n\n"
                f"🎉 **All unknown players have been identified!**\n\n"
                f"🔄 **Resuming the import preview...**",
                ephemeral=True
            )
    
            await run_importtasklog(
                interaction,
                self.review_view.session
            )
    
        else:
    
            await interaction.followup.send(
                f"{alias_message}\n\n"
                f"🎉 **All unknown players have been identified!**\n\n"
                f"Run `/{self.review_view.resume_command}` again with the same screenshot "
                f"to continue the import test.",
                ephemeral=True
            )


class TaskPlayerReviewView(discord.ui.View):
    """
    Queue of unresolved players.

    Only one unresolved player is handled at a time.

    Player aliases selected during an import are kept temporarily
    and are not written to Supabase until the import is confirmed.
    """

    def __init__(
        self,
        player_aliases,
        unknown_entries,
        base_output,
        resume_command="testimportresolve",
        session=None,
    ):
        super().__init__(timeout=600)

        self.player_aliases = player_aliases
        self.unknown_entries = list(unknown_entries)
        self.base_output = base_output
        self.resume_command = resume_command
        self.session = session

        # Aliases proposed during THIS import.
        # These are not written to Supabase until Confirm Import.
        if self.session is not None:
            self.pending_aliases = (
                self.session.pending_aliases
            )
        else:
            self.pending_aliases = []

        self.players = load_current_players()

        self.pending_matches = []

        self.identify_button = discord.ui.Button(
            label="Identify Player",
            emoji="🔍",
            style=discord.ButtonStyle.primary
        )

        self.identify_button.callback = (
            self.open_search
        )

        self.add_item(
            self.identify_button
        )

    async def open_search(
        self,
        interaction: discord.Interaction
    ):
        if not self.unknown_entries:
            await interaction.response.send_message(
                "✅ All players have already been identified.",
                ephemeral=True
            )
            return

        unknown_entry = self.unknown_entries[0]

        await interaction.response.send_modal(
            TaskPlayerSearchModal(
                self,
                unknown_entry
            )
        )

class TaskImportConfirmView(discord.ui.View):

    def __init__(
        self,
        pending_imports,
        pending_aliases=None
    ):
        super().__init__(timeout=600)

        self.pending_imports = list(pending_imports)
        self.pending_aliases = (
            list(pending_aliases)
            if pending_aliases
            else []
        )
        self.finished = False

        if self.pending_imports:
            button_label = (
                f"Import {len(self.pending_imports)} Blossoms"
            )
            button_emoji = "🌸"
        
        elif self.pending_aliases:
            button_label = "Save Player Aliases"
            button_emoji = "👤"
        
        else:
            button_label = "Confirm Import"
            button_emoji = "✅"
        
        confirm_button = discord.ui.Button(
            label=button_label,
            emoji=button_emoji,
            style=discord.ButtonStyle.success
        )

        cancel_button = discord.ui.Button(
            label="Cancel",
            emoji="❌",
            style=discord.ButtonStyle.secondary
        )

        confirm_button.callback = self.confirm_import
        cancel_button.callback = self.cancel_import

        self.add_item(confirm_button)
        self.add_item(cancel_button)

    async def confirm_import(
        self,
        interaction: discord.Interaction
    ):
        if self.finished:
            await interaction.response.send_message(
                "⚠️ This import has already been completed or cancelled.",
                ephemeral=True
            )
            return

        self.finished = True

        await interaction.response.defer(ephemeral=True)

        imported = []
        skipped = []
        failed = []
        aliases_saved = []
        aliases_failed = []

        # -----------------------------------------------------
        # Save aliases proposed during this import.
        #
        # These are the ONLY alias changes made by the import.
        # -----------------------------------------------------

        for alias in self.pending_aliases:

            try:
                alias_supabase = create_client(
                    SUPABASE_URL,
                    SUPABASE_KEY
                )

                alias_query = (
                    alias_supabase
                    .table("player_aliases")
                    .select("id, player_id")
                    .eq("game_name", alias["game_name"])
                )
                
                if alias["server_number"] is None:
                    alias_query = alias_query.is_(
                        "server_number",
                        "null"
                    )
                else:
                    alias_query = alias_query.eq(
                        "server_number",
                        alias["server_number"]
                    )
                
                existing_alias = (
                    alias_query
                    .limit(1)
                    .execute()
                    .data
                    or []
                )

                if existing_alias:

                    if (
                        existing_alias[0]["player_id"]
                        == alias["player_id"]
                    ):
                        aliases_saved.append(
                            f"{alias['game_name']} / "
                            f"s{alias['server_number']}"
                        )
                    else:
                        aliases_failed.append(
                            f"{alias['game_name']} / "
                            f"s{alias['server_number']} "
                            "(alias conflict)"
                        )

                    continue

                result = (
                    alias_supabase
                    .table("player_aliases")
                    .insert({
                        "player_id": alias["player_id"],
                        "game_name": alias["game_name"],
                        "server_number": alias["server_number"]
                    })
                    .execute()
                )

                if result.data:
                    aliases_saved.append(
                        f"{alias['game_name']} / "
                        f"s{alias['server_number']}"
                    )
                else:
                    aliases_failed.append(
                        f"{alias['game_name']} / "
                        f"s{alias['server_number']}"
                    )

            except Exception as e:

                print(
                    "ERROR SAVING IMPORT ALIAS:",
                    alias,
                    repr(e)
                )

                aliases_failed.append(
                    f"{alias['game_name']} / "
                    f"s{alias['server_number']}"
                )

        # -----------------------------------------------------
        # Existing ownership import.
        # -----------------------------------------------------

        for item in self.pending_imports:

            gamename = item["gamename"]
            blossom = item["blossom"]

            try:

                current_supabase = create_client(
                    SUPABASE_URL,
                    SUPABASE_KEY
                )

                # Make sure the florist still exists.
                player_rows = (
                    current_supabase
                    .table("players")
                    .select("id")
                    .eq("gamename", gamename)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )

                if not player_rows:

                    failed.append(
                        f"{gamename} — {blossom} "
                        f"(florist no longer exists)"
                    )
                    continue

                # -------------------------------------------------
                # Re-check ownership.
                # -------------------------------------------------

                existing = (
                    current_supabase
                    .table("ownership")
                    .select("id")
                    .eq("gamename", gamename)
                    .eq("blossom", blossom)
                    .limit(1)
                    .execute()
                    .data
                    or []
                )

                if existing:

                    skipped.append(
                        f"{gamename} — {blossom} "
                        f"(already owned)"
                    )
                    continue

                # -------------------------------------------------
                # Insert ownership.
                #
                # New imported flowers receive no bonus.
                # -------------------------------------------------

                current_supabase.table("ownership").insert({
                    "gamename": gamename,
                    "blossom": blossom,
                    "bonus": None
                }).execute()

                imported.append(
                    f"{gamename} — {blossom}"
                )

            except Exception as e:

                print(
                    "ERROR IMPORTING OWNERSHIP:",
                    gamename,
                    blossom,
                    repr(e)
                )

                failed.append(
                    f"{gamename} — {blossom}"
                )

        # ---------------------------------------------------------
        # Build final result.
        # ---------------------------------------------------------

        lines = [
            "🌸 **Blossom import complete!**"
        ]

        lines.append("")
        lines.append(
            f"✅ Imported: **{len(imported)}**"
        )

        lines.append(
            f"⚠️ Skipped: **{len(skipped)}**"
        )

        lines.append(
            f"❌ Failed: **{len(failed)}**"
        )

        lines.append(
            f"👤 Aliases saved: **{len(aliases_saved)}**"
        )

        lines.append(
            f"⚠️ Alias failures: **{len(aliases_failed)}**"
        )

        if aliases_saved:
            lines.append("")
            lines.append("**Aliases saved:**")

            for alias in aliases_saved:
                lines.append(
                    f"👤 {alias}"
                )

        if aliases_failed:
            lines.append("")
            lines.append("**Alias failures:**")

            for alias in aliases_failed:
                lines.append(
                    f"❌ {alias}"
                )

        if imported:

            lines.append("")
            lines.append("**Imported:**")

            for item in imported:
                lines.append(
                    f"🌸 {item}"
                )

        if skipped:

            lines.append("")
            lines.append("**Skipped:**")

            for item in skipped:
                lines.append(
                    f"⚠️ {item}"
                )

        if failed:

            lines.append("")
            lines.append("**Failed:**")

            for item in failed:
                lines.append(
                    f"❌ {item}"
                )

        # Disable the buttons after completion.
        for child in self.children:
            child.disabled = True

        await interaction.followup.send(
            "\n".join(lines)[:1900],
            ephemeral=True
        )

    async def cancel_import(
        self,
        interaction: discord.Interaction
    ):
        if self.finished:
            await interaction.response.send_message(
                "⚠️ This import has already been completed or cancelled.",
                ephemeral=True
            )
            return

        self.finished = True

        for child in self.children:
            child.disabled = True

        await interaction.response.send_message(
            "❌ **Import cancelled.**\n\n"
            "No ownership or player-alias records were changed.",
            ephemeral=True
        )

# ════════════════════════════════════════════════════════════════════════════════
# PAGINATED VIEW
# ════════════════════════════════════════════════════════════════════════════════

class PaginatedView(discord.ui.View):
    def __init__(self, lines: list[str], title: str, footer_total: str, ephemeral: bool = False, refresh_callback=None):
        super().__init__(timeout=None)
        print("PAGINATED VIEW: timeout=None")
        self.lines           = lines
        self.title           = title
        self.footer_total    = footer_total
        self.ephemeral       = ephemeral
        self.refresh_callback = refresh_callback
        self.done_players = []
        self.page          = 0
        self.total_pages   = max(1, (len(lines) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._update_components()

    def _page_lines(self) -> list[str]:
        start = self.page * PAGE_SIZE
        return self.lines[start:start + PAGE_SIZE]

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            description="\n".join(self._page_lines()),
            color=PINK,
        )

        if hasattr(self, "done_players") and self.done_players:
            embed.add_field(
                name="✅ Florists marked as done",
                value="\n".join(f"• {p}" for p in sorted(self.done_players)),
                inline=False,
            )

        embed.set_footer(text=f"{self.footer_total} · Page {self.page + 1}/{self.total_pages}")
        return embed

    def _update_components(self):
        self.clear_items()

        # Navigation buttons
        first  = discord.ui.Button(emoji="⏮️", style=discord.ButtonStyle.grey,  custom_id="first",  disabled=self.page == 0)
        prev   = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.grey,  custom_id="prev",   disabled=self.page == 0)
        page_b = discord.ui.Button(label=f"{self.page + 1}/{self.total_pages}", style=discord.ButtonStyle.blurple, custom_id="page", disabled=True)
        nxt    = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.grey,  custom_id="next",   disabled=self.page >= self.total_pages - 1)
        last   = discord.ui.Button(emoji="⏭️", style=discord.ButtonStyle.grey,  custom_id="last",   disabled=self.page >= self.total_pages - 1)

        refresh = discord.ui.Button(
            emoji="🔄",
            style=discord.ButtonStyle.grey,
            custom_id="refresh",
        )
        
        first.callback   = self._first
        prev.callback    = self._prev
        nxt.callback     = self._next
        last.callback    = self._last
        refresh.callback = self._refresh

        self.add_item(first)
        self.add_item(prev)
        self.add_item(page_b)
        self.add_item(nxt)
        self.add_item(last)

        if self.refresh_callback:
            self.add_item(refresh)

        # Select menu — blossom names on current page (strip formatting)
        page_lines = self._page_lines()
        options = []
        for line in page_lines:
            # Extract plain blossom name from "icon **Name** — X pts  ..."
            clean = line
            for ch in ["🔴","🟠","🟡","🟢","🔵","🟣","⚪"]:
                clean = clean.replace(ch, "")
            # Strip discord custom emoji <:name:id>
            import re
            clean = re.sub(r"<:[^:]+:\d+>", "", clean)
            clean = clean.strip()
            # Extract just the blossom name between ** **
            match = re.search(r"\*\*(.+?)\*\*", clean)
            name = match.group(1) if match else clean[:100]
            options.append(discord.SelectOption(label=name[:100], value=name[:100]))

        if options:
            select = discord.ui.Select(
                placeholder="🌸 Look up a blossom on this page...",
                options=options,
                custom_id="blossom_select",
            )
            select.callback = self._select_blossom
            self.add_item(select)

    async def _go_to(self, interaction: discord.Interaction, page: int):
        await interaction.response.defer()
    
        self.page = page
        self._update_components()
    
        await interaction.edit_original_response(
            embed=self._build_embed(),
            view=self
        )

    async def _first(self, interaction: discord.Interaction):
        await self._go_to(interaction, 0)

    async def _prev(self, interaction: discord.Interaction):
        await self._go_to(interaction, max(0, self.page - 1))

    async def _next(self, interaction: discord.Interaction):
        await self._go_to(interaction, min(self.total_pages - 1, self.page + 1))

    async def _last(self, interaction: discord.Interaction):
        await self._go_to(interaction, self.total_pages - 1)

    async def _refresh(self, interaction: discord.Interaction):
        if not self.refresh_callback:
            return

        await interaction.response.defer()

        result = await asyncio.to_thread(self.refresh_callback)

        if not result:
            return

        self.lines, self.title, self.footer_total, self.done_players = result

        self.total_pages = max(1, (len(self.lines) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = min(self.page, self.total_pages - 1)

        self._update_components()

        await interaction.edit_original_response(
            embed=self._build_embed(),
            view=self,
        )
        
    async def _select_blossom(self, interaction: discord.Interaction):
        name = interaction.data["values"][0]
        bl = supabase.table("blossoms").select("*").eq("name", name).execute()
        if not bl.data:
            await interaction.response.send_message(
                f"❌ Could not find **{name}**.", ephemeral=True
            )
            return
        b = bl.data[0]
        rarity_raw     = b.get("rarity", "")
        rarity_display = RARITY_DISPLAY.get(rarity_raw, rarity_raw)

        embed = discord.Embed(title=f"🌸 {b['name']}", color=PINK)
        embed.add_field(name="Rarity", value=f"{rarity_icon(rarity_raw)} {rarity_display}", inline=True)
        embed.add_field(name="Points", value=str(b["points"]),   inline=True)
        embed.add_field(name="Source", value=b["source"] or "—", inline=True)

        if b.get("thumbnail_url"):
            embed.set_thumbnail(url=b["thumbnail_url"])

        all_vases = supabase.table("vases").select("name, " + ", ".join(VASE_SLOTS)).execute()
        matching = sorted(v["name"] for v in all_vases.data if name in [v.get(s) for s in VASE_SLOTS])
        embed.add_field(
            name="Found In Vases",
            value="\n".join(f"🏺 {v}" for v in matching) if matching else "Not part of any vase yet.",
            inline=False,
        )

        owners = supabase.table("ownership").select("gamename, bonus").eq("blossom", name).execute()
        if owners.data:
            lines = []
            for row in owners.data:
                line = f"🌿 {row['gamename']}"
                if row.get("bonus"):
                    line += f"  {bonus_icon(row['bonus'])} +{row['bonus']}"
                lines.append(line)
            embed.add_field(name=f"Florists ({len(owners.data)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Florists", value="Nobody owns this blossom yet.", inline=False)

        if b.get("image_url"):
            embed.set_image(url=b["image_url"])

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


async def send_paginated(
    interaction,
    lines: list[str],
    title: str,
    footer_total: str,
    ephemeral: bool = False,
    refresh_callback=None,
):
    view = PaginatedView(
        lines,
        title,
        footer_total,
        ephemeral,
        refresh_callback,
    )
    embed = view._build_embed()
    await interaction.followup.send(
        embed=embed,
        view=view,
        ephemeral=ephemeral,
    )


# ════════════════════════════════════════════════════════════════════════════════
# AUTOCOMPLETE
# ════════════════════════════════════════════════════════════════════════════════

async def florist_autocomplete(interaction: discord.Interaction, current: str):
    res = supabase.table("players").select("gamename").ilike("gamename", f"{current}%").limit(10).execute()
    return [app_commands.Choice(name=r["gamename"], value=r["gamename"]) for r in res.data]


async def blossom_autocomplete(interaction: discord.Interaction, current: str):
    res = supabase.table("blossoms").select("name").ilike("name", f"{current}%").limit(10).execute()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in res.data]


async def vase_autocomplete(interaction: discord.Interaction, current: str):
    res = supabase.table("vases").select("name").ilike("name", f"{current}%").limit(10).execute()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in res.data]


# ════════════════════════════════════════════════════════════════════════════════
# FLORIST COMMANDS — anyone can use
# ════════════════════════════════════════════════════════════════════════════════

@tree.command(name="addflorist", description="Add a new florist to the database")
@app_commands.describe(gamename="The florist's in-game name")
async def add_florist(interaction: discord.Interaction, gamename: str):
    gamename = gamename.strip()
    if supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(f"🌿 **{gamename}** is already registered as a florist!", ephemeral=True)
        return
    supabase.table("players").insert({"gamename": gamename, "done": False}).execute()
    await interaction.response.send_message(f"🌱 **{gamename}** has been added as a florist!", ephemeral=True)

@tree.command(name="updateflorist", description="[Admin] Correct a florist's in-game name")
@app_commands.describe(
    gamename="The florist's current in-game name",
    new_name="The corrected in-game name",
)
@app_commands.autocomplete(gamename=florist_autocomplete)
async def update_florist(
    interaction: discord.Interaction,
    gamename: str,
    new_name: str,
):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "🚫 Only admins can update florist names.",
            ephemeral=True,
        )
        return

    gamename = gamename.strip()
    new_name = new_name.strip()

    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(
            f"❌ No florist named **{gamename}** was found.",
            ephemeral=True,
        )
        return

    if not new_name:
        await interaction.response.send_message(
            "❌ The new florist name cannot be blank.",
            ephemeral=True,
        )
        return

    if new_name == gamename:
        await interaction.response.send_message(
            "❌ The new name is the same as the current name.",
            ephemeral=True,
        )
        return

    if supabase.table("players").select("id").eq("gamename", new_name).execute().data:
        await interaction.response.send_message(
            f"❌ A florist named **{new_name}** is already registered.",
            ephemeral=True,
        )
        return

    result = (
        supabase.table("players")
        .update({"gamename": new_name})
        .eq("gamename", gamename)
        .execute()
    )

    if not result.data:
        await interaction.response.send_message(
            f"❌ Something went wrong while updating **{gamename}**.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🌿 Florist name corrected!\n"
        f"**{gamename}** → **{new_name}**\n"
        f"🌱 Their blossom ownership has been preserved.",
        ephemeral=True,
    )

@tree.command(name="removeflorist", description="Remove a florist and all their blossoms")
@app_commands.describe(gamename="The florist's in-game name")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def remove_florist(interaction: discord.Interaction, gamename: str):
    gamename = gamename.strip()
    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(f"❌ No florist named **{gamename}** was found.", ephemeral=True)
        return
    supabase.table("ownership").delete().eq("gamename", gamename).execute()
    supabase.table("players").delete().eq("gamename", gamename).execute()
    await interaction.response.send_message(f"🍂 **{gamename}** and all their blossoms have been removed.", ephemeral=True)


# ════════════════════════════════════════════════════════════════════════════════
# OWNERSHIP COMMANDS — anyone can use
# ════════════════════════════════════════════════════════════════════════════════

@tree.command(name="add", description="Add up to 10 blossoms to a florist's hoard at once")
@app_commands.describe(
    gamename="The florist's in-game name",
    blossom1="Blossom 1",
    blossom2="Blossom 2 (optional)",  blossom3="Blossom 3 (optional)",
    blossom4="Blossom 4 (optional)",  blossom5="Blossom 5 (optional)",
    blossom6="Blossom 6 (optional)",  blossom7="Blossom 7 (optional)",
    blossom8="Blossom 8 (optional)",  blossom9="Blossom 9 (optional)",
    blossom10="Blossom 10 (optional)",
    bonus="Optional bonus for all listed blossoms: 1 for +1, 2 for +2",
)
@app_commands.autocomplete(
    gamename=florist_autocomplete,
    blossom1=blossom_autocomplete,  blossom2=blossom_autocomplete,
    blossom3=blossom_autocomplete,  blossom4=blossom_autocomplete,
    blossom5=blossom_autocomplete,  blossom6=blossom_autocomplete,
    blossom7=blossom_autocomplete,  blossom8=blossom_autocomplete,
    blossom9=blossom_autocomplete,  blossom10=blossom_autocomplete,
)
async def add_ownership(
    interaction: discord.Interaction,
    gamename: str,
    blossom1: str,
    blossom2: str | None = None,  blossom3: str | None = None,
    blossom4: str | None = None,  blossom5: str | None = None,
    blossom6: str | None = None,  blossom7: str | None = None,
    blossom8: str | None = None,  blossom9: str | None = None,
    blossom10: str | None = None,
    bonus: int | None = None,
):
    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(f"❌ No florist named **{gamename}**. Use `/addflorist` first.", ephemeral=True)
        return
    if bonus is not None and bonus not in (1, 2):
        await interaction.response.send_message("❌ Bonus must be 1 or 2.", ephemeral=True)
        return

    requested = [b for b in [blossom1, blossom2, blossom3, blossom4, blossom5,
                              blossom6, blossom7, blossom8, blossom9, blossom10] if b]
    valid_names = {r["name"] for r in supabase.table("blossoms").select("name").in_("name", requested).execute().data}
    invalid = [b for b in requested if b not in valid_names]
    if invalid:
        await interaction.response.send_message(f"❌ These blossoms aren't in the database: {', '.join(invalid)}", ephemeral=True)
        return

    already = {r["blossom"] for r in supabase.table("ownership").select("blossom").eq("gamename", gamename).in_("blossom", requested).execute().data}
    to_add = [b for b in requested if b not in already]
    if to_add:
        supabase.table("ownership").insert([{"gamename": gamename, "blossom": b, "bonus": bonus} for b in to_add]).execute()

    lines = []
    if to_add:
        bonus_str = f" (bonus: +{bonus})" if bonus else ""
        lines.append(f"🌱 Added{bonus_str}:\n" + "\n".join(f"• {b}" for b in to_add))
    if already:
        lines.append(f"⚠️ Already in hoard (skipped):\n" + "\n".join(f"• {b}" for b in already))
    await interaction.response.send_message(f"**{gamename}'s hoard update:**\n" + "\n".join(lines), ephemeral=True)


@tree.command(name="remove", description="Remove a blossom from a florist's hoard")
@app_commands.describe(gamename="The florist's in-game name", blossom="The blossom to remove")
@app_commands.autocomplete(gamename=florist_autocomplete, blossom=blossom_autocomplete)
async def remove_ownership(interaction: discord.Interaction, gamename: str, blossom: str):
    res = supabase.table("ownership").delete().eq("gamename", gamename).eq("blossom", blossom).execute()
    if res.data:
        await interaction.response.send_message(f"🍂 Removed **{blossom}** from **{gamename}**'s hoard.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ **{gamename}** doesn't have **{blossom}** in their hoard.", ephemeral=True)


@tree.command(name="setbonus", description="Set or update the bonus for a florist's blossom")
@app_commands.describe(gamename="The florist's in-game name", blossom="The blossom to update", bonus="Bonus value: 1 for +1, 2 for +2")
@app_commands.autocomplete(gamename=florist_autocomplete, blossom=blossom_autocomplete)
async def set_bonus(interaction: discord.Interaction, gamename: str, blossom: str, bonus: int):
    if bonus not in (1, 2):
        await interaction.response.send_message("❌ Bonus must be 1 or 2.", ephemeral=True)
        return
    res = supabase.table("ownership").update({"bonus": bonus}).eq("gamename", gamename).eq("blossom", blossom).execute()
    if res.data:
        await interaction.response.send_message(f"✅ Set **+{bonus}** bonus on **{blossom}** for **{gamename}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ **{gamename}** doesn't have **{blossom}** in their hoard.", ephemeral=True)


# ════════════════════════════════════════════════════════════════════════════════
# DISPLAY COMMANDS — anyone can use
# ════════════════════════════════════════════════════════════════════════════════

@tree.command(name="blossom", description="Display info about a blossom and who owns it")
@app_commands.describe(name="The blossom to look up")
@app_commands.autocomplete(name=blossom_autocomplete)
async def blossom_info(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    bl = supabase.table("blossoms").select("*").eq("name", name).execute()
    if not bl.data:
        await interaction.followup.send(f"❌ No blossom named **{name}** found.", ephemeral=True)
        return

    b = bl.data[0]
    rarity_raw     = b.get("rarity", "")
    rarity_display = RARITY_DISPLAY.get(rarity_raw, rarity_raw)

    embed = discord.Embed(title=f"🌸 {b['name']}", color=PINK)
    embed.add_field(name="Rarity", value=f"{rarity_icon(rarity_raw)} {rarity_display}", inline=True)
    embed.add_field(name="Points", value=str(b["points"]),   inline=True)
    embed.add_field(name="Source", value=b["source"] or "—", inline=True)

    if b.get("thumbnail_url"):
        embed.set_thumbnail(url=b["thumbnail_url"])

    all_vases = supabase.table("vases").select("name, " + ", ".join(VASE_SLOTS)).execute()
    matching = sorted(v["name"] for v in all_vases.data if name in [v.get(s) for s in VASE_SLOTS])
    embed.add_field(
        name="Found In Vases",
        value="\n".join(f"🏺 {v}" for v in matching) if matching else "Not part of any vase yet.",
        inline=False,
    )

    owners = supabase.table("ownership").select("gamename, bonus").eq("blossom", name).execute()
    if owners.data:
        lines = [f"🌿 {row['gamename']}" + (f"  {bonus_icon(row['bonus'])} +{row['bonus']}" if row.get("bonus") else "") for row in owners.data]
        embed.add_field(name=f"Florists ({len(owners.data)})", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Florists", value="Nobody owns this blossom yet.", inline=False)

    if b.get("image_url"):
        embed.set_image(url=b["image_url"])

    await interaction.followup.send(embed=embed)


@tree.command(name="myhoard", description="See all blossoms in a florist's hoard")
@app_commands.describe(gamename="The florist's in-game name")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def my_hoard(interaction: discord.Interaction, gamename: str):
    await interaction.response.defer()
    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.followup.send(f"❌ No florist named **{gamename}** found.", ephemeral=True)
        return

    owned = supabase.table("ownership").select("blossom, bonus").eq("gamename", gamename).execute()
    if not owned.data:
        await interaction.followup.send(f"🌱 **{gamename}**'s hoard is empty! Use `/add` to start adding blossoms.")
        return

    blossom_names = [row["blossom"] for row in owned.data]
    bl_map = {b["name"]: b for b in supabase.table("blossoms").select("name, points, rarity").in_("name", blossom_names).execute().data}

    owned_sorted = sorted(owned.data, key=lambda row: sort_key_rarity_points_alpha(bl_map.get(row["blossom"], {})))
    lines = build_hoard_lines(owned_sorted, bl_map)

    await send_paginated(
        interaction, lines,
        title=f"🌷 {gamename}'s Hoard",
        footer_total=f"{len(lines)} blossom(s) in hoard",
    )

@tree.command(name="floristlist", description="List all registered florists and the number of blossoms they own")
async def florist_list(interaction: discord.Interaction):
    await interaction.response.defer()

    players = (
        supabase.table("players")
        .select("gamename")
        .order("gamename")
        .execute()
    )

    if not players.data:
        await interaction.followup.send("🌱 No florists are currently registered.")
        return

    ownership_rows = []
    start = 0
    batch_size = 1000

    while True:
        batch = (
            supabase.table("ownership")
            .select("gamename")
            .range(start, start + batch_size - 1)
            .execute()
        )

        ownership_rows.extend(batch.data or [])

        if len(batch.data or []) < batch_size:
            break

        start += batch_size

    ownership_counts = {}

    for record in ownership_rows:
        gamename = record["gamename"]
        ownership_counts[gamename] = ownership_counts.get(gamename, 0) + 1

    lines = []

    for player in players.data:
        gamename = player["gamename"]
        count = ownership_counts.get(gamename, 0)
        lines.append(f"🌿 **{gamename}** — {count} blossom(s)")

    total_florists = len(lines)
    total_blossoms = sum(
        int(line.rsplit("—", 1)[1].strip().split()[0])
        for line in lines
    )

    description = "\n".join(lines)

    embed = discord.Embed(
        title="🌸 Tintaglia Florists",
        description=description,
        color=PINK,
    )

    embed.set_footer(
        text=f"{total_florists} florist(s) • {total_blossoms} blossoms owned"
    )

    await interaction.followup.send(embed=embed)
    
@tree.command(name="keyhoard", description="Show only the blossoms from a florist's hoard that count toward the competition whitelist")
@app_commands.describe(gamename="The florist's in-game name")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def key_hoard(interaction: discord.Interaction, gamename: str):
    await interaction.response.defer()
    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.followup.send(f"❌ No florist named **{gamename}** found.", ephemeral=True)
        return

    owned = supabase.table("ownership").select("blossom, bonus").eq("gamename", gamename).execute()
    if not owned.data:
        await interaction.followup.send(f"🌱 **{gamename}**'s hoard is empty!")
        return

    blossom_names = [row["blossom"] for row in owned.data]
    bl_map = {b["name"]: b for b in supabase.table("blossoms").select("name, points, rarity").in_("name", blossom_names).execute().data}

    tiers: dict[int, list[str]] = {}
    for row in owned.data:
        b = bl_map.get(row["blossom"], {})
        pts = b.get("points")
        if pts in POINT_TIERS and b.get("rarity") != "Green":
            tiers.setdefault(pts, []).append(row["blossom"])

    member_keep: set[str] = set()

    # Always protect the two highest-value tiers.
    # 30-point tasks are rare, so 28-point flowers remain protected
    # even when the florist already has 10+ flowers at 30 points.
    for tier in (30, 28):
        if tier in tiers:
            member_keep.update(tiers[tier])

    # If the protected tiers don't provide enough options, work downward
    # through the remaining point tiers. Once a tier is needed, include
    # the ENTIRE tier so that no potentially useful flower is arbitrarily
    # excluded.
    for tier in (25, 23, 21, 14, 9):
        if len(member_keep) >= MIN_FLOWERS:
            break
        if tier in tiers:
            member_keep.update(tiers[tier])

    if not member_keep:
        await interaction.followup.send(f"🌾 **{gamename}** has no blossoms that qualify for the whitelist.")
        return

    key_rows = [row for row in owned.data if row["blossom"] in member_keep]
    key_rows_sorted = sorted(key_rows, key=lambda row: sort_key_rarity_points_alpha(bl_map.get(row["blossom"], {})))
    lines = build_hoard_lines(key_rows_sorted, bl_map)

    await send_paginated(
        interaction, lines,
        title=f"🔑 {gamename}'s Key Hoard",
        footer_total=f"{len(lines)} blossom(s) qualify for whitelist",
    )


@tree.command(name="vase", description="Display info about a vase")
@app_commands.describe(name="The vase to look up")
@app_commands.autocomplete(name=vase_autocomplete)
async def vase_info(interaction: discord.Interaction, name: str):
    await interaction.response.defer()
    res = supabase.table("vases").select("*").eq("name", name).execute()
    if not res.data:
        await interaction.followup.send(f"❌ No vase named **{name}** found.", ephemeral=True)
        return

    v = res.data[0]
    embed = discord.Embed(title=f"🏺 {v['name']}", color=PINK)

    all_blossom_names = [v[k] for k in VASE_SLOTS if v.get(k)]
    rarity_map = {}
    if all_blossom_names:
        rarity_res = supabase.table("blossoms").select("name, rarity").in_("name", all_blossom_names).execute()
        rarity_map = {b["name"]: b["rarity"] for b in rarity_res.data}

    def blossom_line(bname):
        ico = rarity_icon(rarity_map.get(bname, "")) if rarity_map.get(bname) else "🌸"
        return f"{ico} {bname}"

    primaries   = [v[k] for k in ["primary_1",   "primary_2",   "primary_3"]   if v.get(k)]
    secondaries = [v[k] for k in ["secondary_1", "secondary_2", "secondary_3"] if v.get(k)]
    tertiaries  = [v[k] for k in ["tertiary_1",  "tertiary_2",  "tertiary_3"]  if v.get(k)]

    if primaries:
        embed.add_field(name="Primary Flowers",   value="\n".join(blossom_line(b) for b in primaries),   inline=False)
    if secondaries:
        embed.add_field(name="Secondary Flowers", value="\n".join(blossom_line(b) for b in secondaries), inline=False)
    if tertiaries:
        embed.add_field(name="Accent Flowers",    value="\n".join(blossom_line(b) for b in tertiaries),  inline=False)

    if v.get("image_url"):
        embed.set_image(url=v["image_url"])

    await interaction.followup.send(embed=embed)


@tree.command(name="noticeboard", description="[Admin] Display the current notice board")
async def noticeboard(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can view the notice board.", ephemeral=True)
        return
    res = supabase.table("notices").select("gamename, notice").order("created_at").execute()
    if not res.data:
        await interaction.response.send_message("📋 The notice board is empty.", ephemeral=True)
        return
    embed = discord.Embed(title="📋 Notice Board", color=PINK)
    for row in res.data:
        embed.add_field(name=f"🌿 {row['gamename']}", value=row["notice"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="blossomhelp", description="Show how to use the Blossom Hoard Bot")
async def blossom_help(interaction: discord.Interaction):
    embed = discord.Embed(title="🌸 Blossom Hoard Bot — Help", description="Here's a quick guide to all available commands.", color=PINK)
    embed.add_field(name="🌿 Florists", value=(
        "`/addflorist <gamename>` — Register a new florist\n"
        "`/removeflorist <gamename>` — Remove a florist and their hoard"
    ), inline=False)
    embed.add_field(name="🌱 Managing a Hoard", value=(
        "`/add <gamename> <blossom(s)> [bonus]` — Add up to 10 blossoms at once\n"
        "`/remove <gamename> <blossom>` — Remove a blossom from a hoard\n"
        "`/setbonus <gamename> <blossom> <bonus>` — Set or update a +1 or +2 bonus"
    ), inline=False)
    embed.add_field(name="🔍 Looking Things Up", value=(
        "`/blossom <name>` — Show blossom info, vases it appears in, and who owns it\n"
        "`/myhoard <gamename>` — Show all blossoms in a florist's hoard\n"
        "`/keyhoard <gamename>` — Show only the blossoms that count toward the whitelist\n"
        "`/vase <name>` — Show vase info and its blossom slots"
    ), inline=False)
    embed.add_field(name="🔒 Admin Only", value=(
        "`/addblossom` — Add a new blossom to the database\n"
        "`/updateblossom <name>` — Update any field on a blossom (including rename)\n"
        "`/removeblossom <name>` — Remove a blossom from the database\n"
        "`/addvase` — Add a new vase to the database\n"
        "`/updatevase <name>` — Update any field on a vase (including rename)\n"
        "`/whitelist [sort]` — Generate the optimal competition keep list\n"
        "`/markdone <gamename>` — Mark a florist as done for this week\n"
        "`/cleardone` — Remove all done markers from all florists\n"
        "`/addnotice <gamename> <notice>` — Add a notice for a florist\n"
        "`/removenotice <gamename>` — Remove a florist's notice\n"
        "`/noticeboard` — View the current notice board"
    ), inline=False)
    embed.set_footer(text="All name fields support autocomplete — start typing to see options!")
    await interaction.response.send_message(embed=embed)

@tree.command(name="changelog", description="View recent changes to flowers, vases, and the bot")
async def changelog(interaction: discord.Interaction):
    await interaction.response.defer()

    cutoff = datetime.now(timezone.utc) - timedelta(days=42)

    result = (
        supabase.table("changelog")
        .select("changed_at, category, change_type, name, old_name, description")
        .gte("changed_at", cutoff.isoformat())
        .order("changed_at", desc=True)
        .execute()
    )

    if not result.data:
        await interaction.followup.send(
            "📜 No changes have been recorded in the last 6 weeks.",
            ephemeral=True
        )
        return

    # Group changes by calendar day
    grouped = {}

    for row in result.data:
        dt = datetime.fromisoformat(row["changed_at"].replace("Z", "+00:00"))
        day = dt.astimezone(timezone.utc).date()
        grouped.setdefault(day, []).append(row)

    description_lines = []

    for day, changes in grouped.items():
        date_label = day.strftime("%B %-d")
        description_lines.append(f"**{date_label}**")

        for change in reversed(changes):
            description_lines.append(f"• {change['description']}")

        description_lines.append("")

    embed = discord.Embed(
        title="📜 BlossomHoard Changelog",
        description="\n".join(description_lines),
        color=PINK,
    )

    embed.set_footer(text="Showing changes from the last 6 weeks")

    await interaction.followup.send(
        embed=embed,
        ephemeral=False,
    )

# ════════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS — admins and staff only
# ════════════════════════════════════════════════════════════════════════════════

@tree.command(name="addblossom", description="[Admin] Add a new blossom to the database")
@app_commands.describe(
    name="Blossom name", rarity="Rarity: Green, Blue, Purple, Gold, or Red",
    points="Point value: 30, 28, 25, 23, 21, 14, or 9", source="Where the blossom comes from",
    image_url="Full image URL (optional — add later via /updateblossom)",
    thumbnail_url="Thumbnail URL (optional — add later via /updateblossom)",
)
async def add_blossom(
    interaction: discord.Interaction, name: str, rarity: str, points: int, source: str,
    image_url: str | None = None, thumbnail_url: str | None = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can add blossoms.", ephemeral=True)
        return
    rarity = rarity.strip().capitalize()
    if rarity not in RARITIES:
        await interaction.response.send_message(f"❌ Invalid rarity. Choose from: {', '.join(RARITIES)}", ephemeral=True)
        return
    if supabase.table("blossoms").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(f"❌ **{name}** already exists in the blossom database.", ephemeral=True)
        return
    res = supabase.table("blossoms").insert({
        "name": name,
        "rarity": rarity,
        "points": points,
        "source": source,
        "image_url": image_url,
        "thumbnail_url": thumbnail_url,
    }).execute()

    if res.data:
        log_change(
            interaction,
            "flower",
            "added",
            name,
            f"Added **{name}**",
        )

    await interaction.response.send_message(
        f"🌸 **{name}** has been added to the blossom database!",
        ephemeral=True
    )


@tree.command(name="updateblossom", description="[Admin] Update any field on an existing blossom")
@app_commands.describe(
    name="The blossom to update", new_name="Rename the blossom",
    rarity="New rarity", points="New point value", source="New source",
    image_url="New image URL", thumbnail_url="New thumbnail URL",
)
@app_commands.autocomplete(name=blossom_autocomplete)
async def update_blossom(
    interaction: discord.Interaction, name: str,
    new_name: str | None = None, rarity: str | None = None, points: int | None = None,
    source: str | None = None, image_url: str | None = None, thumbnail_url: str | None = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can update blossoms.", ephemeral=True)
        return
    if not supabase.table("blossoms").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(f"❌ No blossom named **{name}** was found.", ephemeral=True)
        return

    original_name = name
    changed = []

    # Rename — update blossoms first (CASCADE handles ownership), then vase slots manually
    if new_name is not None:
        new_name = new_name.strip()
        if supabase.table("blossoms").select("id").eq("name", new_name).execute().data:
            await interaction.response.send_message(f"❌ A blossom named **{new_name}** already exists.", ephemeral=True)
            return
        supabase.table("blossoms").update({"name": new_name}).eq("name", name).execute()
        for slot in VASE_SLOTS:
            supabase.table("vases").update({slot: new_name}).eq(slot, name).execute()
        changed.append("name")
        name = new_name

    updates = {}
    if rarity is not None:
        rarity = rarity.strip().capitalize()
        if rarity not in RARITIES:
            await interaction.response.send_message(f"❌ Invalid rarity. Choose from: {', '.join(RARITIES)}", ephemeral=True)
            return
        updates["rarity"] = rarity
    if points        is not None: updates["points"]        = points
    if source        is not None: updates["source"]        = source
    if image_url     is not None: updates["image_url"]     = image_url
    if thumbnail_url is not None: updates["thumbnail_url"] = thumbnail_url

    if updates:
        supabase.table("blossoms").update(updates).eq("name", name).execute()
        changed.extend(updates.keys())

    if not changed:
        await interaction.response.send_message("❌ You didn't provide any fields to update.", ephemeral=True)
        return

    if original_name != name:
        if changed == ["name"]:
            details = f"Renamed **{original_name}** to **{name}**"
        else:
            details = f"Renamed **{original_name}** to **{name}**; updated fields: {', '.join(changed)}"
    else:
        details = f"Updated **{name}**: {', '.join(changed)}"

    log_change(
        interaction,
        "flower",
        "updated",
        name,
        details,
    )

    await interaction.response.send_message(
        f"✅ **{name}** updated! Fields changed: {', '.join(changed)}",
        ephemeral=True
    )


@tree.command(name="removeblossom", description="[Admin] Remove a blossom from the database")
@app_commands.describe(name="The blossom to remove")
@app_commands.autocomplete(name=blossom_autocomplete)
async def remove_blossom(interaction: discord.Interaction, name: str):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can remove blossoms.", ephemeral=True)
        return
    supabase.table("ownership").delete().eq("blossom", name).execute()
    res = supabase.table("blossoms").delete().eq("name", name).execute()
    if res.data:
        log_change(
            interaction,
            "flower",
            "removed",
            name,
            f"Removed **{name}**",
        )
        await interaction.response.send_message(
            f"🍂 **{name}** has been removed from the database.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ No blossom named **{name}** was found.",
            ephemeral=True
        )


@tree.command(name="addvase", description="[Admin] Add a new vase to the database")
@app_commands.describe(
    name="Vase name", primary_1="Primary blossom 1 (required)",
    secondary_1="Secondary blossom 1 (required)", tertiary_1="Accent blossom 1 (required)",
    primary_2="Primary blossom 2 (optional)", primary_3="Primary blossom 3 (optional)",
    secondary_2="Secondary blossom 2 (optional)", secondary_3="Secondary blossom 3 (optional)",
    tertiary_2="Accent blossom 2 (optional)", tertiary_3="Accent blossom 3 (optional)",
    image_url="Image URL (optional — add later via Supabase dashboard)",
)
@app_commands.autocomplete(
    primary_1=blossom_autocomplete,   primary_2=blossom_autocomplete,   primary_3=blossom_autocomplete,
    secondary_1=blossom_autocomplete, secondary_2=blossom_autocomplete, secondary_3=blossom_autocomplete,
    tertiary_1=blossom_autocomplete,  tertiary_2=blossom_autocomplete,  tertiary_3=blossom_autocomplete,
)
async def add_vase(
    interaction: discord.Interaction, name: str,
    primary_1: str, secondary_1: str, tertiary_1: str,
    primary_2: str | None = None, primary_3: str | None = None,
    secondary_2: str | None = None, secondary_3: str | None = None,
    tertiary_2: str | None = None, tertiary_3: str | None = None,
    image_url: str | None = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can add vases.", ephemeral=True)
        return
    if supabase.table("vases").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(f"❌ **{name}** already exists in the vase database.", ephemeral=True)
        return
    provided = [b for b in [primary_1, primary_2, primary_3, secondary_1, secondary_2, secondary_3, tertiary_1, tertiary_2, tertiary_3] if b]
    valid_names = {r["name"] for r in supabase.table("blossoms").select("name").in_("name", provided).execute().data}
    invalid = [b for b in provided if b not in valid_names]
    if invalid:
        await interaction.response.send_message(f"❌ These blossoms aren't in the database yet: {', '.join(invalid)}", ephemeral=True)
        return
    res = supabase.table("vases").insert({
        "name": name, "image_url": image_url,
        "primary_1": primary_1,     "primary_2": primary_2,     "primary_3": primary_3,
        "secondary_1": secondary_1, "secondary_2": secondary_2, "secondary_3": secondary_3,
        "tertiary_1": tertiary_1,   "tertiary_2": tertiary_2,   "tertiary_3": tertiary_3,
    }).execute()
    if res.data:
        log_change(
            interaction,
            "vase",
            "added",
            name,
            f"Added **{name}**",
        )
    await interaction.response.send_message(f"🏺 **{name}** has been added to the vase database!", ephemeral=True)


@tree.command(name="updatevase", description="[Admin] Update any field on an existing vase")
@app_commands.describe(
    name="The vase to update", new_name="Rename the vase",
    primary_1="New primary blossom 1",     primary_2="New primary blossom 2",   primary_3="New primary blossom 3",
    secondary_1="New secondary blossom 1", secondary_2="New secondary blossom 2", secondary_3="New secondary blossom 3",
    tertiary_1="New accent blossom 1",     tertiary_2="New accent blossom 2",   tertiary_3="New accent blossom 3",
    image_url="New image URL",
)
@app_commands.autocomplete(
    name=vase_autocomplete,
    primary_1=blossom_autocomplete,   primary_2=blossom_autocomplete,   primary_3=blossom_autocomplete,
    secondary_1=blossom_autocomplete, secondary_2=blossom_autocomplete, secondary_3=blossom_autocomplete,
    tertiary_1=blossom_autocomplete,  tertiary_2=blossom_autocomplete,  tertiary_3=blossom_autocomplete,
)
async def update_vase(
    interaction: discord.Interaction, name: str, new_name: str | None = None,
    primary_1: str | None = None,   primary_2: str | None = None,   primary_3: str | None = None,
    secondary_1: str | None = None, secondary_2: str | None = None, secondary_3: str | None = None,
    tertiary_1: str | None = None,  tertiary_2: str | None = None,  tertiary_3: str | None = None,
    image_url: str | None = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can update vases.", ephemeral=True)
        return
    if not supabase.table("vases").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(f"❌ No vase named **{name}** was found.", ephemeral=True)
        return

    changed = []
    if new_name is not None:
        new_name = new_name.strip()
        if supabase.table("vases").select("id").eq("name", new_name).execute().data:
            await interaction.response.send_message(f"❌ A vase named **{new_name}** already exists.", ephemeral=True)
            return
        supabase.table("vases").update({"name": new_name}).eq("name", name).execute()
        changed.append("name")
        name = new_name

    updates = {}
    for field, val in [
        ("primary_1", primary_1),     ("primary_2", primary_2),     ("primary_3", primary_3),
        ("secondary_1", secondary_1), ("secondary_2", secondary_2), ("secondary_3", secondary_3),
        ("tertiary_1", tertiary_1),   ("tertiary_2", tertiary_2),   ("tertiary_3", tertiary_3),
        ("image_url", image_url),
    ]:
        if val is not None:
            updates[field] = val

    if updates:
        blossom_fields = {k: v for k, v in updates.items() if k != "image_url"}
        if blossom_fields:
            valid_names = {r["name"] for r in supabase.table("blossoms").select("name").in_("name", list(blossom_fields.values())).execute().data}
            invalid = [v for v in blossom_fields.values() if v not in valid_names]
            if invalid:
                await interaction.response.send_message(f"❌ These blossoms aren't in the database: {', '.join(invalid)}", ephemeral=True)
                return
        supabase.table("vases").update(updates).eq("name", name).execute()
        changed.extend(updates.keys())

    if not changed:
        await interaction.response.send_message("❌ You didn't provide any fields to update.", ephemeral=True)
        return

    log_change(
        interaction,
        "vase",
        "updated",
        name,
        f"Updated **{name}**: {', '.join(changed)}",
    )

    await interaction.response.send_message(
        f"✅ **{name}** updated! Fields changed: {', '.join(changed)}",
    ephemeral=True
    )

@tree.command(name="linkname", description="Link an in-game name to an existing florist")
@app_commands.describe(
    florist="The florist from the database",
    server_number="The server number (example: 5 for s5)",
    game_name="The exact in-game name"
)
@app_commands.autocomplete(florist=florist_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def link_name(
    interaction: discord.Interaction,
    florist: str,
    server_number: int,
    game_name: str
):
    florist = florist.strip()
    game_name = game_name.strip()

    # Find the florist
    player = (
        supabase.table("players")
        .select("id, gamename")
        .eq("gamename", florist)
        .execute()
        .data
    )

    if not player:
        await interaction.response.send_message(
            f"❌ I couldn't find **{florist}** in the florist database.",
            ephemeral=True
        )
        return

    player_id = player[0]["id"]

    # Make sure this server/name combination isn't already linked
    existing = (
        supabase.table("player_aliases")
        .select("id, player_id")
        .eq("server_number", server_number)
        .eq("game_name", game_name)
        .execute()
        .data
    )

    if existing:
        if existing[0]["player_id"] == player_id:
            await interaction.response.send_message(
                f"🔗 **s{server_number}.{game_name}** is already linked to **{florist}**.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ **s{server_number}.{game_name}** is already linked to another florist!",
                ephemeral=True
            )
        return

    # Create the link
    supabase.table("player_aliases").insert({
        "player_id": player_id,
        "server_number": server_number,
        "game_name": game_name
    }).execute()

    await interaction.response.send_message(
        f"🔗 Linked **s{server_number}.{game_name}** → **{florist}**!",
        ephemeral=True
    )

@tree.command(name="markdone", description="[Admin] Mark a florist as done for this week's competition")
@app_commands.describe(gamename="The florist to mark as done")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def mark_done(interaction: discord.Interaction, gamename: str):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can mark florists as done.", ephemeral=True)
        return
    res = supabase.table("players").update({"done": True}).eq("gamename", gamename).execute()
    if res.data:
        await interaction.response.send_message(f"✅ **{gamename}** has been marked as done for this week.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ No florist named **{gamename}** was found.", ephemeral=True)


@tree.command(name="cleardone", description="[Admin] Remove all done markers from all florists")
async def clear_done(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can clear done markers.", ephemeral=True)
        return
    supabase.table("players").update({"done": False}).eq("done", True).execute()
    await interaction.response.send_message("✅ All done markers have been cleared.", ephemeral=True)


@tree.command(name="addnotice", description="[Admin] Add a notice for a florist on the notice board")
@app_commands.describe(gamename="The florist's in-game name", notice="The notice to display (max 200 characters)")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def add_notice(interaction: discord.Interaction, gamename: str, notice: str):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can add notices.", ephemeral=True)
        return
    if len(notice) > 200:
        await interaction.response.send_message(f"❌ Notice is too long ({len(notice)} characters). Maximum is 200.", ephemeral=True)
        return
    supabase.table("notices").upsert({"gamename": gamename, "notice": notice}, on_conflict="gamename").execute()
    await interaction.response.send_message(f"📋 Notice added for **{gamename}**.", ephemeral=True)


@tree.command(name="removenotice", description="[Admin] Remove a florist's notice from the notice board")
@app_commands.describe(gamename="The florist whose notice to remove")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def remove_notice(interaction: discord.Interaction, gamename: str):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can remove notices.", ephemeral=True)
        return
    res = supabase.table("notices").delete().eq("gamename", gamename).execute()
    if res.data:
        await interaction.response.send_message(f"📋 Notice for **{gamename}** has been removed.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ No notice found for **{gamename}**.", ephemeral=True)


@tree.command(name="whitelist", description="[Admin] Generate the optimal blossom keep list for a competition")
@app_commands.describe(sort="How to sort the results")
@app_commands.choices(sort=[
    app_commands.Choice(name="By tier — highest first, alphabetical within tier", value="tier"),
    app_commands.Choice(name="Alphabetically — A to Z",                           value="alpha"),
])
async def whitelist(interaction: discord.Interaction, sort: str = "alpha"):
    await interaction.response.defer()

    if not is_admin(interaction):
        await interaction.followup.send(
            "🚫 Only admins can run the whitelist.",
            ephemeral=True
        )
        return

    def build_whitelist():
        all_players = (
            supabase.table("players")
            .select("gamename, done")
            .execute()
            .data
        )

        done_players = {
            p["gamename"]
            for p in all_players
            if p.get("done")
        }

        active_players = {
            p["gamename"]
            for p in all_players
            if not p.get("done")
        }

        if not active_players:
            return None

        ownership = (
            supabase.table("ownership")
            .select("gamename, blossom")
            .in_("gamename", list(active_players))
            .execute()
        )

        if not ownership.data:
            return None

        blossom_names = list({
            row["blossom"]
            for row in ownership.data
        })

        blossom_details = {
            b["name"]: b
            for b in supabase.table("blossoms")
            .select("name, points, rarity")
            .in_("name", blossom_names)
            .execute()
            .data
        }

        member_tiers: dict[str, dict[int, list[str]]] = {}

        for row in ownership.data:
            b = blossom_details.get(row["blossom"], {})
            pts = b.get("points")

            if pts not in POINT_TIERS or b.get("rarity") == "Green":
                continue

            member_tiers \
                .setdefault(row["gamename"], {}) \
                .setdefault(pts, []) \
                .append(row["blossom"])

        keep: set[str] = set()

        for tiers in member_tiers.values():
            member_keep: set[str] = set()

            # Always protect the protected tiers (currently 30 and 28)
            for tier in ALWAYS_INCLUDE:
                if tier in tiers:
                    member_keep.update(tiers[tier])

            # Then work downward until this florist has enough choices.
            # Once a tier is needed, include the ENTIRE tier.
            for tier in POINT_TIERS:
                if tier in ALWAYS_INCLUDE:
                    continue

                if len(member_keep) >= MIN_FLOWERS:
                    break

                if tier in tiers:
                    member_keep.update(tiers[tier])

            keep.update(member_keep)

        if not keep:
            return None

        kept = (
            supabase.table("blossoms")
            .select("name, points, rarity")
            .in_("name", list(keep))
            .execute()
            .data
        )

        if sort == "tier":
            kept.sort(key=lambda b: (-b["points"], b["name"]))
        else:
            kept.sort(key=lambda b: b["name"])

        lines = build_whitelist_lines(kept)

        sort_label = (
            "by tier (highest first)"
            if sort == "tier"
            else "alphabetically"
        )

        title = "🌺 Competition Whitelist"

        footer = (
            f"Sorted {sort_label} · "
            f"{len(kept)} blossom(s) · "
            f"{len(member_tiers)} active florist(s)"
        )

        return lines, title, footer, done_players

    # Build the initial whitelist
    result = await asyncio.to_thread(build_whitelist)

    if not result:
        await interaction.followup.send(
            "❌ No blossoms qualify for the whitelist.",
            ephemeral=True
        )
        return

    lines, title, footer, done_players = result

    # Give ONLY /whitelist a refresh callback
    view = PaginatedView(
        lines,
        title,
        footer,
        ephemeral=True,
        refresh_callback=build_whitelist,
    )

    # Preserve the "Florists marked as done" field
    view.done_players = done_players

    embed = view._build_embed()

    await interaction.followup.send(
        embed=embed,
        view=view,
        ephemeral=False
    )

@tree.command(name="logchange", description="[Admin] Record a bot change in the changelog")
@app_commands.describe(description="Describe the bot change")
async def logchange(interaction: discord.Interaction, description: str):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "🚫 Only admins can log bot changes.",
            ephemeral=True
        )
        return

    description = description.strip()

    if not description:
        await interaction.response.send_message(
            "❌ Please provide a description of the change.",
            ephemeral=True
        )
        return

    log_bot_change(interaction, description)

    await interaction.response.send_message(
        f"✅ Bot change logged: **{description}**",
        ephemeral=True
    )

@tree.command(name="testocr", description="Test screenshot OCR")
@app_commands.describe(image="Upload a screenshot to test OCR")
@app_commands.checks.has_permissions(administrator=True)
async def test_ocr(
    interaction: discord.Interaction,
    image: discord.Attachment
):
    await interaction.response.defer(ephemeral=True)

    if not image.content_type or not image.content_type.startswith("image/"):
        await interaction.followup.send(
            "❌ Please upload an image file.",
            ephemeral=True
        )
        return

    extension = os.path.splitext(image.filename)[1] or ".png"
    temp_path = f"/tmp/blossomhoard_ocr{extension}"

    try:
        await image.save(temp_path)

        from PIL import Image
        import pytesseract
        from pytesseract import Output

        img = Image.open(temp_path)

        from PIL import ImageOps, ImageEnhance, ImageFilter

        # Preprocess for small/stylized game text
        img = img.convert("L")
        img = img.resize(
            (img.width * 3, img.height * 3),
            Image.Resampling.LANCZOS
        )
        img = ImageOps.autocontrast(img)
        img = ImageEnhance.Contrast(img).enhance(1.8)
        img = img.filter(ImageFilter.SHARPEN)

        data = pytesseract.image_to_data(
            img,
            config="--psm 11",
            output_type=Output.DICT
        )

        results = []

        for i in range(len(data["text"])):
            text = data["text"][i].strip()

            if not text:
                continue

            try:
                confidence = float(data["conf"][i])
            except (ValueError, TypeError):
                confidence = -1

            if confidence < 20:
                continue

            results.append({
                "text": text,
                "x": data["left"][i],
                "y": data["top"][i],
                "w": data["width"][i],
                "h": data["height"][i],
                "conf": confidence,
            })

        # Group OCR words into approximate visual lines
        results.sort(key=lambda r: (r["y"], r["x"]))

        ocr_lines = []

        for r in results:
            placed = False

            for line in ocr_lines:
                if abs(r["y"] - line["y"]) <= 12:
                    line["words"].append(r)
                    placed = True
                    break

            if not placed:
                ocr_lines.append({
                    "y": r["y"],
                    "words": [r]
                })

        for line in ocr_lines:
            line["words"].sort(key=lambda r: r["x"])

        text_lines = [
            " ".join(r["text"] for r in line["words"])
            for line in ocr_lines
        ]

        lines = text_lines

        output = "\n".join(text_lines)

        output = "\n".join(lines)

        if not output:
            output = "No usable OCR text detected."

        # Discord messages have a 2000-character limit.
        output = output[:1800]

        await interaction.followup.send(
            f"🔎 **OCR coordinate test:**\n```text\n{output}\n```",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ OCR test failed: `{type(e).__name__}: {e}`",
            ephemeral=True
        )

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@tree.command(name="testcompocr", description="Test competition screenshot OCR")
@app_commands.describe(image="Upload a competition ranking screenshot")
@app_commands.checks.has_permissions(administrator=True)
async def test_comp_ocr(
    interaction: discord.Interaction,
    image: discord.Attachment
):
    await interaction.response.defer(ephemeral=True)

    if not image.content_type or not image.content_type.startswith("image/"):
        await interaction.followup.send(
            "❌ Please upload an image file.",
            ephemeral=True
        )
        return

    extension = os.path.splitext(image.filename)[1] or ".png"
    temp_path = f"/tmp/blossomhoard_compocr{extension}"

    try:
        await image.save(temp_path)

        from PIL import Image, ImageOps, ImageEnhance
        import pytesseract

        img = Image.open(temp_path)

        # Crop away the avatars/rank numbers and keep the actual player data.
        w, h = img.size
        crop = img.crop((
            int(w * 0.40),
            int(h * 0.39),
            w,
            int(h * 0.91)
        ))

        # Enlarge and simplify the image for OCR.
        crop = crop.resize(
            (crop.width * 2, crop.height * 2),
            Image.Resampling.LANCZOS
        )
        crop = ImageOps.grayscale(crop)
        crop = ImageEnhance.Contrast(crop).enhance(1.5)

        text = pytesseract.image_to_string(
            crop,
            config="--psm 6"
        ).strip()

        if not text:
            text = "No OCR text detected."

        # Keep the Discord message safely under 2000 characters.
        text = text[:1800]

        await interaction.followup.send(
            f"🔎 **Competition OCR test:**\n```text\n{text}\n```",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ OCR test failed: `{type(e).__name__}: {e}`",
            ephemeral=True
        )

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@tree.command(name="testocrdata", description="Test structured OCR output")
@app_commands.describe(image="Upload a screenshot")
async def testocrdata(interaction: discord.Interaction, image: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    image_path = f"/tmp/{image.filename}"

    try:
        await image.save(image_path)

        data = await ocr_image_data(image_path)

        lines = []
        for item in data:
            lines.append(
                f"y={item['y']} x={item['x']} "
                f"conf={item['confidence']:.0f} "
                f"{item['text']}"
            )

        output = "\n".join(lines)

        if len(output) > 1900:
            output = output[:1900] + "\n...[truncated]"

        await interaction.followup.send(
            f"🔎 **Structured OCR result:**\n```text\n{output}\n```",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ OCR test failed: `{type(e).__name__}: {e}`",
            ephemeral=True
        )


@tree.command(
    name="testtaskparse",
    description="Test task-log screenshot parsing"
)
@app_commands.describe(
    image="Upload a task-log screenshot"
)
async def testtaskparse(
    interaction: discord.Interaction,
    image: discord.Attachment
):
    await interaction.response.defer(ephemeral=True)

    image_path = f"/tmp/{image.filename}"
    await image.save(image_path)

    try:
        from PIL import Image

        img = Image.open(image_path)

        # Crop to the actual task-log text area.
        w, h = img.size
        crop = img.crop((
            int(w * 0.27),
            int(h * 0.32),
            int(w * 0.93),
            int(h * 0.91)
        ))

        crop_path = "/tmp/blossomhoard_tasklog_crop.png"
        crop.save(crop_path)

        ocr_text = await ocr_image(crop_path)
        entries = parse_task_logs(ocr_text)

        debug_lines = [
            f"`{line}`"
            for line in ocr_text.splitlines()
            if "spent" in line.lower() or "upgrade" in line.lower()
        ]

        if not entries:
            await interaction.followup.send(
                "❌ OCR worked, but no task-log entries were detected.\n\n"
                "**Upgrade-related OCR lines:**\n" +
                "\n".join(debug_lines),
                ephemeral=True
            )
            return

        lines = ["🔎 **Parsed task-log entries:**"]

        for entry in entries:
            lines.append(
                f"**s{entry['server_number']}.{entry['game_name']}** "
                f"→ {entry['action']} Task {entry['task_number']}: "
                f"{entry['task_text']}"
            )


        await interaction.followup.send(
            "\n".join(lines)[:1900],
            ephemeral=True
        )

    except Exception as e:
        import traceback

        error_details = traceback.format_exc()

        await interaction.followup.send(
            f"❌ Task parser test failed:\n"
            f"```text\n{error_details[-1800:]}\n```",
            ephemeral=True
        )

    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

        crop_path = "/tmp/blossomhoard_tasklog_crop.png"
        if os.path.exists(crop_path):
            os.remove(crop_path)

@tree.command(
    name="testplayerresolve",
    description="Test task-log player resolution"
)
@app_commands.describe(
    image="Upload a task-log screenshot"
)
async def testplayerresolve(
    interaction: discord.Interaction,
    image: discord.Attachment
):
    await interaction.response.defer(ephemeral=True)

    image_path = f"/tmp/{image.filename}"

    try:
        from PIL import Image

        await image.save(image_path)

        img = Image.open(image_path)

        # Use the same task-log crop as /testtaskparse.
        w, h = img.size
        crop = img.crop((
            int(w * 0.27),
            int(h * 0.32),
            int(w * 0.93),
            int(h * 0.91)
        ))

        crop_path = "/tmp/blossomhoard_tasklog_crop.png"
        crop.save(crop_path)

        # Run the existing frozen OCR/parser pipeline.
        ocr_text = await ocr_image(crop_path)
        entries = parse_task_logs(ocr_text)

        if not entries:
            await interaction.followup.send(
                "❌ OCR worked, but no task-log entries were detected.",
                ephemeral=True
            )
            return

        # Load aliases ONCE for the entire screenshot.
        aliases = load_player_aliases()

        lines = ["🔎 **Task-log player resolution:**"]

        for entry in entries:
            server_number = entry.get("server_number")

            # If the parser preserved the raw OCR player name separately,
            # use that for identity resolution. Otherwise use game_name.
            ocr_player = entry.get("ocr_player")
            game_name = entry.get("game_name")

            lookup_name = ocr_player or game_name

            result = resolve_player_alias(
                lookup_name,
                server_number,
                aliases
            )

            if result:
                if result["match_type"] == "exact":
                    match_label = "exact"
                else:
                    match_label = "alias"

                lines.append(
                    f"✅ **{lookup_name}** / s{server_number} "
                    f"→ player_id `{result['player_id']}` "
                    f"({result['game_name']}, {match_label})"
                )
            else:
                lines.append(
                    f"❓ **{lookup_name}** / s{server_number} "
                    f"→ no player match"
                )

        lines.append("")
        lines.append(
            f"Loaded **{len(aliases)}** player alias records."
        )

        await interaction.followup.send(
            "\n".join(lines)[:1900],
            ephemeral=True
        )

    except Exception:
        import traceback

        error_details = traceback.format_exc()

        await interaction.followup.send(
            f"❌ Player resolution test failed:\n"
            f"```text\n{error_details[-1800:]}\n```",
            ephemeral=True
        )

    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

        crop_path = "/tmp/blossomhoard_tasklog_crop.png"

        if os.path.exists(crop_path):
            os.remove(crop_path)

@tree.command(
    name="testblossomresolve",
    description="Test blossom name resolution"
)
@app_commands.describe(
    image="Upload a task-log screenshot"
)
async def testblossomresolve(
    interaction: discord.Interaction,
    image: discord.Attachment
):
    await interaction.response.defer(ephemeral=True)

    image_path = f"/tmp/{image.filename}"

    try:
        from PIL import Image

        await image.save(image_path)

        img = Image.open(image_path)

        # Same task-log crop used by /testtaskparse.
        w, h = img.size
        crop = img.crop((
            int(w * 0.27),
            int(h * 0.32),
            int(w * 0.93),
            int(h * 0.91)
        ))

        crop_path = "/tmp/blossomhoard_tasklog_crop.png"
        crop.save(crop_path)

        # Existing frozen OCR/parser pipeline.
        ocr_text = await ocr_image(crop_path)
        entries = parse_task_logs(ocr_text)

        if not entries:
            await interaction.followup.send(
                "❌ OCR worked, but no flower task entries "
                "were detected.",
                ephemeral=True
            )
            return

        # Load canonical blossom names ONCE.
        blossom_names = load_blossom_names()

        lines = [
            "🌸 **Blossom resolution test:**"
        ]

        for entry in entries:

            flower_text = entry.get("task_text")

            result = resolve_blossom(
                flower_text,
                blossom_names
            )

            if not result:
                lines.append(
                    f"❌ `{flower_text}` "
                    f"→ no blossom data"
                )
                continue

            if result["blossom"] is None:
                lines.append(
                    f"❓ `{flower_text}` "
                    f"→ unresolved "
                    f"({result['score']:.0%})"
                )
                continue

            review_marker = (
                " ⚠️ REVIEW"
                if result["needs_review"]
                else ""
            )

            lines.append(
                f"✅ `{flower_text}` "
                f"→ **{result['blossom']}** "
                f"({result['score']:.0%}, "
                f"{result['match_type']})"
                f"{review_marker}"
            )

        lines.append("")
        lines.append(
            f"Loaded **{len(blossom_names)}** "
            f"canonical blossoms."
        )

        await interaction.followup.send(
            "\n".join(lines)[:1900],
            ephemeral=True
        )

    except Exception:
        import traceback

        error_details = traceback.format_exc()

        await interaction.followup.send(
            f"❌ Blossom resolution test failed:\n"
            f"```text\n{error_details[-1800:]}\n```",
            ephemeral=True
        )

    finally:
        if os.path.exists(image_path):
            os.remove(image_path)

        crop_path = "/tmp/blossomhoard_tasklog_crop.png"

        if os.path.exists(crop_path):
            os.remove(crop_path)

@tree.command(
    name="testimportresolve",
    description="Test task-log OCR, player aliases, and blossom resolution"
)
@app_commands.describe(
    image="Task-log screenshot to test"
)
async def testimportresolve(
    interaction: discord.Interaction,
    image: discord.Attachment
):
    await interaction.response.defer(ephemeral=True)

    image_path = f"/tmp/{image.filename}"
    crop_path = "/tmp/blossomhoard_tasklog_crop.png"

    try:
        from PIL import Image

        # ---------------------------------------------------------
        # Save uploaded image
        # ---------------------------------------------------------

        await image.save(image_path)

        # ---------------------------------------------------------
        # Crop to the task-log area
        # ---------------------------------------------------------

        img = Image.open(image_path)

        w, h = img.size

        crop = img.crop(
            (
                int(w * 0.27),
                int(h * 0.32),
                int(w * 0.93),
                int(h * 0.91),
            )
        )

        crop.save(crop_path)

        # ---------------------------------------------------------
        # OCR
        # ---------------------------------------------------------

        ocr_text = await ocr_image(crop_path)

        # ---------------------------------------------------------
        # Parse task logs
        # ---------------------------------------------------------

        entries = parse_task_logs(ocr_text)

        if not entries:
            await interaction.followup.send(
                "❌ No task-log entries were detected.",
                ephemeral=True
            )
            return

        # ---------------------------------------------------------
        # Load reference data
        # ---------------------------------------------------------

        player_aliases = load_player_aliases()
        blossom_names = load_blossom_names()

        lines = [
            "🌸 **Import resolution test:**"
        ]

        unknown_entries = []

        # ---------------------------------------------------------
        # Resolve every parsed entry
        # ---------------------------------------------------------

        for entry in entries:

            server_number = entry.get("server_number")

            lookup_name = (
                entry.get("ocr_player")
                or entry.get("game_name")
            )

            player_result = resolve_player_alias(
                lookup_name,
                server_number,
                player_aliases
            )

            blossom_result = resolve_blossom(
                entry.get("task_text"),
                blossom_names
            )

            lines.append("")
            lines.append(
                f"**Task {entry.get('task_number')} — "
                f"{entry.get('action')}**"
            )

            # -----------------------------------------------------
            # Player resolution
            # -----------------------------------------------------

            if player_result:

                match_type = player_result.get(
                    "match_type",
                    "unknown"
                )

                lines.append(
                    f"👤 `{lookup_name}` / s{server_number} "
                    f"→ **{player_result['game_name']}** "
                    f"(player_id {player_result['player_id']}, "
                    f"{match_type})"
                )

            else:

                lines.append(
                    f"👤 `{lookup_name}` / s{server_number} "
                    f"→ ❓ **PLAYER NEEDS REVIEW**"
                )

                unknown_entries.append(entry)

            # -----------------------------------------------------
            # Blossom resolution
            # -----------------------------------------------------

            if blossom_result:

                confidence = blossom_result.get(
                    "confidence",
                    0
                )
                
                match_type = blossom_result.get(
                    "match_type",
                    "unknown"
                )
                
                # Exact normalized matches are definitive.
                # Some exact results currently return a zero confidence
                # value internally, so display them as 100%.
                if match_type == "exact":
                    confidence = 1.0

                resolved_blossom = blossom_result.get(
                    "blossom"
                )

                if resolved_blossom:

                    lines.append(
                        f"🌸 `{entry.get('task_text')}` "
                        f"→ **{resolved_blossom}** "
                        f"({confidence:.0%}, {match_type})"
                    )

                else:

                    lines.append(
                        f"🌸 `{entry.get('task_text')}` "
                        f"→ ❓ **BLOSSOM NEEDS REVIEW**"
                    )

            else:

                lines.append(
                    f"🌸 `{entry.get('task_text')}` "
                    f"→ ❓ **BLOSSOM NEEDS REVIEW**"
                )

            # -----------------------------------------------------
            # Ownership check
            # -----------------------------------------------------

            if (
                player_result
                and blossom_result
                and blossom_result.get("blossom")
                and not blossom_result.get("needs_review")
            ):

                player_id = player_result["player_id"]

                current_gamename = get_current_player_gamename(
                    player_id
                )

                if not current_gamename:

                    lines.append(
                        "➡️ ❌ **NOT READY — "
                        "CURRENT FLORIST NAME NOT FOUND**"
                    )

                else:

                    lines.append(
                        f"🏷️ Current florist name: "
                        f"**{current_gamename}**"
                    )

                    owned_blossoms = load_owned_blossoms(
                        current_gamename
                    )

                    canonical_blossom = blossom_result[
                        "blossom"
                    ]

                    if canonical_blossom in owned_blossoms:

                        lines.append(
                            "➡️ ⚠️ **ALREADY OWNED — "
                            "SKIP**"
                        )

                    else:

                        lines.append(
                            "➡️ 🌸 **NEW — READY TO IMPORT**"
                        )

            else:

                lines.append(
                    "➡️ 🚧 **NOT READY — NEEDS REVIEW**"
                )

        # ---------------------------------------------------------
        # Reference data summary
        # ---------------------------------------------------------

        lines.append("")
        lines.append(
            f"Reference data: "
            f"{len(player_aliases)} player aliases, "
            f"{len(blossom_names)} blossoms."
        )

        # ---------------------------------------------------------
        # Player review status
        # ---------------------------------------------------------

        if unknown_entries:

            lines.append("")
            lines.append(
                "🔍 **Select the correct florist below "
                "to save an alias.**"
            )

        else:

            lines.append("")
            lines.append(
                "✅ **All players resolved.**"
            )

        # ---------------------------------------------------------
        # Safety notice
        # ---------------------------------------------------------

        lines.append("")
        lines.append(
            "🔒 **TEST ONLY — no ownership records "
            "were changed.**"
        )

        output = "\n".join(lines)

        # ---------------------------------------------------------
        # Send result
        #
        # IMPORTANT:
        # Discord.py does not accept view=None when the view
        # parameter is explicitly supplied. Only pass a View
        # when one is actually needed.
        # ---------------------------------------------------------

        if unknown_entries:

            view = TaskPlayerReviewView(
                player_aliases,
                unknown_entries,
                output
            )

            await interaction.followup.send(
                output[:1900],
                ephemeral=True,
                view=view
            )

        else:

            await interaction.followup.send(
                output[:1900],
                ephemeral=True
            )

    except Exception as e:

        print(
            "ERROR IN /testimportresolve:"
        )

        import traceback
        traceback.print_exc()

        try:
            await interaction.followup.send(
                "❌ **Import resolution test failed:**\n"
                f"```text\n{traceback.format_exc()[-3500:]}\n```",
                ephemeral=True
            )
        except Exception:
            pass

    finally:

        # ---------------------------------------------------------
        # Clean up temporary files
        # ---------------------------------------------------------

        try:
            if os.path.exists(image_path):
                os.remove(image_path)

            if os.path.exists(crop_path):
                os.remove(crop_path)

        except Exception:
            pass

class TaskImportSession:
    """
    Temporary staging area for one BlossomHoard task-log import.

    Nothing in this object is written to Supabase until the
    staffer explicitly confirms the import.
    """

    def __init__(self, images):
        self.images = list(images)

        # Aliases proposed during this import.
        self.pending_aliases = []

        # Manual blossom resolutions made during this import.
        #
        # These are temporary and are NOT saved to Supabase.
        # Key = normalized OCR blossom text
        # Value = canonical blossom name
        self.pending_blossom_resolutions = {}

        # Ownership records proposed during this import.
        self.pending_imports = []

async def run_importtasklog(
    interaction: discord.Interaction,
    session: TaskImportSession
):
    image_paths = []
    crop_paths = []

    try:
        from PIL import Image

        # ---------------------------------------------------------
        # Process all screenshots in this import session
        # ---------------------------------------------------------

        entries = []

        for index, image in enumerate(session.images):

            image_path = (
                f"/tmp/blossomhoard_import_{index}_{image.filename}"
            )

            crop_path = (
                f"/tmp/blossomhoard_tasklog_import_crop_{index}.png"
            )

            image_paths.append(image_path)
            crop_paths.append(crop_path)

            # -----------------------------------------------------
            # Save uploaded image
            # -----------------------------------------------------

            await image.save(image_path)

            # -----------------------------------------------------
            # Crop to the task-log area
            # -----------------------------------------------------

            img = Image.open(image_path)

            w, h = img.size

            crop = img.crop(
                (
                    int(w * 0.27),
                    int(h * 0.32),
                    int(w * 0.93),
                    int(h * 0.91),
                )
            )

            crop.save(crop_path)

            # -----------------------------------------------------
            # OCR
            # -----------------------------------------------------

            ocr_text = await ocr_image(crop_path)

            # -----------------------------------------------------
            # Parse task logs
            # -----------------------------------------------------

            image_entries = parse_task_logs(
                ocr_text
            )

            entries.extend(image_entries)

        if not entries:
            await interaction.followup.send(
                "❌ No task-log entries were detected.",
                ephemeral=True
            )
            return

        # ---------------------------------------------------------
        # Load reference data
        # ---------------------------------------------------------

        player_aliases = load_player_aliases()

        # Add aliases proposed during this import session.
        # These are temporary and have NOT been saved to Supabase yet.
        for pending_alias in session.pending_aliases:
            if pending_alias not in player_aliases:
                player_aliases.append(pending_alias)
        
        blossom_names = load_blossom_names()

        # Add manual blossom resolutions made during this
        # import session. These are temporary and have NOT
        # been saved to Supabase.
        pending_blossom_resolutions = (
            session.pending_blossom_resolutions
        )

        lines = [
            "🌸 **Blossom import preview:**"
        ]

        unknown_entries = []
        unknown_blossom_entries = []

        pending_imports = session.pending_imports

        # Flowers already staged during an earlier pass of this
        # import session.
        session_keys = {
            (
                item["gamename"],
                item["blossom"]
            )
            for item in pending_imports
        }
        
        # Flowers encountered during THIS pass.
        # Used to detect true duplicates in the uploaded screenshots.
        pending_keys = set()
        
        # Total number of flowers currently staged for import.
        new_count = len(pending_imports)
        owned_count = 0
        duplicate_count = 0
        review_count = 0

        # ---------------------------------------------------------
        # Resolve every parsed entry
        # ---------------------------------------------------------

        for entry in entries:

            server_number = entry.get("server_number")

            lookup_name = (
                entry.get("ocr_player")
                or entry.get("game_name")
            )

            player_result = resolve_player_alias(
                lookup_name,
                server_number,
                player_aliases
            )
            
            task_text = (
                entry.get("task_text")
                or ""
            )

            blossom_key = normalize_blossom_name(
                task_text
            ).lower()

            if blossom_key in pending_blossom_resolutions:

                canonical_blossom = (
                    pending_blossom_resolutions[
                        blossom_key
                    ]
                )

                blossom_result = {
                    "blossom": canonical_blossom,
                    "confidence": 1.0,
                    "match_type": "manual",
                    "needs_review": False
                }

            else:

                blossom_result = resolve_blossom(
                    task_text,
                    blossom_names
                )

                canonical_blossom = None

            task_number = entry.get("task_number")
            action = entry.get("action")

            # -----------------------------------------------------
            # Player resolution
            # -----------------------------------------------------

            if player_result:

                player_match_type = player_result.get(
                    "match_type",
                    "unknown"
                )

                resolved_player_name = player_result["game_name"]

            else:

                resolved_player_name = None

                lines.append("")
                lines.append(
                    f"**Task {task_number} — {action}**"
                )

                lines.append(
                    f"👤 `{lookup_name}` / s{server_number} "
                    f"→ ❓ **PLAYER NEEDS REVIEW**"
                )

                unknown_entries.append(entry)

            # -----------------------------------------------------
            # Blossom resolution
            # -----------------------------------------------------


            if blossom_result:

                confidence = blossom_result.get(
                    "confidence",
                    0
                )

                blossom_match_type = blossom_result.get(
                    "match_type",
                    "unknown"
                )

                if blossom_match_type == "exact":
                    confidence = 1.0

                canonical_blossom = blossom_result.get(
                    "blossom"
                )

                if canonical_blossom:

                    # -------------------------------------------------
                    # If player is unresolved, still show the blossom
                    # so the staffer can see the complete problem.
                    # -------------------------------------------------

                    if not player_result:

                        lines.append(
                            f"🌸 `{entry.get('task_text')}` "
                            f"→ **{canonical_blossom}** "
                            f"({confidence:.0%}, "
                            f"{blossom_match_type})"
                        )

                    # -------------------------------------------------
                    # Resolved player + resolved blossom.
                    # We will decide below whether this is new,
                    # already owned, or a duplicate.
                    # -------------------------------------------------

                else:

                    lines.append("")

                    if player_result:

                        lines.append(
                            f"**Task {task_number} — {action}**"
                        )

                        lines.append(
                            f"👤 `{lookup_name}` / s{server_number} "
                            f"→ **{resolved_player_name}** "
                            f"(player_id "
                            f"{player_result['player_id']}, "
                            f"{player_match_type})"
                        )

                    lines.append(
                        f"🌸 `{entry.get('task_text')}` "
                        f"→ ❓ **BLOSSOM NEEDS REVIEW**"
                    )

                    review_count += 1
                    unknown_blossom_entries.append(entry)

            else:

                lines.append("")

                if player_result:

                    lines.append(
                        f"**Task {task_number} — {action}**"
                    )

                    lines.append(
                        f"👤 `{lookup_name}` / s{server_number} "
                        f"→ **{resolved_player_name}** "
                        f"(player_id "
                        f"{player_result['player_id']}, "
                        f"{player_match_type})"
                    )

                lines.append(
                    f"🌸 `{entry.get('task_text')}` "
                    f"→ ❓ **BLOSSOM NEEDS REVIEW**"
                )

                review_count += 1
                unknown_blossom_entries.append(entry)

            # -----------------------------------------------------
            # If either player or blossom needs review, finish this
            # entry here.
            # -----------------------------------------------------

            if (
                not player_result
                or not canonical_blossom
                or (
                    blossom_result
                    and blossom_result.get("needs_review")
                )
            ):

                lines.append(
                    "➡️ 🚧 **NOT READY — "
                    "NEEDS REVIEW**"
                )

                continue

            # -----------------------------------------------------
            # Ownership preview
            # -----------------------------------------------------

            player_id = player_result["player_id"]

            current_gamename = get_current_player_gamename(
                player_id
            )

            if not current_gamename:

                lines.append("")

                lines.append(
                    f"**Task {task_number} — {action}**"
                )

                lines.append(
                    f"👤 `{lookup_name}` / s{server_number} "
                    f"→ **{resolved_player_name}** "
                    f"(player_id "
                    f"{player_result['player_id']}, "
                    f"{player_match_type})"
                )

                lines.append(
                    f"🌸 `{entry.get('task_text')}` "
                    f"→ **{canonical_blossom}**"
                )

                lines.append(
                    "➡️ ❌ **NOT READY — "
                    "CURRENT FLORIST NAME NOT FOUND**"
                )

                review_count += 1
                continue

            import_key = (
                current_gamename,
                canonical_blossom
            )

            # -----------------------------------------------------
            # If this flower was already staged during an earlier
            # pass of this import session, do not add it again.
            #
            # This is NOT a duplicate. It is already waiting in
            # the session for final confirmation.
            # -----------------------------------------------------

            if import_key in session_keys:

                lines.append("")
                lines.append(
                    f"**Task {task_number} — {action} "
                    f"— {current_gamename}**"
                )

                lines.append(
                    f"🌸 {entry.get('task_text')}"
                )

                lines.append(
                    "➡️ 📦 **ALREADY STAGED — "
                    "WILL IMPORT**"
                )

                continue

            # -----------------------------------------------------
            # Check duplicate within THIS processing pass.
            # -----------------------------------------------------

            if import_key in pending_keys:

                lines.append("")
                lines.append(
                    f"**Task {task_number} — {action} "
                    f"— {current_gamename}**"
                )

                lines.append(
                    f"🌸 {entry.get('task_text')}"
                )

                lines.append(
                    "➡️ 🔁 **DUPLICATE — SKIP**"
                )

                duplicate_count += 1
                continue

            pending_keys.add(import_key)

            # -----------------------------------------------------
            # Check existing ownership.
            # -----------------------------------------------------

            owned_blossoms = load_owned_blossoms(
                current_gamename
            )

            if canonical_blossom in owned_blossoms:

                lines.append("")
                lines.append(
                    f"**Task {task_number} — {action} "
                    f"— {current_gamename}**"
                )

                lines.append(
                    f"🌸 {entry.get('task_text')}"
                )

                lines.append(
                    "➡️ ⚠️ **ALREADY OWNED — SKIP**"
                )

                owned_count += 1

            else:

                # -------------------------------------------------
                # New entry — keep the detailed resolution output.
                # -------------------------------------------------

                lines.append("")
                lines.append(
                    f"**Task {task_number} — {action}**"
                )

                lines.append(
                    f"👤 `{lookup_name}` / s{server_number} "
                    f"→ **{resolved_player_name}** "
                    f"(player_id "
                    f"{player_result['player_id']}, "
                    f"{player_match_type})"
                )

                confidence = blossom_result.get(
                    "confidence",
                    0
                )

                blossom_match_type = blossom_result.get(
                    "match_type",
                    "unknown"
                )

                if blossom_match_type == "exact":
                    confidence = 1.0

                lines.append(
                    f"🌸 `{entry.get('task_text')}` "
                    f"→ **{canonical_blossom}** "
                    f"({confidence:.0%}, "
                    f"{blossom_match_type})"
                )

                lines.append(
                    f"🏷️ Current florist name: "
                    f"**{current_gamename}**"
                )

                lines.append(
                    "➡️ 🌸 **NEW — "
                    "READY TO IMPORT**"
                )

                new_count += 1

                pending_imports.append({
                    "gamename": current_gamename,
                    "blossom": canonical_blossom,
                })


        # ---------------------------------------------------------
        # Summary
        # ---------------------------------------------------------

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("📋 **Import preview summary**")

        lines.append(
            f"🌸 New blossoms: **{new_count}**"
        )

        lines.append(
            f"⚠️ Already owned: **{owned_count}**"
        )

        lines.append(
            f"🔁 Duplicates in this import: **{duplicate_count}**"
        )

        lines.append(
            f"🔍 Needs review: "
            f"**{review_count + len(unknown_entries)}**"
        )

        lines.append(
            f"👤 Player aliases to save: **{len(session.pending_aliases)}**"
        )

        lines.append("")
        lines.append(
            f"Reference data: "
            f"{len(player_aliases)} player aliases, "
            f"{len(blossom_names)} blossoms."
        )

        # ---------------------------------------------------------
        # Review status
        # ---------------------------------------------------------

        if unknown_entries:

            lines.append("")
            lines.append(
                "🔍 **One or more players need identification.**"
            )

        elif unknown_blossom_entries:
        
            lines.append("")
            lines.append(
                "🌸 **One or more flower names need identification.**"
            )
        
        
        elif review_count:

            lines.append("")
            lines.append(
                "🔍 **One or more entries need review.**"
            )

        else:

            lines.append("")
            lines.append(
                "✅ **All entries are ready for review.**"
            )

        # ---------------------------------------------------------
        # Build the appropriate UI
        # ---------------------------------------------------------

        if unknown_entries:

            lines.append("")
            lines.append(
                "Use **Identify Player** to resolve the "
                "unrecognized players."
            )

            view = TaskPlayerReviewView(
                player_aliases,
                unknown_entries,
                "\n".join(lines),
                resume_command="importtasklog",
                session=session
            )

            await interaction.followup.send(
                "\n".join(lines)[:1900],
                ephemeral=True,
                view=view
            )

        elif unknown_blossom_entries:

            lines.append("")
            lines.append(
                "Use **Identify Flower** to choose the correct "
                "canonical flower name."
            )

            view = TaskBlossomReviewView(
                blossom_names,
                unknown_blossom_entries,
                session
            )

            await interaction.followup.send(
                "\n".join(lines)[:1900],
                ephemeral=True,
                view=view
            )

        elif review_count:

            lines.append("")
            lines.append(
                "⚠️ **Import stopped — one or more blossom names "
                "could not be matched.**"
            )

            lines.append("")
            lines.append(
                "Please check the problematic flower name(s) in the "
                "game and BlossomHoard's blossom reference data."
            )

            lines.append(
                "If the game has renamed a flower, update the blossom "
                "reference data first, then run `/importtasklog` again."
            )

            await interaction.followup.send(
                "\n".join(lines)[:1900],
                ephemeral=True
            )

        elif pending_imports or session.pending_aliases:

            lines.append("")
            lines.append(
                "⚠️ **Nothing will be imported until you "
                "explicitly confirm below.**"
            )

            view = TaskImportConfirmView(
                session.pending_imports,
                session.pending_aliases
            )

            await interaction.followup.send(
                "\n".join(lines)[:1900],
                ephemeral=True,
                view=view
            )

        else:

            lines.append("")
            lines.append(
                "ℹ️ **There are no new blossoms to import.**"
            )

            await interaction.followup.send(
                "\n".join(lines)[:1900],
                ephemeral=True
            )

    except Exception as e:

        print(
            "ERROR IN /importtasklog:"
        )

        import traceback
        traceback.print_exc()

        try:
            await interaction.followup.send(
                "❌ **Import preview failed:**\n"
                f"```text\n{traceback.format_exc()[-3500:]}\n```",
                ephemeral=True
            )
        except Exception:
            pass
            
    finally:

        # ---------------------------------------------------------
        # Clean up temporary files
        # ---------------------------------------------------------

        try:

            for image_path in image_paths:
                if os.path.exists(image_path):
                    os.remove(image_path)

            for crop_path in crop_paths:
                if os.path.exists(crop_path):
                    os.remove(crop_path)

        except Exception:
            pass

@tree.command(
    name="importtasklog",
    description="Preview and import blossoms from 1-3 task-log screenshots"
)
@app_commands.describe(
    image1="Task-log screenshot 1",
    image2="Task-log screenshot 2 (optional)",
    image3="Task-log screenshot 3 (optional)"
)
async def importtasklog(
    interaction: discord.Interaction,
    image1: discord.Attachment,
    image2: discord.Attachment | None = None,
    image3: discord.Attachment | None = None
):
    await interaction.response.defer(ephemeral=True)

    images = [
        image
        for image in (
            image1,
            image2,
            image3
        )
        if image is not None
    ]

    session = TaskImportSession(
        images
    )

    await run_importtasklog(
        interaction,
        session
    )

# ════════════════════════════════════════════════════════════════════════════════
# RUN
# ════════════════════════════════════════════════════════════════════════════════

client.run(os.environ["DISCORD_TOKEN"])
