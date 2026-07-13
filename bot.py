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
import sys
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
# 추론 시 참고할 스레드 내부 메시지 수(최신 기준). 스레드가 핵심 근거다.
THREAD_CONTEXT_LIMIT = 30
# !scan 시 채널에서 훑어볼 최근 메시지 수
SCAN_HISTORY_LIMIT = 200

# 배포 확인용: 봇이 기동되면(프로세스당 최초 1회) "정상 기동" 알림을 보낸다.
# 정기 리마인드 시각(09/13/16시)까지 기다리지 않고 배포 직후 동작을 바로 확인할 수 있다.
# 우선순위: 웹훅(STARTUP_NOTIFY_WEBHOOK) → 채널(STARTUP_NOTIFY_CHANNEL_ID) → 로그만.
# 웹훅은 채널 전송 권한/인텐트 없이도 동작해서 배포 확인용으로 가장 안전하다.
STARTUP_NOTIFY_WEBHOOK = os.environ.get("STARTUP_NOTIFY_WEBHOOK", "")
STARTUP_NOTIFY_CHANNEL_ID = os.environ.get("STARTUP_NOTIFY_CHANNEL_ID", "")

# 선택: Gemini 무료 티어로 리마인드에 '맥락 추론'을 덧붙인다.
# 근처 메시지를 그대로 보여주는 대신, 대화 흐름으로부터 "무엇을 왜 처리해야 하는지"를 추론한다.
# GEMINI_API_KEY 가 있고 USE_LLM_SUMMARY 가 참이면 활성화된다(.env 로 끌 수 있음).
# 키 발급: https://aistudio.google.com/apikey (무료 티어, 카드 불필요)
# 주의: 무료 티어 입력은 Google 모델 학습에 사용될 수 있음.
USE_LLM_SUMMARY = os.environ.get("USE_LLM_SUMMARY", "true").lower() in ("1", "true", "yes")
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

# on_ready 는 재접속마다 다시 호출될 수 있으므로, 기동 알림은 프로세스당 1회만 보낸다.
_startup_notified = False


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


async def send_thread_opening_notice(
    thread: discord.Thread,
    message: discord.Message,
    item: "TrackedMessage",
) -> None:
    """
    스레드를 갓 만든 직후, 사이드바의 '활성 스레드'에 바로 뜨도록 첫 메시지를 보낸다.
    (스레드는 메시지가 하나도 없으면 목록에 노출되지 않는다.)
    """
    urgent = item.is_urgent
    embed = discord.Embed(
        title=("🔥 급한 미완료 작업으로 등록됨" if urgent else "⭐ 미완료 작업으로 등록됨"),
        description=(
            "이 건을 여기서 챙길게요. 완료되면 원본 메시지에 ⚡️ 를 달아주세요.\n"
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
    embed.set_footer(text="정기 리마인드는 정해진 시각(09/13/16시, KST)에 이 스레드로 전달됩니다.")
    try:
        await thread.send(embed=embed)
    except discord.HTTPException as exc:
        log.warning("스레드 개설 알림 전송 실패 (thread %s): %s", thread.id, exc)


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

    return "\n".join(lines) if lines else "_(맥락을 불러오지 못했습니다)_"


def clip_tail(text: str, limit: int) -> str:
    """임베드 필드 길이 제한에 맞춰 자르되, 최신 내용(뒤쪽)을 남긴다."""
    if len(text) <= limit:
        return text
    return "…\n" + text[-(limit - 2):]


async def build_thread_context(item: TrackedMessage) -> str:
    """
    이 건의 스레드 안에서 오간 '사람' 대화를 모은다. 추론의 핵심 근거.
    - 봇이 남긴 리마인드/안내 메시지는 제외(노이즈·자기참조 방지)
    - 빈 내용(임베드만 있는 메시지 등)도 제외
    """
    if item.thread_id is None:
        return ""
    lines: list[str] = []
    try:
        thread = await resolve_channel(item.thread_id)
        msgs = [m async for m in thread.history(limit=THREAD_CONTEXT_LIMIT)]
        msgs.sort(key=lambda m: m.created_at)
        for m in msgs:
            if bot.user and m.author.id == bot.user.id:
                continue
            content = (m.content or "").strip().replace("\n", " ")
            if not content:
                continue
            snippet = content[:200] + ("…" if len(content) > 200 else "")
            lines.append(f"**{m.author.display_name}**: {snippet}")
    except discord.HTTPException as exc:
        log.warning("스레드 맥락 수집 실패 (thread %s): %s", item.thread_id, exc)
    return "\n".join(lines)


async def infer_task_with_llm(thread_text: str, channel_text: str) -> Optional[str]:
    """
    Gemini 로 '무엇을/왜 처리해야 하는지'를 추론한다.
    스레드 대화가 있으면 그것을 최우선 근거로, 채널 주변 맥락은 보조로 삼는다.
    """
    if not (USE_LLM_SUMMARY and os.environ.get("GEMINI_API_KEY")):
        return None
    # 스레드도 없고 채널 맥락도 못 불러왔으면 추론 불가
    if not thread_text and channel_text.startswith("_("):
        return None
    try:
        from google import genai  # 지연 임포트: 미설치여도 봇은 동작
    except ImportError:
        return None

    parts = []
    if thread_text:
        parts.append(
            "[이 건의 스레드 대화 — 가장 중요한 근거. 실제 논의·진행상황이다]\n" + thread_text
        )
    else:
        parts.append("[이 건의 스레드 대화 — 없음]")
    parts.append("[채널 주변 맥락 — 보조 참고용]\n" + channel_text)
    material = "\n\n".join(parts)

    try:
        client = genai.Client()  # 환경변수 GEMINI_API_KEY 사용
        resp = await client.aio.models.generate_content(
            model=LLM_MODEL,
            contents=(
                "너는 다정한 업무 리마인드 도우미야. 아래 자료를 보고 담당자가 어떤 일을 하려던 건지 "
                "정리해줘.\n"
                "**스레드 대화가 있으면 그걸 가장 중요한 근거로 삼아** — 이 건의 실제 논의와 진행상황이 "
                "담겨 있어. 채널 주변 맥락은 스레드가 비었을 때만 보조로 참고해.\n"
                "- 문장을 그대로 복붙하지 말고 맥락으로 의도를 해석할 것\n"
                "- 무엇을/왜/지금까지 진행된 부분/남은 부분을 '상황 설명'하듯 부드럽게 한국어 2~4문장\n"
                "- 재촉하거나 압박하는 표현('빨리', '서둘러', '빠른 시일 내에', '~해야 합니다' 등)은 쓰지 말 것\n"
                "- 톤은 유머러스하고 적당히 귀엽게, 맨 끝에 짧은 응원 한마디로 마무리(예: 화이팅! 🌱)\n"
                "- 근거가 부족하면 '근거가 조금 부족해요' 정도로 부드럽게 밝히고 추정을 덧붙일 것\n"
                "추론 결과만 출력:\n\n"
                + material
            ),
        )
        return (getattr(resp, "text", "") or "").strip() or None
    except Exception as exc:  # 네트워크/인증 등 실패해도 리마인드는 계속
        log.warning("Gemini 추론 실패: %s", exc)
        return None


async def send_reminder(item: TrackedMessage) -> bool:
    """해당 건의 스레드로 리마인드를 보낸다. 실제로 전송했으면 True."""
    if not item.is_active or item.thread_id is None:
        return False
    try:
        thread = await resolve_channel(item.thread_id)
        source_channel = await resolve_channel(item.channel_id)
        message = await source_channel.fetch_message(item.message_id)
    except (discord.NotFound, discord.HTTPException) as exc:
        log.warning("리마인드 대상 조회 실패 (message %s): %s", item.message_id, exc)
        return False

    # 스레드 대화가 핵심 근거. 스레드에 사람 대화가 있으면 그것만 쓰고,
    # 비어 있을 때만(주로 스레드 갓 생성됨) 아래 주변 메시지로 폴백한다.
    thread_context = await build_thread_context(item)
    if thread_context:
        channel_context = ""  # 스레드가 있으면 주변 채널 조회 자체를 생략
        display_context = thread_context
        display_name = "🧵 스레드 내용"
    else:
        channel_context = await build_context(message)
        display_context = channel_context
        display_name = "맥락(주변 메시지)"
    inference = await infer_task_with_llm(thread_context, channel_context)
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
    if inference:  # LLM 이 맥락으로부터 추론한 '해야 할 일'
        embed.add_field(name="🤖 AI 맥락 추론", value=inference[:1000], inline=False)
    embed.add_field(name=display_name, value=clip_tail(display_context, 1000), inline=False)
    footer = "완료되면 원본 메시지에 ⚡️ 를 달아주세요."
    if inference:
        footer += " · AI 추론은 참고용입니다."
    embed.set_footer(text=footer)

    try:
        await thread.send(embed=embed)
        item.last_reminded = datetime.now(timezone.utc)
        return True
    except discord.HTTPException as exc:
        log.warning("리마인드 전송 실패 (thread %s): %s", item.thread_id, exc)
        return False


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
    await send_startup_notice()


async def send_startup_notice() -> None:
    """배포 직후 봇이 실제로 연결·전송까지 되는지 확인하기 위한 1회성 기동 알림."""
    global _startup_notified
    if _startup_notified:
        return
    _startup_notified = True  # 재접속으로 on_ready 가 또 불려도 중복 전송 안 함

    times = ", ".join(f"{t.hour:02d}:{t.minute:02d}" for t in DAILY_REMIND_TIMES)
    log.info(
        "기동 완료: 서버 %d개, 추적 %d건, 정기 리마인드 %s (KST)",
        len(bot.guilds), len(tracked), times,
    )

    embed = discord.Embed(
        title="✅ 리마인드 봇 기동 완료",
        description=(
            f"봇이 정상적으로 로그인·연결되었습니다.\n"
            f"정기 리마인드 예정 시각: **{times}** (KST)\n"
            f"현재 추적 중인 미완료 건: **{len(tracked)}**개"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="이 메시지가 보이면 봇이 배포·연결까지 정상입니다.")

    # 1순위: 웹훅 (권한/인텐트 불필요, 배포 확인에 가장 안전)
    if STARTUP_NOTIFY_WEBHOOK:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                webhook = discord.Webhook.from_url(STARTUP_NOTIFY_WEBHOOK, session=session)
                await webhook.send(embed=embed, username="리마인드 봇")
            log.info("기동 알림 웹훅 전송 완료.")
        except Exception as exc:  # 웹훅 실패해도 봇 본 기능은 계속
            log.warning("기동 알림 웹훅 전송 실패: %s", exc)
        return

    # 2순위: 채널 ID
    if STARTUP_NOTIFY_CHANNEL_ID:
        try:
            channel = await resolve_channel(int(STARTUP_NOTIFY_CHANNEL_ID))
            await channel.send(embed=embed)
            log.info("기동 알림 전송 완료 (channel %s)", STARTUP_NOTIFY_CHANNEL_ID)
        except (ValueError, discord.NotFound, discord.HTTPException) as exc:
            log.warning("기동 알림 채널 전송 실패 (%s): %s", STARTUP_NOTIFY_CHANNEL_ID, exc)
        return

    log.info("기동 알림 대상 미설정 → 로그로만 확인.")


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
        newly_created = item.thread_id is None  # 이번에 처음 스레드를 붙이는가
        thread = await get_or_create_thread(message)
        if thread is not None:
            item.thread_id = thread.id
            # 스레드는 메시지가 하나라도 있어야 사이드바의 '활성 스레드'에 뜬다.
            # 방금 새로 만든 경우에만 첫 메시지를 보내 활성 상태로 노출시킨다.
            if newly_created:
                await send_thread_opening_notice(thread, message, item)
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


def jump_url_for(item: TrackedMessage) -> str:
    """추가 조회 없이 ID 만으로 원본 메시지로 가는 점프 링크를 만든다."""
    return (
        f"https://discord.com/channels/"
        f"{item.guild_id}/{item.channel_id}/{item.message_id}"
    )


async def describe_item(item: TrackedMessage) -> str:
    """미완료 건을 사람이 알아볼 수 있게: 태그 + 내용 미리보기 + 원본 링크."""
    tag = "🔥" if item.is_urgent else "⭐"
    try:
        channel = await resolve_channel(item.channel_id)
        message = await channel.fetch_message(item.message_id)
        author = message.author.display_name
        content = (message.content or "").strip().replace("\n", " ")
        if content:
            preview = f"**{author}**: {content[:80]}" + ("…" if len(content) > 80 else "")
        else:
            preview = f"**{author}**: _(내용 없음)_"
    except (discord.NotFound, discord.HTTPException):
        preview = "_(메시지를 불러오지 못함)_"
    return f"{tag} {preview}\n    → [원본 열기]({jump_url_for(item)})"


def active_items_for(ctx: commands.Context) -> list[TrackedMessage]:
    """이 명령이 실행된 서버의 미완료 건들(서버 밖이면 전체)."""
    items = [i for i in tracked.values() if i.is_active]
    if ctx.guild is not None:
        items = [i for i in items if i.guild_id == ctx.guild.id]
    return items


@bot.command(name="pending")
async def pending(ctx: commands.Context):
    """현재 추적 중인 미완료 건 목록을 보여준다."""
    active = active_items_for(ctx)
    if not active:
        await ctx.send("현재 미완료 건이 없습니다. ✨")
        return
    lines = [await describe_item(i) for i in active[:25]]
    text = "**미완료 목록**\n" + "\n".join(lines)
    if len(active) > 25:
        text += f"\n… 외 {len(active) - 25}건"
    await ctx.send(text)


@bot.command(name="remind")
async def remind(ctx: commands.Context):
    """정기 시각을 기다리지 않고 지금 당장 모든 미완료 건에 리마인드를 보낸다."""
    active = active_items_for(ctx)
    if not active:
        await ctx.send("지금 리마인드할 미완료 건이 없습니다. ✨")
        return

    await ctx.send(f"🔔 미완료 {len(active)}건에 대해 지금 리마인드를 보냅니다…")
    sent = 0
    skipped = 0
    for item in active:
        if await send_reminder(item):
            sent += 1
        else:
            # 스레드가 아직 없거나(스레드 생성 실패) 원본을 못 찾은 경우
            skipped += 1

    msg = f"✅ 리마인드 전송 완료: {sent}건 (각 건의 스레드로 전송)"
    if skipped:
        msg += f"\n⚠️ {skipped}건은 스레드가 없거나 원본을 찾지 못해 건너뛰었습니다."
    await ctx.send(msg)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("환경변수 DISCORD_BOT_TOKEN 을 설정해주세요.")
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        # 토큰이 잘못됐을 때: 재시작해도 계속 실패하므로 명확히 로그를 남기고 종료한다.
        # (systemd StartLimit 이 곧 재시작을 멈추지만, 여기서도 무의미한 재시도를 막는다)
        log.error(
            "로그인 실패: 토큰이 올바르지 않습니다. Discord 개발자 포털에서 토큰을 "
            "재발급해 .env 의 DISCORD_BOT_TOKEN 을 갱신하세요. (재시작해도 해결되지 않음)"
        )
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        # message_content 는 특권 인텐트라 포털에서 명시적으로 켜야 한다. 역시 영구 오류.
        log.error(
            "특권 인텐트 미허용: 개발자 포털(Bot → Privileged Gateway Intents)에서 "
            "'Message Content Intent' 를 켜세요. (재시작해도 해결되지 않음)"
        )
        sys.exit(1)
