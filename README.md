# 이모지 리마인드 디스코드 봇

메시지에 붙는 이모지로 할 일을 추적하고, 스레드에서 리마인드합니다.

| 이모지 | 의미 |
| --- | --- |
| ⭐️ | 확인했고 추후 작업 필요 (미완료) |
| ⚡️ | 작업 완료 |
| 🔥 | 급한 건 |

## 동작 요약
- ⭐️ 가 붙고 ⚡️ 가 없는 메시지 → 스레드를 만들고(이미 스레드 안이면 그 스레드에서) 리마인드
- 🔥 가 붙으면 **자주**(기본 2시간마다), 나머지는 **정해진 시각마다**(기본 KST 09시·13시·16시) 리마인드
- 리마인드에는 원본 메시지 + 위아래 맥락 + 이동 링크가 함께 표시됨
- ⚡️ 가 붙으면 완료 처리하고 리마인드를 멈춤 (⚡️ 를 떼면 다시 리마인드)

## 준비
1. Discord 개발자 포털에서 봇을 만들고, **Privileged Gateway Intents** 중
   `MESSAGE CONTENT INTENT` 를 켠다 (맥락을 읽기 위해 필요).
2. 봇 초대 시 권한: `View Channels`, `Send Messages`, `Read Message History`,
   `Create Public Threads`, `Send Messages in Threads`.

## 설치 & 실행
```bash
pip install -r requirements.txt
echo 'DISCORD_BOT_TOKEN=봇토큰' > .env   # 같은 디렉터리의 .env 에서 읽어옴
python bot.py
```
> `.env` 는 `.gitignore` 에 포함되어 커밋되지 않습니다.
> 환경변수(`export DISCORD_BOT_TOKEN=...`)로 직접 넣어도 동작합니다.

## 명령어
- `!scan` : 현재 채널의 최근 메시지에서 이모지를 다시 읽어 등록 (봇 재시작 후 복구용)
- `!pending` : 추적 중인 미완료 건 목록 보기

## 설정 (bot.py 상단)
- `DAILY_REMIND_TIMES` : 정기 리마인드 시각들 (기본 KST 09:00, 13:00, 16:00)
- `URGENT_REMIND_INTERVAL_HOURS` : 🔥 급한 건 반복 주기 (기본 2시간)
- `CONTEXT_MESSAGE_COUNT` : 함께 보여줄 앞뒤 맥락 메시지 수
- `USE_LLM_SUMMARY` : `True` + `GEMINI_API_KEY` 설정 시, Gemini 가 "무슨 건인지"
  한두 문장으로 요약해 리마인드에 덧붙임 (선택 기능)

### 요약 기능(Gemini 무료 티어) 켜기
1. https://aistudio.google.com/apikey 에서 API 키 발급 (카드 불필요, 무료 티어)
2. `pip install google-genai`
3. `.env` 에 키를 추가하고 토글 설정 후 실행:
   ```bash
   echo 'GEMINI_API_KEY=발급받은키' >> .env
   ```
   그리고 `bot.py` 상단에서 `USE_LLM_SUMMARY = True` 로 변경.

> ⚠️ 무료 티어는 입력 내용이 Google 모델 학습에 사용될 수 있습니다.
> 메시지에 민감한 내용이 있으면 유료 티어/Vertex AI 를 쓰거나 이 기능을 끄세요
> (꺼도 원본 + 위아래 맥락은 그대로 표시됩니다).

## 참고 / 한계
- 상태는 **메모리에만** 저장됩니다. 봇을 재시작하면 추적 상태가 사라지므로,
  `!scan` 으로 복구하세요. 영구 저장이 필요해지면 `tracked` 딕셔너리를
  SQLite 등으로 바꾸면 됩니다.
