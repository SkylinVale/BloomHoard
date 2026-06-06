import os
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

# Rarity sort order for /myhoard and /keyhoard (Red first)
RARITY_ORDER = {r: i for i, r in enumerate(["Red", "Gold", "Purple", "Blue", "Green"])}

# Point tiers in descending order — used by /whitelist algorithm
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

PINK = discord.Color.from_rgb(255, 182, 193)

# Mountain Time is UTC-7 (MDT) or UTC-7 (MST); we use UTC-7 for Sunday 10pm
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
    await tree.sync(guild=MY_GUILD)
    print(f"Logged in as {client.user} — slash commands synced!")
    client.loop.create_task(weekly_cleardone())


# ════════════════════════════════════════════════════════════════════════════════
# WEEKLY AUTO-CLEAR DONE MARKERS
# ════════════════════════════════════════════════════════════════════════════════

async def weekly_cleardone():
    """Auto-clears all done markers every Sunday at 10pm Mountain Time."""
    await client.wait_until_ready()
    while not client.is_closed():
        now = datetime.now(MOUNTAIN_TZ)
        # Calculate seconds until next Sunday 22:00 Mountain
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 22:
            days_until_sunday = 7
        next_sunday = now.replace(hour=22, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
        wait_seconds = (next_sunday - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        supabase.table("players").update({"done": False}).eq("done", True).execute()
        print("Auto-cleared all done markers (Sunday 10pm Mountain)")


# ════════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.name in STAFF_ROLES for role in interaction.user.roles)


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
    """Sort by rarity (Red first), then points desc, then name asc."""
    return (
        RARITY_ORDER.get(b.get("rarity", "Green"), 99),
        -b.get("points", 0),
        b.get("name", ""),
    )


async def send_hoard_embeds(interaction, gamename: str, rows: list, bl_map: dict, title_prefix: str):
    """Build and send one or more embeds for a hoard list."""
    lines = []
    for row in rows:
        b    = bl_map.get(row["blossom"], {})
        rico = rarity_icon(b.get("rarity", "")) if b.get("rarity") else ""
        line = f"{rico} **{row['blossom']}** — {b.get('points', '?')} pts"
        if row.get("bonus"):
            line += f"  {bonus_icon(row['bonus'])} +{row['bonus']}"
        lines.append(line)

    chunks = []
    current, cur_len = [], 0
    for line in lines:
        if cur_len + len(line) + 1 > 3800:
            chunks.append(current)
            current, cur_len = [line], len(line)
        else:
            current.append(line)
            cur_len += len(line) + 1
    if current:
        chunks.append(current)

    total = len(rows)
    for i, chunk in enumerate(chunks):
        title = f"{title_prefix}" if i == 0 else f"{title_prefix} (cont.)"
        embed = discord.Embed(title=title, description="\n".join(chunk), color=PINK)
        if i == len(chunks) - 1:
            embed.set_footer(text=f"{total} blossom(s)")
        await interaction.followup.send(embed=embed)


# ════════════════════════════════════════════════════════════════════════════════
# AUTOCOMPLETE
# ════════════════════════════════════════════════════════════════════════════════

async def florist_autocomplete(interaction: discord.Interaction, current: str):
    res = (
        supabase.table("players")
        .select("gamename")
        .ilike("gamename", f"{current}%")
        .limit(10)
        .execute()
    )
    return [app_commands.Choice(name=r["gamename"], value=r["gamename"]) for r in res.data]


async def blossom_autocomplete(interaction: discord.Interaction, current: str):
    res = (
        supabase.table("blossoms")
        .select("name")
        .ilike("name", f"{current}%")
        .limit(10)
        .execute()
    )
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in res.data]


async def vase_autocomplete(interaction: discord.Interaction, current: str):
    res = (
        supabase.table("vases")
        .select("name")
        .ilike("name", f"{current}%")
        .limit(10)
        .execute()
    )
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in res.data]


# ════════════════════════════════════════════════════════════════════════════════
# FLORIST COMMANDS — anyone can use
# ════════════════════════════════════════════════════════════════════════════════

@tree.command(name="addflorist", description="Add a new florist to the database")
@app_commands.describe(gamename="The florist's in-game name")
async def add_florist(interaction: discord.Interaction, gamename: str):
    gamename = gamename.strip()
    if supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(
            f"🌿 **{gamename}** is already registered as a florist!", ephemeral=True
        )
        return
    supabase.table("players").insert({"gamename": gamename, "done": False}).execute()
    await interaction.response.send_message(
        f"🌱 **{gamename}** has been added as a florist!", ephemeral=True
    )


@tree.command(name="removeflorist", description="Remove a florist and all their blossoms")
@app_commands.describe(gamename="The florist's in-game name")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def remove_florist(interaction: discord.Interaction, gamename: str):
    gamename = gamename.strip()
    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(
            f"❌ No florist named **{gamename}** was found.", ephemeral=True
        )
        return
    supabase.table("ownership").delete().eq("gamename", gamename).execute()
    supabase.table("players").delete().eq("gamename", gamename).execute()
    await interaction.response.send_message(
        f"🍂 **{gamename}** and all their blossoms have been removed.", ephemeral=True
    )


# ════════════════════════════════════════════════════════════════════════════════
# OWNERSHIP COMMANDS — anyone can use
# ════════════════════════════════════════════════════════════════════════════════

@tree.command(name="add", description="Add up to 10 blossoms to a florist's hoard at once")
@app_commands.describe(
    gamename="The florist's in-game name",
    blossom1="Blossom 1",
    blossom2="Blossom 2 (optional)", blossom3="Blossom 3 (optional)",
    blossom4="Blossom 4 (optional)", blossom5="Blossom 5 (optional)",
    blossom6="Blossom 6 (optional)", blossom7="Blossom 7 (optional)",
    blossom8="Blossom 8 (optional)", blossom9="Blossom 9 (optional)",
    blossom10="Blossom 10 (optional)",
    bonus="Optional bonus for all listed blossoms: 1 for +1, 2 for +2",
)
@app_commands.autocomplete(
    gamename=florist_autocomplete,
    blossom1=blossom_autocomplete, blossom2=blossom_autocomplete,
    blossom3=blossom_autocomplete, blossom4=blossom_autocomplete,
    blossom5=blossom_autocomplete, blossom6=blossom_autocomplete,
    blossom7=blossom_autocomplete, blossom8=blossom_autocomplete,
    blossom9=blossom_autocomplete, blossom10=blossom_autocomplete,
)
async def add_ownership(
    interaction: discord.Interaction,
    gamename: str,
    blossom1: str,
    blossom2: str | None = None, blossom3: str | None = None,
    blossom4: str | None = None, blossom5: str | None = None,
    blossom6: str | None = None, blossom7: str | None = None,
    blossom8: str | None = None, blossom9: str | None = None,
    blossom10: str | None = None,
    bonus: int | None = None,
):
    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(
            f"❌ No florist named **{gamename}**. Use `/addflorist` first.", ephemeral=True
        )
        return
    if bonus is not None and bonus not in (1, 2):
        await interaction.response.send_message("❌ Bonus must be 1 or 2.", ephemeral=True)
        return

    requested = [b for b in [
        blossom1, blossom2, blossom3, blossom4, blossom5,
        blossom6, blossom7, blossom8, blossom9, blossom10
    ] if b]

    # Validate all blossoms exist
    valid_names = {
        r["name"] for r in
        supabase.table("blossoms").select("name").in_("name", requested).execute().data
    }
    invalid = [b for b in requested if b not in valid_names]
    if invalid:
        await interaction.response.send_message(
            f"❌ These blossoms aren't in the database: {', '.join(invalid)}", ephemeral=True
        )
        return

    # Check which are already owned
    already = {
        r["blossom"] for r in
        supabase.table("ownership").select("blossom")
        .eq("gamename", gamename).in_("blossom", requested).execute().data
    }

    to_add = [b for b in requested if b not in already]
    if to_add:
        supabase.table("ownership").insert([
            {"gamename": gamename, "blossom": b, "bonus": bonus} for b in to_add
        ]).execute()

    lines = []
    if to_add:
        bonus_str = f" (bonus: +{bonus})" if bonus else ""
        lines.append(f"🌱 Added{bonus_str}:\n" + "\n".join(f"• {b}" for b in to_add))
    if already:
        lines.append(f"⚠️ Already in hoard (skipped):\n" + "\n".join(f"• {b}" for b in already))

    await interaction.response.send_message(
        f"**{gamename}'s hoard update:**\n" + "\n".join(lines), ephemeral=True
    )


@tree.command(name="remove", description="Remove a blossom from a florist's hoard")
@app_commands.describe(
    gamename="The florist's in-game name",
    blossom="The blossom to remove",
)
@app_commands.autocomplete(gamename=florist_autocomplete, blossom=blossom_autocomplete)
async def remove_ownership(interaction: discord.Interaction, gamename: str, blossom: str):
    res = (
        supabase.table("ownership")
        .delete()
        .eq("gamename", gamename)
        .eq("blossom", blossom)
        .execute()
    )
    if res.data:
        await interaction.response.send_message(
            f"🍂 Removed **{blossom}** from **{gamename}**'s hoard.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ **{gamename}** doesn't have **{blossom}** in their hoard.", ephemeral=True
        )


@tree.command(name="setbonus", description="Set or update the bonus for a florist's blossom")
@app_commands.describe(
    gamename="The florist's in-game name",
    blossom="The blossom to update",
    bonus="Bonus value: 1 for +1, 2 for +2",
)
@app_commands.autocomplete(gamename=florist_autocomplete, blossom=blossom_autocomplete)
async def set_bonus(interaction: discord.Interaction, gamename: str, blossom: str, bonus: int):
    if bonus not in (1, 2):
        await interaction.response.send_message("❌ Bonus must be 1 or 2.", ephemeral=True)
        return
    res = (
        supabase.table("ownership")
        .update({"bonus": bonus})
        .eq("gamename", gamename)
        .eq("blossom", blossom)
        .execute()
    )
    if res.data:
        await interaction.response.send_message(
            f"✅ Set **+{bonus}** bonus on **{blossom}** for **{gamename}**.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ **{gamename}** doesn't have **{blossom}** in their hoard.", ephemeral=True
        )


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
        await interaction.followup.send(
            f"❌ No blossom named **{name}** found.", ephemeral=True
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

    all_vases = (
        supabase.table("vases")
        .select("name, " + ", ".join(VASE_SLOTS))
        .execute()
    )
    matching = sorted(
        v["name"] for v in all_vases.data
        if name in [v.get(slot) for slot in VASE_SLOTS]
    )
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
        embed.add_field(
            name=f"Florists ({len(owners.data)})",
            value="\n".join(lines),
            inline=False,
        )
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
        await interaction.followup.send(
            f"❌ No florist named **{gamename}** found.", ephemeral=True
        )
        return

    owned = (
        supabase.table("ownership")
        .select("blossom, bonus")
        .eq("gamename", gamename)
        .execute()
    )
    if not owned.data:
        await interaction.followup.send(
            f"🌱 **{gamename}**'s hoard is empty! Use `/add` to start adding blossoms."
        )
        return

    blossom_names = [row["blossom"] for row in owned.data]
    bl_map = {
        b["name"]: b for b in
        supabase.table("blossoms")
        .select("name, points, rarity")
        .in_("name", blossom_names)
        .execute()
        .data
    }

    owned_sorted = sorted(
        owned.data,
        key=lambda row: sort_key_rarity_points_alpha(bl_map.get(row["blossom"], {}))
    )

    lines = []
    for row in owned_sorted:
        b    = bl_map.get(row["blossom"], {})
        rico = rarity_icon(b.get("rarity", "")) if b.get("rarity") else ""
        line = f"{rico} **{row['blossom']}** — {b.get('points', '?')} pts"
        if row.get("bonus"):
            line += f"  {bonus_icon(row['bonus'])} +{row['bonus']}"
        lines.append(line)

    chunks = []
    current, cur_len = [], 0
    for line in lines:
        if cur_len + len(line) + 1 > 3800:
            chunks.append(current)
            current, cur_len = [line], len(line)
        else:
            current.append(line)
            cur_len += len(line) + 1
    if current:
        chunks.append(current)

    total = len(owned_sorted)
    for i, chunk in enumerate(chunks):
        title = f"🌷 {gamename}'s Hoard" if i == 0 else f"🌷 {gamename}'s Hoard (cont.)"
        embed = discord.Embed(title=title, description="\n".join(chunk), color=PINK)
        if i == len(chunks) - 1:
            embed.set_footer(text=f"{total} blossom(s) in hoard")
        await interaction.followup.send(embed=embed)


@tree.command(name="keyhoard", description="Show only the blossoms from a florist's hoard that count toward the competition whitelist")
@app_commands.describe(gamename="The florist's in-game name")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def key_hoard(interaction: discord.Interaction, gamename: str):
    await interaction.response.defer()

    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.followup.send(
            f"❌ No florist named **{gamename}** found.", ephemeral=True
        )
        return

    owned = (
        supabase.table("ownership")
        .select("blossom, bonus")
        .eq("gamename", gamename)
        .execute()
    )
    if not owned.data:
        await interaction.followup.send(
            f"🌱 **{gamename}**'s hoard is empty!"
        )
        return

    blossom_names = [row["blossom"] for row in owned.data]
    bl_map = {
        b["name"]: b for b in
        supabase.table("blossoms")
        .select("name, points, rarity")
        .in_("name", blossom_names)
        .execute()
        .data
    }

    # Run the whitelist algorithm for just this florist
    tiers: dict[int, list[str]] = {}
    for row in owned.data:
        pts = bl_map.get(row["blossom"], {}).get("points")
        if pts in POINT_TIERS:
            tiers.setdefault(pts, []).append(row["blossom"])

    member_keep: set[str] = set()
    for tier in ALWAYS_INCLUDE:
        if tier in tiers:
            member_keep.update(tiers[tier])
    for tier in POINT_TIERS:
        if tier in ALWAYS_INCLUDE:
            continue
        if len(member_keep) >= MIN_FLOWERS:
            break
        if tier in tiers:
            member_keep.update(tiers[tier])

    if not member_keep:
        await interaction.followup.send(
            f"🌾 **{gamename}** has no blossoms that qualify for the whitelist."
        )
        return

    key_rows = [row for row in owned.data if row["blossom"] in member_keep]
    key_rows_sorted = sorted(
        key_rows,
        key=lambda row: sort_key_rarity_points_alpha(bl_map.get(row["blossom"], {}))
    )

    await send_hoard_embeds(
        interaction, gamename, key_rows_sorted, bl_map, f"🔑 {gamename}'s Key Hoard"
    )


@tree.command(name="vase", description="Display info about a vase")
@app_commands.describe(name="The vase to look up")
@app_commands.autocomplete(name=vase_autocomplete)
async def vase_info(interaction: discord.Interaction, name: str):
    res = supabase.table("vases").select("*").eq("name", name).execute()
    if not res.data:
        await interaction.response.send_message(
            f"❌ No vase named **{name}** found.", ephemeral=True
        )
        return

    v = res.data[0]
    embed = discord.Embed(title=f"🏺 {v['name']}", color=PINK)

    # Fetch rarity for each blossom slot to display correct icon
    all_blossom_names = [v[k] for k in VASE_SLOTS if v.get(k)]
    rarity_map = {}
    if all_blossom_names:
        rarity_res = supabase.table("blossoms").select("name, rarity").in_("name", all_blossom_names).execute()
        rarity_map = {b["name"]: b["rarity"] for b in rarity_res.data}

    def blossom_line(name):
        ico = rarity_icon(rarity_map.get(name, "")) if rarity_map.get(name) else "🌸"
        return f"{ico} {name}"

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

    await interaction.response.send_message(embed=embed)


@tree.command(name="noticeboard", description="[Admin] Display the current notice board")
async def noticeboard(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can view the notice board.", ephemeral=True)
        return
    res = supabase.table("notices").select("gamename, notice").order("created_at").execute()
    if not res.data:
        await interaction.response.send_message(
            "📋 The notice board is empty.", ephemeral=False
        )
        return
    embed = discord.Embed(title="📋 Notice Board", color=PINK)
    for row in res.data:
        embed.add_field(name=f"🌿 {row['gamename']}", value=row["notice"], inline=False)
    await interaction.response.send_message(embed=embed)


@tree.command(name="blossomhelp", description="Show how to use the Blossom Hoard Bot")
async def blossom_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌸 Blossom Hoard Bot — Help",
        description="Here's a quick guide to all available commands.",
        color=PINK,
    )
    embed.add_field(
        name="🌿 Florists",
        value=(
            "`/addflorist <gamename>` — Register a new florist\n"
            "`/removeflorist <gamename>` — Remove a florist and their hoard"
        ),
        inline=False,
    )
    embed.add_field(
        name="🌱 Managing a Hoard",
        value=(
            "`/add <gamename> <blossom(s)> [bonus]` — Add up to 10 blossoms at once\n"
            "`/remove <gamename> <blossom>` — Remove a blossom from a hoard\n"
            "`/setbonus <gamename> <blossom> <bonus>` — Set or update a +1 or +2 bonus"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔍 Looking Things Up",
        value=(
            "`/blossom <name>` — Show blossom info, vases it appears in, and who owns it\n"
            "`/myhoard <gamename>` — Show all blossoms in a florist's hoard\n"
            "`/keyhoard <gamename>` — Show only the blossoms that count toward the whitelist\n"
            "`/vase <name>` — Show vase info and its blossom slots"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔒 Admin Only",
        value=(
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
        ),
        inline=False,
    )
    embed.set_footer(text="All name fields support autocomplete — start typing to see options!")
    await interaction.response.send_message(embed=embed)


# ════════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS — admins and staff only
# ════════════════════════════════════════════════════════════════════════════════

@tree.command(name="addblossom", description="[Admin] Add a new blossom to the database")
@app_commands.describe(
    name="Blossom name",
    rarity="Rarity: Green, Blue, Purple, Gold, or Red",
    points="Point value: 30, 28, 25, 23, 21, 14, or 9",
    source="Where the blossom comes from",
    image_url="Full image URL from Supabase Storage (optional — add later via /updateblossom)",
    thumbnail_url="Thumbnail URL from Supabase Storage (optional — add later via /updateblossom)",
)
async def add_blossom(
    interaction: discord.Interaction,
    name: str,
    rarity: str,
    points: int,
    source: str,
    image_url: str | None = None,
    thumbnail_url: str | None = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can add blossoms.", ephemeral=True)
        return
    rarity = rarity.strip().capitalize()
    if rarity not in RARITIES:
        await interaction.response.send_message(
            f"❌ Invalid rarity. Choose from: {', '.join(RARITIES)}", ephemeral=True
        )
        return
    if supabase.table("blossoms").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(
            f"❌ **{name}** already exists in the blossom database.", ephemeral=True
        )
        return
    supabase.table("blossoms").insert({
        "name": name, "rarity": rarity, "points": points, "source": source,
        "image_url": image_url, "thumbnail_url": thumbnail_url,
    }).execute()
    await interaction.response.send_message(
        f"🌸 **{name}** has been added to the blossom database!", ephemeral=True
    )


@tree.command(name="updateblossom", description="[Admin] Update any field on an existing blossom")
@app_commands.describe(
    name="The blossom to update",
    new_name="Rename the blossom (leave blank to keep existing)",
    rarity="New rarity (leave blank to keep existing)",
    points="New point value (leave blank to keep existing)",
    source="New source (leave blank to keep existing)",
    image_url="New image URL (leave blank to keep existing)",
    thumbnail_url="New thumbnail URL (leave blank to keep existing)",
)
@app_commands.autocomplete(name=blossom_autocomplete)
async def update_blossom(
    interaction: discord.Interaction,
    name: str,
    new_name: str | None = None,
    rarity: str | None = None,
    points: int | None = None,
    source: str | None = None,
    image_url: str | None = None,
    thumbnail_url: str | None = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can update blossoms.", ephemeral=True)
        return
    if not supabase.table("blossoms").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(
            f"❌ No blossom named **{name}** was found.", ephemeral=True
        )
        return

    changed = []

    # Handle rename — ON UPDATE CASCADE automatically updates ownership and vases
    if new_name is not None:
        new_name = new_name.strip()
        if supabase.table("blossoms").select("id").eq("name", new_name).execute().data:
            await interaction.response.send_message(
                f"❌ A blossom named **{new_name}** already exists.", ephemeral=True
            )
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
            await interaction.response.send_message(
                f"❌ Invalid rarity. Choose from: {', '.join(RARITIES)}", ephemeral=True
            )
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
        await interaction.response.send_message(
            "❌ You didn't provide any fields to update.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ **{name}** updated! Fields changed: {', '.join(changed)}", ephemeral=True
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
        await interaction.response.send_message(
            f"🍂 **{name}** has been removed from the database.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ No blossom named **{name}** was found.", ephemeral=True
        )


@tree.command(name="addvase", description="[Admin] Add a new vase to the database")
@app_commands.describe(
    name="Vase name",
    primary_1="Primary blossom 1 (required)",
    secondary_1="Secondary blossom 1 (required)",
    tertiary_1="Accent blossom 1 (required)",
    primary_2="Primary blossom 2 (optional)",
    primary_3="Primary blossom 3 (optional)",
    secondary_2="Secondary blossom 2 (optional)",
    secondary_3="Secondary blossom 3 (optional)",
    tertiary_2="Accent blossom 2 (optional)",
    tertiary_3="Accent blossom 3 (optional)",
    image_url="Image URL from Supabase Storage (optional — add later via Supabase dashboard)",
)
@app_commands.autocomplete(
    primary_1=blossom_autocomplete,   primary_2=blossom_autocomplete,   primary_3=blossom_autocomplete,
    secondary_1=blossom_autocomplete, secondary_2=blossom_autocomplete, secondary_3=blossom_autocomplete,
    tertiary_1=blossom_autocomplete,  tertiary_2=blossom_autocomplete,  tertiary_3=blossom_autocomplete,
)
async def add_vase(
    interaction: discord.Interaction,
    name: str,
    primary_1: str,
    secondary_1: str,
    tertiary_1: str,
    primary_2: str | None = None,
    primary_3: str | None = None,
    secondary_2: str | None = None,
    secondary_3: str | None = None,
    tertiary_2: str | None = None,
    tertiary_3: str | None = None,
    image_url: str | None = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can add vases.", ephemeral=True)
        return
    if supabase.table("vases").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(
            f"❌ **{name}** already exists in the vase database.", ephemeral=True
        )
        return
    provided = [b for b in [
        primary_1, primary_2, primary_3,
        secondary_1, secondary_2, secondary_3,
        tertiary_1, tertiary_2, tertiary_3,
    ] if b]
    valid_names = {
        r["name"] for r in
        supabase.table("blossoms").select("name").in_("name", provided).execute().data
    }
    invalid = [b for b in provided if b not in valid_names]
    if invalid:
        await interaction.response.send_message(
            f"❌ These blossoms aren't in the database yet: {', '.join(invalid)}", ephemeral=True
        )
        return
    supabase.table("vases").insert({
        "name": name, "image_url": image_url,
        "primary_1": primary_1,     "primary_2": primary_2,     "primary_3": primary_3,
        "secondary_1": secondary_1, "secondary_2": secondary_2, "secondary_3": secondary_3,
        "tertiary_1": tertiary_1,   "tertiary_2": tertiary_2,   "tertiary_3": tertiary_3,
    }).execute()
    await interaction.response.send_message(
        f"🏺 **{name}** has been added to the vase database!", ephemeral=True
    )


@tree.command(name="updatevase", description="[Admin] Update any field on an existing vase")
@app_commands.describe(
    name="The vase to update",
    new_name="Rename the vase (leave blank to keep existing)",
    primary_1="New primary blossom 1",   primary_2="New primary blossom 2",   primary_3="New primary blossom 3",
    secondary_1="New secondary blossom 1", secondary_2="New secondary blossom 2", secondary_3="New secondary blossom 3",
    tertiary_1="New accent blossom 1",   tertiary_2="New accent blossom 2",   tertiary_3="New accent blossom 3",
    image_url="New image URL",
)
@app_commands.autocomplete(
    name=vase_autocomplete,
    primary_1=blossom_autocomplete,   primary_2=blossom_autocomplete,   primary_3=blossom_autocomplete,
    secondary_1=blossom_autocomplete, secondary_2=blossom_autocomplete, secondary_3=blossom_autocomplete,
    tertiary_1=blossom_autocomplete,  tertiary_2=blossom_autocomplete,  tertiary_3=blossom_autocomplete,
)
async def update_vase(
    interaction: discord.Interaction,
    name: str,
    new_name: str | None = None,
    primary_1: str | None = None,   primary_2: str | None = None,   primary_3: str | None = None,
    secondary_1: str | None = None, secondary_2: str | None = None, secondary_3: str | None = None,
    tertiary_1: str | None = None,  tertiary_2: str | None = None,  tertiary_3: str | None = None,
    image_url: str | None = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can update vases.", ephemeral=True)
        return
    if not supabase.table("vases").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(
            f"❌ No vase named **{name}** was found.", ephemeral=True
        )
        return

    changed = []

    if new_name is not None:
        new_name = new_name.strip()
        if supabase.table("vases").select("id").eq("name", new_name).execute().data:
            await interaction.response.send_message(
                f"❌ A vase named **{new_name}** already exists.", ephemeral=True
            )
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
        # Validate any blossom fields
        blossom_fields = {k: v for k, v in updates.items() if k != "image_url"}
        if blossom_fields:
            valid_names = {
                r["name"] for r in
                supabase.table("blossoms").select("name").in_("name", list(blossom_fields.values())).execute().data
            }
            invalid = [v for v in blossom_fields.values() if v not in valid_names]
            if invalid:
                await interaction.response.send_message(
                    f"❌ These blossoms aren't in the database: {', '.join(invalid)}", ephemeral=True
                )
                return
        supabase.table("vases").update(updates).eq("name", name).execute()
        changed.extend(updates.keys())

    if not changed:
        await interaction.response.send_message(
            "❌ You didn't provide any fields to update.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ **{name}** updated! Fields changed: {', '.join(changed)}", ephemeral=True
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
        await interaction.response.send_message(
            f"✅ **{gamename}** has been marked as done for this week.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ No florist named **{gamename}** was found.", ephemeral=True
        )


@tree.command(name="cleardone", description="[Admin] Remove all done markers from all florists")
async def clear_done(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can clear done markers.", ephemeral=True)
        return
    supabase.table("players").update({"done": False}).eq("done", True).execute()
    await interaction.response.send_message(
        "✅ All done markers have been cleared.", ephemeral=True
    )


@tree.command(name="addnotice", description="[Admin] Add a notice for a florist on the notice board")
@app_commands.describe(
    gamename="The florist's in-game name",
    notice="The notice to display (max 200 characters)",
)
@app_commands.autocomplete(gamename=florist_autocomplete)
async def add_notice(interaction: discord.Interaction, gamename: str, notice: str):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can add notices.", ephemeral=True)
        return
    if len(notice) > 200:
        await interaction.response.send_message(
            f"❌ Notice is too long ({len(notice)} characters). Maximum is 200.", ephemeral=True
        )
        return
    # Upsert — replace existing notice if one already exists for this florist
    supabase.table("notices").upsert(
        {"gamename": gamename, "notice": notice},
        on_conflict="gamename"
    ).execute()
    await interaction.response.send_message(
        f"📋 Notice added for **{gamename}**.", ephemeral=True
    )


@tree.command(name="removenotice", description="[Admin] Remove a florist's notice from the notice board")
@app_commands.describe(gamename="The florist whose notice to remove")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def remove_notice(interaction: discord.Interaction, gamename: str):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can remove notices.", ephemeral=True)
        return
    res = supabase.table("notices").delete().eq("gamename", gamename).execute()
    if res.data:
        await interaction.response.send_message(
            f"📋 Notice for **{gamename}** has been removed.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ No notice found for **{gamename}**.", ephemeral=True
        )


@tree.command(name="whitelist", description="[Admin] Generate the optimal blossom keep list for a competition")
@app_commands.describe(sort="How to sort the results")
@app_commands.choices(sort=[
    app_commands.Choice(name="By tier — highest first, alphabetical within tier", value="tier"),
    app_commands.Choice(name="Alphabetically — A to Z",                           value="alpha"),
])
async def whitelist(interaction: discord.Interaction, sort: str = "tier"):
    if not is_admin(interaction):
        await interaction.response.send_message("🚫 Only admins can run the whitelist.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    # Exclude florists marked as done
    all_players = supabase.table("players").select("gamename, done").execute().data
    done_players = {p["gamename"] for p in all_players if p.get("done")}
    active_players = {p["gamename"] for p in all_players if not p.get("done")}

    if not active_players:
        await interaction.followup.send("❌ All florists are marked as done.", ephemeral=True)
        return

    ownership = (
        supabase.table("ownership")
        .select("gamename, blossom")
        .in_("gamename", list(active_players))
        .execute()
    )
    if not ownership.data:
        await interaction.followup.send("❌ No ownership data found for active florists.", ephemeral=True)
        return

    blossom_names = list({row["blossom"] for row in ownership.data})
    blossom_details = {
        b["name"]: b for b in
        supabase.table("blossoms").select("name, points, rarity").in_("name", blossom_names).execute().data
    }

    member_tiers: dict[str, dict[int, list[str]]] = {}
    for row in ownership.data:
        b = blossom_details.get(row["blossom"], {})
        pts = b.get("points")
        if pts not in POINT_TIERS:
            continue
        # Skip Green (Ordinary) rarity — everyone has them, no need to whitelist
        if b.get("rarity") == "Green":
            continue
        member_tiers.setdefault(row["gamename"], {}).setdefault(pts, []).append(row["blossom"])

    keep: set[str] = set()
    for tiers in member_tiers.values():
        member_keep: set[str] = set()
        for tier in ALWAYS_INCLUDE:
            if tier in tiers:
                member_keep.update(tiers[tier])
        for tier in POINT_TIERS:
            if tier in ALWAYS_INCLUDE:
                continue
            if len(member_keep) >= MIN_FLOWERS:
                break
            if tier in tiers:
                member_keep.update(tiers[tier])
        keep.update(member_keep)

    if not keep:
        await interaction.followup.send("❌ No blossoms qualify for the whitelist.", ephemeral=True)
        return

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

    lines = []
    for b in kept:
        ico = rarity_icon(b["rarity"]) if b.get("rarity") else ""
        lines.append(f"{ico} **{b['name']}** — {b['points']} pts")

    description = "\n".join(lines)
    if len(description) > 3800:
        description = description[:3800] + "\n…(list truncated)"

    sort_label = "by tier (highest first)" if sort == "tier" else "alphabetically"
    embed = discord.Embed(
        title="🌺 Competition Whitelist",
        description=(
            f"Sorted {sort_label} · **{len(kept)} blossom(s)** "
            f"across **{len(member_tiers)} active florist(s)**\n\u200b\n" + description
        ),
        color=PINK,
    )

    if done_players:
        embed.add_field(
            name="✅ Florists marked as done",
            value="\n".join(f"• {p}" for p in sorted(done_players)),
            inline=False,
        )

    embed.set_footer(text="Any blossom not listed here is safe to remove from the competition.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ════════════════════════════════════════════════════════════════════════════════
# RUN
# ════════════════════════════════════════════════════════════════════════════════

client.run(os.environ["DISCORD_TOKEN"])
