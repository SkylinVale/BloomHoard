import os
import discord
from discord import app_commands
from supabase import create_client, Client

# ════════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════════

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

RARITIES = ["Green", "Blue", "Purple", "Gold", "Red"]

# Point tiers in descending order — used by /whitelist algorithm
POINT_TIERS    = [30, 28, 25, 23, 21, 14, 9]
ALWAYS_INCLUDE = {30, 28, 25}  # always in whitelist regardless of count
MIN_FLOWERS    = 10            # per-florist minimum before stepping down tiers

# Fallback emojis shown when a tier icon hasn't been uploaded to config yet
TIER_FALLBACK = {30: "🔴", 28: "🟠", 25: "🟡", 23: "🟢", 21: "🔵", 14: "🟣", 9: "⚪"}

# Vase blossom slots — all nine fields checked when looking up which vases
# contain a given blossom
VASE_SLOTS = [
    "primary_1",   "primary_2",   "primary_3",
    "secondary_1", "secondary_2", "secondary_3",
    "tertiary_1",  "tertiary_2",  "tertiary_3",
]

PINK = discord.Color.from_rgb(255, 182, 193)

# ════════════════════════════════════════════════════════════════════════════════
# BOT SETUP
# ════════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user} — slash commands synced!")


# ════════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════════

STAFF_ROLES = {"Vice President", "Steward"}

def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.name in STAFF_ROLES for role in interaction.user.roles)


def get_config_icon(key: str) -> str | None:
    """Fetch one icon URL from the config table by key. Returns None if not set."""
    res = supabase.table("config").select("icon_url").eq("key", key).execute()
    return res.data[0]["icon_url"] if res.data else None


def rarity_icon(rarity: str) -> str:
    """Return the rarity icon URL for a given rarity name, or empty string."""
    return get_config_icon(f"rarity_{rarity.lower()}") or ""


def bonus_icon(bonus: int) -> str:
    """Return the bonus icon URL for +1 or +2, or empty string."""
    return get_config_icon(f"bonus_{bonus}") or ""


def tier_icon(pts: int) -> str:
    """Return the tier icon URL for a point value, falling back to an emoji."""
    return get_config_icon(f"tier_{pts}") or TIER_FALLBACK.get(pts, "•")


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
    supabase.table("players").insert({"gamename": gamename}).execute()
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

@tree.command(name="add", description="Add a blossom to a florist's hoard")
@app_commands.describe(
    gamename="The florist's in-game name",
    blossom="The blossom to add",
    bonus="Optional bonus: 1 for +1, 2 for +2",
)
@app_commands.autocomplete(gamename=florist_autocomplete, blossom=blossom_autocomplete)
async def add_ownership(
    interaction: discord.Interaction,
    gamename: str,
    blossom: str,
    bonus: int | None = None,
):
    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(
            f"❌ No florist named **{gamename}**. Use `/addflorist` first.", ephemeral=True
        )
        return
    if not supabase.table("blossoms").select("name").eq("name", blossom).execute().data:
        await interaction.response.send_message(
            f"❌ **{blossom}** isn't in the blossom database.", ephemeral=True
        )
        return
    if bonus is not None and bonus not in (1, 2):
        await interaction.response.send_message("❌ Bonus must be 1 or 2.", ephemeral=True)
        return
    if (
        supabase.table("ownership")
        .select("id")
        .eq("gamename", gamename)
        .eq("blossom", blossom)
        .execute()
        .data
    ):
        await interaction.response.send_message(
            f"🌸 **{gamename}** already has **{blossom}** in their hoard!", ephemeral=True
        )
        return
    supabase.table("ownership").insert({
        "gamename": gamename, "blossom": blossom, "bonus": bonus
    }).execute()
    bonus_str = f" (bonus: +{bonus})" if bonus else ""
    await interaction.response.send_message(
        f"🌱 Added **{blossom}**{bonus_str} to **{gamename}**'s hoard!", ephemeral=True
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
    bl = supabase.table("blossoms").select("*").eq("name", name).execute()
    if not bl.data:
        await interaction.response.send_message(
            f"❌ No blossom named **{name}** found.", ephemeral=True
        )
        return

    b = bl.data[0]
    embed = discord.Embed(title=f"🌸 {b['name']}", color=PINK)
    embed.add_field(name="Rarity", value=f"{rarity_icon(b['rarity'])} {b['rarity']}", inline=True)
    embed.add_field(name="Points", value=str(b["points"]),   inline=True)
    embed.add_field(name="Source", value=b["source"] or "—", inline=True)

    if b.get("thumbnail_url"):
        embed.set_thumbnail(url=b["thumbnail_url"])
    if b.get("image_url"):
        embed.set_image(url=b["image_url"])

    # Dynamically look up which vases contain this blossom
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

    # List florists who own this blossom
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

    await interaction.response.send_message(embed=embed)


@tree.command(name="myhoard", description="See all blossoms in a florist's hoard")
@app_commands.describe(gamename="The florist's in-game name")
@app_commands.autocomplete(gamename=florist_autocomplete)
async def my_hoard(interaction: discord.Interaction, gamename: str):
    if not supabase.table("players").select("id").eq("gamename", gamename).execute().data:
        await interaction.response.send_message(
            f"❌ No florist named **{gamename}** found.", ephemeral=True
        )
        return

    owned = (
        supabase.table("ownership")
        .select("blossom, bonus")
        .eq("gamename", gamename)
        .order("blossom")
        .execute()
    )
    if not owned.data:
        await interaction.response.send_message(
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

    lines = []
    for row in owned.data:
        b    = bl_map.get(row["blossom"], {})
        rico = rarity_icon(b["rarity"]) if b.get("rarity") else ""
        line = f"{rico} **{row['blossom']}** — {b.get('points', '?')} pts"
        if row.get("bonus"):
            line += f"  {bonus_icon(row['bonus'])} +{row['bonus']}"
        lines.append(line)

    embed = discord.Embed(
        title=f"🌷 {gamename}'s Hoard",
        description="\n".join(lines),
        color=PINK,
    )
    embed.set_footer(text=f"{len(owned.data)} blossom(s) in hoard")
    await interaction.response.send_message(embed=embed)


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

    for label, key in [
        ("Primary 1",   "primary_1"),   ("Primary 2",   "primary_2"),   ("Primary 3",   "primary_3"),
        ("Secondary 1", "secondary_1"), ("Secondary 2", "secondary_2"), ("Secondary 3", "secondary_3"),
        ("Tertiary 1",  "tertiary_1"),  ("Tertiary 2",  "tertiary_2"),  ("Tertiary 3",  "tertiary_3"),
    ]:
        if v.get(key):
            embed.add_field(name=label, value=v[key], inline=True)

    if v.get("image_url"):
        embed.set_image(url=v["image_url"])

    await interaction.response.send_message(embed=embed)


# ════════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS — admins only
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

    # Check blossom exists
    if not supabase.table("blossoms").select("id").eq("name", name).execute().data:
        await interaction.response.send_message(
            f"❌ No blossom named **{name}** was found.", ephemeral=True
        )
        return

    # Handle rename separately — must update ownership table too
    if new_name is not None:
        new_name = new_name.strip()
        if supabase.table("blossoms").select("id").eq("name", new_name).execute().data:
            await interaction.response.send_message(
                f"❌ A blossom named **{new_name}** already exists.", ephemeral=True
            )
            return
        supabase.table("ownership").update({"blossom": new_name}).eq("blossom", name).execute()
        supabase.table("blossoms").update({"name": new_name}).eq("name", name).execute()
        name = new_name  # continue updating other fields under the new name if provided

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

    if not updates and new_name is None:
        await interaction.response.send_message(
            "❌ You didn't provide any fields to update.", ephemeral=True
        )
        return

    changed = (["name"] if new_name else []) + list(updates.keys())
    await interaction.response.send_message(
        f"✅ **{new_name or name}** updated! Fields changed: {', '.join(changed)}", ephemeral=True
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
    tertiary_1="Tertiary blossom 1 (required)",
    primary_2="Primary blossom 2 (optional)",
    primary_3="Primary blossom 3 (optional)",
    secondary_2="Secondary blossom 2 (optional)",
    secondary_3="Secondary blossom 3 (optional)",
    tertiary_2="Tertiary blossom 2 (optional)",
    tertiary_3="Tertiary blossom 3 (optional)",
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
    # Validate that all provided blossom names exist in the database
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

    ownership = supabase.table("ownership").select("gamename, blossom").execute()
    if not ownership.data:
        await interaction.followup.send("❌ No ownership data found.", ephemeral=True)
        return

    blossom_names = list({row["blossom"] for row in ownership.data})
    points_map = {
        b["name"]: b["points"] for b in
        supabase.table("blossoms").select("name, points").in_("name", blossom_names).execute().data
    }

    # Group each florist's blossoms by point tier
    member_tiers: dict[str, dict[int, list[str]]] = {}
    for row in ownership.data:
        pts = points_map.get(row["blossom"])
        if pts not in POINT_TIERS:
            continue
        member_tiers.setdefault(row["gamename"], {}).setdefault(pts, []).append(row["blossom"])

    # Per-florist: always include 25/28/30, then step down until MIN_FLOWERS reached
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

    # Sort: by tier (points desc) then alpha within tier — or purely alphabetical
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
        description = description[:3800] + "\n…(list truncated — consider exporting from Supabase)"

    sort_label = "by tier (highest first)" if sort == "tier" else "alphabetically"
    embed = discord.Embed(
        title="🌺 Competition Whitelist",
        description=(
            f"Sorted {sort_label} · **{len(kept)} blossom(s)** "
            f"across **{len(member_tiers)} florist(s)**\n\u200b\n" + description
        ),
        color=PINK,
    )
    embed.set_footer(text="Any blossom not listed here is safe to remove from the competition.")
    await interaction.followup.send(embed=embed, ephemeral=True)


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
            "`/add <gamename> <blossom> [bonus]` — Add a blossom (bonus: 1 or 2, optional)\n"
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
            "`/whitelist [sort]` — Generate the optimal competition keep list"
        ),
        inline=False,
    )
    embed.set_footer(text="All name fields support autocomplete — start typing to see options!")
    await interaction.response.send_message(embed=embed)


# ════════════════════════════════════════════════════════════════════════════════
# RUN
# ════════════════════════════════════════════════════════════════════════════════

client.run(os.environ["DISCORD_TOKEN"])
