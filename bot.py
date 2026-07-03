"""
이모지 기반 할 일 리마인드 디스코드 봇
-----------------------------------
- ⭐️  : 확인은 했지만 추후 작업 필요 (미완료)
- ⚡️  : 작업 완료
- 🔥  : 급한 건

동작
- ⭐️ 있고 ⚡️ 없는 메시지 → 스레드 생성(없으면) 후 그 스레드에서 리마인드
- 메시지가 이미 스레드 안에 있으면 → 그 스레드에서 리마인드
- 🔥 붙은 급한 건 → 자주 리마인드 / 나머지 → 하루 1회
- 리마인드 시 원본 메시지 + 위아래 맥락을 함께 보여줌
- ⚡️ 가 붙으면 완료 처리하고 리마인드 중단

상태는 메모리에만 저장한다(봇 재시작 시 초기화). 재시작 후에는
채널에서 `!scan` 을 실행하면 최근 메시지의 ⭐️/⚡️ 를 다시 읽어들인다.
"""

import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
load_dotenv()  # 같은 디렉터리의 .env 파일에서 환경변수를 읽어온다
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# 이모지 (변형 선택자 U+FE0F 는 비교 시 무시한다)
STAR = "⭐"
LIGHTNING = "⚡"
FIRE = "🔥"

KST = ZoneInfo("Asia/Seoul")

# 정기 리마인드 시각들 (급하지 않은 모든 미완료 건). KST 기준.
DAILY_REMIND_TIMES = [
    time(hour=9, minute=0, tzinfo=KST),
    time(hour=13, minute=0, tzinfo=KST),
    time(hour=16, minute=0, tzinfo=KST),
]
# 🔥 급한 건 반복 주기(시간)
URGENT_REMIND_INTERVAL_HOURS = 2
# 새로 만든 스레드 자동 보관까지의 시간(분). 60/1440/4320/10080 중 하나.
THREAD_AUTO_ARCHIVE_MINUTES = 10080  # 7일
# 리마인드에 함께 보여줄 앞뒤 맥락 메시지 수
CONTEXT_MESSAGE_COUNT = 4
# !scan 시 채널에서 훑어볼 최근 메시지 수
SCAN_HISTORY_LIMIT = 200

# 선택: Gemini 무료 티어로 맥락을 한두 문장 요약해서 보여주기.
# GEMINI_API_KEY 환경변수가 있고 아래가 True 이면 활성화된다.
# 키 발급: https://aistudio.google.com/apikey (무료 티어, 카드 불필요)
# 주의: 무료 티어 입력은 Google 모델 학습에 사용될 수 있음.
USE_LLM_SUMMARY = False
LLM_MODEL = "gemini-2.5-flash"  # 무료 티어 지원 모델(Flash 계열)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("remind-bot")

# ---------------------------------------------------------------------------
# 봇 인스턴스
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # 맥락 읽기용 (개발자 포털에서 활성화 필요)
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------------------------------------------------------------------------
# 상태
# ---------------------------------------------------------------------------
@dataclass
class TrackedMessage:
    message_id: int
    channel_id: int          # 원본 메시지가 있는 채널(스레드일 수도 있음)
    guild_id: int
    thread_id: Optional[int] = None   # 리마인드를 보낼 스레드
    has_star: bool = False
    has_lightning: bool = False
    is_urgent: bool = False
    last_reminded: Optional[datetime] = field(default=None)

    @property
    def is_active(self) -> bool:
        # ⭐️ 있고 ⚡️ 없는 건만 리마인드 대상
        return self.has_star and not self.has_lightning


# message_id -> TrackedMessage
tracked: dict[int, TrackedMessage] = {}


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------
def normalize_emoji(raw: str) -> str:
    """변형 선택자(U+FE0F)를 제거해 ⭐ 과 ⭐️ 를 같게 취급한다."""
    return raw.replace("\uFE0F", "")


def emoji_kind(raw: str) -> Optional[str]:
    e = normalize_emoji(raw)
    if e == STAR:
        return "star"
    if e == LIGHTNING:
        return "lightning"
    if e == FIRE:
        return "fire"
    return None


def make_thread_name(message: discord.Message) -> str:
    content = (message.content or "").strip().replace("\n", " ")
    if not content:
        content = f"{message.author.display_name} 의 메시지"
    name = f"📌 {content}"
    return name[:90] if len(name) > 90 else name


async def resolve_channel(channel_id: int):
    ch = bot.get_channel(channel_id)
    if ch is None:
        ch = await bot.fetch_channel(channel_id)
    return ch


async def get_or_create_thread(message: discord.Message) -> Optional[discord.Thread]:
    """
    - 메시지가 이미 스레드 안에 있으면 그 스레드를 반환
    - 아니면 메시지에 스레드를 만들어(또는 이미 있으면 재사용) 반환
    """
    channel = message.channel
    if isinstance(channel, discord.Thread):
        return channel

    if message.thread is not None:  # 이 메시지로 이미 만든 스레드가 있으면 재사용
        return message.thread

    try:
        return await message.create_thread(
            name=make_thread_name(message),
            auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
        )
    except discord.HTTPException as exc:
        log.warning("스레드 생성 실패 (message %s): %s", message.id, exc)
        return None


async def build_context(message: discord.Message) -> str:
    """원본 메시지 주변(위아래) 메시지를 모아 짧은 맥락 텍스트로 만든다."""
    lines: list[str] = []
    try:
        around = [
            m async for m in message.channel.history(
                around=message, limit=CONTEXT_MESSAGE_COUNT * 2 + 1
            )
        ]
        around.sort(key=lambda m: m.created_at)
        for m in around:
            content = (m.content or "").strip().replace("\n", " ")
            if not content:
                continue
            marker = "👉 " if m.id == message.id else "   "
            snippet = content[:120] + ("…" if len(content) > 120 else "")
            lines.append(f"{marker}**{m.author.display_name}**: {snippet}")
    except discord.HTTPException as exc:
        log.warning("맥락 수집 실패 (message %s): %s", message.id, exc)

    text = "\n".join(lines) if lines else "_(맥락을 불러오지 못했습니다)_"

    if USE_LLM_SUMMARY and os.environ.get("GEMINI_API_KEY"):
        summary = await summarize_with_llm(text)
        if summary:
            text = f"**요약:** {summary}\n\n{text}"
    return text


async def summarize_with_llm(context_text: str) -> Optional[str]:
    """선택 기능: Gemini 무료 티어로 '무슨 건인지' 한두 문장 요약."""
    try:
        from google import genai  # 지연 임포트: 미설치여도 봇은 동작
    except ImportError:
        return None
    try:
        client = genai.Client()  # 환경변수 GEMINI_API_KEY 사용
        resp = await client.aio.models.generate_content(
            model=LLM_MODEL,
            contents=(
                "다음 디스코드 대화 맥락에서 '처리해야 할 일'이 무엇인지 "
                "한국어로 한두 문장으로 요약해줘. 사족 없이 요약만:\n\n"
                + context_text
            ),
        )
        return (getattr(resp, "text", "") or "").strip() or None
    except Exception as exc:  # 네트워크/인증 등 실패해도 리마인드는 계속
        log.warning("Gemini 요약 실패: %s", exc)
        return None


async def send_reminder(item: TrackedMessage) -> None:
    if not item.is_active or item.thread_id is None:
        return
    try:
        thread = await resolve_channel(item.thread_id)
        source_channel = await resolve_channel(item.channel_id)
        message = await source_channel.fetch_message(item.message_id)
    except (discord.NotFound, discord.HTTPException) as exc:
        log.warning("리마인드 대상 조회 실패 (message %s): %s", item.message_id, exc)
        return

    context = await build_context(message)
    urgent = item.is_urgent

    embed = discord.Embed(
        title=("🔥 급한 미완료 작업" if urgent else "⭐ 미완료 작업 리마인드"),
        description=(
            f"아직 완료(⚡️)되지 않은 건입니다.\n"
            f"[원본 메시지로 이동]({message.jump_url})"
        ),
        color=discord.Color.red() if urgent else discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    original = (message.content or "").strip() or "_(내용 없음)_"
    embed.add_field(
        name="원본",
        value=f"**{message.author.display_name}**: {original[:1000]}",
        inline=False,
    )
    embed.add_field(name="맥락", value=context[:1000], inline=False)
    embed.set_footer(text="완료되면 원본 메시지에 ⚡️ 를 달아주세요.")

    try:
        await thread.send(embed=embed)
        item.last_reminded = datetime.now(timezone.utc)
    except discord.HTTPException as exc:
        log.warning("리마인드 전송 실패 (thread %s): %s", item.thread_id, exc)


# ---------------------------------------------------------------------------
# 이벤트
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("로그인됨: %s (%s)", bot.user, bot.user.id if bot.user else "?")
    if not urgent_reminder_loop.is_running():
        urgent_reminder_loop.start()
    if not daily_reminder_loop.is_running():
        daily_reminder_loop.start()


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id:
        return
    kind = emoji_kind(str(payload.emoji))
    if kind is None or payload.guild_id is None:
        return

    try:
        channel = await resolve_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.HTTPException) as exc:
        log.warning("반응 대상 조회 실패: %s", exc)
        return

    item = tracked.get(payload.message_id)
    if item is None:
        item = TrackedMessage(
            message_id=payload.message_id,
            channel_id=payload.channel_id,
            guild_id=payload.guild_id,
        )
        tracked[payload.message_id] = item

    if kind == "star":
        item.has_star = True
        thread = await get_or_create_thread(message)
        if thread is not None:
            item.thread_id = thread.id
        log.info("⭐ 등록: message %s", payload.message_id)

    elif kind == "lightning":
        item.has_lightning = True
        log.info("⚡ 완료: message %s", payload.message_id)
        if item.thread_id is not None:
            try:
                thread = await resolve_channel(item.thread_id)
                await thread.send("✅ **완료 처리되었습니다.** 리마인드를 중단합니다.")
            except discord.HTTPException:
                pass

    elif kind == "fire":
        item.is_urgent = True
        log.info("🔥 급함: message %s", payload.message_id)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    kind = emoji_kind(str(payload.emoji))
    if kind is None:
        return
    item = tracked.get(payload.message_id)
    if item is None:
        return

    if kind == "star":
        item.has_star = False
    elif kind == "lightning":
        item.has_lightning = False   # 완료 취소 → 다시 리마인드 대상
    elif kind == "fire":
        item.is_urgent = False


# ---------------------------------------------------------------------------
# 리마인드 루프
# ---------------------------------------------------------------------------
@tasks.loop(hours=URGENT_REMIND_INTERVAL_HOURS)
async def urgent_reminder_loop():
    for item in list(tracked.values()):
        if item.is_active and item.is_urgent:
            await send_reminder(item)


@urgent_reminder_loop.before_loop
async def _before_urgent():
    await bot.wait_until_ready()


@tasks.loop(time=DAILY_REMIND_TIMES)
async def daily_reminder_loop():
    for item in list(tracked.values()):
        if item.is_active:   # 급한 건 포함 모든 미완료 건 정해진 시각마다
            await send_reminder(item)


@daily_reminder_loop.before_loop
async def _before_daily():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# 명령어
# ---------------------------------------------------------------------------
@bot.command(name="scan")
async def scan(ctx: commands.Context):
    """현재 채널의 최근 메시지에서 ⭐️/⚡️/🔥 를 다시 읽어 등록한다(재시작 후 복구용)."""
    found = 0
    async for message in ctx.channel.history(limit=SCAN_HISTORY_LIMIT):
        kinds = set()
        for reaction in message.reactions:
            k = emoji_kind(str(reaction.emoji))
            if k:
                kinds.add(k)
        if "star" not in kinds:
            continue

        item = tracked.get(message.id) or TrackedMessage(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=ctx.guild.id if ctx.guild else 0,
        )
        item.has_star = True
        item.has_lightning = "lightning" in kinds
        item.is_urgent = "fire" in kinds
        if item.is_active and item.thread_id is None:
            thread = await get_or_create_thread(message)
            if thread is not None:
                item.thread_id = thread.id
        tracked[message.id] = item
        if item.is_active:
            found += 1

    await ctx.send(f"🔎 스캔 완료. 미완료 건 {found}개를 등록했습니다.")


@bot.command(name="pending")
async def pending(ctx: commands.Context):
    """현재 추적 중인 미완료 건 목록을 보여준다."""
    active = [i for i in tracked.values() if i.is_active]
    if not active:
        await ctx.send("현재 미완료 건이 없습니다. ✨")
        return
    lines = []
    for i in active:
        tag = "🔥" if i.is_urgent else "⭐"
        lines.append(f"{tag} message `{i.message_id}` (thread `{i.thread_id}`)")
    await ctx.send("**미완료 목록**\n" + "\n".join(lines[:25]))


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("환경변수 DISCORD_BOT_TOKEN 을 설정해주세요.")
    bot.run(TOKEN)
