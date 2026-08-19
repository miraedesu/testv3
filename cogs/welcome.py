"""AI welcome-card generation, /test, /say, /avatar, /banner, /server."""
from __future__ import annotations

import asyncio
import io
import os

import discord
from discord import app_commands
from discord.ext import commands
import logging
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont, ImageOps

from common.constants import AI_MODEL


openrouter_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)
logger = logging.getLogger(__name__)

async def generate_welcome_card(member: discord.Member, bot_instance: discord.Client) -> discord.File:
    username = f"{member.name}"
    user_id = f"{member.id}"
    custom_paragraph = f"Roast {member.name} in 10-20 words"
    try:
        # 1. Send the request to OpenRouter
        response = await openrouter_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a witty, highly sarcastic, and clever Discord bot. "
                        "Your job is to deliver a sharp, lighthearted roast welcoming a new user. "
                        "CRITICAL SAFETY RULES:\n"
                        "- Strict NO to racism, sexism, homophobia, transphobia, or any form of hate speech.\n"
                        "- Do NOT mention body shaming, weight, physical appearance, or mental health.\n"
                        "- Avoid genuine harassment or cruelty. The tone must remain a comedic, friendly 'burn' among friends.\n"
                        "- Keep it safe for work (NSFW content is forbidden).\n"
                        "- If the username violates safety rules or is un-roastable, reply ONLY with the word: 'Error'.\n\n"
                        "FORMATTING RULE:\n"
                        "- Do NOT include any introductory greetings like 'Welcome', 'Welcome back', or the user's name at the start. "
                        "- Start generating the roast directly. Your entire response will be appended directly to a 'Welcome [Name]!' message."
                    )
                },
                {
                    "role": "user",
                    "content": custom_paragraph
                }
            ],
            max_tokens=50
        )

        # 2. Safely grab the text from the response object
        roast_text = None
        if response and response.choices:
            roast_text = response.choices[0].message.content

        # 3. Check if the AI failed, safety-blocked itself, or outputted 'Error'
        if not roast_text or "error" in roast_text.lower() or "cannot fulfill" in roast_text.lower():
            roast_text = f"Welcome {member.name}! I wanted to roast you, but your username is so boring it broke my AI."
        else:
            roast_text = f"{roast_text.strip()}"

    except Exception as e:
        # 4. Fallback if the entire API crashes or times out
        logger.error(f"OpenRouter API Error: {e}")
        roast_text = f"Welcome {member.mention}! Glad you made it here."

    created_date = member.created_at.strftime('%b %d, %Y')
    joined_date = member.joined_at.strftime(
        '%b %d, %Y') if member.joined_at else "N/A"

    now = discord.utils.utcnow()
    age_days = (now - member.created_at).days

    if age_days <= 30:
        age_icon_file = "assets/create30d.png"
        age_text = f"{age_days} days ago" if age_days > 0 else "New Today"
    elif age_days <= 365:
        age_icon_file = "assets/create0y.png"
        months = max(1, age_days // 30)
        age_text = f"{months} months ago"
    elif age_days <= 730:
        age_icon_file = "assets/create1y.png"
        years = age_days // 365
        age_text = f"{years} year ago"
    elif age_days <= 1095:
        age_icon_file = "assets/create2y.png"
        years = age_days // 365
        age_text = f"{years} years ago"
    elif age_days <= 1460:
        age_icon_file = "assets/create3y.png"
        years = age_days // 365
        age_text = f"{years} years ago"
    elif age_days <= 1825:
        age_icon_file = "assets/create4y.png"
        years = age_days // 365
        age_text = f"{years} years ago"
    else:
        age_icon_file = "assets/create5plus.png"
        years = age_days // 365
        age_text = f"{years} years ago"

    try:
        user = await bot_instance.fetch_user(member.id)
        if user.banner:
            banner_bytes = await user.banner.read()
            banner_img = Image.open(io.BytesIO(banner_bytes))
        else:
            banner_img = Image.open("assets/default_banner.png").convert("RGBA")
    except Exception as e:
        logger.error(f"Banner fetch failed, falling back: {e}")
        try:
            banner_img = Image.open("assets/default_banner.png").convert("RGBA")
        except FileNotFoundError:
            banner_img = Image.new("RGBA", (310, 110), (88, 101, 242, 255))

    try:
        avatar_bytes = await member.display_avatar.read()
        avatar_img = Image.open(io.BytesIO(avatar_bytes))
    except Exception:
        avatar_img = Image.new("RGBA", (90, 90), (88, 101, 242, 255))
    try:
        if member.guild.icon:
            s_icon_bytes = await member.guild.icon.read()
            s_icon_raw = Image.open(io.BytesIO(s_icon_bytes)).convert("RGBA")
        else:
            s_icon_raw = None
    except Exception:
        s_icon_raw = None
    try:
        raw_logo = Image.open("assets/discord_logo.png").convert("RGBA")
    except FileNotFoundError:
        raw_logo = None

    try:
        raw_age_icon = Image.open(age_icon_file).convert("RGBA")
    except FileNotFoundError:
        raw_age_icon = None

    banner_frames = getattr(banner_img, "n_frames", 1)
    avatar_frames = getattr(avatar_img, "n_frames", 1)
    is_animated = banner_frames > 1 or avatar_frames > 1

    if is_animated:
        scale = 1.25
    else:
        scale = 3

    base_width, base_height = 310, 450
    width, height = int(base_width * scale), int(base_height * scale)
    banner_h = int(110 * scale)
    avatar_size = int(90 * scale)
    border_size = int(100 * scale)
    margin = int(5 * scale)
    badge_size = int(24 * scale)

    def sync_processing():
        if s_icon_raw:
            s_icon = ImageOps.fit(
                s_icon_raw, (badge_size, badge_size), Image.Resampling.LANCZOS)
            s_mask = Image.new("L", (badge_size, badge_size), 0)
            ImageDraw.Draw(s_mask).ellipse(
                (0, 0, badge_size, badge_size), fill=255)
            server_icon_img = Image.new("RGBA", (badge_size, badge_size))
            server_icon_img.paste(s_icon, (0, 0), s_mask)
        else:
            server_icon_img = Image.new(
                "RGBA", (badge_size, badge_size), (148, 155, 164, 255))

        if raw_logo:
            logo_w, logo_h = raw_logo.size
            discord_logo = raw_logo.resize(
                (int(logo_w * (badge_size / logo_h)), badge_size), Image.Resampling.LANCZOS)
        else:
            discord_logo = Image.new(
                "RGBA", (int(26 * scale), badge_size), (0, 0, 0, 0))
            ImageDraw.Draw(discord_logo).ellipse(
                (int(1 * scale), int(2 * scale), int(25 * scale), int(22 * scale)),
                fill=(148, 155, 164, 255))

        if raw_age_icon:
            a_w, a_h = raw_age_icon.size
            age_icon_img = raw_age_icon.resize(
                (int(a_w * (badge_size / a_h)), badge_size), Image.Resampling.LANCZOS)
        else:
            age_icon_img = Image.new(
                "RGBA", (badge_size, badge_size), (0, 0, 0, 0))
            ImageDraw.Draw(age_icon_img).ellipse(
                (0, 0, badge_size, badge_size), fill=(230, 183, 10, 255))

        banner_target = (width - margin * 2, banner_h)
        banner_cache = []
        banner_durations = []
        for i in range(banner_frames):
            try:
                banner_img.seek(i)
                frame = ImageOps.fit(
                    banner_img.convert("RGBA"), banner_target, Image.Resampling.LANCZOS)
                banner_cache.append(frame)
                banner_durations.append(banner_img.info.get("duration", 50))
            except EOFError:
                break

        avatar_cache = []
        avatar_durations = []
        for i in range(avatar_frames):
            try:
                avatar_img.seek(i)
                avatar_cache.append(
                    avatar_img.convert("RGBA").resize(
                        (avatar_size, avatar_size), Image.Resampling.LANCZOS))
                avatar_durations.append(avatar_img.info.get("duration", 50))
            except EOFError:
                break

        av_mask = Image.new("L", (avatar_size, avatar_size), 0)
        ImageDraw.Draw(av_mask).ellipse(
            (0, 0, avatar_size, avatar_size), fill=255)

        border_template = Image.new("RGBA", (border_size, border_size))
        ImageDraw.Draw(border_template).ellipse(
            (0, 0, border_size, border_size), fill=(43, 45, 49, 255))

        arial_path = "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf"
        try:
            font_title = ImageFont.truetype(arial_path, int(24 * scale))
            font_id = ImageFont.truetype(arial_path, int(18 * scale))
            font_paragraph = ImageFont.truetype(arial_path, int(16 * scale))
            font_details = ImageFont.truetype(arial_path, int(11 * scale))
        except IOError:
            font_title = font_id = font_paragraph = font_details = ImageFont.load_default()

        _d = ImageDraw.Draw(Image.new("RGBA", (width, height)))
        max_text_width = width - int(30 * scale)
        wrapped_lines = []
        current_line = ""
        for word in roast_text.split():
            test = f"{current_line} {word}".strip()
            try:
                lw = _d.textbbox((0, 0), test, font=font_paragraph)[2]
            except AttributeError:
                lw = _d.textlength(test, font=font_paragraph)
            if lw <= max_text_width:
                current_line = test
            else:
                wrapped_lines.append(current_line)
                current_line = word
        if current_line:
            wrapped_lines.append(current_line)

        def text_w(draw, text, font):
            try:
                return draw.textbbox((0, 0), text, font=font)[2]
            except AttributeError:
                return draw.textlength(text, font=font)

        avatar_y = margin + banner_h - (border_size // 2)
        username_y = avatar_y + border_size + int(15 * scale)
        id_y = username_y + int(32 * scale)
        paragraph_y = id_y + int(45 * scale)
        bottom_y_start = height - int(55 * scale)
        text_y_offset = bottom_y_start + badge_size + int(6 * scale)
        col1_center = int(width * 0.23)
        col2_center = int(width * 0.50)
        col3_center = int(width * 0.77)
        avatar_x = (width - border_size) // 2

        u_width = text_w(_d, username, font_title)
        id_width = text_w(_d, user_id, font_id)
        created_width = text_w(_d, created_date, font_details)
        age_width = text_w(_d, age_text, font_details)
        joined_width = text_w(_d, joined_date, font_details)
        line_widths = [text_w(_d, ln, font_paragraph) for ln in wrapped_lines]

        def render_frame(banner_idx, avatar_idx):
            canvas = Image.new("RGBA", (width, height), (43, 45, 49, 255))
            canvas.paste(banner_cache[banner_idx], (margin, margin))
            circular_avatar = Image.new("RGBA", (avatar_size, avatar_size))
            circular_avatar.paste(avatar_cache[avatar_idx], (0, 0), av_mask)

            avatar_border = border_template.copy()
            avatar_border.paste(circular_avatar, (int(
                5 * scale), int(5 * scale)), circular_avatar)
            canvas.paste(avatar_border, (avatar_x, avatar_y), avatar_border)

            draw = ImageDraw.Draw(canvas)

            draw.text(((width - u_width) // 2, username_y),
                      username, fill=(255, 255, 255), font=font_title)
            draw.text(((width - id_width) // 2, id_y),
                      user_id, fill=(148, 155, 164), font=font_id)

            cy = paragraph_y
            for line, lw in zip(wrapped_lines, line_widths):
                draw.text(((width - lw) // 2, cy), line,
                          fill=(218, 222, 225), font=font_paragraph)
                cy += int(20 * scale)

            canvas.paste(discord_logo,
                         (col1_center - discord_logo.size[0] // 2, bottom_y_start), discord_logo)
            canvas.paste(age_icon_img,
                         (col2_center - age_icon_img.size[0] // 2, bottom_y_start), age_icon_img)
            canvas.paste(server_icon_img,
                         (col3_center - server_icon_img.size[0] // 2, bottom_y_start), server_icon_img)

            draw.text((col1_center - created_width // 2, text_y_offset),
                      created_date, fill=(242, 243, 245), font=font_details)
            draw.text((col2_center - age_width // 2, text_y_offset),
                      age_text, fill=(242, 243, 245), font=font_details)
            draw.text((col3_center - joined_width // 2, text_y_offset),
                      joined_date, fill=(242, 243, 245), font=font_details)

            return canvas

        image_binary = io.BytesIO()
        if is_animated:
            import math

            banner_total_ms = sum(banner_durations)
            avatar_total_ms = sum(
                avatar_durations) if avatar_durations else banner_total_ms

            banner_cumul = []
            acc = 0
            for d in banner_durations:
                acc += d
                banner_cumul.append(acc)

            avatar_cumul = []
            acc = 0
            for d in (avatar_durations if avatar_durations else banner_durations):
                acc += d
                avatar_cumul.append(acc)

            all_d = banner_durations + \
                (avatar_durations if avatar_durations else [])
            tick = all_d[0]
            for d in all_d[1:]:
                tick = math.gcd(tick, d)
            tick = max(tick, 20)

            def lcm(a, b):
                return a * b // math.gcd(a, b)
            total_ms = min(lcm(banner_total_ms, avatar_total_ms), 4000)

            frames = []
            output_durations = []
            t = 0
            while t < total_ms:
                bt = t % banner_total_ms
                bf = next((i for i, c in enumerate(banner_cumul) if bt < c),
                          len(banner_cumul) - 1)

                at = t % avatar_total_ms
                af = next((i for i, c in enumerate(avatar_cumul) if at < c),
                          len(avatar_cumul) - 1)

                frames.append(render_frame(bf, af))
                output_durations.append(tick)
                t += tick

            frames[0].save(
                image_binary, format="GIF", save_all=True,
                append_images=frames[1:],
                duration=output_durations,
                loop=0,
                optimize=False
            )
            filename = 'welcome.gif'
        else:
            render_frame(0, 0).save(image_binary, format="PNG", optimize=True)
            filename = 'welcome.png'

        image_binary.seek(0)
        return image_binary, filename

    image_binary, filename = await asyncio.to_thread(sync_processing)
    return discord.File(fp=image_binary, filename=filename)


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue: asyncio.Queue = asyncio.Queue()
        self._worker_started = False

    async def _worker(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            member = await self.queue.get()
            try:
                channel = member.guild.system_channel
                if not channel:
                    continue

                guild_preview = await self.bot.fetch_guild(member.guild.id)
                exact_member_count = guild_preview.approximate_member_count

                welcome_file = await generate_welcome_card(member, self.bot)

                join_unix = int(member.joined_at.timestamp()) if member.joined_at else 0

                welcome_message = (
                    f"Welcome to **{member.guild.name}** {member.mention}! You are **Member #{exact_member_count:,}**.\n"
                    f"**Joined:** <t:{join_unix}:F>"
                )

                await channel.send(content=welcome_message, file=welcome_file)
            except Exception as e:
                logger.error(f"Error processing welcome card in queue background: {e}")
            finally:
                self.queue.task_done()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._worker_started:
            self._worker_started = True
            asyncio.ensure_future(self._worker())

    @app_commands.command(name="test", description="Try welcome banner manually")
    @app_commands.describe(target="Whose welcome banner do you want to see?")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def test(self, interaction: discord.Interaction, target: discord.Member = None):
        await interaction.response.defer()
        target_user = target or interaction.user

        try:
            img_file = await generate_welcome_card(target_user, self.bot)

            join_unix = int(target_user.joined_at.timestamp()) if target_user.joined_at else 0
            welcome_message = (
                f"Welcome to **{target_user.guild.name}** {target_user.mention}! You are **Member #{target_user.guild.member_count:,}**.\n"
                f"**Joined:** <t:{join_unix}:F>"
            )

            await interaction.followup.send(content=welcome_message, file=img_file)

        except Exception as e:
            logger.error(f"Error in slash command processing loop: {e}")
            await interaction.followup.send(
                content="❌ Failed to generate welcome banner structure safely.",
                ephemeral=True
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
